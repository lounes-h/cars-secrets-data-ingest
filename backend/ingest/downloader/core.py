"""Browser-independent parts of the downloader: settings, pacing state, page classification,
file validation and hand-off to the ingestion inbox. Kept apart from Playwright so it is
fully unit-testable.

Behaviour rules (see docs/downloader.md):
- one download per MIN_INTERVAL at most, whatever the scheduler does;
- an expired session is renewed by clicking "Sign in" once per run when Chrome's password
  manager filled the form; after MAX_FAILED_LOGINS runs in a row that do not get in, and at
  once on any bot challenge, everything stops until a person runs `login` -- nothing here
  tries to get past a challenge;
- repeated plain failures back off exponentially.
"""

from __future__ import annotations

import json
import os
import random
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
HOME_DIR = Path.home() / ".cars-ingest"


class DownloaderSettings(BaseSettings):
    """Reads the same .env as docker-compose, so it talks to the same database."""

    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    copart_download_url: str = "https://www.copart.com/downloadSalesData"
    copart_login_url: str = "https://www.copart.com/login"
    inbox_dir: Path = REPO_ROOT / "data" / "inbox" / "copart"
    failed_dir: Path = REPO_ROOT / "data" / "failed" / "downloader"
    profile_dir: Path = HOME_DIR / "copart-profile"
    state_file: Path = HOME_DIR / "downloader-state.json"
    lock_file: Path = HOME_DIR / "downloader.lock"

    # A visible browser window, like a person using the site; set false to run headless.
    copart_headed: bool = True
    min_interval_minutes: int = 45
    max_backoff_hours: int = 6
    download_timeout_seconds: int = 600
    min_file_bytes: int = 1024
    # Chrome crashing or closing mid-run: relaunch it this many times in all, after a pause.
    browser_attempts: int = 3
    browser_retry_seconds: int = 20
    # Runs in a row where the saved login did not get in before downloads pause (protects
    # the account from lockout on a wrong saved password).
    max_failed_logins: int = 3
    # Extra CSS selectors for close buttons of pop-ups Copart may add, as a JSON list, e.g.
    # COPART_DISMISS_SELECTORS='["#promo .btn-close"]'. Common dialogs are closed without it.
    copart_dismiss_selectors: list[str] = []
    # Pause a random time in this range before each click and after each page load, like a
    # person reading the page, instead of clicking through at machine speed.
    action_delay_min_seconds: float = 1.5
    action_delay_max_seconds: float = 4.0

    # Where alerts go (the Postgres published by docker-compose on localhost).
    postgres_password: str = ""
    postgres_port: int = 5433
    database_url: str = ""

    @property
    def db_url(self) -> str | None:
        if self.database_url:
            return self.database_url
        if self.postgres_password:
            return (f"postgresql://ingest:{self.postgres_password}"
                    f"@127.0.0.1:{self.postgres_port}/ingest")
        return None


# --- page classification ---------------------------------------------------------------------

class PageState(StrEnum):
    READY = "ready"  # logged in, download button present
    LOGIN_REQUIRED = "login_required"  # login page and Chrome did not fill the saved login
    SIGN_IN_REJECTED = "sign_in_rejected"  # "Sign in" clicked, still on the login page
    CHALLENGE = "challenge"  # CAPTCHA / bot wall / access denied: a person must look
    UNKNOWN = "unknown"


_CHALLENGE_MARKERS = (
    "captcha", "incapsula", "incident id", "access denied", "request unsuccessful",
    "pardon our interruption", "are you a robot", "verify you are human", "unusual traffic",
)


def classify_page(url: str, title: str, text: str, has_download_button: bool) -> PageState:
    haystack = f"{title}\n{text[:5000]}".lower()
    if any(marker in haystack for marker in _CHALLENGE_MARKERS):
        return PageState.CHALLENGE
    if "/login" in url.lower() or "sign in" in title.lower():
        return PageState.LOGIN_REQUIRED
    if has_download_button:
        return PageState.READY
    return PageState.UNKNOWN


def should_auto_sign_in(
    state: PageState, username_filled: bool, password_filled: bool, has_button: bool,
    already_tried: bool,
) -> bool:
    """Click "Sign in" for the person only on a plain login page, only when Chrome's password
    manager filled both fields, and only once (a second failure means the person must look:
    wrong password, second factor...)."""
    return (state is PageState.LOGIN_REQUIRED and username_filled and password_filled
            and has_button and not already_tried)


