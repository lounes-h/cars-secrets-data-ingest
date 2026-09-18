"""Downloader rules, with a fake browser: no test ever contacts Copart."""

import random
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from ingest.downloader import __main__ as cli
from ingest.downloader.core import (
    DownloaderSettings,
    InvalidDownload,
    PageState,
    State,
    action_delay_seconds,
    classify_page,
    deliver,
    is_close_label,
    jitter_seconds,
    next_allowed_attempt,
    retry,
    should_auto_sign_in,
    validate_csv,
)

from .conftest import SAMPLE

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def dsettings(tmp_path, db_url):
    return DownloaderSettings(
        _env_file=None,
        inbox_dir=tmp_path / "inbox" / "copart",
        failed_dir=tmp_path / "failed" / "downloader",
        profile_dir=tmp_path / "profile",
        state_file=tmp_path / "state.json",
        database_url=db_url,
    )


class Clock:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at


def fetch_returning(state, source_file=None):
    calls = []

    def fetch(settings, work_dir):
        calls.append(work_dir)
        if source_file is None:
            return state, None
        target = settings.state_file.parent / "downloaded.csv"
        shutil.copy(source_file, target)
        return state, target

    fetch.calls = calls
    return fetch


def open_alerts(conn):
    return {r["fingerprint"]: r for r in conn.execute(
        "SELECT * FROM alerts WHERE resolved_at IS NULL")}


# --- page classification -----------------------------------------------------------------------

@pytest.mark.parametrize("url, title, text, button, expected", [
    ("https://www.copart.com/downloadSalesData", "Online Car Auctions", "CSV Sales Data", True,
     PageState.READY),
    ("https://www.copart.com/login?redirect=x", "Login", "", False, PageState.LOGIN_REQUIRED),
    ("https://www.copart.com/downloadSalesData", "Sign in | Copart", "", False,
     PageState.LOGIN_REQUIRED),
    ("https://www.copart.com/downloadSalesData", "", "Request unsuccessful. Incapsula incident ID",
     False, PageState.CHALLENGE),
    ("https://www.copart.com/downloadSalesData", "Just a moment", "Please complete the CAPTCHA",
     True, PageState.CHALLENGE),  # a challenge wins even if the button is somewhere on the page
    ("https://www.copart.com/downloadSalesData", "Copart", "Something new", False,
     PageState.UNKNOWN),
])
def test_classify_page(url, title, text, button, expected):
    assert classify_page(url, title, text, button) is expected


# --- validation & hand-off ---------------------------------------------------------------------

def test_validate_accepts_real_export():
    validate_csv(SAMPLE, 1024)


@pytest.mark.parametrize("content, match", [
    ("x" * 10, "only 10 bytes"),
    ("<!doctype html><html><body>Please log in</body></html>" + " " * 2000, "not a sales data"),
    ("Id,Make,Model\n" + "1,a,b\n" * 400, "not a sales data"),
])
def test_validate_rejects_non_exports(tmp_path, content, match):
    path = tmp_path / "x.csv"
    path.write_text(content)
    with pytest.raises(InvalidDownload, match=match):
        validate_csv(path, 1024)


def test_deliver_is_atomic_and_timestamped(tmp_path):
    src = tmp_path / "dl.csv"
    src.write_text("data")
    final = deliver(src, tmp_path / "inbox", T0)
    assert final.name == "salesdata_20260918T120000Z.csv"
    assert final.read_text() == "data"
    assert not src.exists()
    assert [p.name for p in final.parent.iterdir()] == [final.name]  # no .part left behind


# --- pacing ------------------------------------------------------------------------------------

def test_pacing_min_interval_backoff_and_pause(dsettings):
    assert next_allowed_attempt(State(), dsettings) < T0  # never ran: go
    state = State(last_attempt_at=T0.isoformat())
    assert next_allowed_attempt(state, dsettings) == T0 + timedelta(minutes=45)
    state.consecutive_failures = 1
    assert next_allowed_attempt(state, dsettings) == T0 + timedelta(hours=1)
    state.consecutive_failures = 3
    assert next_allowed_attempt(state, dsettings) == T0 + timedelta(hours=4)
    state.consecutive_failures = 10
    assert next_allowed_attempt(state, dsettings) == T0 + timedelta(hours=6)  # capped
    state.needs_attention = "challenge"
    assert next_allowed_attempt(state, dsettings) is None


def test_state_roundtrip_and_corrupt_file(tmp_path):
    path = tmp_path / "s.json"
    State(last_attempt_at="x", consecutive_failures=2).save(path)
    assert State.load(path).consecutive_failures == 2
    path.write_text("{not json")
    assert State.load(path) == State()


