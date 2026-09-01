"""Media Agent 的可重试 RSS 失败条目受控 qB 重试动作。"""
from __future__ import annotations

from datetime import datetime
import secrets
from typing import Any

from app import database as db
from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_LIMIT = 10
_MAX_ITEMS = 20
def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rss_failure_retry_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) - {"limit"}:
        raise AgentToolError("rss.retry_failed_to_qb 只接受 limit 参数")
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
        "failure_code": str(row["failure_code"] or ""),
        "failure_retryable": int(row["failure_retryable"] or 0),
        "retry_count": int(row["retry_count"] or 0),
        "failed_at": str(row["failed_at"] or ""),
        "download_method": str(row["download_method"] or ""),
        "qb_save_path": str(row["qb_save_path"] or ""),
    }


def _capture(arguments: dict[str, Any]) -> dict[str, Any]:
    from app.modules.rss import capture_rss_qb_runtime_config

    runtime_config, config_error = capture_rss_qb_runtime_config()
    limit = arguments["limit"]
    rows = db.get_retryable_failed_rss_qb_snapshot(
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
            payload, domain="rss-retry-failed-to-qb"
        ),
    }


def _preview_rss_failure_retry(
    arguments: dict[str, Any], state: dict[str, Any]
) -> ToolResult:
    """只读选择可安全重试的 failed qB 条目，不 claim、不访问网络。"""
    count = len(state["entries"])
    if count == 0:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary="当前没有可安全重试的 qBittorrent RSS 失败条目",
            error="只有已分类为可重试的 qB 失败条目会进入本动作。",
            suggestions=["可先询问：诊断 RSS 失败状态。"],
        )
    if state["config_error"]:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="qBittorrent 重试配置当前不可用",
            error="请检查 qBittorrent 配置后重新预检。",
            suggestions=["可询问：为什么下载器配置不可用？"],
        )

    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将重试 {count} 个可安全重试的 RSS 失败条目",
        data={
            "action": "rss.retry_failed_to_qb",
            "target": "qbittorrent",
            "selected_count": count,
            "requested_limit": arguments["limit"],
            "has_more": bool(state["has_more"]),
            "effects": [
                "所选失败条目将原子认领后按当前确认配置重新提交到 qBittorrent。",
                "成功条目会标记为已下载；再次失败会记录新的稳定失败分类。",
            ],
            "limits": {
                "max_items": _MAX_ITEMS,
                "retryable_failures_only": True,
                "max_retry_count": 5,
                "rate_limit_cooldown_seconds": 60,
            },
        },
        evidence=[Evidence(
            "rss_database",
            "只读核对本地可重试 RSS 失败条目；未刷新订阅、未认领条目、未访问下载器。",
            _now(),
        )],
        suggestions=["确认前请核对重试数量；如数量过多，请使用更小的 limit。"],
    )


def prepare_rss_failure_retry(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    state = _capture(arguments)
    return _preview_rss_failure_retry(arguments, state), str(state["fingerprint"])


def _retry_failed_rss_to_qb_state(state: dict[str, Any]) -> ToolResult:
    entries = list(state.get("entries") or [])
    runtime_config = dict(state.get("runtime_config") or {})
    if not entries or len(entries) > _MAX_ITEMS or not runtime_config.get("url"):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="RSS 失败重试条件已变化",
            error="请重新预检后再确认。",
        )

    from app.modules.rss import RSSEngine

    raw = RSSEngine().retry_failed_qb_snapshot(entries, runtime_config)
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
            summary="可重试 RSS 失败条目已变化，本次未提交",
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
            f"RSS 失败条目提交结果：成功 {submitted}，待核对 {outcome_unknown}，"
            f"确认失败 {confirmed_failed}"
        )
    elif failed == 0:
        ok = True
        status = "completed"
        summary = f"已成功重试 {submitted} 个 RSS 失败条目"
    elif submitted:
        ok = True
        status = "partial"
        summary = f"RSS 失败条目部分重试完成：成功 {submitted}，失败 {failed}"
    else:
        ok = False
        status = "failed"
        summary = f"本次 {failed} 个 RSS 失败条目仍未成功提交"

    logger.info(
        "Agent RSS qB 失败重试完成 requested=%s claimed=%s submitted=%s failed=%s",
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
            "rss_retry",
            "已按确认时冻结的失败集合与 qB 配置执行一次有界重试；响应仅包含聚合计数。",
            _now(),
        )],
        suggestions=(
            ["请先核对 qBittorrent 中是否已存在对应任务，勿直接重复提交。"]
            if outcome_unknown else
            ([] if failed == 0 else ["请重新诊断 RSS 失败状态后再决定下一步。"])
        ),
        error=(
            "部分提交结果未知，请先核对 qBittorrent，勿直接重试。"
            if outcome_unknown else
            ("RSS 失败条目重试未全部成功。" if failed else "")
        ),
    )


def retry_failed_rss_to_qb_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    state = _capture(arguments)
    if not secrets.compare_digest(
        str(state["fingerprint"]), str(expected_context or "")
    ):
        raise AgentToolError(
            "RSS 失败条目或 qBittorrent 配置已变化，请重新预检",
            code="confirmation_stale",
        )
    return _retry_failed_rss_to_qb_state(state)
