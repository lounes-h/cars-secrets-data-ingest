"""Drives real Google Chrome (Playwright, persistent profile) to click Copart's download button.

The profile keeps the Copart session cookie and, if the person saved it there, their login in
Chrome's password manager. The code never types or stores credentials: when the session has
expired and Chrome has filled the login form, it clicks "Sign in" once per run. It closes
pop-up dialogs (never accepting anything), but never interacts with a CAPTCHA or verification
page. When Chrome crashes or closes mid-run it is relaunched a few times before giving up.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import structlog

from .core import (
    CLOSE_LABEL,
    DownloaderSettings,
    PageState,
    action_delay_seconds,
    classify_page,
    is_file_response,
    retry,
    should_auto_sign_in,
)

log = structlog.get_logger()

DOWNLOAD_BUTTON = re.compile(r"download\s+csv", re.IGNORECASE)
SIGN_IN_BUTTON = re.compile(r"^\s*(sign|log)\s*in\s*$", re.IGNORECASE)
# Test hook: lets tests serve a fake Copart from memory (context.route) -- never used in prod.
Prepare = Callable[[object], None] | None

# JS: is this input filled, including by Chrome autofill (whose value JS may not read yet)?
_IS_FILLED = """el => el.value.length > 0 || el.matches(':autofill')
    || (() => { try { return el.matches(':-webkit-autofill') } catch { return false } })()"""

# Pop-ups: what counts as a dialog, and controls inside one that close it whatever their text.
DIALOGS = ("[role=dialog], [role=alertdialog], [aria-modal=true], dialog[open], .modal, "
           ".p-dialog, .ui-dialog, [class*=modal i], [class*=popup i], [class*=overlay i], "
           "#onetrust-banner-sdk")
CLOSE_CONTROLS = ("[aria-label*=close i], [title*=close i], [data-dismiss], [data-bs-dismiss], "
                  "[class*=close i], .p-dialog-header-close, .ui-dialog-titlebar-close")
_MARK = "data-ingest-dismiss"
AUTOFILL_WAIT_MS = 10_000  # Chrome fills saved logins shortly after the page loads

# JS: mark the first visible close control of a visible dialog (or one matching the extra
# selectors anywhere) and return the dialog's text, for the log. Dialogs holding a password
# field are skipped: that is the login form, not a pop-up.
_FIND_CLOSE = """([dialogs, controls, extra, label]) => {
  const re = new RegExp(label, 'i');
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
      && s.opacity !== '0';
  };
  const text = el => (el.getAttribute('aria-label') || el.getAttribute('title')
    || el.innerText || el.value || '').trim();
  const mark = (el, where) => {
    el.setAttribute('data-ingest-dismiss', '1');
    return (where.innerText || text(el) || '').trim().replace(/\\s+/g, ' ').slice(0, 160);
  };
  if (extra) {
    const hit = [...document.querySelectorAll(extra)].find(visible);
    if (hit) return mark(hit, hit.closest(dialogs) || hit);
  }
  for (const d of [...document.querySelectorAll(dialogs)].filter(visible)) {
    if (d.querySelector('input[type=password]')) continue;
    const all = [...d.querySelectorAll('button, a, [role=button], span, i, div, input')]
      .filter(visible);
    const clickable = all.filter(c => c.matches('button, a, [role=button], input'));
    const hit = clickable.find(c => re.test(text(c)))
      || all.find(c => c.matches(controls) && !c.matches(dialogs))
      || all.find(c => re.test(text(c)));
    if (hit) return mark(hit, d);
  }
  return null;
}"""


class WindowClosed(RuntimeError):
    """Chrome closed or crashed on every attempt before the download was saved."""


def _launch_args() -> list[str]:
    # After a crash Chrome offers to "Restore pages?"; that bubble is useless here.
    return ["--hide-crash-restore-bubble"]


def _chrome_pids(profile_dir: Path) -> list[int]:
    """Chrome processes using this profile (Playwright passes --user-data-dir=<profile>)."""
    pattern = "--user-data-dir=" + re.escape(str(profile_dir)) + r"( |$)"
    try:
        out = subprocess.run(["pgrep", "-f", "--", pattern], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(pid) for pid in out.split() if pid.isdigit() and int(pid) != os.getpid()]


def kill_orphaned_chrome(profile_dir: Path, wait_seconds: float = 5.0) -> int:
    """Stop Chrome processes a crashed run left on the profile, which would make the next
    launch fail ("profile in use"), and remove Chrome's stale profile lock. Only called while
    holding the download lock, so no live run of ours owns them."""
    pids = _chrome_pids(profile_dir)
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait_seconds
    while pids and time.monotonic() < deadline and _chrome_pids(profile_dir):
        time.sleep(0.2)
    for pid in _chrome_pids(profile_dir):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        with contextlib.suppress(FileNotFoundError):
            (profile_dir / name).unlink()
    if pids:
        log.warning("downloader.orphaned_chrome_stopped", pids=pids)
    return len(pids)


def _open(playwright, settings: DownloaderSettings, *, headed: bool, downloads: Path | None = None):
    settings.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    kill_orphaned_chrome(settings.profile_dir)
    return playwright.chromium.launch_persistent_context(
        str(settings.profile_dir),
        channel="chrome",  # the installed Google Chrome, not a bundled test browser
        headless=not headed,
        accept_downloads=True,
        downloads_path=str(downloads) if downloads else None,
        viewport={"width": 1440, "height": 900},
        args=_launch_args(),
    )


def _human_pause(page, settings: DownloaderSettings) -> None:
    """A person reads before clicking: wait a random moment before each action."""
    delay = action_delay_seconds(settings)
    if delay > 0:
        page.wait_for_timeout(delay * 1000)


def _inspect(page) -> tuple[PageState, object]:
    button = page.get_by_role("button", name=DOWNLOAD_BUTTON)
    try:
        text = page.inner_text("body", timeout=5_000)
    except Exception:  # noqa: BLE001 - an empty/odd page is classified below
        text = ""
    state = classify_page(page.url, page.title(), text, button.count() > 0)
    return state, button


def dismiss_dialogs(page, settings: DownloaderSettings, rounds: int = 5) -> int:
    """Close pop-up dialogs covering the page; returns how many were closed. Never on a
    verification page: those are for a person."""
    closed = 0
    extra = ", ".join(settings.copart_dismiss_selectors)
    for _ in range(rounds):
        state, _ = _inspect(page)
        if state is PageState.CHALLENGE:
            break
        try:
            found = page.evaluate(_FIND_CLOSE, [DIALOGS, CLOSE_CONTROLS, extra,
                                                CLOSE_LABEL.pattern])
        except Exception:  # noqa: BLE001 - page navigating; the caller inspects again
            break
        if found is None:
            break
        target = page.locator(f"[{_MARK}]").first
        _human_pause(page, settings)
        try:
            target.click(timeout=5_000)
        except Exception:  # noqa: BLE001 - covered or animated away: Escape closes most
            with contextlib.suppress(Exception):
                page.keyboard.press("Escape")
        with contextlib.suppress(Exception):
            page.evaluate(f"() => document.querySelectorAll('[{_MARK}]')"
                          f".forEach(e => e.removeAttribute('{_MARK}'))")
        closed += 1
        log.info("downloader.dialog_closed", text=found)
        page.wait_for_timeout(500)  # closing animations
    return closed


def clear_work_dir(work_dir: Path) -> None:
    """Remove what earlier interrupted runs left behind (downloads are ~100 MB each)."""
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)


def _is_closed_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "closed" in text or "crash" in text


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


def _sign_in(page, settings: DownloaderSettings) -> bool:
    """Click "Sign in" once Chrome's password manager has filled the form (it may take a
    moment after the page loads). False if it never filled both fields."""
    waited = 0
    while True:
        user_ok, pass_ok, button = _login_form_filled(page)
        if should_auto_sign_in(PageState.LOGIN_REQUIRED, user_ok, pass_ok, button is not None,
                               already_tried=False):
            break
        if waited >= AUTOFILL_WAIT_MS:
            return False
        page.wait_for_timeout(500)
        waited += 500
    _human_pause(page, settings)
    log.info("downloader.auto_sign_in")
    button.click()
    with contextlib.suppress(Exception):
        page.wait_for_url(lambda url: "/login" not in url.lower(), timeout=30_000)
    with contextlib.suppress(Exception):
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
    return True


def _wait_for_page(page, settings: DownloaderSettings, timeout_ms: int = 20_000) -> None:
    """Wait until the download button or a login form shows, then a moment more, as a person
    would. Neither: classified by the caller (challenge or changed layout)."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    ready = page.get_by_role("button", name=DOWNLOAD_BUTTON).or_(
        page.locator("input[type=password]"))
    with contextlib.suppress(PlaywrightTimeout):
        ready.first.wait_for(timeout=timeout_ms)
    _human_pause(page, settings)


