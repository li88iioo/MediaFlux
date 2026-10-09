"""按 owner 绑定候选序号提交资源；内部 result_id 不再成为 Agent 工具参数。"""

from __future__ import annotations

import re
from typing import Any

from app.agent.errors import AgentToolError
from app.agent.indexer_actions import (
    prepare_submit_resource,
    prepare_submit_resource_batch,
    submit_resource_batch_confirmed,
    submit_resource_confirmed,
)
from app.agent.models import ToolContext, ToolReference, ToolResult
from app.agent.public_safety import sanitize_resource_title
from app.agent.recent_resource_candidates import (
    RecentResourceCandidateStore,
    normalize_resource_search_id,
    restore_resource_candidate_reference,
    validate_safe_resource_snapshot,
)
from app.agent.state_commit import active_agent_resource_candidates

_RESOURCE_CANDIDATES_REF_RE = re.compile(r"^ref_[A-Za-z0-9_-]{16,160}$")


def present_candidates_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """校验显式候选展示请求；引用解析由 Kernel 在 handler 前完成。"""
    if not isinstance(arguments, dict):
        raise AgentToolError("资源候选筛选参数必须是对象")
    if set(arguments) != {"resource_candidates_ref", "positions"}:
        raise AgentToolError("资源候选筛选只接受 resource_candidates_ref 和 positions")
    reference = arguments.get("resource_candidates_ref")
    if (
        not isinstance(reference, str)
        or not _RESOURCE_CANDIDATES_REF_RE.fullmatch(reference.strip())
    ):
        raise AgentToolError("resource_candidates_ref 不是有效的资源候选引用")
    positions = arguments.get("positions")
    if (
        not isinstance(positions, list)
        or len(positions) > 12
        or any(type(position) is not int or not 1 <= position <= 12 for position in positions)
        or len(set(positions)) != len(positions)
    ):
        raise AgentToolError("positions 必须是 0 到 12 个不重复的 1 到 12 整数")
    return {
        "resource_candidates_ref": reference.strip(),
        "positions": list(positions),
    }


def present_candidates(arguments: dict[str, Any]) -> ToolResult:
    """只展示已解析的候选快照，不恢复 Provider、不读取最近候选、不发起搜索。"""
    value = arguments.get("resource_candidates")
    if not isinstance(value, dict):
        raise AgentToolError("资源候选引用无效或已过期", code="confirmation_stale")
    public_snapshot = {
        key: value.get(key)
        for key in ("search_id", "search_status", "candidates")
    }
    snapshot = validate_safe_resource_snapshot(public_snapshot)
    if snapshot is None:
        raise AgentToolError("资源候选引用无效或已过期", code="confirmation_stale")
    positions = list(arguments.get("positions") or [])
    candidate_count = len(snapshot["candidates"])
    if any(position > candidate_count for position in positions):
        raise AgentToolError(
            "positions 中包含当前候选快照不存在的序号",
            code="precondition_failed",
        )
    return ToolResult(
        True,
        "found" if positions else "empty",
        (
            f"已筛选{len(positions)}项资源候选"
            if positions
            else "本轮不展示资源候选"
        ),
        data={"positions": positions},
        references=[ToolReference("resource_candidates", value)],
    )


