"""The real browser code against a fake Copart served from memory (Playwright routing).

The fake lives on `copart.test` (a reserved TLD that never resolves) and a catch-all route
aborts anything it does not serve, so no test request can ever reach the real Copart.
Redirects are done by page scripts, not HTTP 3xx, because routing does not see requests the
browser follows from a redirect. Skipped when Google Chrome is not installed."""

import os
import signal
import subprocess
import sys
import time

import pytest

from ingest.downloader import browser
from ingest.downloader.core import DownloaderSettings, PageState

from .conftest import SAMPLE

playwright = pytest.importorskip("playwright.sync_api")

CSV = SAMPLE.read_bytes()

LOGIN_PAGE = """<!doctype html><title>Login | Copart</title>
<form method="post" action="/processLogin">
  <input type="email" name="username" value="{user}">
  <input type="password" name="password" value="{password}">
  <button type="submit">Sign In</button>
</form>"""

DATA_PAGE = """<!doctype html><title>Online Car Auctions</title>
<h1>CSV Sales Data</h1>
<button type="button" onclick="location.href='/salesdata/download.csv'"
  >Download CSV file</button>"""

BASE = "https://www.copart.test"


def redirect(route, path):
    route.fulfill(content_type="text/html",
                  body=f"<!doctype html><script>location.replace('{path}')</script>")


# Pop-ups seen after login come and go and change markup; each covers the whole page, so the
# download button cannot be clicked until it is closed.
OVERLAY = ('<div style="position:fixed;inset:0;background:#0008;z-index:10" {attrs}>'
           '<div style="background:#fff;margin:100px auto;width:400px;padding:20px">'
           '<p>New: bid from your phone!</p>{close}</div></div>')
DIALOGS = {
    "role_dialog_close_text": ('role="dialog"', '<button onclick="{hide}">Close</button>'),
    "aria_label_x": ('class="modal"', '<button aria-label="Close" onclick="{hide}">'
                     '<span>\u00d7</span></button>'),
    "no_thanks_link": ('class="promo-popup"', '<a href="#" onclick="{hide};return false"'
                       '>No thanks</a>'),
    "icon_only_close_class": ('class="p-dialog"', '<span class="p-dialog-header-close-icon '
                              'pi pi-times" style="display:inline-block;width:16px;height:16px"'
                              ' onclick="{hide}"></span>'),
}
_HIDE = "this.closest('[data-popup]').remove()"


def dialog_html(kind):
    attrs, close = DIALOGS[kind]
    return OVERLAY.format(attrs=f"{attrs} data-popup", close=close.format(hide=_HIDE))


class FakeCopart:
    """Minimal stand-in for the three pages the downloader touches."""

    def __init__(self, *, logged_in=False, user="", password="", dialog=None,
                 landing="/downloadSalesData"):
        self.logged_in = logged_in
        self.user, self.password = user, password
        self.dialog = dialog  # shown on the data page after a sign-in, like Copart's notices
        self.landing = landing  # where a sign-in lands
        self.sign_ins = []
        self.show_dialog = False

    def install(self, context):
        context.route("**/*", self._handle)  # everything: nothing reaches the network

    def _handle(self, route):
        req = route.request
        if not req.url.startswith(BASE):
            return route.abort()
        path = req.url[len(BASE):].split("?")[0]
        if path == "/processLogin" and req.method == "POST":
            self.sign_ins.append(req.post_data)
            self.logged_in = True
            self.show_dialog = self.dialog is not None
            return redirect(route, self.landing)
        if path.lower() == "/downloadsalesdata":
            if not self.logged_in:
                return redirect(route, "/login")
            body = DATA_PAGE + (dialog_html(self.dialog) if self.show_dialog else "")
            return route.fulfill(content_type="text/html", body=body)
        if path == "/dashboard":
            return route.fulfill(content_type="text/html",
                                 body="<!doctype html><title>Member dashboard</title><h1>Hi</h1>")
        if path == "/login":
            return route.fulfill(content_type="text/html",
                                 body=LOGIN_PAGE.format(user=self.user, password=self.password))
        if path == "/salesdata/download.csv":
            return route.fulfill(body=CSV, headers={
                "Content-Type": "text/csv",
                "Content-Disposition": 'attachment; filename="salesdata.csv"'})
        return route.fulfill(status=404, body="")


