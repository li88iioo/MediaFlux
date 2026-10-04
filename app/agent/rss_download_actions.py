"""Media Agent 的待处理 RSS 条目受控 qB 提交动作。"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from app import database as db
from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.agent.rss_entry_actions import build_rss_submission_result
from app.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_LIMIT = 10
_MAX_ITEMS = 20


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rss_pending_download_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) - {"limit"}:
        raise AgentToolError("rss.submit_pending_to_qb 只接受 limit 参数")
    limit = arguments.get("limit", _DEFAULT_LIMIT)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_ITEMS
    ):
        raise AgentToolError("limit 必须是 1 到 20 的整数")
    return {"limit": limit}


def _row_snapshot(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "rss_item_id": int(row["rss_item_id"]),
        "title": str(row["title"] or ""),
        "payload": str(row["payload"] or ""),
        "created_at": str(row["created_at"] or ""),
        "download_method": str(row["download_method"] or ""),
        "qb_save_path": str(row["qb_save_path"] or ""),
    }


def _capture(arguments: dict[str, Any]) -> dict[str, Any]:
    from app.modules.rss import capture_rss_qb_runtime_config

    runtime_config, config_error = capture_rss_qb_runtime_config()
    limit = arguments["limit"]
    rows = db.get_pending_rss_qb_snapshot(
        default_method=str(runtime_config.get("default_method") or ""),
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    entries = [_row_snapshot(row) for row in rows[:limit]]
    payload = {
        "limit": limit,
        "entries": entries,
        "has_more": has_more,
        "runtime_config": runtime_config,
        "config_error": str(config_error or ""),
    }
    return {
        "limit": limit,
        "entries": entries,
        "has_more": has_more,
        "runtime_config": runtime_config,
        "config_error": str(config_error or ""),
        "fingerprint": confirmation_context_fingerprint(
            payload, domain="rss-submit-pending-to-qb"
        ),
    }


def _preview_rss_pending_download(
    arguments: dict[str, Any], state: dict[str, Any]
) -> ToolResult:
    """只读选择最新 pending qB 条目，不 claim、不访问网络。"""
    count = len(state["entries"])
    if count == 0:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary="当前没有可提交到 qBittorrent 的待处理 RSS 条目",
            error="没有符合 pending 与 qB 目标条件的条目。",
            suggestions=["可先询问：诊断 RSS 订阅状态。"],
        )
    if state["config_error"]:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="qBittorrent 提交配置当前不可用",
            error="请检查 qBittorrent 配置后重新预检。",
            suggestions=["可询问：为什么下载器配置不可用？"],
        )

    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将向 qBittorrent 提交 {count} 个待处理 RSS 条目",
        data={
            "action": "rss.submit_pending_to_qb",
            "target": "qbittorrent",
            "selected_count": count,
            "requested_limit": arguments["limit"],
            "has_more": bool(state["has_more"]),
            "effects": [
                "所选条目将原子认领后按当前确认配置提交到 qBittorrent。",
                "提交成功的条目会标记为已下载；失败条目会标记为失败。",
            ],
            "limits": {"max_items": _MAX_ITEMS, "pending_only": True},
        },
        evidence=[
            Evidence(
                "rss_database",
                "只读核对本地待处理 RSS 条目；未刷新订阅、未认领条目、未访问下载器。",
                _now(),
            )
        ],
        suggestions=["确认前请核对提交数量；如数量过多，请使用更小的 limit。"],
    )


def prepare_rss_pending_download(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    state = _capture(arguments)
    return _preview_rss_pending_download(arguments, state), str(state["fingerprint"])


def _submit_pending_rss_to_qb_state(state: dict[str, Any]) -> ToolResult:
    entries = list(state.get("entries") or [])
    runtime_config = dict(state.get("runtime_config") or {})
    if not entries or len(entries) > _MAX_ITEMS or not runtime_config.get("url"):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="RSS 提交条件已变化",
            error="请重新预检后再确认。",
        )

    from app.modules.rss import RSSEngine

    raw = RSSEngine().submit_snapshot(
        entries, runtime_config, claim=db.claim_pending_rss_qb_entries,
    )
    result = build_rss_submission_result(
        raw,
        kind="download",
        target="qbittorrent",
        evidence_description=(
            "已按确认时冻结的集合与 qB 配置执行一次有界提交；响应仅包含聚合计数。"
        ),
    )
    logger.info(
        "Agent RSS qB 提交结果 status=%s requested=%s claimed=%s submitted=%s failed=%s",
        result.status,
        result.data["requested"],
        result.data["claimed"],
        result.data["submitted"],
        result.data["failed"],
    )
    return result


def submit_pending_rss_to_qb_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    state = _capture(arguments)
    if not secrets.compare_digest(
        str(state["fingerprint"]), str(expected_context or "")
    ):
        raise AgentToolError(
            "待处理 RSS 条目或 qBittorrent 配置已变化，请重新预检",
            code="confirmation_stale",
        )
    return _submit_pending_rss_to_qb_state(state)
