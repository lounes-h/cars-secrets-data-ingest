"""Alert storage and periodic checks against a real PostgreSQL."""

from ingest.alerts import AlertSpec, raise_alert, resolve_fingerprint
from ingest.monitor import run_checks


def open_alerts(conn):
    return {r["fingerprint"]: r for r in conn.execute(
        "SELECT * FROM alerts WHERE resolved_at IS NULL")}


def test_same_condition_bumps_occurrences_instead_of_duplicating(conn):
    spec = AlertSpec("warning", "t", "title", "fp")
    first = raise_alert(conn, spec)
    second = raise_alert(conn, AlertSpec("critical", "t", "worse", "fp"))
    assert first == second
    row = open_alerts(conn)["fp"]
    assert (row["occurrences"], row["severity"], row["title"]) == (2, "critical", "worse")


def test_resolved_condition_opens_a_new_alert_next_time(conn):
    spec = AlertSpec("warning", "t", "title", "fp")
    first = raise_alert(conn, spec)
    assert resolve_fingerprint(conn, "fp") == 1
    assert resolve_fingerprint(conn, "fp") == 0
    assert raise_alert(conn, spec) != first


def test_worker_down_raised_and_auto_resolved(conn, settings):
    run_checks(conn, settings)
    assert open_alerts(conn)["worker_down"]["title"].startswith("Ingest worker has never")

    conn.execute("""INSERT INTO worker_heartbeat (worker_id, started_at, last_beat_at, state)
                    VALUES ('w', now(), now(), 'idle')""")
    run_checks(conn, settings)
    assert "worker_down" not in open_alerts(conn)

    conn.execute("UPDATE worker_heartbeat SET last_beat_at = now() - interval '10 minutes'")
    run_checks(conn, settings)
    assert open_alerts(conn)["worker_down"]["severity"] == "critical"


def test_no_new_file_alert_per_enabled_provider(conn, settings):
    conn.execute("""INSERT INTO worker_heartbeat (worker_id, started_at, last_beat_at, state)
                    VALUES ('w', now() - interval '3 hours', now(), 'idle')""")
    run_checks(conn, settings)
    assert open_alerts(conn)["no_new_file:copart"]["severity"] == "critical"

    conn.execute("""INSERT INTO ingest_files (source, filename, sha256, status, finished_at)
                    VALUES ('copart', 'a.csv', 'x', 'succeeded', now() - interval '10 minutes')""")
    run_checks(conn, settings)
    assert "no_new_file:copart" not in open_alerts(conn)

    conn.execute("UPDATE ingest_files SET finished_at = now() - interval '76 minutes'")
    run_checks(conn, settings)
    assert "no_new_file:copart" in open_alerts(conn)

    # an unchanged export (skipped as duplicate) still counts as the feed being alive
    conn.execute("""INSERT INTO ingest_files (source, filename, sha256, status, finished_at)
                    VALUES ('copart', 'b.csv', 'x', 'duplicate', now())""")
    run_checks(conn, settings)
    assert "no_new_file:copart" not in open_alerts(conn)


def test_downloader_status_rules():
    from datetime import UTC, datetime, timedelta

    from ingest.monitor import downloader_status

    now = datetime(2026, 9, 18, 4, 0, tzinfo=UTC)
    fresh = {"heartbeat_at": now - timedelta(seconds=40), "activity": "idle"}
    assert downloader_status(None, now, 150) == "not_installed"
    assert downloader_status(fresh, now, 150) == "scheduled"
    assert downloader_status({**fresh, "activity": "downloading"}, now, 150) == "downloading"
    assert downloader_status({**fresh, "run_requested_at": now}, now, 150) == "requested"
    assert downloader_status({**fresh, "needs_attention": "login_required"}, now,
                             150) == "paused"
    assert downloader_status({"heartbeat_at": now - timedelta(minutes=3)}, now,
                             150) == "not_running"
    assert downloader_status({"heartbeat_at": None}, now, 150) == "not_running"


def test_downloader_down_alert_raised_and_auto_resolved(conn, settings):
    run_checks(conn, settings)
    assert "downloader_down:copart" not in open_alerts(conn)  # not installed: no alert

    conn.execute("""INSERT INTO downloader_status (source, heartbeat_at)
                    VALUES ('copart', now() - interval '10 minutes')""")
    run_checks(conn, settings, after_sleep=True)
    assert "downloader_down:copart" not in open_alerts(conn)  # just woke up: give it time

    run_checks(conn, settings)
    alert = open_alerts(conn)["downloader_down:copart"]
    assert alert["severity"] == "critical" and "not running since" in alert["title"]

    conn.execute("UPDATE downloader_status SET heartbeat_at = now()")
    run_checks(conn, settings, after_sleep=True)  # resolving is always allowed
    assert "downloader_down:copart" not in open_alerts(conn)
