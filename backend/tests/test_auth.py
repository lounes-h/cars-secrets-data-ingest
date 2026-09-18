import pytest

from ingest.api import auth
from ingest.config import Settings


def make_settings(**kw) -> Settings:
    base = dict(_env_file=None, dashboard_username="admin", dashboard_password="pw",
                session_secret="x" * 40)
    return Settings(**{**base, **kw})


def test_token_roundtrip():
    s = make_settings()
    assert auth.read_token(auth.issue_token("admin", s), s) == "admin"


def test_token_signed_with_other_secret_is_invalid():
    token = auth.issue_token("admin", make_settings(session_secret="y" * 40))
    assert auth.read_token(token, make_settings()) is None
    assert auth.read_token("garbage", make_settings()) is None


def test_token_expires():
    s = make_settings(session_max_age_seconds=-1)
    assert auth.read_token(auth.issue_token("admin", s), s) is None


def test_token_for_renamed_account_is_invalid():
    token = auth.issue_token("admin", make_settings())
    assert auth.read_token(token, make_settings(dashboard_username="ops")) is None


def test_credentials_valid():
    s = make_settings()
    assert auth.credentials_valid("admin", "pw", s)
    assert not auth.credentials_valid("admin", "PW", s)
    assert not auth.credentials_valid("root", "pw", s)


def test_check_auth_settings():
    auth.check_auth_settings(make_settings())
    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        auth.check_auth_settings(make_settings(dashboard_password=""))
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        auth.check_auth_settings(make_settings(session_secret="short"))


def test_rate_limiter_blocks_then_forgets():
    now = [0.0]
    limiter = auth.LoginRateLimiter(limit=3, window=60, clock=lambda: now[0])
    for _ in range(3):
        assert not limiter.blocked("ip")
        limiter.failure("ip")
    assert limiter.blocked("ip")
    assert not limiter.blocked("other-ip")
    now[0] = 61
    assert not limiter.blocked("ip")


def test_rate_limiter_reset_on_success():
    limiter = auth.LoginRateLimiter(limit=2)
    limiter.failure("ip")
    limiter.failure("ip")
    limiter.reset("ip")
    assert not limiter.blocked("ip")
