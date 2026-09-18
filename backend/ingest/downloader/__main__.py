"""Copart sales data downloader.

    python -m ingest.downloader daemon    # always-on process launchd keeps running
    python -m ingest.downloader login     # once, and whenever an alert asks for it
    python -m ingest.downloader run [--force]   # one download now (manual)
    python -m ingest.downloader status
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from ..alerts import AlertSpec, raise_alert, resolve_fingerprint
from ..db import connect
from ..db import notify as notify_event
from ..log_config import configure_logging
from .core import (
    REPO_ROOT,
    DownloaderSettings,
    InvalidDownload,
    PageState,
    State,
    deliver,
    download_lock,
    jitter_seconds,
    next_allowed_attempt,
    validate_csv,
)

log = structlog.get_logger()

SOURCE = "copart"
FP_ATTENTION = f"downloader_attention:{SOURCE}"
FP_FAILED = f"downloader_failed:{SOURCE}"
WORK_DIR = REPO_ROOT / "data" / ".downloads"  # outside the watched inbox, same filesystem

# Exit codes (launchd logs them)
OK, SKIPPED, NEEDS_PERSON, FAILED, BUSY = 0, 0, 2, 1, 3


def notify(settings: DownloaderSettings, spec: AlertSpec | None, resolve: tuple[str, ...] = ()):
    """Raise and/or resolve dashboard alerts; never fails the run if the DB is down."""
    url = settings.db_url
    if not url:
        log.warning("downloader.no_database", hint="POSTGRES_PASSWORD missing from .env")
        return
    try:
        with connect(url) as conn:
            for fp in resolve:
                resolve_fingerprint(conn, fp)
            if spec:
                raise_alert(conn, spec, source=SOURCE)
    except Exception:  # noqa: BLE001
        log.exception("downloader.alert_failed")


def save(
    settings: DownloaderSettings, state: State, *, heartbeat: bool = False,
    started_at: datetime | None = None, stopped: bool = False,
) -> None:
    """Persist the state locally and publish it for the dashboard (best effort). Only the
    always-on process sends heartbeats; `stopped` clears it so the dashboard knows at once."""
    state.save(settings.state_file)
    url = settings.db_url
    if not url:
        return
    allowed = next_allowed_attempt(state, settings)
    try:
        with connect(url) as conn:
            conn.execute(
                """INSERT INTO downloader_status AS d (source, updated_at, last_attempt_at,
                       last_success_at, consecutive_failures, next_allowed_at, needs_attention,
                       activity, next_run_at, heartbeat_at, daemon_started_at)
                   VALUES (%(source)s, now(), %(attempt)s, %(success)s, %(failures)s,
                           %(allowed)s, %(attention)s, %(activity)s, %(next_run)s,
                           CASE WHEN %(beat)s THEN now() END, %(started)s)
                   ON CONFLICT (source) DO UPDATE SET
                       updated_at = now(),
                       last_attempt_at = EXCLUDED.last_attempt_at,
                       last_success_at = EXCLUDED.last_success_at,
                       consecutive_failures = EXCLUDED.consecutive_failures,
                       next_allowed_at = EXCLUDED.next_allowed_at,
                       needs_attention = EXCLUDED.needs_attention,
                       activity = EXCLUDED.activity,
                       next_run_at = EXCLUDED.next_run_at,
                       heartbeat_at = CASE WHEN %(stopped)s THEN NULL
                                           WHEN %(beat)s THEN now()
                                           ELSE d.heartbeat_at END,
                       daemon_started_at = COALESCE(EXCLUDED.daemon_started_at,
                                                    d.daemon_started_at)""",
                {"source": SOURCE, "attempt": state.last_attempt_at,
                 "success": state.last_success_at, "failures": state.consecutive_failures,
                 "allowed": None if allowed is None or state.last_attempt_at is None
                 else allowed,
                 "attention": state.needs_attention, "activity": state.activity,
                 "next_run": state.next_run_at, "beat": heartbeat, "stopped": stopped,
                 "started": started_at},
            )
            notify_event(conn, "downloader", source=SOURCE)
    except Exception:  # noqa: BLE001 - the dashboard view is best effort
        log.exception("downloader.status_publish_failed")


def read_request(settings: DownloaderSettings) -> datetime | None:
    """A pending "Download now" from the dashboard, if any."""
    if not settings.db_url:
        return None
    with connect(settings.db_url) as conn:
        row = conn.execute("SELECT run_requested_at FROM downloader_status WHERE source = %s",
                           (SOURCE,)).fetchone()
    return row["run_requested_at"] if row else None


def clear_request(settings: DownloaderSettings, at: datetime) -> None:
    if not settings.db_url:
        return
    with connect(settings.db_url) as conn:
        conn.execute(
            """UPDATE downloader_status SET run_requested_at = NULL, run_requested_by = NULL
               WHERE source = %s AND run_requested_at <= %s""", (SOURCE, at))


def run_locked(settings: DownloaderSettings, *, force: bool, **kwargs) -> int:
    """run_once, unless another process is already using the Chrome profile."""
    with download_lock(settings.lock_file) as acquired:
        if not acquired:
            log.warning("downloader.busy", hint="a download or login is already in progress")
            return BUSY
        return run_once(settings, force=force, **kwargs)


def run_once(
    settings: DownloaderSettings,
    *,
    force: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    fetch: Callable[[DownloaderSettings, Path], tuple[PageState, Path | None]] | None = None,
) -> int:
    if fetch is None:
        from .browser import download as fetch

    state = State.load(settings.state_file)
    allowed = next_allowed_attempt(state, settings)
    if allowed is None:
        log.warning("downloader.waiting_for_login", reason=state.needs_attention,
                    hint="run: python -m ingest.downloader login")
        return NEEDS_PERSON
    if not force and now() < allowed:
        log.info("downloader.skipped", next_attempt=allowed.isoformat())
        return SKIPPED

    started = now()
    state.last_attempt_at = started.isoformat()
    save(settings, state)

    try:
        page_state, path = fetch(settings, WORK_DIR)
    except Exception as exc:  # noqa: BLE001 - timeouts, network, browser crashes
        return _failed(settings, state, f"{type(exc).__name__}: {exc}")

    if page_state in (PageState.LOGIN_REQUIRED, PageState.CHALLENGE):
        state.needs_attention = page_state.value
        save(settings, state)
        what = ("Copart session expired" if page_state is PageState.LOGIN_REQUIRED
                else "Copart showed a verification/blocking page")
        notify(settings, AlertSpec(
            "critical", "downloader_attention",
            f"{what}: downloads paused until you log in again",
            FP_ATTENTION,
            {"page_state": page_state.value,
             "fix": "cd backend && .venv/bin/python -m ingest.downloader login"},
        ))
        return NEEDS_PERSON
    if page_state is not PageState.READY or path is None:
        return _failed(settings, state, "download button not found (page layout changed?)")

    try:
        validate_csv(path, settings.min_file_bytes)
    except InvalidDownload as exc:
        settings.failed_dir.mkdir(parents=True, exist_ok=True)
        kept = settings.failed_dir / f"{started:%Y%m%dT%H%M%SZ}_{path.name}"
        path.replace(kept)
        return _failed(settings, state, f"{exc}; kept at {kept}")

    final = deliver(path, settings.inbox_dir, started)
    state.consecutive_failures = 0
    state.last_success_at = now().isoformat()
    save(settings, state)
    log.info("downloader.delivered", file=str(final), bytes=final.stat().st_size)
    notify(settings, None, resolve=(FP_FAILED,))
    return OK


def _failed(settings: DownloaderSettings, state: State, error: str) -> int:
    state.consecutive_failures += 1
    save(settings, state)
    retry = next_allowed_attempt(state, settings)
    log.error("downloader.failed", error=error, failures=state.consecutive_failures,
              next_attempt=retry.isoformat() if retry else None)
    notify(settings, AlertSpec(
        "critical" if state.consecutive_failures >= 3 else "warning",
        "downloader_failed",
        f"Copart download failed ({state.consecutive_failures}x in a row): {error}"[:300],
        FP_FAILED,
        {"error": error, "next_attempt": retry.isoformat() if retry else None},
    ))
    return FAILED


def login(settings: DownloaderSettings) -> int:
    from .browser import interactive_login

    print("A Chrome window opens on Copart. If Chrome has your login saved, 'Sign in' is\n"
          "clicked for you; otherwise log in there yourself. The window closes by itself once\n"
          "the 'Download CSV file' button is reachable.")
    with download_lock(settings.lock_file) as acquired:
        if not acquired:
            print("A download is in progress right now; try again in a minute or two.")
            return BUSY
        logged_in = interactive_login(settings)
    if not logged_in:
        print("Not logged in (window closed or timed out). Downloads stay paused.")
        return NEEDS_PERSON
    state = State.load(settings.state_file)
    state.needs_attention = None
    state.consecutive_failures = 0
    save(settings, state)
    notify(settings, None, resolve=(FP_ATTENTION,))
    print("Logged in. Hourly downloads will resume.")
    return OK


def status(settings: DownloaderSettings) -> int:
    state = State.load(settings.state_file)
    save(settings, state)  # also refreshes the dashboard
    allowed = next_allowed_attempt(state, settings)
    print(f"last attempt:  {state.last_attempt_at or 'never'}")
    print(f"last success:  {state.last_success_at or 'never'}")
    print(f"failures:      {state.consecutive_failures} in a row")
    print(f"next attempt:  {'paused -- needs login' if allowed is None else allowed.isoformat()}")
    return OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.downloader", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="log in to Copart in a visible Chrome window")
    daemon = sub.add_parser("daemon", help="always-on process (what launchd keeps running)")
    daemon.add_argument("--every", type=int, default=3600, help="seconds between downloads")
    daemon.add_argument("--jitter", type=int, default=300,
                        help="random extra delay 0..N seconds before each planned download")
    run = sub.add_parser("run", help="download once now, if pacing allows")
    run.add_argument("--jitter", type=int, default=0,
                     help="wait a random 0..N seconds first")
    run.add_argument("--force", action="store_true", help="ignore the minimum interval")
    sub.add_parser("status", help="show pacing state")
    args = parser.parse_args(argv)

    configure_logging()
    settings = DownloaderSettings()
    if args.command == "login":
        return login(settings)
    if args.command == "status":
        return status(settings)
    if args.command == "daemon":
        return run_daemon(settings, every=args.every, jitter=args.jitter)
    time.sleep(jitter_seconds(args.jitter))
    return run_locked(settings, force=args.force)


def run_daemon(settings: DownloaderSettings, *, every: int, jitter: int) -> int:
    from .daemon import Daemon, serve

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    daemon = Daemon(
        settings=settings, every=timedelta(seconds=every), max_jitter_s=jitter,
        run=lambda force: run_locked(settings, force=force),
        publish=lambda state, **kw: save(settings, state, **kw),
        read_request=lambda: read_request(settings),
        clear_request=lambda at: clear_request(settings, at),
    )
    serve(daemon, stop)
    return OK


if __name__ == "__main__":
    sys.exit(main())
