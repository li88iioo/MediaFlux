"""用户后台全库审计的公共业务；沿用agent_jobs原表、owner分区和批次调度。"""

from __future__ import annotations

import json
import re
from datetime import date, datetime

from app.modules.library_patrol import (
    empty_patrol_projection,
    validate_persisted_patrol_projection,
)
from app.repositories import agent_jobs as jobs
from app.repositories.agent_jobs import AgentJobError

JOB_TYPE = "library_episode_audit"
_ACTIVE = {"pending", "running", "retry_wait"}
_LABELS = {
    "pending": "等待执行",
    "running": "正在检查",
    "retry_wait": "等待自动重试",
    "succeeded": "检查完成",
    "failed": "检查失败",
    "cancelled": "已取消",
}
_COUNTS = (
    "checked_series_count",
    "updates_available_count",
    "missing_episode_count",
    "inconclusive_count",
    "unmapped_series_count",
)


def _arguments(as_of, max_series):
    today = datetime.now().astimezone().date()
    if as_of is not None and not isinstance(as_of, str):
        raise TypeError("as_of必须是YYYY-MM-DD字符串")
    when = date.fromisoformat(as_of.strip()) if as_of else today
    if when > today:
        raise ValueError("as_of不能晚于今天")
    if type(max_series) is not int or not 1 <= max_series <= 100:
        raise ValueError("max_series必须是1到100的整数")
    return {"as_of": when.isoformat(), "max_series": max_series}


def _job_id(value, *, optional=False):
    if value is None or value == "":
        if optional:
            return None
        raise AgentJobError("后台任务编号无效", code="job_not_found")
    if not isinstance(value, str) or not re.fullmatch(
        r"job_[A-Za-z0-9_-]{16,80}", value.strip()
    ):
        raise AgentJobError("后台任务编号无效", code="job_not_found")
    return value.strip()


def _start_snapshot(owner, arguments):
    key = f"{arguments['as_of']}:{arguments['max_series']}"
    row = jobs.find_active_agent_job(owner=owner, job_type=JOB_TYPE, dedupe_key=key)
    return {
        "owner_digest": jobs.agent_job_owner_digest(owner),
        "job_type": JOB_TYPE,
        "dedupe_key": key,
        "dedupe_active_id": str(row["job_id"]) if row is not None else None,
    }


def prepare_start_audit(
    *, owner: str, as_of: str | None = None, max_series: int = 50
) -> tuple[dict, dict]:
    arguments = _arguments(as_of, max_series)
    snapshot = _start_snapshot(owner, arguments)
    reused = snapshot["dedupe_active_id"] is not None
    preview = {
        "ok": True,
        "status": "confirmation_required",
        "summary": "确认后复用同范围的活动任务"
        if reused
        else f"确认后创建全库后台缺集审计，每批最多{max_series}部",
        "data": {
            **arguments,
            "reused": reused,
            "job_id": snapshot["dedupe_active_id"],
            "effects": [
                "按批核对本地库存与TMDB已播清单，未映射条目单独计数。",
                "支持重启恢复和批次边界取消，不下载、删除或整理媒体。",
            ],
        },
    }
    return preview, snapshot


def start_audit(
    *, owner: str, as_of: str, max_series: int, expected_snapshot: dict
) -> dict:
    arguments = _arguments(as_of, max_series)
    if _start_snapshot(owner, arguments) != expected_snapshot:
        raise AgentJobError("后台任务状态已变化，请重新确认", code="confirmation_stale")
    row, created = jobs.create_agent_job(
        owner=owner,
        job_type=JOB_TYPE,
        dedupe_key=expected_snapshot["dedupe_key"],
        input_json=json.dumps(arguments, sort_keys=True),
        checkpoint_json=json.dumps(
            {"as_of": arguments["as_of"], "cursor": "", "stall_attempts": 0}
        ),
        projection_json=json.dumps(empty_patrol_projection(as_of=arguments["as_of"])),
        progress_total=0,
        expected_active_job_id=expected_snapshot["dedupe_active_id"],
    )
    from app.modules.agent_jobs_scheduler import get_agent_jobs_scheduler

    get_agent_jobs_scheduler().wake()
    return {
        "ok": True,
        "status": "accepted",
        "summary": "后台全库审计已创建" if created else "同范围任务仍在运行，已复用",
        "data": {
            "accepted": True,
            "created": created,
            "reused": not created,
            "job_id": str(row["job_id"]),
            "task_status": row["status"],
            **arguments,
            "progress_current": row["progress_current"],
            "progress_total": row["progress_total"] or None,
        },
    }


