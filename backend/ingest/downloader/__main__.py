"""Copart sales data downloader.

    python -m ingest.downloader login            # once, and whenever an alert asks for it
    python -m ingest.downloader run [--jitter S] # one paced download
    python -m ingest.downloader run --scheduled --every 3600 --jitter 300   # what launchd runs
    python -m ingest.downloader status
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
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
OK, SKIPPED, NEEDS_PERSON, FAILED = 0, 0, 2, 1


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


def save(settings: DownloaderSettings, state: State) -> None:
    """Persist the state locally and publish it for the dashboard (best effort)."""
    state.save(settings.state_file)
    url = settings.db_url
    if not url:
        return
    allowed = next_allowed_attempt(state, settings)
    try:
        with connect(url) as conn:
            conn.execute(
                """INSERT INTO downloader_status (source, updated_at, last_invocation_at,
                       schedule_interval_seconds, jitter_seconds, last_attempt_at,
                       last_success_at, consecutive_failures, next_allowed_at, needs_attention)
                   VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (source) DO UPDATE SET
                       updated_at = now(),
                       last_invocation_at = EXCLUDED.last_invocation_at,
                       schedule_interval_seconds = EXCLUDED.schedule_interval_seconds,
                       jitter_seconds = EXCLUDED.jitter_seconds,
                       last_attempt_at = EXCLUDED.last_attempt_at,
                       last_success_at = EXCLUDED.last_success_at,
                       consecutive_failures = EXCLUDED.consecutive_failures,
                       next_allowed_at = EXCLUDED.next_allowed_at,
                       needs_attention = EXCLUDED.needs_attention""",
                (SOURCE, state.last_invocation_at, state.schedule_interval_seconds,
                 state.jitter_seconds, state.last_attempt_at, state.last_success_at,
                 state.consecutive_failures,
                 None if allowed is None or state.last_attempt_at is None else allowed,
                 state.needs_attention),
            )
            notify_event(conn, "downloader", source=SOURCE)
    except Exception:  # noqa: BLE001 - the dashboard view is best effort
        log.exception("downloader.status_publish_failed")


def record_invocation(settings: DownloaderSettings, now: datetime, every: int,
                      jitter: int) -> None:
    """A scheduled wake-up: remember it (before the random delay) to predict the next one."""
    state = State.load(settings.state_file)
    state.last_invocation_at = now.isoformat()
    state.schedule_interval_seconds = every
    state.jitter_seconds = jitter
    save(settings, state)


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
    if not interactive_login(settings):
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
    run = sub.add_parser("run", help="download once if pacing allows")
    run.add_argument("--jitter", type=int, default=0,
                     help="wait a random 0..N seconds first (spreads scheduled runs)")
    run.add_argument("--force", action="store_true", help="ignore the minimum interval")
    run.add_argument("--scheduled", action="store_true",
                     help="invoked by the scheduler: record the wake-up for the dashboard")
    run.add_argument("--every", type=int, default=3600,
                     help="scheduler interval in seconds (with --scheduled)")
    sub.add_parser("status", help="show pacing state")
    mark = sub.add_parser("mark-scheduled",
                          help="record that the schedule was (re)installed now (install script)")
    mark.add_argument("--every", type=int, default=3600)
    mark.add_argument("--jitter", type=int, default=0)
    args = parser.parse_args(argv)

    configure_logging()
    settings = DownloaderSettings()
    if args.command == "login":
        return login(settings)
    if args.command == "status":
        return status(settings)
    if args.command == "mark-scheduled":
        # launchd starts its interval when the job is loaded: the first wake-up is `every`
        # seconds from now, exactly as if it had just woken up.
        record_invocation(settings, datetime.now(UTC), args.every, args.jitter)
        return OK
    if args.scheduled:
        record_invocation(settings, datetime.now(UTC), args.every, args.jitter)
    time.sleep(jitter_seconds(args.jitter))
    return run_once(settings, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
