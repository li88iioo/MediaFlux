"""用户触发的 Agent 持久化长任务：安全预检、进度查询与取消。"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import re
import secrets
from typing import Any, Mapping

from app import database as db
from app.agent.library_patrol_progress import empty_patrol_projection
from app.agent.library_patrol_status import validate_persisted_patrol_projection
from app.agent.models import Evidence, ToolContext, ToolResult
from app.agent.registry import AgentToolError
from app.modules.agent_jobs_scheduler import get_agent_jobs_scheduler
from app.repositories.agent_jobs import agent_job_owner_digest

_JOB_TYPE = "library_episode_audit"
_ACTIVE_STATUSES = {"pending", "running", "retry_wait"}
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_JOB_ID_RE = re.compile(r"job_[A-Za-z0-9_-]{16,80}")
_MAX_RECENT_JOBS = 10

_STATUS_LABELS = {
    "pending": "等待执行",
    "running": "正在检查",
    "retry_wait": "等待自动重试",
    "succeeded": "检查完成",
    "failed": "检查失败",
    "cancelled": "已取消",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _require_owner(context: ToolContext) -> str:
    owner = str(context.owner or "").strip()
    if not owner:
        raise AgentToolError("该动作需要在已登录会话中执行", code="identity_required")
    return owner


def _safe_job_id(value: object, *, optional: bool = False) -> str:
    job_id = str(value or "").strip()
    if optional and not job_id:
        return ""
    if not _JOB_ID_RE.fullmatch(job_id):
        raise AgentToolError("后台任务编号无效", code="job_not_found")
    return job_id


def _safe_date(value: object) -> str:
    raw = str(value or date.today().isoformat()).strip()
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期") from exc
    if parsed > date.today():
        raise AgentToolError("as_of 不能晚于今天")
    return parsed.isoformat()


def _safe_max_series(value: object) -> int:
    if value is None:
        return 50
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise AgentToolError("max_series 必须是 1 到 100 的整数")
    return value


def start_episode_audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - {"as_of", "max_series"}
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    return {
        "as_of": _safe_date(arguments.get("as_of")),
        "max_series": _safe_max_series(arguments.get("max_series")),
    }


def agent_job_status_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - {"job_id", "limit"}
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    raw_limit = arguments.get("limit", 5)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or not 1 <= raw_limit <= _MAX_RECENT_JOBS:
        raise AgentToolError(f"limit 必须是 1 到 {_MAX_RECENT_JOBS} 的整数")
    return {
        "job_id": _safe_job_id(arguments.get("job_id"), optional=True),
        "limit": raw_limit,
    }


def cancel_agent_job_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - {"job_id"}
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    return {"job_id": _safe_job_id(arguments.get("job_id"), optional=True)}


def _dedupe_key(arguments: Mapping[str, Any]) -> str:
    return f"{arguments['as_of']}:{int(arguments['max_series'])}"


def _find_active_for_request(*, owner: str, arguments: Mapping[str, Any]):
    return db.find_active_agent_job(
        owner=owner,
        job_type=_JOB_TYPE,
        dedupe_key=_dedupe_key(arguments),
    )


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _start_context(arguments: Mapping[str, Any], context: ToolContext) -> tuple[str, object | None]:
    owner = _require_owner(context)
    active = _find_active_for_request(owner=owner, arguments=arguments)
    context_hash = _fingerprint({
        "owner_digest": agent_job_owner_digest(owner),
        "job_type": _JOB_TYPE,
        "dedupe_key": _dedupe_key(arguments),
        "active_job_id": str(active["job_id"]) if active is not None else "",
    })
    return context_hash, active


def prepare_start_episode_audit(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    context_hash, active = _start_context(arguments, context)
    max_series = int(arguments["max_series"])
    if active is None:
        summary = (
            f"确认后将创建后台全库缺集巡检任务；任务会分批核对整个媒体库，"
            f"每批最多 {max_series} 部剧集"
        )
        effects = [
            "任务会读取 Jellyfin / Emby 的本地剧集库存，并与 TMDB 截至指定日期的已播清单核对。",
            "后台任务只审计具有可靠 TMDB 映射的剧集；未映射剧集会单独计数并提示，不会被误判为完整。",
            "刷新页面或重启服务后仍可继续；不会自动下载、删除或整理文件。",
            "可以随时查询进度或请求安全取消。",
        ]
        reused = False
    else:
        summary = "已存在同范围的后台全库缺集巡检任务"
        effects = [
            "确认后不会创建新任务，将继续使用现有后台检查。",
            "任务只读核对本地剧集库存与 TMDB 已播清单，不会自动下载、删除或整理文件。",
            "可以随后查询进度或请求安全取消。",
        ]
        reused = True
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=summary,
        data={
            "as_of": arguments["as_of"],
            "max_series": max_series,
            "reused": reused,
            "effects": effects,
        },
        evidence=[Evidence(
            "agent_job_queue",
            "仅检查当前会话的同范围后台任务是否已经存在；未读取媒体内容或启动巡检。",
            _now(),
        )],
        suggestions=["确认后任务将在后台运行，并可继续与 Agent 对话。"],
    ), context_hash


def start_episode_audit(_arguments: dict[str, Any]) -> ToolResult:
    """Fail closed：该写入只允许通过 owner 绑定的确认处理器执行。"""
    raise AgentToolError("该动作需要先预检并确认", code="confirmation_required")


def start_episode_audit_confirmed(
    arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    current_context, _active = _start_context(arguments, context)
    if not secrets.compare_digest(current_context, str(expected_context or "")):
        raise AgentToolError("后台任务状态已变化，请重新确认", code="confirmation_stale")

    owner = _require_owner(context)
    as_of = str(arguments["as_of"])
    max_series = int(arguments["max_series"])
    row, created = db.create_agent_job(
        owner=owner,
        job_type=_JOB_TYPE,
        dedupe_key=_dedupe_key(arguments),
        input_json=json.dumps(
            {"as_of": as_of, "max_series": max_series},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        checkpoint_json=json.dumps(
            {"as_of": as_of, "cursor": "", "stall_attempts": 0},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        projection_json=json.dumps(
            empty_patrol_projection(as_of=as_of),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        progress_total=0,
    )
    get_agent_jobs_scheduler().wake()
    job_id = str(row["job_id"])
    return ToolResult(
        ok=True,
        status="accepted",
        summary=(
            f"后台全库缺集巡检已创建；将按批次核对整个媒体库中具有可靠映射的剧集（每批最多 {max_series} 部）"
            if created
            else "相同范围的后台全库检查已在运行，未重复创建"
        ),
        data={
            "accepted": True,
            "created": created,
            "reused": not created,
            "job_id": job_id,
            "task_status": str(row["status"] or "pending"),
            "as_of": as_of,
            "max_series": max_series,
            "progress_current": max(0, int(row["progress_current"] or 0)),
            "progress_total": max(0, int(row["progress_total"] or 0)),
        },
        evidence=[Evidence(
            "agent_job_queue",
            "后台任务已按当前登录会话隔离保存；未把会话标识、媒体路径或服务凭据写入任务投影。",
            _now(),
        )],
        suggestions=["可以继续问：全库检查到哪了。", "也可以说：取消全库检查。"],
    )


def _load_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        raw = json.loads(str(row["projection_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = None
    projection = validate_persisted_patrol_projection(raw)
    if projection is not None:
        return projection
    as_of = ""
    try:
        input_data = json.loads(str(row["input_json"] or "{}"))
        if isinstance(input_data, dict):
            as_of = str(input_data.get("as_of") or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        return empty_patrol_projection(as_of=_safe_date(as_of or date.today().isoformat()))
    except AgentToolError:
        return empty_patrol_projection(as_of=date.today().isoformat())


def _load_input_summary(row: Mapping[str, Any]) -> tuple[str, int]:
    try:
        raw = json.loads(str(row["input_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "", 0
    if not isinstance(raw, dict):
        return "", 0
    try:
        as_of = date.fromisoformat(str(raw.get("as_of") or "")).isoformat()
    except ValueError:
        as_of = ""
    max_series = raw.get("max_series")
    if isinstance(max_series, bool) or not isinstance(max_series, int) or not 1 <= max_series <= 100:
        max_series = 0
    return as_of, max_series


def _safe_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    return parsed.astimezone().isoformat(timespec="seconds")


def _findings_from_options(options: object) -> list[dict[str, Any]]:
    if not isinstance(options, list):
        return []
    findings: list[dict[str, Any]] = []
    for option in options[:20]:
        if not isinstance(option, dict):
            continue
        title = str(option.get("title") or "").strip()[:160]
        tmdb_id = str(option.get("tmdb_id") or "").strip()
        season = option.get("season")
        sample = option.get("episode_sample")
        if (
            not title
            or not tmdb_id.isascii()
            or not tmdb_id.isdigit()
            or isinstance(season, bool)
            or not isinstance(season, int)
            or not isinstance(sample, list)
        ):
            continue
        episodes = [
            episode for episode in sample[:20]
            if isinstance(episode, int) and not isinstance(episode, bool) and 1 <= episode <= 1000
        ]
        if not episodes:
            continue
        findings.append({
            "title": title,
            "tmdb_id": tmdb_id,
            "status": "updates_available",
            "missing_count": max(0, int(option.get("missing_count") or len(episodes))),
            "missing_sample": [
                {"season": season, "episode": episode} for episode in episodes
            ],
            "missing_sample_truncated": False,
        })
    return findings


def _project_row(row: Mapping[str, Any], *, include_findings: bool) -> dict[str, Any]:
    projection = _load_projection(row)
    as_of, max_series = _load_input_summary(row)
    current = max(0, int(row["progress_current"] or 0))
    total = max(0, int(row["progress_total"] or 0))
    status = str(row["status"] or "pending")
    item = {
        "job_id": str(row["job_id"]),
        "task_status": status if status in _ACTIVE_STATUSES | _TERMINAL_STATUSES else "failed",
        "task_status_label": _STATUS_LABELS.get(status, "状态未知"),
        "summary": str(row["summary"] or "").strip()[:240],
        "as_of": as_of or projection["as_of"],
        "max_series": max_series,
        "patrol_status": projection["patrol_status"],
        "progress_current": current,
        "progress_total": total,
        "progress_percent": round(current * 100 / total, 1) if total else 0.0,
        "checked_series_count": projection["checked_series_count"],
        "updates_available_count": projection["updates_available_count"],
        "missing_episode_count": projection["missing_episode_count"],
        "inconclusive_count": projection["inconclusive_count"],
        "unmapped_series_count": projection["unmapped_series_count"],
        "findings_truncated": bool(projection["findings_truncated"]),
        "cancel_requested": bool(row["cancel_requested"]),
        "created_at": _safe_timestamp(row["created_at"]),
        "updated_at": _safe_timestamp(row["updated_at"]),
        "finished_at": _safe_timestamp(row["finished_at"]),
    }
    if include_findings:
        item["findings"] = _findings_from_options(projection["options"])
    return item


def _status_summary(latest: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    status = str(latest.get("task_status") or "")
    current = int(latest.get("progress_current") or 0)
    total = int(latest.get("progress_total") or 0)
    checked = int(latest.get("checked_series_count") or 0)
    updates = int(latest.get("updates_available_count") or 0)
    missing = int(latest.get("missing_episode_count") or 0)
    if status == "pending":
        return "pending", "全库检查已排队，正在等待后台执行", ["可以继续处理其他事情，稍后再问进度。"]
    if status == "running":
        detail = f"，进度 {current}/{total}" if total else (f"，已检查 {current} 部" if current else "")
        return "running", f"全库检查正在运行{detail}", ["任务会在批次边界保存进度；需要时可以安全取消。"]
    if status == "retry_wait":
        return "retry_wait", "全库检查暂时无法完成，正在等待自动重试", ["请确认媒体服务器与 TMDB 连接可用；也可以取消后重新开始。"]
    if status == "cancelled":
        return "cancelled", "全库检查已取消，已保存的结果不会触发下载", ["如需重新检查，可以再次发起全库巡检。"]
    if status == "failed":
        return "failed", "全库检查未能完成", ["请检查媒体服务器与 TMDB 配置后重新发起。"]
    patrol_status = str(latest.get("patrol_status") or "")
    if updates or patrol_status == "updates_available":
        return "updates_available", f"全库检查已完成：核对 {checked} 部，发现 {updates} 部共缺 {missing} 集", ["可以继续说：把第 1 项缺集找资源。"]
    if patrol_status == "not_configured":
        return "failed", "全库检查未开始：媒体服务器或 TMDB 配置不完整", ["请完成媒体服务器与 TMDB 配置后重新发起。"]
    if patrol_status == "unavailable":
        return "failed", "全库检查未完成：媒体服务器或 TMDB 当前不可用", ["请稍后重试，并检查媒体服务器与 TMDB 连接。"]
    if patrol_status == "failed":
        return "failed", "全库检查执行失败", ["请检查媒体服务器与 TMDB 配置后重新发起。"]
    inconclusive = int(latest.get("inconclusive_count") or 0)
    unmapped = int(latest.get("unmapped_series_count") or 0)
    if patrol_status == "inconclusive" or inconclusive or unmapped:
        detail = []
        if inconclusive:
            detail.append(f"{inconclusive} 部暂时无法确认")
        if unmapped:
            detail.append(f"{unmapped} 部缺少可靠 TMDB 映射")
        suffix = "；" + "，".join(detail) if detail else ""
        return "inconclusive", f"全库检查已结束但覆盖不完整：核对 {checked} 部{suffix}", ["可检查媒体映射与 TMDB 配置后重新运行。"]
    if patrol_status == "up_to_date":
        return "up_to_date", f"全库检查已完成：核对 {checked} 部，暂未发现已播缺集", ["无需处理；之后可按需再次巡检。"]
    return "inconclusive", f"全库检查已完成：核对 {checked} 部，但结论仍需确认", ["请检查媒体映射与 TMDB 配置后重新运行。"]


def get_agent_job_status(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    owner = _require_owner(context)
    if arguments["job_id"]:
        selected = db.get_agent_job(owner=owner, job_id=arguments["job_id"])
        rows = [selected] if selected is not None else []
    else:
        rows = db.list_agent_jobs(
            owner=owner,
            limit=int(arguments["limit"]),
            job_type=_JOB_TYPE,
        )
    if not rows:
        return ToolResult(
            ok=True,
            status="not_run",
            summary="当前会话还没有发起过全库检查",
            data={"jobs": [], "task_status": "not_created"},
            suggestions=["可以说：巡检整个媒体库有没有缺集。"],
        )

    latest = _project_row(rows[0], include_findings=True)
    public_status, summary, suggestions = _status_summary(latest)
    recent = [_project_row(row, include_findings=False) for row in rows]
    return ToolResult(
        ok=public_status != "failed",
        status=public_status,
        summary=summary,
        data={**latest, "jobs": recent},
        evidence=[Evidence(
            "agent_job_queue",
            "仅返回当前登录会话的任务状态、聚合计数与安全缺集候选，不包含媒体路径、服务地址或凭据。",
            _now(),
        )],
        suggestions=suggestions,
        error="后台全库检查失败。" if public_status == "failed" else "",
    )


def _resolve_cancel_target(arguments: Mapping[str, Any], context: ToolContext):
    owner = _require_owner(context)
    job_id = str(arguments.get("job_id") or "")
    row = (
        db.get_agent_job(owner=owner, job_id=job_id)
        if job_id
        else db.find_latest_active_agent_job(owner=owner, job_type=_JOB_TYPE)
    )
    if row is None or str(row["status"] or "") not in _ACTIVE_STATUSES:
        raise AgentToolError("当前没有可取消的全库检查", code="precondition_failed")
    return owner, row


def _cancel_context(arguments: Mapping[str, Any], context: ToolContext) -> tuple[str, object]:
    owner, row = _resolve_cancel_target(arguments, context)
    return _fingerprint({
        "owner_digest": agent_job_owner_digest(owner),
        "job_type": _JOB_TYPE,
        "job_id": str(row["job_id"]),
    }), row


def prepare_cancel_agent_job(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    context_hash, row = _cancel_context(arguments, context)
    status = str(row["status"] or "pending")
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary="确认后将安全停止当前全库检查",
        data={
            "job_id": str(row["job_id"]),
            "task_status": status,
            "progress_current": max(0, int(row["progress_current"] or 0)),
            "progress_total": max(0, int(row["progress_total"] or 0)),
            "effects": [
                "尚未开始的任务会立即取消。",
                "正在运行的任务会完成当前有界批次后停止。",
                "已经产生的只读检查结果会保留，但不会触发下载或文件修改。",
            ],
        },
        evidence=[Evidence(
            "agent_job_queue",
            "仅定位当前登录会话最近的活动全库检查；尚未修改任务状态。",
            _now(),
        )],
        suggestions=["取消不会删除媒体文件，也不会回滚已保存的只读检查进度。"],
    ), context_hash


def cancel_agent_job(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("该动作需要先预检并确认", code="confirmation_required")


def cancel_agent_job_confirmed(
    arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    current_context, target = _cancel_context(arguments, context)
    if not secrets.compare_digest(current_context, str(expected_context or "")):
        raise AgentToolError("后台任务状态已变化，请重新确认", code="confirmation_stale")
    owner = _require_owner(context)
    row, outcome = db.cancel_agent_job(
        owner=owner,
        job_id=str(target["job_id"]),
        job_type=_JOB_TYPE,
    )
    if row is None or outcome in {"not_found", "terminal"}:
        raise AgentToolError("后台任务状态已变化，请重新确认", code="confirmation_stale")
    get_agent_jobs_scheduler().wake()
    requested = outcome == "requested"
    return ToolResult(
        ok=True,
        status="accepted" if requested else "completed",
        summary=(
            "已请求安全停止全库检查，当前批次结束后会退出"
            if requested
            else "全库检查已取消"
        ),
        data={
            "accepted": requested,
            "cancelled": not requested,
            "cancel_requested": True,
            "job_id": str(row["job_id"]),
            "task_status": str(row["status"] or ("running" if requested else "cancelled")),
            "progress_current": max(0, int(row["progress_current"] or 0)),
            "progress_total": max(0, int(row["progress_total"] or 0)),
        },
        evidence=[Evidence(
            "agent_job_queue",
            "取消请求只作用于当前登录会话的目标任务，不会修改媒体文件或其他用户的任务。",
            _now(),
        )],
        suggestions=["可以继续问：全库检查到哪了。"],
    )