def _cancel_snapshot(owner, job_id):
    identity = _job_id(job_id, optional=True)
    row = (
        jobs.get_agent_job(owner=owner, job_id=identity)
        if identity
        else jobs.find_latest_active_agent_job(owner=owner, job_type=JOB_TYPE)
    )
    if row is None or row["job_type"] != JOB_TYPE or row["status"] not in _ACTIVE:
        raise AgentJobError("当前没有可取消的全库检查", code="precondition_failed")
    return {
        "owner_digest": jobs.agent_job_owner_digest(owner),
        "job_type": JOB_TYPE,
        "job_id": str(row["job_id"]),
    }, row


def prepare_cancel_audit(*, owner: str, job_id: str | None = None) -> tuple[dict, dict]:
    snapshot, row = _cancel_snapshot(owner, job_id)
    return {
        "ok": True,
        "status": "confirmation_required",
        "summary": "确认后安全取消指定后台全库审计",
        "data": {
            "job_id": snapshot["job_id"],
            "task_status": row["status"],
            "progress_current": row["progress_current"],
            "progress_total": row["progress_total"] or None,
            "effects": [
                "未开始的任务立即取消；运行任务在当前有界批次结束后停止。",
                "已保存的只读结果保留，不删除媒体。",
            ],
        },
    }, snapshot


def cancel_audit(*, owner: str, job_id: str, expected_snapshot: dict) -> dict:
    try:
        current, _ = _cancel_snapshot(owner, job_id)
    except AgentJobError as exc:
        raise AgentJobError(
            "后台任务状态已变化，请重新确认", code="confirmation_stale"
        ) from exc
    if current != expected_snapshot:
        raise AgentJobError("后台任务状态已变化，请重新确认", code="confirmation_stale")
    row, outcome = jobs.cancel_agent_job(
        owner=owner, job_id=current["job_id"], job_type=JOB_TYPE
    )
    if row is None or outcome in {"not_found", "terminal"}:
        raise AgentJobError("后台任务状态已变化，请重新确认", code="confirmation_stale")
    from app.modules.agent_jobs_scheduler import get_agent_jobs_scheduler

    get_agent_jobs_scheduler().wake()
    requested = outcome == "requested"
    return {
        "ok": True,
        "status": "accepted" if requested else "completed",
        "summary": "已请求在当前批次结束后安全停止" if requested else "全库检查已取消",
        "data": {
            "job_id": str(row["job_id"]),
            "accepted": requested,
            "cancelled": not requested,
            "cancel_requested": True,
            "task_status": row["status"],
            "progress_current": row["progress_current"],
            "progress_total": row["progress_total"] or None,
        },
    }


def _json(raw):
    try:
        return json.loads(raw or "{}")
    except (ValueError, TypeError):
        return None


def _timestamp(value):
    try:
        return (
            datetime.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S")
            .astimezone()
            .isoformat(timespec="seconds")
        )
    except ValueError:
        return ""


