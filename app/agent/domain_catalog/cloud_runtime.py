"""光鸭整理运行状态的安全领域投影。"""

from __future__ import annotations

from typing import Any

from app.agent.models import Evidence, ToolContext, ToolResult

from .shared import _bounded_int, _now, _safe_choice, _safe_timestamp


def guangya_organize_status(
    arguments: dict[str, Any], context: ToolContext | None = None
) -> ToolResult:
    """读取光鸭整理任务、持久化操作与调度器的脱敏运行快照。"""
    context = context or ToolContext()
    from app.modules.organize_tasks import get_organize_manager

    manager = get_organize_manager()
    operation_ref = str(arguments.get("operation_ref") or "").strip().upper()
    overview = manager.status()
    raw = (
        manager.task_result(operation_ref, owner=context.owner)
        if operation_ref
        else overview
    )
    if operation_ref and raw is None:
        return ToolResult(
            ok=False,
            status="empty",
            summary="没有找到这个光鸭操作编号",
            data={"operation_ref": operation_ref, "found": False},
            evidence=[
                Evidence(
                    "guangya_organizer",
                    "已按公开操作编号查询持久化任务；未返回目录、内部任务标识或错误正文。",
                    _now(),
                )
            ],
            suggestions=["请核对操作编号，或直接查看当前光鸭整理状态。"],
        )
    raw = raw or {}
    task_status = _safe_choice(
        raw.get("status"),
        {
            "idle",
            "queued",
            "running",
            "stopping",
            "completed",
            "partial",
            "stopped",
            "failed",
            "cancelled",
            "manual_review",
        },
        "idle",
    )
    running = task_status in {"running", "stopping"}
    allowed_stats = {
        "total",
        "matched",
        "need_confirm",
        "moved",
        "renamed",
        "rename_failed",
        "metadata_moved",
        "stopped",
        "skipped",
        "conflict",
        "failed",
        "subtitle_moved",
        "subtitle_skipped",
        "replacement_cleanup_failed",
        "empty_dir_cleanup_failed",
        "source_dir_cleanup_failed",
        "audit_failures",
    }
    stats = (
        {
            key: _bounded_int(value)
            for key, value in (raw.get("stats") or {}).items()
            if key in allowed_stats
        }
        if isinstance(raw.get("stats"), dict)
        else {}
    )
    if not stats and isinstance(raw.get("result"), dict):
        persisted_stats = raw["result"].get("stats")
        if isinstance(persisted_stats, dict):
            stats = {
                key: _bounded_int(value)
                for key, value in persisted_stats.items()
                if key in allowed_stats
            }

    schedule_raw = (
        overview.get("schedule") if isinstance(overview.get("schedule"), dict) else {}
    )
    schedule = {
        "enabled": bool(schedule_raw.get("enabled")),
        "configured": not bool(schedule_raw.get("config_error")),
        "cron_valid": bool(schedule_raw.get("cron_valid")),
        "next_run": _safe_timestamp(schedule_raw.get("next_run")),
    }
    queue_raw = overview.get("operation_queue")
    queue_total = (
        _bounded_int(queue_raw.get("total")) if isinstance(queue_raw, dict) else 0
    )

    if running:
        ok, status, summary = True, "running", "光鸭整理任务正在运行"
        suggestions: list[str] = []
    elif task_status == "queued":
        ok, status, summary = True, "queued", "光鸭整理操作正在排队"
        suggestions = ["任务会在当前整理操作结束后自动执行。"]
    elif task_status == "manual_review":
        ok, status, summary = False, "attention", "光鸭操作在进程中断后需要人工核验"
        suggestions = ["请先核对光鸭目标目录，确认远端结果后再决定是否重新执行。"]
    elif task_status == "failed":
        ok, status, summary = False, "attention", "最近一次光鸭整理任务未成功"
        suggestions = ["请到网盘整理页查看任务详情后再决定是否重试。"]
    elif task_status == "completed":
        ok, status, summary = True, "completed", "最近一次光鸭整理任务已完成"
        suggestions = []
    elif task_status == "partial":
        ok, status, summary = False, "attention", "最近一次光鸭整理任务部分完成"
        suggestions = ["请到网盘整理页核对失败项后再决定是否重试。"]
    elif task_status in {"stopped", "cancelled"}:
        ok, status, summary = True, "stopped", "最近一次光鸭整理任务已停止"
        suggestions = []
    else:
        ok, status, summary = (
            True,
            "idle",
            (
                f"光鸭整理任务当前空闲，另有 {queue_total} 项操作排队"
                if queue_total
                else "光鸭整理任务当前空闲"
            ),
        )
        suggestions = []

    task_data = {
        "status": task_status,
        "running": running,
        "stoppable": bool(raw.get("stoppable")) if running else False,
        "trigger_type": _safe_choice(
            raw.get("trigger_type"), {"manual", "cron", "telegram"}
        ),
        "started_at": _safe_timestamp(raw.get("started_at")),
        "finished_at": _safe_timestamp(raw.get("finished_at")),
        "stats": stats,
    }
    if operation_ref:
        task_data["operation_ref"] = operation_ref
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "task": task_data,
            "queue": {"pending_count": queue_total},
            "schedule": schedule,
        },
        evidence=[
            Evidence(
                "guangya_organizer",
                "读取光鸭整理任务脱敏快照；仅在用户提供时返回公开操作编号，不返回目录、内部任务标识或错误正文。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )
