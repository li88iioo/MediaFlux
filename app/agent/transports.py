"""MediaFlux 的 Web / Telegram 入口协议适配器。

校验传输字段及领域选择协议，构造 AgentInput，并转发内核事件与确认。
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing, suppress
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from app.agent.domains.resources.selection import normalize_selection
from app.agent.kernel.adapters import (
    EventObserver,
    TurnView,
    consume_events,
    iter_ndjson,
)
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import (
    AgentInput,
    CancellationToken,
    SelectionInvalidError,
)
from app.agent.owner_routes import TransportInputError, normalize_delivery_route

_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,160}$")
_PLAN_RE = re.compile(r"^[A-Za-z0-9_-]{16,96}$")


@dataclass(frozen=True, slots=True)
class QueryEnvelope:
    owner: str
    session_id: str
    message: str
    request_id: str = ""
    channel: str = "api"
    reply_context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    selection: Mapping[str, Any] | None = None
    delivery_route: Mapping[str, Any] = field(default_factory=dict)
    initial_read_tool: str = ""
    initial_read_arguments: Mapping[str, Any] = field(default_factory=dict)

    def to_agent_input(self) -> AgentInput:
        owner = _owner(self.owner)
        session_id = _scope(self.session_id, "session_id")
        request_id = _request_id(self.request_id)
        channel = (self.channel or "api").strip().casefold()
        if channel not in {"web", "telegram", "api", "test"}:
            raise TransportInputError("channel 无效")
        try:
            metadata = dict(self.metadata)
            if self.selection is not None:
                metadata["selection"] = normalize_selection(self.selection)
            return AgentInput(
                owner=owner,
                session_id=session_id,
                message=self.message,
                request_id=request_id,
                channel=channel,
                reply_context=dict(self.reply_context),
                metadata=metadata,
                initial_read_tool=self.initial_read_tool,
                initial_read_arguments=dict(self.initial_read_arguments),
                delivery_route=normalize_delivery_route(
                    self.delivery_route, channel=channel, owner=owner,
                ),
            )
        except (ValueError, SelectionInvalidError) as exc:
            raise TransportInputError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class EffectEnvelope:
    owner: str
    session_id: str
    plan_id: str
    request_id: str = ""
    channel: str = "api"
    delivery_route: Mapping[str, Any] = field(default_factory=dict)

    def normalized(self) -> EffectEnvelope:
        plan_id = (self.plan_id or "").strip()
        if not _PLAN_RE.fullmatch(plan_id):
            raise TransportInputError("plan_id 无效或已损坏")
        channel = (self.channel or "api").strip().casefold()
        if channel not in {"web", "telegram", "api", "test"}:
            raise TransportInputError("channel 无效")
        return EffectEnvelope(
            owner=_owner(self.owner),
            session_id=_scope(self.session_id, "session_id"),
            plan_id=plan_id,
            request_id=_request_id(self.request_id),
            channel=channel,
            delivery_route=normalize_delivery_route(
                self.delivery_route, channel=channel, owner=_owner(self.owner),
            ),
        )


@dataclass(frozen=True, slots=True)
class ExplainEnvelope:
    owner: str
    session_id: str
    plan_id: str
    message: str
    request_id: str = ""
    channel: str = "api"

    def normalized(self) -> ExplainEnvelope:
        effect = EffectEnvelope(self.owner, self.session_id, self.plan_id, self.request_id, self.channel).normalized()
        if not isinstance(self.message, str):
            raise TransportInputError("message 无效")
        try:
            message = AgentInput(message=self.message, owner=effect.owner, session_id=effect.session_id).message
        except ValueError as exc:
            raise TransportInputError(str(exc)) from exc
        return ExplainEnvelope(
            owner=effect.owner, session_id=effect.session_id, plan_id=effect.plan_id,
            request_id=effect.request_id, channel=effect.channel, message=message,
        )


class _KernelTransport:
    """共享会话依赖与确认取消；具体通道仅适配各自的事件协议。"""

    def __init__(self, session: AgentSession) -> None:
        self.session = session

    async def cancel_effect(self, request: EffectEnvelope) -> bool:
        normalized = request.normalized()
        return await self.session.cancel_effect(
            owner=normalized.owner, session_id=normalized.session_id,
            plan_id=normalized.plan_id, request_id=normalized.request_id,
        )


class WebKernelTransport(_KernelTransport):
    """Web 只取得真实 NDJSON 事件并转发控制命令。"""

    async def explain(self, request: ExplainEnvelope) -> AsyncGenerator[bytes, None]:
        events = self.session.explain(**asdict(replace(request, channel="web").normalized()))
        async with aclosing(events):
            async for chunk in iter_ndjson(events):
                yield chunk

    async def explain_view(self, request: ExplainEnvelope) -> TurnView:
        events = self.session.explain(**asdict(replace(request, channel="web").normalized()))
        async with aclosing(events):
            return await consume_events(events)

    async def query(self, request: QueryEnvelope) -> AsyncGenerator[bytes, None]:
        async with aclosing(self.session.run(replace(request, channel="web", delivery_route={}).to_agent_input())) as events:
            async for chunk in iter_ndjson(events):
                yield chunk

    async def query_view(self, request: QueryEnvelope) -> TurnView:
        async with aclosing(self.session.run(replace(request, channel="web", delivery_route={}).to_agent_input())) as events:
            return await consume_events(events)

    async def confirm(self, request: EffectEnvelope) -> AsyncGenerator[bytes, None]:
        events = self.session.confirm(**asdict(replace(request, channel="web", delivery_route={}).normalized()))
        async with aclosing(events):
            async for chunk in iter_ndjson(events):
                yield chunk

    async def confirm_view(self, request: EffectEnvelope) -> TurnView:
        events = self.session.confirm(**asdict(replace(request, channel="web", delivery_route={}).normalized()))
        async with aclosing(events):
            return await consume_events(events)

    async def activity(self, *, owner: str, session_id: str) -> dict[str, Any] | None:
        return await self.session.activity(
            owner=_owner(owner), session_id=_scope(session_id, "session_id"),
        )

    async def cancel(self, *, owner: str, session_id: str, request_id: str) -> bool:
        return await self.session.cancel(
            owner=_owner(owner),
            session_id=_scope(session_id, "session_id"),
            request_id=_scope(request_id, "request_id"),
        )


class TelegramKernelTransport(_KernelTransport):
    """Telegram 消费与 Web 相同的事件，不自行实现 Agent 状态机。"""

    async def explain(self, request: ExplainEnvelope) -> TurnView:
        events = self.session.explain(**asdict(replace(request, channel="telegram").normalized()))
        async with aclosing(events):
            return await consume_events(events)

    async def query(
        self,
        request: QueryEnvelope,
        *,
        observe: EventObserver | None = None,
        cancellation: CancellationToken | None = None,
    ) -> TurnView:
        normalized = replace(request, channel="telegram")
        events = self.session.run(normalized.to_agent_input())
        cancel_task = None

        async def cancel_on_stop(token: CancellationToken):
            await token.wait()
            await self.cancel(owner=normalized.owner, session_id=normalized.session_id)

        async def cancellable_events():
            nonlocal cancel_task
            async for event in events:
                # 先注册真实 turn，再响应停机，避免 stop 早于 admission 时丢失取消。
                if cancellation is not None and event.type is AgentEventType.TURN_STARTED:
                    cancel_task = asyncio.create_task(cancel_on_stop(cancellation))
                yield event

        try:
            return await consume_events(cancellable_events(), observe=observe)
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cancel_task

    async def confirm(
        self,
        request: EffectEnvelope,
        *,
        observe: EventObserver | None = None,
    ) -> TurnView:
        normalized = replace(request, channel="telegram").normalized()
        events = self.session.confirm(**asdict(normalized))
        return await consume_events(
            events,
            observe=observe,
        )

    async def cancel(self, *, owner: str, session_id: str) -> bool:
        return await self.session.cancel(
            owner=_owner(owner),
            session_id=_scope(session_id, "session_id"),
        )


def _owner(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 512:
        raise TransportInputError("owner 无效")
    # Telegram 的规范 owner 使用单个 Unit Separator 绑定 chat/user；
    # 其它控制字符、换行和 NUL 一律拒绝。
    if any(ord(char) < 32 and char != "\x1f" for char in normalized):
        raise TransportInputError("owner 无效")
    return normalized


def _scope(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SCOPE_RE.fullmatch(normalized):
        raise TransportInputError(f"{label} 无效")
    return normalized


def _request_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return secrets.token_urlsafe(12)
    if not _SCOPE_RE.fullmatch(normalized):
        raise TransportInputError("request_id 无效")
    return normalized
