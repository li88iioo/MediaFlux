"""后台全库缺集巡检的公共状态、投影、进度及当前策略触发。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from app import config, database as db
from app.repositories import agent_library_patrol as agent_library_patrol_repo
from app.repositories.agent_library_patrol import patrol_task_state

_ALLOWED_FINDING_STATUS = {"updates_available"}
_MAX_OPTIONS = 20
_MAX_EPISODES_PER_OPTION = 20
_SAFE_PATROL_STATUSES = {
    "updates_available",
    "up_to_date",
    "inconclusive",
    "not_configured",
    "unavailable",
    "failed",
    "not_run",
    "running",
    "pending",
}

_MAX_COUNT = 1000000
_PROJECTION_KEYS = {
    "as_of",
    "patrol_status",
    "findings_truncated",
    "checked_series_count",
    "updates_available_count",
    "missing_episode_count",
    "inconclusive_count",
    "unmapped_series_count",
    "options",
}


def _safe_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(value), _MAX_COUNT))
    except (TypeError, ValueError):
        return 0


def build_persisted_patrol_projection(result: dict[str, Any]) -> dict[str, Any]:
    """从公共审计结果重新构造可长期保存的字段白名单。"""
    data = result["data"] if isinstance(result.get("data"), dict) else {}
    snapshot = build_safe_patrol_snapshot(result)
    return {
        "as_of": snapshot["as_of"],
        "patrol_status": snapshot["patrol_status"],
        "findings_truncated": snapshot["findings_truncated"],
        "checked_series_count": _safe_count(data.get("checked_series_count")),
        "updates_available_count": _safe_count(data.get("updates_available_count")),
        "missing_episode_count": _safe_count(data.get("missing_episode_count")),
        "inconclusive_count": _safe_count(data.get("inconclusive_count")),
        "unmapped_series_count": _safe_count(data.get("unmapped_series_count")),
        "options": snapshot["options"],
    }


def validate_persisted_patrol_projection(value: Any) -> dict[str, Any] | None:
    """拒绝被篡改或包含额外字段的持久化投影。"""
    if not isinstance(value, dict) or set(value) != _PROJECTION_KEYS:
        return None
    snapshot = validate_safe_patrol_snapshot(
        {
            "as_of": value.get("as_of"),
            "patrol_status": value.get("patrol_status"),
            "findings_truncated": value.get("findings_truncated"),
            "options": value.get("options"),
        }
    )
    if snapshot is None:
        return None
    counts: dict[str, int] = {}
    for key in (
        "checked_series_count",
        "updates_available_count",
        "missing_episode_count",
        "inconclusive_count",
        "unmapped_series_count",
    ):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or (not 0 <= raw <= _MAX_COUNT):
            return None
        counts[key] = raw
    return {**snapshot, **counts}


def serialize_patrol_projection(result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    projection = build_persisted_patrol_projection(result)
    return (json.dumps(projection, ensure_ascii=False, separators=(",", ":")), projection)


def _load_projection(raw: object) -> dict[str, Any] | None:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return None
    return validate_persisted_patrol_projection(value)


def _safe_timestamp(value: object) -> str:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").isoformat(timespec="seconds")
    except ValueError:
        return ""


def _findings_from_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for option in options:
        findings.append(
            {
                "title": option["title"],
                "tmdb_id": option["tmdb_id"],
                "status": "updates_available",
                "missing_count": option["missing_count"],
                "missing_sample": [
                    {"season": option["season"], "episode": episode} for episode in option["episode_sample"]
                ],
                "missing_sample_truncated": False,
            }
        )
    return findings


def patrol_status(run_id: int | None = None) -> dict[str, Any]:
    """查询最近一次后台巡检，不触发新的审计或下载。"""
    if run_id is not None:
        if type(run_id) is not int or run_id <= 0:
            raise ValueError("run_id 必须是正整数")
        return _run_status(run_id)
    enabled = config.get_bool("AGENT_LIBRARY_PATROL_ENABLED", False)
    row = agent_library_patrol_repo.get_agent_library_patrol()
    if row is None:
        return {
            "ok": True,
            "status": "not_run",
            "summary": "自动缺集巡检已启用，但尚无完成记录" if enabled else "自动缺集巡检当前未启用，且尚无历史记录",
            "data": {
                "enabled": enabled,
                "task_status": "not_created",
                "outcome": "",
                "as_of": "",
                "checked_series_count": 0,
                "updates_available_count": 0,
                "missing_episode_count": 0,
                "inconclusive_count": 0,
                "unmapped_series_count": 0,
                "findings": [],
                "findings_truncated": False,
                "last_finished_at": "",
                "next_run_at": "",
                "continuation_pending": False,
                "cycle_checked_series_count": 0,
            },
            "suggestions": [
                "启用自动缺集巡检后会按周期执行。也可以发起一次全库后台巡检（需要确认），或说“检查媒体库有没有缺集”进行即时只读核对。"
            ],
        }
    return project_library_patrol_status(row, enabled=enabled)


def project_library_patrol_status(row: Any, *, enabled: bool) -> dict[str, Any]:
    """按明确持久事实生成唯一安全展示，不在领域投影中另读最近状态。"""
    projection: dict[str, Any] | None = _load_projection(row["projection_json"])
    projection_valid = projection is not None
    if projection is None:
        projection = {
            "as_of": "",
            "patrol_status": "inconclusive",
            "findings_truncated": False,
            "checked_series_count": 0,
            "updates_available_count": 0,
            "missing_episode_count": 0,
            "inconclusive_count": 1,
            "unmapped_series_count": 0,
            "options": [],
        }
    task_status = str(row["status"] or "pending")
    cycle_projection = _load_projection(row["cycle_accumulator_json"])
    continuation_pending = bool(str(row["cycle_as_of"] or "").strip())
    cycle_checked = (
        cycle_projection["checked_series_count"] if continuation_pending and cycle_projection is not None else 0
    )
    outcome = (
        str(row["outcome"] or projection["patrol_status"] or "")
        if projection_valid or row["outcome"] == "cancelled"
        else "inconclusive"
    )
    public_status = "running" if task_status == "running" else outcome or "pending"
    checked = projection["checked_series_count"]
    missing = projection["missing_episode_count"]
    updates = projection["updates_available_count"]
    if task_status == "running":
        summary = "自动缺集巡检正在运行"
    elif continuation_pending:
        summary = f"自动缺集巡检已核对 {cycle_checked} 部，等待继续下一批"
    elif outcome == "updates_available":
        summary = f"最近一次自动缺集巡检已核对 {checked} 部剧集，发现 {updates} 部共缺 {missing} 集"
    elif outcome == "up_to_date":
        summary = f"最近一次自动缺集巡检已核对 {checked} 部剧集，暂未发现已播缺集"
    elif outcome == "cancelled":
        summary = "本次自动缺集巡检已取消"
    elif outcome == "not_configured":
        summary = "最近一次自动缺集巡检未开始：媒体服务器或 TMDB 配置不完整"
    elif outcome == "unavailable":
        summary = "最近一次自动缺集巡检未完成：媒体服务器或 TMDB 当前不可用"
    else:
        detail = []
        inconclusive = int(projection["inconclusive_count"] or 0)
        unmapped = int(projection["unmapped_series_count"] or 0)
        if inconclusive:
            detail.append(f"{inconclusive} 部暂时无法确认")
        if unmapped:
            detail.append(f"{unmapped} 部缺少可靠 TMDB 映射")
        suffix = "；" + "，".join(detail) if detail else ""
        summary = f"最近一次自动缺集巡检覆盖不完整：已核对 {checked} 部{suffix}"
    findings = _findings_from_options(projection["options"])
    suggestions = ["巡检只读且不会自动下载。"]
    if findings:
        suggestions.append("可以继续说：把刚才巡检发现的缺集找资源。")
    elif not enabled:
        suggestions.append(
            "当前自动巡检已关闭；可以说“检查媒体库有没有缺集”进行即时只读核对，或发起需要确认的全库后台巡检。"
        )
    return {
        "ok": True,
        "status": public_status,
        "summary": summary,
        "data": {
            "enabled": enabled,
            "task_status": task_status,
            "outcome": outcome,
            "as_of": projection["as_of"],
            "checked_series_count": checked,
            "updates_available_count": updates,
            "missing_episode_count": missing,
            "inconclusive_count": projection["inconclusive_count"],
            "unmapped_series_count": projection["unmapped_series_count"],
            "findings": findings,
            "findings_truncated": projection["findings_truncated"],
            "last_finished_at": _safe_timestamp(row["last_finished_at"]),
            "next_run_at": _safe_timestamp(row["next_run_at"]),
            "continuation_pending": continuation_pending,
            "cycle_checked_series_count": cycle_checked,
        },
        "evidence": [
            {
                "source": "sqlite:agent_library_patrol",
                "description": "仅返回后台巡检的固定聚合计数与安全缺集候选，不包含媒体路径、服务地址或原始响应。",
                "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        ],
        "suggestions": suggestions,
    }


def build_safe_patrol_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    data = result["data"] if isinstance(result.get("data"), dict) else {}
    as_of = _safe_date(data.get("as_of"))
    options: list[dict[str, Any]] = []
    findings = data.get("findings")
    if not isinstance(findings, list):
        findings = []

    for finding in findings:
        if len(options) >= _MAX_OPTIONS:
            break
        if not isinstance(finding, dict):
            continue
        if finding.get("status") not in _ALLOWED_FINDING_STATUS:
            continue
        if bool(finding.get("missing_sample_truncated")):
            continue
        title = _safe_title(finding.get("title"))
        tmdb_id = _safe_tmdb_id(finding.get("tmdb_id"))
        if not title or not tmdb_id:
            continue
        grouped: dict[int, set[int]] = {}
        missing_sample = finding.get("missing_sample")
        if not isinstance(missing_sample, list):
            continue
        for item in missing_sample[:_MAX_EPISODES_PER_OPTION]:
            if not isinstance(item, dict):
                continue
            season = item.get("season")
            episode = item.get("episode")
            if (
                isinstance(season, bool)
                or not isinstance(season, int)
                or not 1 <= season <= 100
                or isinstance(episode, bool)
                or not isinstance(episode, int)
                or not 1 <= episode <= 1000
            ):
                continue
            grouped.setdefault(season, set()).add(episode)
        for season in sorted(grouped):
            episodes = sorted(grouped[season])
            if not episodes:
                continue
            options.append(
                {
                    "position": len(options) + 1,
                    "title": title,
                    "tmdb_id": tmdb_id,
                    "season": season,
                    "missing_count": len(episodes),
                    "episode_sample": episodes,
                }
            )
            if len(options) >= _MAX_OPTIONS:
                break

    return {
        "as_of": as_of,
        "patrol_status": _safe_patrol_status(result.get("status")),
        "findings_truncated": bool(data.get("findings_truncated")),
        "options": options,
    }


def validate_safe_patrol_snapshot(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {"as_of", "patrol_status", "findings_truncated", "options"}:
        return None
    as_of = _safe_date(value.get("as_of"))
    status = value.get("patrol_status")
    truncated = value.get("findings_truncated")
    raw_options = value.get("options")
    if status not in _SAFE_PATROL_STATUSES or not isinstance(truncated, bool):
        return None
    if not isinstance(raw_options, list) or len(raw_options) > _MAX_OPTIONS:
        return None
    options: list[dict[str, Any]] = []
    for expected_position, item in enumerate(raw_options, start=1):
        if not isinstance(item, dict) or set(item) != {
            "position",
            "title",
            "tmdb_id",
            "season",
            "missing_count",
            "episode_sample",
        }:
            return None
        title = _safe_title(item.get("title"))
        tmdb_id = _safe_tmdb_id(item.get("tmdb_id"))
        season = item.get("season")
        missing_count = item.get("missing_count")
        episode_sample = item.get("episode_sample")
        if (
            item.get("position") != expected_position
            or not title
            or not tmdb_id
            or isinstance(season, bool)
            or not isinstance(season, int)
            or not 1 <= season <= 100
            or isinstance(missing_count, bool)
            or not isinstance(missing_count, int)
            or not 1 <= missing_count <= _MAX_EPISODES_PER_OPTION
            or not isinstance(episode_sample, list)
            or len(episode_sample) != missing_count
            or any(
                isinstance(episode, bool) or not isinstance(episode, int) or not 1 <= episode <= 1000
                for episode in episode_sample
            )
            or episode_sample != sorted(set(episode_sample))
        ):
            return None
        options.append(
            {
                "position": expected_position,
                "title": title,
                "tmdb_id": tmdb_id,
                "season": season,
                "missing_count": missing_count,
                "episode_sample": list(episode_sample),
            }
        )
    return {
        "as_of": as_of,
        "patrol_status": status,
        "findings_truncated": truncated,
        "options": options,
    }


def _safe_patrol_status(value: Any) -> str:
    status = str(value or "").strip()
    return status if status in _SAFE_PATROL_STATUSES else "inconclusive"


def _safe_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _safe_title(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    title = unicodedata.normalize("NFKC", value).strip()
    if not title or any(unicodedata.category(char).startswith("C") for char in title):
        return ""
    lowered = title.casefold()
    if any(
        marker in lowered
        for marker in (
            "://",
            "magnet:",
            "token=",
            "password=",
            "passwd=",
            "cookie=",
            "authorization:",
            "api_key=",
            "api-key=",
            "secret=",
            "bearer ",
            "/volume/",
            "/mnt/",
            "/media/",
            "/home/",
            "/srv/",
            "/data/",
            "\\volume\\",
            "\\mnt\\",
            "\\media\\",
            "\\home\\",
        )
    ) or re.search(r"(?:^|\s)[a-z]:[\\/]", lowered):
        return ""
    return title[:120]


def _safe_tmdb_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() and 1 <= len(text) <= 10 else ""


def empty_patrol_projection(*, as_of: str) -> dict[str, Any]:
    return {
        "as_of": as_of,
        "patrol_status": "inconclusive",
        "findings_truncated": False,
        "checked_series_count": 0,
        "updates_available_count": 0,
        "missing_episode_count": 0,
        "inconclusive_count": 0,
        "unmapped_series_count": 0,
        "options": [],
    }


def load_patrol_progress(
    raw: object,
    *,
    as_of: str,
    resumed: bool = False,
) -> dict[str, Any]:
    """续跑必须携带同日有效进度；不能保留游标却把历史计数清零。"""
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        value = None
    validated = validate_persisted_patrol_projection(value)
    if validated is not None and validated["as_of"] == as_of:
        return validated
    if resumed:
        raise ValueError("InvalidPatrolProgress")
    return empty_patrol_projection(as_of=as_of)


def merge_patrol_progress(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """累加聚合计数，并把可执行缺集选项限制为最多 20 组。"""
    options: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in [*previous["options"], *current["options"]]:
        identity = (str(item["tmdb_id"]), int(item["season"]))
        if identity in seen:
            continue
        seen.add(identity)
        options.append({**item, "position": len(options) + 1})
        if len(options) >= 20:
            break
    return {
        "as_of": current["as_of"] or previous["as_of"],
        "patrol_status": "inconclusive",
        "findings_truncated": bool(
            previous["findings_truncated"]
            or current["findings_truncated"]
            or len(previous["options"]) + len(current["options"]) > len(options)
        ),
        "checked_series_count": (previous["checked_series_count"] + current["checked_series_count"]),
        "updates_available_count": (previous["updates_available_count"] + current["updates_available_count"]),
        "missing_episode_count": (previous["missing_episode_count"] + current["missing_episode_count"]),
        "inconclusive_count": (previous["inconclusive_count"] + current["inconclusive_count"]),
        "unmapped_series_count": max(previous["unmapped_series_count"], current["unmapped_series_count"]),
        "options": options,
    }


def finalize_patrol_progress(projection: dict[str, Any], *, terminal_status: str, resumed: bool) -> dict[str, Any]:
    finalized = dict(projection)
    if terminal_status in {"not_configured", "unavailable"}:
        finalized["patrol_status"] = terminal_status
    elif terminal_status in {"failed", "inconclusive"}:
        finalized["patrol_status"] = "inconclusive"
    elif finalized["inconclusive_count"] or finalized["unmapped_series_count"]:
        finalized["patrol_status"] = "inconclusive"
    elif terminal_status == "updates_available" or (resumed and finalized["updates_available_count"]):
        finalized["patrol_status"] = "updates_available"
    else:
        finalized["patrol_status"] = "up_to_date"
    return finalized


def _run_status(run_id: int) -> dict[str, Any]:
    row = db.get_task_run(run_id)
    if row is None or row["task_name"] != "agent_library_patrol":
        return {"ok": False, "status": "unknown", "summary": "全库巡检状态暂时无法确认"}
    status = str(row["status"])
    if status in {"queued", "running"}:
        return {
            "ok": True,
            "status": status,
            "summary": "全库缺集巡检正在运行" if status == "running" else "全库缺集巡检正在排队",
            "data": {
                "run_id": run_id,
                "task": {"status": status, "started_at": str(row["started_at"] or ""), "stats": {}},
            },
        }
    value = project_library_patrol_status(
        {
            "projection_json": row["result"],
            "status": "pending",
            "outcome": status,
            "cycle_accumulator_json": "{}",
            "cycle_as_of": "",
            "last_finished_at": row["finished_at"],
            "next_run_at": "",
        },
        enabled=config.get_bool("AGENT_LIBRARY_PATROL_ENABLED", False),
    )
    data = value["data"]
    data["run_id"] = run_id
    data["task"] = {
        "status": value["status"],
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "stats": {
            "checked_series": data["checked_series_count"],
            "updates_available": data["updates_available_count"],
            "missing_episodes": data["missing_episode_count"],
        },
    }
    value["ok"] = value["status"] in {"updates_available", "up_to_date"}
    value["error"] = "" if value["ok"] else value["summary"]
    return value


def patrol_policy() -> dict[str, Any]:
    return {
        "enabled": config.get_bool("AGENT_LIBRARY_PATROL_ENABLED", False),
        "interval_hours": max(1, min(config.get_int("AGENT_LIBRARY_PATROL_INTERVAL_HOURS", 24), 168)),
        "max_series": max(1, min(config.get_int("AGENT_LIBRARY_PATROL_MAX_SERIES", 50), 100)),
    }


def prepare_patrol() -> dict[str, Any]:
    """只读冻结策略及单例任务身份；不创建任务或预留运行编号。"""
    return {"policy": patrol_policy(), "state": patrol_task_state(agent_library_patrol_repo.get_agent_library_patrol())}


def run_patrol(
    expected_snapshot: dict[str, Any], *, check_cancelled: Callable[[], None] | None = None
) -> dict[str, Any]:
    """按预检策略和任务状态条件排队；保留当前逻辑周期及 run_id。"""
    if prepare_patrol() != expected_snapshot:
        return {"ok": False, "status": "conflict", "summary": "巡检策略或任务状态已变化，请重新预检"}
    if not expected_snapshot["policy"]["enabled"]:
        return {"ok": False, "status": "disabled", "summary": "全库缺集巡检当前未启用"}
    from app.modules.agent_library_patrol_scheduler import get_agent_library_patrol_scheduler

    outcome = get_agent_library_patrol_scheduler().trigger_now(
        expected_state=expected_snapshot["state"],
        check_cancelled=check_cancelled,
    )
    status = outcome["status"]
    if status not in {"queued", "already_running"}:
        return {"ok": False, "status": status, "summary": "当前无法安排全库缺集巡检，请重新预检"}
    reused = status == "already_running"
    return {
        "ok": True,
        "status": "accepted",
        "summary": "全库缺集巡检已在运行，本次未重复创建任务" if reused else "已按当前策略把全库缺集巡检排到现在",
        "data": {
            "queued": not reused,
            "reused": reused,
            "task_status": "running" if reused else "pending",
            "run_id": int(outcome["run_id"]),
        },
        "suggestions": ["稍后可查询本次全库巡检结果。"],
    }
