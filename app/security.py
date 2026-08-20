"""Application security helpers: CSRF, headers, login throttling and redaction."""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque

from app.sensitive_data import is_sensitive_key, redact_sensitive_text

SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passport|secret|token|api[_-]?key|authorization|cookie|phone)"
)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_FAILURES = 5
_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_login_lock = threading.Lock()
_SETUP_WINDOW_SECONDS = 15 * 60
_SETUP_MAX_FAILURES = 5
_setup_attempts: dict[str, deque[float]] = defaultdict(deque)
_setup_lock = threading.Lock()


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
        attempts = _login_attempts[identity]
        while attempts and now - attempts[0] > _LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        return len(attempts) >= _LOGIN_MAX_FAILURES


def record_login_failure(identity: str) -> None:
    with _login_lock:
        _login_attempts[identity].append(time.monotonic())


def clear_login_failures(identity: str) -> None:
    with _login_lock:
        _login_attempts.pop(identity, None)


def setup_rate_limited(identity: str) -> bool:
    now = time.monotonic()
    with _setup_lock:
        attempts = _setup_attempts[identity]
        while attempts and now - attempts[0] > _SETUP_WINDOW_SECONDS:
            attempts.popleft()
        return len(attempts) >= _SETUP_MAX_FAILURES


def record_setup_failure(identity: str) -> None:
    with _setup_lock:
        _setup_attempts[identity].append(time.monotonic())


def clear_setup_failures(identity: str) -> None:
    with _setup_lock:
        _setup_attempts.pop(identity, None)