# Pop-ups ("what's new", surveys, cookie banners) are closed through a control labelled like
# this. Only ways to *close* -- never accept, agree or continue on the person's behalf.
CLOSE_LABEL = re.compile(
    r"^\s*(close|close (dialog|modal|window|popup)|dismiss|[x\u00d7\u2715\u2716\u2573]"
    r"|no,? thanks|not now|maybe later|later|skip|got it)\s*[.!]?\s*$",
    re.IGNORECASE)


def is_close_label(label: str | None) -> bool:
    return bool(label) and CLOSE_LABEL.match(label) is not None


def retry[T](
    fn: Callable[[], T], *, attempts: int, retryable: Callable[[BaseException], bool],
    pause: Callable[[], None], on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Call fn until it returns, up to `attempts` times, while it raises a retryable error."""
    for n in range(1, max(attempts, 1) + 1):
        try:
            return fn()
        except Exception as exc:
            if n >= attempts or not retryable(exc):
                raise
            if on_retry:
                on_retry(n, exc)
            pause()
    raise AssertionError("unreachable")


_FILE_TYPES = ("text/csv", "application/csv", "application/octet-stream",
               "application/vnd.ms-excel", "application/force-download")


def is_file_response(status: int, headers: dict[str, str]) -> bool:
    """Is this navigation response a file to save (the export), rather than a page to show?
    Copart serves the export as application/octet-stream."""
    if not 200 <= status < 300:
        return False
    lower = {k.lower(): v.lower() for k, v in headers.items()}
    if "attachment" in lower.get("content-disposition", ""):
        return True
    return lower.get("content-type", "").split(";")[0].strip() in _FILE_TYPES


# --- file validation and hand-off --------------------------------------------------------------

class InvalidDownload(Exception):
    pass


def validate_csv(path: Path, min_bytes: int) -> None:
    """A real export is a CSV whose header has a lot number column; an error page saved as a
    download is not."""
    size = path.stat().st_size
    if size < min_bytes:
        raise InvalidDownload(f"file is only {size} bytes")
    with open(path, "rb") as fh:
        head = fh.read(4096).decode("utf-8-sig", errors="replace")
    first_line = head.splitlines()[0] if head else ""
    if "<html" in head.lower() or "lot number" not in first_line.lower():
        raise InvalidDownload("not a sales data CSV (no 'Lot number' column in the header)")


def deliver(tmp: Path, inbox: Path, now: datetime) -> Path:
    """Move a validated download into the inbox atomically: the worker ignores `.part` files,
    so it only ever sees the complete file under its final name."""
    inbox.mkdir(parents=True, exist_ok=True)
    final = inbox / f"salesdata_{now:%Y%m%dT%H%M%SZ}.csv"
    part = final.with_name(final.name + ".part")
    os.replace(tmp, part)  # same filesystem in practice; replace is atomic
    os.replace(part, final)
    return final


# --- pacing state ------------------------------------------------------------------------------

@dataclass
class State:
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    consecutive_failures: int = 0
    # Runs in a row that ended on the login page (saved login missing or rejected).
    failed_logins: int = 0
    # Set on a challenge, or after max_failed_logins; cleared only by `login`.
    needs_attention: str | None = None
    # The always-on process (daemon.py): when its last hourly cycle started, when it plans
    # the next download, and what it is doing now ('idle' | 'downloading').
    last_cycle_at: str | None = None
    next_run_at: str | None = None
    activity: str = "idle"

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}  # older/newer versions may have other keys
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, path)


@contextmanager
def download_lock(path: Path) -> Iterator[bool]:
    """One Chrome per profile: yields False (without waiting) when another process -- the
    always-on downloader, a manual `run` or `login` -- is using it."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def next_allowed_attempt(state: State, settings: DownloaderSettings) -> datetime | None:
    """Earliest time another download may start; None means never (needs a person)."""
    if state.needs_attention:
        return None
    last = _parse(state.last_attempt_at)
    if last is None:
        return datetime.min.replace(tzinfo=UTC)
    wait = timedelta(minutes=settings.min_interval_minutes)
    if state.consecutive_failures:
        backoff = timedelta(hours=min(2 ** (state.consecutive_failures - 1),
                                      settings.max_backoff_hours))
        wait = max(wait, backoff)
    return last + wait


def action_delay_seconds(settings: DownloaderSettings, rng: random.Random | None = None) -> float:
    lo, hi = sorted((max(settings.action_delay_min_seconds, 0.0),
                     max(settings.action_delay_max_seconds, 0.0)))
    return (rng or random).uniform(lo, hi) if hi > 0 else 0.0


def jitter_seconds(max_seconds: int, rng: random.Random | None = None) -> float:
    return (rng or random).uniform(0, max_seconds) if max_seconds > 0 else 0.0
