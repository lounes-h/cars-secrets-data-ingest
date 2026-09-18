"""Drives real Google Chrome (Playwright, persistent profile) to click Copart's download button.

The profile keeps the Copart session cookie, so a person logs in once with `login` and later
runs reuse that session. Nothing here fills in credentials or interacts with a CAPTCHA.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

import structlog

from .core import DownloaderSettings, PageState, classify_page

log = structlog.get_logger()

DOWNLOAD_BUTTON = re.compile(r"download\s+csv", re.IGNORECASE)


def _open(playwright, settings: DownloaderSettings, *, headed: bool, downloads: Path | None = None):
    settings.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return playwright.chromium.launch_persistent_context(
        str(settings.profile_dir),
        channel="chrome",  # the installed Google Chrome, not a bundled test browser
        headless=not headed,
        accept_downloads=True,
        downloads_path=str(downloads) if downloads else None,
        viewport={"width": 1440, "height": 900},
    )


def _inspect(page) -> tuple[PageState, object]:
    button = page.get_by_role("button", name=DOWNLOAD_BUTTON)
    try:
        text = page.inner_text("body", timeout=5_000)
    except Exception:  # noqa: BLE001 - an empty/odd page is classified below
        text = ""
    state = classify_page(page.url, page.title(), text, button.count() > 0)
    return state, button


def download(settings: DownloaderSettings, work_dir: Path) -> tuple[PageState, Path | None]:
    """Open the sales data page and download the CSV once. Returns the page state and, when
    READY, the downloaded file (not yet validated)."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    work_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = _open(p, settings, headed=settings.copart_headed, downloads=work_dir)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(settings.copart_download_url, wait_until="domcontentloaded", timeout=60_000)
            # No button yet: classified below (login page, challenge or changed layout).
            with contextlib.suppress(PlaywrightTimeout):
                page.get_by_role("button", name=DOWNLOAD_BUTTON).wait_for(timeout=20_000)
            state, button = _inspect(page)
            log.info("downloader.page", state=state.value, url=page.url)
            if state is not PageState.READY:
                return state, None
            with page.expect_download(timeout=settings.download_timeout_seconds * 1000) as info:
                button.first.click()
            dl = info.value
            failure = dl.failure()
            if failure:
                raise RuntimeError(f"download failed: {failure}")
            target = work_dir / "salesdata.csv"
            dl.save_as(target)
            return state, target
        finally:
            context.close()


def interactive_login(settings: DownloaderSettings, timeout_minutes: int = 15) -> bool:
    """Open a visible Chrome on Copart's login page and wait until the person has logged in
    and the download button is reachable. The person types their own credentials."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = _open(p, settings, headed=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(settings.copart_download_url, wait_until="domcontentloaded", timeout=60_000)
            deadline_ms = timeout_minutes * 60_000
            waited = 0
            while waited < deadline_ms:
                state, _ = _inspect(page)
                if state is PageState.READY:
                    return True
                if "/login" not in page.url.lower() and state is PageState.UNKNOWN:
                    # Logged in and landed elsewhere (dashboard): go back to the data page.
                    page.goto(settings.copart_download_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3_000)
                waited += 3_000
            return False
        except Exception:  # noqa: BLE001 - the person closed the window
            return False
        finally:
            with contextlib.suppress(Exception):
                context.close()