def test_action_delay_is_random_within_bounds():
    settings = DownloaderSettings(_env_file=None, action_delay_min_seconds=1.5,
                                  action_delay_max_seconds=4)
    rng = random.Random(7)
    delays = [action_delay_seconds(settings, rng) for _ in range(200)]
    assert all(1.5 <= d <= 4 for d in delays)
    assert len({round(d, 3) for d in delays}) > 100  # not a fixed rhythm
    swapped = DownloaderSettings(_env_file=None, action_delay_min_seconds=4,
                                 action_delay_max_seconds=1.5)
    assert 1.5 <= action_delay_seconds(swapped, rng) <= 4
    off = DownloaderSettings(_env_file=None, action_delay_min_seconds=0,
                             action_delay_max_seconds=0)
    assert action_delay_seconds(off) == 0


def test_jitter_bounds():
    rng = random.Random(1)
    assert all(0 <= jitter_seconds(300, rng) <= 300 for _ in range(100))
    assert jitter_seconds(0) == 0


# --- run_once ------------------------------------------------------------------------------------

def test_successful_run_delivers_to_inbox_and_clears_failure_alert(dsettings, conn):
    cli.notify(dsettings, cli.AlertSpec("warning", "downloader_failed", "x", cli.FP_FAILED))
    fetch = fetch_returning(PageState.READY, SAMPLE)

    assert cli.run_once(dsettings, now=Clock(), fetch=fetch) == cli.OK

    (delivered,) = dsettings.inbox_dir.iterdir()
    assert delivered.name == "salesdata_20260918T120000Z.csv"
    state = State.load(dsettings.state_file)
    assert state.last_success_at and state.consecutive_failures == 0
    assert cli.FP_FAILED not in open_alerts(conn)


def test_runs_are_paced_even_if_scheduled_too_often(dsettings, conn):
    fetch = fetch_returning(PageState.READY, SAMPLE)
    clock = Clock()
    cli.run_once(dsettings, now=clock, fetch=fetch)
    clock.at = T0 + timedelta(minutes=10)
    assert cli.run_once(dsettings, now=clock, fetch=fetch) == cli.SKIPPED
    assert len(fetch.calls) == 1
    assert cli.run_once(dsettings, now=clock, fetch=fetch, force=True) == cli.OK
    assert len(fetch.calls) == 2


def test_challenge_pauses_until_a_person_logs_in(dsettings, conn, monkeypatch):
    fetch = fetch_returning(PageState.CHALLENGE)
    clock = Clock()

    assert cli.run_once(dsettings, now=clock, fetch=fetch) == cli.NEEDS_PERSON
    alert = open_alerts(conn)[cli.FP_ATTENTION]
    assert alert["severity"] == "critical" and "paused" in alert["title"]

    # Hours later, still paused, and Copart is not contacted again -- even with --force.
    clock.at = T0 + timedelta(hours=5)
    assert cli.run_once(dsettings, now=clock, fetch=fetch, force=True) == cli.NEEDS_PERSON
    assert len(fetch.calls) == 1

    # The person logs in: alert resolved, downloads resume.
    import ingest.downloader.browser as browser
    monkeypatch.setattr(browser, "interactive_login", lambda settings: True)
    assert cli.login(dsettings) == cli.OK
    assert cli.FP_ATTENTION not in open_alerts(conn)
    assert State.load(dsettings.state_file).needs_attention is None
    ok = fetch_returning(PageState.READY, SAMPLE)
    assert cli.run_once(dsettings, now=clock, fetch=ok) == cli.OK


@pytest.mark.parametrize("page_state", [PageState.LOGIN_REQUIRED, PageState.SIGN_IN_REJECTED])
def test_login_page_is_retried_then_pauses_to_protect_the_account(dsettings, conn, page_state):
    """The run already tried the saved login once. A miss is a failure with backoff; only
    MAX_FAILED_LOGINS runs in a row pause, so a wrong saved password cannot lock the account."""
    fetch = fetch_returning(page_state)
    clock = Clock()
    for n in (1, 2):
        assert cli.run_once(dsettings, now=clock, fetch=fetch, force=True) == cli.FAILED
        state = State.load(dsettings.state_file)
        assert (state.failed_logins, state.needs_attention) == (n, None)
        assert f"{n}/3 before pausing" in open_alerts(conn)[cli.FP_FAILED]["title"]
        clock.at += timedelta(hours=3)
    assert cli.FP_ATTENTION not in open_alerts(conn)

    assert cli.run_once(dsettings, now=clock, fetch=fetch, force=True) == cli.NEEDS_PERSON
    assert State.load(dsettings.state_file).needs_attention == page_state.value
    alert = open_alerts(conn)[cli.FP_ATTENTION]
    assert alert["severity"] == "critical" and "3 runs in a row" in alert["title"]
    assert cli.run_once(dsettings, now=clock, fetch=fetch, force=True) == cli.NEEDS_PERSON
    assert len(fetch.calls) == 3  # paused: Copart not contacted again


