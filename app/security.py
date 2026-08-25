"""Application security helpers: CSRF, headers, login throttling and redaction."""
from __future__ import annotations

import re
import threading
import time
from collections import deque

from app.sensitive_data import is_sensitive_key, redact_sensitive_text

SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passport|secret|token|api[_-]?key|authorization|cookie|phone)"
)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_FAILURES = 5
_login_attempts: dict[str, deque[float]] = {}
_login_lock = threading.Lock()
_SETUP_WINDOW_SECONDS = 15 * 60
_SETUP_MAX_FAILURES = 5
_setup_attempts: dict[str, deque[float]] = {}
_setup_lock = threading.Lock()
_RATE_LIMIT_MAX_IDENTITIES = 4096


def _prune_attempts(
    attempts_by_identity: dict[str, deque[float]],
    identity: str,
    *,
    now: float,
    window_seconds: float,
) -> deque[float] | None:
    attempts = attempts_by_identity.get(identity)
    if attempts is None:
        return None
    while attempts and now - attempts[0] > window_seconds:
        attempts.popleft()
    if not attempts:
        attempts_by_identity.pop(identity, None)
        return None
    return attempts


def _record_failure(
    attempts_by_identity: dict[str, deque[float]],
    identity: str,
    *,
    now: float,
    window_seconds: float,
) -> None:
    attempts = _prune_attempts(
        attempts_by_identity, identity, now=now, window_seconds=window_seconds
    )
    if attempts is None:
        if len(attempts_by_identity) >= _RATE_LIMIT_MAX_IDENTITIES:
            for key in tuple(attempts_by_identity):
                _prune_attempts(
                    attempts_by_identity, key, now=now, window_seconds=window_seconds
                )
            while len(attempts_by_identity) >= _RATE_LIMIT_MAX_IDENTITIES:
                oldest = min(
                    attempts_by_identity,
                    key=lambda key: attempts_by_identity[key][-1],
                )
                attempts_by_identity.pop(oldest, None)
        attempts = deque()
        attempts_by_identity[identity] = attempts
    attempts.append(now)


def redact_config(items: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in items.items():
        if value and (SENSITIVE_KEY_RE.search(key) or is_sensitive_key(key)):
            redacted[key] = "********"
        else:
            # URL/userinfo 等凭据可能藏在普通 URL 配置值中，不能只依赖键名。
            redacted[key] = redact_sensitive_text(value)
    return redacted


def login_rate_limited(identity: str) -> bool:
    now = time.monotonic()
    with _login_lock:
        attempts = _prune_attempts(
            _login_attempts, identity, now=now, window_seconds=_LOGIN_WINDOW_SECONDS
        )
        return bool(attempts and len(attempts) >= _LOGIN_MAX_FAILURES)


def record_login_failure(identity: str) -> None:
    now = time.monotonic()
    with _login_lock:
        _record_failure(
            _login_attempts, identity, now=now, window_seconds=_LOGIN_WINDOW_SECONDS
        )


def clear_login_failures(identity: str) -> None:
    with _login_lock:
        _login_attempts.pop(identity, None)


def setup_rate_limited(identity: str) -> bool:
    now = time.monotonic()
    with _setup_lock:
        attempts = _prune_attempts(
            _setup_attempts, identity, now=now, window_seconds=_SETUP_WINDOW_SECONDS
        )
        return bool(attempts and len(attempts) >= _SETUP_MAX_FAILURES)


def record_setup_failure(identity: str) -> None:
    now = time.monotonic()
    with _setup_lock:
        _record_failure(
            _setup_attempts, identity, now=now, window_seconds=_SETUP_WINDOW_SECONDS
        )


def clear_setup_failures(identity: str) -> None:
    with _setup_lock:
        _setup_attempts.pop(identity, None)
