"""统一会话状态、publication generation 与取消协调。"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol


class KernelStateError(RuntimeError):
    pass


class StalePublicationError(KernelStateError):
    """当前回合已失去发布权，禁止提交迟到状态。"""


class SessionBusyError(KernelStateError):
    """已确认的副作用正在执行；新回合不能抢占。"""


@dataclass(frozen=True, slots=True)
class AgentInput:
    message: str
    owner: str
    session_id: str
    request_id: str = ""
    channel: str = "api"
    reply_context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        message = str(self.message or "").strip()
        owner = str(self.owner or "").strip()
        session_id = str(self.session_id or "").strip()
        request_id = str(self.request_id or "").strip() or secrets.token_urlsafe(12)
        channel = str(self.channel or "api").strip().lower() or "api"
        if not message:
            raise ValueError("message cannot be empty")
        if not owner:
            raise ValueError("owner cannot be empty")
        if not session_id:
            raise ValueError("session_id cannot be empty")
        if len(message) > 12_000:
            raise ValueError("message is too long")
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "reply_context", deepcopy(dict(self.reply_context)))
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class PublicationLease:
    owner: str
    session_id: str
    generation: int
    turn_id: str
    request_id: str


@dataclass(frozen=True, slots=True)
class StateUpdate:
    key: str
    value: Any
    mode: str = "set"


@dataclass(slots=True)
class SessionState:
    owner: str
    session_id: str
    generation: int = 0
    conversation: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    recent_refs: list[str] = field(default_factory=list)
    ref_kinds: set[str] = field(default_factory=set)
    pending_effect_plan_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def clone(self) -> SessionState:
        return SessionState(
            owner=self.owner,
            session_id=self.session_id,
            generation=self.generation,
            conversation=deepcopy(self.conversation),
            summary=self.summary,
            recent_refs=list(self.recent_refs),
            ref_kinds=set(self.ref_kinds),
            pending_effect_plan_id=self.pending_effect_plan_id,
            metadata=deepcopy(self.metadata),
        )

    def apply(self, updates: Sequence[StateUpdate]) -> None:
        for update in updates:
            key = str(update.key or "").strip()
            if not key:
                continue
            if key == "summary":
                self.summary = str(update.value or "")[:8_000]
            elif key == "pending_effect_plan_id":
                self.pending_effect_plan_id = str(update.value or "")[:200]
            elif key == "recent_refs":
                values = [str(item) for item in (update.value or []) if str(item)]
                if update.mode == "append":
                    self.recent_refs = (self.recent_refs + values)[-100:]
                else:
                    self.recent_refs = values[-100:]
            elif key == "ref_kinds":
                values = {str(item) for item in (update.value or []) if str(item)}
                self.ref_kinds = (
                    self.ref_kinds | values if update.mode == "append" else values
                )
            elif key.startswith("metadata."):
                field_name = key.partition(".")[2]
                if field_name:
                    if update.mode == "delete":
                        self.metadata.pop(field_name, None)
                    else:
                        self.metadata[field_name] = deepcopy(update.value)


class SessionStateStore(Protocol):
    async def begin_turn(
        self, *, owner: str, session_id: str, request_id: str
    ) -> tuple[PublicationLease, SessionState]: ...

    async def is_current(self, lease: PublicationLease) -> bool: ...

    async def commit(
        self,
        lease: PublicationLease,
        *,
        conversation: Sequence[Mapping[str, Any]] | None = None,
        updates: Sequence[StateUpdate] = (),
    ) -> SessionState: ...

    async def load(self, *, owner: str, session_id: str) -> SessionState: ...


class InMemorySessionStateStore:
    """测试与单进程运行使用的权威状态实现。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: dict[tuple[str, str], SessionState] = {}

    async def begin_turn(
        self, *, owner: str, session_id: str, request_id: str
    ) -> tuple[PublicationLease, SessionState]:
        key = (owner, session_id)
        async with self._lock:
            current = self._states.get(key)
            generation = (current.generation if current else 0) + 1
            state = (
                current.clone()
                if current
                else SessionState(owner=owner, session_id=session_id)
            )
            state.generation = generation
            self._states[key] = state
            lease = PublicationLease(
                owner=owner,
                session_id=session_id,
                generation=generation,
                turn_id=secrets.token_urlsafe(12),
                request_id=request_id,
            )
            return lease, state.clone()

    async def is_current(self, lease: PublicationLease) -> bool:
        async with self._lock:
            state = self._states.get((lease.owner, lease.session_id))
            return bool(state and state.generation == lease.generation)

    async def commit(
        self,
        lease: PublicationLease,
        *,
        conversation: Sequence[Mapping[str, Any]] | None = None,
        updates: Sequence[StateUpdate] = (),
    ) -> SessionState:
        key = (lease.owner, lease.session_id)
        async with self._lock:
            state = self._states.get(key)
            if state is None or state.generation != lease.generation:
                raise StalePublicationError("turn no longer owns publication authority")
            if conversation is not None:
                state.conversation = deepcopy([dict(item) for item in conversation])[
                    -80:
                ]
            state.apply(updates)
            return state.clone()

    async def load(self, *, owner: str, session_id: str) -> SessionState:
        async with self._lock:
            state = self._states.get((owner, session_id))
            return (
                state.clone()
                if state
                else SessionState(owner=owner, session_id=session_id)
            )


class CancellationToken:
    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason = ""

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason or "cancelled"

    def cancel(self, reason: str = "cancelled") -> None:
        self._reason = str(reason or "cancelled")[:200]
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError(self.reason)


class TurnCoordinator:
    """同一 owner/session 的最新回合拥有唯一发布权。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: dict[tuple[str, str], tuple[str, CancellationToken, bool]] = {}

    async def begin(
        self,
        lease: PublicationLease,
        *,
        protected: bool = False,
    ) -> CancellationToken:
        key = (lease.owner, lease.session_id)
        token = CancellationToken()
        async with self._lock:
            previous = self._active.get(key)
            if previous is not None:
                if previous[2]:
                    raise SessionBusyError("confirmed effect is executing")
                previous[1].cancel("superseded")
            self._active[key] = (lease.turn_id, token, bool(protected))
        return token

    async def cancel(
        self, *, owner: str, session_id: str, reason: str = "cancelled"
    ) -> bool:
        async with self._lock:
            current = self._active.get((owner, session_id))
            if current is None or current[2]:
                return False
            current[1].cancel(reason)
            return True

    async def has_protected_turn(self, *, owner: str, session_id: str) -> bool:
        async with self._lock:
            current = self._active.get((owner, session_id))
            return bool(current and current[2])

    async def is_current(
        self, lease: PublicationLease, token: CancellationToken
    ) -> bool:
        async with self._lock:
            current = self._active.get((lease.owner, lease.session_id))
            return bool(
                current
                and current[0] == lease.turn_id
                and current[1] is token
                and not token.cancelled
            )

    async def finish(self, lease: PublicationLease, token: CancellationToken) -> None:
        key = (lease.owner, lease.session_id)
        async with self._lock:
            current = self._active.get(key)
            if current and current[0] == lease.turn_id and current[1] is token:
                self._active.pop(key, None)
