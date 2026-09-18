from datetime import UTC, datetime, timedelta

from ingest.downloader.schedule import next_run

T0 = datetime(2026, 9, 18, 2, 0, tzinfo=UTC)  # last scheduler wake-up
H = 3600


def nr(now, **kw):
    args = dict(last_invocation=T0, interval_s=H, jitter_s=300, next_allowed=None,
                needs_attention=None)
    return next_run(now, **{**args, **kw})


def test_next_wake_up_after_the_last_one():
    r = nr(T0 + timedelta(minutes=20))
    assert (r.status, r.at, r.window_end) == (
        "scheduled", T0 + timedelta(hours=1), T0 + timedelta(hours=1, minutes=5))


def test_backoff_skips_wake_ups_until_allowed():
    # after failures the next download is only allowed 2h30 later: the 3rd wake-up downloads
    r = nr(T0 + timedelta(minutes=5), next_allowed=T0 + timedelta(hours=2, minutes=30))
    assert r.status == "scheduled" and r.at == T0 + timedelta(hours=3)


def test_allowed_exactly_on_a_wake_up():
    r = nr(T0, next_allowed=T0 + timedelta(hours=2))
    assert r.at == T0 + timedelta(hours=2)


def test_within_the_random_delay_it_is_starting():
    r = nr(T0 + timedelta(hours=1, minutes=3))
    assert r.status == "starting"


def test_missed_wake_up_is_late():
    r = nr(T0 + timedelta(hours=1, minutes=20))
    assert r.status == "late" and r.at == T0 + timedelta(hours=1)


def test_paused_and_unknown():
    assert nr(T0, needs_attention="login_required").status == "paused"
    assert nr(T0, last_invocation=None).status == "unknown"
    assert nr(T0, interval_s=None).status == "unknown"
