"""基于真实 AgentEvent 流的轻量运行指标。"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter, deque
from collections.abc import AsyncGenerator, AsyncIterable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from .events import TERMINAL_EVENT_TYPES, AgentEvent, AgentEventType
from .task_state import classify_public_task


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
    task_status: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "turn_status": self.status}


@dataclass(slots=True)
class _TurnObservation:
    started: float = field(default_factory=lambda: time.monotonic())
    first_event_at: float | None = None
    first_text_at: float | None = None
    model_calls: int = 0
    tool_calls: int = 0
    request_id: str = ""
    last_terminal: AgentEvent | None = None

    def observe(self, event: AgentEvent) -> None:
        now = time.monotonic()
        self.request_id = self.request_id or event.request_id
        if self.first_event_at is None:
            self.first_event_at = now
        if event.type is AgentEventType.MODEL_DELTA and self.first_text_at is None:
            self.first_text_at = now
        elif event.type is AgentEventType.MODEL_STARTED:
            self.model_calls += 1
        elif event.type is AgentEventType.TOOL_STARTED:
            self.tool_calls += 1
        if event.type in TERMINAL_EVENT_TYPES:
            self.last_terminal = event


class KernelMetrics:
    """有界、线程安全且不记录用户正文或工具参数。"""

    def __init__(self, *, history_size: int = 500) -> None:
        self._lock = threading.Lock()
        self._history: deque[TurnMeasurement] = deque(maxlen=max(20, history_size))
        self._status_counts: Counter[str] = Counter()
        self._task_status_counts: Counter[str] = Counter()
        self._detached_deliveries = 0

    def record_delivery_detached(self) -> None:
        """消费者离开后执行转入后台；不改变任务终态统计。"""
        with self._lock:
            self._detached_deliveries += 1

    async def track(
        self,
        events: AsyncIterable[AgentEvent],
        *,
        channel: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        with self.measure(channel=channel) as measurement:
            async for event in events:
                measurement.observe(event)
                yield event

    @contextmanager
    def measure(self, *, channel: str, request_id: str = "") -> Generator[_TurnObservation, None, None]:
        """由执行器持有直到真正结束；页面断流不代表执行取消。"""
        observation = _TurnObservation(request_id=request_id)
        status = "interrupted"
        task_status = "unknown"
        exhausted = False
        try:
            yield observation
            # 只在执行正常结束后采信最后终态，异常收尾不能计为成功。
            if observation.last_terminal is not None:
                status = _terminal_status(observation.last_terminal)
                task_status = classify_public_task(observation.last_terminal.payload.get("task"))
            exhausted = True
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception:
            status = "failed"
            raise
        finally:
            ended = time.monotonic()
            measurement = TurnMeasurement(
                request_id=observation.request_id,
                channel=(channel or "api")[:20],
                status=status,
                ttfe_ms=max(0, int(((observation.first_event_at or ended) - observation.started) * 1000)),
                ttft_ms=(
                    max(0, int((observation.first_text_at - observation.started) * 1000))
                    if observation.first_text_at is not None
                    else None
                ),
                total_ms=max(0, int((ended - observation.started) * 1000)),
                model_calls=observation.model_calls,
                tool_calls=observation.tool_calls,
                task_status=task_status,
            )
            with self._lock:
                self._history.append(measurement)
                self._status_counts[status] += 1
                if exhausted and observation.last_terminal is not None:
                    self._task_status_counts[task_status] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            history = list(self._history)
            statuses = dict(self._status_counts)
            task_statuses = dict(self._task_status_counts)
            detached_deliveries = self._detached_deliveries
        return {
            "turns": sum(statuses.values()),
            "statuses": statuses,
            "detached_deliveries": detached_deliveries,
            # Last terminal snapshot per normally exhausted, recorded turn;
            # not distinct tasks, and not a task success rate.
            "task_statuses_by_turn": task_statuses,
            "task_statuses_by_turn_unit": "正常耗尽并已记录的回合中最后终态携带的会话任务快照分类次数",
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
    return (ordered[index])


def _average(values: list[int]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0
