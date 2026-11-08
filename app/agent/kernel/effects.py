"""唯一副作用执行边界、冻结计划与一次性确认存储。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from dataclasses import replace
from functools import partial
from typing import Any, Protocol

from app.agent.confirmation import ConfirmationExpiredError, ConfirmationStore
from app.agent.errors import AgentToolError, ConfirmationClaimError, ToolPipelineError, ToolRateLimitError, _ReadPermitRejected
from app.agent.model_context_budget import compact_tool_content, decode_tool_content
from app.agent.public_safety import sanitize_public_text
from app.agent.schema import normalize_read_arguments, validate_json_schema
from app.concurrency import CrossLoopAsyncLock
from app.sensitive_data import contains_sensitive_credential, is_sensitive_key

from .capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from .completion_receipts import EffectCompletionScope
from .invocation import invoke
from .projection import DefaultProjector, ReferenceValue, ToolOutcome
from .references import InMemoryReferenceStore, ReferenceError, ReferenceStore
from .session_guard import _CancelledWorkerResult, guarded_state_call, session_scope_guard
from .session_state import PendingEffectGuard, SessionState
from .state_ports import SessionStateStore
from .state_types import PublicationLease, StalePublicationError, StateUpdate, _ConfirmationClaim, _PendingEffectTransition
from .tool_calls import EffectPlan, PipelineResult, PreparedEffect, ToolCallContext

_PRIVATE_EDIT_KEY = re.compile(
    r"(?:^|_)(?:id|ids|ref|refs|handle|fingerprint|token|secret|password|cookie|"
    r"credential|authorization|signature|signed|nonce|code|passcode|pin|otp|path|paths|url|uri|headers?)(?:_|$)", re.I,
)
_EDIT_SCHEMA_KEYS = frozenset({"type", "enum", "minimum", "maximum", "exclusiveMinimum",
                               "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
                               "pattern", "minItems", "maxItems", "uniqueItems"})
_EDIT_SCALAR_TYPES = ("string", "integer", "number", "boolean")


def confirmation_editing(tool: KernelToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """只公布原 Schema 中可直接编辑的公开标量/标量列表，不序列化内部参数。"""
    def safe_value(value: Any) -> bool:
        if isinstance(value, str):
            return (not value.startswith(("ref_", "plan_")) and not contains_sensitive_credential(value)
                    and sanitize_public_text(value, limit=max(1, len(value))) == value)
        if isinstance(value, list):
            return all(safe_value(item) for item in value)
        return value is None or type(value) in (bool, int, float)

    properties, values = {}, {}
    for key, schema in tool.input_schema.get("properties", {}).items():
        private_key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).replace("-", "_")
        if (is_sensitive_key(key) or _PRIVATE_EDIT_KEY.search(private_key)
                or not isinstance(schema, Mapping) or schema.get("readOnly") or schema.get("writeOnly")):
            continue
        kind = schema.get("type")
        items = schema.get("items", {})
        variants = schema.get("oneOf", ())
        scalar_union = bool(variants) and all(isinstance(item, Mapping) and item.get("type") in _EDIT_SCALAR_TYPES for item in variants)
        if kind not in (*_EDIT_SCALAR_TYPES, "array") and not scalar_union:
            continue
        if kind == "array" and (not isinstance(items, Mapping) or items.get("type") not in _EDIT_SCALAR_TYPES):
            continue
        public = {name: deepcopy(value) for name, value in schema.items() if name in _EDIT_SCHEMA_KEYS}
        if scalar_union:
            public["oneOf"] = [{name: deepcopy(value) for name, value in item.items() if name in _EDIT_SCHEMA_KEYS} for item in variants]
        if kind == "array":
            public["items"] = {name: deepcopy(value) for name, value in items.items() if name in _EDIT_SCHEMA_KEYS}
        if (not safe_value(public.get("enum", [])) or not safe_value(public.get("items", {}).get("enum", []))
                or any(not safe_value(item.get("enum", [])) for item in public.get("oneOf", ()))):
            continue
        if key in arguments and not safe_value(arguments[key]):
            continue
        for label in ("title", "description"):
            if isinstance(schema.get(label), str):
                public[label] = sanitize_public_text(schema[label], limit=240)
        properties[key] = public
        if key in arguments:
            values[key] = deepcopy(arguments[key])
    return {"schema": {"type": "object", "properties": properties, "additionalProperties": False}, "values": values}


class EffectPlanError(RuntimeError):
    pass


class EffectPlanExpiredError(EffectPlanError):
    """票据已注销，只携带原计划供失败回执/只读复验使用。"""

    def __init__(self, plan: EffectPlan) -> None:
        super().__init__("effect plan expired")
        self.plan = plan


class EffectPlanStore(Protocol):
    def freeze(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        tool_name: str,
        effect: ToolEffect,
        arguments: Mapping[str, Any],
        prepared: PreparedEffect,
        public_result: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> EffectPlan: ...

    def claim(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        plan_id: str,
    ) -> EffectPlan: ...

    def cancel(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        plan_id: str,
    ) -> EffectPlan | None: ...

    def get_active_plan(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        plan_id: str,
    ) -> EffectPlan | None: ...

    def get_plan_evidence(
        self, *, owner: str, session_id: str, generation: int, plan_id: str,
    ) -> EffectPlan | None: ...

class ConfirmationEffectPlanStore:
    """在既有一次性 ConfirmationStore 之上提供 Kernel EffectPlan。"""

    CONTRACT_VERSION = 1

    def __init__(
        self,
        store: ConfirmationStore | None = None,
        *,
        record_actions: bool = False,
    ) -> None:
        self.store = store or ConfirmationStore()
        self.record_actions = record_actions

    @staticmethod
    def _scoped_owner(owner: str, session_id: str) -> str:
        owner_key = (owner or "").strip()
        session_key = (session_id or "").strip()
        if not owner_key or not session_key:
            raise EffectPlanError("effect owner and session are required")
        return f"kernel:{owner_key}\x1f{session_key}"

    def freeze(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        tool_name: str,
        effect: ToolEffect,
        arguments: Mapping[str, Any],
        prepared: PreparedEffect,
        public_result: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> EffectPlan:
        if effect is ToolEffect.READ:
            raise EffectPlanError("READ tools cannot create effect plans")
        scoped_owner = self._scoped_owner(owner, session_id)
        effective_arguments = dict(prepared.arguments or arguments)
        normalized_public_result = dict(public_result or {})
        edit_deadline = prepared.metadata.get("confirmation_deadline")
        if (type(edit_deadline) is int or type(edit_deadline) is float):
            remaining = edit_deadline - self.store._clock()
            if remaining <= 0:
                raise EffectPlanError("effect plan expired during edit")
            deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + remaining)
        contract = {
            "kernel_effect_version": self.CONTRACT_VERSION,
            "session_id": session_id,
            "generation": generation,
            "effect": effect.value,
            "preview": deepcopy(dict(prepared.preview)),
            "public_result": deepcopy(normalized_public_result),
            "metadata": deepcopy(dict(prepared.metadata)),
            "audit_risk": str(prepared.metadata.get("risk") or effect.value),
            "audit_contract": deepcopy(
                dict(prepared.metadata.get("confirmation") or {})
            ),
        }
        ticket = self.store.issue(
            owner=scoped_owner,
            tool_name=tool_name,
            arguments=effective_arguments,
            context_fingerprint=(prepared.snapshot_fingerprint or ""),
            confirmation_contract=contract,
            replace_active_ticket=True,
            deadline=deadline,
        )
        return EffectPlan(
            plan_id=ticket.confirmation_id,
            owner=owner,
            session_id=session_id,
            generation=generation,
            tool_name=tool_name,
            effect=effect,
            arguments=deepcopy(ticket.arguments),
            snapshot_fingerprint=ticket.context_fingerprint,
            preview=deepcopy(dict(prepared.preview)),
            expires_at=ticket.expires_at,
            owner_generation=ticket.owner_generation,
            confirmation_contract=deepcopy(ticket.confirmation_contract),
            metadata=deepcopy(dict(prepared.metadata)),
            public_result=deepcopy(normalized_public_result),
        )

    @staticmethod
    def _restore_plan(
        ticket: Any,
        *,
        owner: str,
        session_id: str,
        generation: int,
    ) -> EffectPlan:
        contract = ticket.confirmation_contract
        try:
            version = int(contract.get("kernel_effect_version"))
            stored_session = str(contract.get("session_id") or "")
            stored_generation = int(contract.get("generation"))
            effect = ToolEffect(str(contract.get("effect") or ""))
            preview = contract.get("preview")
            public_result = contract.get("public_result", {})
            metadata = contract.get("metadata")
        except (TypeError, ValueError) as exc:
            raise EffectPlanError("effect plan payload is invalid") from exc
        if (
            version != ConfirmationEffectPlanStore.CONTRACT_VERSION
            or stored_session != session_id
        ):
            raise EffectPlanError("effect plan scope is invalid")
        if stored_generation != generation:
            raise EffectPlanError("effect plan is stale")
        if (
            not isinstance(preview, dict)
            or not isinstance(public_result, dict)
            or not isinstance(metadata, dict)
        ):
            raise EffectPlanError("effect plan payload is invalid")
        return EffectPlan(
            plan_id=ticket.confirmation_id,
            owner=owner,
            session_id=session_id,
            generation=stored_generation,
            tool_name=ticket.tool_name,
            effect=effect,
            arguments=deepcopy(ticket.arguments),
            snapshot_fingerprint=ticket.context_fingerprint,
            preview=deepcopy(preview),
            expires_at=ticket.expires_at,
            owner_generation=ticket.owner_generation,
            confirmation_contract=deepcopy(contract),
            metadata=deepcopy(metadata),
            public_result=deepcopy(public_result),
        )

    def claim(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        plan_id: str,
    ) -> EffectPlan:
        scoped_owner = self._scoped_owner(owner, session_id)
        # 先只读校验 generation/contract，再做一次性原子领取。旧回合不能
        # 通过“先领取后失败”撤销同会话更新的有效计划。
        preview_ticket = self.store.inspect_ticket(owner=scoped_owner, confirmation_id=plan_id)
        if preview_ticket is None:
            raise EffectPlanError("effect plan is unavailable")
        self._restore_plan(
            preview_ticket,
            owner=owner,
            session_id=session_id,
            generation=generation,
        )
        try:
            ticket = self.store.claim_and_rotate_owner(
                owner=scoped_owner,
                confirmation_id=plan_id,
                record_execution=self.record_actions,
                execution_owner=owner,
            )
        except ConfirmationExpiredError as exc:
            raise EffectPlanExpiredError(self._restore_plan(
                exc.ticket, owner=owner, session_id=session_id, generation=generation,
            )) from exc
        return self._restore_plan(
            ticket,
            owner=owner,
            session_id=session_id,
            generation=generation,
        )

    def get_plan_evidence(
        self, *, owner: str, session_id: str, generation: int, plan_id: str,
    ) -> EffectPlan | None:
        """仅在原确认窗口内恢复引用失效证据；不是写入授权或普通票据续期。"""
        try:
            ticket = self.store.inspect_ticket(owner=self._scoped_owner(owner, session_id), confirmation_id=plan_id)
            if ticket is None:
                return None
            plan = self._restore_plan(ticket, owner=owner, session_id=session_id, generation=generation)
            now = self.store._clock()
            recovery_until = plan.confirmation_contract.get("reference_recovery_until")
            edit_deadline = plan.metadata.get("confirmation_deadline")
            if (type(edit_deadline) is int or type(edit_deadline) is float) and edit_deadline <= now:
                return None
            return plan if plan.expires_at > now or (
                (type(recovery_until) is int or type(recovery_until) is float) and recovery_until > now
            ) else None
        except EffectPlanError:
            return None

    def get_active_plan(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        plan_id: str,
    ) -> EffectPlan | None:
        """从唯一有效的确认快照恢复卡片；不依赖可淘汰的事件日志。"""
        plan = self.get_plan_evidence(owner=owner, session_id=session_id, generation=generation, plan_id=plan_id)
        return plan if plan is not None and plan.expires_at > self.store._clock() else None

    def cancel(
        self,
        *,
        owner: str,
        session_id: str,
        generation: int,
        plan_id: str,
    ) -> EffectPlan | None:
        scoped_owner = self._scoped_owner(owner, session_id)
        ticket = self.store.inspect_ticket(owner=scoped_owner, confirmation_id=plan_id)
        if ticket is None:
            return None
        try:
            plan = self._restore_plan(
                ticket,
                owner=owner,
                session_id=session_id,
                generation=generation,
            )
        except EffectPlanError:
            plan = None
        discarded = self.store.discard(
            owner=scoped_owner,
            confirmation_id=plan_id,
        )
        if not discarded or plan is None:
            return None
        if plan.expires_at <= self.store._clock():
            # 注销过期证据仍需释放领域预占，但不能冒充取消了一张有效卡。
            raise EffectPlanExpiredError(plan)
        return plan

    def revoke_session(self, *, owner: str, session_id: str) -> int:
        """撤销指定 Kernel 会话仍有效的全部确认计划。"""
        return self.store.revoke_owner(
            owner=self._scoped_owner(owner, session_id),
        )


class AuthorizationPolicy(Protocol):
    async def authorize(
        self,
        tool: KernelToolSpec,
        arguments: Mapping[str, Any],
        context: ToolCallContext,
    ) -> None: ...


class RateLimiter(Protocol):
    async def acquire(
        self, *, owner: str, tool_name: str, cost: float, arguments: Mapping[str, Any]
    ) -> None: ...


class EffectLifecycle(Protocol):
    """EffectPlan 的确定性领域后置动作、审计与运行代次生命周期。"""

    def prepared(
        self,
        *,
        tool: KernelToolSpec,
        arguments: Mapping[str, Any],
        prepared: PreparedEffect,
        context: ToolCallContext,
    ) -> PreparedEffect:
        del tool, arguments, context
        return prepared

    def prepare_failed(
        self, *, prepared: PreparedEffect, context: ToolCallContext
    ) -> None:
        del prepared, context

    def substep_started(self, *, plan: EffectPlan) -> None:
        """已消费的组合票据内，在每项写入前登记子操作；不再创建或消费票据。"""
        del plan

    def completed(self, *, plan: EffectPlan, value: Any, elapsed_ms: int) -> None:
        del plan, value, elapsed_ms

    def failed(self, *, plan: EffectPlan, code: str, elapsed_ms: int) -> None:
        del plan, code, elapsed_ms

    def interrupted(self, *, plan: EffectPlan) -> None:
        del plan

    def cancelled(self, *, plan: EffectPlan) -> None:
        del plan

    async def register_terminal_failure(
        self, *, scope: EffectCompletionScope, tool: str, result: Mapping[str, Any],
    ) -> bool:
        del scope, tool, result
        return False


class NoopEffectLifecycle(EffectLifecycle):
    pass


class DefaultAuthorizationPolicy:
    async def authorize(
        self,
        tool: KernelToolSpec,
        arguments: Mapping[str, Any],
        context: ToolCallContext,
    ) -> None:
        if tool.effect is not ToolEffect.READ and not context.owner:
            raise ToolPipelineError(
                "写操作需要已登录身份", code="authorization_required"
            )
        if tool.authorize is not None:
            try:
                allowed = (tool.authorize(
                        {
                            **context.policy_context(),
                            "arguments": dict(arguments),
                            "effect": tool.effect.value,
                        }
                    ))
            except Exception as exc:
                raise ToolPipelineError(
                    "工具授权检查失败", code="authorization_failed"
                ) from exc
            if not allowed:
                raise ToolPipelineError(
                    "当前身份无权使用该工具", code="authorization_denied"
                )


class InMemoryRateLimiter:
    """按 owner/tool 的轻量滑动窗口限流；生产可替换而不改变 Pipeline。"""

    def __init__(self, *, limit: int = 30, window_seconds: float = 60.0) -> None:
        self.limit = max(1, limit)
        self.window_seconds = max(1.0, float(window_seconds))
        self._lock = CrossLoopAsyncLock()
        self._events: dict[tuple[str, str], list[tuple[float, float]]] = {}

    async def acquire(
        self, *, owner: str, tool_name: str, cost: float, arguments: Mapping[str, Any]
    ) -> None:
        del arguments
        now = time.monotonic()
        key = (owner, tool_name)
        weighted_cost = max(0.1, float(cost or 1.0))
        async with self._lock:
            cutoff = now - self.window_seconds
            events = [
                (stamp, weight)
                for stamp, weight in self._events.get(key, ())
                if stamp > cutoff
            ]
            if sum(weight for _stamp, weight in events) + weighted_cost > self.limit:
                raise ToolPipelineError(
                    "工具调用过于频繁，请稍后重试", code="rate_limited"
                )
            events.append((now, weighted_cost))
            self._events[key] = events


class EffectExecutor:
    """共享目录、授权、快照、claim 与回执；可直接由宿主业务入口使用。

    不调用 ToolPipeline，不接受脚本提供的确认凭据。所有写入只能消费本会话
    已冻结计划，旧工具入口与原生 SDK 使用相同生命周期和持久化存储。
    """

    def __init__(
        self,
        *,
        catalog: ToolCatalog,
        state_store: SessionStateStore,
        reference_store: ReferenceStore | None = None,
        effect_store: EffectPlanStore | None = None,
        projector: DefaultProjector | None = None,
        authorization: AuthorizationPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        effect_lifecycle: EffectLifecycle | None = None,
    ) -> None:
        self.catalog = catalog
        self.selection = catalog.selection
        self.state_store = state_store
        self.reference_store = reference_store or InMemoryReferenceStore()
        self.effect_store = effect_store or ConfirmationEffectPlanStore()
        self.projector = projector or catalog.projector or DefaultProjector()
        self.authorization = authorization or DefaultAuthorizationPolicy()
        self.rate_limiter = rate_limiter or InMemoryRateLimiter()
        self.effect_lifecycle = effect_lifecycle or NoopEffectLifecycle()
        from .scoped_trust import ScopedTrustPolicy
        self.scoped_trust = ScopedTrustPolicy(
            catalog=catalog, state_store=state_store, pending_plan=self.get_pending_plan,
            clock=lambda: getattr(getattr(self.effect_store, "store", None), "_clock", time.time)(),
        )
        from .batch_effects import batch_tool
        if not catalog.has("session.batch"):
            catalog.register(batch_tool(self))

    def _normalize_call(
        self, name: str, arguments: Mapping[str, Any] | None, context: ToolCallContext,
    ) -> tuple[KernelToolSpec, dict[str, Any], dict[str, Any]]:
        context.cancellation.raise_if_cancelled()
        try:
            tool = self.catalog.get(name)
        except KeyError as exc:
            raise ToolPipelineError("未知工具", code="tool_not_found") from exc
        if context.diagnostic_only and tool.effect is not ToolEffect.READ:
            raise ToolPipelineError("前序写操作未能完整核验；请先用只读业务工具核对真实状态，再为后续动作重新预检。新计划仍需确认，不能重放旧写入。", code="effect_diagnostic_only")
        raw_arguments = dict(arguments or {})
        if tool.effect is ToolEffect.READ:
            raw_arguments = normalize_read_arguments(raw_arguments, tool.input_schema)
        if self.selection is not None:
            self.selection.validate_call(tool, raw_arguments, context)
        elif context.selection_arguments is not None:
            raise ToolPipelineError("当前能力没有选择预检协议", code="selection_mismatch")
        validate_json_schema(raw_arguments, tool.input_schema)
        try:
            normalized = tool.validator(raw_arguments)
        except ToolPipelineError:
            raise
        except Exception as exc:
            safe_message = sanitize_public_text(
                getattr(exc, "safe_message", ""),
                limit=500,
            )
            error = ToolPipelineError(safe_message or "工具参数无效", code="invalid_arguments")
            raise error from exc
        if not isinstance(normalized, dict):
            raise ToolPipelineError(
                "工具参数校验器返回无效结果", code="invalid_arguments"
            )
        tool = tool.for_arguments(normalized)
        return tool, raw_arguments, normalized

    async def prepare(
        self, name: str, arguments: Mapping[str, Any] | None, *, context: ToolCallContext,
        prepare_only: bool = False, confirmation_deadline: float | None = None,
    ) -> PipelineResult:
        """由稳定目录名字预检并冻结计划；不执行业务写入，不依赖 ToolPipeline。"""
        started = time.monotonic()
        tool, raw_arguments, normalized = self._normalize_call(name, arguments, context)
        if tool.effect is ToolEffect.READ:
            raise ToolPipelineError("只读能力不能创建确认计划", code="confirmation_not_supported")
        return await self._prepare_effect(tool, raw_arguments, normalized, context, started,
            prepare_only=prepare_only, confirmation_deadline=confirmation_deadline)

    async def _prepare_effect(self, tool, raw_arguments, normalized, context, started, *,
                              prepare_only=False, confirmation_deadline=None) -> PipelineResult:
        resolved = await self._authorize(tool, normalized, context)
        await self._admit(tool, resolved, context)
        completed = context.confirmed_effect
        plan = completed.effect_plan if completed else None
        if (
            completed and plan and tool.effect is not ToolEffect.READ
            and (plan.owner, plan.session_id) == (context.owner, context.session_id)
            and tool.name == completed.tool.name and normalized == completed.arguments
            and completed.outcome.public_content.get("ok") is not False
        ):
            authoritative = await self.state_store.load(owner=context.owner, session_id=context.session_id)
            if not await self.state_store.is_current(context.lease):
                raise StalePublicationError("turn lost publication authority")
            if authoritative.metadata.get("confirmed_publication") == {
                "generation": context.lease.generation, "turn_id": context.lease.turn_id, "plan_id": plan.plan_id,
            }:
                # 只回读本次确认的事实，不重放写操作、票据、引用或状态更新。
                outcome = replace(completed.outcome, refs=(), state_updates=(), effect_plan=None,
                                  model_content="本轮该操作已确认执行；以下为原回执，不是再次执行。\n" + completed.outcome.model_message())
                return PipelineResult(tool=tool, arguments=normalized, outcome=outcome)

        if tool.prepare is None:  # pragma: no cover - KernelToolSpec 已校验
            raise ToolPipelineError("工具不支持预检", code="confirmation_not_supported")
        # 讨论保留原票据；只有已校验并准入的新写预检才能替换它。
        # 必须在领域prepare之前撤销：部分领域预检会替换自己的冻结快照。
        current = await self.state_store.load(owner=context.owner, session_id=context.session_id)
        if current.pending_effect_plan_id:
            await self.cancel_effect(current.pending_effect_plan_id, lease=context.lease)
        prepared_value = await invoke(tool.prepare, resolved, context)
        if isinstance(prepared_value, ToolOutcome):
            if prepared_value.public_content.get("ok") is not True or prepared_value.public_content.get("status") != "no_changes":
                raise ToolPipelineError("预检只能直接返回无需变更的结果", code="invalid_effect_preview")
            outcome = await self._materialize_refs(self.projector.project(prepared_value), context=context)
            await self._commit_updates(context.lease, outcome.state_updates)
            return PipelineResult(tool=tool, arguments=normalized, outcome=outcome,
                                  elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
        if not isinstance(prepared_value, PreparedEffect):
            raise ToolPipelineError(
                "工具预检返回无效结果", code="invalid_effect_preview"
            )
        if not prepared_value.snapshot_fingerprint:
            raise ToolPipelineError(
                "工具预检缺少快照指纹", code="invalid_effect_preview"
            )
        prepared_value = self._effect_prepared(
            tool=tool,
            arguments=normalized,
            prepared=prepared_value,
            context=context,
        )
        trust_scope = self.scoped_trust.scope(tool, prepared_value.arguments or normalized, prepared_value.metadata)
        from .continuation import reserve_budget_values
        frozen_budget = dict((context.continuation_policy or {}).get("budget") or {})
        if frozen_budget:
            reserve_budget_values(frozen_budget, {"stop": True}, self.state_store._clock())
        prepared_value = replace(prepared_value, metadata={
            **prepared_value.metadata,
            "confirmation_input": normalized,
            "editing": confirmation_editing(tool, normalized),
            "trust_eligible": trust_scope is not None,
            "trust_scope": trust_scope,
            **({"continuation_budget": {"budget": frozen_budget,
                 "task_id": (context.continuation_policy or {}).get("budget_task_id", "")}} if frozen_budget else {}),
            **({"confirmation_deadline": confirmation_deadline} if confirmation_deadline is not None else {}),
        })
        if prepare_only:
            return PipelineResult(tool=tool, arguments=normalized, prepared_effect=prepared_value,
                                  outcome=self.projector.project(prepared_value.preview))
        with ExitStack() as scope:
            plan: EffectPlan | None = None
            try:
                scope.enter_context(session_scope_guard(context.owner, context.session_id))
                if not await self.state_store.is_current(context.lease):
                    raise StalePublicationError("turn lost publication authority")
                preview_outcome = await self._materialize_refs(
                    self.projector.project(prepared_value.preview), context=context
                )
                try:
                    deadline = await self._reference_deadline(
                        [normalized, prepared_value.arguments], owner=context.owner, session_id=context.session_id,
                    )
                    try:
                        plan = await guarded_state_call(context.owner, context.session_id, partial(
                            self.effect_store.freeze,
                            owner=context.owner,
                            session_id=context.session_id,
                            generation=context.lease.generation,
                            tool_name=tool.name,
                            effect=tool.effect,
                            arguments=normalized,
                            prepared=prepared_value,
                            public_result=preview_outcome.public_content,
                            deadline=deadline,
                        ))
                    except _CancelledWorkerResult as exc:
                        plan = exc.result
                        raise
                except ReferenceError as exc:
                    raise ToolPipelineError("输入引用在预检期间已失效，本次未执行；请重新观察并预检",
                                            code="reference_invalid") from exc
                except AgentToolError as exc:
                    raise ToolPipelineError(str(exc), code=exc.code) from exc
                assert plan is not None
                updates = tuple(preview_outcome.state_updates) + (
                    _PendingEffectTransition(
                        "pending_effect_plan_id", plan.plan_id, plan_generation=plan.generation,
                        goal=({"role": "user", "content": context.user_source,
                               **({"reply_context": dict(context.user_reply_context)} if context.user_reply_context else {})}
                              if context.user_source else {}),
                    ),
                )
                if self.selection is not None:
                    updates += self.selection.plan_updates(tool, raw_arguments, plan)
                outcome = replace(preview_outcome, state_updates=updates, effect_plan=plan)
                await self._commit_updates(context.lease, updates)
                return PipelineResult(
                    tool=tool,
                    arguments=normalized,
                    outcome=outcome,
                    effect_plan=plan,
                    elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
                )
            except BaseException:
                if plan is None:
                    self._notify_effect("prepare_failed", prepared=prepared_value, context=context)
                else:
                    await self.discard_unpublished_effect(plan=plan, prepared=prepared_value, context=context)
                raise


    async def save_result(self, content: str, context: ToolCallContext) -> str:
        chunk_size = min(16_384, (getattr(self.reference_store, "max_ref_bytes", 131_072) - 128) // 6)
        parts = []
        for offset in range(0, len(content), chunk_size):
            part = await self._put_reference(context, kind="tool_result_part", value=content[offset:offset + chunk_size], ttl_seconds=900)
            parts.append(part.ref)
        snapshot = await self._put_reference(context, kind="tool_result", value={"parts": parts}, ttl_seconds=900)
        return snapshot.ref


    def _effect_prepared(
        self,
        *,
        tool: KernelToolSpec,
        arguments: Mapping[str, Any],
        prepared: PreparedEffect,
        context: ToolCallContext,
    ) -> PreparedEffect:
        try:
            value = self.effect_lifecycle.prepared(
                tool=tool,
                arguments=arguments,
                prepared=prepared,
                context=context,
            )
        except Exception:  # noqa: BLE001 - 辅助领域状态不得遮蔽有效预检
            return prepared
        return value if isinstance(value, PreparedEffect) else prepared


    def _notify_effect(self, method: str, **payload: Any) -> None:
        """终态钩子共享容错边界；辅助审计/清理故障不覆写执行事实。"""
        try:
            getattr(self.effect_lifecycle, method)(**payload)
        except Exception:  # noqa: BLE001 - hooks cannot change authoritative outcome
            return


    async def discard_unpublished_effect(
        self,
        *,
        plan: EffectPlan,
        context: ToolCallContext,
        prepared: PreparedEffect | None = None,
    ) -> None:
        """预览发布失败时撤销已冻结票据并释放领域预占状态。"""
        cancelled: EffectPlan | None = None
        try:
            cancelled = await guarded_state_call(context.owner, context.session_id, partial(
                self.effect_store.cancel,
                owner=context.owner,
                session_id=context.session_id,
                generation=context.lease.generation,
                plan_id=plan.plan_id,
            ), complete_on_cancel=True)
        except Exception:  # noqa: BLE001 - 原始发布失败仍是主错误
            cancelled = None
        if cancelled is not None:
            self._notify_effect("cancelled", plan=cancelled)
        elif prepared is not None:
            self._notify_effect("prepare_failed", prepared=prepared, context=context)
        try:
            await self._commit_updates(context.lease, (
                _PendingEffectTransition("pending_effect_plan_id", plan.plan_id,
                                         mode="clear_if_equals", cancelled=cancelled is not None),
            ))
        except StalePublicationError:
            # 停止/新回合已经撤销发布权；只撤原票据，不能改新回合的状态。
            pass


    @staticmethod
    def _pending_generation(state: SessionState) -> int | None:
        binding = state.metadata.get("pending_effect_binding")
        if binding is None:
            return state.generation
        if (isinstance(binding, Mapping) and set(binding) == {"plan_id", "plan_generation"}
                and binding.get("plan_id") == state.pending_effect_plan_id
                and type(binding.get("plan_generation")) is int
                and 0 < binding["plan_generation"] <= state.generation):
            return binding["plan_generation"]
        return None


    async def edit_confirmation(
        self, plan_id: str, patch: Mapping[str, Any], *, context: ToolCallContext,
    ) -> PipelineResult:
        with session_scope_guard(context.owner, context.session_id):
            plan = await self.get_pending_plan(owner=context.owner, session_id=context.session_id)
            if plan is None or plan.plan_id != plan_id:
                raise ConfirmationClaimError()
            # 与确认共享会话互斥；撤销成功前绝不进入新预检。
            if not await self._cancel_effect_scoped(plan_id, lease=context.lease):
                raise ConfirmationClaimError()
            try:
                tool = self.catalog.get(plan.tool_name)
                original = plan.metadata.get("confirmation_input")
                if not isinstance(original, Mapping):
                    raise ToolPipelineError("原确认卡不支持编辑，请重新预检。", code="confirmation_edit_invalid")
                editable = confirmation_editing(tool, original)["schema"]["properties"]
                if not isinstance(patch, Mapping) or not patch or not set(patch) <= editable.keys():
                    raise ToolPipelineError("只能修改卡片允许的字段，请重新预检。", code="confirmation_edit_invalid")
                # 仍走完整 Schema、validator、鉴权、限流、原 prepare 与冻结链路。
                return await self.prepare(tool.name, {**original, **patch}, context=context,
                                          confirmation_deadline=plan.expires_at)
            except Exception as exc:
                error = exc if isinstance(exc, ToolPipelineError) else ToolPipelineError(
                    "修改预检未完成，请重新预检。", code="confirmation_edit_failed")
                error.plan_invalidated = True
                raise error from (exc if error is not exc else None)


    async def get_pending_plan(self, *, owner: str, session_id: str) -> EffectPlan | None:
        """只读卡片快照不争抢写锁；确认/编辑入口仍在锁内重新验证票据。"""
        state = await self.state_store.load(owner=owner, session_id=session_id)
        generation = self._pending_generation(state)
        if not state.pending_effect_plan_id or generation is None:
            return None
        plan = await asyncio.to_thread(self.effect_store.get_active_plan,
            owner=owner, session_id=session_id, generation=generation, plan_id=state.pending_effect_plan_id)
        if plan is None:
            return None
        try:
            deadline = await self._reference_deadline(plan.arguments, owner=owner, session_id=session_id)
        except ReferenceError:
            return None
        if deadline is None:
            return plan
        clock = getattr(getattr(self.effect_store, "store", None), "_clock", time.time)
        now = clock()
        remaining = deadline - time.monotonic()
        return replace(plan, expires_at=min(plan.expires_at, now + remaining)) if remaining > 0 else None


    def pending_guard(self, state: SessionState, plan: EffectPlan) -> PendingEffectGuard:
        clock = getattr(getattr(self.effect_store, "store", None), "_clock", time.time)
        return PendingEffectGuard(state.generation, plan.plan_id, plan.generation, plan.expires_at, clock)


    async def get_confirmation_evidence(self, *, owner: str, session_id: str, scoped: bool = False) -> EffectPlan | None:
        """确认入口读取原计划证据；不是可用卡查询，更不是写准入。"""
        with nullcontext() if scoped else session_scope_guard(owner, session_id):
            state = await self.state_store.load(owner=owner, session_id=session_id)
            generation = self._pending_generation(state)
            if not state.pending_effect_plan_id or generation is None:
                return None
            return await asyncio.to_thread(self.effect_store.get_plan_evidence,
                owner=owner, session_id=session_id, generation=generation, plan_id=state.pending_effect_plan_id)


    async def execute_confirmed(
        self, plan_id: str, *, context: ToolCallContext, scoped_trust: bool = False,
    ) -> PipelineResult:
        with session_scope_guard(context.owner, context.session_id, kind="effect"):
            if scoped_trust:
                plan = await self.get_pending_plan(owner=context.owner, session_id=context.session_id)
                if plan is None or plan.plan_id != plan_id or not await self.scoped_trust.allows(plan, context=context):
                    raise ConfirmationClaimError()
                context = replace(context, scoped_trust=True)
            return await self._execute_confirmed_scoped(plan_id, context=context)


    async def _execute_confirmed_scoped(
        self,
        plan_id: str,
        *,
        context: ToolCallContext,
    ) -> PipelineResult:
        started = time.monotonic()
        context.cancellation.raise_if_cancelled()
        current = await self.state_store.load(owner=context.owner, session_id=context.session_id)
        if current.generation != context.lease.generation:
            raise StalePublicationError("turn lost publication authority")
        pending = await self.get_confirmation_evidence(owner=context.owner, session_id=context.session_id, scoped=True)
        if pending is None or pending.plan_id != plan_id:
            await self._commit_updates(context.lease, (
                _PendingEffectTransition("pending_effect_plan_id", plan_id, mode="clear_if_equals"),
            ))
            raise ConfirmationClaimError()
        rejection = None
        try:
            plan = await asyncio.to_thread(
                self.effect_store.claim,
                owner=context.owner,
                session_id=context.session_id,
                generation=pending.generation,
                plan_id=plan_id,
            )
        except EffectPlanExpiredError as exc:
            # 存储已原子注销过期票据；只登记失败并恢复只读复验，绝不进入 handler。
            plan = exc.plan
            rejection = ToolPipelineError("确认计划或输入引用已过期，请重新观察后预检", code="confirmation_stale")
        except ToolPipelineError:
            raise
        except Exception as exc:
            # 只有确定失效的当前计划才清理；存储故障不能撤销仍可确认的计划。
            if isinstance(exc, EffectPlanError) and current.pending_effect_plan_id == plan_id:
                await self._commit_updates(context.lease, (
                    _PendingEffectTransition("pending_effect_plan_id", plan_id, mode="clear_if_equals"),
                ))
            raise ConfirmationClaimError() from exc

        from .continuation import effect_origin

        completion_scope = None

        async def remember_failure(result):
            if completion_scope is None:
                return
            try:
                await self.effect_lifecycle.register_terminal_failure(scope=completion_scope, tool=plan.tool_name, result=result)
            except Exception as exc:  # 登记故障不能覆盖已经成立的原始失败事实/code。
                from app.logger import get_logger
                get_logger(__name__).warning("确认失败续行登记未完成 type=%s", type(exc).__name__)

        try:
            # 只有真实领取或一次性注销过期票据才接管同 generation 的发布权。该标记在原状态
            # JSON 内持久化，使锁释放/进程结束后旧读回合仍无法覆盖确认回执。
            await self.state_store.commit(
                context.lease,
                updates=(
                    _ConfirmationClaim(
                        "",
                        {
                            "generation": context.lease.generation,
                            "turn_id": context.lease.turn_id,
                            "plan_id": plan.plan_id,
                        },
                        request_id=context.lease.request_id,
                    ),
                ),
            )
            await context.report_progress({
                "plan_id": plan_id, "kind": "confirmed_effect", "tool": plan.tool_name,
            })
            try:
                from .batch_progress import restored_effect_tool
                tool = restored_effect_tool(self.catalog, plan.tool_name, plan.arguments)
            except KeyError as exc:
                raise ToolPipelineError(
                    "确认计划对应工具不存在", code="tool_not_found"
                ) from exc
            if tool.effect is ToolEffect.READ or tool.effect is not plan.effect:
                raise ToolPipelineError(
                    "确认计划风险类型不匹配", code="confirmation_invalid"
                )
            if tool.execute_confirmed is None:  # pragma: no cover - KernelToolSpec 已校验
                raise ToolPipelineError(
                    "工具不支持确认执行", code="confirmation_not_supported"
                )
            completion_scope = EffectCompletionScope(
                store=self.state_store, lease=context.lease,
                plan_id=plan.plan_id, channel=context.channel,
                origin=effect_origin(current, context, plan),
            )
            context = replace(context, completion_scope=completion_scope)
            if rejection is not None:
                raise rejection
            value = await self._run_effect(plan, context=context)
        except ToolPipelineError as exc:
            exc.mark_execution_started(False)
            self._notify_effect(
                "failed", plan=plan, code=exc.code,
                elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
            await remember_failure({"ok": False, "status": exc.code, "summary": str(exc),
                                    "data": {"execution_started": exc.execution_started}})
            raise
        except BaseException:
            self._notify_effect("interrupted", plan=plan)
            raise

        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        # 写操作与写后验证已取得可信终态。先收束审计并推进全局运行代次；
        # 后续 DTO 投影、引用或会话提交失败，不得把已发生的副作用伪装成未执行。
        self._notify_effect("completed", plan=plan, value=value, elapsed_ms=elapsed_ms)
        outcome = await self._materialize_refs(
            self.projector.project(value), context=context
        )
        updates = tuple(outcome.state_updates)
        outcome = replace(outcome, effect_plan=plan)
        await self._commit_updates(context.lease, updates)
        await remember_failure(outcome.public_content)
        return PipelineResult(
            tool=tool,
            arguments=dict(plan.arguments),
            outcome=outcome,
            effect_plan=plan,
            elapsed_ms=elapsed_ms,
        )


    async def _run_effect(self, plan: EffectPlan, *, context: ToolCallContext):
        try:
            from .batch_progress import restored_effect_tool
            tool = restored_effect_tool(self.catalog, plan.tool_name, plan.arguments)
            if tool.effect is ToolEffect.READ or tool.effect is not plan.effect:
                raise ToolPipelineError("确认计划风险类型不匹配", code="confirmation_invalid")
            context = replace(context, active_effect_plan=plan)
            resolved_arguments = await self._authorize(tool, dict(plan.arguments), context, confirmation=True)
            await self._admit(tool, resolved_arguments, context, confirmation=True)
            if context.scoped_trust:
                await self.scoped_trust.consume(plan, context=context)
        except ToolPipelineError as exc:
            exc.mark_execution_started(False)
            raise
        try:
            # WRITE/DANGER 的处理函数已由 KernelToolSpec 校验。
            assert tool.execute_confirmed is not None
            value = await invoke(
                tool.execute_confirmed,
                resolved_arguments,
                plan.snapshot_fingerprint,
                context,
                protection=context,
            )
            if tool.verify is not None:
                verified = await invoke(
                    tool.verify,
                    resolved_arguments,
                    value,
                    context,
                    protection=context,
                )
                if verified is False:
                    raise ToolPipelineError(
                        "写后验证失败", code="post_write_verification_failed"
                    )
                if verified is not True and verified is not None:
                    value = verified
            return value
        except ToolPipelineError as exc:
            exc.mark_execution_started(True)
            raise


    async def cancel_effect(
        self, plan_id: str, *, lease: PublicationLease,
    ) -> bool:
        with session_scope_guard(lease.owner, lease.session_id, kind="effect"):
            return await self._cancel_effect_scoped(plan_id, lease=lease)


    async def _cancel_effect_scoped(self, plan_id: str, *, lease: PublicationLease) -> bool:
        current = await self.state_store.load(owner=lease.owner, session_id=lease.session_id)
        if current.generation != lease.generation:
            raise StalePublicationError("turn lost publication authority")
        if current.pending_effect_plan_id != plan_id:
            return False
        generation = self._pending_generation(current)
        try:
            # 撤票及其历史收尾共用当前会话锁；请求取消不能先于实际撤票放锁。
            cancelled_plan = await guarded_state_call(lease.owner, lease.session_id, partial(
                self.effect_store.cancel, owner=lease.owner, session_id=lease.session_id,
                generation=generation if generation is not None else current.generation, plan_id=plan_id,
            ), kind="commit", complete_on_cancel=True)
            cancelled = cancelled_plan is not None
        except EffectPlanExpiredError as exc:
            cancelled_plan, cancelled = exc.plan, False
        if cancelled_plan is not None:
            self._notify_effect("cancelled", plan=cancelled_plan)
        await self._commit_updates(lease, (
            _PendingEffectTransition("pending_effect_plan_id", plan_id, mode="clear_if_equals",
                                     cancelled=cancelled),
        ))
        return cancelled


    async def _authorize(
        self, tool: KernelToolSpec, arguments: dict[str, Any], context: ToolCallContext, *, confirmation: bool = False,
    ) -> dict[str, Any]:
        """解析引用并复核权限；组合预复验不消费执行额度。"""
        try:
            resolved = await self._resolve_references(arguments, context=context)
            if self.selection is not None:
                await self.selection.authorize(tool, arguments, resolved, context, self.reference_store, self.state_store)
        except ReferenceError as exc:
            raise ToolPipelineError(
                "确认计划引用无效、已过期或不属于当前会话" if confirmation else "工具引用无效、已过期或不属于当前会话",
                code="confirmation_stale" if confirmation else "reference_invalid",
            ) from exc
        await self.authorization.authorize(tool, resolved, context)
        context.cancellation.raise_if_cancelled()
        return resolved


    async def _admit(
        self, tool: KernelToolSpec, arguments: dict[str, Any], context: ToolCallContext, *, confirmation: bool = False,
    ) -> None:
        """实际调用前领取一次额度，并在等待后复核取消与回合有效性。"""
        try:
            await self.rate_limiter.acquire(
                owner=context.owner, tool_name=f"confirm:{tool.name}" if confirmation else tool.name,
                cost=max(1.0, tool.cost) if confirmation else tool.cost, arguments=arguments,
            )
        except ToolRateLimitError as exc:
            if not confirmation and tool.effect is ToolEffect.READ and not tool.name.startswith("agent."):
                raise _ReadPermitRejected(exc, tool.name) from exc
            raise
        context.cancellation.raise_if_cancelled()
        # A preview cannot reenter a held confirmation, even with an old lease.
        # Reject busy before stale so the confirmed receipt can finish settling.
        scope = (session_scope_guard(context.owner, context.session_id)
                 if not confirmation and tool.effect is not ToolEffect.READ else nullcontext())
        with scope:
            if context.lease.continuation_id:
                # Same-connection fence check linearizes handler start after acquire.
                await self.state_store.commit(context.lease)
            elif not await self.state_store.is_current(context.lease):
                raise StalePublicationError("turn revoked before handler")


    async def _reference_deadline(self, value: Any, *, owner: str, session_id: str) -> float | None:
        deadlines = []

        async def visit(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if key.endswith("_ref") and isinstance(child, str) and child.startswith("ref_"):
                        deadlines.append(await self.reference_store.deadline(
                            child, owner=owner, session_id=session_id, expected_kind=key[:-4].strip("_").casefold(),
                        ))
                    else:
                        await visit(child)
            elif isinstance(node, list):
                for child in node:
                    await visit(child)

        await visit(value)
        return min(deadlines, default=None)


    async def _resolve_references(
        self,
        value: Any,
        *,
        context: ToolCallContext,
    ) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for child_key, child_value in value.items():
                if (
                    child_key.endswith("_ref")
                    and isinstance(child_value, str)
                    and child_value.startswith("ref_")
                ):
                    expected_kind = child_key[:-4].strip("_").casefold()
                    result[
                        child_key[:-4] or child_key
                    ] = await self.reference_store.resolve(
                        child_value,
                        owner=context.owner,
                        session_id=context.session_id,
                        expected_kind=expected_kind,
                    )
                else:
                    result[child_key] = await self._resolve_references(child_value, context=context)
            return result
        if isinstance(value, list):
            return [
                await self._resolve_references(item, context=context)
                for item in value
            ]
        return value


    async def _put_reference(self, context, **kwargs):
        put = getattr(self.reference_store, "put_for_turn", None)
        if put is not None:
            return await put(context.lease, owner=context.owner, session_id=context.session_id, **kwargs)
        if not await self.state_store.is_current(context.lease):
            raise StalePublicationError("turn revoked before reference")
        return await self.reference_store.put(owner=context.owner, session_id=context.session_id, **kwargs)


    async def _materialize_refs(
        self,
        outcome: ToolOutcome,
        *,
        context: ToolCallContext,
        preserve_native: bool = False,
    ) -> ToolOutcome:
        if not outcome.refs and not outcome.full_model_content:
            return outcome
        exposed: list[dict[str, str]] = []
        reference_arguments: dict[str, str] = {}
        issued_references = []
        for item in outcome.refs:
            if not isinstance(item, ReferenceValue):
                raise ToolPipelineError(
                    "工具返回了无效引用", code="invalid_tool_result"
                )
            reference = await self._put_reference(
                context,
                kind=item.kind,
                value=item.value,
                ttl_seconds=item.ttl_seconds,
            )
            issued_references.append((item, reference))
            exposed.append({"ref": reference.ref, "kind": reference.kind})
            kind = re.sub(r"[^a-z0-9_]+", "_", reference.kind.casefold()).strip("_")
            if kind:
                reference_arguments[f"{kind}_ref"] = reference.ref
        outcome = await self.projector.present_references(outcome, issued_references,
            context=context, reference_store=self.reference_store)
        public = dict(outcome.public_content)
        public["refs"] = exposed
        if reference_arguments:
            public["reference_arguments"] = reference_arguments
        content = outcome.full_model_content or outcome.model_content
        payload = decode_tool_content(content)
        if exposed:
            payload["opaque_refs"] = exposed
            payload["reference_arguments"] = reference_arguments
        model_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        # Reference appendices can also become lossy in the later 400-char pass.
        if outcome.full_model_content or len(model_content) > 400:
            handle = await self.save_result(model_content, context)
            public.update(result_handle=handle, read_method="session.result")
            try:
                payload = decode_tool_content(compact_tool_content(model_content, maximum=self.projector.max_model_chars, trusted=not bool(outcome.full_model_content)))
                payload.update(result_handle=handle, read_method="session.result")
                model_content = compact_tool_content(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), maximum=self.projector.max_model_chars, trusted=True)
            except ToolPipelineError as exc:
                # Eager snapshots must not narrow the pre-projected ToolOutcome
                # domain: atomic status/code facts may fit native history even
                # when they cannot fit a compact continuation envelope. Keep the
                # original fact (never infer success from the public summary).
                # The complete scoped snapshot remains publicly readable; normal
                # Session/provider/state admission still applies without changes.
                if (exc.code != "context_budget_exceeded" or not preserve_native or exposed
                        or outcome.full_model_content != outcome.model_content):
                    raise
                model_content = outcome.model_content
        updates = tuple(outcome.state_updates) + (
            StateUpdate("recent_refs", [item["ref"] for item in exposed], mode="append"),
            StateUpdate("ref_kinds", [item["kind"] for item in exposed], mode="append"),
        )
        if public.get("result_handle"):
            updates += (StateUpdate("recent_refs", [public["result_handle"]], mode="append"), StateUpdate("ref_kinds", ["tool_result"], mode="append"))
        return replace(
            outcome,
            public_content=public,
            model_content=model_content,
            state_updates=updates,
            full_model_content="",
        )


    async def _commit_updates(
        self,
        lease: PublicationLease,
        updates: Sequence[StateUpdate],
    ) -> None:
        if not updates:
            if not await self.state_store.is_current(lease):
                raise StalePublicationError("turn lost publication authority")
            return
        await self.state_store.commit(lease, updates=updates)
