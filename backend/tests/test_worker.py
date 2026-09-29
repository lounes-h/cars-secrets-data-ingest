import signal
import threading
import time

import psycopg

from ingest import worker as worker_module
from ingest.pipeline import Outcome
from ingest.worker import Worker

from .conftest import read_sample, write_csv
from .test_pricing import save_model


def wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_offer_routes_by_provider_folder_and_deduplicates(conn, settings):
    worker = Worker(settings)
    header, rows = read_sample()
    good = write_csv(settings.inbox_dir / "copart" / "a.csv", header, rows[:1])
    partial = write_csv(settings.inbox_dir / "copart" / "b.csv.part", header, rows[:1])
    stray = write_csv(settings.inbox_dir / "c.csv", header, rows[:1])

    for path in (good, good, partial, stray, stray):
        worker.offer(path)

    assert worker.queue.qsize() == 1
    alerts = conn.execute("SELECT type, severity FROM alerts").fetchall()
    assert alerts == [{"type": "unroutable_file", "severity": "warning"}]  # once, not twice


def test_worker_ingests_files_dropped_into_the_inbox(conn, settings, api_url):
    settings.inbox_rescan_seconds = 1
    settings.api_database_url = api_url
    worker = Worker(settings)
    header, rows = read_sample()
    # present before start (picked up by the startup scan)
    write_csv(settings.inbox_dir / "copart" / "before.csv", header, rows[:100])
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: conn.execute(
            "SELECT count(*) AS n FROM ingest_files WHERE status = 'succeeded'"
        ).fetchone()["n"] == 1)
        # dropped while running, written under a temp name then renamed
        tmp = write_csv(settings.inbox_dir / "copart" / "after.csv.part", header, rows)
        tmp.rename(tmp.with_name("after.csv"))
        assert wait_for(lambda: conn.execute(
            "SELECT count(*) AS n FROM ingest_files WHERE status = 'succeeded'"
        ).fetchone()["n"] == 2)
        assert wait_for(lambda: conn.execute(
            "SELECT count(*) AS n FROM worker_heartbeat").fetchone()["n"] == 1)
        # each ingested file is copied to the API database without waiting for the timer
        assert wait_for(lambda: api_lot_count(api_url) == 300)
    finally:
        worker.stop.set()
        thread.join(timeout=10)
    assert conn.execute("SELECT count(*) AS n FROM lots").fetchone()["n"] == 300
    assert not list((settings.inbox_dir / "copart").glob("*.csv"))


def api_lot_count(api_url):
    try:
        with psycopg.connect(api_url) as c:
            return c.execute("SELECT count(*) FROM lots").fetchone()[0]
    except psycopg.errors.UndefinedTable:  # before the first sync
        return 0


class FakeConn:
    closed = False

    def execute(self, *_args):
        return None

    def close(self):
        self.closed = True


def test_unreachable_database_leaves_the_file_for_the_next_rescan(settings, monkeypatch):
    worker = Worker(settings)
    path = settings.inbox_dir / "copart" / "a.csv"
    worker._pending.add(path)
    worker.queue.put(path)

    def down(_url):
        worker.stop.set()  # ends the loop instead of waiting 5 s to reconnect
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(worker_module, "connect", down)
    worker.process_loop()
    assert path not in worker._pending  # offered again by the next rescan


def test_a_crashing_file_does_not_stop_the_worker(settings, monkeypatch):
    worker = Worker(settings)
    conn = FakeConn()
    monkeypatch.setattr(worker_module, "connect", lambda _url: conn)
    processed = []

    def crash(_conn, path):
        processed.append(path)
        if len(processed) == 2:
            worker.stop.set()
        raise RuntimeError("bad file")

    monkeypatch.setattr(worker, "_process", crash)
    for name in ("a.csv", "b.csv"):
        worker.queue.put(settings.inbox_dir / "copart" / name)
    worker.process_loop()
    assert len(processed) == 2 and conn.closed


def test_a_file_that_vanished_or_is_still_growing_is_skipped(conn, settings, monkeypatch):
    worker = Worker(settings)
    calls = []
    monkeypatch.setattr(worker_module, "process_file", lambda *a: calls.append(a))
    worker._process(conn, settings.inbox_dir / "copart" / "gone.csv")
    assert calls == []


def test_a_file_that_could_not_be_archived_is_not_ingested_again(conn, settings, monkeypatch):
    worker = Worker(settings)
    header, rows = read_sample()
    path = write_csv(settings.inbox_dir / "copart" / "a.csv", header, rows[:1])
    # loaded, but archiving "failed"
    monkeypatch.setattr(worker_module, "process_file", lambda *a: Outcome(1, "succeeded"))
    worker._process(conn, path)
    assert worker.state == "idle"
    assert worker.sync_requested.is_set()  # the API database is updated right away
    worker.offer(path)
    assert worker.queue.empty()


def test_heartbeat_survives_database_and_archive_errors(settings, monkeypatch):
    worker = Worker(settings)

    def down(_url):
        raise psycopg.OperationalError("connection refused")

    def purge_fails(*_args):
        worker.stop.set()
        raise OSError("disk")

    monkeypatch.setattr(worker_module, "connect", down)
    monkeypatch.setattr(worker_module.files, "purge_archive", purge_fails)
    worker.heartbeat_loop()  # returns once stopped, having logged both errors