@pytest.fixture
def bsettings(tmp_path):
    settings = DownloaderSettings(_env_file=None, profile_dir=tmp_path / "profile",
                                  copart_download_url=f"{BASE}/downloadSalesData",
                                  state_file=tmp_path / "state.json", copart_headed=False,
                                  download_timeout_seconds=30, browser_retry_seconds=0,
                                  action_delay_min_seconds=0, action_delay_max_seconds=0)
    try:  # skip cleanly where Google Chrome is not installed (e.g. CI)
        with playwright.sync_playwright() as p:
            browser._open(p, settings, headed=False).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Google Chrome not available: {exc}")
    return settings


def test_download_saves_complete_file_and_clears_leftovers(bsettings, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "old.crdownload").write_text("left by an interrupted run")

    state, path = browser.download(bsettings, work,
                                   prepare=FakeCopart(logged_in=True).install)

    assert state is PageState.READY
    assert path == work / "salesdata.csv"
    assert path.read_bytes() == CSV  # whole file, not a partial copy
    assert sorted(p.name for p in work.iterdir()) == ["salesdata.csv"]


def test_download_reports_login_page(bsettings, tmp_path):
    state, path = browser.download(bsettings, tmp_path / "work",
                                   prepare=FakeCopart(logged_in=False).install)
    assert (state, path) == (PageState.LOGIN_REQUIRED, None)


def test_login_clicks_sign_in_once_when_chrome_filled_the_form(bsettings):
    site = FakeCopart(user="dealer@example.com", password="from-password-manager")
    assert browser.interactive_login(bsettings, timeout_minutes=1, headed=False,
                                     prepare=site.install)
    assert len(site.sign_ins) == 1
    assert "dealer%40example.com" in site.sign_ins[0]


def test_login_waits_for_the_person_when_the_form_is_empty(bsettings, monkeypatch):
    monkeypatch.setattr(browser, "AUTOFILL_WAIT_MS", 1_000)
    site = FakeCopart(user="", password="")
    # Nobody logs in within the (short) timeout: no click, not logged in.
    assert not browser.interactive_login(bsettings, timeout_minutes=0.05, headed=False,
                                         prepare=site.install)
    assert site.sign_ins == []


class RejectingCopart(FakeCopart):
    def _handle(self, route):
        if route.request.url.endswith("/processLogin"):
            self.sign_ins.append(route.request.post_data)
            return redirect(route, "/login")
        return super()._handle(route)


def test_login_does_not_retry_a_rejected_sign_in(bsettings):
    site = RejectingCopart(user="dealer@example.com", password="wrong")
    assert not browser.interactive_login(bsettings, timeout_minutes=0.1, headed=False,
                                         prepare=site.install)
    assert len(site.sign_ins) == 1  # clicked once, then left it to the person


# --- unattended runs: sign in, pop-ups, crashes ------------------------------------------------

def test_scheduled_run_signs_in_with_the_saved_login_and_downloads(bsettings, tmp_path):
    site = FakeCopart(user="dealer@example.com", password="from-password-manager",
                      landing="/dashboard")  # lands elsewhere: goes back to the data page
    state, path = browser.download(bsettings, tmp_path / "work", prepare=site.install)
    assert state is PageState.READY and path.read_bytes() == CSV
    assert len(site.sign_ins) == 1


