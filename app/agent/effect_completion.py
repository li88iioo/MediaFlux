"""确认写操作的统一后台终态收敛。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from app.agent.models import Evidence, ToolContext, ToolReference, ToolResult
from app.agent.public_safety import sanitize_public_text

if TYPE_CHECKING:
    from cryptography.fernet import Fernet
    from app.agent.kernel.state import PublicationLease, SessionState, SessionStateStore

_GY_OPERATION_REF_RE = re.compile(r"GY-(?:[0-9A-F]{4}-){7}[0-9A-F]{4}")
_WAITABLE_STATUSES = frozenset(
    {"accepted", "queued", "running", "in_progress", "retry_wait"}
)
_WAIT_TIMEOUT_SECONDS = 30 * 60
_WAIT_INTERVAL_SECONDS = 1.0
_EFFECT_POLL_SECONDS = 10.0
_EFFECT_RETENTION_SECONDS = 24 * 60 * 60
_EFFECT_WAITS_KEY = "effect_waits"
_EFFECT_NEXT_POLL_KEY = "effect_next_poll_at"
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
        {"completed", "partial", "failed", "cancelled", "manual_review", "stopped"}
    ),
    "local_media_task": frozenset({"completed", "failed", "manual_review"}),
    "local_media_scan": frozenset({"completed", "partial", "failed", "requires_manual"}),
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
class EffectCompletionScope:
    """确认管道注入的原会话范围，不来自模型参数。"""

    store: SessionStateStore
    lease: PublicationLease
    plan_id: str
    channel: str = "api"


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
    if isinstance(task.get("operation_items"), list):
        data["operation_items"] = task["operation_items"]
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


def _local_media_scan_status(tracker: _CompletionTracker) -> ToolResult:
    """只冻结指定 LM 回执的成员；轮询真实任务与文件事实，不使用通知旧快照。"""
    from app import database as db
    from app.modules.local_media_models import LOCAL_BUSY_TASK_STATUSES
    from app.modules.local_media_outcomes import local_media_task_outcome
    from app.modules.local_media_scan_runs import resolve_local_media_scan

    if "task_ids" not in tracker.value:
        scan_ref = str(tracker.value.get("scan_ref") or "").strip()
        if not scan_ref:
            # resolve 的空引用表示最近扫描，此链路绝不能回退到其他批次。
            raise LookupError("缺少本次扫描回执")
        scan = resolve_local_media_scan(scan_ref, owner="admin")
        tracker.value["task_ids"] = tuple(scan["task_ids"])
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
            f"失败 {stats['failed']} 个任务，待人工 {stats['requires_manual']} 个任务"
        ),
        data={"task": {"status": status, "stats": stats}},
        error="本次扫描尚未全部成功整理。" if status in {"partial", "failed", "requires_manual"} else "",
        suggestions=["请在本地媒体待确认页处理本批任务。"] if stats["requires_manual"] else [],
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
    if tracker.kind == "local_media_scan":
        snapshot = await asyncio.to_thread(_local_media_scan_status, tracker)
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


def _completion_cipher() -> Fernet:
    from cryptography.fernet import Fernet
    from app.modules.web_secret import get_web_secret

    secret = str(get_web_secret() or "")
    if not secret:
        raise ValueError("Agent 回执持久化密钥不可用")
    key = hashlib.sha256(b"mediaflux-agent-effect-wait:v1\0" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _next_effect_poll(state: SessionState) -> None:
    waits = state.metadata.get(_EFFECT_WAITS_KEY) or {}
    due = [float(row["next_poll_at"]) for row in waits.values() if not row.get("delivered")]
    if due:
        state.metadata[_EFFECT_NEXT_POLL_KEY] = min(due)
    else:
        state.metadata.pop(_EFFECT_NEXT_POLL_KEY, None)


def _effect_seed(result: ToolResult, tracker: _CompletionTracker, tool: str,
                 scope: EffectCompletionScope) -> dict[str, Any]:
    from app.agent.kernel.projection import DefaultProjector

    return {
        "owner": scope.lease.owner, "session_id": scope.lease.session_id,
        "request_id": scope.lease.request_id, "turn_id": scope.lease.turn_id,
        "plan_id": scope.plan_id, "channel": scope.channel, "tool": str(tool),
        "tracker": {"kind": tracker.kind, "value": tracker.value},
        "result": dict(DefaultProjector().project(result).public_content),
    }


def _seed_result(seed: Mapping[str, Any]) -> ToolResult:
    value = seed["result"]
    return ToolResult(
        bool(value["ok"]), str(value["status"]), str(value["summary"]),
        data=dict(value.get("data") or {}),
        evidence=[Evidence(**item) for item in value.get("evidence") or []],
        suggestions=list(value.get("suggestions") or []), error=str(value.get("error") or ""),
    )


async def _register_effect_wait(result: ToolResult, tracker: _CompletionTracker, tool: str,
                                scope: EffectCompletionScope) -> None:
    from app.agent.kernel.state import StalePublicationError, publication_matches

    now = time.time()
    sealed = _completion_cipher().encrypt(json.dumps(
        _effect_seed(result, tracker, tool, scope), ensure_ascii=False, allow_nan=False,
        separators=(",", ":"),
    ).encode()).decode()
    def register(state):
        if not publication_matches(scope.lease, generation=state.generation,
                                   confirmed=state.metadata.get("confirmed_publication")):
            raise StalePublicationError("确认回合已变化，未注册新的结果跟踪")
        waits = state.metadata.setdefault(_EFFECT_WAITS_KEY, {})
        if scope.plan_id not in waits:
            waits[scope.plan_id] = {
                "sealed": sealed, "created_at": now, "expires_at": now + _EFFECT_RETENTION_SECONDS,
                "next_poll_at": now + _EFFECT_POLL_SECONDS, "delivered": False,
                "state": "pending", "last_status": "", "read_errors": 0,
            }
        _next_effect_poll(state)
        return True
    registered = await scope.store.update_effect_state(owner=scope.lease.owner,
        session_id=scope.lease.session_id, change=register)
    if registered is not True:
        raise StalePublicationError("原会话已清理，未重建结果跟踪")


async def _record_effect_observation(
    scope: EffectCompletionScope | None, *, status: str, final: ToolResult | None = None,
    due_now: bool = False,
) -> None:
    if scope is None:
        return
    from app.agent.kernel.projection import DefaultProjector

    def update(state):
        row = (state.metadata.get(_EFFECT_WAITS_KEY) or {}).get(scope.plan_id)
        if row is None:
            return False
        row["last_status"] = str(status or "unknown")
        row["next_poll_at"] = time.time() + (0 if due_now else _EFFECT_POLL_SECONDS)
        if final is not None:
            row.update(state="terminal", final_result=dict(DefaultProjector().project(final).public_content),
                       foreground_final=True)
        _next_effect_poll(state)
        return True
    updated = await scope.store.update_effect_state(owner=scope.lease.owner,
        session_id=scope.lease.session_id, change=update)
    if updated is not True:
        raise asyncio.CancelledError("原会话结果跟踪已清理")


async def remember_effect_receipt(store, *, owner, session_id, plan_id, message):
    """先保存投影后的完整回执模板，再写会话，崩溃后仍能补回opaque refs等投影。"""
    def remember(state):
        row = (state.metadata.get(_EFFECT_WAITS_KEY) or {}).get(plan_id)
        if row and row.get("state") == "terminal":
            row["receipt_template"] = dict(message)
            return True
        return False
    return await store.update_effect_state(owner=owner, session_id=session_id, change=remember)


async def acknowledge_effect_receipt(store, *, owner, session_id, plan_id):
    """仅在前台终态会话已落盘后清理等待记录；超时的占位回执不能ACK。"""
    def acknowledge(state):
        waits = state.metadata.get(_EFFECT_WAITS_KEY) or {}
        row = waits.get(plan_id)
        if row and row.get("state") == "terminal" and row.get("foreground_final"):
            waits.pop(plan_id)
            _next_effect_poll(state)
            return True
        return False
    return await store.update_effect_state(owner=owner, session_id=session_id, change=acknowledge)


def _effect_reply_key(seed: Mapping[str, Any]) -> str:
    from app.agent.kernel.persistence import SQLiteKernelStore

    owner, session = SQLiteKernelStore()._scope(seed["owner"], seed["session_id"])
    plan = hashlib.sha256(seed["plan_id"].encode()).hexdigest()
    return f"agent-effect:{owner}:{session}:{plan}"


def agent_effect_reply_is_current(event_key: str) -> bool:
    """Outbox 投递前核对签名会话，删除/重置后的迟到通知不再发送。"""
    from app import database as db
    from app.agent.kernel.persistence import SQLiteKernelStore

    match = re.fullmatch(
        r"tg:event:agent:agent-effect:([0-9a-f]{64}):([0-9a-f]{64}):([0-9a-f]{64}):[0-9a-f]{16}",
        str(event_key),
    )
    if match is None:
        return False
    owner, session, plan = match.groups()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT generation,state_json,state_hmac FROM agent_kernel_sessions "
            "WHERE owner_digest=? AND session_digest=?", (owner, session),
        ).fetchone()
    if row is None:
        return False
    try:
        payload = SQLiteKernelStore()._decode(
            row["state_json"], row["state_hmac"],
            domain=f"state:v1:{owner}:{session}:{row['generation']}".encode(), expected_type=dict,
        )
    except (ValueError, TypeError):
        return False
    return any(
        hashlib.sha256(str(message.get("completion_receipt_id") or "").encode()).hexdigest() == plan
        for message in payload.get("conversation") or []
    )


def _late_receipt(seed: Mapping[str, Any], public_result: Mapping[str, Any]) -> dict[str, Any]:
    from app.agent.kernel.model import ModelMessage
    from app.agent.public_view import _CONFIRMED_RESULT_MARKER, format_public_result

    message = ModelMessage(
        role="assistant", tool_name=seed["tool"], effect_plan_id=seed["plan_id"],
        completion_receipt_id=seed["plan_id"],
        content=_CONFIRMED_RESULT_MARKER + "\n" + json.dumps(public_result, ensure_ascii=False),
    ).to_dict()
    message["public_content"] = format_public_result(public_result)
    return message


def _enqueue_late_receipt(seed: Mapping[str, Any], message: Mapping[str, Any]) -> bool:
    from app.modules.telegram_notification_center import publish_agent_interaction_reply
    from app.notifier import NotificationEvent

    if seed["channel"] != "telegram":
        return True
    match = re.fullmatch(r"tg:v1:(-?[0-9]+)\x1f[0-9]+", seed["owner"])
    if match is None:
        # 只保留会话回执，不把无法确定接收人的交互回复发到默认通知群。
        return True
    published = publish_agent_interaction_reply(
        _effect_reply_key(seed), NotificationEvent(
            title="已确认任务的后续结果", lines=(message["public_content"],),
        ), chat_id=match.group(1), deliver_now=False,
    )
    # 用户关闭 TG Agent 时不排队补发旧交互，但 Web/历史中的任务事实仍保留。
    return bool(published) or published.status == "disabled"


async def poll_effect_receipts(
    store: SessionStateStore, *, cancelled: Callable[[], bool] = lambda: False, limit: int = 16,
) -> int:
    """复用现有调度器读取已提交任务；不调用 execute、不创建新确认或模型回合。"""
    from app.agent.kernel.projection import DefaultProjector
    from app.agent.kernel.state import SessionBusyError, merge_effect_receipts, retain_conversation
    from app.logger import get_logger
    import secrets

    semaphore = asyncio.Semaphore(4)

    async def follow(record):
        async with semaphore:
            if cancelled():
                return 0
            try:
                seed = json.loads(_completion_cipher().decrypt(record["sealed"].encode()))
                if seed["plan_id"] != record["plan_id"]:
                    return 0
                plan_id, claim_id = seed["plan_id"], secrets.token_hex(12)
                scope = {"owner": seed["owner"], "session_id": seed["session_id"]}

                def claim(state):
                    row = (state.metadata.get(_EFFECT_WAITS_KEY) or {}).get(plan_id)
                    if (not row or row.get("sealed") != record["sealed"] or row.get("delivered")
                            or float(row["next_poll_at"]) > time.time()):
                        return None
                    row.update(claim_id=claim_id, next_poll_at=time.time() + 60)
                    _next_effect_poll(state)
                    return dict(row)

                current = await store.update_effect_state(**scope, change=claim)
                if current is None:
                    return 0
                final = current.get("final_result")
                status = current.get("last_status", "unknown")
                errors = int(current.get("read_errors", 0))
                if final is None and not cancelled():
                    tracker = _CompletionTracker(**seed["tracker"])
                    result = _seed_result(seed)
                    try:
                        snapshot, status, task = await asyncio.wait_for(
                            _poll(tracker, ToolContext(**scope, request_id=seed["request_id"],
                                                       cancelled=cancelled)), timeout=15,
                        )
                        errors = 0
                        if status in _TERMINAL_STATUSES[tracker.kind]:
                            final = dict(DefaultProjector().project(
                                _terminal_result(result, snapshot, status, task, tracker),
                            ).public_content)
                    except (LookupError, ValueError):
                        # 失效/被清理的稳定引用不可改查最近任务，也不可无限重试。
                        current["expires_at"] = 0
                    except Exception:  # noqa: BLE001 - 读取失败不改变任务本身状态
                        errors += 1
                    if final is None and time.time() >= float(current["expires_at"]):
                        final = dict(DefaultProjector().project(ToolResult(
                            False, "outcome_unknown", "后台任务结果仍无法核验，自动跟踪已结束",
                            data={"original_summary": result.summary},
                            suggestions=["请核对原任务状态；未确认结果前不要重复提交。"],
                        )).public_content)
                if cancelled():
                    return 0

                def save(state):
                    waits = state.metadata.get(_EFFECT_WAITS_KEY) or {}
                    row = waits.get(plan_id)
                    if not row or row.get("claim_id") != claim_id:
                        return None
                    row.update(last_status=status, read_errors=errors,
                               next_poll_at=time.time() + min(60, _EFFECT_POLL_SECONDS * (errors + 1)))
                    if final is None:
                        _next_effect_poll(state)
                        return None
                    template = row.get("receipt_template")
                    if row.get("foreground_final") and template and template in state.conversation:
                        waits.pop(plan_id)
                        _next_effect_poll(state)
                        return None
                    message = template or _late_receipt(seed, final)
                    row.update(state="terminal", final_result=final, receipt_message=message)
                    state.conversation = retain_conversation(merge_effect_receipts(state.conversation, state.metadata))
                    _next_effect_poll(state)
                    return message

                message = await store.update_effect_state(**scope, change=save)
                if message is None or cancelled():
                    return 0
                # 先保存会话回执，再交给同一个 outbox；崩溃重试沿用相同幂等键。
                if not await asyncio.to_thread(_enqueue_late_receipt, seed, message):
                    return 0

                def delivered(state):
                    row = (state.metadata.get(_EFFECT_WAITS_KEY) or {}).get(plan_id)
                    if row and row.get("claim_id") == claim_id:
                        row["delivered"] = True  # 已交付会话/outbox，不等同远端已收到。
                        _next_effect_poll(state)
                await store.update_effect_state(**scope, change=delivered)
                return 1
            except SessionBusyError:
                return 0  # 活跃确认持锁时让路，不抢占用户操作。
            except Exception as exc:  # noqa: BLE001 - 单个损坏跟踪记录不阻塞其它会话
                get_logger(__name__).warning("Agent 后续回执核验暂未完成 type=%s", type(exc).__name__)
                return 0

    return sum(await asyncio.gather(*(follow(row) for row in await store.due_effect_waits(limit=limit))))


async def wait_for_effect_completion(
    result: ToolResult,
    *,
    tool: str,
    context: ToolContext,
    report_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    timeout_seconds: float = _WAIT_TIMEOUT_SECONDS,
    scope: EffectCompletionScope | None = None,
) -> ToolResult:
    """追踪有稳定句柄的后台写操作；纯提交结果原样交给公开语义层。"""
    status = str(result.status or "").strip().casefold()
    tracker = _completion_tracker(result)
    if not result.ok or status not in _WAITABLE_STATUSES or tracker is None:
        return result

    if scope is not None:
        await _register_effect_wait(result, tracker, tool, scope)
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
        if scope is not None:
            data["background_job"]["followup_pending"] = True
            return replace(result, ok=True, status="running",
                summary=(f"{label}仍在后台执行，完成后会自动回报" if job_status in active_statuses
                         else f"{label}状态暂未确认，后台将继续核验并回报"),
                data=data, model_data=_background_model_data(result, data),
                suggestions=["无需再次确认或重复提交；系统会继续跟踪这个已提交任务。"], error="")
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
    failures = 0
    last_saved = loop.time()
    while True:
        if context.cancelled():
            raise asyncio.CancelledError
        try:
            snapshot, task_status, task = await _poll(tracker, context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 仅重读状态，不重放写操作。
            failures += 1
            remaining = deadline - loop.time()
            if not isinstance(exc, (LookupError, ValueError)) and failures < 3 and remaining > 0:
                await asyncio.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))
                continue
            await _record_effect_observation(scope, status=last_status, due_now=True)
            return unknown(last_status, last_snapshot, last_task)

        failures = 0
        last_snapshot, last_task = snapshot, task
        if task_status in terminal_statuses:
            await report(snapshot, task_status)
            final = _terminal_result(result, snapshot, task_status, task, tracker)
            await _record_effect_observation(scope, status=task_status, final=final)
            return final
        if task_status not in active_statuses:
            await _record_effect_observation(scope, status=task_status, due_now=True)
            return unknown(
                last_status
                or ("unknown" if task_status in {"", "empty", "idle"} else task_status),
                snapshot,
                task,
            )

        if task_status != last_status or loop.time() - last_saved >= _EFFECT_POLL_SECONDS:
            await _record_effect_observation(scope, status=task_status)
            last_saved = loop.time()
        last_status = task_status
        await report(snapshot, task_status)
        remaining = deadline - loop.time()
        if remaining <= 0:
            await _record_effect_observation(scope, status=last_status, due_now=True)
            return unknown(last_status, snapshot, task, timed_out=True)
        await asyncio.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))
