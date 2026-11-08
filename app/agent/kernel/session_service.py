"""宿主绑定的原生会话服务；复用结果、票据控制与持久任务发布事务。"""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

from app.agent.errors import ToolPipelineError

from . import telegram_reference
from .references import ReferenceError, ResultSnapshotReader
from .session_guard import session_scope_guard
from .state_types import StalePublicationError
from .task_state import proposal, task_projection

if TYPE_CHECKING:
    from .effects import EffectExecutor
    from .tool_calls import ToolCallContext


class SessionService:
    def __init__(self, executor: EffectExecutor, context: ToolCallContext):
        self.executor = executor
        self.context = context
        self.snapshots = ResultSnapshotReader(executor.reference_store)

    async def result(self, handle: str, path: str = "") -> dict:
        """返回原节点全文；范围身份只来自宿主上下文，旧引用沿原存储与TTL校验。"""
        if not isinstance(handle, str) or not handle or len(handle) > 200:
            raise ToolPipelineError("handle 必须是已保存结果的真实引用", code="invalid_arguments")
        if not isinstance(path, str) or path and not path.startswith("/") or re.search(r"~(?![01])", path):
            raise ToolPipelineError("path 必须是原快照的 JSON Pointer；空串读取全文", code="invalid_arguments")
        self.context.cancellation.raise_if_cancelled()
        scope = {"owner": self.context.owner, "session_id": self.context.session_id}
        refs = self.executor.reference_store
        reference = None
        copy_result = False
        try:
            try:
                manifest = await refs.resolve(handle, **scope, expected_kind="tool_result")
            except ReferenceError:
                reference = await telegram_reference.resolve(handle, **scope, reference_store=refs,
                                                               state_store=self.executor.state_store)
                if path not in ("", "/text"):
                    raise ToolPipelineError("引用正文仅支持根节点或 /text", code="invalid_arguments")
                node = {"text": reference["text"]}
            else:
                node, copy_result = await self.snapshots.read(handle, manifest, **scope)
        except (ReferenceError, TypeError, ValueError) as exc:
            raise ToolPipelineError("结果快照已过期、不完整或不属于当前会话；未重新查询原服务。", code="reference_invalid") from exc
        try:
            for part in path.split("/")[1:]:
                if isinstance(node, list) and not re.fullmatch(r"0|[1-9][0-9]*", part):
                    raise KeyError(part)
                key = part.replace("~1", "/").replace("~0", "~")
                node = node[int(key)] if isinstance(node, list) else node[key]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ToolPipelineError("原快照不存在该 JSON Pointer；可读取根节点核对路径。", code="invalid_arguments") from exc
        self.context.cancellation.raise_if_cancelled()
        result = {"ok": True, "status": "snapshot", "result_handle": handle, "path": path,
                  "source_kind": "telegram_reference" if reference is not None else "tool_result",
                  "data": deepcopy(node) if copy_result else node}
        if reference is not None:
            result.update(untrusted_reference=True, source=reference["source"])
        return result

    async def pending_plan(self, operation: str = "inspect") -> dict:
        """沿既有票据取消链管理当前会话未执行的卡；不执行业务。"""
        if operation not in ("inspect", "cancel") or not isinstance(operation, str):
            raise ToolPipelineError("operation 只支持 inspect 或 cancel", code="invalid_arguments")
        context = self.context
        context.cancellation.raise_if_cancelled()
        with session_scope_guard(context.owner, context.session_id, kind="effect"):
            if not await self.executor.state_store.is_current(context.lease):
                raise StalePublicationError("turn lost publication authority")
            state = await self.executor.state_store.load(owner=context.owner, session_id=context.session_id)
            plan = await self.executor.get_pending_plan(owner=context.owner, session_id=context.session_id)
            context.cancellation.raise_if_cancelled()
            if operation == "cancel" or plan is None:
                cancelled = bool(state.pending_effect_plan_id) and await self.executor._cancel_effect_scoped(
                    state.pending_effect_plan_id, lease=context.lease)
                return {"ok": True, "status": "completed", "summary": "已取消原确认卡，未执行业务操作" if cancelled else "当前没有有效的待确认计划",
                        "data": {"pending": False, "cancelled": cancelled}}
            return {"ok": True, "status": "completed", "summary": "原确认卡仍有效，尚未执行",
                    "data": {"pending": True, "preview": plan.public_result,
                             "confirmation": plan.public_dict().get("confirmation", {})}}

    async def _task(self, arguments: dict | None) -> dict:
        context = self.context
        context.cancellation.raise_if_cancelled()
        control = context.task_control
        if control is None:
            raise ToolPipelineError("当前执行未绑定持久任务运行时", code="task_runtime_unavailable")
        task = await control(None)
        context = replace(context, current_task=task)
        try:
            patch = proposal(arguments, context) if arguments is not None else None
        except ValueError as exc:
            raise ToolPipelineError(str(exc), code="invalid_arguments") from exc
        if patch is not None:
            task = await control(patch)
        self.context = replace(context, current_task=task)
        return {"ok": True, "status": task["status"] if task else "empty",
                "summary": "当前持久验收任务" if task else "当前没有持久验收任务；普通计划可保存在workspace笔记中",
                "data": {"task": task_projection(task)}}

    async def task(self) -> dict:
        return await self._task(None)

    async def start_task(self, root_goal: str, constraints: list[str], obligations: list[dict], *, source: str) -> dict:
        return await self._task({"operation": "create", "root_goal": root_goal, "constraints": constraints, "obligations": obligations, "source": source})

    async def revise_task(self, root_goal: str, constraints: list[str], obligations: list[dict], *, source: str) -> dict:
        return await self._task({"operation": "revise", "root_goal": root_goal, "constraints": constraints, "obligations": obligations, "source": source})

    async def checkpoint_task(self, evidence_ids: list[str], blockers: list[str], *, read_evidence: dict[str, str] | None = None) -> dict:
        return await self._task({"operation": "checkpoint", "evidence_ids": evidence_ids, "blockers": blockers,
                                 **({"read_evidence": read_evidence} if read_evidence is not None else {})})

    async def cancel_task(self, source: str) -> dict:
        return await self._task({"operation": "cancel", "source": source})
