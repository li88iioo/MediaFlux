"""Agent 会话重置/删除的统一一致性边界。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, Protocol

from app.agent.kernel.session_state import SessionState
from app.agent.kernel.state_types import SessionBusyError

from .effects import ConfirmationEffectPlanStore
from .session import AgentSession
from .session_guard import guarded_state_call, session_scope_guard
from .sandbox import clear_workspace


class SessionLifecycleStore(Protocol):
    async def reset_session(self, *, owner: str, session_id: str) -> SessionState: ...

    async def delete_session(self, *, owner: str, session_id: str) -> bool: ...


class AgentSessionLifecycle:
    """串行撤销回合、确认票据与会话临时状态后再变更会话。"""

    def __init__(
        self,
        *,
        session: AgentSession,
        store: SessionLifecycleStore,
        effect_store: ConfirmationEffectPlanStore,
        clear_legacy_plans: Callable[..., Any],
    ) -> None:
        self.session = session
        self.store = store
        self.effect_store = effect_store
        self.clear_legacy_plans = clear_legacy_plans

    async def _invalidate(self, *, owner: str, session_id: str) -> None:
        await guarded_state_call(owner, session_id, partial(
            clear_workspace, owner=owner, session_id=session_id,
        ))
        await guarded_state_call(owner, session_id, partial(
            self.effect_store.revoke_session, owner=owner, session_id=session_id,
        ))
        await guarded_state_call(owner, session_id, partial(
            self.clear_legacy_plans, owner=owner, session_id=session_id,
        ))


    async def _change[T](
        self, change: Callable[..., Awaitable[T]], owner: str, session_id: str,
    ) -> T:
        owner_key, session_key = (owner or "").strip(), (session_id or "").strip()
        with session_scope_guard(owner_key, session_key):
            async with self.session._start_lock:
                if await self.session.coordinator.has_protected_turn(
                    owner=owner_key, session_id=session_key,
                ):
                    raise SessionBusyError("confirmed effect is executing")
                await self.session.cancel(owner=owner_key, session_id=session_key)
                await self._invalidate(owner=owner_key, session_id=session_key)
                return await change(owner=owner_key, session_id=session_key)

    async def reset(self, *, owner: str, session_id: str) -> SessionState:
        return await self._change(self.store.reset_session, owner, session_id)

    async def delete(self, *, owner: str, session_id: str) -> bool:
        return await self._change(self.store.delete_session, owner, session_id)
