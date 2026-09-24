"""Alert storage and periodic checks against a real PostgreSQL."""

import pytest

from ingest import alerts
from ingest.alerts import AlertSpec, email_critical_alerts, raise_alert, resolve_fingerprint
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


def age_alerts(conn, minutes=5):
    conn.execute("UPDATE alerts SET created_at = now() - make_interval(mins => %s)", (minutes,))


def test_critical_alerts_are_mailed_once_in_one_message(conn, settings):
    settings = settings.model_copy(update={"smtp_host": "smtp.test", "alert_email_to": "me@x.test"})
    sent = []
    raise_alert(conn, AlertSpec("critical", "worker_down", "Worker silent", "a", {"n": 1}),
                source="copart")
    raise_alert(conn, AlertSpec("critical", "no_new_file", "No file", "b"))
    raise_alert(conn, AlertSpec("warning", "slow_ingest", "Slow", "c"))
    resolve_fingerprint(conn, "b")
    raise_alert(conn, AlertSpec("critical", "ingest_failed", "Load failed", "d"))
    age_alerts(conn)
    raise_alert(conn, AlertSpec("critical", "fresh", "Just opened", "e"))  # may clear by itself

    def send(_settings, subject, body):
        sent.append((subject, body))

    assert email_critical_alerts(conn, settings, send) == 2
    [(subject, body)] = sent
    assert subject == "[Cars ingest] 2 critical alerts: Worker silent"
    assert "Worker silent\n  worker_down, source copart" in body and '{"n": 1}' in body
    assert "Load failed" in body
    assert not {"No file", "Slow", "Just opened"} & set(body.split("\n"))

    assert email_critical_alerts(conn, settings, send) == 0  # already mailed
    raise_alert(conn, AlertSpec("critical", "worker_down", "Worker silent", "a"))
    assert email_critical_alerts(conn, settings, send) == 0  # repeat of a mailed alert

    raise_alert(conn, AlertSpec("critical", "slow_ingest", "Very slow", "c"))  # escalated
    age_alerts(conn)
    assert email_critical_alerts(conn, settings, send) == 2
    assert sent[-1][0] == "[Cars ingest] 2 critical alerts: Very slow"


def test_alert_mail_is_retried_after_a_failed_send(conn, settings):
    settings = settings.model_copy(update={"smtp_host": "smtp.test", "alert_email_to": "me@x.test"})
    raise_alert(conn, AlertSpec("critical", "t", "Down", "a"))
    age_alerts(conn)

    def broken(*_):
        raise OSError("smtp unreachable")

    with pytest.raises(OSError):
        email_critical_alerts(conn, settings, broken)
    subjects = []
    assert email_critical_alerts(conn, settings, lambda _s, subj, _b: subjects.append(subj)) == 1
    assert subjects == ["[Cars ingest] Down"]


def test_alerts_are_not_mailed_without_smtp_or_recipient(conn, settings):
    raise_alert(conn, AlertSpec("critical", "t", "Down", "a"))
    age_alerts(conn)
    for update in ({}, {"smtp_host": "smtp.test"}, {"alert_email_to": "me@x.test"}):
        assert email_critical_alerts(conn, settings.model_copy(update=update),
                                     lambda *_: pytest.fail("mailed")) == 0


def test_send_mail_uses_starttls_and_login(settings, monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def starttls(self):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, message):
            calls.append(("send", message["To"], message["Subject"], message.get_content()))

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    settings = settings.model_copy(update={
        "smtp_host": "smtp.test", "smtp_port": 2525, "alert_email_to": "me@x.test",
        "smtp_username": "u", "smtp_password": "p"})
    alerts.send_mail(settings, "Bad\nfile.csv", "Body")
    assert calls == [("connect", "smtp.test", 2525), ("starttls",), ("login", "u", "p"),
                     ("send", "me@x.test", "Bad file.csv", "Body\n")]

    calls.clear()
    alerts.send_mail(settings.model_copy(update={"smtp_username": ""}), "S", "B")
    assert [c[0] for c in calls] == ["connect", "starttls", "send"]  # no login for a relay