def test_heartbeat_beats_and_purges_old_archives(settings, monkeypatch):
    worker = Worker(settings)
    conn = FakeConn()
    purged = []

    def purge(*args):
        purged.append(args)
        worker.stop.set()
        return 2

    monkeypatch.setattr(worker_module, "connect", lambda _url: conn)
    monkeypatch.setattr(worker_module.files, "purge_archive", purge)
    worker.heartbeat_loop()
    assert purged == [(settings.archive_dir, settings.archive_retention_days)]


def test_a_failing_rescan_is_retried_on_the_next_round(settings, monkeypatch):
    settings.inbox_rescan_seconds = 0.01
    worker = Worker(settings)
    rounds = []

    def rescan():
        rounds.append(1)
        if len(rounds) == 2:
            worker.stop.set()
        raise OSError("inbox unmounted")

    monkeypatch.setattr(worker, "rescan", rescan)
    worker.rescan_loop()
    assert len(rounds) == 2


def test_main_stops_the_worker_on_sigterm(settings, monkeypatch):
    handlers = {}
    workers = []
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "configure_logging", lambda: None)
    monkeypatch.setattr(worker_module.signal, "signal",
                        lambda sig, fn: handlers.setdefault(sig, fn))
    monkeypatch.setattr(Worker, "run", lambda self: workers.append(self))
    worker_module.main()
    handlers[signal.SIGTERM]()
    assert workers[0].stop.is_set()


def test_sync_api_copies_lots_and_alerts_until_it_works_again(conn, settings, db_url, api_url):
    worker = Worker(settings)
    header, rows = read_sample()
    worker._process(conn, write_csv(settings.inbox_dir / "copart" / "a.csv", header, rows))
    worker.sync_api()  # no API database configured: nothing to do

    settings.api_database_url = db_url  # misconfigured: refused and alerted, nothing copied
    worker.sync_api()
    alert = conn.execute("SELECT * FROM alerts WHERE type = 'api_sync_failed'").fetchone()
    assert alert["severity"] == "warning" and alert["resolved_at"] is None
    assert "points at the ingest database" in alert["title"]

    settings.api_database_url = api_url
    worker.sync_api()
    assert api_lot_count(api_url) == 300
    assert conn.execute("SELECT resolved_at FROM alerts WHERE id = %s",
                        (alert["id"],)).fetchone()["resolved_at"] is not None


def test_sync_api_prices_lots_and_alerts_while_the_model_is_broken(
        conn, settings, api_url):
    worker = Worker(settings)
    header, rows = read_sample()
    worker._process(conn, write_csv(settings.inbox_dir / "copart" / "a.csv", header, rows))
    model = settings.price_model_path
    model.parent.mkdir(parents=True)
    model.write_text("not a model")

    worker.sync_api()  # no API database: lots are still priced (here: fail to be)

    alert = conn.execute("SELECT * FROM alerts WHERE type = 'pricing_failed'").fetchone()
    assert alert["severity"] == "warning" and alert["resolved_at"] is None

    settings.api_database_url = api_url
    worker.sync_api()  # a broken model never holds up the sync
    assert api_lot_count(api_url) == 300

    save_model(conn, model)
    worker.sync_api()
    with psycopg.connect(api_url) as c:
        assert c.execute("SELECT count(predicted_final_price) FROM lots").fetchone()[0] == 300
    assert conn.execute("SELECT resolved_at FROM alerts WHERE id = %s",
                        (alert["id"],)).fetchone()["resolved_at"] is not None


def test_sync_api_survives_an_unreachable_ingest_database(settings, monkeypatch):
    settings.api_database_url = "postgresql://nowhere/api"
    worker = Worker(settings)

    def down(_url):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(worker_module, "connect", down)
    worker.sync_api()  # logged; the next round retries


def test_sync_loop_runs_at_start_on_request_and_on_its_timer(settings, monkeypatch):
    worker = Worker(settings)
    settings.api_sync_interval_seconds = 3600
    calls = []

    def sync_api():
        calls.append(1)
        if len(calls) == 1:
            worker.sync_requested.set()  # a file was ingested meanwhile: no hour-long wait
        else:
            worker.stop.set()
            worker.sync_requested.set()  # ends the wait

    monkeypatch.setattr(worker, "sync_api", sync_api)
    started = time.monotonic()
    worker.sync_loop()
    assert len(calls) == 2 and time.monotonic() - started < 5

    worker.stop.clear()
    settings.api_sync_interval_seconds = 0.01
    calls.clear()
    monkeypatch.setattr(worker, "sync_api", lambda: calls.append(1) or (
        len(calls) == 3 and worker.stop.set()))
    worker.sync_loop()  # nothing requested: the timer alone drives it
    assert len(calls) == 3


def test_analysis_loop_runs_requests_and_survives_errors(settings, monkeypatch):
    settings.analysis_poll_seconds = 0.01
    worker = Worker(settings)
    conns = []
    outcomes = [True, True, psycopg.OperationalError("gone"), RuntimeError("bug"), False]

    def process_next(conn, _settings):
        outcome = outcomes.pop(0)
        if not outcomes:
            worker.stop.set()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def connect(_url):
        conns.append(FakeConn())
        return conns[-1]

    monkeypatch.setattr(worker_module, "connect", connect)
    monkeypatch.setattr(worker_module.vehicle_analysis, "process_next", process_next)
    worker.analysis_loop()
    assert outcomes == []
    # reconnected after the database error; the last connection is closed on stop
    assert len(conns) == 2 and all(c.closed for c in conns)
