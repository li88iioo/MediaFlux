"""按 owner 绑定候选序号提交资源；内部 result_id 不再成为 Agent 工具参数。"""
from __future__ import annotations

import secrets
from typing import Any

from app.agent.indexer_actions import (
    preview_submit_resource,
    preview_submit_resource_batch,
    submit_batch_confirmation_context,
    submit_confirmation_context,
    submit_resource,
    submit_resource_batch,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.recent_resource_candidates import RecentResourceCandidateStore
from app.agent.registry import AgentToolError
from app.agent.state_commit import active_agent_resource_candidates

_TARGETS = frozenset({"qb", "guangya", "both"})


def indexer_candidate_submit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"position", "target"}:
        raise AgentToolError("indexer.submit_candidate 只接受 position 和 target 参数")
    position = arguments.get("position")
    if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= 12:
        raise AgentToolError("position 必须是 1 到 12 的整数")
    target = str(arguments.get("target") or "").strip().casefold()
    if target not in _TARGETS:
        raise AgentToolError("target 仅支持 qb、guangya 或 both")
    return {"position": position, "target": target}


def indexer_candidate_batch_submit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != {"positions", "target"}:
        raise AgentToolError("indexer.submit_candidates 只接受 positions 和 target 参数")
    raw_positions = arguments.get("positions")
    if not isinstance(raw_positions, list) or not 2 <= len(raw_positions) <= 12:
        raise AgentToolError("positions 必须包含 2 到 12 个候选序号")
    positions: list[int] = []
    for value in raw_positions:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
            raise AgentToolError("候选序号必须是 1 到 12 的整数")
        if value not in positions:
            positions.append(value)
    if len(positions) < 2:
        raise AgentToolError("批量提交至少需要 2 个不同候选")
    target = str(arguments.get("target") or "").strip().casefold()
    if target not in _TARGETS:
        raise AgentToolError("target 仅支持 qb、guangya 或 both")
    return {"positions": positions, "target": target}


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
        result = preview_submit_resource(internal)
        if isinstance(result.data, dict):
            resource = result.data.get("resource")
            if isinstance(resource, dict):
                resource.pop("result_id", None)
                resource["position"] = int(arguments["position"])
        return result, submit_confirmation_context(internal)

    def confirm_one(
        self,
        arguments: dict[str, Any],
        expected_context: str,
        context: ToolContext,
    ) -> ToolResult:
        internal, candidate = self._resolve_one(arguments, context)
        current_context = submit_confirmation_context(internal)
        if not secrets.compare_digest(current_context, str(expected_context or "")):
            raise AgentToolError("资源候选或下载目标已变化，请重新预检", code="confirmation_stale")
        result = submit_resource(internal)
        if isinstance(result.data, dict):
            verification = candidate.get("_verification_context")
            if isinstance(verification, dict):
                result.data["_verification_context"] = verification
        return result

    def prepare_batch(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> tuple[ToolResult, str]:
        internal, _candidates = self._resolve_batch(arguments, context)
        result = preview_submit_resource_batch(internal)
        if isinstance(result.data, dict):
            resources = result.data.get("resources")
            if isinstance(resources, list):
                for public_position, resource in zip(arguments["positions"], resources):
                    if isinstance(resource, dict):
                        resource["position"] = int(public_position)
        return result, submit_batch_confirmation_context(internal)

    def confirm_batch(
        self,
        arguments: dict[str, Any],
        expected_context: str,
        context: ToolContext,
    ) -> ToolResult:
        internal, candidates = self._resolve_batch(arguments, context)
        current_context = submit_batch_confirmation_context(internal)
        if not secrets.compare_digest(current_context, str(expected_context or "")):
            raise AgentToolError("资源候选或下载目标已变化，请重新预检", code="confirmation_stale")
        result = submit_resource_batch(internal)
        if isinstance(result.data, dict):
            result.data["_verification_contexts"] = [
                candidate.get("_verification_context")
                if isinstance(candidate.get("_verification_context"), dict)
                else None
                for candidate in candidates
            ]
        return result


def confirmation_required(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("资源提交需要用户确认", code="confirmation_required")
