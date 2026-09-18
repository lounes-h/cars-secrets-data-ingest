"""Drives real Google Chrome (Playwright, persistent profile) to click Copart's download button.

The profile keeps the Copart session cookie, so a person logs in once with `login` and later
runs reuse that session. The code never types credentials: at most, during `login`, it clicks
"Sign in" once when Chrome's password manager has already filled the form. It never interacts
with a CAPTCHA or verification page.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import structlog

from .core import DownloaderSettings, PageState, classify_page, should_auto_sign_in

log = structlog.get_logger()

DOWNLOAD_BUTTON = re.compile(r"download\s+csv", re.IGNORECASE)
SIGN_IN_BUTTON = re.compile(r"^\s*(sign|log)\s*in\s*$", re.IGNORECASE)
# Test hook: lets tests serve a fake Copart from memory (context.route) -- never used in prod.
Prepare = Callable[[object], None] | None

# JS: is this input filled, including by Chrome autofill (whose value JS may not read yet)?
_IS_FILLED = """el => el.value.length > 0 || el.matches(':autofill')
    || (() => { try { return el.matches(':-webkit-autofill') } catch { return false } })()"""


class WindowClosed(RuntimeError):
    """The browser was closed (usually by hand) before the download was saved."""


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


def clear_work_dir(work_dir: Path) -> None:
    """Remove what earlier interrupted runs left behind (downloads are ~100 MB each)."""
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)


def _is_closed_error(exc: BaseException) -> bool:
    return "closed" in str(exc).lower()


def download(
    settings: DownloaderSettings, work_dir: Path, *, prepare: Prepare = None,
) -> tuple[PageState, Path | None]:
    """Open the sales data page and download the CSV once. Returns the page state and, when
    READY, the downloaded file (not yet validated)."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    clear_work_dir(work_dir)
    with sync_playwright() as p:
        context = _open(p, settings, headed=settings.copart_headed, downloads=work_dir)
        try:
            if prepare:
                prepare(context)
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
            # Chrome writes the file into work_dir itself; path() waits until it is complete.
            # Renaming it (same directory, instant) avoids a ~100 MB copy that would break if
            # the window were closed meanwhile.
            finished = Path(dl.path())
            target = work_dir / "salesdata.csv"
            os.replace(finished, target)
            return state, target
        except PlaywrightError as exc:
            if _is_closed_error(exc):
                raise WindowClosed(
                    "the Chrome window was closed before the download finished "
                    "(leave it open: it closes by itself)"
                ) from exc
            raise
        finally:
            with contextlib.suppress(Exception):
                context.close()


def _login_form_filled(page) -> tuple[bool, bool, object | None]:
    """(username filled, password filled, sign-in button) for the visible login form."""
    password = page.locator("input[type=password]:visible").first
    if password.count() == 0:
        return False, False, None
    form = page.locator("form", has=password).first
    scope = form if form.count() else page
    user = scope.locator(
        "input[type=email]:visible, input[type=text]:visible, input:not([type]):visible").first
    user_filled = user.count() > 0 and bool(user.evaluate(_IS_FILLED))
    pass_filled = bool(password.evaluate(_IS_FILLED))
    button = scope.get_by_role("button", name=SIGN_IN_BUTTON)
    if button.count() == 0:
        button = scope.locator("button[type=submit], input[type=submit]")
    return user_filled, pass_filled, (button.first if button.count() else None)


def interactive_login(
    settings: DownloaderSettings, timeout_minutes: int = 15, *,
    headed: bool = True, prepare: Prepare = None,
) -> bool:
    """Open Chrome on Copart and wait until the person is logged in and the download button is
    reachable. If Chrome's password manager has filled the login form, click "Sign in" once;
    otherwise (or if that is not enough, e.g. a second factor) the person finishes by hand."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = _open(p, settings, headed=headed)
        try:
            if prepare:
                prepare(context)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(settings.copart_download_url, wait_until="domcontentloaded", timeout=60_000)
            tried_sign_in = False
            deadline_ms = timeout_minutes * 60_000
            waited = 0
            while waited < deadline_ms:
                state, _ = _inspect(page)
                if state is PageState.READY:
                    return True
                if state is PageState.LOGIN_REQUIRED and not tried_sign_in:
                    user_ok, pass_ok, button = _login_form_filled(page)
                    if should_auto_sign_in(state, user_ok, pass_ok, button is not None,
                                           tried_sign_in):
                        tried_sign_in = True
                        log.info("downloader.auto_sign_in")
                        button.click()
                        with contextlib.suppress(Exception):
                            page.wait_for_load_state("domcontentloaded", timeout=30_000)
                        continue
                if "/login" not in page.url.lower() and state is PageState.UNKNOWN:
                    # Logged in and landed elsewhere (dashboard): go back to the data page.
                    page.goto(settings.copart_download_url, wait_until="domcontentloaded")
                page.wait_for_timeout(1_000)
                waited += 1_000
            return False
        except Exception:  # noqa: BLE001 - the person closed the window
            return False
        finally:
            with contextlib.suppress(Exception):
                context.close()