def reach_data_page(page, settings: DownloaderSettings) -> tuple[PageState, object]:
    """Open the sales data page, signing in once if the session expired and Chrome filled the
    login form, and closing pop-ups. Returns the final state and the download button."""
    page.goto(settings.copart_download_url, wait_until="domcontentloaded", timeout=60_000)
    tried_sign_in = False
    state, button = PageState.UNKNOWN, None
    for _ in range(4):  # login -> sign in -> (landing page) -> data page, with slack
        _wait_for_page(page, settings)
        dismiss_dialogs(page, settings)
        state, button = _inspect(page)
        log.info("downloader.page", state=state.value, url=page.url)
        if state in (PageState.READY, PageState.CHALLENGE):
            return state, button
        if state is PageState.LOGIN_REQUIRED:
            if tried_sign_in:
                return PageState.SIGN_IN_REJECTED, button
            if not _sign_in(page, settings):
                return PageState.LOGIN_REQUIRED, button
            tried_sign_in = True
            dismiss_dialogs(page, settings)  # e.g. a notice shown right after login
            if "/login" in page.url.lower():
                continue  # still on the login page: rejected, or a slow redirect
        elif _same_page(page.url, settings.copart_download_url):
            return state, button  # on the data page, no button: layout changed
        # Logged in and landed elsewhere (dashboard): go back to the data page.
        page.goto(settings.copart_download_url, wait_until="domcontentloaded", timeout=60_000)
    return state, button


