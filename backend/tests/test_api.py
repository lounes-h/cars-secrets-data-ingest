import dataclasses

import pytest
from fastapi.testclient import TestClient

from ingest.api.app import TTLCache, create_app, diff_attributes
from ingest.pipeline import process_file
from ingest.providers import COPART, PROVIDERS, LotImage, LotImages

from .conftest import read_sample


@pytest.fixture
def client(conn, settings):
    with TestClient(create_app(settings, background=False)) as c:
        yield c


@pytest.fixture
def authed(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
    assert r.status_code == 200
    return client


def test_health_is_public(client):
    assert client.get("/api/health").json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/api/overview", "/api/runs", "/api/alerts", "/api/schema",
                                  "/api/lots", "/api/auth/me", "/api/events"])
def test_everything_else_requires_login(client, path):
    assert client.get(path).status_code == 401


def test_login_sets_http_only_strict_cookie(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/auth/me").json() == {"username": "admin"}
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_login_rejects_bad_password_and_rate_limits(client):
    for _ in range(10):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
    assert r.status_code == 429


def test_forged_cookie_is_rejected(client):
    client.cookies.set("ingest_session", "forged.token.value")
    assert client.get("/api/overview").status_code == 401


def test_overview_runs_and_lots(authed, conn, settings, make_export):
    header, rows = read_sample()
    first = process_file(conn, make_export(rows), COPART, settings)
    changed = [list(r) for r in rows]
    changed[0][header.index("High Bid =non-vix,Sealed=Vix")] = "4242.0"
    process_file(conn, make_export(changed), COPART, settings)

    overview = authed.get("/api/overview").json()
    assert overview["lots"]["active"] == 300
    assert overview["lots"]["changed_24h"] == 1
    (provider,) = overview["providers"]
    assert provider["source"] == "copart" and provider["display_name"] == "Copart"
    assert provider["last_run"]["rows_updated"] == 1
    assert overview["health"] == "down"  # no worker heartbeat in tests

    runs = authed.get("/api/runs").json()
    assert runs["total"] == 2 and runs["items"][0]["rows_updated"] == 1
    assert authed.get("/api/runs", params={"status": "failed"}).json()["total"] == 0
    detail = authed.get(f"/api/runs/{first.file_id}").json()
    assert detail["run"]["rows_new"] == 300 and detail["rejected_rows"] == []
    assert authed.get("/api/runs/99999").status_code == 404

    lot_number = rows[0][header.index("Lot number")]
    found = authed.get("/api/lots", params={"q": lot_number}).json()["items"]
    assert [lot["lot_number"] for lot in found] == [lot_number]
    assert authed.get("/api/lots", params={"q": "acu"}).json()["items"]
    assert authed.get("/api/lots", params={"q": "%"}).json()["items"] == []  # LIKE escaped

    lot = authed.get(f"/api/lots/copart/{lot_number}").json()
    assert lot["lot"]["listing_url"] == f"https://www.copart.com/lot/{lot_number}"
    (change,) = lot["history"]
    assert {"field": "high_bid_non_vix_sealed_vix", "old": "0", "new": "4242"} in change["changes"]
    assert authed.get("/api/lots/copart/0").status_code == 404

    schema = authed.get("/api/schema").json()["items"]
    assert len(schema) == 59


def test_alert_ack_and_resolve(authed, conn):
    conn.execute("""INSERT INTO alerts (severity, type, title, fingerprint)
                    VALUES ('warning', 't', 'something', 'fp')""")
    alert_id = authed.get("/api/alerts").json()["items"][0]["id"]
    acked = authed.post(f"/api/alerts/{alert_id}/ack").json()
    assert acked["acked_by"] == "admin"
    resolved = authed.post(f"/api/alerts/{alert_id}/resolve").json()
    assert resolved["resolved_by"] == "admin"
    assert authed.get("/api/alerts").json()["total"] == 0
    assert authed.get("/api/alerts", params={"state": "resolved"}).json()["total"] == 1
    assert authed.post(f"/api/alerts/{alert_id}/resolve").status_code == 404


def test_retry_moves_failed_file_back_to_inbox(authed, conn, settings, make_export):
    path = make_export(header=["nothing", "useful"], rows=[["1", "2"]], name="bad.csv")
    outcome = process_file(conn, path, COPART, settings)
    assert outcome.status == "failed"

    r = authed.post(f"/api/runs/{outcome.file_id}/retry")
    assert r.status_code == 202
    assert (settings.inbox_dir / "copart" / "bad.csv").exists()
    # the file is gone from failed/, a second retry has nothing to move
    assert authed.post(f"/api/runs/{outcome.file_id}/retry").status_code == 409


def test_retry_refuses_non_failed_runs(authed, conn, settings, make_export):
    outcome = process_file(conn, make_export(), COPART, settings)
    assert authed.post(f"/api/runs/{outcome.file_id}/retry").status_code == 409


def test_retry_refuses_paths_outside_failed_dir(authed, conn, settings, tmp_path):
    outside = tmp_path / "secret.csv"
    outside.write_text("x")
    file_id = conn.execute(
        """INSERT INTO ingest_files (source, filename, sha256, status, archived_path)
           VALUES ('copart', 'secret.csv', 'x', 'failed', %s) RETURNING id""", (str(outside),),
    ).fetchone()["id"]
    assert authed.post(f"/api/runs/{file_id}/retry").status_code == 409
    assert outside.exists()


def test_diff_attributes():
    assert diff_attributes({"a": "1", "b": "2"}, {"a": "1", "b": "3", "c": "4"}) == [
        {"field": "b", "old": "2", "new": "3"}, {"field": "c", "old": None, "new": "4"},
    ]


def test_lot_images_are_fetched_once_and_cached(authed, conn, settings, make_export, monkeypatch):
    header, rows = read_sample()
    process_file(conn, make_export(rows), COPART, settings)
    seen = []

    def fake_fetch(lot):
        seen.append(dict(lot))
        return LotImages([LotImage(thumb="https://cs.copart.com/a_thb.jpg",
                                   full="https://cs.copart.com/a_ful.jpg")], complete=False)

    monkeypatch.setitem(PROVIDERS, "copart", dataclasses.replace(COPART, fetch_images=fake_fetch))
    lot_number = rows[0][header.index("Lot number")]
    for _ in range(2):
        body = authed.get(f"/api/lots/copart/{lot_number}/images").json()
        assert body == {"images": [{"thumb": "https://cs.copart.com/a_thb.jpg",
                                    "full": "https://cs.copart.com/a_ful.jpg", "hd": None}],
                        "complete": False}
    assert len(seen) == 1
    # the provider only ever gets the URLs stored for the lot
    assert seen[0]["image_url"].startswith("http://inventoryv2.copart.io/v1/lotImages/")
    assert authed.get("/api/lots/copart/0/images").status_code == 404


def test_lot_images_require_login(client):
    assert client.get("/api/lots/copart/1/images").status_code == 401


def test_ttl_cache_expires_and_is_bounded():
    now = [0.0]
    cache = TTLCache(ttl_seconds=10, max_items=2, clock=lambda: now[0])
    cache.put("a", 1)
    assert cache.get("a") == 1
    now[0] = 11
    assert cache.get("a") is None
    cache.put("b", 2)
    cache.put("c", 3)
    cache.put("d", 4)
    assert cache.get("b") is None and cache.get("d") == 4


def test_downloader_status_and_download_now(authed, conn):
    body = authed.get("/api/downloader").json()
    assert (body["status"], body["installed"]) == ("not_installed", False)
    assert authed.post("/api/downloader/run").status_code == 409

    conn.execute("""INSERT INTO downloader_status (source, heartbeat_at, activity, next_run_at)
                    VALUES ('copart', now(), 'idle', now() + interval '50 minutes')""")
    body = authed.get("/api/downloader").json()
    assert body["status"] == "scheduled" and body["next_run_at"]

    r = authed.post("/api/downloader/run")
    assert r.status_code == 202 and r.json()["status"] == "requested"
    assert conn.execute("SELECT run_requested_by FROM downloader_status"
                        ).fetchone()["run_requested_by"] == "admin"

    conn.execute("UPDATE downloader_status SET activity = 'downloading'")
    assert authed.get("/api/downloader").json()["status"] == "downloading"
    assert authed.post("/api/downloader/run").status_code == 409

    conn.execute("UPDATE downloader_status SET needs_attention = 'challenge'")
    assert authed.get("/api/downloader").json()["status"] == "paused"
    assert "log in" in authed.post("/api/downloader/run").json()["detail"]

    conn.execute("""UPDATE downloader_status SET needs_attention = NULL, activity = 'idle',
                    heartbeat_at = now() - interval '10 minutes'""")
    assert authed.get("/api/downloader").json()["status"] == "not_running"


def test_overview_reports_downloader_and_counts_it_in_health(authed, conn):
    conn.execute("""INSERT INTO worker_heartbeat (worker_id, started_at, last_beat_at, state)
                    VALUES ('w', now(), now(), 'idle')""")
    overview = authed.get("/api/overview").json()
    assert overview["downloader"]["status"] == "not_installed"
    assert overview["health"] == "healthy"  # not set up is not a failure of the pipeline

    conn.execute("""INSERT INTO downloader_status (source, heartbeat_at, activity)
                    VALUES ('copart', now(), 'idle')""")
    assert authed.get("/api/overview").json()["health"] == "healthy"
    conn.execute("UPDATE downloader_status SET heartbeat_at = now() - interval '1 hour'")
    overview = authed.get("/api/overview").json()
    assert overview["downloader"]["status"] == "not_running"
    assert overview["health"] == "down"


def test_downloader_status_requires_login(client):
    assert client.get("/api/downloader").status_code == 401
    assert client.post("/api/downloader/run").status_code == 401
