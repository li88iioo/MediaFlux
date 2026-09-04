"""Agent 会话重置/删除的统一一致性边界。"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from .effects import ConfirmationEffectPlanStore
from .session import AgentSession
from .state import SessionBusyError, SessionState


class SessionLifecycleStore(Protocol):
    async def reset_session(self, *, owner: str, session_id: str) -> SessionState: ...

    async def delete_session(self, *, owner: str, session_id: str) -> bool: ...


class AgentSessionLifecycle:
    """串行撤销回合、确认票据和 Provider 临时状态后再变更会话。"""

    def __init__(
        self,
        *,
        session: AgentSession,
        store: SessionLifecycleStore,
        effect_store: ConfirmationEffectPlanStore,
        clear_provider_state: Callable[..., Any],
    ) -> None:
        self.session = session
        self.store = store
        self.effect_store = effect_store
        self.clear_provider_state = clear_provider_state

    @staticmethod
    def _scope(owner: str, session_id: str) -> tuple[str, str]:
        owner_key = str(owner or "").strip()
        session_key = str(session_id or "").strip()
        if not owner_key or not session_key:
            raise ValueError("Agent 会话 scope 无效")
        return owner_key, session_key

    async def _invalidate(self, *, owner: str, session_id: str) -> None:
        await asyncio.to_thread(
            self.effect_store.revoke_session,
            owner=owner,
            session_id=session_id,
        )
        await asyncio.to_thread(
            self.clear_provider_state,
            owner=owner,
            session_id=session_id,
        )

    async def reset(self, *, owner: str, session_id: str) -> SessionState:
        owner_key, session_key = self._scope(owner, session_id)
        async with self.session._start_lock:
            if await self.session.coordinator.has_protected_turn(
                owner=owner_key,
                session_id=session_key,
            ):
                raise SessionBusyError("confirmed effect is executing")
            await self.session.cancel(owner=owner_key, session_id=session_key)
            await self._invalidate(owner=owner_key, session_id=session_key)
            return await self.store.reset_session(
                owner=owner_key,
                session_id=session_key,
            )

    async def delete(self, *, owner: str, session_id: str) -> bool:
        owner_key, session_key = self._scope(owner, session_id)
        async with self.session._start_lock:
            if await self.session.coordinator.has_protected_turn(
                owner=owner_key,
                session_id=session_key,
            ):
                raise SessionBusyError("confirmed effect is executing")
            await self.session.cancel(owner=owner_key, session_id=session_key)
            await self._invalidate(owner=owner_key, session_id=session_key)
            return await self.store.delete_session(
                owner=owner_key,
                session_id=session_key,
            )
