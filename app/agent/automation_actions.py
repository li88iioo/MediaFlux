"""媒体自动化链路的本地、只读诊断。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app import database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger

logger = get_logger(__name__)

_FAILURE_STATUSES = {
    "failed", "error", "interrupted", "partial_failed", "revert_failed", "cancelled",
}
_SUCCESS_STATUSES = {"success", "completed"}
_ACTIVE_STATUSES = {"running", "started", "queued"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def automation_pipeline_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise AgentToolError("automation.diagnose_pipeline 不接受参数")
    return {}


def _count(value: Any) -> int:
    """数据库聚合值只按非负整数输出，避免异常值污染状态判断。"""
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def _empty_data() -> dict[str, Any]:
    return {
        "probe_mode": "local",
        "network_accessed": False,
        "stages": {
            "downloads": {"status": "unavailable", "active": 0, "needs_review": 0},
            "rss": {
                "status": "unavailable",
                "enabled_subscriptions": 0,
                "pending": 0,
                "failed": 0,
            },
            "guangya_organize": {"status": "unavailable", "historical_issues": 0},
            "strm": {
                "status": "unavailable",
                "open_failures": 0,
                "last_run": "unknown",
            },
        },
        "attention": {"total": 0, "blockers": []},
    }


def _strm_stage(open_failures: int, raw_last_status: Any) -> dict[str, Any]:
    last_status = str(raw_last_status or "").strip().casefold()
    if last_status in _FAILURE_STATUSES:
        last_run = "failed"
    elif last_status in _SUCCESS_STATUSES:
        last_run = "completed"
    elif last_status in _ACTIVE_STATUSES:
        last_run = "running"
    elif last_status:
        last_run = "unknown"
    else:
        last_run = "not_observed"

    if open_failures or last_run == "failed":
        status = "attention"
    elif last_run == "running":
        status = "active"
    elif last_run == "completed":
        status = "healthy"
    elif last_run == "not_observed":
        status = "not_configured"
    else:
        status = "unknown"
    return {"status": status, "open_failures": open_failures, "last_run": last_run}


def diagnose_automation_pipeline(_arguments: dict[str, Any]) -> ToolResult:
    try:
        aggregate = db.get_dashboard_automation_summary()
    except Exception as exc:
        logger.warning("Agent 自动化链路诊断失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取媒体自动化状态",
            data=_empty_data(),
            evidence=[Evidence(
                "automation_database",
                "尝试读取本地自动化汇总；未访问外部服务或启动任务。",
                _now(),
            )],
            suggestions=["请检查本地数据库状态后重试。"],
            error="自动化链路诊断当前不可用。",
        )

    downloads_active = _count(aggregate.get("downloads_active"))
    downloads_review = _count(aggregate.get("downloads_review"))
    rss_subscriptions = _count(aggregate.get("rss_subscriptions"))
    rss_pending = _count(aggregate.get("rss_pending"))
    rss_failed = _count(aggregate.get("rss_failed"))
    organize_issues = _count(aggregate.get("organize_issues"))
    strm_failures = _count(aggregate.get("strm_failures"))

    downloads_status = "attention" if downloads_review else ("active" if downloads_active else "idle")
    if rss_failed:
        rss_status = "attention"
    elif not rss_subscriptions:
        rss_status = "not_configured"
    elif rss_pending:
        rss_status = "active"
    else:
        rss_status = "healthy"
    organize_status = "attention" if organize_issues else "healthy"
    strm_stage = _strm_stage(strm_failures, aggregate.get("strm_last_status"))

    blocker_specs = (
        ("downloads_need_review", "downloads", downloads_review),
        ("rss_failed_entries", "rss", rss_failed),
        ("organize_historical_issues", "guangya_organize", organize_issues),
        ("strm_open_failures", "strm", strm_failures),
    )
    blockers = [
        {"code": code, "stage": stage, "count": count}
        for code, stage, count in blocker_specs
        if count
    ]
    if strm_stage["last_run"] == "failed" and not strm_failures:
        blockers.append({"code": "strm_last_run_failed", "stage": "strm", "count": 1})
    attention_total = sum(item["count"] for item in blockers)
    observed = any((
        downloads_active,
        downloads_review,
        rss_subscriptions,
        rss_pending,
        rss_failed,
        organize_issues,
        strm_failures,
        str(aggregate.get("strm_last_status") or "").strip(),
    ))

    if attention_total:
        status = "attention"
        summary = f"媒体自动化有 {attention_total} 项需要关注"
    elif not observed:
        status = "not_configured"
        summary = "尚未观察到自动化任务或订阅"
    else:
        status = "healthy"
        summary = "媒体自动化链路当前未发现异常"

    suggestions: list[str] = []
    if downloads_review:
        suggestions.append("请运行下载队列诊断，核对需要人工处理的任务。")
    if rss_failed:
        suggestions.append("请运行 RSS 诊断，查看失败条目的分类统计。")
    if organize_issues:
        suggestions.append("光鸭整理存在历史异常记录，请在整理页面核对最近任务。")
    if strm_failures:
        suggestions.append("STRM 存在未关闭失败记录，请运行 STRM 诊断后再决定是否修复。")
    elif strm_stage["last_run"] == "failed":
        suggestions.append("最近一次 STRM 运行失败，请运行 STRM 诊断查看当前状态。")
    if status == "not_configured":
        suggestions.append("可先配置 RSS、下载或 STRM 自动化，再运行链路诊断。")

    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data={
            "probe_mode": "local",
            "network_accessed": False,
            "stages": {
                "downloads": {
                    "status": downloads_status,
                    "active": downloads_active,
                    "needs_review": downloads_review,
                },
                "rss": {
                    "status": rss_status,
                    "enabled_subscriptions": rss_subscriptions,
                    "pending": rss_pending,
                    "failed": rss_failed,
                },
                "guangya_organize": {
                    "status": organize_status,
                    "historical_issues": organize_issues,
                },
                "strm": strm_stage,
            },
            "attention": {"total": attention_total, "blockers": blockers},
        },
        evidence=[Evidence(
            "automation_database",
            "读取本地自动化汇总；未访问外部服务或启动任务。",
            _now(),
        )],
        suggestions=suggestions,
    )
