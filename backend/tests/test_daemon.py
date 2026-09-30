"""The always-on downloader loop, with a fake clock, runner and database hooks."""

import random
import threading
from datetime import UTC, datetime, timedelta

import pytest

from ingest.downloader.core import DownloaderSettings, State
from ingest.downloader.daemon import Daemon, lift_login_pause, plan_next_run, serve

T0 = datetime(2026, 9, 18, 4, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Harness:
    """A Daemon wired to fakes that record what it does."""

    def __init__(self, tmp_path, *, run_result=0, run_error=None):
        self.settings = DownloaderSettings(_env_file=None, state_file=tmp_path / "state.json")
        self.clock = Clock()
        self.runs, self.published, self.cleared, self.reports = [], [], [], []
        self.request = None
        self.run_error = run_error

        def run(force):
            self.runs.append((self.clock(), force))
            if self.run_error:
                raise self.run_error
            state = State.load(self.settings.state_file)  # what run_once records
            state.last_attempt_at = self.clock().isoformat()
            state.save(self.settings.state_file)
            return run_result

        self.daemon = Daemon(
            settings=self.settings, every=HOUR, max_jitter_s=0, run=run,
            publish=lambda state, **kw: self.published.append((State(**vars(state)), kw)),
            read_request=lambda: self.request,
            clear_request=lambda at: (self.cleared.append(at), setattr(self, "request", None)),
            report=lambda *issue: self.reports.append(issue),
            clock=self.clock, rng=random.Random(1),
        )

    def state(self):
        return State.load(self.settings.state_file)

    def live(self, **kw):
        """Let time pass while awake: one tick every 5 s, like the real loop."""
        end = self.clock() + timedelta(**kw)
        while self.clock() < end:
            self.clock.advance(seconds=5)
            self.daemon.tick()


def test_plan_next_run():
    s = DownloaderSettings(_env_file=None)
    assert plan_next_run(State(), s, T0, HOUR, timedelta(0)) == T0  # first ever: now
    last = State(last_cycle_at=T0.isoformat(), last_attempt_at=T0.isoformat())
    assert plan_next_run(last, s, T0 + timedelta(minutes=5), HOUR,
                         timedelta(minutes=3)) == T0 + timedelta(hours=1, minutes=3)
    backoff = State(last_cycle_at=T0.isoformat(), last_attempt_at=T0.isoformat(),
                    consecutive_failures=2)  # 2 h backoff beats the hourly slot
    assert plan_next_run(backoff, s, T0, HOUR, timedelta(0)) == T0 + timedelta(hours=2)
    overdue = State(last_cycle_at=(T0 - 3 * HOUR).isoformat())
    assert plan_next_run(overdue, s, T0, HOUR, timedelta(0)) == T0  # catch up now
    assert plan_next_run(State(needs_attention="challenge"), s, T0, HOUR, timedelta(0)) is None


def test_start_publishes_heartbeat_and_start_time(tmp_path):
    h = Harness(tmp_path)
    h.daemon.start()
    state, kw = h.published[-1]
    assert kw == {"heartbeat": True, "started_at": T0}
    assert state.activity == "idle"


def test_downloads_when_due_then_plans_an_hour_later(tmp_path):
    h = Harness(tmp_path)
    State(last_cycle_at=(T0 - timedelta(minutes=30)).isoformat(),
          last_attempt_at=(T0 - timedelta(minutes=30)).isoformat()).save(h.settings.state_file)
    h.daemon.start()
    h.daemon.tick()
    assert h.runs == []
    assert h.state().next_run_at == (T0 + timedelta(minutes=30)).isoformat()

    h.live(minutes=30)
    assert h.runs == [(T0 + timedelta(minutes=30), False)]  # scheduled: not forced
    state = h.state()
    assert state.activity == "idle"
    assert state.next_run_at == (T0 + timedelta(minutes=90)).isoformat()
    assert any(p[0].activity == "downloading" for p in h.published)  # shown while running
    assert h.reports == []  # on time: nothing missed


def test_download_now_request_runs_immediately_and_is_cleared(tmp_path):
    h = Harness(tmp_path)
    h.daemon.start()
    h.daemon.tick()  # first ever run is due right away; let it happen
    h.live(minutes=10)
    h.request = h.clock() - timedelta(seconds=3)
    h.clock.advance(seconds=5)
    h.daemon.tick()
    at = T0 + timedelta(minutes=10, seconds=5)  # handled on the very next tick
    assert h.runs[-1] == (at, True)  # forced past the 45 min pacing
    assert h.cleared and h.request is None
    assert h.state().next_run_at == (at + HOUR).isoformat()


@pytest.mark.parametrize("pause", [
    State(needs_attention="challenge"),
    State(needs_attention="sign_in_rejected", failed_logins=3),
])
def test_paused_never_downloads_even_on_request(tmp_path, pause):
    h = Harness(tmp_path)
    pause.save(h.settings.state_file)
    h.daemon.start()
    h.request = T0
    h.live(hours=3)
    assert h.runs == []
    assert h.state().next_run_at is None

    # the person logs in (login clears the pause): planning resumes
    state = h.state()
    state.needs_attention = None
    state.save(h.settings.state_file)
    h.live(seconds=5)
    assert len(h.runs) == 1


def test_login_pause_from_before_auto_sign_in_is_lifted_on_start(tmp_path):
    # Written by the old version: paused at the first login page, no saved login ever tried.
    h = Harness(tmp_path)
    State(needs_attention="login_required", consecutive_failures=2).save(h.settings.state_file)
    h.daemon.start()
    assert h.state().needs_attention is None
    h.live(seconds=5)
    assert len(h.runs) == 1  # the run tries the saved login


@pytest.mark.parametrize("state, lifted", [
    (State(needs_attention="login_required"), True),
    (State(needs_attention="sign_in_rejected", failed_logins=2), True),
    (State(needs_attention="login_required", failed_logins=3), False),  # earned: stays
    (State(needs_attention="challenge"), False),  # never lifted automatically
    (State(), False),
])
def test_lift_login_pause(state, lifted):
    settings = DownloaderSettings(_env_file=None)
    before = state.needs_attention
    assert lift_login_pause(state, settings) is lifted
    assert state.needs_attention == (None if lifted else before)


def test_catches_up_after_sleep_once_the_network_had_a_minute(tmp_path):
    h = Harness(tmp_path)
    State(last_cycle_at=T0.isoformat(), last_attempt_at=T0.isoformat()).save(
        h.settings.state_file)
    h.daemon.start()
    h.daemon.tick()
    h.clock.advance(hours=3)  # the Mac slept through two planned downloads
    h.daemon.tick()
    assert h.runs == []  # just woke: wait for the network
    h.clock.advance(seconds=30)
    h.daemon.tick()
    assert h.runs == []
    h.clock.advance(seconds=31)
    h.daemon.tick()
    assert len(h.runs) == 1  # one catch-up download, not one per missed hour
    assert h.reports == [("missed", T0 + HOUR, "Not downloaded at the planned time: the Mac was "
                          "asleep. The download started 2 h 1 min late.")]


def test_a_late_download_while_awake_is_reported_as_stuck(tmp_path):
    h = Harness(tmp_path)
    h.daemon.start()
    State(next_run_at=(T0 - timedelta(minutes=25)).isoformat()).save(h.settings.state_file)
    h.daemon.tick()
    assert h.reports == [("missed", T0 - timedelta(minutes=25), "Not downloaded at the planned "
                          "time: the downloader was stuck, see its log. The download started "
                          "25 min late.")]


@pytest.mark.parametrize("late, reported", [(timedelta(hours=3), True),
                                            (timedelta(minutes=2), False)])
def test_a_plan_missed_while_the_downloader_was_not_running(tmp_path, late, reported):
    h = Harness(tmp_path)
    State(next_run_at=(T0 - late).isoformat()).save(h.settings.state_file)
    h.daemon.start()
    assert h.reports == ([("missed", T0 - late, "Not downloaded at the planned time: the "
                           "downloader was not running (Mac off or logged out). It was back "
                           "3 h 0 min later.")] if reported else [])


def test_heartbeat_every_30_seconds_while_idle(tmp_path):
    h = Harness(tmp_path)
    State(last_cycle_at=T0.isoformat(), last_attempt_at=T0.isoformat()).save(
        h.settings.state_file)
    h.daemon.start()
    h.daemon.tick()  # plans the next run: publishes
    count = len(h.published)
    for _ in range(12):  # 60 s of 5 s ticks
        h.clock.advance(seconds=5)
        h.daemon.tick()
    assert len(h.published) - count == 2


def test_a_crashing_download_does_not_stop_the_process(tmp_path):
    h = Harness(tmp_path, run_error=RuntimeError("chrome crashed"))
    h.daemon.start()
    h.daemon.tick()
    assert len(h.runs) == 1
    state = h.state()
    assert state.activity == "idle" and state.next_run_at is not None
    assert h.reports == [("failed", T0, "The download crashed: RuntimeError: chrome crashed")]


def test_serve_stops_cleanly_and_clears_the_heartbeat(tmp_path):
    h = Harness(tmp_path)
    stop = threading.Event()
    stop.set()  # stop right after start
    serve(h.daemon, stop, poll_seconds=0)
    assert h.published[0][1]["started_at"] == T0
    assert h.published[-1][1] == {"heartbeat": False, "stopped": True}


@pytest.mark.parametrize("failures, hours", [(1, 1), (3, 4)])
def test_failures_push_the_plan_back(tmp_path, failures, hours):
    h = Harness(tmp_path)
    State(last_cycle_at=T0.isoformat(), last_attempt_at=T0.isoformat(),
          consecutive_failures=failures).save(h.settings.state_file)
    h.daemon.start()
    h.daemon.tick()
    assert h.state().next_run_at == (T0 + timedelta(hours=hours)).isoformat()


def test_serve_keeps_going_after_a_failed_tick(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    stop = threading.Event()
    ticks = []

    def tick():
        ticks.append(1)
        if len(ticks) == 2:
            stop.set()
        raise OSError("database briefly down")

    monkeypatch.setattr(h.daemon, "tick", tick)
    serve(h.daemon, stop, poll_seconds=0)
    assert len(ticks) == 2
    assert h.published[-1][1] == {"heartbeat": False, "stopped": True}
