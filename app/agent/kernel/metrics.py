"""基于真实 AgentEvent 流的轻量运行指标。"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import asdict, dataclass
from typing import Any

from .events import TERMINAL_EVENT_TYPES, AgentEvent, AgentEventType


@dataclass(frozen=True, slots=True)
class TurnMeasurement:
    request_id: str
    channel: str
    status: str
    ttfe_ms: int
    ttft_ms: int | None
    total_ms: int
    model_calls: int
    tool_calls: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KernelMetrics:
    """有界、线程安全且不记录用户正文或工具参数。"""

    def __init__(self, *, history_size: int = 500) -> None:
        self._lock = threading.Lock()
        self._history: deque[TurnMeasurement] = deque(maxlen=max(20, int(history_size)))
        self._status_counts: Counter[str] = Counter()

    async def track(
        self,
        events: AsyncIterable[AgentEvent],
        *,
        channel: str,
    ) -> AsyncIterator[AgentEvent]:
        started = time.monotonic()
        first_event_at: float | None = None
        first_text_at: float | None = None
        model_calls = 0
        tool_calls = 0
        request_id = ""
        status = "interrupted"
        async for event in events:
            now = time.monotonic()
            request_id = request_id or event.request_id
            if first_event_at is None:
                first_event_at = now
            if event.type is AgentEventType.MODEL_DELTA and first_text_at is None:
                first_text_at = now
            elif event.type is AgentEventType.MODEL_STARTED:
                model_calls += 1
            elif event.type is AgentEventType.TOOL_STARTED:
                tool_calls += 1
            if event.type in TERMINAL_EVENT_TYPES:
                status = _terminal_status(event)
            yield event
        ended = time.monotonic()
        measurement = TurnMeasurement(
            request_id=request_id,
            channel=str(channel or "api")[:20],
            status=status,
            ttfe_ms=max(0, int(((first_event_at or ended) - started) * 1000)),
            ttft_ms=(
                max(0, int((first_text_at - started) * 1000))
                if first_text_at is not None
                else None
            ),
            total_ms=max(0, int((ended - started) * 1000)),
            model_calls=model_calls,
            tool_calls=tool_calls,
        )
        with self._lock:
            self._history.append(measurement)
            self._status_counts[status] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            history = list(self._history)
            statuses = dict(self._status_counts)
        return {
            "turns": sum(statuses.values()),
            "statuses": statuses,
            "latency_ms": {
                "ttfe_p50": _percentile([item.ttfe_ms for item in history], 0.50),
                "ttfe_p95": _percentile([item.ttfe_ms for item in history], 0.95),
                "ttft_p50": _percentile(
                    [item.ttft_ms for item in history if item.ttft_ms is not None], 0.50
                ),
                "ttft_p95": _percentile(
                    [item.ttft_ms for item in history if item.ttft_ms is not None], 0.95
                ),
                "total_p50": _percentile([item.total_ms for item in history], 0.50),
                "total_p95": _percentile([item.total_ms for item in history], 0.95),
            },
            "average_model_calls": _average([item.model_calls for item in history]),
            "average_tool_calls": _average([item.tool_calls for item in history]),
            "recent": [item.to_dict() for item in history[-20:]],
        }


def _terminal_status(event: AgentEvent) -> str:
    if event.type is AgentEventType.TURN_COMPLETED:
        return str(event.payload.get("status") or "success")[:40]
    if event.type is AgentEventType.TURN_CANCELLED:
        return "cancelled"
    return "failed"


def _percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return int(ordered[index])


def _average(values: list[int]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0
