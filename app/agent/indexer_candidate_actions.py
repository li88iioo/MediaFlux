"""按 owner 绑定候选序号提交资源；内部 result_id 不再成为 Agent 工具参数。"""
from __future__ import annotations

from typing import Any

from app.agent.indexer_actions import (
    prepare_submit_resource,
    prepare_submit_resource_batch,
    submit_resource_batch_confirmed,
    submit_resource_confirmed,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.recent_resource_candidates import RecentResourceCandidateStore
from app.agent.registry import AgentToolError
from app.agent.state_commit import active_agent_resource_candidates



class IndexerCandidateActions:
    """把公开候选序号解析为 owner 绑定的短期内部句柄。"""

    def __init__(self, store: RecentResourceCandidateStore) -> None:
        self.store = store

    def _candidates(self, context: ToolContext) -> list[dict[str, Any]]:
        owner = str(context.owner or "").strip()
        if not owner:
            raise AgentToolError("请先登录后搜索资源", code="precondition_failed")
        snapshot = self.store.get(owner=owner)
        if snapshot is None:
            snapshot = active_agent_resource_candidates(owner=owner)
        candidates = snapshot.get("candidates") if isinstance(snapshot, dict) else None
        if not isinstance(candidates, list) or not candidates:
            raise AgentToolError(
                "最近资源候选不存在或已过期，请重新搜索",
                code="precondition_failed",
            )
        return [item for item in candidates if isinstance(item, dict)]

    def _resolve_one(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> tuple[dict[str, str], dict[str, Any]]:
        candidates = self._candidates(context)
        position = int(arguments["position"])
        if position > len(candidates):
            raise AgentToolError("最近资源候选中没有这个序号", code="precondition_failed")
        candidate = candidates[position - 1]
        result_id = str(candidate.get("result_id") or "").strip()
        if not result_id:
            raise AgentToolError("资源候选已过期，请重新搜索", code="precondition_failed")
        return {"result_id": result_id, "target": str(arguments["target"])}, candidate

    def _resolve_batch(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        candidates = self._candidates(context)
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
                raise AgentToolError("部分资源候选已过期，请重新搜索", code="precondition_failed")
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
        internal, candidate = self._resolve_one(arguments, context)
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
        internal, candidates = self._resolve_batch(arguments, context)
        result = submit_resource_batch_confirmed(internal, expected_context)
        if isinstance(result.data, dict):
            result.data["_verification_contexts"] = [
                candidate.get("_verification_context")
                if isinstance(candidate.get("_verification_context"), dict)
                else None
                for candidate in candidates
            ]
        return result
