"""Provider 无关的模型流协议。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from .state_types import CancellationToken


@dataclass(frozen=True, slots=True)
class ProviderState:
    """Private, immutable wire state; provenance is assigned only by the adapter.

    The JSON layout contains native private blocks and references to ordinary
    tool calls, never a second copy of executable arguments or arbitrary roles.
    Ordinary text blocks are untrusted segmentation hints, not canonical text:
    outbound projection must reconcile them with ModelMessage.content, even
    for legacy checkpoints. Never edit signed/opaque blocks to reconcile text.
    """

    protocol: str
    model: str
    endpoint: str
    layout_json: str = field(repr=False)

    def __post_init__(self) -> None:
        # Reject whole states: signed/opaque values must never be truncated.
        if len(self.layout_json.encode("utf-8")) > 128 * 1024:
            raise ValueError("Provider private state exceeds limit")

    def to_dict(self) -> dict[str, str]:
        return {
            "protocol": self.protocol,
            "model": self.model,
            "endpoint": self.endpoint,
            "layout_json": self.layout_json,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProviderState:
        return cls(**{
            key: str(value[key])
            for key in ("protocol", "model", "endpoint", "layout_json")
        })

    def layout(self) -> list[dict[str, Any]]:
        return json.loads(self.layout_json)


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any] | None
    argument_error: Literal["invalid_json"] | None = None
    raw_arguments: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.argument_error is None:
            # Ordinary DTOs retain their old construction boundary: Session
            # rejects non-Mapping roots atomically with structured feedback.
            if self.raw_arguments is not None:
                raise ValueError("Invalid tool arguments state")
        elif (self.argument_error != "invalid_json" or self.arguments is not None
              or not isinstance(self.raw_arguments, str)
              or len(self.raw_arguments.encode("utf-8")) > 32_768
              or not all(isinstance(v, str) and v.strip() for v in (self.call_id, self.name))):
            raise ValueError("Invalid tool arguments state")

    def to_dict(self) -> dict[str, Any]:
        result = {"call_id": self.call_id, "name": self.name,
                  "arguments": dict(self.arguments) if self.arguments is not None else None}
        if self.argument_error is not None:
            result.update(argument_error=self.argument_error, raw_arguments=self.raw_arguments)
        return result


class ModelEventType(StrEnum):
    TEXT_DELTA = "text_delta"
    REASONING_SUMMARY_DELTA = "reasoning_summary_delta"
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
    provider_state: ProviderState | None = field(default=None, repr=False)
    summary_id: str = ""


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    tool_call_id: str = ""
    tool_name: str = ""
    effect_plan_id: str = ""
    completion_receipt_id: str = ""
    provider_state: ProviderState | None = field(default=None, repr=False)
    history_id: str = ""  # 本地事件身份；正文相同的两次用户输入不能合并。

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.role == "assistant" and self.provider_state is not None:
            result["provider_state"] = self.provider_state.to_dict()
        if self.tool_calls:
            result["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        if self.tool_name:
            result["tool_name"] = self.tool_name
        if self.effect_plan_id:
            result["effect_plan_id"] = self.effect_plan_id
        if self.completion_receipt_id:
            result["completion_receipt_id"] = self.completion_receipt_id
        if self.role == "user" and self.history_id:
            result["history_id"] = self.history_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelMessage:
        calls: list[ModelToolCall] = []
        for raw in value.get("tool_calls") or ():
            if isinstance(raw, Mapping):
                argument_error = raw.get("argument_error")
                call_id = raw.get("call_id")
                name = raw.get("name")
                arguments = raw.get("arguments")
                if argument_error is None:
                    call_id = str(call_id or "")
                    name = str(name or "")
                    arguments = dict(arguments or {})
                elif not isinstance(call_id, str) or not isinstance(name, str):
                    raise ValueError("Invalid tool arguments state")
                calls.append(
                    ModelToolCall(
                        call_id=call_id,
                        name=name,
                        arguments=arguments,
                        argument_error=argument_error,
                        raw_arguments=raw.get("raw_arguments"),
                    )
                )
        return cls(
            role=str(value.get("role") or "user"),
            content=str(value.get("content") or ""),
            tool_calls=tuple(calls),
            tool_call_id=str(value.get("tool_call_id") or ""),
            tool_name=str(value.get("tool_name") or ""),
            effect_plan_id=str(value.get("effect_plan_id") or ""),
            completion_receipt_id=str(value.get("completion_receipt_id") or ""),
            provider_state=ProviderState.from_dict(value["provider_state"])
            if value.get("role") == "assistant" and isinstance(value.get("provider_state"), Mapping) else None,
            history_id=str(value.get("history_id") or "") if value.get("role") == "user" else "",
        )


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system_prompt: str
    messages: Sequence[ModelMessage]
    tools: Sequence[Mapping[str, Any]]
    max_output_tokens: int = 1_500
    round_index: int = 0
    model: str = ""  # 由受控会话偏好选择，不改变 Provider URL、凭据或协议。
    allow_parallel_tool_calls: bool = False  # 仅接受服务端窗口授予的显式许可。
    # system_prompt 开头跨回合不变的字符数；Provider 据此放置提示缓存断点。
    stable_system_chars: int = 0


class ModelAdapter(Protocol):
    def stream(
        self,
        request: ModelRequest,
        *,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]: ...


@runtime_checkable
class BudgetedModelAdapter(ModelAdapter, Protocol):
    """可选预算能力；不依赖具体 Provider 或渠道。"""

    def budget_for_model(self, model: str, context: int, output: int) -> tuple[int, int, bool]: ...
