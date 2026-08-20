"""全库剧集巡检批次的安全进度合并。"""
from __future__ import annotations

import json
from typing import Any

from app.agent.library_patrol_status import validate_persisted_patrol_projection


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


def load_patrol_progress(raw: object, *, as_of: str) -> dict[str, Any]:
    """只接受经过白名单校验且属于同一截止日期的进度。"""
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        value = None
    validated = validate_persisted_patrol_projection(value)
    if validated is not None and validated["as_of"] == as_of:
        return validated
    return empty_patrol_projection(as_of=as_of)


def merge_patrol_progress(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
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
        "checked_series_count": (
            previous["checked_series_count"] + current["checked_series_count"]
        ),
        "updates_available_count": (
            previous["updates_available_count"] + current["updates_available_count"]
        ),
        "missing_episode_count": (
            previous["missing_episode_count"] + current["missing_episode_count"]
        ),
        "inconclusive_count": (
            previous["inconclusive_count"] + current["inconclusive_count"]
        ),
        "unmapped_series_count": max(
            previous["unmapped_series_count"], current["unmapped_series_count"]
        ),
        "options": options,
    }


def finalize_patrol_progress(
    projection: dict[str, Any], *, terminal_status: str, resumed: bool
) -> dict[str, Any]:
    finalized = dict(projection)
    if terminal_status in {"not_configured", "unavailable"}:
        finalized["patrol_status"] = terminal_status
    elif terminal_status in {"failed", "inconclusive"}:
        finalized["patrol_status"] = "inconclusive"
    elif finalized["inconclusive_count"] or finalized["unmapped_series_count"]:
        finalized["patrol_status"] = "inconclusive"
    elif terminal_status == "updates_available" or (
        resumed and finalized["updates_available_count"]
    ):
        finalized["patrol_status"] = "updates_available"
    else:
        finalized["patrol_status"] = "up_to_date"
    return finalized
