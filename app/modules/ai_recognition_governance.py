"""AI 媒体识别调用治理：限速、并发、日预算与短路熔断。"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable

from app.config import get


class AIRecognitionGovernanceError(RuntimeError):
    """AI 识别请求在访问 Provider 前被本地治理拒绝。"""


@dataclass(frozen=True, slots=True)
class AIRecognitionGovernanceConfig:
    requests_per_minute: int
    daily_request_limit: int
    max_concurrency: int
    circuit_breaker_seconds: int


@dataclass(slots=True)
class _ProviderState:
    semaphore: threading.BoundedSemaphore
    requests_per_minute: int
    max_concurrency: int
    request_times: deque[float] = field(default_factory=deque)
    consecutive_provider_failures: int = 0
    circuit_open_until: float = 0.0
    lock: threading.RLock = field(default_factory=threading.RLock)


class AIRecognitionAttemptLease:
    """一次真实协议请求的有界租约。"""

    def __init__(self, state: _ProviderState) -> None:
        self._state = state
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._state.semaphore.release()

    def __enter__(self) -> "AIRecognitionAttemptLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


_states: dict[str, _ProviderState] = {}
_states_lock = threading.RLock()


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(get(name, str(default)) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def governance_config() -> AIRecognitionGovernanceConfig:
    return AIRecognitionGovernanceConfig(
        requests_per_minute=_bounded_int(
            "AI_RECOGNITION_REQUESTS_PER_MINUTE", 6, minimum=1, maximum=30
        ),
        daily_request_limit=_bounded_int(
            "AI_RECOGNITION_DAILY_REQUEST_LIMIT", 100, minimum=1, maximum=100_000
        ),
        max_concurrency=_bounded_int(
            "AI_RECOGNITION_MAX_CONCURRENCY", 2, minimum=1, maximum=8
        ),
        circuit_breaker_seconds=_bounded_int(
            "AI_RECOGNITION_CIRCUIT_BREAKER_SECONDS", 60, minimum=10, maximum=600
        ),
    )


def provider_fingerprint(
    *, base_url: str, model: str, api_key: str = "", protocol: str = "auto"
) -> str:
    """生成不含明文凭据的 Provider 身份。"""
    digest = hashlib.sha256()
    for value in (base_url, model, api_key, protocol):
        digest.update(str(value or "").encode("utf-8", "ignore"))
        digest.update(b"\0")
    return digest.hexdigest()


def _state_for(fingerprint: str, config: AIRecognitionGovernanceConfig) -> _ProviderState:
    with _states_lock:
        state = _states.get(fingerprint)
        if (
            state is None
            or state.requests_per_minute != config.requests_per_minute
            or state.max_concurrency != config.max_concurrency
        ):
            state = _ProviderState(
                semaphore=threading.BoundedSemaphore(config.max_concurrency),
                requests_per_minute=config.requests_per_minute,
                max_concurrency=config.max_concurrency,
            )
            _states[fingerprint] = state
        return state


def _reserve_daily_request(limit: int) -> bool:
    from app.database import (
        current_agent_web_search_usage_date,
        reserve_agent_web_search_credits,
    )

    return reserve_agent_web_search_credits(
        provider="ai_recognition",
        usage_date=current_agent_web_search_usage_date(),
        cost=1,
        daily_limit=limit,
    )


def acquire_ai_recognition_attempt(
    fingerprint: str,
    *,
    reserve_daily: Callable[[int], bool] = _reserve_daily_request,
) -> AIRecognitionAttemptLease:
    """按熔断、分钟限速、并发、日预算的顺序取得一次请求资格。

    并发槽在扣减持久化日额度前取得，避免任务已经没有执行容量时仍消耗
    用户的当日预算。日额度预留失败时会立即归还槽位。
    """
    config = governance_config()
    state = _state_for(fingerprint, config)
    now = time.monotonic()
    with state.lock:
        if state.circuit_open_until > now:
            remaining = max(1, round(state.circuit_open_until - now))
            raise AIRecognitionGovernanceError(
                f"AI 识别服务熔断中，请约 {remaining} 秒后重试"
            )
        while state.request_times and now - state.request_times[0] >= 60.0:
            state.request_times.popleft()
        if len(state.request_times) >= config.requests_per_minute:
            raise AIRecognitionGovernanceError("AI 识别分钟请求额度已用完，请稍后重试")
        if not state.semaphore.acquire(blocking=False):
            raise AIRecognitionGovernanceError("AI 识别并发任务已满，请稍后重试")
        try:
            if not reserve_daily(config.daily_request_limit):
                raise AIRecognitionGovernanceError("AI 识别今日请求额度已用完")
        except Exception:
            state.semaphore.release()
            raise
        state.request_times.append(now)
    return AIRecognitionAttemptLease(state)


def _retry_after_seconds(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return max(1, min(int(float(text)), 3600))
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(text)
        seconds = int(parsed.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None
    return max(1, min(seconds, 3600))


def record_ai_recognition_success(fingerprint: str) -> None:
    config = governance_config()
    state = _state_for(fingerprint, config)
    with state.lock:
        state.consecutive_provider_failures = 0
        state.circuit_open_until = 0.0


def record_ai_recognition_failure(
    fingerprint: str,
    *,
    status_code: int | None = None,
    retry_after: object = "",
    provider_failure: bool = True,
) -> None:
    if not provider_failure:
        return
    config = governance_config()
    state = _state_for(fingerprint, config)
    now = time.monotonic()
    with state.lock:
        if status_code in {401, 403}:
            state.consecutive_provider_failures = 0
            state.circuit_open_until = max(
                state.circuit_open_until,
                now + max(config.circuit_breaker_seconds, 300),
            )
            return
        if status_code == 429:
            state.consecutive_provider_failures = 0
            cooldown = _retry_after_seconds(retry_after) or config.circuit_breaker_seconds
            state.circuit_open_until = max(state.circuit_open_until, now + cooldown)
            return
        if status_code is not None and status_code < 500:
            state.consecutive_provider_failures = 0
            return
        state.consecutive_provider_failures += 1
        if state.consecutive_provider_failures >= 3:
            state.consecutive_provider_failures = 0
            state.circuit_open_until = max(
                state.circuit_open_until,
                now + config.circuit_breaker_seconds,
            )


def clear_ai_recognition_governance() -> None:
    """清空进程内限速与熔断状态，供配置热更新后立即生效。"""
    with _states_lock:
        _states.clear()


def reset_ai_recognition_governance_for_tests() -> None:
    clear_ai_recognition_governance()