def test_a_successful_run_resets_login_misses_and_clears_the_pause_alert(dsettings, conn):
    clock = Clock()
    cli.run_once(dsettings, now=clock, fetch=fetch_returning(PageState.SIGN_IN_REJECTED))
    cli.notify(dsettings, cli.AlertSpec("critical", "downloader_attention", "old pause",
                                        cli.FP_ATTENTION, {}))
    clock.at += timedelta(hours=2)
    assert cli.run_once(dsettings, now=clock, fetch=fetch_returning(PageState.READY, SAMPLE),
                        force=True) == cli.OK
    assert State.load(dsettings.state_file).failed_logins == 0
    assert not {cli.FP_FAILED, cli.FP_ATTENTION} & set(open_alerts(conn))


def test_failed_login_keeps_downloads_paused(dsettings, monkeypatch):
    State(needs_attention="login_required").save(dsettings.state_file)
    import ingest.downloader.browser as browser
    monkeypatch.setattr(browser, "interactive_login", lambda settings: False)
    assert cli.login(dsettings) == cli.NEEDS_PERSON
    assert State.load(dsettings.state_file).needs_attention == "login_required"


def test_invalid_download_is_kept_aside_not_ingested(dsettings, conn, tmp_path):
    page = tmp_path / "error.html"
    page.write_text("<html>" + "x" * 5000 + "</html>")

    assert cli.run_once(dsettings, now=Clock(), fetch=fetch_returning(PageState.READY, page)) \
        == cli.FAILED

    assert not dsettings.inbox_dir.exists() or not any(dsettings.inbox_dir.iterdir())
    (kept,) = dsettings.failed_dir.iterdir()
    assert kept.name.startswith("20260918T120000Z_")
    assert open_alerts(conn)[cli.FP_FAILED]["severity"] == "warning"


def test_repeated_errors_back_off_and_escalate(dsettings, conn):
    def broken(settings, work_dir):
        raise TimeoutError("page did not load")

    clock = Clock()
    for _ in range(3):
        assert cli.run_once(dsettings, now=clock, fetch=broken, force=True) == cli.FAILED
        clock.at += timedelta(minutes=1)
    state = State.load(dsettings.state_file)
    assert state.consecutive_failures == 3
    alert = open_alerts(conn)[cli.FP_FAILED]
    assert alert["severity"] == "critical" and alert["occurrences"] == 3
    assert "TimeoutError" in alert["title"]
    # without --force the backoff (4 h after the 3rd failure) is respected
    assert cli.run_once(dsettings, now=clock, fetch=broken) == cli.SKIPPED


def test_page_without_button_counts_as_failure(dsettings, conn):
    assert cli.run_once(dsettings, now=Clock(), fetch=fetch_returning(PageState.UNKNOWN)) \
        == cli.FAILED
    assert "layout changed" in open_alerts(conn)[cli.FP_FAILED]["title"]


def test_alerts_are_skipped_quietly_without_database(tmp_path):
    settings = DownloaderSettings(_env_file=None, state_file=tmp_path / "s.json",
                                  inbox_dir=tmp_path / "in", failed_dir=tmp_path / "f")
    assert settings.db_url is None
    assert cli.run_once(settings, now=Clock(), fetch=fetch_returning(PageState.UNKNOWN)) \
        == cli.FAILED


def test_db_url_built_from_compose_env():
    s = DownloaderSettings(_env_file=None, postgres_password="pw", postgres_port=5433)
    assert s.db_url == "postgresql://ingest:pw@127.0.0.1:5433/ingest"


@pytest.mark.parametrize("state, user, password, button, tried, expected", [
    (PageState.LOGIN_REQUIRED, True, True, True, False, True),
    (PageState.LOGIN_REQUIRED, True, True, True, True, False),  # only once
    (PageState.LOGIN_REQUIRED, True, False, True, False, False),  # password not saved
    (PageState.LOGIN_REQUIRED, False, True, True, False, False),
    (PageState.LOGIN_REQUIRED, True, True, False, False, False),  # no button found
    (PageState.CHALLENGE, True, True, True, False, False),  # never on a verification page
    (PageState.READY, True, True, True, False, False),
])
def test_should_auto_sign_in(state, user, password, button, tried, expected):
    assert should_auto_sign_in(state, user, password, button, tried) is expected


def test_browser_crashing_every_attempt_gets_a_clear_alert(dsettings, conn):
    from ingest.downloader.browser import WindowClosed

    def closed(settings, work_dir):
        raise WindowClosed("Chrome closed or crashed before the download finished, "
                           "3 times in a row")

    assert cli.run_once(dsettings, now=Clock(), fetch=closed) == cli.FAILED
    assert "crashed" in open_alerts(conn)[cli.FP_FAILED]["title"]


