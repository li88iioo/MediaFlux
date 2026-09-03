"""按 owner 绑定候选序号提交资源；内部 result_id 不再成为 Agent 工具参数。"""

from __future__ import annotations

from typing import Any

from app.agent.errors import AgentToolError
from app.agent.indexer_actions import (
    prepare_submit_resource,
    prepare_submit_resource_batch,
    submit_resource_batch_confirmed,
    submit_resource_confirmed,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.recent_resource_candidates import (
    RecentResourceCandidateStore,
    normalize_resource_search_id,
)
from app.agent.state_commit import active_agent_resource_candidates


class IndexerCandidateActions:
    """把公开候选序号解析为 owner 绑定的短期内部句柄。"""

    def __init__(self, store: RecentResourceCandidateStore) -> None:
        self.store = store

    def _snapshot(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        require_search_id: bool = False,
    ) -> dict[str, Any]:
        owner = str(context.owner or "").strip()
        if not owner:
            raise AgentToolError("请先登录后搜索资源", code="precondition_failed")

        search_id = normalize_resource_search_id(arguments.get("search_id"))
        if require_search_id and not search_id:
            raise AgentToolError(
                "资源确认缺少已冻结的搜索快照，请重新选择",
                code="confirmation_stale",
            )

        staged = active_agent_resource_candidates(owner=owner)
        if search_id:
            staged_search_id = (
                normalize_resource_search_id(staged.get("search_id"))
                if isinstance(staged, dict)
                else ""
            )
            snapshot = (
                staged
                if staged_search_id == search_id
                else self.store.get(owner=owner, search_id=search_id)
            )
            if snapshot is None:
                raise AgentToolError(
                    "资源搜索快照不存在或已过期，请重新搜索",
                    code="confirmation_stale",
                )
        else:
            # 同一请求内刚产生的候选必须覆盖旧的跨请求 latest；只有没有 staged
            # 结果时，才读取持久化的最近快照。
            snapshot = (
                staged if isinstance(staged, dict) else self.store.get(owner=owner)
            )
            if snapshot is None:
                raise AgentToolError(
                    "最近资源候选不存在或已过期，请重新搜索",
                    code="precondition_failed",
                )
            search_id = normalize_resource_search_id(snapshot.get("search_id"))
            if not search_id:
                raise AgentToolError(
                    "资源搜索快照无法安全绑定，请重新搜索",
                    code="precondition_failed",
                )
            arguments["search_id"] = search_id

        return snapshot

    def current_snapshot(
        self, context: ToolContext, *, search_id: str = ""
    ) -> dict[str, Any]:
        """返回 staged 优先或精确 search_id 绑定的安全快照。"""
        arguments: dict[str, Any] = {}
        if search_id:
            arguments["search_id"] = search_id
        return self._snapshot(arguments, context)

    def _candidates(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        require_search_id: bool = False,
    ) -> list[dict[str, Any]]:
        snapshot = self._snapshot(
            arguments,
            context,
            require_search_id=require_search_id,
        )
        candidates = snapshot.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise AgentToolError(
                "最近资源候选不存在或已过期，请重新搜索",
                code=(
                    "confirmation_stale"
                    if normalize_resource_search_id(arguments.get("search_id"))
                    else "precondition_failed"
                ),
            )
        return [item for item in candidates if isinstance(item, dict)]

    def _resolve_one(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        require_search_id: bool = False,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        candidates = self._candidates(
            arguments,
            context,
            require_search_id=require_search_id,
        )
        position = int(arguments["position"])
        if position > len(candidates):
            raise AgentToolError(
                "最近资源候选中没有这个序号", code="precondition_failed"
            )
        candidate = candidates[position - 1]
        result_id = str(candidate.get("result_id") or "").strip()
        if not result_id:
            raise AgentToolError(
                "资源候选已过期，请重新搜索", code="precondition_failed"
            )
        return {"result_id": result_id, "target": str(arguments["target"])}, candidate

    def _resolve_batch(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        require_search_id: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        candidates = self._candidates(
            arguments,
            context,
            require_search_id=require_search_id,
        )
        selected: list[dict[str, Any]] = []
        result_ids: list[str] = []
        for position in arguments["positions"]:
            if int(position) > len(candidates):
                raise AgentToolError(
                    f"最近资源候选中没有第 {position} 项",
                    code="precondition_failed",
                )
            candidate = candidates[int(position) - 1]
            result_id = str(candidate.get("result_id") or "").strip()
            if not result_id:
                raise AgentToolError(
                    "部分资源候选已过期，请重新搜索", code="precondition_failed"
                )
            selected.append(candidate)
            result_ids.append(result_id)
        return {"result_ids": result_ids, "target": str(arguments["target"])}, selected

    def prepare_one(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> tuple[ToolResult, str]:
        internal, _candidate = self._resolve_one(arguments, context)
        result, confirmation_context = prepare_submit_resource(internal)
        if isinstance(result.data, dict):
            resource = result.data.get("resource")
            if isinstance(resource, dict):
                resource.pop("result_id", None)
                resource["position"] = int(arguments["position"])
        return result, confirmation_context

    def confirm_one(
        self,
        arguments: dict[str, Any],
        expected_context: str,
        context: ToolContext,
    ) -> ToolResult:
        internal, candidate = self._resolve_one(
            arguments,
            context,
            require_search_id=True,
        )
        result = submit_resource_confirmed(internal, expected_context)
        if isinstance(result.data, dict):
            verification = candidate.get("_verification_context")
            if isinstance(verification, dict):
                result.data["_verification_context"] = verification
        return result

    def prepare_batch(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> tuple[ToolResult, str]:
        internal, _candidates = self._resolve_batch(arguments, context)
        result, confirmation_context = prepare_submit_resource_batch(internal)
        if isinstance(result.data, dict):
            resources = result.data.get("resources")
            if isinstance(resources, list):
                for public_position, resource in zip(arguments["positions"], resources):
                    if isinstance(resource, dict):
                        resource["position"] = int(public_position)
        return result, confirmation_context

    def confirm_batch(
        self,
        arguments: dict[str, Any],
        expected_context: str,
        context: ToolContext,
    ) -> ToolResult:
        internal, candidates = self._resolve_batch(
            arguments,
            context,
            require_search_id=True,
        )
        result = submit_resource_batch_confirmed(internal, expected_context)
        if isinstance(result.data, dict):
            result.data["_verification_contexts"] = [
                candidate.get("_verification_context")
                if isinstance(candidate.get("_verification_context"), dict)
                else None
                for candidate in candidates
            ]
        return result
