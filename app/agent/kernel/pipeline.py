"""普通工具读取、参数归一与共用副作用边界的分发。"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from app.agent.errors import ToolPipelineError

from .capabilities import ToolCatalog, ToolEffect
from .effects import (
    AuthorizationPolicy,
    EffectExecutor,
    EffectLifecycle,
    EffectPlanStore,
    RateLimiter,
)
from .invocation import invoke
from .projection import DefaultProjector, ToolOutcome
from .references import ReferenceStore
from .state_ports import SessionStateStore
from .task_state import TaskPatch, observation_patch, read_observations
from .tool_calls import PipelineResult, ToolCallContext


class ToolPipeline(EffectExecutor):
    """在唯一 EffectExecutor 上增加 READ 工具；写入没有另一套实现。"""

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
        super().__init__(
            catalog=catalog, state_store=state_store, reference_store=reference_store,
            effect_store=effect_store, projector=projector, authorization=authorization,
            rate_limiter=rate_limiter, effect_lifecycle=effect_lifecycle,
        )
    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any] | None,
        *,
        context: ToolCallContext,
        _returned_read=None,
        _read_admitted: asyncio.Event | None = None,
        _read_publication_lock: asyncio.Lock | None = None,
    ) -> PipelineResult:
        started = time.monotonic()
        tool, raw_arguments, normalized = self._normalize_call(name, arguments, context)
        if tool.effect is not ToolEffect.READ:
            return await self._prepare_effect(tool, raw_arguments, normalized, context, started)
        async with (_read_publication_lock if _read_publication_lock is not None else nullcontext()):
            resolved = await self._authorize(tool, normalized, context)
            await self._admit(tool, resolved, context)
        return await self._execute_read(tool, normalized, resolved, context, started,
            returned_read=_returned_read, admitted=_read_admitted, publication_lock=_read_publication_lock)

    async def _execute_read(self, tool, normalized, resolved, context, started, *,
                            returned_read=None, admitted=None, publication_lock=None) -> PipelineResult:
        if tool.read is None:  # pragma: no cover - KernelToolSpec 已校验
            raise ToolPipelineError("工具不可执行", code="tool_not_executable")
        await self.projector.before_read(tool, context, self.state_store)
        if admitted is not None:
            admitted.set()
        deadline = asyncio.timeout_at((context.continuation_policy or {}).get("deadline"))
        try:
            async with deadline:
                value = await invoke(tool.read, resolved, context)
        except TimeoutError as exc:
            if not deadline.expired():
                raise
            raise ToolPipelineError("活跃执行时间额度已用完。", code="active_time_budget_exceeded") from exc
        except ToolPipelineError as exc:
            if tool.observe is not None:
                exc.task_patch = observation_patch(context.current_task, owner=context.owner,
                    session_id=context.session_id, tool=tool.name, call_id=context.call_id,
                    observations=tool.observe(None, normalized))
            raise
        # 组合只读工具复用同一执行结果，证据仍由外层原有回执事务提交。
        patch = value.task_patch if isinstance(value, PipelineResult) else value if isinstance(value, TaskPatch) else None
        plan = value.effect_plan if isinstance(value, PipelineResult) else None
        if isinstance(value, PipelineResult):
            value = value.outcome
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
        # 原生终端已有会话内输出文件；不再复制到业务结果快照和 reader 通道。
        # Later Session/history pressure can reduce a business result to 400 chars.
        # Freeze this already-safe source before that loss, using the same
        # scoped TTL store. Native workspace reads use their existing full-result files.
        snapshot_only = tool.name != "agent.run_code" and not projected.full_model_content and len(projected.model_content) > 400
        if snapshot_only:
            projected = replace(projected, full_model_content=projected.model_content)
        try:
            # 只串行化同一回合的引用/状态发布，不持锁等待远端只读I/O。
            async with (publication_lock if publication_lock is not None else nullcontext()):
                outcome = await self._materialize_refs(
                    projected, context=context,
                    preserve_native=snapshot_only,
                )
                await self._commit_updates(context.lease, outcome.state_updates)
        except BaseException:
            if plan is not None:
                await self.discard_unpublished_effect(plan=plan, context=context)
            raise
        return PipelineResult(
            tool=tool,
            arguments=normalized,
            outcome=outcome,
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            task_patch=patch,
            effect_plan=plan,
        )
