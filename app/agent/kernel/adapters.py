"""Web、Telegram 与未来 API 共用的 AgentEvent 消费层。

适配器只负责协议转换和显示状态聚合，不得做领域路由、工具选择或确认执行。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .events import AgentEvent, AgentEventType

EventObserver = Callable[[AgentEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ApprovalView:
    """供任意入口渲染的统一确认卡数据。"""

    plan_id: str
    tool_name: str
    effect: str
    preview: Mapping[str, Any]
    result: Mapping[str, Any]
    expires_at: str
    confirmation: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "tool_name": self.tool_name,
            "effect": self.effect,
            "preview": deepcopy(dict(self.preview)),
            "result": deepcopy(dict(self.result)),
            "confirmation": deepcopy(dict(self.confirmation)),
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class TurnView:
    """一个事件流结束后的规范公开视图。"""

    session_id: str
    turn_id: str
    request_id: str
    status: str
    answer: str = ""
    approval: ApprovalView | None = None
    effect_result: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    tool_calls: tuple[str, ...] = ()
    event_count: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in {
            "success",
            "approval_required",
            "effect_completed",
            "failed",
            "cancelled",
        }

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "status": self.status,
            "answer": self.answer,
            "effect_result": deepcopy(dict(self.effect_result)),
            "error": {
                "code": self.error_code,
                "message": self.error_message,
            }
            if self.error_code or self.error_message
            else None,
            "tool_calls": list(self.tool_calls),
            "event_count": self.event_count,
        }
        payload["approval"] = self.approval.to_dict() if self.approval else None
        return payload


class TurnViewBuilder:
    """按真实事件顺序聚合公开状态；Web/TG 必须共用此实现。"""

    def __init__(self) -> None:
        self._identity: tuple[str, str, str] | None = None
        self._last_sequence = 0
        self._count = 0
        self._status = "running"
        self._answer = ""
        self._approval: ApprovalView | None = None
        self._effect_result: dict[str, Any] = {}
        self._error_code = ""
        self._error_message = ""
        self._tool_calls: list[str] = []

    def apply(self, event: AgentEvent) -> None:
        identity = (event.session_id, event.turn_id, event.request_id)
        if self._identity is None:
            self._identity = identity
        elif identity != self._identity:
            raise ValueError("一个 TurnView 不能混入不同回合的事件")
        if event.sequence <= self._last_sequence:
            raise ValueError("AgentEvent sequence 必须严格递增")
        self._last_sequence = event.sequence
        self._count += 1
        payload = dict(event.payload)

        if event.type is AgentEventType.MODEL_TOOL_CALL:
            tool_name = str(payload.get("tool") or "").strip()
            if tool_name:
                self._tool_calls.append(tool_name)
        elif event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED:
            plan = payload.get("plan")
            result = payload.get("result")
            if isinstance(plan, Mapping):
                preview = plan.get("preview")
                confirmation = plan.get("confirmation")
                self._approval = ApprovalView(
                    plan_id=str(plan.get("plan_id") or ""),
                    tool_name=str(plan.get("tool_name") or payload.get("tool") or ""),
                    effect=str(plan.get("effect") or "WRITE"),
                    preview=deepcopy(dict(preview))
                    if isinstance(preview, Mapping)
                    else {},
                    result=deepcopy(dict(result))
                    if isinstance(result, Mapping)
                    else {},
                    confirmation=deepcopy(dict(confirmation))
                    if isinstance(confirmation, Mapping)
                    else {},
                    expires_at=str(plan.get("expires_at") or ""),
                )
                self._status = "approval_required"
        elif event.type is AgentEventType.EFFECT_COMPLETED:
            result = payload.get("result")
            self._effect_result = (
                deepcopy(dict(result)) if isinstance(result, Mapping) else {}
            )
            self._status = "effect_completed"
            self._error_code = ""
            self._error_message = ""
        elif event.type in {AgentEventType.TOOL_FAILED, AgentEventType.EFFECT_FAILED}:
            # 工具失败可由同一模型循环自愈；只有 turn.failed 才是终态。
            self._error_code = str(payload.get("code") or self._error_code)
            self._error_message = str(payload.get("message") or self._error_message)
        elif event.type is AgentEventType.TURN_COMPLETED:
            self._status = str(payload.get("status") or "success")
            self._answer = str(payload.get("answer") or self._answer)
            # tool.failed 是可自愈的中间事实；回合成功后不能把旧错误
            # 泄漏成 API/TG 的终态错误。
            self._error_code = ""
            self._error_message = ""
        elif event.type is AgentEventType.TURN_FAILED:
            self._status = "failed"
            self._error_code = str(payload.get("code") or "turn_failed")
            self._error_message = str(payload.get("message") or "Agent 运行失败")
        elif event.type is AgentEventType.TURN_CANCELLED:
            self._status = "cancelled"
            self._error_code = "cancelled"
            self._error_message = str(payload.get("reason") or "请求已取消")

    def build(self) -> TurnView:
        if self._identity is None:
            raise ValueError("事件流为空")
        session_id, turn_id, request_id = self._identity
        return TurnView(
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            status=self._status,
            answer=self._answer,
            approval=self._approval,
            effect_result=deepcopy(self._effect_result),
            error_code=self._error_code,
            error_message=self._error_message,
            tool_calls=tuple(self._tool_calls),
            event_count=self._count,
        )


async def consume_events(
    events: AsyncIterable[AgentEvent],
    *,
    observe: EventObserver | None = None,
) -> TurnView:
    """消费一次真实事件流，并生成入口无关的最终视图。"""

    builder = TurnViewBuilder()
    async for event in events:
        builder.apply(event)
        if observe is not None:
            await observe(event)
    return builder.build()


async def iter_ndjson(events: AsyncIterable[AgentEvent]) -> AsyncIterator[bytes]:
    """将事件原样编码为 Web NDJSON；不等待最终 trace 再伪流式回放。"""

    async for event in events:
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        yield f"{line}\n".encode()