def _same_page(url: str, target: str) -> bool:
    def strip(u: str) -> str:
        return u.split("?")[0].split("#")[0].rstrip("/").lower()
    return strip(url) == strip(target)


class _ResponseCapture:
    """Saves the export straight from the network response instead of letting Chrome's
    download manager handle it: Chrome 153 on macOS crashes (SIGSEGV on its main thread) while
    finalizing large downloads, most of the time for a ~90 MB export. The request still goes
    through Chrome itself (same session, cookies and connection); only the response body is
    read over the DevTools protocol and written to `target`, and the navigation is then
    cancelled, so the page stays where it is and Chrome never starts a download."""

    def __init__(self, page, target: Path):
        self.page, self.target = page, target
        self.done = False
        self.error: BaseException | None = None
        self.cdp = None

    def __enter__(self) -> _ResponseCapture:
        self.cdp = self.page.context.new_cdp_session(self.page)
        self.cdp.on("Fetch.requestPaused", lambda event: self._on_paused(event))
        self.cdp.send("Fetch.enable", {"patterns": [
            {"urlPattern": "*", "resourceType": "Document", "requestStage": "Response"}]})
        return self

    def __exit__(self, *exc) -> None:
        with contextlib.suppress(Exception):
            self.cdp.send("Fetch.disable")
        with contextlib.suppress(Exception):
            self.cdp.detach()

    def _on_paused(self, event: dict) -> None:
        request_id = event["requestId"]
        try:
            headers = {h["name"]: h["value"] for h in event.get("responseHeaders") or []}
            if self.done or not is_file_response(event.get("responseStatusCode") or 0, headers):
                self.cdp.send("Fetch.continueRequest", {"requestId": request_id})
                return
            log.info("downloader.response_capture", url=event["request"]["url"].split("?")[0])
            self._save(request_id)
            self.done = True
        except Exception as exc:  # noqa: BLE001 - reported to the waiting loop
            self.error = exc
            with contextlib.suppress(Exception):
                self.cdp.send("Fetch.failRequest", {"requestId": request_id,
                                                    "errorReason": "Aborted"})

    def _save(self, request_id: str) -> None:
        stream = self.cdp.send("Fetch.takeResponseBodyAsStream",
                               {"requestId": request_id})["stream"]
        part = self.target.with_name(self.target.name + ".part")
        try:
            with open(part, "wb") as fh:
                while True:
                    chunk = self.cdp.send("IO.read", {"handle": stream, "size": 1 << 20})
                    data = chunk.get("data", "")
                    fh.write(base64.b64decode(data) if chunk.get("base64Encoded")
                             else data.encode())
                    if chunk.get("eof"):
                        break
        finally:
            with contextlib.suppress(Exception):
                self.cdp.send("IO.close", {"handle": stream})
        # Cancel the navigation: the file is saved, the page stays on the data page.
        with contextlib.suppress(Exception):
            self.cdp.send("Fetch.failRequest", {"requestId": request_id,
                                                "errorReason": "Aborted"})
        os.replace(part, self.target)


