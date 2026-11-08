"""已受理后台任务的领域观察与终态解释；不登记会话、不创建或执行写操作。"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from app.agent.models import Evidence, ToolContext, ToolReference, ToolResult
from app.agent.public_safety import sanitize_public_text

_GY_OPERATION_REF_RE = re.compile(r"GY-(?:[0-9A-F]{4}-){7}[0-9A-F]{4}")


_TRACKER_KEY = "completion"


_TRACKER_KINDS = frozenset(
    {
        "guangya_organize_task",
        "local_media_task",
        "local_media_scan",
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
    "local_media_scan": frozenset({"running"}),
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
        {"completed", "partial", "failed", "cancelled", "manual_review", "stopped", "outcome_unknown"}
    ),
    "local_media_task": frozenset({"completed", "failed", "manual_review"}),
    "local_media_scan": frozenset({"completed", "partial", "failed", "requires_manual"}),
    "agent_job": frozenset(
        {"updates_available", "up_to_date", "inconclusive", "cancelled", "failed"}
    ),
    "strm_run": frozenset({"completed", "partial", "failed", "outcome_unknown"}),
    "library_patrol": frozenset(
        {
            "cancelled",
            "updates_available",
            "up_to_date",
            "inconclusive",
            "not_configured",
            "unavailable",
            "failed",
            "outcome_unknown",
        }
    ),
}


_LOCAL_MEDIA_OUTCOME_FIELDS = (
    "file_outcome", "video_count", "archived_video_count",
    "skipped_video_count", "unknown_video_count",
)


@dataclass(frozen=True)
class _CompletionTracker:
    kind: str
    value: dict[str, Any]


def _bounded_int(value: Any) -> int:
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
    job: dict[str, Any] = {"status": status, "last_status": status, "timed_out": timed_out}
    for key in ("stats", "started_at", "finished_at", "progress"):
        value = task.get(key)
        if value not in (None, ""):
            job[key] = (
                dict(value) if key == "stats" and isinstance(value, dict) else value
            )
    if isinstance(task.get("stats"), dict):
        data["stats"] = dict(task["stats"])
    if isinstance(task.get("operation_items"), list):
        data["operation_items"] = task["operation_items"]
    if tracker.kind == "local_media_task":
        for key in _LOCAL_MEDIA_OUTCOME_FIELDS:
            if key in task:
                data[key] = job[key] = task[key]
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
        "operation_items",
        "verified",
        "verification_pending",
        "scope_note",
        *_LOCAL_MEDIA_OUTCOME_FIELDS,
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


def _strm_status(tracker: _CompletionTracker) -> ToolResult:
    from app.modules.scheduler import strm_status

    result = strm_status(_bounded_int(tracker.value.get("run_id")))
    return ToolResult(**result)


def _local_media_scan_status(tracker: _CompletionTracker) -> ToolResult:
    """只冻结指定 LM 回执的成员；轮询真实任务与文件事实，不使用通知旧快照。"""
    from app import database as db
    from app.modules.local_media_models import LOCAL_BUSY_TASK_STATUSES
    from app.modules.local_media_outcomes import local_media_task_outcome
    from app.modules.local_media_scan_runs import (
        local_media_scan_summary,
        resolve_local_media_scan,
    )

    if "task_ids" not in tracker.value:
        scan_ref = str(tracker.value.get("scan_ref") or "").strip()
        if not scan_ref:
            # resolve 的空引用表示最近扫描，此链路绝不能回退到其他批次。
            raise LookupError("缺少本次扫描回执")
        scan = resolve_local_media_scan(scan_ref, owner="admin")
        tracker.value["task_ids"] = tuple(scan["task_ids"])
        tracker.value["scan_summary"] = local_media_scan_summary(scan["summary"])
    membership = tracker.value["task_ids"]
    if not membership:
        return ToolResult(False, "unknown", "本次入队扫描没有可核验的任务成员")

    tasks = {}
    items: dict[int, list[Any]] = {}
    with db.get_conn() as conn:
        # 同一只读快照，避免任务状态与文件明细来自不同提交；不受列表 20 项限制。
        conn.execute("BEGIN")
        for offset in range(0, len(membership), 500):
            batch = membership[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            for row in conn.execute(
                f"SELECT * FROM local_media_tasks WHERE owner=? AND id IN ({placeholders})",
                ["admin", *batch],
            ).fetchall():
                tasks[int(row["id"])] = row
            for row in conn.execute(
                f"SELECT * FROM local_media_task_items WHERE owner=? AND task_id IN ({placeholders})",
                ["admin", *batch],
            ).fetchall():
                items.setdefault(int(row["task_id"]), []).append(row)

    missing = len(membership) - len(tasks)
    stats = {
        "total": len(membership), "completed": 0, "running": 0,
        "requires_manual": 0, "failed": 0, "unknown": missing,
        "missing_tasks": missing, "archived_video_count": 0,
        "skipped_video_count": 0, "unknown_video_count": 0,
        "failed_sources": _bounded_int(tracker.value.get("scan_summary", {}).get("failed_sources")),
        "enqueue_failures": _bounded_int(tracker.value.get("scan_summary", {}).get("enqueue_failures")),
    }
    for task_id, task in tasks.items():
        raw_status = str(task["status"])
        outcome = local_media_task_outcome(task, items.get(task_id, []))
        for key in ("archived_video_count", "skipped_video_count", "unknown_video_count"):
            stats[key] += outcome[key]
        if raw_status in LOCAL_BUSY_TASK_STATUSES:
            stats["running"] += 1
        elif raw_status == "completed" and (
            outcome["file_outcome"] in {"unknown", "preview_only"}
            or outcome["unknown_video_count"]
        ):
            stats["unknown"] += 1
        elif raw_status in {"requires_manual", "failed", "completed"}:
            stats[raw_status] += 1
        else:
            stats["unknown"] += 1

    status = next(
        (name for name in ("unknown", "running", "failed", "requires_manual") if stats[name]),
        "completed",
    )
    if status in {"failed", "requires_manual"} and stats["archived_video_count"]:
        status = "partial"
    if status == "completed" and stats["failed_sources"]:
        status = "partial"
    heading = {
        "unknown": "本次扫描的整理结果仍有未知项",
        "running": "本次扫描的整理任务仍在运行",
        "failed": "本次扫描有整理任务失败",
        "requires_manual": "本次扫描有整理任务需要人工确认",
        "partial": "本次扫描的整理任务部分完成",
        "completed": "本次扫描的整理任务已结束",
    }[status]
    return ToolResult(
        ok=status in {"running", "completed"},
        status=status,
        summary=(
            f"{heading}：共 {stats['total']} 个任务，"
            f"归档 {stats['archived_video_count']} 个视频，"
            f"冲突跳过 {stats['skipped_video_count']} 个视频，"
            f"来源失败 {stats['failed_sources']} 个，"
            f"失败 {stats['failed']} 个任务，待人工 {stats['requires_manual']} 个任务"
        ),
        data={"task": {"status": status, "stats": stats}},
        error="本次扫描尚未全部成功整理。" if status in {"partial", "failed", "requires_manual"} else "",
        suggestions=["请在本地媒体待确认页处理本批任务。"] if stats["requires_manual"] else [],
    )


def _patrol_status(tracker: _CompletionTracker) -> ToolResult:
    from app.modules.library_patrol import patrol_status

    value = patrol_status(_bounded_int(tracker.value.get("run_id")))
    return ToolResult(value["ok"], value["status"], value["summary"], data=value.get("data", {}),
                      evidence=[Evidence(**item) for item in value.get("evidence", [])],
                      suggestions=value.get("suggestions", []), error=value.get("error", ""))


async def _poll(
    tracker: _CompletionTracker, context: ToolContext
) -> tuple[ToolResult, str, dict[str, Any]]:
    requires_run_id = tracker.kind in {"strm_run", "library_patrol"} or (
        tracker.kind == "guangya_organize_task"
        and str(tracker.value.get("operation") or "").strip().casefold() == "run"
    )
    run_id = tracker.value.get("run_id")
    if requires_run_id and (type(run_id) is not int or run_id <= 0):
        snapshot = ToolResult(
            False, "outcome_unknown", "后台任务结果无法核验，自动跟踪已结束",
            error="缺少本次运行的可靠记录。",
            suggestions=["请核对原任务状态；未确认结果前不要重复提交。"],
        )
        return snapshot, snapshot.status, {"status": snapshot.status}
    if tracker.kind == "guangya_operation":
        from app.modules.organize_tasks import get_organize_operation_status

        snapshot = ToolResult(**(await asyncio.to_thread(
            get_organize_operation_status, tracker.value["operation_ref"], owner=context.owner,
        )))
        status, task = _snapshot_task(snapshot)
        return snapshot, status, task
    if tracker.kind == "guangya_task":
        from app.clients.guangya import GuangYaClient, close_guangya_client
        from app.modules.guangya_recycle import read_task

        def read_provider_task():
            client = GuangYaClient()
            try:
                return read_task(client, str(tracker.value["task_id"]))
            finally:
                close_guangya_client(client)

        state = await asyncio.to_thread(read_provider_task)
        status = state["status"]
        snapshot = ToolResult(status in {"completed", "running"}, status, {
            "completed": "光鸭任务已完成", "failed": "光鸭任务执行失败",
            "running": "光鸭任务仍在处理中", "unknown": "光鸭任务状态暂时无法确认，不能判断为正在运行或已完成",
        }[status], data={**state, "operation": str(tracker.value.get("operation") or "operation")})
        status = (snapshot.status or "").strip().casefold()
        return (
            snapshot,
            status,
            dict(snapshot.data) if isinstance(snapshot.data, dict) else {},
        )
    if tracker.kind == "guangya_organize_task":
        from app.modules.organize_results import observe_organize_task

        snapshot = ToolResult(**(await asyncio.to_thread(observe_organize_task, tracker.value)))
        status, task = _snapshot_task(snapshot)
        return snapshot, status, task
    if tracker.kind == "local_media_task":
        from app.modules.local_media_tasks import local_task_completion
        snapshot = ToolResult(**(await asyncio.to_thread(local_task_completion, int(tracker.value.get("task_id") or 0))))
        status, task = _snapshot_task(snapshot)
        return snapshot, status, task
    if tracker.kind == "local_media_scan":
        snapshot = await asyncio.to_thread(_local_media_scan_status, tracker)
        status, task = _snapshot_task(snapshot)
        return snapshot, status, task
    if tracker.kind == "agent_job":
        from app.modules.library_audit_jobs import audit_job

        payload = await asyncio.to_thread(
            audit_job, owner=context.owner,
            job_id=str(tracker.value.get("job_id") or ""), limit=1,
        )
        snapshot = ToolResult(**payload)
        return snapshot, payload["status"], dict(payload["data"])
    if tracker.kind == "strm_run":
        snapshot = await asyncio.to_thread(_strm_status, tracker)
        return (
            snapshot,
            (snapshot.status or "").strip().casefold(),
            _snapshot_task(snapshot)[1],
        )
    if tracker.kind == "library_patrol":
        snapshot = await asyncio.to_thread(_patrol_status, tracker)
        status = (snapshot.status or "").strip().casefold()
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
    ok = (snapshot.ok)
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
    if tracker.kind == "guangya_operation" and data.get("operation") == "filesystem_change":
        data["scope_note"] = (
            "本次仅核验文件系统变更；改名或移动不代表已完成元数据识别、刮削归档或媒体库入库。"
            "仍有这些步骤时应继续核验，不能保证下次扫库就能自动识别。"
        )
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
