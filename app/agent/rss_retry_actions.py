"""Media Agent 的可重试 RSS 失败条目受控重试动作。"""

from __future__ import annotations

import secrets
from dataclasses import asdict
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


def rss_failure_retry_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) - {"limit"}:
        raise AgentToolError("rss.retry_failed 只接受 limit 参数")
    limit = arguments.get("limit", _DEFAULT_LIMIT)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_ITEMS
    ):
        raise AgentToolError("limit 必须是 1 到 20 的整数")
    return {"limit": limit}


def _capture(arguments: dict[str, Any]) -> dict[str, Any]:
    from app.modules.rss import capture_rss_qb_runtime_config
    from app.modules.offline import OfflineRules

    runtime_config, config_error = capture_rss_qb_runtime_config()
    limit = arguments["limit"]
    rows = db.get_retryable_failed_rss_snapshot(
        default_method=str(runtime_config.get("default_method") or ""),
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    entries = [db.rss_retry_entry_snapshot(row) for row in rows[:limit]]
    targets = sorted({
        str(entry["download_method"] or runtime_config.get("default_method") or "qb").strip().lower()
        for entry in entries
    })
    if "qb" not in targets:
        config_error = ""
        runtime_config = {"default_method": runtime_config.get("default_method") or "qb"}
    target = "both" if len(targets) > 1 else ("qbittorrent" if targets == ["qb"] else "guangya")
    cloud_rules = asdict(OfflineRules.from_config()) if "guangya" in targets else {}
    payload = {
        "limit": limit,
        "entries": entries,
        "has_more": has_more,
        "runtime_config": runtime_config,
        "config_error": str(config_error or ""),
        "cloud_rules": cloud_rules,
        "target": target,
    }
    return {
        **payload,
        "fingerprint": confirmation_context_fingerprint(payload, domain="rss-retry-failed"),
    }


def _preview_rss_failure_retry(
    arguments: dict[str, Any], state: dict[str, Any]
) -> ToolResult:
    """只读选择可安全重试的 failed 条目，不 claim、不访问网络。"""
    count = len(state["entries"])
    if count == 0:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary="当前没有可安全重试的 RSS 失败条目",
            error="只有已分类为可重试的 qB / 光鸭失败条目会进入本动作。",
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
            "action": "rss.retry_failed",
            "target": state["target"],
            "selected_count": count,
            "requested_limit": arguments["limit"],
            "has_more": bool(state["has_more"]),
            "effects": [
                "所选失败条目将原子认领后按各条目确认时的目标重新提交到 qBittorrent / 光鸭。",
                "已明确受理的条目会标记为已处理；再次失败会记录新的稳定失败分类。",
            ],
            "limits": {
                "max_items": _MAX_ITEMS,
                "retryable_failures_only": True,
                "max_retry_count": 5,
                "rate_limit_cooldown_seconds": 60,
            },
        },
        evidence=[
            Evidence(
                "rss_database",
                "只读核对本地可重试 RSS 失败条目；未刷新订阅、未认领条目、未访问下载器。",
                _now(),
            )
        ],
        suggestions=["确认前请核对重试数量；如数量过多，请使用更小的 limit。"],
    )


def prepare_rss_failure_retry(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    state = _capture(arguments)
    return _preview_rss_failure_retry(arguments, state), str(state["fingerprint"])


def _retry_failed_rss_state(state: dict[str, Any]) -> ToolResult:
    entries = list(state.get("entries") or [])
    runtime_config = dict(state.get("runtime_config") or {})
    if not entries or len(entries) > _MAX_ITEMS or state["config_error"]:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="RSS 失败重试条件已变化",
            error="请重新预检后再确认。",
        )

    from app.modules.rss import RSSEngine

    raw = RSSEngine().submit_snapshot(
        entries, runtime_config, claim=db.claim_retryable_failed_rss_entries,
    )
    result = build_rss_submission_result(
        raw,
        kind="retry",
        target=state["target"],
        evidence_description=(
            "已按确认时冻结的失败集合与目标配置执行一次有界重试；响应仅包含聚合计数。"
        ),
    )
    logger.info(
        "Agent RSS 失败重试结果 status=%s requested=%s claimed=%s submitted=%s failed=%s",
        result.status,
        result.data["requested"],
        result.data["claimed"],
        result.data["submitted"],
        result.data["failed"],
    )
    return result


def retry_failed_rss_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    state = _capture(arguments)
    if not secrets.compare_digest(
        str(state["fingerprint"]), str(expected_context or "")
    ):
        raise AgentToolError(
            "RSS 失败条目或下载目标/配置已变化，请重新预检",
            code="confirmation_stale",
        )
    return _retry_failed_rss_state(state)
