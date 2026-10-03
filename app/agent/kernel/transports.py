"""Agent Kernel 的入口协议适配器。

这里不包含任何媒体业务判断；只校验传输层字段、构造 AgentInput、转发事件与确认。
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing, suppress
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .adapters import EventObserver, TurnView, consume_events, iter_ndjson
from .events import AgentEventType
from .metrics import KernelMetrics
from .session import AgentSession
from .state import AgentInput, CancellationToken, SelectionInvalidError
from .ux_selection import normalize_selection

_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,160}$")
_PLAN_RE = re.compile(r"^[A-Za-z0-9_-]{16,96}$")


class TransportInputError(ValueError):
    """对外安全的入口参数错误。"""


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

    def to_agent_input(self) -> AgentInput:
        owner = _owner(self.owner)
        session_id = _scope(self.session_id, "session_id")
        request_id = _request_id(self.request_id)
        channel = str(self.channel or "api").strip().casefold()
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

    def normalized(self) -> EffectEnvelope:
        plan_id = str(self.plan_id or "").strip()
        if not _PLAN_RE.fullmatch(plan_id):
            raise TransportInputError("plan_id 无效或已损坏")
        channel = str(self.channel or "api").strip().casefold()
        if channel not in {"web", "telegram", "api", "test"}:
            raise TransportInputError("channel 无效")
        return EffectEnvelope(
            owner=_owner(self.owner),
            session_id=_scope(self.session_id, "session_id"),
            plan_id=plan_id,
            request_id=_request_id(self.request_id),
            channel=channel,
        )


class WebKernelTransport:
    """Web 只取得真实 NDJSON 事件并转发控制命令。"""

    def __init__(
        self, session: AgentSession, *, metrics: KernelMetrics | None = None
    ) -> None:
        self.session = session
        self.metrics = metrics or KernelMetrics()

    async def query(self, request: QueryEnvelope) -> AsyncIterator[bytes]:
        async with aclosing(self.session.run(replace(request, channel="web").to_agent_input())) as events:
            async for chunk in iter_ndjson(self.metrics.track(events, channel="web")):
                yield chunk

    async def query_view(self, request: QueryEnvelope) -> TurnView:
        async with aclosing(self.session.run(replace(request, channel="web").to_agent_input())) as events:
            return await consume_events(self.metrics.track(events, channel="web"))

    async def confirm(self, request: EffectEnvelope) -> AsyncIterator[bytes]:
        events = self.session.confirm(**asdict(request.normalized()))
        async with aclosing(events):
            async for chunk in iter_ndjson(self.metrics.track(events, channel="web")):
                yield chunk

    async def confirm_view(self, request: EffectEnvelope) -> TurnView:
        events = self.session.confirm(**asdict(request.normalized()))
        async with aclosing(events):
            return await consume_events(self.metrics.track(events, channel="web"))

    async def activity(self, *, owner: str, session_id: str) -> dict[str, Any] | None:
        return await self.session.coordinator.describe(
            owner=_owner(owner), session_id=_scope(session_id, "session_id"),
        )

    async def cancel(self, *, owner: str, session_id: str, request_id: str) -> bool:
        return await self.session.cancel(
            owner=_owner(owner),
            session_id=_scope(session_id, "session_id"),
            request_id=_scope(request_id, "request_id"),
        )

    async def cancel_effect(self, request: EffectEnvelope) -> bool:
        arguments = asdict(request.normalized())
        arguments.pop("channel")
        return await self.session.cancel_effect(**arguments)


class TelegramKernelTransport:
    """Telegram 消费与 Web 相同的事件，不自行实现 Agent 状态机。"""

    def __init__(
        self, session: AgentSession, *, metrics: KernelMetrics | None = None
    ) -> None:
        self.session = session
        self.metrics = metrics or KernelMetrics()

    async def query(
        self,
        request: QueryEnvelope,
        *,
        observe: EventObserver | None = None,
        cancellation: CancellationToken | None = None,
    ) -> TurnView:
        normalized = replace(request, channel="telegram")
        events = self.metrics.track(
            self.session.run(normalized.to_agent_input()),
            channel="telegram",
        )
        cancel_task = None

        async def cancel_on_stop():
            await cancellation.wait()
            await self.cancel(owner=normalized.owner, session_id=normalized.session_id)

        async def cancellable_events():
            nonlocal cancel_task
            async for event in events:
                # 先注册真实 turn，再响应停机，避免 stop 早于 admission 时丢失取消。
                if cancellation is not None and event.type is AgentEventType.TURN_STARTED:
                    cancel_task = asyncio.create_task(cancel_on_stop())
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
            self.metrics.track(events, channel="telegram"),
            observe=observe,
        )

    async def cancel(self, *, owner: str, session_id: str) -> bool:
        return await self.session.cancel(
            owner=_owner(owner),
            session_id=_scope(session_id, "session_id"),
        )

    async def cancel_effect(self, request: EffectEnvelope) -> bool:
        arguments = asdict(request.normalized())
        arguments.pop("channel")
        return await self.session.cancel_effect(**arguments)


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
