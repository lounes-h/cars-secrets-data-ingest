"""The real browser code against a fake Copart served from memory (Playwright routing).

The fake lives on `copart.test` (a reserved TLD that never resolves) and a catch-all route
aborts anything it does not serve, so no test request can ever reach the real Copart.
Redirects are done by page scripts, not HTTP 3xx, because routing does not see requests the
browser follows from a redirect. Skipped when Google Chrome is not installed."""

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


class FakeCopart:
    """Minimal stand-in for the three pages the downloader touches."""

    def __init__(self, *, logged_in=False, user="", password=""):
        self.logged_in = logged_in
        self.user, self.password = user, password
        self.sign_ins = []

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
            return redirect(route, "/downloadSalesData")
        if path.lower() == "/downloadsalesdata":
            if not self.logged_in:
                return redirect(route, "/login")
            return route.fulfill(content_type="text/html", body=DATA_PAGE)
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
                                  download_timeout_seconds=30)
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


def test_login_waits_for_the_person_when_the_form_is_empty(bsettings):
    site = FakeCopart(user="", password="")
    # Nobody logs in within the (short) timeout: no click, not logged in.
    assert not browser.interactive_login(bsettings, timeout_minutes=0.05, headed=False,
                                         prepare=site.install)
    assert site.sign_ins == []


def test_login_does_not_retry_a_rejected_sign_in(bsettings):
    class RejectingCopart(FakeCopart):
        def _handle(self, route):
            if route.request.url.endswith("/processLogin"):
                self.sign_ins.append(route.request.post_data)
                return redirect(route, "/login")
            return super()._handle(route)

    site = RejectingCopart(user="dealer@example.com", password="wrong")
    assert not browser.interactive_login(bsettings, timeout_minutes=0.1, headed=False,
                                         prepare=site.install)
    assert len(site.sign_ins) == 1  # clicked once, then left it to the person
