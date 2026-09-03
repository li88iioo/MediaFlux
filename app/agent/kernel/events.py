"""Agent Kernel 的规范事件协议。"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class AgentEventType(StrEnum):
    TURN_STARTED = "turn.started"
    CAPABILITIES_SELECTED = "capabilities.selected"
    MODEL_STARTED = "model.started"
    MODEL_DELTA = "model.delta"
    MODEL_TOOL_CALL = "model.tool_call"
    TOOL_STARTED = "tool.started"
    TOOL_PROGRESS = "tool.progress"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    EFFECT_PREVIEW_STARTED = "effect.preview_started"
    EFFECT_APPROVAL_REQUIRED = "effect.approval_required"
    EFFECT_COMPLETED = "effect.completed"
    EFFECT_FAILED = "effect.failed"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    TURN_CANCELLED = "turn.cancelled"


TERMINAL_EVENT_TYPES = frozenset(
    {
        AgentEventType.TURN_COMPLETED,
        AgentEventType.TURN_FAILED,
        AgentEventType.TURN_CANCELLED,
    }
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """入口层唯一允许消费的事件 DTO。"""

    type: AgentEventType
    session_id: str
    turn_id: str
    request_id: str
    sequence: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: str = field(default_factory=utc_now_iso)
    event_id: str = field(default_factory=lambda: secrets.token_urlsafe(12))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type.value,
            "occurred_at": self.occurred_at,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "payload": deepcopy(dict(self.payload)),
        }


class EventFactory:
    """为单个回合生成单调递增的真实事件。"""

    __slots__ = ("_sequence", "request_id", "session_id", "turn_id")

    def __init__(self, *, session_id: str, turn_id: str, request_id: str) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.request_id = request_id
        self._sequence = 0

    def create(
        self,
        event_type: AgentEventType,
        payload: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> AgentEvent:
        self._sequence += 1
        data = dict(payload or {})
        data.update(extra)
        return AgentEvent(
            type=event_type,
            session_id=self.session_id,
            turn_id=self.turn_id,
            request_id=self.request_id,
            sequence=self._sequence,
            payload=data,
        )
