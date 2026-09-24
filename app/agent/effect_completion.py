"""确认写操作的统一后台终态收敛。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from app.agent.models import ToolContext, ToolReference, ToolResult
from app.agent.public_safety import sanitize_public_text

_GY_OPERATION_REF_RE = re.compile(r"GY-(?:[0-9A-F]{4}-){7}[0-9A-F]{4}")
_WAITABLE_STATUSES = frozenset(
    {"accepted", "queued", "running", "in_progress", "retry_wait"}
)
_WAIT_TIMEOUT_SECONDS = 30 * 60
_WAIT_INTERVAL_SECONDS = 1.0
_TRACKER_KEY = "completion"
_TRACKER_KINDS = frozenset(
    {
        "guangya_organize_task",
        "local_media_task",
        "agent_job",
        "strm_run",
        "library_patrol",
    }
)
_ACTIVE_STATUSES = {
    "guangya_operation": frozenset({"queued", "running", "stopping"}),
    "guangya_task": frozenset({"running"}),
    "guangya_organize_task": frozenset({"queued", "running", "stopping"}),
    "local_media_task": frozenset({"running"}),
    "agent_job": frozenset({"pending", "running", "retry_wait"}),
    "strm_run": frozenset({"queued", "running"}),
    "library_patrol": frozenset({"queued", "running"}),
}
_TERMINAL_STATUSES = {
    "guangya_operation": frozenset(
        {"completed", "partial", "failed", "cancelled", "manual_review", "stopped"}
    ),
    "guangya_task": frozenset({"completed", "failed"}),
    "guangya_organize_task": frozenset(
        {"completed", "partial", "failed", "cancelled", "manual_review", "stopped"}
    ),
    "local_media_task": frozenset({"completed", "failed", "manual_review"}),
    "agent_job": frozenset(
        {"updates_available", "up_to_date", "inconclusive", "cancelled", "failed"}
    ),
    "strm_run": frozenset({"completed", "partial", "failed"}),
    "library_patrol": frozenset(
        {
            "updates_available",
            "up_to_date",
            "inconclusive",
            "not_configured",
            "unavailable",
            "failed",
        }
    ),
}
_STRM_STAT_FIELDS = frozenset(
    {
        "directories",
        "scanned_files",
        "generated",
        "created",
        "updated",
        "skipped",
        "failed",
        "metadata_generated",
        "metadata_queued",
        "metadata_failed",
        "metadata_cleaned",
        "cleaned",
        "empty_dirs_cleaned",
        "read_retries",
        "read_failures",
        "scan_workers_peak",
        "scan_workers_configured",
    }
)


@dataclass(frozen=True)
class _CompletionTracker:
    kind: str
    value: dict[str, Any]


def _bounded_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _reference_value(result: ToolResult, kind: str) -> dict[str, Any]:
    for reference in result.references:
        if not isinstance(reference, ToolReference) or reference.kind != kind:
            continue
        if isinstance(reference.value, dict):
            return dict(reference.value)
    return {}


def _completion_tracker(result: ToolResult) -> _CompletionTracker | None:
    data = result.data if isinstance(result.data, dict) else {}
    operation_ref = str(data.get("operation_ref") or "").strip().upper()
    if _GY_OPERATION_REF_RE.fullmatch(operation_ref):
        return _CompletionTracker("guangya_operation", {"operation_ref": operation_ref})

    provider = _reference_value(result, "guangya_task")
    if str(provider.get("task_id") or "").strip():
        return _CompletionTracker("guangya_task", provider)

    metadata = (
        result.effect_metadata if isinstance(result.effect_metadata, dict) else {}
    )
    raw = metadata.get(_TRACKER_KEY)
    if not isinstance(raw, Mapping):
        return None
    kind = str(raw.get("kind") or "").strip()
    if kind not in _TRACKER_KINDS:
        return None
    return _CompletionTracker(kind, dict(raw))


def _snapshot_task(snapshot: ToolResult) -> tuple[str, dict[str, Any]]:
    payload = snapshot.data if isinstance(snapshot.data, dict) else {}
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    status = str(task.get("status") or snapshot.status or "").strip().casefold()
    return status, dict(task)


def _background_data(
    result: ToolResult,
    snapshot: ToolResult | None,
    status: str,
    task: dict[str, Any],
    tracker: _CompletionTracker,
    *,
    timed_out: bool = False,
) -> dict[str, Any]:
    data = dict(result.data) if isinstance(result.data, dict) else {}
    operation_ref = str(tracker.value.get("operation_ref") or "").strip().upper()
    if operation_ref:
        # execute 的公开范围沿用了 preview DTO；终态不能继续声称未写云端。
        data.pop("cloud_write", None)
        data["operation_ref"] = operation_ref
    job = {"status": status, "last_status": status, "timed_out": timed_out}
    for key in ("stats", "started_at", "finished_at", "progress"):
        value = task.get(key)
        if value not in (None, ""):
            job[key] = (
                dict(value) if key == "stats" and isinstance(value, dict) else value
            )
    if isinstance(task.get("stats"), dict):
        data["stats"] = dict(task["stats"])
    data["background_job"] = job
    if tracker.kind == "guangya_task":
        data["verified"] = status == "completed"
        data["verification_pending"] = status not in {"completed", "failed"}
    return data


def _background_model_data(
    result: ToolResult, data: dict[str, Any]
) -> dict[str, Any] | None:
    if not isinstance(result.model_data, dict):
        return result.model_data
    model_data = dict(result.model_data)
    model_data.pop("cloud_write", None)
    for key in (
        "operation_ref",
        "stats",
        "background_job",
        "verified",
        "verification_pending",
    ):
        if key in data:
            model_data[key] = data[key]
    return model_data


def _provider_summary(
    result: ToolResult, tracker: _CompletionTracker, status: str
) -> str:
    operation = str(tracker.value.get("operation") or "").strip().casefold()
    count = (
        _bounded_int(result.data.get("count")) if isinstance(result.data, dict) else 0
    )
    if status == "completed":
        if operation == "recycle_clear":
            return f"光鸭回收站已清空，共永久删除 {count} 个对象"
        if operation == "recycle_restore":
            return f"已从光鸭回收站恢复 {count} 个对象"
        return "光鸭后台任务已完成"
    if operation == "recycle_clear":
        return "光鸭回收站清空任务执行失败"
    if operation == "recycle_restore":
        return "光鸭回收站恢复任务执行失败"
    return "光鸭后台任务执行失败"


def _row_value(row: Mapping[str, Any], key: str, default: Any = "") -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _safe_strm_result(row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    raw_status = str(_row_value(row, "status")).strip().casefold()
    status = {
        "success": "completed",
        "completed": "completed",
        "partial": "partial",
        "failed": "failed",
        "skipped": "failed",
        "running": "running",
    }.get(raw_status, "unknown")
    try:
        payload = json.loads(str(_row_value(row, "result", "{}") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    raw_stats = payload.get("stats") if isinstance(payload, dict) else None
    stats = (
        {
            key: _bounded_int(value)
            for key, value in raw_stats.items()
            if key in _STRM_STAT_FIELDS
        }
        if isinstance(raw_stats, dict)
        else {}
    )
    return status, stats


def _strm_status(tracker: _CompletionTracker) -> ToolResult:
    from app import database as db
    from app.modules.scheduler import get_scheduler

    after_run_id = _bounded_int(tracker.value.get("after_run_id"))
    trigger_type = str(tracker.value.get("trigger_type") or "manual")
    rows = [
        item
        for item in db.list_task_runs("strm_sync", limit=20)
        if _bounded_int(item["id"]) > after_run_id
        and str(item["trigger_type"] or "") == trigger_type
    ]
    # trigger() 成功取得全局执行锁后，本次运行必然是基线后的第一条同类记录；
    # 选择最早记录，避免任务很快结束后另一轮人工同步被误认成本次任务。
    row = min(rows, key=lambda item: _bounded_int(item["id"]), default=None)
    runtime = get_scheduler().status()
    if row is None:
        status = "running" if bool(runtime.get("running")) else "queued"
        return ToolResult(
            True,
            status,
            "STRM 同步任务正在运行" if status == "running" else "STRM 同步任务正在排队",
            data={
                "task": {
                    "status": status,
                    "progress": dict(runtime.get("progress") or {}),
                    "stats": {},
                }
            },
        )

    status, stats = _safe_strm_result(row)
    summary = {
        "running": "STRM 同步任务正在运行",
        "completed": "STRM 同步已完成",
        "partial": "STRM 同步部分完成",
        "failed": "STRM 同步执行失败",
        "unknown": "STRM 同步状态暂时无法确认",
    }[status]
    return ToolResult(
        status in {"running", "completed"},
        status,
        summary,
        data={
            "task": {
                "status": status,
                "started_at": str(row["started_at"] or ""),
                "finished_at": str(row["finished_at"] or ""),
                "progress": dict(runtime.get("progress") or {})
                if status == "running"
                else {},
                "stats": stats,
            }
        },
        error="STRM 同步未成功完成。" if status in {"partial", "failed"} else "",
    )


def _patrol_status(tracker: _CompletionTracker) -> ToolResult:
    from app import database as db
    from app.agent.library_patrol_status import get_library_patrol_status

    row = db.get_agent_library_patrol()
    if row is None:
        return ToolResult(False, "unknown", "全库巡检状态暂时无法确认")
    baseline_generation = _bounded_int(tracker.value.get("lease_generation"))
    baseline_status = str(tracker.value.get("task_status") or "")
    baseline_finished = str(tracker.value.get("last_finished_at") or "")
    current_generation = _bounded_int(row["lease_generation"])
    current_status = str(row["status"] or "pending")
    current_finished = str(row["last_finished_at"] or "")
    finished = bool(
        current_finished
        and current_finished != baseline_finished
        and current_status != "running"
        and (current_generation > baseline_generation or baseline_status == "running")
    )
    if not finished:
        status = "running" if current_status == "running" else "queued"
        return ToolResult(
            True,
            status,
            "全库缺集巡检正在运行" if status == "running" else "全库缺集巡检正在排队",
            data={
                "task": {
                    "status": status,
                    "started_at": str(row["last_started_at"] or ""),
                    "finished_at": "",
                    "stats": {
                        "checked_series": _bounded_int(row["checked_series_count"]),
                    },
                }
            },
        )
    result = get_library_patrol_status({})
    data = dict(result.data) if isinstance(result.data, dict) else {}
    data["task"] = {
        "status": str(result.status or ""),
        "started_at": str(row["last_started_at"] or ""),
        "finished_at": current_finished,
        "stats": {
            "checked_series": _bounded_int(row["checked_series_count"]),
            "updates_available": _bounded_int(row["updates_available_count"]),
            "missing_episodes": _bounded_int(row["missing_episode_count"]),
        },
    }
    return replace(result, data=data)


async def _poll(
    tracker: _CompletionTracker, context: ToolContext
) -> tuple[ToolResult, str, dict[str, Any]]:
    if tracker.kind == "guangya_operation":
        from app.agent.domain_catalog.cloud_runtime import guangya_organize_status

        snapshot = await asyncio.to_thread(
            guangya_organize_status,
            {"operation_ref": tracker.value["operation_ref"]},
            context=context,
        )
        status, task = _snapshot_task(snapshot)
        return snapshot, status, task
    if tracker.kind == "guangya_task":
        from app.agent.guangya_recycle_actions import query_guangya_task_status

        snapshot = await asyncio.to_thread(
            query_guangya_task_status,
            {"guangya_task": tracker.value},
            context,
        )
        status = str(snapshot.status or "").strip().casefold()
        return (
            snapshot,
            status,
            dict(snapshot.data) if isinstance(snapshot.data, dict) else {},
        )
    if tracker.kind == "guangya_organize_task":
        from app.agent.domain_catalog.cloud_runtime import guangya_organize_task_status

        snapshot = await asyncio.to_thread(
            guangya_organize_task_status, str(tracker.value.get("task_id") or "")
        )
        status, task = _snapshot_task(snapshot)
        return snapshot, status, task
    if tracker.kind == "local_media_task":
        from app.agent.local_media_task_actions import local_media_completion_status

        snapshot = await asyncio.to_thread(
            local_media_completion_status, tracker.value, context
        )
        status, task = _snapshot_task(snapshot)
        return snapshot, status, task
    if tracker.kind == "agent_job":
        from app.agent.durable_job_actions import get_agent_job_status

        snapshot = await asyncio.to_thread(
            get_agent_job_status,
            {"job_id": str(tracker.value.get("job_id") or ""), "limit": 1},
            context,
        )
        status = str(snapshot.status or "").strip().casefold()
        return (
            snapshot,
            status,
            dict(snapshot.data) if isinstance(snapshot.data, dict) else {},
        )
    if tracker.kind == "strm_run":
        snapshot = await asyncio.to_thread(_strm_status, tracker)
        return (
            snapshot,
            str(snapshot.status or "").strip().casefold(),
            _snapshot_task(snapshot)[1],
        )
    if tracker.kind == "library_patrol":
        snapshot = await asyncio.to_thread(_patrol_status, tracker)
        status = str(snapshot.status or "").strip().casefold()
        return snapshot, status, _snapshot_task(snapshot)[1]
    raise RuntimeError("unsupported completion tracker")


def _terminal_result(
    result: ToolResult,
    snapshot: ToolResult,
    status: str,
    task: dict[str, Any],
    tracker: _CompletionTracker,
) -> ToolResult:
    public_status = status
    ok = bool(snapshot.ok)
    summary = sanitize_public_text(snapshot.summary, limit=500) or result.summary
    suggestions = list(dict.fromkeys([*result.suggestions, *snapshot.suggestions]))
    error = snapshot.error or result.error
    operation = str(tracker.value.get("operation") or "").strip().casefold()

    if tracker.kind == "guangya_task":
        ok = status == "completed"
        summary = _provider_summary(result, tracker, status)
        suggestions = [] if ok else ["请核对光鸭回收站状态后再决定是否重新执行。"]
        error = "" if ok else "Provider 报告后台任务执行失败"
    elif tracker.kind == "guangya_operation":
        summary = {
            "completed": "光鸭后台任务已完成",
            "partial": "光鸭后台任务部分完成",
            "failed": "光鸭后台任务执行失败",
            "cancelled": "光鸭后台任务已取消",
            "manual_review": "光鸭后台任务结果未知，需要人工核验",
            "stopped": "光鸭后台任务已停止",
        }.get(status, summary)
        ok = status in {"completed", "stopped"}
        error = "" if ok else error or "请核对任务统计和失败项"
    elif tracker.kind == "guangya_organize_task":
        if operation == "stop" and status in {"completed", "stopped", "cancelled"}:
            ok, public_status, summary, error = (
                True,
                "completed",
                "光鸭整理任务已停止",
                "",
            )
        elif status == "completed":
            summary = "回退任务已完成" if operation == "undo" else "光鸭整理任务已完成"
            ok, error = True, ""
    elif tracker.kind == "agent_job" and operation == "cancel":
        if status in {"cancelled", "updates_available", "up_to_date", "inconclusive"}:
            ok, public_status, error = True, "completed", ""
            if status == "cancelled":
                summary = "全库检查已取消"
            else:
                summary = f"全库检查已在取消生效前结束：{summary}"

    data = _background_data(result, snapshot, status, task, tracker)
    return replace(
        result,
        ok=ok,
        status=public_status,
        summary=summary,
        data=data,
        model_data=_background_model_data(result, data),
        suggestions=suggestions,
        error=error,
    )


async def wait_for_effect_completion(
    result: ToolResult,
    *,
    tool: str,
    context: ToolContext,
    report_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    timeout_seconds: float = _WAIT_TIMEOUT_SECONDS,
) -> ToolResult:
    """追踪有稳定句柄的后台写操作；纯提交结果原样交给公开语义层。"""
    status = str(result.status or "").strip().casefold()
    tracker = _completion_tracker(result)
    if not result.ok or status not in _WAITABLE_STATUSES or tracker is None:
        return result

    active_statuses = _ACTIVE_STATUSES[tracker.kind]
    terminal_statuses = _TERMINAL_STATUSES[tracker.kind]
    label = sanitize_public_text(result.summary, limit=120) or "后台任务"

    async def report(snapshot: ToolResult, task_status: str) -> None:
        if report_progress is None:
            return
        payload = {
            "phase": "background_job",
            "tool": str(tool or "")[:120],
            "status": task_status,
            "summary": sanitize_public_text(snapshot.summary, limit=240)
            or "后台任务状态已更新",
        }
        operation_ref = str(tracker.value.get("operation_ref") or "").strip().upper()
        if operation_ref:
            payload["operation_ref"] = operation_ref
        try:
            await report_progress(payload)
        except Exception:  # noqa: BLE001 - 进度通道故障不应改写业务终态
            return

    def unknown(
        last_status: str,
        snapshot: ToolResult | None = None,
        task: dict[str, Any] | None = None,
        *,
        timed_out: bool = False,
    ) -> ToolResult:
        job_status = last_status or "unknown"
        data = _background_data(
            result,
            snapshot,
            job_status,
            task or {},
            tracker,
            timed_out=timed_out,
        )
        suggestions = list(
            dict.fromkeys(
                [
                    *result.suggestions,
                    "可继续询问刚才的任务状态；确认终态前请勿重复提交。",
                ]
            )
        )
        return replace(
            result,
            ok=False,
            status="outcome_unknown",
            summary=(
                f"{label}仍在运行，等待已达上限，结果尚未确认"
                if timed_out and job_status in active_statuses
                else f"{label}状态暂时未知，结果尚未确认"
            ),
            data=data,
            model_data=_background_model_data(result, data),
            suggestions=suggestions,
            error=result.error or "后台任务尚未返回可信终态",
        )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout_seconds))
    last_status = ""
    last_snapshot: ToolResult | None = None
    last_task: dict[str, Any] = {}
    while True:
        if context.cancelled():
            raise asyncio.CancelledError
        try:
            snapshot, task_status, task = await _poll(tracker, context)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 查询异常只能安全降级为未知
            return unknown(last_status, last_snapshot, last_task)

        last_snapshot, last_task = snapshot, task
        if task_status in terminal_statuses:
            await report(snapshot, task_status)
            return _terminal_result(result, snapshot, task_status, task, tracker)
        if task_status not in active_statuses:
            return unknown(
                last_status
                or ("unknown" if task_status in {"", "empty", "idle"} else task_status),
                snapshot,
                task,
            )

        last_status = task_status
        await report(snapshot, task_status)
        remaining = deadline - loop.time()
        if remaining <= 0:
            return unknown(last_status, snapshot, task, timed_out=True)
        await asyncio.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))
