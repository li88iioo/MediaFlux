"""会话限定预授权：授予、撤销、范围匹配与一次执行额度扣减。"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.agent.errors import ConfirmationClaimError, ToolPipelineError

from .capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from .tool_calls import EffectPlan
from .session_guard import session_scope_guard
from .session_state import SessionState
from .state_ports import SessionStateStore
from .state_types import StalePublicationError, _ScopedTrustTransition
from .tool_calls import ToolCallContext


def watchlist_approval_scope(tool_name, arguments, metadata) -> dict[str, str] | None:
    """收藏添加共用原有 TMDB/类型/正整数 ID 范围；移除不支持预授权。"""
    if (tool_name != "watchlist.add" or metadata.get("risk") != "low_write"
            or set(arguments) != {"provider", "media_type", "external_id"}
            or arguments.get("provider") != "tmdb" or arguments.get("media_type") not in {"movie", "tv"}
            or not isinstance(arguments.get("external_id"), str)
            or not re.fullmatch(r"[1-9][0-9]{0,17}", arguments["external_id"])):
        return None
    return {"provider": "tmdb", "media_type": arguments["media_type"]}


class ScopedTrustPolicy:
    """只管理预授权；冻结票据和业务执行由各自持有者负责。"""

    def __init__(self, *, catalog: ToolCatalog, state_store: SessionStateStore,
                 pending_plan: Callable[..., Awaitable[EffectPlan | None]], clock: Callable[[], float]) -> None:
        self._catalog = catalog
        self._store = state_store
        self._pending_plan = pending_plan
        self._clock = clock

    def _tool_name(self, name):
        # 只有同一窄 policy 的 native 入口就绪才迁移名称；不改 owner、范围、额度或到期时间。
        if (name == "discovery.add_watchlist" and self._catalog.has("watchlist.add")
                and self._catalog.get("watchlist.add").approval_scope is watchlist_approval_scope):
            return "watchlist.add"
        return name

    @staticmethod
    def scope(tool: KernelToolSpec, arguments: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, str] | None:
        if tool.effect is not ToolEffect.WRITE or tool.metadata.get("risk") != "low_write" or tool.approval_scope is None:
            return None
        return tool.approval_scope(tool.name, arguments, metadata)

    def _grant(self, state: SessionState) -> Mapping[str, Any] | None:
        grant = state.metadata.get("scoped_trust") or {}
        if (not isinstance(grant, Mapping) or grant.get("owner") != state.owner or grant.get("session_id") != state.session_id
                or type(grant.get("expires_at")) not in (int, float) or grant["expires_at"] <= self._clock()
                or type(grant.get("remaining")) is not int or grant["remaining"] <= 0):
            return None
        return grant

    def view(self, state: SessionState) -> dict[str, Any]:
        grant = self._grant(state)
        tool_name = self._tool_name(grant.get("tool_name")) if grant is not None else None
        if (grant is None or tool_name is None or not self._catalog.has(tool_name)
                or self._catalog.get(tool_name).approval_scope is None):
            return {"active": False}
        return {"active": True, "tool_name": tool_name, "scope": dict(grant["scope"]),
                "remaining": grant["remaining"], "expires_at": grant["expires_at"]}

    async def set(
        self, *, context: ToolCallContext, operation: str, plan_id: str = "", tool_name: str = "",
        scope: Mapping[str, Any] | None = None, ttl_seconds: int = 0, max_actions: int = 0,
    ) -> dict[str, Any]:
        with session_scope_guard(context.owner, context.session_id, kind="effect"):
            state = await self._store.load(owner=context.owner, session_id=context.session_id)
            if state.generation != context.lease.generation:
                raise StalePublicationError("turn lost publication authority")
            if operation == "revoke":
                if state.metadata.get("scoped_trust"):
                    await self._store.commit(context.lease, updates=(_ScopedTrustTransition("metadata.scoped_trust", None),))
                return {"active": False}
            if (operation != "grant" or not isinstance(scope, Mapping)
                    or type(ttl_seconds) is not int or ttl_seconds not in {300, 900, 1800}
                    or type(max_actions) is not int or max_actions not in {5, 10, 20}):
                raise ToolPipelineError("请选择本会话的收藏来源/类型、5/10/20次上限及5/15/30分钟有效期。", code="scoped_trust_invalid")
            plan = await self._pending_plan(owner=context.owner, session_id=context.session_id)
            if plan is None or plan.plan_id != plan_id or plan.tool_name != tool_name or not self._catalog.has(plan.tool_name):
                raise ConfirmationClaimError()
            tool = self._catalog.get(plan.tool_name).for_arguments(plan.arguments)
            allowed_scope = self.scope(tool, plan.arguments, plan.metadata)
            if allowed_scope is None or dict(scope) != allowed_scope:
                raise ToolPipelineError("该工具或目标范围不支持预授权，仍需逐次确认。", code="scoped_trust_denied")
            grant = {"owner": context.owner, "session_id": context.session_id, "tool_name": self._tool_name(tool.name),
                     "scope": allowed_scope, "remaining": max_actions, "expires_at": self._clock() + ttl_seconds}
            state = await self._store.commit(context.lease, updates=(_ScopedTrustTransition("metadata.scoped_trust", grant),))
            return self.view(state)

    async def allows(self, plan: EffectPlan, *, context: ToolCallContext) -> bool:
        if ((plan.owner, plan.session_id) != (context.owner, context.session_id) or plan.expires_at <= self._clock()
                or not self._catalog.has(plan.tool_name)):
            return False
        state = await self._store.load(owner=context.owner, session_id=context.session_id)
        grant = self._grant(state)
        if grant is None:
            return False
        tool = self._catalog.get(plan.tool_name).for_arguments(plan.arguments)
        scope = self.scope(tool, plan.arguments, plan.metadata)
        return (scope is not None and self._tool_name(grant.get("tool_name")) == self._tool_name(tool.name)
                and grant.get("scope") == scope)

    async def consume(self, plan: EffectPlan, *, context: ToolCallContext) -> None:
        """在执行锁内复验并扣一次；限流、鉴权拒绝不得花费执行额度。"""
        with session_scope_guard(context.owner, context.session_id, kind="effect"):
            if not await self.allows(plan, context=context):
                raise ToolPipelineError("预授权已到期或范围已变化，本次未执行；请重新预检并确认。", code="scoped_trust_expired")
            state = await self._store.load(owner=context.owner, session_id=context.session_id)
            grant = state.metadata["scoped_trust"]
            await self._store.commit(context.lease, updates=(_ScopedTrustTransition(
                "metadata.scoped_trust", {**grant, "tool_name": self._tool_name(plan.tool_name),
                                          "remaining": grant["remaining"] - 1}),))