def _project(row, *, findings):
    projection = validate_persisted_patrol_projection(_json(row["projection_json"]))
    arguments = _json(row["input_json"])
    try:
        arguments = (
            _arguments(**arguments)
            if isinstance(arguments, dict) and set(arguments) == {"as_of", "max_series"}
            else {}
        )
    except (ValueError, TypeError):
        arguments = {}
    status = str(row["status"])
    total = int(row["progress_total"])
    total = total if total or status == "succeeded" else None
    item = {
        "job_id": str(row["job_id"]),
        "task_status": status,
        "task_status_label": _LABELS.get(status, "状态未知"),
        "as_of": arguments.get("as_of", ""),
        "max_series": arguments.get("max_series"),
        "summary": str(row["summary"] or "").strip()[:240],
        "progress_current": int(row["progress_current"]),
        "progress_total": total,
        "progress_percent": round(row["progress_current"] * 100 / total, 1)
        if total
        else None,
        "projection_valid": projection is not None,
        "patrol_status": projection["patrol_status"] if projection else "inconclusive",
        **{key: projection[key] if projection else None for key in _COUNTS},
        "findings_truncated": projection["findings_truncated"] if projection else None,
        "cancel_requested": bool(row["cancel_requested"]),
        **{
            key: _timestamp(row[key])
            for key in ("created_at", "updated_at", "finished_at")
        },
    }
    values = []
    if projection:
        for option in projection["options"]:
            sample = [
                {"season": option["season"], "episode": e}
                for e in option["episode_sample"]
            ]
            values.append(
                {
                    "title": option["title"],
                    "tmdb_id": option["tmdb_id"],
                    "status": "updates_available",
                    "missing_count": None
                    if projection["findings_truncated"]
                    else option["missing_count"],
                    "missing_sample_count": len(sample),
                    "missing_sample_complete": not projection["findings_truncated"],
                    "missing_sample": sample,
                    "missing_sample_truncated": None
                    if projection["findings_truncated"]
                    else False,
                }
            )
    item["coverage_complete"] = bool(
        status == "succeeded"
        and projection
        and projection["patrol_status"] in {"up_to_date", "updates_available"}
        and not projection["inconclusive_count"]
        and not projection["unmapped_series_count"]
    )
    item["findings_complete"] = (
        item["coverage_complete"]
        and not item["findings_truncated"]
        and all(v["missing_sample_truncated"] is False for v in values)
    )
    if findings:
        item.update(
            findings=values,
            findings_returned=len(values),
            findings_total=None
            if item["findings_truncated"] is not False
            else len(values),
            projection_scope="persisted_bounded_sample",
        )
    return item


def _status(item):
    status = item["task_status"]
    if status != "succeeded":
        return status if status in _LABELS else "unknown"
    if item["updates_available_count"] or item["patrol_status"] == "updates_available":
        return "updates_available"
    if item["patrol_status"] in {"not_configured", "unavailable", "failed"}:
        return "failed"
    if not item["coverage_complete"]:
        return "inconclusive"
    return "up_to_date"


def audit_job(*, owner: str, job_id: str | None = None, limit: int = 5) -> dict:
    identity = _job_id(job_id, optional=True)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("limit必须是1到10的整数")
    if identity:
        row = jobs.get_agent_job(owner=owner, job_id=identity)
        rows = [row] if row is not None and row["job_type"] == JOB_TYPE else []
    else:
        rows = jobs.list_agent_jobs(owner=owner, job_type=JOB_TYPE, limit=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    scope = {
        "owner": "current",
        "job_type": JOB_TYPE,
        "job_id": identity,
        "limit": limit,
        "jobs_has_more": more,
        "jobs_total": None if more else len(rows),
    }
    if not rows:
        return {
            "ok": True,
            "status": "not_run",
            "summary": "当前身份没有匹配的全库审计任务",
            "data": {"jobs": [], "task_status": "not_created", "scope": scope},
        }
    latest = _project(rows[0], findings=True)
    status = _status(latest)
    summary = {
        "pending": "全库检查已排队",
        "running": "全库检查正在运行",
        "retry_wait": "全库检查等待自动重试",
        "cancelled": "全库检查已取消",
        "failed": "全库检查未能完成",
        "up_to_date": "全库检查已完成，未发现已播缺集",
        "inconclusive": "全库检查覆盖不完整",
        "updates_available": "全库检查已完成并发现缺集",
        "unknown": "后台任务状态未知",
    }[status]
    return {
        "ok": status != "failed",
        "status": status,
        "summary": f"{summary}；已核对{latest['checked_series_count'] if latest['checked_series_count'] is not None else '未知'}部",
        "data": {
            **latest,
            "jobs": [_project(row, findings=False) for row in rows],
            "scope": scope,
        },
        "error": "后台全库检查失败" if status == "failed" else "",
    }
