"""本地持久任务的查询、预览、确认重试及完成事实；Web/脚本共用原业务服务。"""
from __future__ import annotations

import secrets
import json
from pathlib import Path

from app import database as db
from app.modules.local_media_models import LOCAL_BUSY_TASK_STATUSES, LOCAL_TASK_STATUSES
from app.modules.local_media_outcomes import local_media_task_outcome
from app.modules import local_media_scan_runs
from app.modules.local_media_service import LocalMediaServiceError, get_local_media_service
from app.modules.web_secret import signed_context_fingerprint
from app.repositories import local_media as repository
from app.sensitive_data import is_sensitive_key, redact_sensitive_text

RETRYABLE = frozenset({"failed", "requires_manual"})
SCOPES = frozenset({"all", "attention", "active", "history", "skipped", "latest_scan", *LOCAL_TASK_STATUSES})


def _redacted(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if is_sensitive_key(key) and item else _redacted(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted(item) for item in value]
    return redact_sensitive_text(value) if isinstance(value, str) else value


def _task_id(task_id: int) -> int:
    if type(task_id) is not int or not 1 <= task_id <= 2**63 - 1:
        raise ValueError("task_id必须是持久任务的真实正整数ID")
    return task_id


def _snapshot(task_id: int) -> dict:
    _task_id(task_id)
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        return repository.local_media_task_snapshot(conn, task_id)


def _outcome(task: dict, items: list) -> dict:
    outcome = local_media_task_outcome(task, items)
    names = list(dict.fromkeys(Path(str(item["source_path"]).replace("\\", "/")).name
                              for item in items if item["role"] == "video"))
    return {**outcome, "file_names": names, "file_names_truncated": False}


def _task_fact(task: dict, items: list) -> dict:
    return {**task, **_outcome(task, items), "task_id": task["id"],
            "reason_code": _status_reason(task["status"]),
            "can_inspect": task["status"] == "requires_manual", "can_retry": task["status"] in RETRYABLE,
            "can_refresh_library": task["status"] == "completed", "can_verify_library": task["status"] == "completed"}


def _status_reason(status: str) -> str:
    return {"requires_manual": "manual_match_required", "failed": "processing_failed", "waiting_stable": "waiting_to_run",
            "recognizing": "recognizing", "planned": "plan_ready", "moving": "moving_files", "verifying": "verifying_files",
            "refreshing": "refreshing_library", "rolling_back": "rolling_back", "completed": "processing_completed"}.get(status, "unknown")


def _statuses(scope: str) -> set[str]:
    return {"attention": set(RETRYABLE), "active": set(LOCAL_BUSY_TASK_STATUSES),
            "history": {"completed", "failed"}, "skipped": {"completed"}}.get(
                scope, {scope} if scope in LOCAL_TASK_STATUSES else set())


def local_tasks(scope: str = "all", *, source_id: int | None = None, scan_ref: str = "", limit: int = 100, offset: int = 0) -> dict:
    if scope not in SCOPES or type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
        raise ValueError("scope无效，或分页参数无效（limit为1–500，offset为非负整数）")
    if source_id is not None:
        _task_id(source_id)
    if not isinstance(scan_ref, str):
        raise ValueError("scan_ref必须是原扫描回执LM编号")
    scan = None
    scan_data: dict = {}
    snapshots: dict[int, dict] = {}
    if scan_ref or scope == "latest_scan":
        try:
            scan = local_media_scan_runs.resolve_local_media_scan(scan_ref)
        except LookupError as exc:
            return {"items": [], "total": 0, "returned": 0, "offset": offset, "limit": limit,
                    "has_more": False, "next_offset": None, "scope": scope, "status": "not_recorded",
                    "scan_ref": scan_ref, "scan_recorded": False, "error": str(exc)}
        membership = list(dict.fromkeys(scan["task_ids"]))
        snapshots = {item["task_id"]: item for item in scan["summary"].get("task_outcomes", [])
                     if isinstance(item, dict) and type(item.get("task_id")) is int}
        with db.get_conn() as conn:
            rows = conn.execute("SELECT * FROM local_media_tasks WHERE owner='admin' AND id IN (SELECT value FROM json_each(?))",
                                (json.dumps(membership),)).fetchall()
        found = {row["id"]: dict(row) for row in rows}
        selected = []
        for identifier in membership:
            task = found.get(identifier)
            if task is None or (source_id is not None and task["source_id"] != source_id):
                continue
            old = snapshots.get(identifier, {})
            status = old.get("status", task["status"])
            if _statuses(scope) and status not in _statuses(scope):
                continue
            if scope == "skipped" and not (old or _outcome(task, repository.list_local_media_task_items(identifier))).get("skipped_video_count"):
                continue
            selected.append(task)
        total, tasks = len(selected), selected[offset:offset + limit]
        missing = len(membership) - len(found)
        scan_data = {**local_media_scan_runs.local_media_scan_summary(scan["summary"]),
                     "scan_ref": scan["scan_ref"], "scan_recorded": True, "scan_task_count": len(membership),
                     "missing_task_count": missing, "missing_tasks_excluded": bool(missing),
                     "scan_started_at": scan["started_at"], "scan_finished_at": scan["finished_at"],
                     "scan_reported_at": scan["summary"].get("reported_at", "")}
    else:
        tasks, total = repository.local_media_task_page(statuses=_statuses(scope), skipped=scope == "skipped",
                                                       source_id=source_id, limit=limit, offset=offset)
    items = []
    for task in tasks:
        item = _task_fact(task, repository.list_local_media_task_items(task["id"]))
        if scan:
            old = snapshots.get(task["id"])
            item["outcome_basis"] = "scan_snapshot" if old else "current_task_state"
            if old:
                item.update({**old, "task_id": task["id"], "current_status": task["status"], "scan_outcome": old})
                item["reason_code"] = _status_reason(item["status"])
        items.append(item)
    return _redacted({"items": items, "total": total, "returned": len(items), "scope": scope,
                      "offset": offset, "limit": limit, "has_more": offset + len(items) < total,
                      "next_offset": offset + len(items) if offset + len(items) < total else None,
                      "status": "attention" if any(item["status"] in RETRYABLE for item in items)
                          or scan_data.get("missing_task_count") or scan_data.get("failed_sources") else "completed", **scan_data})


def local_task(task_id: int, *, inspect: bool = False) -> dict:
    snapshot = _snapshot(task_id)
    task = snapshot["task"]
    result = {"task": _task_fact(task, snapshot["items"]), "items": snapshot["items"], "steps": snapshot["steps"]}
    if inspect:
        service = get_local_media_service()
        inspection = service.inspect_task("admin", task_id)
        try:
            result["inspection"] = {key: value for key, value in inspection.items() if key != "inspection_id"}
        finally:
            service.inspections.discard("admin", inspection["inspection_id"])
    return _redacted(result)


def _preview(task: dict, changes: dict, *, require_source_digest: bool = False) -> dict:
    service = get_local_media_service()
    inspection = service.inspect_source("admin", task["source_id"], task["content_path"])
    try:
        digest = str(task["snapshot_digest"] or "")
        digest = digest.split(":")[1] if digest.startswith("preview:") else digest
        if require_source_digest and (not digest or inspection["digest"] != digest):
            raise LocalMediaServiceError("源文件范围已变化，请重新检查并确认整理预览")
        return service.preview("admin", inspection["inspection_id"],
            tmdb_id=changes.get("tmdb_id", task["tmdb_id"]), media_type=changes.get("media_type", task["media_type"]),
            rules_snapshot=task["rules_snapshot"], automatic=False,
            season_override=changes.get("season", task["season_override"]),
            episode_override=changes.get("episode", task["episode_override"]),
            numbering_mode=changes.get("numbering_mode", task["numbering_mode"]))
    finally:
        service.inspections.discard("admin", inspection["inspection_id"])


def preview_local_task(task_id: int, **changes) -> dict:
    task = _snapshot(task_id)["task"]
    if task["status"] != "requires_manual":
        raise LocalMediaServiceError("仅待确认任务可以进入人工复核预览")
    result = _preview(task, changes)
    return _redacted({"task_id": task_id, **{key: value for key, value in result.items() if not key.startswith("_")}})


def prepare_local_task_retry(task_id: int, *, confirm_interrupted_write: bool = False, **changes) -> dict:
    if set(changes) - {"tmdb_id", "media_type", "season", "episode", "numbering_mode"}:
        raise ValueError("本地任务重试参数无效")
    changes = {key: value for key, value in changes.items() if key not in {"season", "episode"} or value is not None}
    snapshot = _snapshot(task_id)
    task = snapshot["task"]
    if task["status"] not in RETRYABLE:
        raise LocalMediaServiceError("只有失败或待人工确认的任务可以重试")
    interrupted = db.is_interrupted_local_media_write_error(task["error"])
    if type(confirm_interrupted_write) is not bool or (interrupted and not confirm_interrupted_write):
        raise LocalMediaServiceError("任务曾在本地写入期间中断，请先核验文件及qB状态，再显式confirm_interrupted_write=True")
    for key, low, high in (("season", 0, 99), ("episode", 1, 999)):
        value = changes.get(key)
        if value is not None and (type(value) is not int or not low <= value <= high):
            raise ValueError(f"{key}必须为{low}–{high}的整数")
    for key in ("tmdb_id", "media_type", "numbering_mode"):
        if key in changes and not isinstance(changes[key], str):
            raise ValueError(f"{key}必须为字符串")
    fields = {("season_override" if key == "season" else "episode_override" if key == "episode" else key): value
              for key, value in changes.items()}
    confirmed_manual = task["trigger"] == "manual" and task["confirmation_actor"] and not interrupted
    if changes or confirmed_manual:
        raw = _preview(task, changes, require_source_digest=bool(task["snapshot_digest"]))
        if raw.get("status") != "planned" or raw.get("pending_confirmations"):
            raise LocalMediaServiceError("重试计划仍需人工选择，请重新检查任务")
        fields.update(preview_digest=raw["preview_digest"], rules_snapshot=raw["rules_snapshot"])
    fingerprint = signed_context_fingerprint({"snapshot": snapshot, "fields": fields,
        "confirm_interrupted_write": confirm_interrupted_write}, domain="local-task-retry")
    return {"fingerprint": fingerprint, "snapshot": snapshot, "fields": fields,
            "preview": {"ok": True, "status": "confirmation_required", "summary": f"确认后重新排队本地任务 {task_id}",
                "data": _redacted({"task_id": task_id, "version": task["version"], "current_status": task["status"],
                    "error": task["error"], "interrupted_write": interrupted, "changes": changes,
                    "effects": ["只重新排队原持久任务，不扩大来源或扫描范围；不直接移动文件",
                                "使用原识别身份/季集映射、规则和来源模式；重新确认冻结的文件预览",
                                "中断任务需人工核验文件及qB；本次确认不会回放旧文件步骤"]})}}


def retry_local_task(task_id: int, *, expected_fingerprint: str, cancelled=None, confirm_interrupted_write: bool = False, **changes) -> dict:
    try:
        prepared = prepare_local_task_retry(task_id, confirm_interrupted_write=confirm_interrupted_write, **changes)
    except (ValueError, LookupError, LocalMediaServiceError) as exc:
        raise repository.LocalMediaTaskChanged(str(exc)) from exc
    if not secrets.compare_digest(prepared["fingerprint"], expected_fingerprint):
        raise repository.LocalMediaTaskChanged("任务、来源或整理预览已变化，请重新预检")
    task = prepared["snapshot"]["task"]
    try:
        changed = repository.reset_local_media_task(task_id, expected_version=task["version"], expected_status=task["status"],
            expected_snapshot=prepared["snapshot"], cancelled=cancelled, confirm_interrupted_write=confirm_interrupted_write, **prepared["fields"])
    except repository.LocalMediaTaskCancelled:
        return {"ok": False, "status": "cancelled", "summary": "本地任务重试已取消", "data": {"task_id": task_id}}
    if not changed:
        raise repository.LocalMediaTaskChanged("任务状态已变化，请重新预检")
    from app.modules.local_media_scheduler import get_local_media_scheduler
    get_local_media_scheduler().reload()
    return {"ok": True, "status": "accepted", "summary": f"本地任务 {task_id} 已重新排队",
            "data": {"operation": "retry", "task_id": task_id, "affected": 1, "runtime_refreshed": True,
                     "version": task["version"] + 1, "remapped": bool(set(prepared["fields"]) - {"preview_digest", "rules_snapshot"})}}


def local_task_completion(task_id: int) -> dict:
    try:
        detail = local_task(task_id)
    except LookupError:
        return {"ok": False, "status": "unknown", "summary": "本地任务已不存在，无法确认结果", "data": {}}
    task = detail["task"]
    state = task["status"]
    status = "running" if state in LOCAL_BUSY_TASK_STATUSES else "manual_review" if state == "requires_manual" else state
    ok = status in {"running", "completed"}
    summary = f"本地任务 {task_id} 状态 {status}"
    if status == "completed":
        if task["file_outcome"] == "preview_only":
            summary = f"本地任务 {task_id} 预览已完成，未移动文件，归档 0 个视频"
        else:
            ok = task["file_outcome"] in {"archived", "conflict_skipped", "partial"} and task["unknown_video_count"] == 0
            summary = f"本地任务 {task_id} 已结束：归档 {task['archived_video_count']} 个视频，冲突跳过 {task['skipped_video_count']} 个视频"
    return {"ok": ok, "status": status, "summary": summary,
            "data": {"task": {**task, "items": detail["items"], "steps": detail["steps"], "source_status": state, "status": status}},
            "error": task["error"] if status in {"failed", "manual_review"} else "视频归档结果尚无法核验" if not ok else ""}


def diagnose_local_tasks() -> dict:
    from app.modules.local_media_scheduler import peek_local_media_scheduler_status
    aggregate = repository.get_local_media_diagnostic_summary(owner="admin")
    runtime = peek_local_media_scheduler_status()
    sources, tasks = aggregate["sources"], aggregate["tasks"]
    categories = {"requires_manual": tasks["requires_manual"], "failed": tasks["failed"],
                  "enabled_sources_without_targets": sources["enabled_without_targets"],
                  "scheduler_not_running": int(bool(sources["enabled"] and not runtime["running"]))}
    attention = sum(categories.values())
    status = ("not_configured" if not sources["total"] else "attention" if attention else "inactive" if not sources["enabled"]
              else "active" if tasks["waiting_stable"] or tasks["active"] else "idle")
    return {"ok": True, "status": status, "summary": "本地来源/任务与调度器状态已读取",
            "data": {"probe_mode": "local", "network_accessed": False, **aggregate, "scheduler": runtime,
                     "attention": {"total": attention, "categories": categories}}}
