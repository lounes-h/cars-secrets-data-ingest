"""Browser-independent parts of the downloader: settings, pacing state, page classification,
file validation and hand-off to the ingestion inbox. Kept apart from Playwright so it is
fully unit-testable.

Behaviour rules (see docs/downloader.md):
- one download per MIN_INTERVAL at most, whatever the scheduler does;
- a login page or any bot challenge stops everything until a person runs `login` again --
  nothing here tries to get past one;
- repeated plain failures back off exponentially.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import asdict, dataclass
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

    # A visible browser window, like a person using the site; set false to run headless.
    copart_headed: bool = True
    min_interval_minutes: int = 45
    max_backoff_hours: int = 6
    download_timeout_seconds: int = 600
    min_file_bytes: int = 1024

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
    LOGIN_REQUIRED = "login_required"
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
    # Set when Copart shows a login page or a challenge; cleared only by `login`.
    needs_attention: str | None = None
    # Last wake-up by the scheduler (before the random delay) and the schedule it runs on;
    # together they predict the next download (see schedule.py).
    last_invocation_at: str | None = None
    schedule_interval_seconds: int | None = None
    jitter_seconds: int = 0

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            return cls(**json.loads(path.read_text()))
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, path)


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


def jitter_seconds(max_seconds: int, rng: random.Random | None = None) -> float:
    return (rng or random).uniform(0, max_seconds) if max_seconds > 0 else 0.0
