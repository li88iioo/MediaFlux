"""资源展示端口：连接原生SDK观察、会话发布与原Web/TG选择。"""
from __future__ import annotations

import asyncio
import json
import uuid
from copy import deepcopy

from app.agent.domains.resources.selection import (
    CANDIDATE_VIEW_KEY, RESOURCE_KIND, issue_candidate_view, resource_download_arguments,
)
from app.agent.kernel.state_types import StateUpdate
from app.agent.kernel.references import ReferenceError, ResultSnapshotReader
from app.modules.resource_candidates import capture_candidates, merge_candidates, presentation_snapshot, reranked_candidates


class ResourcePresentation:
    """宿主绑定实际SDK搜索观察；模型只提交真实result_ids。"""

    def __init__(self, context, effects, snapshots: ResultSnapshotReader):
        self.context = context
        self.effects = effects
        self.snapshots = snapshots
        self.lock = asyncio.Lock()
        self.operation_id: str | None = None
        self.changed = False
        self.view = None

    async def _state(self):
        self.context.cancellation.raise_if_cancelled()
        return await self.effects.state_store.load(owner=self.context.owner, session_id=self.context.session_id)

    async def _commit(self, *updates):
        self.context.cancellation.raise_if_cancelled()
        await self.effects.state_store.commit(self.context.lease, updates=updates)

    async def begin(self, method):
        if method not in {"resources.search", "resources.search_missing", "resources.present"}:
            return None
        async with self.lock:
            self.operation_id = uuid.uuid4().hex
            if method != "resources.present":
                await self._commit(StateUpdate(f"metadata.{CANDIDATE_VIEW_KEY}", None))
                self.view, self.changed = None, True
                await self.context.report_progress({"tool": method, "candidate_view": None})
            return self.operation_id

    async def snapshot(self):
        state = await self._state()
        scope = state.metadata.get("resource_candidate_scope") or {}
        handle = scope.get("result_handle")
        if not handle:
            return None
        # 完整批次复用现有结果分块存储；50项卡片容量不截断搜索事实。
        store = self.effects.reference_store
        kwargs = {"owner": self.context.owner, "session_id": self.context.session_id}
        manifest = await store.resolve(handle, **kwargs, expected_kind="tool_result")
        snapshot, cached = await self.snapshots.read(handle, manifest, **kwargs)
        return deepcopy(snapshot) if cached else snapshot

    async def observed(self, method, arguments, result, *, operation_id: str | None = None, snapshot=None):
        if method in {"resources.search", "resources.search_missing", "resources.rank_episode"}:
            from app.indexers.runtime import get_indexer_service
            async with self.lock:
                try:
                    previous = await self.snapshot()
                except ReferenceError:
                    previous = None
                if method == "resources.rank_episode":
                    captured = reranked_candidates(previous, result)
                    if captured is None:
                        return
                else:
                    assert operation_id is not None
                    captured = await asyncio.to_thread(capture_candidates, result, arguments, get_indexer_service(), "rs_" + operation_id)
                captured = merge_candidates(previous, captured)
                handle = await self.effects.save_result(json.dumps(captured, ensure_ascii=False), self.context)
                await self._commit(StateUpdate("metadata.resource_candidate_scope", {"result_handle": handle}))
                # 搜索开始时已经撤下旧卡；把实际展示状态一并返回，避免调用方
                # 将仍可复用的候选快照误认为仍在页面展示的候选。
                state = await self._state()
                view = state.metadata.get(CANDIDATE_VIEW_KEY) or {}
                result["presentation"] = {
                    "result_ids": [item["result_id"] for item in view.get("items", [])],
                }
        elif method == "resources.present":
            async with self.lock:
                # 同一工作区允许并发 SDK 调用，展示顺序取请求顺序而非完成顺序。
                if operation_id != self.operation_id:
                    result.update(presented=False, status="superseded", summary="已有后续查询或展示，本次候选未发布。")
                    return
                if result["result_ids"]:
                    assert snapshot is not None
                    selected = presentation_snapshot(snapshot, result)
                    reference = await self.effects._put_reference(self.context, kind=RESOURCE_KIND, value=selected, ttl_seconds=900)
                    view = await issue_candidate_view(store=self.effects.reference_store,
                        owner=self.context.owner, session_id=self.context.session_id, generation=self.context.lease.generation,
                        ref=reference.ref, items=selected["_display_items"], ttl_seconds=900, turn_id=self.context.turn_id,
                        selected_positions=[item["position"] for item in selected["candidates"] if item["result_id"] in result["result_ids"]])
                    if view and result["coverage_notice"]:
                        view["coverage_notice"] = result["coverage_notice"]
                else:
                    view = None
                await self._commit(StateUpdate(f"metadata.{CANDIDATE_VIEW_KEY}", view))
                self.view, self.changed = view, True
                result["presented"] = view is not None
                await self.context.report_progress({"tool": method, "candidate_view":
                    {key: value for key, value in view.items() if key != "generation"} if view else None})


