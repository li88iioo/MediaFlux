"""持久化全库缺集巡检的安全投影与只读查询。"""
from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from app import config, database as db
from app.agent.models import Evidence, ToolResult
from app.agent.recent_patrol import (
    build_safe_patrol_snapshot,
    validate_safe_patrol_snapshot,
)

_MAX_COUNT = 1_000_000
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


def _safe_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(value), _MAX_COUNT))
    except (TypeError, ValueError):
        return 0


def build_persisted_patrol_projection(result: ToolResult) -> dict[str, Any]:
    """从 ToolResult 重新构造可长期保存的字段白名单。"""
    data = result.data if isinstance(result.data, dict) else {}
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
    snapshot = validate_safe_patrol_snapshot({
        "as_of": value.get("as_of"),
        "patrol_status": value.get("patrol_status"),
        "findings_truncated": value.get("findings_truncated"),
        "options": value.get("options"),
    })
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
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= _MAX_COUNT:
            return None
        counts[key] = raw
    return {**snapshot, **counts}


def serialize_patrol_projection(result: ToolResult) -> tuple[str, dict[str, Any]]:
    projection = build_persisted_patrol_projection(result)
    return json.dumps(projection, ensure_ascii=False, separators=(",", ":")), projection


def _load_projection(raw: object) -> dict[str, Any] | None:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
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
        findings.append({
            "title": option["title"],
            "tmdb_id": option["tmdb_id"],
            "status": "updates_available",
            "missing_count": option["missing_count"],
            "missing_sample": [
                {"season": option["season"], "episode": episode}
                for episode in option["episode_sample"]
            ],
            "missing_sample_truncated": False,
        })
    return findings


def patrol_status_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise ValueError("该工具不接受参数")
    return {}


def get_library_patrol_status(_arguments: dict[str, Any]) -> ToolResult:
    """查询最近一次后台巡检，不触发新的审计或下载。"""
    enabled = config.get_bool("AGENT_LIBRARY_PATROL_ENABLED", False)
    row = db.get_agent_library_patrol()
    if row is None:
        return ToolResult(
            ok=True,
            status="not_run",
            summary=(
                "自动缺集巡检已启用，但尚无完成记录"
                if enabled
                else "自动缺集巡检当前未启用，且尚无历史记录"
            ),
            data={
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
            suggestions=[
                "启用自动缺集巡检后会按周期执行。也可以发起一次全库后台巡检（需要确认），或说“检查媒体库有没有缺集”进行即时只读核对。"
            ],
        )

    projection = _load_projection(row["projection_json"])
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
        cycle_projection["checked_series_count"]
        if continuation_pending and cycle_projection is not None
        else 0
    )
    outcome = (
        str(row["outcome"] or projection["patrol_status"] or "")
        if projection_valid
        else "inconclusive"
    )
    public_status = "running" if task_status == "running" else (outcome or "pending")
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
        suggestions.append("当前自动巡检已关闭；可以说“检查媒体库有没有缺集”进行即时只读核对，或发起需要确认的全库后台巡检。")
    return ToolResult(
        ok=True,
        status=public_status,
        summary=summary,
        data={
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
        evidence=[Evidence(
            "sqlite:agent_library_patrol",
            "仅返回后台巡检的固定聚合计数与安全缺集候选，不包含媒体路径、服务地址或原始响应。",
            datetime.now().astimezone().isoformat(timespec="seconds"),
        )],
        suggestions=suggestions,
    )