def _click_download(page, button, settings: DownloaderSettings, target: Path) -> Path:
    """Click the download button and save the export to `target`; returns it when complete."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    # Fallback if the file ever arrives as a Chrome download anyway (e.g. opened in a new
    # tab, which the capture does not watch).
    downloads: list = []
    for p in page.context.pages:
        p.on("download", lambda d: downloads.append(d))
    page.context.on("page", lambda p: p.on("download", lambda d: downloads.append(d)))

    with _ResponseCapture(page, target) as capture:
        for attempt in range(3):
            _human_pause(page, settings)
            try:
                button.first.click(timeout=10_000)
                break
            except PlaywrightTimeout:
                # Something covers the button, typically a pop-up that appeared late.
                if attempt == 2 or not dismiss_dialogs(page, settings):
                    raise
        deadline = time.monotonic() + settings.download_timeout_seconds
        while time.monotonic() < deadline:
            if capture.error is not None:
                raise capture.error
            if capture.done:
                return target
            if downloads:
                # Chrome writes the file into work_dir itself; path() waits until it is
                # complete. Renaming it (same directory) avoids a ~100 MB copy.
                os.replace(Path(downloads[0].path()), target)
                return target
            page.wait_for_timeout(250)
    raise PlaywrightTimeout(
        f"no file received within {settings.download_timeout_seconds} s of the click")


def _download_once(
    settings: DownloaderSettings, work_dir: Path, prepare: Prepare,
) -> tuple[PageState, Path | None]:
    from playwright.sync_api import sync_playwright

    clear_work_dir(work_dir)
    with sync_playwright() as p:
        context = _open(p, settings, headed=settings.copart_headed, downloads=work_dir)
        try:
            if prepare:
                prepare(context)
            page = context.pages[0] if context.pages else context.new_page()
            state, button = reach_data_page(page, settings)
            if state is not PageState.READY:
                return state, None
            return state, _click_download(page, button, settings, work_dir / "salesdata.csv")
        finally:
            with contextlib.suppress(Exception):
                context.close()


def download(
    settings: DownloaderSettings, work_dir: Path, *, prepare: Prepare = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[PageState, Path | None]:
    """Open the sales data page and download the CSV once. Returns the page state and, when
    READY, the downloaded file (not yet validated). A browser crash, a closed window or a
    browser-side timeout relaunches Chrome, up to `browser_attempts` times in all."""
    from playwright.sync_api import Error as PlaywrightError

    def on_retry(n: int, exc: BaseException) -> None:
        log.warning("downloader.browser_retry", attempt=n, error=f"{type(exc).__name__}: {exc}"
                    [:300], retry_in=settings.browser_retry_seconds)

    try:
        return retry(
            lambda: _download_once(settings, work_dir, prepare),
            attempts=settings.browser_attempts,
            retryable=lambda exc: isinstance(exc, PlaywrightError),
            pause=lambda: sleep(settings.browser_retry_seconds),
            on_retry=on_retry,
        )
    except PlaywrightError as exc:
        if _is_closed_error(exc):
            raise WindowClosed(
                f"Chrome closed or crashed before the download finished, "
                f"{settings.browser_attempts} times in a row"
            ) from exc
        raise


def interactive_login(
    settings: DownloaderSettings, timeout_minutes: float = 15, *,
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
            state, _ = reach_data_page(page, settings)
            deadline_ms = timeout_minutes * 60_000
            waited = 0
            while state is not PageState.READY:
                if waited >= deadline_ms:
                    return False
                page.wait_for_timeout(1_000)
                waited += 1_000
                dismiss_dialogs(page, settings)
                state, _ = _inspect(page)
                if "/login" not in page.url.lower() and state is PageState.UNKNOWN:
                    # Logged in and landed elsewhere (dashboard): go back to the data page.
                    page.goto(settings.copart_download_url, wait_until="domcontentloaded")
            return True
        except Exception:  # noqa: BLE001 - the person closed the window
            return False
        finally:
            with contextlib.suppress(Exception):
                context.close()
