"""Single-account dashboard login: signed, http-only session cookie."""

from __future__ import annotations

import hmac
import threading
import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import Settings, get_settings

COOKIE_NAME = "ingest_session"
MIN_SECRET_LENGTH = 32


def check_auth_settings(settings: Settings) -> None:
    """Refuse to start with an unusable login configuration."""
    if not settings.dashboard_password:
        raise RuntimeError("DASHBOARD_PASSWORD is not set (run ./start.sh to generate one)")
    if len(settings.session_secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"SESSION_SECRET must be at least {MIN_SECRET_LENGTH} characters")


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="dashboard-session")


def credentials_valid(username: str, password: str, settings: Settings) -> bool:
    # Compare both fields in constant time, and always both, to not leak which one was wrong.
    user_ok = hmac.compare_digest(username.encode(), settings.dashboard_username.encode())
    pass_ok = hmac.compare_digest(password.encode(), settings.dashboard_password.encode())
    return user_ok and pass_ok


def issue_token(username: str, settings: Settings) -> str:
    return _serializer(settings).dumps({"u": username})


def read_token(token: str, settings: Settings) -> str | None:
    try:
        data = _serializer(settings).loads(token, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    username = data.get("u") if isinstance(data, dict) else None
    # A token for a renamed account is no longer valid.
    return username if username == settings.dashboard_username else None


def current_user(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> str:
    token = request.cookies.get(COOKIE_NAME)
    user = read_token(token, settings) if token else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    return user


class LoginRateLimiter:
    """At most `limit` failed attempts per client within `window` seconds."""

    def __init__(self, limit: int = 10, window: float = 300.0, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str) -> deque[float]:
        q = self._failures[key]
        cutoff = self.clock() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key)) >= self.limit

    def failure(self, key: str) -> None:
        with self._lock:
            self._prune(key).append(self.clock())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
