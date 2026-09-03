"""Provider 无关的模型流协议。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .state import CancellationToken


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any]


class ModelEventType(StrEnum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    USAGE = "usage"
    FINISH = "finish"


@dataclass(frozen=True, slots=True)
class ModelEvent:
    type: ModelEventType
    text: str = ""
    tool_call: ModelToolCall | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    finish_reason: str = ""
    raw: Any = None


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    tool_call_id: str = ""
    tool_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            result["tool_calls"] = [
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": dict(call.arguments),
                }
                for call in self.tool_calls
            ]
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        if self.tool_name:
            result["tool_name"] = self.tool_name
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelMessage:
        calls: list[ModelToolCall] = []
        for raw in value.get("tool_calls") or ():
            if isinstance(raw, Mapping):
                calls.append(
                    ModelToolCall(
                        call_id=str(raw.get("call_id") or ""),
                        name=str(raw.get("name") or ""),
                        arguments=dict(raw.get("arguments") or {}),
                    )
                )
        return cls(
            role=str(value.get("role") or "user"),
            content=str(value.get("content") or ""),
            tool_calls=tuple(calls),
            tool_call_id=str(value.get("tool_call_id") or ""),
            tool_name=str(value.get("tool_name") or ""),
        )


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system_prompt: str
    messages: Sequence[ModelMessage]
    tools: Sequence[Mapping[str, Any]]
    max_output_tokens: int = 1_500
    round_index: int = 0


class ModelAdapter(Protocol):
    async def stream(
        self,
        request: ModelRequest,
        *,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]: ...
