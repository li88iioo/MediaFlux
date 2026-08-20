"""工作区统一待办的本地、只读、安全聚合。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app import database as db
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.agent.result_projection import public_followup_prompt
from app.logger import get_logger

logger = get_logger(__name__)

_AREA_SPECS = (
    ("downloads", "downloads.diagnose_queue"),
    ("rss", "rss.diagnose"),
    ("organize", "guangya.organize.status"),
    ("strm", "strm.triage_failures"),
    ("local_media", "local_media.diagnose"),
    ("download_verification", "downloads.diagnose_queue"),
    ("library_patrol", "library.patrol_status"),
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def workspace_todo_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise AgentToolError("workspace.todo 不接受参数")
    return {}


def _count(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def _area(
    source: str,
    next_tool: str,
    *,
    attention: int = 0,
    active: int = 0,
    waiting: int = 0,
    reason_codes: list[str] | None = None,
    unavailable: bool = False,
) -> dict[str, Any]:
    attention = _count(attention)
    active = _count(active)
    waiting = _count(waiting)
    if unavailable:
        status = "unavailable"
    elif attention:
        status = "attention"
    elif active:
        status = "active"
    elif waiting:
        status = "waiting"
    else:
        status = "idle"
    return {
        "source": source,
        "status": status,
        "attention_count": attention,
        "active_count": active,
        "waiting_count": waiting,
        "reason_codes": list(reason_codes or []),
        "next_tool": next_tool,
    }


def _unavailable_area(source: str, next_tool: str) -> dict[str, Any]:
    return _area(source, next_tool, unavailable=True)


def summarize_workspace_todo(_arguments: dict[str, Any]) -> ToolResult:
    automation: dict[str, Any] | None = None
    local_media: dict[str, Any] | None = None
    persistent_health: dict[str, Any] | None = None
    unavailable_sources: list[str] = []

    try:
        raw_automation = db.get_dashboard_automation_summary()
        automation = raw_automation if isinstance(raw_automation, dict) else {}
    except Exception as exc:
        unavailable_sources.append("automation")
        logger.warning("Agent 工作区待办读取自动化汇总失败 type=%s", type(exc).__name__)

    try:
        raw_local_media = db.get_local_media_diagnostic_summary(owner="admin")
        local_media = raw_local_media if isinstance(raw_local_media, dict) else {}
    except Exception as exc:
        unavailable_sources.append("local_media")
        logger.warning("Agent 工作区待办读取本地媒体汇总失败 type=%s", type(exc).__name__)

    try:
        raw_persistent = db.get_agent_persistent_health_summary()
        persistent_health = raw_persistent if isinstance(raw_persistent, dict) else {}
    except Exception as exc:
        unavailable_sources.append("agent_persistent")
        logger.warning("Agent 工作区待办读取持久自动化汇总失败 type=%s", type(exc).__name__)

    areas: list[dict[str, Any]] = []
    if automation is None:
        areas.extend(_unavailable_area(source, next_tool) for source, next_tool in _AREA_SPECS[:4])
    else:
        downloads_review = _count(automation.get("downloads_review"))
        downloads_active = _count(automation.get("downloads_active"))
        areas.append(_area(
            "downloads",
            "downloads.diagnose_queue",
            attention=downloads_review,
            active=downloads_active,
            reason_codes=["download_needs_review"] if downloads_review else [],
        ))

        rss_failed = _count(automation.get("rss_failed"))
        rss_pending = _count(automation.get("rss_pending"))
        rss_reasons: list[str] = []
        if rss_failed:
            rss_reasons.append("rss_failed")
        if rss_pending:
            rss_reasons.append("rss_pending")
        areas.append(_area(
            "rss",
            "rss.diagnose",
            attention=rss_failed,
            waiting=rss_pending,
            reason_codes=rss_reasons,
        ))

        organize_issues = _count(automation.get("organize_issues"))
        areas.append(_area(
            "organize",
            "guangya.organize.status",
            attention=organize_issues,
            reason_codes=["organize_issue"] if organize_issues else [],
        ))

        strm_failures = _count(automation.get("strm_failures"))
        strm_last_status = str(automation.get("strm_last_status") or "").strip().casefold()
        strm_last_failed = strm_last_status in {
            "failed", "error", "interrupted", "partial_failed", "revert_failed", "cancelled",
        }
        strm_running = strm_last_status in {"running", "started", "queued"}
        strm_attention = strm_failures or int(strm_last_failed)
        strm_reasons: list[str] = []
        if strm_failures:
            strm_reasons.append("strm_open_failure")
        if strm_last_failed:
            strm_reasons.append("strm_last_run_failed")
        if strm_running:
            strm_reasons.append("strm_running")
        areas.append(_area(
            "strm",
            "strm.triage_failures",
            attention=strm_attention,
            active=int(strm_running),
            reason_codes=strm_reasons,
        ))

    if local_media is None:
        areas.append(_unavailable_area("local_media", "local_media.diagnose"))
    else:
        raw_sources = local_media.get("sources")
        raw_tasks = local_media.get("tasks")
        sources = raw_sources if isinstance(raw_sources, dict) else {}
        tasks = raw_tasks if isinstance(raw_tasks, dict) else {}
        requires_manual = _count(tasks.get("requires_manual"))
        failed = _count(tasks.get("failed"))
        missing_target = _count(sources.get("enabled_without_targets"))
        active = _count(tasks.get("active"))
        waiting = _count(tasks.get("waiting_stable")) + _count(tasks.get("planned"))
        local_reasons: list[str] = []
        if requires_manual:
            local_reasons.append("local_media_requires_manual")
        if failed:
            local_reasons.append("local_media_failed")
        if missing_target:
            local_reasons.append("local_media_missing_target")
        if active:
            local_reasons.append("local_media_active")
        if waiting:
            local_reasons.append("local_media_waiting")
        areas.append(_area(
            "local_media",
            "local_media.diagnose",
            attention=requires_manual + failed + missing_target,
            active=active,
            waiting=waiting,
            reason_codes=local_reasons,
        ))

    if persistent_health is None:
        areas.extend(
            _unavailable_area(source, next_tool)
            for source, next_tool in _AREA_SPECS[5:]
        )
    else:
        raw_verification = persistent_health.get("download_verification")
        verification = raw_verification if isinstance(raw_verification, dict) else {}
        verification_attention = _count(verification.get("attention"))
        verification_running = _count(verification.get("running"))
        verification_pending = _count(verification.get("pending"))
        verification_retry = _count(verification.get("retry_wait"))
        verification_reasons: list[str] = []
        if verification_attention:
            verification_reasons.append("download_verification_attention")
        if verification_running:
            verification_reasons.append("download_verification_running")
        if verification_pending:
            verification_reasons.append("download_verification_pending")
        if verification_retry:
            verification_reasons.append("download_verification_retry_wait")
        verification_area = _area(
            "download_verification",
            "downloads.diagnose_queue",
            attention=verification_attention,
            active=verification_running,
            waiting=verification_pending + verification_retry,
            reason_codes=verification_reasons,
        )
        verification_area.update({
            "pending_count": verification_pending,
            "retry_wait_count": verification_retry,
            "visible_count": _count(verification.get("visible")),
        })
        areas.append(verification_area)

        raw_patrol = persistent_health.get("library_patrol")
        patrol = raw_patrol if isinstance(raw_patrol, dict) else {}
        patrol_status = str(patrol.get("status") or "not_created").strip().casefold()
        if patrol_status not in {"not_created", "pending", "running", "retry_wait"}:
            patrol_status = "not_created"
        patrol_outcome = str(patrol.get("outcome") or "").strip().casefold()
        if patrol_outcome not in {
            "", "updates_available", "up_to_date", "inconclusive",
            "not_configured", "unavailable", "failed",
        }:
            patrol_outcome = ""
        updates_available = _count(patrol.get("updates_available_count"))
        patrol_failed = patrol_outcome in {
            "inconclusive", "not_configured", "unavailable", "failed",
        }
        patrol_attention = max(updates_available, int(patrol_failed))
        patrol_reasons: list[str] = []
        if updates_available:
            patrol_reasons.append("library_patrol_updates_available")
        if patrol_failed:
            patrol_reasons.append(f"library_patrol_{patrol_outcome}")
        if patrol_status == "running":
            patrol_reasons.append("library_patrol_running")
        if patrol_status == "retry_wait":
            patrol_reasons.append("library_patrol_retry_wait")
        patrol_area = _area(
            "library_patrol",
            "library.patrol_status",
            attention=patrol_attention,
            active=int(patrol_status == "running"),
            waiting=int(patrol_status == "retry_wait"),
            reason_codes=patrol_reasons,
        )
        patrol_area.update({
            "task_status": patrol_status,
            "outcome": patrol_outcome,
            "checked_series_count": _count(patrol.get("checked_series_count")),
            "updates_available_count": updates_available,
            "missing_episode_count": _count(patrol.get("missing_episode_count")),
            "inconclusive_count": _count(patrol.get("inconclusive_count")),
            "unmapped_series_count": _count(patrol.get("unmapped_series_count")),
            "findings_truncated": bool(_count(patrol.get("findings_truncated"))),
        })
        areas.append(patrol_area)

    attention_total = sum(item["attention_count"] for item in areas)
    active_total = sum(item["active_count"] for item in areas)
    waiting_total = sum(item["waiting_count"] for item in areas)
    data = {
        "probe_mode": "local",
        "network_accessed": False,
        "filesystem_accessed": False,
        "attention_total": attention_total,
        "active_total": active_total,
        "waiting_total": waiting_total,
        "unavailable_areas": [
            item["source"] for item in areas if item["status"] == "unavailable"
        ],
        "areas": areas,
    }

    evidence: list[Evidence] = []
    if automation is not None:
        evidence.append(Evidence(
            "automation_database",
            "读取下载、RSS、整理与 STRM 的本地安全计数；未访问外部服务或启动任务。",
            _now(),
        ))
    if local_media is not None:
        evidence.append(Evidence(
            "local_media_database",
            "读取本地媒体安全计数；未扫描媒体文件系统或启动任务。",
            _now(),
        ))
    if persistent_health is not None:
        evidence.append(Evidence(
            "agent_persistent_database",
            "读取下载后核验与媒体库巡检的匿名状态计数；未读取标题、路径、任务标识或错误正文。",
            _now(),
        ))

    if areas and all(item["status"] == "unavailable" for item in areas):
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="暂时无法读取工作区待办",
            data=data,
            evidence=[Evidence(
                "workspace_database",
                "尝试读取本地安全聚合；未访问网络、文件系统或启动任务。",
                _now(),
            )],
            suggestions=["请检查本地数据库状态后重试。"],
            error="工作区待办当前不可用。",
        )

    suggestions: list[str] = []
    for item in areas:
        if item["attention_count"]:
            suggestions.append(public_followup_prompt(item.get("source")))
    if unavailable_sources:
        status = "partial"
        summary = f"已读取部分工作区待办，共有 {attention_total} 项需要关注"
        suggestions.append("部分本地聚合暂时不可用，请稍后重试。")
    elif attention_total:
        status = "attention"
        summary = f"工作区共有 {attention_total} 项需要关注"
    elif active_total:
        status = "active"
        summary = f"工作区有 {active_total} 项正在处理"
    elif waiting_total:
        status = "waiting"
        summary = f"工作区有 {waiting_total} 项等待处理"
    else:
        status = "empty"
        summary = "工作区当前没有待处理事项"

    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data=data,
        evidence=evidence,
        suggestions=suggestions,
    )