def test_scheduled_run_without_saved_login_reports_login_required(bsettings, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr(browser, "AUTOFILL_WAIT_MS", 1_000)
    site = FakeCopart(user="", password="")
    assert browser.download(bsettings, tmp_path / "work", prepare=site.install) \
        == (PageState.LOGIN_REQUIRED, None)
    assert site.sign_ins == []


def test_scheduled_run_reports_a_rejected_sign_in_without_retrying(bsettings, tmp_path):
    site = RejectingCopart(user="dealer@example.com", password="wrong")
    assert browser.download(bsettings, tmp_path / "work", prepare=site.install) \
        == (PageState.SIGN_IN_REJECTED, None)
    assert len(site.sign_ins) == 1


@pytest.mark.parametrize("kind", sorted(DIALOGS))
def test_pop_up_after_login_is_closed_before_downloading(bsettings, tmp_path, kind):
    site = FakeCopart(user="dealer@example.com", password="saved", dialog=kind)
    state, path = browser.download(bsettings, tmp_path / "work", prepare=site.install)
    assert state is PageState.READY and path.read_bytes() == CSV


def test_unknown_pop_up_is_closed_through_configured_selector(bsettings, tmp_path):
    class OddPopup(FakeCopart):
        def _handle(self, route):
            if route.request.url.endswith("/downloadSalesData") and self.logged_in:
                popup = ('<div id="promo" style="position:fixed;inset:0;z-index:10;'
                         'background:#fff"><b>Big news</b>'
                         f'<em class="go-away" onclick="{_HIDE}">hide me</em></div>')
                return route.fulfill(content_type="text/html",
                                     body=DATA_PAGE + popup.replace("<div id", "<div data-popup"
                                                                    " id"))
            return super()._handle(route)

    # Nothing marks it as a dialog or a close button: only the configured selector finds it.
    settings = bsettings.model_copy(update={"copart_dismiss_selectors": ["#promo .go-away"]})
    state, path = browser.download(settings, tmp_path / "work",
                                   prepare=OddPopup(logged_in=True).install)
    assert state is PageState.READY and path.read_bytes() == CSV


def test_dialogs_on_a_verification_page_are_left_alone(bsettings, tmp_path):
    class Challenge(FakeCopart):
        def _handle(self, route):
            if route.request.url.endswith("/downloadSalesData"):
                return route.fulfill(content_type="text/html", body=(
                    "<title>Pardon Our Interruption</title><div role=dialog>"
                    "<p>Verify you are human</p><button onclick='window.clicked=1'>Close"
                    "</button></div>"))
            return super()._handle(route)

    with playwright.sync_playwright() as p:
        context = browser._open(p, bsettings, headed=False)
        Challenge(logged_in=True).install(context)
        page = context.new_page()
        state, _ = browser.reach_data_page(page, bsettings)
        assert state is PageState.CHALLENGE
        assert page.evaluate("window.clicked") is None
        context.close()


def test_a_crashed_browser_is_relaunched_and_the_download_completes(bsettings, tmp_path):
    class CrashOnce(FakeCopart):
        crashed = False

        def _handle(self, route):
            if route.request.url.endswith("/download.csv") and not self.crashed:
                self.crashed = True
                for pid in browser._chrome_pids(bsettings.profile_dir):  # Chrome dies
                    os.kill(pid, signal.SIGKILL)
                return None
            return super()._handle(route)

    site = CrashOnce(logged_in=True)
    state, path = browser.download(bsettings, tmp_path / "work", prepare=site.install)
    assert site.crashed
    assert state is PageState.READY and path.read_bytes() == CSV


def test_a_browser_that_keeps_crashing_gives_a_clear_error(bsettings, tmp_path):
    class AlwaysCrash(FakeCopart):
        def _handle(self, route):
            if route.request.url.endswith("/download.csv"):
                for pid in browser._chrome_pids(bsettings.profile_dir):
                    os.kill(pid, signal.SIGKILL)
                return None
            return super()._handle(route)

    settings = bsettings.model_copy(update={"browser_attempts": 2})
    with pytest.raises(browser.WindowClosed, match="2 times in a row"):
        browser.download(settings, tmp_path / "work", prepare=AlwaysCrash(logged_in=True).install)


def test_orphaned_chrome_on_the_profile_is_stopped_before_launch(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("otherhost-12345")
    other = tmp_path / "other-profile"
    # Stand-ins for Chrome processes: one left on our profile, one on another profile.
    ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                             f"--user-data-dir={profile}"])
    theirs = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                               f"--user-data-dir={other}"])
    try:
        time.sleep(0.3)
        assert browser.kill_orphaned_chrome(profile, wait_seconds=5) == 1
        assert ours.wait(timeout=5) is not None
        assert theirs.poll() is None  # another profile is never touched
        assert not (profile / "SingletonLock").is_symlink()
    finally:
        for proc in (ours, theirs):
            proc.kill()
            proc.wait()


def test_every_click_waits_like_a_person(bsettings, tmp_path, monkeypatch):
    pauses = []
    real = browser._human_pause
    monkeypatch.setattr(browser, "_human_pause",
                        lambda page, settings: (pauses.append(1), real(page, settings)))
    site = FakeCopart(user="dealer@example.com", password="saved", dialog="aria_label_x")
    state, _ = browser.download(bsettings, tmp_path / "work", prepare=site.install)
    assert state is PageState.READY
    # login page read, sign in, data page read, close pop-up, download: each paused first
    assert len(pauses) >= 5
