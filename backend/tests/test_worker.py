import threading
import time

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
