"""光鸭整理运行状态的安全领域投影。"""

from __future__ import annotations

from typing import Any

from app.agent.models import Evidence, ToolContext, ToolResult
from app.repositories.organize_operation_jobs import CLOUD_COPY_PENDING_CODE, sanitize_organize_operation_result

from .shared import _bounded_int, _now, _safe_choice, _safe_timestamp

_TASK_STATUSES = {
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
}


def _safe_result(raw: dict[str, Any]) -> dict[str, Any]:
    stats = raw.get("stats")
    nested_result = raw.get("result")
    if not stats and isinstance(nested_result, dict):
        result = nested_result
        stats = result.get("stats")
        if not isinstance(stats, dict):
            stats = result.get("counters")

    payload: dict[str, Any] = {}
    if isinstance(stats, dict):
        payload["stats"] = {key: _bounded_int(value) for key, value in stats.items()}
    if "operation_items" in raw:
        payload["operation_items"] = raw["operation_items"]
    elif isinstance(nested_result, dict) and "operation_items" in nested_result:
        payload["operation_items"] = nested_result["operation_items"]

    return sanitize_organize_operation_result(payload)


def _project_guangya_status(
    raw: dict[str, Any],
    *,
    overview: dict[str, Any],
    operation_ref: str = "",
) -> ToolResult:
    task_status = _safe_choice(raw.get("status"), _TASK_STATUSES, "idle")
    running = task_status in {"running", "stopping"}
    safe_result = _safe_result(raw)
    stats = safe_result.get("stats", {})
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
        if raw.get("error_code") == CLOUD_COPY_PENDING_CODE:
            summary = "光鸭复制仍在处理中，完成后将继续原计划"
            suggestions = ["系统会继续跟踪原任务；无需再次确认或重复提交复制。"]
    elif task_status == "manual_review":
        ok, status, summary = False, "attention", "光鸭操作需要进一步核验"
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

    if stats.get("strm_scope_unknown"):
        suggestions.append(
            "文件变更结果已记录，但同步范围未能确认，本次未触发 STRM 联动；请核对同步目录后手动同步。"
        )

    # 只公开有明确统计依据的故障阶段，不转发 Provider 原始异常或私有路径。
    problems = []
    if task_status in {"partial", "failed", "manual_review"}:
        if raw.get("error_code") == "GuangYaFSChangeStale":
            attempted = any(item.get("status") not in {"not_started", "blocked"} for item in safe_result.get("operation_items", []))
            problems.append(
                "执行期间凭据或对象状态已变化；已发生的变更以逐项回执为准，剩余操作未继续"
                if attempted else "冻结计划、凭据或对象状态已变化，本次变更未执行；请重新读取目录并生成预览"
            )
        for key, description in (
            ("precondition_failed", "项写前核对未通过，后续动作未执行"),
            ("verification_failed", "项写后状态未核验通过，不能据此认定未执行，请勿直接重复提交"),
            ("audit_failures", "项执行审计未完整保存，需要核对实际状态"),
        ):
            if stats.get(key):
                problems.append(f"{stats[key]} {description}")

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
    if "operation_items" in safe_result:
        task_data["operation_items"] = safe_result["operation_items"]
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
        error="；".join(problems),
    )


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
    return _project_guangya_status(
        raw or {}, overview=overview, operation_ref=operation_ref
    )


def guangya_organize_task_status(task_id: str) -> ToolResult:
    """按 Kernel 私有任务 ID 读取普通整理任务终态，不公开该 ID。"""
    from app.modules.organize_tasks import get_organize_manager

    manager = get_organize_manager()
    raw = manager.task_result(str(task_id or "").strip())
    if raw is None:
        return ToolResult(
            ok=False,
            status="unknown",
            summary="光鸭整理任务状态暂时无法确认",
            error="未找到对应的后台任务快照。",
        )
    return _project_guangya_status(raw, overview=manager.status())