class Flaky(Exception):
    pass


def test_retry_until_success_pausing_between_attempts():
    calls, pauses, retried = [], [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise Flaky(len(calls))
        return "done"

    assert retry(fn, attempts=3, retryable=lambda e: isinstance(e, Flaky),
                 pause=lambda: pauses.append(1),
                 on_retry=lambda n, e: retried.append(n)) == "done"
    assert (len(calls), len(pauses), retried) == (3, 2, [1, 2])


def test_retry_gives_up_after_the_last_attempt_or_on_other_errors():
    calls = []

    def flaky():
        calls.append(1)
        raise Flaky

    with pytest.raises(Flaky):
        retry(flaky, attempts=2, retryable=lambda e: True, pause=lambda: None)
    assert len(calls) == 2

    def broken():
        calls.append(1)
        raise ValueError

    with pytest.raises(ValueError):
        retry(broken, attempts=5, retryable=lambda e: isinstance(e, Flaky), pause=lambda: None)
    assert len(calls) == 3  # not retried


@pytest.mark.parametrize("label, expected", [
    ("Close", True), ("  close dialog ", True), ("×", True), ("✕", True), ("X", True),
    ("No thanks", True), ("No, thanks!", True), ("Not now", True), ("Maybe later", True),
    ("Skip", True), ("Got it", True), ("Dismiss", True),
    # never accept or agree on the person's behalf, nor act on anything else
    ("Accept", False), ("I agree", False), ("Continue", False), ("OK", False),
    ("Sign in", False), ("Close account", False), ("Download CSV file", False), ("", False),
    (None, False),
])
def test_close_labels(label, expected):
    assert is_close_label(label) is expected


def test_status_is_published_for_the_dashboard(dsettings, conn):
    cli.run_once(dsettings, now=Clock(T0), fetch=fetch_returning(PageState.READY, SAMPLE))
    row = conn.execute("SELECT * FROM downloader_status").fetchone()
    assert row["last_success_at"] == T0
    assert row["next_allowed_at"] == T0 + timedelta(minutes=45)
    assert row["heartbeat_at"] is None  # only the always-on process sends heartbeats

    state = State.load(dsettings.state_file)
    state.next_run_at = (T0 + timedelta(hours=1)).isoformat()
    cli.save(dsettings, state, heartbeat=True, started_at=T0)
    row = conn.execute("SELECT * FROM downloader_status").fetchone()
    assert row["heartbeat_at"] is not None and row["daemon_started_at"] == T0
    assert row["next_run_at"] == T0 + timedelta(hours=1)

    cli.save(dsettings, state)  # a CLI save keeps the heartbeat...
    assert conn.execute("SELECT heartbeat_at FROM downloader_status").fetchone()["heartbeat_at"]
    cli.save(dsettings, state, stopped=True)  # ...a clean stop clears it at once
    assert conn.execute("SELECT heartbeat_at FROM downloader_status").fetchone()[
        "heartbeat_at"] is None


def test_download_now_request_round_trip(dsettings, conn):
    cli.save(dsettings, State())
    assert cli.read_request(dsettings) is None
    conn.execute("UPDATE downloader_status SET run_requested_at = now()")
    at = cli.read_request(dsettings)
    assert at is not None
    cli.clear_request(dsettings, at)
    assert cli.read_request(dsettings) is None


def test_only_one_process_uses_the_chrome_profile(dsettings, conn):
    from ingest.downloader.core import download_lock

    with download_lock(dsettings.lock_file) as first:
        assert first
        with download_lock(dsettings.lock_file) as second:
            assert not second
        fetch = fetch_returning(PageState.READY, SAMPLE)
        assert cli.run_locked(dsettings, force=True, fetch=fetch) == cli.BUSY
        assert fetch.calls == []  # Chrome never started
    assert cli.run_locked(dsettings, force=True, now=Clock(),
                          fetch=fetch_returning(PageState.READY, SAMPLE)) == cli.OK


def test_state_file_from_an_older_version_still_loads(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"consecutive_failures": 2, "last_invocation_at": "x", '
                    '"schedule_interval_seconds": 3600}')
    assert State.load(path).consecutive_failures == 2


def test_pause_is_published(dsettings, conn):
    cli.run_once(dsettings, now=Clock(), fetch=fetch_returning(PageState.CHALLENGE))
    row = conn.execute("SELECT needs_attention, next_allowed_at FROM downloader_status"
                       ).fetchone()
    assert row == {"needs_attention": "challenge", "next_allowed_at": None}