class ResourceSelection:
    """资源候选选择、冻结计划和执行回执的领域绑定。"""

    async def admit(self, agent_input, *, state_store, reference_store, catalog):
        from app.agent.domains.resources.selection import validate_selection
        from app.agent.errors import ToolPipelineError
        from app.agent.kernel.capabilities import ToolEffect
        from app.agent.kernel.state_types import SelectionInvalidError

        selection = candidate = tool = None
        if "selection" in agent_input.metadata:
            state = await state_store.load(owner=agent_input.owner, session_id=agent_input.session_id)
            selection = await validate_selection(agent_input.metadata["selection"], state=state, store=reference_store)
            try:
                tool = catalog.get("resources.download")
                if tool.effect is ToolEffect.READ:
                    raise KeyError("selection must require confirmation")
            except KeyError as exc:
                raise ToolPipelineError("候选预览能力暂不可用", code="selection_unavailable") from exc
        if agent_input.metadata.get("candidate_context") and selection is None:
            state = await state_store.load(owner=agent_input.owner, session_id=agent_input.session_id)
            items = (state.metadata.get(CANDIDATE_VIEW_KEY) or {}).get("items") or []
            if not items:
                raise SelectionInvalidError()
            candidate = await validate_selection({"ref": agent_input.metadata["candidate_context"],
                "positions": [items[0]["position"]], "target": "guangya"},
                state=state, store=reference_store, for_preview=False)
        return selection, candidate, tool

    def validate_call(self, tool, arguments, context):
        from app.agent.errors import ToolPipelineError
        from app.agent.kernel.capabilities import ToolEffect

        if context.selection_arguments is None:
            return
        actual = {**arguments, "target": arguments.get("target", "preferred")}
        if (tool.name != "resources.download" or tool.effect is ToolEffect.READ
                or actual != dict(context.selection_arguments)):
            raise ToolPipelineError("只能预览本次已验证的候选选择", code="selection_mismatch")

    async def authorize(self, tool, arguments, resolved, context, reference_store, state_store):
        from app.modules.resource_candidates import restore_resource_candidate_reference
        from app.agent.errors import ToolPipelineError

        if tool.name != "resources.download" or context.active_effect_plan is not None:
            return
        state = await state_store.load(owner=context.owner, session_id=context.session_id)
        view = state.metadata.get(CANDIDATE_VIEW_KEY) or {}
        required = bool(context.resource_candidate_ref or context.selection_arguments is not None)
        reference = context.resource_candidate_ref or view.get("ref")
        if not reference:
            return
        from app.agent.kernel.references import ReferenceError
        try:
            bound = await reference_store.resolve(reference, owner=context.owner, session_id=context.session_id,
                                                  expected_kind=RESOURCE_KIND)
        except ReferenceError:
            if required:
                raise
            return
        candidates = bound.get("candidates", []) if isinstance(bound, dict) else []
        by_id = {item["result_id"]: item for item in candidates}
        identities = arguments["result_ids"]
        if not set(identities).issubset(by_id):
            if required:
                raise ToolPipelineError("请从回复消息所属的候选批次选择，不可替换为其他资源", code="candidate_scope_mismatch")
            return
        snapshot = await asyncio.to_thread(restore_resource_candidate_reference, bound)
        if snapshot is None:
            raise ToolPipelineError("候选快照已失效，请重新查询", code="reference_invalid")
        positions = [by_id[identity]["position"] for identity in identities]
        goals = resource_download_arguments(snapshot, positions).get("verification_targets")
        if goals and not resolved.get("verification_targets"):
            resolved["verification_targets"] = goals
        resolved["_resource_selection"] = {
            "ref": reference, "positions": dict(zip(identities, positions)),
        }

    def plan_updates(self, tool, arguments, plan):
        binding = plan.metadata.get("resource_selection")
        if tool.name == "resources.download" and binding:
            return (StateUpdate("metadata.ux_candidate_plan", {
                "plan_id": plan.plan_id, "ref": binding["ref"],
            }),)
        return ()

    def confirmed_result(self, result, public_result):
        from app.agent.public_view import format_public_result

        if not result or result.tool.name != "resources.download" or result.effect_plan is None:
            return None
        binding = result.effect_plan.metadata.get("resource_selection")
        if not binding:
            return None
        data = public_result.get("data")
        items = data.get("items", []) if isinstance(data, dict) else []
        return {"ref": binding["ref"], "text": format_public_result(public_result),
                "target": result.arguments.get("target"),
                "handled_positions": [binding["positions"][item["result_id"]] for item in items
                    if item.get("result_id") in binding["positions"] and item.get("status") != "failed"]}