def _candidate_result(result: ToolResult, candidates: list[dict[str, Any]], positions: list[int], target: str) -> ToolResult:
    data = dict(result.data) if isinstance(result.data, dict) else {}
    raw_items = data.get("items")
    items = raw_items if isinstance(raw_items, list) else [dict(data)] if len(candidates) == 1 else []
    public_items = []
    for index, (candidate, position) in enumerate(zip(candidates, positions)):
        item = dict(items[index]) if index < len(items) and isinstance(items[index], dict) else {}
        item.update({
            "position": position, "title": sanitize_resource_title(candidate.get("title"), limit=160),
            "target": target,
        })
        # 没有可信逐项目标状态时不得臆造“失败可重试”或成功。
        item.setdefault("status", "manual_review")
        public_items.append(item)
    data.update({"target": target, "items": public_items})
    result.data = data
    return result


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
        provided_value = arguments.get("resource_candidates")
        if provided_value is not None:
            snapshot = restore_resource_candidate_reference(provided_value)
            if snapshot is None:
                raise AgentToolError(
                    "资源候选引用无效或已过期",
                    code="confirmation_stale"
                    if require_search_id
                    else "precondition_failed",
                )
            snapshot_search_id = normalize_resource_search_id(snapshot.get("search_id"))
            if search_id and search_id != snapshot_search_id:
                raise AgentToolError(
                    "资源候选引用与搜索快照不匹配",
                    code="confirmation_stale"
                    if require_search_id
                    else "precondition_failed",
                )
            arguments["search_id"] = snapshot_search_id
            return snapshot

        if require_search_id and not search_id:
            raise AgentToolError(
                "资源确认缺少已冻结的搜索快照，请重新选择",
                code="confirmation_stale",
            )

        staged = active_agent_resource_candidates(owner=owner)
        if (
            context.session_id
            and not search_id
            and not isinstance(staged, dict)
        ):
            raise AgentToolError(
                "当前会话缺少资源候选引用，请使用搜索结果返回的引用",
                code="precondition_failed",
            )
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
        self,
        context: ToolContext,
        *,
        search_id: str = "",
        resource_candidates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """返回 staged 优先或精确 search_id 绑定的安全快照。"""
        arguments: dict[str, Any] = {}
        if search_id:
            arguments["search_id"] = search_id
        if resource_candidates is not None:
            arguments["resource_candidates"] = resource_candidates
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
                f"该快照只有 {len(candidates)} 项可提交候选，序号 {position} 不存在；"
                "请使用 candidate_numbers 的 position，集号和排序名次不是提交序号。",
                code="precondition_failed"
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
                    f"该快照只有 {len(candidates)} 项可提交候选，序号 {position} 不存在；"
                    "请使用 candidate_numbers 的 position，集号和排序名次不是提交序号。",
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
        internal, candidate = self._resolve_one(arguments, context)
        result, confirmation_context = prepare_submit_resource(internal)
        if isinstance(result.data, dict):
            resource = result.data.get("resource")
            if isinstance(resource, dict):
                resource.pop("result_id", None)
                resource["position"] = int(arguments["position"])
        verification = candidate.get("_verification_context")
        if isinstance(verification, dict):
            result.effect_metadata["missing_media_candidates"] = [
                {
                    "verification": verification,
                    "candidate_title": str(candidate.get("title") or "")[:300],
                    "target": str(arguments["target"]),
                }
            ]
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
        return _candidate_result(
            submit_resource_confirmed(internal, expected_context), [candidate],
            [arguments["position"]], arguments["target"],
        )

    def prepare_batch(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> tuple[ToolResult, str]:
        internal, candidates = self._resolve_batch(arguments, context)
        result, confirmation_context = prepare_submit_resource_batch(internal)
        if isinstance(result.data, dict):
            resources = result.data.get("resources")
            if isinstance(resources, list):
                for public_position, resource in zip(arguments["positions"], resources):
                    if isinstance(resource, dict):
                        resource["position"] = int(public_position)
        effect_candidates = []
        for candidate in candidates:
            verification = candidate.get("_verification_context")
            effect_candidates.append(
                {
                    "verification": (
                        verification if isinstance(verification, dict) else None
                    ),
                    "candidate_title": str(candidate.get("title") or "")[:300],
                    "target": str(arguments["target"]),
                }
            )
        if any(
            isinstance(item.get("verification"), dict)
            for item in effect_candidates
        ):
            result.effect_metadata["missing_media_candidates"] = effect_candidates
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
        return _candidate_result(
            submit_resource_batch_confirmed(internal, expected_context), candidates,
            arguments["positions"], arguments["target"],
        )
