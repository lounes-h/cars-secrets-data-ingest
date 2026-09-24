import asyncio
import contextlib
import dataclasses
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ingest.api import app as app_module
from ingest.api import auth
from ingest.api.app import EventBroker, TTLCache, create_app, diff_attributes, monitor_loop
from ingest.db import EVENTS_CHANNEL
from ingest.pipeline import process_file
from ingest.providers import COPART, PROVIDERS, LotImage, LotImages

from .conftest import read_sample
from .test_pipeline import auctions_over


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


@pytest.mark.parametrize("path", ["/api/overview", "/api/runs", "/api/runs/1/rows?kind=new",
                                  "/api/alerts", "/api/schema",
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
    assert detail["run"]["rows_new"] == 300
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


def test_run_rows_page_through_new_updated_and_rejected(authed, conn, settings, make_export):
    header, rows = read_sample()
    first = process_file(conn, make_export(rows), COPART, settings)

    def page(file_id, **params):
        r = authed.get(f"/api/runs/{file_id}/rows", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    numbers = sorted(row[header.index("Lot number")] for row in rows)
    new = page(first.file_id, kind="new", limit=2)
    assert (new["total"], new["available"], new["reason"]) == (300, True, None)
    assert [item["lot_number"] for item in new["items"]] == numbers[:2]
    assert new["items"][0]["vin"] and "attributes" not in new["items"][0]
    assert [item["lot_number"] for item in
            page(first.file_id, kind="new", limit=2, offset=2)["items"]] == numbers[2:4]
    assert page(first.file_id, kind="new", limit=2, offset=300)["items"] == []
    assert page(first.file_id, kind="updated")["items"] == []

    changed = [list(r) for r in rows]
    changed[0][header.index("High Bid =non-vix,Sealed=Vix")] = "4242.0"
    second = process_file(conn, make_export(changed), COPART, settings)
    updated = page(second.file_id, kind="updated")
    assert updated["total"] == 1
    assert ([item["lot_number"] for item in updated["items"]]
            == [changed[0][header.index("Lot number")]])
    assert page(second.file_id, kind="new")["items"] == []

    # The same lot twice in one file: the earlier row is rejected, with the reason.
    third = process_file(conn, make_export([*changed, changed[5]]), COPART, settings)
    rejected = page(third.file_id, kind="rejected")
    assert rejected["total"] == 1 and rejected["reason"] is None
    assert rejected["items"][0]["reason"].startswith("duplicate lot number")
    assert rejected["items"][0]["raw"]

    assert authed.get("/api/runs/99999/rows", params={"kind": "new"}).status_code == 404
    assert authed.get(f"/api/runs/{first.file_id}/rows", params={"kind": "nope"}).status_code == 422


def test_run_rows_list_removed_lots_with_the_reason(authed, conn, settings, make_export):
    header, rows = read_sample()
    rows = auctions_over(rows)
    process_file(conn, make_export(rows), COPART, settings)
    outcome = process_file(conn, make_export(rows[2:]), COPART, settings)
    expected = sorted(row[header.index("Lot number")] for row in rows[:2])

    def removed(**params):
        return authed.get(f"/api/runs/{outcome.file_id}/rows",
                          params={"kind": "removed", **params}).json()

    page = removed()
    assert page["total"] == len(expected) and page["available"]
    assert [item["lot_number"] for item in page["items"]] == expected
    assert all(item["removed_at"] for item in page["items"])
    assert "grace period" in page["reason"]
    assert not set(expected) & {row[header.index("Id")] for row in rows[:2]}
    assert [item["lot_number"] for item in removed(limit=1)["items"]] == expected[:1]
    assert [item["lot_number"] for item in removed(limit=1, offset=1)["items"]] == expected[1:]
    for lot in expected:
        assert authed.get(f"/api/lots/copart/{lot}").status_code == 200

    # Older runs can be recovered when all removals still belong to this run's interval.
    conn.execute("UPDATE ingest_files SET removed_lot_numbers = NULL WHERE id = %s",
                 (outcome.file_id,))
    assert [item["lot_number"] for item in removed()["items"]] == expected

    # An overlapping run makes attribution ambiguous: don't guess from the timestamps.
    overlap = conn.execute(
        """INSERT INTO ingest_files (source, filename, sha256, status, started_at, finished_at)
           SELECT source, 'overlap.csv', 'overlap', status, started_at, finished_at
           FROM ingest_files WHERE id = %s RETURNING id""", (outcome.file_id,),
    ).fetchone()["id"]
    ambiguous = removed()
    assert (ambiguous["items"], ambiguous["total"], ambiguous["available"]) == ([], 2, False)
    conn.execute("DELETE FROM ingest_files WHERE id = %s", (overlap,))

    # Reappearing lots lose their removal timestamp, so a partial list isn't complete.
    conn.execute("UPDATE lots SET removed_at = NULL WHERE lot_number = %s", (expected[0],))
    assert removed()["available"] is False


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


def test_retry_refuses_a_missing_run_an_unknown_provider_and_a_file_already_queued(
        authed, conn, settings, make_export):
    assert authed.post("/api/runs/999/retry").status_code == 404

    kept = settings.failed_dir / "iaai" / "1_x.csv"
    kept.parent.mkdir(parents=True)
    kept.write_text("x")
    file_id = conn.execute(
        """INSERT INTO ingest_files (source, filename, sha256, status, archived_path)
           VALUES ('iaai', 'x.csv', 'x', 'failed', %s) RETURNING id""", (str(kept),),
    ).fetchone()["id"]
    r = authed.post(f"/api/runs/{file_id}/retry")
    assert r.status_code == 409 and "unknown provider" in r.json()["detail"]

    path = make_export(header=["nothing", "useful"], rows=[["1", "2"]], name="bad.csv")
    outcome = process_file(conn, path, COPART, settings)
    make_export(header=["nothing", "useful"], rows=[["1", "2"]], name="bad.csv")  # dropped again
    r = authed.post(f"/api/runs/{outcome.file_id}/retry")
    assert r.status_code == 409 and "already waiting" in r.json()["detail"]


def test_alert_and_lot_filters(authed, conn, settings, make_export):
    header, rows = read_sample()
    process_file(conn, make_export(rows), COPART, settings)
    alerts = authed.get("/api/alerts", params={"state": "all", "severity": "critical"})
    assert alerts.status_code == 200
    assert len(authed.get("/api/lots", params={"state": "active", "limit": 5}).json()["items"]) == 5
    assert authed.get("/api/lots", params={"state": "removed"}).json()["items"] == []
    make = rows[0][header.index("Make")]
    found = authed.get("/api/lots", params={"q": f" {make} "}).json()["items"]
    assert found and all(lot["make"] == make for lot in found)


def test_lot_images_of_a_provider_without_a_photo_api(authed, conn, settings, make_export,
                                                     monkeypatch):
    header, rows = read_sample()
    process_file(conn, make_export(rows), COPART, settings)
    monkeypatch.setitem(PROVIDERS, "copart", dataclasses.replace(COPART, fetch_images=None))
    lot_number = rows[0][header.index("Lot number")]
    assert authed.get(f"/api/lots/copart/{lot_number}/images").json() == {
        "images": [], "complete": False}


def test_app_factory_reads_the_environment(settings, monkeypatch):
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    assert app_module.app_factory().title == "Ingest dashboard API"


def test_background_tasks_start_and_stop_with_the_app(conn, settings):
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/health").status_code == 200


def test_broker_fans_out_and_drops_events_for_a_full_stream():
    broker = EventBroker()
    fast, full = asyncio.Queue(), asyncio.Queue(maxsize=1)
    full.put_nowait("old")
    broker.subscribers |= {fast, full}
    broker.publish("new")
    assert fast.get_nowait() == "new"
    assert full.get_nowait() == "old" and full.empty()


def test_broker_relays_database_notifications(conn, db_url):
    async def scenario():
        broker = EventBroker()
        queue = asyncio.Queue()
        broker.subscribers.add(queue)
        task = asyncio.create_task(broker.listen(db_url))
        try:
            while queue.empty():  # until LISTEN is in place
                conn.execute("SELECT pg_notify(%s, %s)", (EVENTS_CHANNEL, "hello"))
                await asyncio.sleep(0.05)
            return await queue.get()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert asyncio.run(asyncio.wait_for(scenario(), 20)) == "hello"


def test_broker_reconnects_after_a_failure(monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        raise asyncio.CancelledError  # stop after the first reconnect pause

    monkeypatch.setattr(app_module, "asyncio", SimpleNamespace(
        sleep=sleep, CancelledError=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(EventBroker().listen("postgresql://nobody@127.0.0.1:1/none"))
    assert waits == [3]


def test_monitor_checks_every_round_survives_errors_and_notices_sleep(settings, monkeypatch):
    seen, waits = [], []
    clock = iter([0.0, 10.0, 10_000.0])

    def checks(_conn, _settings, after_sleep):
        seen.append(after_sleep)
        if len(seen) == 1:
            raise RuntimeError("database hiccup")

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise asyncio.CancelledError

    class Pool:
        @contextlib.contextmanager
        def connection(self):
            yield None

    def mail(_conn, _settings):
        mailed.append(True)
        raise OSError("smtp unreachable")  # logged; the next round still runs

    mailed = []
    monkeypatch.setattr(app_module, "run_checks", checks)
    monkeypatch.setattr(app_module, "email_critical_alerts", mail)
    monkeypatch.setattr(app_module, "time", SimpleNamespace(time=lambda: next(clock)))
    monkeypatch.setattr(app_module, "asyncio", SimpleNamespace(
        sleep=sleep, to_thread=asyncio.to_thread, CancelledError=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor_loop(Pool(), settings))
    assert seen == [False, False, True]  # a gap of over three rounds means the machine slept
    assert waits == [settings.monitor_interval_seconds] * 3
    assert mailed == [True, True]  # not after the round whose checks failed


def test_event_stream_relays_events_and_keeps_alive_until_the_browser_leaves(settings,
                                                                            monkeypatch):
    brokers = []

    class Capturing(EventBroker):
        def __init__(self):
            super().__init__()
            brokers.append(self)

    real_wait_for = asyncio.wait_for

    async def quick_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, 0.05)

    monkeypatch.setattr(app_module, "EventBroker", Capturing)
    monkeypatch.setattr(app_module.asyncio, "wait_for", quick_wait_for)
    app = create_app(settings, background=False)
    app.dependency_overrides[auth.current_user] = lambda: "admin"

    async def scenario():
        """One browser on the stream, over plain ASGI: it leaves after the first keep-alive."""
        left = asyncio.Event()
        chunks = []
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await left.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"].decode())
                if len(chunks) == 1:
                    brokers[0].publish('{"type":"run_finished"}')
                if chunks[-1].startswith(": keep-alive"):
                    left.set()

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "GET", "scheme": "http", "path": "/api/events",
                 "raw_path": b"/api/events", "root_path": "", "query_string": b"",
                 "headers": [], "client": ("test", 1), "server": ("test", 80)}
        await real_wait_for(app(scope, receive, send), 10)
        return chunks

    chunks = asyncio.run(scenario())
    assert chunks[:3] == [
        "retry: 3000\n\n", 'data: {"type":"run_finished"}\n\n', ": keep-alive\n\n"]
    assert brokers[0].subscribers == set()
