"""The always-on downloader process (`python -m ingest.downloader daemon`), kept alive by launchd.

Every few seconds it:
- publishes a heartbeat (every 30 s), so the dashboard knows it is running;
- starts a download when the planned time comes (hourly, plus a random 0..jitter delay, never
  before pacing/backoff allows), or right away when the dashboard asked for one;
- catches up after the Mac slept through a planned download, a minute after waking so the
  network is back first, and reports the planned download as missed (the Runs page shows it).

It plans but never forces past a pause: after a challenge page, or a saved login that failed
several runs in a row, nothing is scheduled until `login` clears it. All I/O is injected, so
the loop is tested without Chrome or a real clock.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog

from .core import DownloaderSettings, State, next_allowed_attempt

log = structlog.get_logger()

HEARTBEAT = timedelta(seconds=30)
WAKE_GRACE = timedelta(seconds=60)  # after a sleep, let the network come back first
SLEEP_GAP = timedelta(seconds=60)  # a longer gap between ticks means the Mac was asleep
MISSED_AFTER = timedelta(minutes=10)  # a planned download this late was missed


def _minutes(delta: timedelta) -> str:
    m = round(delta.total_seconds() / 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def plan_next_run(
    state: State, settings: DownloaderSettings, now: datetime, every: timedelta,
    jitter: timedelta,
) -> datetime | None:
    """When the next download should start; None while paused (needs a login)."""
    allowed = next_allowed_attempt(state, settings)
    if allowed is None:
        return None
    last = _parse(state.last_cycle_at) or _parse(state.last_attempt_at)
    base = last + every if last else now
    return max(max(base, allowed) + jitter, now)


def lift_login_pause(state: State, settings: DownloaderSettings) -> bool:
    """A login pause written before runs could sign in by themselves (so without the failed
    attempts that justify one now) is lifted: the next run tries the saved login. Challenge
    pauses always stay."""
    if (state.needs_attention in ("login_required", "sign_in_rejected")
            and state.failed_logins < settings.max_failed_logins):
        state.needs_attention = None
        return True
    return False


@dataclass
class Daemon:
    settings: DownloaderSettings
    every: timedelta
    max_jitter_s: int
    run: Callable[[bool], int]  # force -> exit code (the locked run_once)
    publish: Callable[..., None]  # (state, *, heartbeat, started_at=None, stopped=False)
    read_request: Callable[[], datetime | None]
    clear_request: Callable[[datetime], None]
    report: Callable[[str, datetime, str], None]  # (status, at, message): no file delivered
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    rng: random.Random = field(default_factory=random.Random)
    started_at: datetime | None = None
    _last_tick: datetime | None = None
    _last_heartbeat: datetime | None = None
    _hold_until: datetime | None = None
    _woke_at: datetime | None = None

    def _jitter(self) -> timedelta:
        return timedelta(seconds=self.rng.uniform(0, self.max_jitter_s))

    def _load(self) -> State:
        return State.load(self.settings.state_file)

    def _publish(self, state: State, now: datetime) -> None:
        state.save(self.settings.state_file)
        self.publish(state, heartbeat=True)
        self._last_heartbeat = now

    def start(self) -> None:
        now = self.clock()
        self.started_at = now
        state = self._load()
        due = _parse(state.next_run_at)
        if due is not None and now - due > MISSED_AFTER:
            self.report("missed", due, "Not downloaded at the planned time: the downloader was "
                        f"not running (Mac off or logged out). It was back {_minutes(now - due)} "
                        "later.")
        state.activity = "idle"
        state.next_run_at = None  # replanned on the first tick
        if lift_login_pause(state, self.settings):
            log.info("downloader.login_pause_lifted", hint="the saved login will be tried")
        state.save(self.settings.state_file)
        self.publish(state, heartbeat=True, started_at=now)
        self._last_heartbeat = now
        log.info("downloader.daemon_started")

    def stop(self) -> None:
        state = self._load()
        state.activity = "idle"
        state.save(self.settings.state_file)
        self.publish(state, heartbeat=False, stopped=True)
        log.info("downloader.daemon_stopped")

    def tick(self) -> None:
        now = self.clock()
        if self._last_tick and now - self._last_tick > SLEEP_GAP:
            self._woke_at, self._hold_until = now, now + WAKE_GRACE
            log.info("downloader.resumed_after_sleep", gap=str(now - self._last_tick))
        self._last_tick = now

        state = self._load()
        changed = False
        if state.needs_attention:
            if state.next_run_at is not None:
                state.next_run_at, changed = None, True
        elif state.next_run_at is None:
            planned = plan_next_run(state, self.settings, now, self.every, self._jitter())
            state.next_run_at, changed = planned.isoformat(), True

        held = self._hold_until is not None and now < self._hold_until
        request = self.read_request()
        if request is not None and not state.needs_attention and not held:
            self._download(state, now, force=True, request=request)
            return
        due = _parse(state.next_run_at)
        if due is not None and now >= due and not held:
            self._download(state, now, force=False)
            return
        if changed or self._last_heartbeat is None or now - self._last_heartbeat >= HEARTBEAT:
            self._publish(state, now)

    def _download(self, state: State, now: datetime, *, force: bool,
                  request: datetime | None = None) -> None:
        due = _parse(state.next_run_at)
        if due is not None and now - due > MISSED_AFTER:
            why = ("the Mac was asleep" if self._woke_at and self._woke_at > due
                   else "the downloader was stuck, see its log")
            self.report("missed", due, f"Not downloaded at the planned time: {why}. "
                        f"The download started {_minutes(now - due)} late.")
        state.activity = "downloading"
        self._publish(state, now)
        log.info("downloader.cycle", requested=request is not None)
        try:
            self.run(force)
        except Exception as exc:  # noqa: BLE001 - run_once already alerts; never kill the loop
            log.exception("downloader.cycle_crashed")
            self.report("failed", now, f"The download crashed: {type(exc).__name__}: {exc}")
        finally:
            if request is not None:
                self.clear_request(request)
            state = self._load()  # run_once updated attempts/failures/pause
            state.activity = "idle"
            state.last_cycle_at = now.isoformat()  # the next hour counts from this download
            planned = plan_next_run(state, self.settings, self.clock(), self.every,
                                    self._jitter())
            state.next_run_at = planned.isoformat() if planned else None
            self._publish(state, self.clock())


def serve(daemon: Daemon, stop: threading.Event, poll_seconds: float = 5.0) -> None:
    daemon.start()
    try:
        while not stop.is_set():
            try:
                daemon.tick()
            except Exception:  # noqa: BLE001 - e.g. database briefly down: keep going
                log.exception("downloader.tick_failed")
            stop.wait(poll_seconds)
    finally:
        daemon.stop()
