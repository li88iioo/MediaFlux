"""本地媒体来源、任务与调度器的安全只读诊断。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app import database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger
from app.modules.local_media_scheduler import peek_local_media_scheduler_status

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def local_media_diagnosis_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise AgentToolError("local_media.diagnose 不接受参数")
    return {}


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _empty_data() -> dict[str, Any]:
    return {
        "probe_mode": "local",
        "network_accessed": False,
        "sources": {
            "total": 0,
            "enabled": 0,
            "disabled": 0,
            "move_mode": 0,
            "preview_only_mode": 0,
            "enabled_without_targets": 0,
        },
        "tasks": {
            "total": 0,
            "waiting_stable": 0,
            "active": 0,
            "requires_manual": 0,
            "planned": 0,
            "failed": 0,
            "completed": 0,
            "by_trigger": {"qb_completed": 0, "scan": 0, "manual": 0},
        },
        "scheduler": {"running": False, "interval_seconds": 0.0},
        "attention": {
            "total": 0,
            "categories": {
                "requires_manual": 0,
                "failed": 0,
                "enabled_sources_without_targets": 0,
                "scheduler_not_running": 0,
            },
        },
    }


def diagnose_local_media(_arguments: dict[str, Any]) -> ToolResult:
    try:
        aggregate = db.get_local_media_diagnostic_summary(owner="admin")
        runtime = peek_local_media_scheduler_status()
    except Exception as exc:
        logger.warning("Agent 本地媒体诊断失败 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取本地媒体状态",
            data=_empty_data(),
            evidence=[Evidence(
                "local_media_database",
                "尝试读取本地媒体数据库与进程状态；未扫描媒体文件系统，也未访问下载器或外部服务。",
                _now(),
            )],
            suggestions=["请检查本地数据库与服务进程状态后重试。"],
            error="本地媒体诊断当前不可用。",
        )

    raw_sources = aggregate.get("sources") if isinstance(aggregate, dict) else {}
    raw_tasks = aggregate.get("tasks") if isinstance(aggregate, dict) else {}
    raw_sources = raw_sources if isinstance(raw_sources, dict) else {}
    raw_tasks = raw_tasks if isinstance(raw_tasks, dict) else {}

    sources = {
        "total": _count(raw_sources.get("total")),
        "enabled": _count(raw_sources.get("enabled")),
        "disabled": _count(raw_sources.get("disabled")),
        "move_mode": _count(raw_sources.get("move_mode")),
        "preview_only_mode": _count(raw_sources.get("preview_only_mode")),
        "enabled_without_targets": _count(raw_sources.get("enabled_without_targets")),
    }
    tasks = {
        "total": _count(raw_tasks.get("total")),
        "waiting_stable": _count(raw_tasks.get("waiting_stable")),
        "active": _count(raw_tasks.get("active")),
        "requires_manual": _count(raw_tasks.get("requires_manual")),
        "planned": _count(raw_tasks.get("planned")),
        "failed": _count(raw_tasks.get("failed")),
        "completed": _count(raw_tasks.get("completed")),
        "by_trigger": {
            "qb_completed": _count(raw_tasks.get("qb_completed")),
            "scan": _count(raw_tasks.get("scan")),
            "manual": _count(raw_tasks.get("manual")),
        },
    }
    scheduler = {
        "running": bool(runtime.get("running")) if isinstance(runtime, dict) else False,
        "interval_seconds": max(
            0.0,
            float(runtime.get("interval_seconds") or 0.0) if isinstance(runtime, dict) else 0.0,
        ),
    }
    categories = {
        "requires_manual": tasks["requires_manual"],
        "failed": tasks["failed"],
        "enabled_sources_without_targets": sources["enabled_without_targets"],
        "scheduler_not_running": int(bool(sources["enabled"] and not scheduler["running"])),
    }
    attention_total = sum(categories.values())

    if not sources["total"]:
        status = "not_configured"
        summary = "尚未配置本地媒体来源"
    elif attention_total:
        status = "attention"
        summary = f"本地媒体有 {attention_total} 项需要关注"
    elif not sources["enabled"]:
        status = "inactive"
        summary = "本地媒体来源当前均未启用"
    elif tasks["waiting_stable"] or tasks["active"]:
        status = "active"
        summary = "本地媒体任务正在等待或处理中"
    else:
        status = "idle"
        summary = "本地媒体当前没有待处理异常"

    suggestions: list[str] = []
    if not sources["total"]:
        suggestions.append("请先在本地媒体页面配置媒体来源。")
    elif not sources["enabled"]:
        suggestions.append("如需自动处理，请启用至少一个本地媒体来源。")
    if categories["requires_manual"]:
        suggestions.append("请优先处理本地媒体待确认队列。")
    if categories["failed"]:
        suggestions.append("请查看失败任务并确认原因后再重试。")
    if categories["enabled_sources_without_targets"]:
        suggestions.append("请为已启用但未映射目标的来源配置媒体库目标。")
    if categories["scheduler_not_running"]:
        suggestions.append("本地媒体调度器当前未运行；如非维护状态，请检查后台服务。")

    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data={
            "probe_mode": "local",
            "network_accessed": False,
            "sources": sources,
            "tasks": tasks,
            "scheduler": scheduler,
            "attention": {"total": attention_total, "categories": categories},
        },
        evidence=[Evidence(
            "local_media_database",
            "读取本地媒体安全聚合与进程内调度状态；未扫描媒体文件系统，未访问下载器、TMDB 或外部服务，也未启动任务。",
            _now(),
        )],
        suggestions=suggestions,
    )


def local_media_review_queue_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError("local_media.review_queue_summary 不接受参数")
    return {}


def local_media_history_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError("local_media.history_summary 不接受参数")
    return {}


def _fixed_age_buckets(raw: Any) -> dict[str, int]:
    source = raw if isinstance(raw, dict) else {}
    return {
        "under_1h": _count(source.get("under_1h")),
        "1h_to_24h": _count(source.get("1h_to_24h")),
        "1d_to_7d": _count(source.get("1d_to_7d")),
        "over_7d": _count(source.get("over_7d")),
        "unknown": _count(source.get("unknown")),
    }


def _fixed_triggers(raw: Any) -> dict[str, int]:
    source = raw if isinstance(raw, dict) else {}
    known = {
        "qb_completed": _count(source.get("qb_completed")),
        "scan": _count(source.get("scan")),
        "manual": _count(source.get("manual")),
    }
    known["unknown"] = sum(
        _count(value) for key, value in source.items()
        if str(key) not in known
    )
    return known


def _summary_evidence(label: str) -> Evidence:
    return Evidence(
        "sqlite:local_media_tasks",
        f"仅统计本地媒体{label}的数量、触发来源和年龄区间；未读取或返回标题、路径、任务标识、错误正文或凭据。",
        _now(),
    )


def summarize_local_media_review_queue(_arguments: dict[str, Any]) -> ToolResult:
    empty = {
        "probe_mode": "database",
        "network_accessed": False,
        "filesystem_accessed": False,
        "scope": "requires_manual",
        "total": 0,
        "by_trigger": _fixed_triggers({}),
        "age_buckets": _fixed_age_buckets({}),
    }
    try:
        raw = db.get_local_media_review_queue_summary(owner="admin")
    except Exception as exc:
        logger.warning("Agent 本地媒体待确认摘要不可用 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取本地媒体待确认队列",
            data=empty,
            evidence=[_summary_evidence("待确认队列")],
            suggestions=["请检查本地数据库状态后重试。"],
            error="本地媒体待确认摘要当前不可用。",
        )

    raw = raw if isinstance(raw, dict) else {}
    total = _count(raw.get("total"))
    by_trigger = _fixed_triggers(raw.get("by_trigger"))
    age_buckets = _fixed_age_buckets(raw.get("age_buckets"))
    if total:
        status = "attention"
        summary = f"本地媒体有 {total} 项等待人工确认"
        ok = False
    else:
        status = "healthy"
        summary = "本地媒体当前没有待人工确认项"
        ok = True
    suggestions: list[str] = []
    if age_buckets["over_7d"]:
        suggestions.append(f"其中有 {age_buckets['over_7d']} 项已等待超过 7 天，建议优先处理。")
    elif age_buckets["1d_to_7d"]:
        suggestions.append(f"其中有 {age_buckets['1d_to_7d']} 项已等待 1 到 7 天。")
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            **empty,
            "total": total,
            "by_trigger": by_trigger,
            "age_buckets": age_buckets,
        },
        evidence=[_summary_evidence("待确认队列")],
        suggestions=suggestions,
    )


def summarize_local_media_history(_arguments: dict[str, Any]) -> ToolResult:
    empty = {
        "probe_mode": "database",
        "network_accessed": False,
        "filesystem_accessed": False,
        "scope": "terminal_history",
        "total": 0,
        "by_status": {"completed": 0, "failed": 0},
        "by_trigger": _fixed_triggers({}),
        "age_buckets": _fixed_age_buckets({}),
    }
    try:
        raw = db.get_local_media_history_summary(owner="admin")
    except Exception as exc:
        logger.warning("Agent 本地媒体历史摘要不可用 type=%s", type(exc).__name__)
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取本地媒体处理历史",
            data=empty,
            evidence=[_summary_evidence("终态历史")],
            suggestions=["请检查本地数据库状态后重试。"],
            error="本地媒体历史摘要当前不可用。",
        )

    raw = raw if isinstance(raw, dict) else {}
    raw_status = raw.get("by_status") if isinstance(raw.get("by_status"), dict) else {}
    total = _count(raw.get("total"))
    by_status = {
        "completed": _count(raw_status.get("completed")),
        "failed": _count(raw_status.get("failed")),
    }
    failed = by_status["failed"]
    if failed:
        status = "attention"
        summary = f"本地媒体历史中有 {failed} 项失败，已完成 {by_status['completed']} 项"
        ok = False
    elif total:
        status = "healthy"
        summary = f"本地媒体最近共有 {total} 项终态记录，均已完成"
        ok = True
    else:
        status = "empty"
        summary = "本地媒体当前没有已完成或失败的历史记录"
        ok = True
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            **empty,
            "total": total,
            "by_status": by_status,
            "by_trigger": _fixed_triggers(raw.get("by_trigger")),
            "age_buckets": _fixed_age_buckets(raw.get("age_buckets")),
        },
        evidence=[_summary_evidence("终态历史")],
        suggestions=(
            ["可查看最近整理失败摘要，确认失败集中在本地整理还是光鸭整理。"]
            if failed else []
        ),
    )
