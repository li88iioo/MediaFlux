"""Media Agent 的待处理 RSS 条目受控 qB 提交动作。"""
from __future__ import annotations

from datetime import datetime
import threading
from typing import Any

from app import database as db
from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_LIMIT = 10
_MAX_ITEMS = 20
_CONFIRMATION_STATE = threading.local()


def clear_confirmation_state() -> None:
    _CONFIRMATION_STATE.preview = None
    _CONFIRMATION_STATE.pending = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rss_pending_download_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) - {"limit"}:
        raise AgentToolError("rss.submit_pending_to_qb 只接受 limit 参数")
    limit = arguments.get("limit", _DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_ITEMS:
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


def preview_rss_pending_download(arguments: dict[str, Any]) -> ToolResult:
    """只读选择最新 pending qB 条目，不 claim、不访问网络。"""
    _CONFIRMATION_STATE.preview = None
    state = _capture(arguments)
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

    _CONFIRMATION_STATE.preview = state
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
        evidence=[Evidence(
            "rss_database",
            "只读核对本地待处理 RSS 条目；未刷新订阅、未认领条目、未访问下载器。",
            _now(),
        )],
        suggestions=["确认前请核对提交数量；如数量过多，请使用更小的 limit。"],
    )


def rss_pending_download_confirmation_context(arguments: dict[str, Any]) -> str:
    state = getattr(_CONFIRMATION_STATE, "preview", None)
    _CONFIRMATION_STATE.preview = None
    if not isinstance(state, dict) or state.get("limit") != arguments["limit"]:
        state = _capture(arguments)
    _CONFIRMATION_STATE.pending = state
    return str(state["fingerprint"])


def submit_pending_rss_to_qb(arguments: dict[str, Any]) -> ToolResult:
    state = getattr(_CONFIRMATION_STATE, "pending", None)
    _CONFIRMATION_STATE.pending = None
    if not isinstance(state, dict) or state.get("limit") != arguments["limit"]:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="RSS 提交确认上下文已失效",
            error="请重新预检后再确认。",
        )
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

    raw = RSSEngine().submit_pending_qb_snapshot(entries, runtime_config)
    requested = max(0, int(raw.get("requested") or 0))
    claimed = max(0, int(raw.get("claimed") or 0))
    submitted = max(0, int(raw.get("submitted") or 0))
    failed = max(0, int(raw.get("failed") or 0))
    outcome_unknown = min(failed, max(0, int(raw.get("outcome_unknown") or 0)))
    confirmed_failed = max(0, failed - outcome_unknown)
    if raw.get("conflict") or claimed != requested:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="待处理 RSS 条目已变化，本次未提交",
            data={
                "target": "qbittorrent",
                "requested": requested,
                "claimed": 0,
                "submitted": 0,
                "failed": 0,
            },
            error="请重新预检后再确认。",
        )

    if outcome_unknown:
        ok = submitted > 0
        status = "partial" if submitted or confirmed_failed else "review_required"
        summary = (
            f"RSS 条目提交结果：成功 {submitted}，待核对 {outcome_unknown}，"
            f"确认失败 {confirmed_failed}"
        )
    elif failed == 0:
        ok = True
        status = "completed"
        summary = f"已向 qBittorrent 提交 {submitted} 个 RSS 条目"
    elif submitted:
        ok = True
        status = "partial"
        summary = f"RSS 条目部分提交完成：成功 {submitted}，失败 {failed}"
    else:
        ok = False
        status = "failed"
        summary = f"本次 {failed} 个 RSS 条目均未成功提交"

    logger.info(
        "Agent RSS qB 提交完成 requested=%s claimed=%s submitted=%s failed=%s",
        requested,
        claimed,
        submitted,
        failed,
    )
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "target": "qbittorrent",
            "requested": requested,
            "claimed": claimed,
            "submitted": submitted,
            "failed": failed,
            **({"outcome_unknown": outcome_unknown} if outcome_unknown else {}),
        },
        evidence=[Evidence(
            "rss_submission",
            "已按确认时冻结的集合与 qB 配置执行一次有界提交；响应仅包含聚合计数。",
            _now(),
        )],
        suggestions=(
            ["请先核对 qBittorrent 中是否已存在对应任务，勿直接重复提交。"]
            if outcome_unknown else
            ([] if failed == 0 else ["请在 RSS 订阅页和下载任务页核对失败项。"])
        ),
        error=(
            "部分提交结果未知，请先核对 qBittorrent，勿直接重试。"
            if outcome_unknown else
            ("RSS 条目提交未全部成功。" if failed else "")
        ),
    )
