"""Long-running worker: watches inbox/<provider>/ and ingests each complete CSV, one at a time.

Filesystem events give low latency; a periodic rescan catches anything the events missed
(Docker bind mounts on macOS, files present at startup, a restart mid-download)."""

from __future__ import annotations

import os
import queue
import signal
import socket
import threading
import time
from pathlib import Path

import psycopg
import structlog
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import files
from .alerts import AlertSpec, raise_alert
from .config import Settings, get_settings
from .db import connect
from .log_config import configure_logging
from .pipeline import mark_interrupted, process_file
from .providers import PROVIDERS

log = structlog.get_logger()


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self.queue: queue.Queue[Path] = queue.Queue()
        self._pending: set[Path] = set()
        self._done: dict[Path, tuple[int, int]] = {}  # processed but still present (archive failed)
        self._warned: set[Path] = set()
        self._lock = threading.Lock()
        self.stop = threading.Event()
        self.state = "idle"

    # --- discovery -------------------------------------------------------------------------
    def offer(self, path: Path) -> None:
        if not files.is_candidate(path) or not path.is_file():
            return
        inbox = self.settings.inbox_dir
        source = files.provider_of(path, inbox)
        if source not in PROVIDERS:
            self._warn_unroutable(path)
            return
        with self._lock:
            if path in self._pending or self._done.get(path) == files.signature(path):
                return
            self._pending.add(path)
        self.queue.put(path)

    def rescan(self) -> None:
        inbox = self.settings.inbox_dir
        candidates = [p for p in inbox.rglob("*") if p.is_file()]
        for path in sorted(candidates, key=lambda p: p.stat().st_mtime):
            self.offer(path)

    def _warn_unroutable(self, path: Path) -> None:
        if path in self._warned:
            return
        self._warned.add(path)
        known = ", ".join(sorted(PROVIDERS))
        log.warning("inbox.unroutable", file=str(path))
        with connect(self.settings.database_url) as conn:
            raise_alert(conn, AlertSpec(
                "warning", "unroutable_file",
                f"{path.name} is not in a provider folder; it will not be ingested",
                f"unroutable_file:{path}",
                {"path": str(path), "hint": f"move it to inbox/<provider>/ (one of: {known})"},
            ))

    # --- loops -------------------------------------------------------------------------------
    def process_loop(self) -> None:
        conn = None
        while not self.stop.is_set():
            try:
                path = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if conn is None or conn.closed:
                    conn = connect(self.settings.database_url)
                self._process(conn, path)
            except psycopg.OperationalError:
                # Database unreachable: the file stays in the inbox and the rescan retries it.
                log.exception("db.unavailable", file=str(path))
                if conn is not None:
                    conn.close()
                conn = None
                self.stop.wait(5)
            except Exception:  # noqa: BLE001 - one bad file must not stop the worker
                log.exception("process.crashed", file=str(path))
            finally:
                with self._lock:
                    self._pending.discard(path)
        if conn is not None:
            conn.close()

    def _process(self, conn, path: Path) -> None:
        if not files.wait_until_stable(path, self.settings.file_stable_seconds):
            log.info("inbox.not_ready", file=str(path))  # vanished or still downloading
            return
        provider = PROVIDERS[files.provider_of(path, self.settings.inbox_dir)]
        self.state = f"processing {path.name}"
        try:
            process_file(conn, path, provider, self.settings)
        finally:
            self.state = "idle"
        if path.exists():  # archiving failed; don't re-ingest the same bytes every rescan
            with self._lock:
                self._done[path] = files.signature(path)

    def heartbeat_loop(self) -> None:
        started = time.time()
        last_purge = 0.0
        conn = None
        while not self.stop.is_set():
            try:
                if conn is None or conn.closed:
                    conn = connect(self.settings.database_url)
                conn.execute(
                    """INSERT INTO worker_heartbeat (worker_id, started_at, last_beat_at, state)
                       VALUES (%s, to_timestamp(%s), now(), %s)
                       ON CONFLICT (worker_id) DO UPDATE
                       SET last_beat_at = now(), state = EXCLUDED.state""",
                    (self.worker_id, started, self.state),
                )
            except psycopg.Error:  # keep trying; the API alerts if beats stop arriving
                log.exception("heartbeat.failed")
                if conn is not None:
                    conn.close()
                conn = None
            if time.time() - last_purge > 3600:
                last_purge = time.time()
                try:
                    purged = files.purge_archive(
                        self.settings.archive_dir, self.settings.archive_retention_days
                    )
                    if purged:
                        log.info("archive.purged", files=purged)
                except OSError:
                    log.exception("archive.purge_failed")
            self.stop.wait(self.settings.heartbeat_seconds)

    def rescan_loop(self) -> None:
        while not self.stop.wait(self.settings.inbox_rescan_seconds):
            try:
                self.rescan()
            except Exception:  # noqa: BLE001
                log.exception("rescan.failed")

    # --- lifecycle ---------------------------------------------------------------------------
    def run(self) -> None:
        s = self.settings
        for d in (s.inbox_dir, s.archive_dir, s.failed_dir):
            d.mkdir(parents=True, exist_ok=True)
        for name in PROVIDERS:
            (s.inbox_dir / name).mkdir(exist_ok=True)

        with connect(s.database_url) as conn:
            interrupted = mark_interrupted(conn)
            conn.execute(
                """DELETE FROM worker_heartbeat
                   WHERE last_beat_at < now() - make_interval(secs => %s)""",
                (s.heartbeat_timeout_seconds,),
            )
        log.info("worker.started", worker_id=self.worker_id, inbox=str(s.inbox_dir),
                 interrupted_runs=interrupted, providers=sorted(PROVIDERS))

        worker = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event: FileSystemEvent) -> None:
                if event.is_directory:
                    return
                target = getattr(event, "dest_path", "") or event.src_path
                worker.offer(Path(os.fsdecode(target)))

        observer = Observer()
        observer.schedule(Handler(), str(s.inbox_dir), recursive=True)
        observer.start()
        threads = [threading.Thread(target=t, daemon=True, name=t.__name__)
                   for t in (self.heartbeat_loop, self.rescan_loop)]
        for t in threads:
            t.start()
        self.rescan()  # files that arrived while the worker was down
        try:
            self.process_loop()
        finally:
            observer.stop()
            observer.join(timeout=5)
            log.info("worker.stopped")


def main() -> None:
    configure_logging()
    worker = Worker(get_settings())

    def shutdown(*_: object) -> None:
        worker.stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker.run()


if __name__ == "__main__":
    main()
