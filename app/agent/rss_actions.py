"""RSS 订阅与条目状态的只读、安全诊断。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.logger import get_logger

logger = get_logger(__name__)

_STALE_SUBMITTING_MINUTES = 15
_PENDING_BACKLOG_HOURS = 24
_MAX_ATTENTION_SUBSCRIPTIONS = 20
_MAX_SUMMARY_SUBSCRIPTIONS = 16


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rss_diagnosis_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise AgentToolError("rss.diagnose 不接受参数")
    return {}


def rss_subscription_summaries_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError("rss.subscription_summaries 不接受参数")
    return {}


def rss_subscription_summary_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) != {"subscription_id"}:
        raise AgentToolError("rss.get_subscription_summary 只接受 subscription_id 参数")
    subscription_id = arguments.get("subscription_id")
    if isinstance(subscription_id, bool) or not isinstance(subscription_id, int):
        raise AgentToolError("subscription_id 必须是正整数")
    if subscription_id <= 0:
        raise AgentToolError("subscription_id 必须是正整数")
    return {"subscription_id": subscription_id}


def _schedule_label(value: str) -> str:
    return {
        "disabled": "已停用",
        "manual_only": "仅手动刷新",
        "scheduled": "自动刷新正常",
        "scheduled_due": "已到刷新时间",
        "scheduled_invalid": "刷新时间记录异常",
    }.get(str(value or ""), "状态未知")


def list_rss_subscription_summaries(_arguments: dict[str, Any]) -> ToolResult:
    try:
        aggregate = db.list_rss_subscription_safe_summaries(
            db.now(), limit=_MAX_SUMMARY_SUBSCRIPTIONS
        )
    except Exception as exc:
        logger.warning("Agent RSS 订阅摘要读取失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取 RSS 订阅摘要",
            data={"total": 0, "returned": 0, "truncated": False, "items": []},
            evidence=[
                Evidence(
                    "rss_database",
                    "尝试读取本地 RSS 安全摘要；未读取订阅地址、条目正文、路径或凭据。",
                    _now(),
                )
            ],
            suggestions=["请检查本地数据库状态后重试。"],
            error="RSS 订阅摘要当前不可用。",
        )

    total = max(0, int(aggregate.get("total") or 0))
    raw_items = list(aggregate.get("items") or [])[:_MAX_SUMMARY_SUBSCRIPTIONS]
    items = [
        {
            "subscription_number": max(0, int(item.get("subscription_number") or 0)),
            "name": str(item.get("name") or "").strip()[:120],
            "subscription_name": str(item.get("name") or "").strip()[:120],
            "enabled": bool(item.get("enabled")),
            "schedule_state": _schedule_label(str(item.get("schedule_state") or "")),
            "attention_count": max(0, int(item.get("attention_count") or 0)),
            "downloaded_last_24h": max(
                0,
                int((item.get("entry_counts") or {}).get("downloaded_last_24h") or 0),
            ),
        }
        for item in raw_items
        if isinstance(item, dict)
    ]
    returned = len(items)
    if total == 0:
        status = "not_configured"
        summary = "尚未创建 RSS 订阅"
        suggestions = ["可先在 RSS 订阅页创建订阅。"]
    else:
        status = "completed"
        names = "、".join(item["name"] for item in items if item["name"])
        summary = (
            f"共有 {returned} 个 RSS 订阅：{names}"
            if names
            else f"共有 {returned} 个 RSS 订阅"
        )
        suggestions = ["可以直接说“刷新全部 RSS 订阅”或“刷新 Mikan”。"]
    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data={
            "total": total,
            "returned": returned,
            "truncated": total > returned,
            "items": items,
        },
        evidence=[
            Evidence(
                "rss_database",
                "读取订阅名称、编号、启用/调度状态与聚合计数；未读取 URL、过滤词、条目正文或路径。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )


def get_rss_subscription_summary(arguments: dict[str, int]) -> ToolResult:
    subscription_id = int(arguments["subscription_id"])
    try:
        item = db.get_rss_subscription_safe_summary(subscription_id, db.now())
    except Exception as exc:
        logger.warning("Agent RSS 单订阅摘要读取失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取该 RSS 订阅状态",
            data={"subscription_number": subscription_id},
            evidence=[
                Evidence(
                    "rss_database",
                    "尝试按精确编号读取本地安全摘要；未读取订阅地址、条目正文、路径或凭据。",
                    _now(),
                )
            ],
            suggestions=["请稍后重试。"],
            error="RSS 订阅摘要当前不可用。",
        )
    if item is None:
        return ToolResult(
            ok=False,
            status="not_found",
            summary=f"未找到 RSS 订阅 #{subscription_id}",
            data={"subscription_number": subscription_id},
            evidence=[
                Evidence(
                    "rss_database",
                    "按精确订阅编号检查本地记录；未读取任何敏感配置。",
                    _now(),
                )
            ],
            suggestions=["可先查看 RSS 订阅安全摘要列表确认编号。"],
            error="指定的 RSS 订阅不存在。",
        )

    attention_count = max(0, int(item.get("attention_count") or 0))
    schedule_label = _schedule_label(str(item.get("schedule_state") or ""))
    status = "attention" if attention_count else "healthy"
    summary = (
        f"RSS 订阅《{item.get('name') or subscription_id}》为{schedule_label}，有 {attention_count} 项需要关注"
        if attention_count
        else f"RSS 订阅《{item.get('name') or subscription_id}》状态正常，当前为{schedule_label}"
    )
    suggestions = ["如需修改，请明确说明启用、停用或刷新周期，并再次确认。"]
    if attention_count:
        suggestions.insert(0, "可运行 RSS 诊断查看需要关注的聚合原因。")
    public_item = dict(item)
    public_item["schedule_state"] = schedule_label
    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data=public_item,
        evidence=[
            Evidence(
                "rss_database",
                "读取指定订阅的名称、编号、启用/调度状态与聚合计数；未读取 URL、过滤词、条目正文或路径。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )


def get_rss_recent_activity(_arguments: dict[str, Any]) -> ToolResult:
    """返回最近 24 小时成功下载次数，按订阅名称汇总。"""
    try:
        aggregate = db.list_rss_subscription_safe_summaries(
            db.now(), limit=_MAX_SUMMARY_SUBSCRIPTIONS
        )
    except Exception as exc:
        logger.warning("Agent RSS 最近活动读取失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法统计最近 24 小时的 RSS 下载次数",
            error="请稍后重试。",
        )

    try:
        total_downloaded = db.count_rss_downloaded_entries_since(db.now(), hours=24)
    except Exception as exc:
        logger.warning("Agent RSS 最近活动总数读取失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法统计最近 24 小时的 RSS 下载次数",
            error="请稍后重试。",
        )

    items: list[dict[str, Any]] = []
    for raw in list(aggregate.get("items") or []):
        if not isinstance(raw, dict):
            continue
        count = max(
            0,
            int((raw.get("entry_counts") or {}).get("downloaded_last_24h") or 0),
        )
        items.append(
            {
                "subscription_number": max(0, int(raw.get("subscription_number") or 0)),
                "name": str(raw.get("name") or "").strip()[:120],
                "subscription_name": str(raw.get("name") or "").strip()[:120],
                "downloaded_last_24h": count,
            }
        )

    return ToolResult(
        ok=True,
        status="completed",
        summary=f"最近 24 小时 RSS 共成功下载 {total_downloaded} 次",
        data={
            "window_hours": 24,
            "downloaded": total_downloaded,
            "subscriptions": items,
            "subscriptions_returned": len(items),
            "subscriptions_truncated": bool(aggregate.get("truncated")),
        },
        evidence=[
            Evidence(
                "rss_database",
                "按已处理成功条目的完成时间统计最近 24 小时下载次数；跳过条目不计入。",
                _now(),
            )
        ],
    )


def _empty_data() -> dict[str, Any]:
    return {
        "thresholds": {
            "stale_submitting_minutes": _STALE_SUBMITTING_MINUTES,
            "pending_backlog_hours": _PENDING_BACKLOG_HOURS,
        },
        "subscriptions": {},
        "entries": {},
        "attention": {
            "total": 0,
            "categories": {},
            "subscriptions": [],
            "truncated": False,
        },
    }


def diagnose_rss(_arguments: dict[str, Any]) -> ToolResult:
    try:
        snapshot = db.now()
        aggregate = db.get_rss_diagnostic_summary(
            snapshot,
            stale_submitting_minutes=_STALE_SUBMITTING_MINUTES,
            pending_backlog_hours=_PENDING_BACKLOG_HOURS,
            attention_limit=_MAX_ATTENTION_SUBSCRIPTIONS,
        )
    except Exception as exc:
        logger.warning("Agent RSS 诊断失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取 RSS 订阅状态",
            data=_empty_data(),
            evidence=[
                Evidence(
                    "rss_database",
                    "尝试读取本地 RSS 状态；未刷新订阅或提交下载。",
                    _now(),
                )
            ],
            suggestions=["请检查本地数据库状态后重试。"],
            error="RSS 诊断当前不可用。",
        )

    subscriptions = dict(aggregate.get("subscriptions") or {})
    entries = dict(aggregate.get("entries") or {})
    attention_subscriptions = list(aggregate.get("attention_subscriptions") or [])
    categories = {
        "failed_entries": max(0, int(entries.get("failed") or 0)),
        "stale_submitting": max(0, int(entries.get("stale_submitting") or 0)),
        "aged_pending_backlog": max(0, int(entries.get("pending_backlog") or 0)),
        "unknown_or_inconsistent": max(
            0, int(entries.get("unknown_or_inconsistent") or 0)
        ),
        "invalid_last_refreshed_at": max(
            0, int(subscriptions.get("invalid_last_refreshed_at") or 0)
        ),
        "cron_configured_but_not_scheduled": max(
            0, int(subscriptions.get("cron_configured_but_not_scheduled") or 0)
        ),
    }
    attention_total = sum(categories.values())
    total_subscriptions = max(0, int(subscriptions.get("total") or 0))
    enabled_subscriptions = max(0, int(subscriptions.get("enabled") or 0))

    if total_subscriptions == 0:
        status = "not_configured"
        summary = "尚未创建 RSS 订阅"
    elif enabled_subscriptions == 0:
        status = "inactive"
        summary = f"已有 {total_subscriptions} 个 RSS 订阅，但当前均未启用"
    elif attention_total:
        status = "attention"
        summary = f"RSS 状态有 {attention_total} 项需要关注"
    else:
        status = "healthy"
        summary = f"RSS 状态正常，当前启用 {enabled_subscriptions} 个订阅"

    suggestions: list[str] = []
    if categories["failed_entries"]:
        suggestions.append("请在 RSS 订阅页检查失败条目，并确认下载目标可用。")
    if categories["stale_submitting"]:
        suggestions.append(
            "存在长期停留在提交中的条目，请核对下载后端后再决定是否重试。"
        )
    if categories["aged_pending_backlog"]:
        suggestions.append(
            "存在超过 24 小时的待处理条目，请检查自动下载策略或进行人工处理。"
        )
    if categories["unknown_or_inconsistent"]:
        suggestions.append("发现状态不一致的 RSS 条目，建议先在管理页核对。")
    if categories["invalid_last_refreshed_at"]:
        suggestions.append(
            "发现无法解析的上次刷新时间；系统会将其视为到期并在下次刷新后修复。"
        )
    if categories["cron_configured_but_not_scheduled"]:
        suggestions.append(
            "当前 RSS 调度按刷新间隔运行；仅填写 Cron 不会启用自动刷新。"
        )
    if status == "not_configured":
        suggestions.append("可先创建一个 RSS 订阅，再运行诊断。")
    elif status == "inactive":
        suggestions.append("如需自动刷新，请启用至少一个 RSS 订阅。")

    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data={
            "thresholds": dict(aggregate.get("thresholds") or {}),
            "subscriptions": subscriptions,
            "entries": entries,
            "attention": {
                "total": attention_total,
                "categories": categories,
                "subscriptions": attention_subscriptions,
                "truncated": bool(aggregate.get("attention_truncated")),
            },
        },
        evidence=[
            Evidence(
                "rss_database",
                "读取本地 RSS 聚合状态；未访问订阅源、刷新订阅或提交下载。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )
