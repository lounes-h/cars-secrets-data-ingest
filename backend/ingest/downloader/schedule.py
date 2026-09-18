"""When will the next download happen? Pure function shared by the downloader and the API.

The scheduler wakes the downloader every `interval` (then it waits a random 0..jitter), and a
wake-up only downloads if pacing allows (`next_allowed`). So the next download starts in the
window [tick, tick + jitter] of the first scheduler tick at or after `next_allowed`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Status = Literal["scheduled", "starting", "paused", "late", "unknown"]


@dataclass(frozen=True)
class NextRun:
    status: Status
    at: datetime | None = None  # start of the window
    window_end: datetime | None = None  # latest start, after the random delay


def next_run(
    now: datetime,
    *,
    last_invocation: datetime | None,
    interval_s: int | None,
    jitter_s: int,
    next_allowed: datetime | None,
    needs_attention: str | None,
) -> NextRun:
    if needs_attention:
        return NextRun("paused")
    if last_invocation is None or not interval_s:
        return NextRun("unknown")  # never woken by the scheduler (not installed yet)
    interval = timedelta(seconds=interval_s)
    jitter = timedelta(seconds=max(jitter_s, 0))
    # The wake-up that is running now (or due) is the one right after the last invocation.
    tick = last_invocation + interval
    if next_allowed is not None and next_allowed > tick:
        # Skipped wake-ups until pacing allows a download.
        tick += interval * math.ceil((next_allowed - tick) / interval)
    if now <= tick:
        return NextRun("scheduled", tick, tick + jitter)
    if now <= tick + jitter + timedelta(minutes=5):
        return NextRun("starting", tick, tick + jitter)  # within its random delay
    # A wake-up should have happened: the Mac was asleep, or the schedule was removed.
    return NextRun("late", tick, tick + jitter)
