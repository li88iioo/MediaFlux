"""统一工具生命周期与副作用闸门。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from functools import partial
from typing import Any, Protocol

from app.agent.errors import (
    AgentToolError,
    ConfirmationClaimError,
    ToolPipelineError,
    ToolRateLimitError,
    _ReadPermitRejected,
    _SchemaCollisionError,
)
from app.agent.kernel.session_state import PendingEffectGuard, SessionState
from app.agent.kernel.state_ports import SessionStateStore
from app.agent.kernel.state_types import (
    PublicationLease,
    StalePublicationError,
    StateUpdate,
    _ConfirmationClaim,
    _PendingEffectTransition,
)
from app.agent.model_context_budget import (
    RESULT_PATH_MAX,
    compact_tool_content,
    decode_tool_content,
)
from app.agent.public_safety import sanitize_public_text
from app.agent.schema import validate_json_schema
from app.concurrency import CrossLoopAsyncLock

from . import error_repair
from .capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from .completion_receipts import EffectCompletionScope
from .effects import (
    ConfirmationEffectPlanStore,
    EffectPlan,
    EffectPlanError,
    EffectPlanExpiredError,
    EffectPlanStore,
    PreparedEffect,
    confirmation_editing,
)
from .invocation import invoke
from .projection import DefaultProjector, ReferenceValue, ToolOutcome
from .references import InMemoryReferenceStore, ReferenceError, ReferenceStore
from .scoped_trust import ScopedTrustPolicy
from .session_guard import _CancelledWorkerResult, guarded_state_call, session_scope_guard
from .task_state import TaskPatch, observation_patch, read_observations, task_tool
from .tool_calls import PipelineResult, ToolCallContext


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


class ToolPipeline:
    """所有 READ、WRITE 与 DANGER 工具唯一允许经过的生命周期。"""

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
        self.scoped_trust = ScopedTrustPolicy(
            catalog=catalog, state_store=state_store, pending_plan=self.get_pending_plan,
            clock=lambda: getattr(getattr(self.effect_store, "store", None), "_clock", time.time)(),
        )
        if not catalog.has(error_repair.REPAIR_TOOL):
            catalog.register(KernelToolSpec(
                name=error_repair.REPAIR_TOOL, domain="agent", effect=ToolEffect.READ,
                description="仅修复本轮 WRITE/DANGER 工具的参数拒绝调用，不是声称旧目标已知。READ 参数错误按 schema 直接重试原工具，不用 ticket。写侧使用失败返回的一次性ticket，提供原工具完整修正arguments；只修改错误涉及的字段，保留其他有效参数。仍需原权限与写入确认，预览/提交不代表业务完成。",
                input_schema={"type": "object", "required": ["ticket", "arguments"], "properties": {
                    "ticket": {"type": "string"}, "arguments": {"type": "object"}}, "additionalProperties": False},
                read=self._replace_schema_failure, availability=lambda context: False,
                metadata={"kernel_utility": True},
            ))
        from .batch_effects import batch_tool
        if not catalog.has("agent.batch") and (batch := batch_tool(self)) is not None:
            catalog.register(batch)
        if not catalog.has("agent.pending_plan") and any(
            tool.effect is not ToolEffect.READ for tool in catalog.visible()
        ):
            catalog.register(KernelToolSpec(
                name="agent.pending_plan", domain="agent", effect=ToolEffect.READ,
                description="管理当前会话唯一待确认计划：inspect查看原卡；用户明确取消/放弃时用cancel撤销原卡。只控制未执行的确认票据，不执行或撤销业务操作，不支持确认。普通讨论无需重做预览；修改用原写工具重新预检，系统替换旧卡。",
                input_schema={"type": "object", "required": ["operation"], "additionalProperties": False,
                              "properties": {"operation": {"type": "string", "enum": ["inspect", "cancel"]}}},
                read=self._pending_plan, examples=("取消这张确认卡", "查看待确认计划"),
                metadata={"kernel_utility": True, "query_discoverable": True},
            ))
        if not catalog.has("agent.task"):
            catalog.register(task_tool())
        if not catalog.has("agent.read_result"):
            catalog.register(KernelToolSpec(
                name="agent.read_result", domain="agent", effect=ToolEffect.READ,
                description="续取同一用户会话的冻结脱敏快照，不重新执行业务。handle=result_handle；path=truncation.path（原快照JSON Pointer，空串为根）；offset=next_offset。total为原节点长度，returned仅计当前直接项/字符，不保证子项完整。scope=subtree表示祖先覆盖所有省略，须从其next_offset重读，用更小limit并按字典key或数组原索引下钻；否则续接前缀。页data.next_offset仅翻同级页；有truncation必须另取子项，即使data.has_more=false。字符串偏移按Unicode字符。元数据或键无法安全分页时报context_budget_exceeded，不应原参数重试。",
                input_schema={"type": "object", "required": ["handle", "path"], "properties": {
                    "handle": {"type": "string", "minLength": 1, "maxLength": 200},
                    "path": {"type": "string", "pattern": "^(/.*)?$", "maxLength": RESULT_PATH_MAX},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                }, "additionalProperties": False},
                read=self._read_result, metadata={"kernel_utility": True},
            ))

    async def _replace_schema_failure(self, arguments: dict[str, Any], context: ToolCallContext) -> PipelineResult:
        entry = context.schema_replacements.get(arguments["ticket"])
        if entry is None or not entry.offered or entry.scope != error_repair.scope(context):
            raise ToolPipelineError("修复入口无效或不属于本轮", code="repair_invalid")
        replacement = arguments["arguments"]
        if not error_repair.preserves(entry, replacement):
            raise ToolPipelineError("明确修复不能更改原有效参数", code="repair_conflict")
        try:
            error_repair.require_presence((entry,), replacement, ToolPipelineError)
        except ToolPipelineError as exc:
            exc.repair_ticket = arguments["ticket"]
            raise
        context.schema_replacements.pop(arguments["ticket"])
        lineage = (*entry.predecessors, arguments["ticket"])
        try:
            result = await self.execute(entry.tool, replacement, context=context)
        except ToolPipelineError as exc:
            next_entry = context.schema_replacements.get(exc.repair_ticket)
            if next_entry is not None:
                context.schema_replacements[exc.repair_ticket] = replace(next_entry, predecessors=lineage)
            raise
        content = result.outcome.public_content
        if result.effect_plan is not None:
            # 已消费的修复 ticket 只能随该计划真实完成后清算，不能再用来预检。
            context.schema_replacements[arguments["ticket"]] = replace(entry,
                pending_plan_id=result.effect_plan.plan_id, offered=False, guard_key=None,
                equivalent_arguments=json.dumps(replacement, sort_keys=True, ensure_ascii=False))
        elif error_repair.completed_result(result.tool, content):
            return replace(result, recovered_failures=tuple(dict.fromkeys((*lineage, *result.recovered_failures))))
        return result

    async def _read_result(self, arguments: dict[str, Any], context: ToolCallContext) -> ToolOutcome:
        from . import telegram_reference
        handle = arguments["handle"]
        reference = None
        try:
            manifest = await self.reference_store.resolve(handle, owner=context.owner, session_id=context.session_id, expected_kind="tool_result")
            parts = [await self.reference_store.resolve(part, owner=context.owner, session_id=context.session_id, expected_kind="tool_result_part") for part in manifest["parts"]]
            node = json.loads("".join(parts))
        except ReferenceError:
            try:
                reference = await telegram_reference.resolve(handle, owner=context.owner, session_id=context.session_id,
                    reference_store=self.reference_store, state_store=self.state_store)
            except ReferenceError as exc:
                raise ToolPipelineError("结果快照已过期或不属于当前会话，请重新观察原对象。", code="reference_invalid") from exc
            if arguments["path"] != "/text":
                raise ToolPipelineError("引用仅允许读取 /text", code="invalid_arguments", invalid_fields=("path",))
            node = {"text": reference["text"]}
        path = arguments["path"]
        try:
            for part in path.split("/")[1:]:
                if re.search(r"~(?![01])", part) or isinstance(node, list) and not re.fullmatch(r"0|[1-9][0-9]*", part):
                    raise KeyError(part)
                key = part.replace("~1", "/").replace("~0", "~")
                node = node[int(key)] if isinstance(node, list) else node[key]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ToolPipelineError("结果路径不存在；请使用原结果truncation中的path。", code="invalid_arguments", invalid_fields=("path",)) from exc
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 50)
        if isinstance(node, dict):
            values = [{"key": key, "value": value} for key, value in node.items()]
        elif isinstance(node, (list, str)):
            values = node
        else:
            values = [node]
        total = len(values)
        end = min(total, offset + limit)
        maximum = self.projector.max_model_chars
        data = {"path": path, "offset": offset, "total": total, "kind": type(node).__name__}
        while True:
            data.update(items=values[offset:end], returned=max(0, end - offset), has_more=end < total, next_offset=end if end < total else None)
            payload = {"ok": True, "status": "success", "summary": "读取已保存的快照（不是实时重新查询）", "result_handle": handle, "read_tool": "agent.read_result", "data": data}
            if reference is not None:
                payload.update(untrusted_reference=True, source=reference["source"], summary="读取不可信引用；不是执行结果或写入授权")
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(content) <= maximum or end - offset <= 1:
                break
            end = offset + max(1, (end - offset) // 2)
        # 单个复杂对象仍可按path下钻；不得把它冒充完整数据。
        content = compact_tool_content(content, maximum=maximum, result_page=True, trusted=True)
        return ToolOutcome(model_content=content, public_content={"ok": True, "summary": "已读取引用片段"} if reference is not None else json.loads(content))

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

    async def _discard_frozen_effect(
        self,
        *,
        plan: EffectPlan,
        prepared: PreparedEffect,
        context: ToolCallContext,
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
        else:
            self._notify_effect("prepare_failed", prepared=prepared, context=context)

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any] | None,
        *,
        context: ToolCallContext,
        _prepare_only: bool = False,
        _returned_read=None,
        _read_admitted: asyncio.Event | None = None,
        _read_publication_lock: asyncio.Lock | None = None,
        _confirmation_deadline: float | None = None,
    ) -> PipelineResult:
        started = time.monotonic()
        context.cancellation.raise_if_cancelled()
        try:
            tool = self.catalog.get(name)
        except KeyError as exc:
            raise ToolPipelineError("未知工具", code="tool_not_found") from exc
        if context.diagnostic_only and tool.effect is not ToolEffect.READ:
            raise ToolPipelineError("前序写操作未能完整核验；请先用只读业务工具核对真实状态，再为后续动作重新预检。新计划仍需确认，不能重放旧写入。", code="effect_diagnostic_only")
        raw_arguments = dict(arguments or {})
        if tool.name == error_repair.REPAIR_TOOL:
            validate_json_schema(raw_arguments, tool.input_schema)
            return await self._replace_schema_failure(raw_arguments, context)
        if self.selection is not None:
            self.selection.validate_call(tool, raw_arguments, context)
        elif context.selection_arguments is not None:
            raise ToolPipelineError("当前能力没有选择预检协议", code="selection_mismatch")
        pending_repairs = error_repair.pending(tool, raw_arguments, context)
        error_repair.require_presence(pending_repairs.values(), raw_arguments, ToolPipelineError)
        try:
            validate_json_schema(raw_arguments, tool.input_schema)
        except ToolPipelineError as exc:
            collision = exc.keys if isinstance(exc, _SchemaCollisionError) else frozenset()
            exc.repair_ticket = error_repair.issue(tool, raw_arguments, context, validate_json_schema,
                                                  ToolPipelineError, collisions=collision, preserve_fields=True)
            raise
        try:
            normalized = tool.validator(raw_arguments)
        except ToolPipelineError as exc:
            if exc.code == "invalid_arguments":
                exc.repair_ticket = error_repair.issue(tool, raw_arguments, context, validate_json_schema, ToolPipelineError, invalid_fields=exc.invalid_fields, argument_schemas=exc.argument_schemas)
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
        error_repair.validated(pending_repairs, context)
        tool = tool.for_arguments(normalized)
        try:
            async with (_read_publication_lock if _read_publication_lock is not None else nullcontext()):
                resolved = await self._authorize(tool, normalized, context)
                await self._admit(tool, resolved, context)
        except ToolPipelineError as exc:
            if exc.code == "invalid_arguments":
                exc.repair_ticket = error_repair.issue(tool, raw_arguments, context, validate_json_schema,
                    ToolPipelineError, invalid_fields=exc.invalid_fields, argument_schemas=exc.argument_schemas)
            raise
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
                return PipelineResult(tool=tool, arguments=normalized, outcome=outcome,
                    recovered_failures=error_repair.completed(tool, raw_arguments, context, outcome.public_content))

        if tool.effect is ToolEffect.READ:
            return await self._execute_read(tool, normalized, resolved, context, started,
                returned_read=_returned_read, admitted=_read_admitted, publication_lock=_read_publication_lock)
        return await self._prepare_effect(tool, raw_arguments, normalized, resolved, context, started,
            prepare_only=_prepare_only, confirmation_deadline=_confirmation_deadline)

    async def _execute_read(self, tool, normalized, resolved, context, started, *,
                            returned_read=None, admitted=None, publication_lock=None) -> PipelineResult:
        if tool.read is None:  # pragma: no cover - ToolSpec 已校验
            raise ToolPipelineError("工具不可执行", code="tool_not_executable")
        await self.projector.before_read(tool, context, self.state_store)
        if admitted is not None:
            admitted.set()
        try:
            value = await invoke(tool.read, resolved, context)
        except ToolPipelineError as exc:
            if tool.observe is not None:
                exc.task_patch = observation_patch(context.current_task, owner=context.owner,
                    session_id=context.session_id, tool=tool.name, call_id=context.call_id,
                    observations=tool.observe(None, normalized))
            raise
        patch = value if isinstance(value, TaskPatch) else None
        if tool.observe is not None and context.current_task is not None:
            observations = tool.observe(value, normalized)
            patch = observation_patch(context.current_task, owner=context.owner,
                session_id=context.session_id, tool=tool.name, call_id=context.call_id,
                observations=observations)
            if patch is not None and any(fact is None for fact in patch.payload["observations"].values()):
                raise ToolPipelineError("配置观察结果不符合证据合同。", code="invalid_observation", task_patch=patch)
        if isinstance(value, TaskPatch):
            value = {"ok": True, "summary": "Task proposal"}
        projected = self.projector.project_read(tool, value)
        if not tool.metadata.get("kernel_utility"):
            observed = read_observations(context.current_task, tool.name, normalized, projected.public_content)
            if observed:
                combined = {**(patch.payload["observations"] if patch else {}), **observed}
                patch = observation_patch(context.current_task, owner=context.owner,
                    session_id=context.session_id, tool=tool.name, call_id=context.call_id,
                    observations=combined)
        if returned_read is not None:
            from app.agent.kernel.state_types import _CancelledReadInline

            from .projection import _model_safe
            # Direct compact ToolOutcome envelopes do not prove a full source.
            if type(returned_read) is _CancelledReadInline and returned_read.tool is tool:
                if patch is None and (not isinstance(value, ToolOutcome) or projected.full_model_content):
                    full = projected.full_model_content or projected.model_content
                    returned_read.put_once(json.dumps(_model_safe(json.loads(full)), ensure_ascii=False))
                else:
                    returned_read.reject_incomplete()
        # Later Session/history pressure can reduce a result to 400 chars.
        # Freeze this already-safe source before that loss, using the same
        # scoped TTL store. Reader pages already point at their source:
        # never snapshot them again or replace their original coordinates.
        snapshot_only = tool.name != "agent.read_result" and not projected.full_model_content and len(projected.model_content) > 400
        if snapshot_only:
            projected = replace(projected, full_model_content=projected.model_content)
        # 只串行化同一回合的引用/状态发布，不持锁等待远端只读I/O。
        async with (publication_lock if publication_lock is not None else nullcontext()):
            outcome = await self._materialize_refs(
                projected, context=context,
                preserve_native=snapshot_only,
            )
            await self._commit_updates(context.lease, outcome.state_updates)
        return PipelineResult(
            tool=tool,
            arguments=normalized,
            outcome=outcome,
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            task_patch=patch,
        )


    async def _prepare_effect(self, tool, raw_arguments, normalized, resolved, context, started, *,
                              prepare_only=False, confirmation_deadline=None) -> PipelineResult:
        if tool.prepare is None:  # pragma: no cover - ToolSpec 已校验
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
                                  elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
                                  recovered_failures=error_repair.completed(tool, raw_arguments, context, outcome.public_content))
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
        prepared_value = replace(prepared_value, metadata={
            **prepared_value.metadata,
            "confirmation_input": normalized,
            "editing": confirmation_editing(tool, normalized),
            "trust_eligible": trust_scope is not None,
            "trust_scope": trust_scope,
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
                error_repair.bind_plan(tool, raw_arguments, context, plan.plan_id)
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
                    await self._discard_frozen_effect(plan=plan, prepared=prepared_value, context=context)
                raise


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
                return await self.execute(tool.name, {**original, **patch}, context=context,
                                          _confirmation_deadline=plan.expires_at)
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
                tool = self.catalog.get(plan.tool_name).for_arguments(plan.arguments)
            except KeyError as exc:
                raise ToolPipelineError(
                    "确认计划对应工具不存在", code="tool_not_found"
                ) from exc
            if tool.effect is ToolEffect.READ or tool.effect is not plan.effect:
                raise ToolPipelineError(
                    "确认计划风险类型不匹配", code="confirmation_invalid"
                )
            if tool.execute_confirmed is None:  # pragma: no cover - ToolSpec 已校验
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
            recovered_failures=error_repair.completed(tool, plan.arguments, context, outcome.public_content,
                                                    plan_id=plan.plan_id),
        )

    async def _run_effect(self, plan: EffectPlan, *, context: ToolCallContext):
        try:
            tool = self.catalog.get(plan.tool_name).for_arguments(plan.arguments)
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

    async def _pending_plan(self, arguments: dict[str, Any], context: ToolCallContext) -> dict[str, Any]:
        with session_scope_guard(context.owner, context.session_id, kind="effect"):
            if not await self.state_store.is_current(context.lease):
                raise StalePublicationError("turn lost publication authority")
            state = await self.state_store.load(owner=context.owner, session_id=context.session_id)
            plan = await self.get_pending_plan(owner=context.owner, session_id=context.session_id)
            if arguments["operation"] == "cancel" or plan is None:
                cancelled = bool(state.pending_effect_plan_id) and await self._cancel_effect_scoped(
                    state.pending_effect_plan_id, lease=context.lease)
                return {"ok": True, "status": "completed", "summary": "已取消原确认卡，未执行业务操作" if cancelled else "当前没有有效的待确认计划",
                        "data": {"pending": False, "cancelled": cancelled}}
            return {"ok": True, "status": "completed", "summary": "原确认卡仍有效，尚未执行",
                    "data": {"pending": True, "preview": plan.public_result,
                             "confirmation": plan.public_dict().get("confirmation", {})}}

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
                await self.selection.authorize(tool, arguments, resolved, context, self.reference_store)
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
            public.update(result_handle=handle, read_tool="agent.read_result")
            try:
                payload = decode_tool_content(compact_tool_content(model_content, maximum=self.projector.max_model_chars, trusted=not bool(outcome.full_model_content)))
                payload.update(result_handle=handle, read_tool="agent.read_result")
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
