import signal
import threading
import time

import psycopg

from ingest import worker as worker_module
from ingest.worker import Worker

from .conftest import read_sample, write_csv


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


def test_worker_ingests_files_dropped_into_the_inbox(conn, settings):
    settings.inbox_rescan_seconds = 1
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
    finally:
        worker.stop.set()
        thread.join(timeout=10)
    assert conn.execute("SELECT count(*) AS n FROM lots").fetchone()["n"] == 300
    assert not list((settings.inbox_dir / "copart").glob("*.csv"))


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
    monkeypatch.setattr(worker_module, "process_file", lambda *a: None)  # archiving "failed"
    worker._process(conn, path)
    assert worker.state == "idle"
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
