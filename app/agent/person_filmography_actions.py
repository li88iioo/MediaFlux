"""人物电影作品表的受控 TMDB 查询。"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime
from typing import Any

from app import config
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.clients.tmdb import TMDBClient, close_tmdb_client

_ALLOWED_ARGUMENTS = {"person", "role", "include_upcoming", "limit"}
_ALLOWED_ROLES = {"directing", "acting", "writing", "production"}
_ROLE_DEPARTMENTS = {
    "directing": "Directing",
    "writing": "Writing",
    "production": "Production",
}
_ROLE_LABELS = {
    "directing": "导演",
    "acting": "参演",
    "writing": "编剧",
    "production": "制片",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _visible_text(value: object, *, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        return ""
    return " ".join(normalized.split())[:limit]


def _identity_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _positive_int(value: object, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentToolError("limit 必须是整数")
    if not 1 <= value <= maximum:
        raise AgentToolError(f"limit 必须在 1 到 {maximum} 之间")
    return value


def person_filmography_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _ALLOWED_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    person = _visible_text(arguments.get("person"), limit=120)
    if not person:
        raise AgentToolError("person 必须是 1 到 120 个可见字符")
    role = str(arguments.get("role") or "directing").strip().casefold()
    if role not in _ALLOWED_ROLES:
        raise AgentToolError("role 仅支持 directing、acting、writing 或 production")
    include_upcoming = arguments.get("include_upcoming", False)
    if not isinstance(include_upcoming, bool):
        raise AgentToolError("include_upcoming 必须是布尔值")
    return {
        "person": person,
        "role": role,
        "include_upcoming": include_upcoming,
        "limit": _positive_int(arguments.get("limit"), default=50, maximum=100),
    }


def _popularity(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        result = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _select_person(
    candidates: list[dict[str, Any]], *, query: str, role: str
) -> dict[str, Any] | None:
    valid = [
        item
        for item in candidates
        if str(item.get("id") or "").isdigit()
        and _visible_text(item.get("name"), limit=160)
    ]
    if not valid:
        return None
    target = _identity_key(query)
    exact = [item for item in valid if _identity_key(item.get("name")) == target]
    expected_department = _ROLE_DEPARTMENTS.get(role, "Acting")
    department_matches = [
        item
        for item in (exact or valid)
        if str(item.get("known_for_department") or "").casefold()
        == expected_department.casefold()
    ]
    pool = department_matches or exact or valid
    return max(pool, key=lambda item: _popularity(item.get("popularity")))


def _release_date(value: object) -> str:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        return ""
    try:
        date.fromisoformat(normalized)
    except ValueError:
        return ""
    return normalized


def _credit_rows(
    credits: dict[str, list[dict[str, Any]]], *, role: str
) -> list[dict[str, Any]]:
    if role == "acting":
        return [
            {**item, "__credit_job": "Actor"}
            for item in credits.get("cast", [])
        ]
    department = _ROLE_DEPARTMENTS[role]
    return [
        {**item, "__credit_job": str(item.get("job") or department)}
        for item in credits.get("crew", [])
        if str(item.get("department") or "").casefold() == department.casefold()
        and (role != "directing" or str(item.get("job") or "").casefold() == "director")
    ]


def person_filmography(arguments: dict[str, Any]) -> ToolResult:
    normalized = person_filmography_arguments(arguments)
    if not config.get_bool("DISCOVERY_ENABLED"):
        return ToolResult(
            ok=False,
            status="disabled",
            summary="影视探索功能当前已关闭",
            data={"person": normalized["person"], "items": [], "count": 0},
            evidence=[Evidence("discovery_config", "检查影视探索总开关。", _now())],
            suggestions=["请由管理员在设置中启用影视探索后重试。"],
            error="影视探索功能未启用。",
        )

    as_of = datetime.now().astimezone().date()
    client = TMDBClient()
    try:
        candidates = client.search_people(normalized["person"], limit=10)
        selected = _select_person(
            candidates,
            query=normalized["person"],
            role=normalized["role"],
        )
        if selected is None:
            return ToolResult(
                ok=True,
                status="empty",
                summary="TMDB 中没有找到匹配人物",
                data={
                    "requested_person": normalized["person"],
                    "role": normalized["role"],
                    "as_of": as_of.isoformat(),
                    "count": 0,
                    "items": [],
                },
                evidence=[Evidence("tmdb_person", "查询 TMDB 人物索引。", _now())],
                suggestions=["可尝试人物全名或英文名。"],
            )
        person_id = str(selected["id"])
        credits = client.person_movie_credits(person_id)
    except (OSError, RuntimeError, ValueError):
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="TMDB 人物作品数据暂时不可用",
            data={"person": normalized["person"], "items": [], "count": 0},
            evidence=[Evidence("tmdb_person", "查询 TMDB 人物及电影作品。", _now())],
            suggestions=["请稍后重试。"],
            error="TMDB 人物作品数据暂时不可用。",
        )
    finally:
        close_tmdb_client(client)

    today = as_of
    items_by_id: dict[str, dict[str, Any]] = {}
    excluded_upcoming = 0
    excluded_undated = 0
    for raw in _credit_rows(credits, role=normalized["role"]):
        tmdb_id = str(raw.get("id") or "").strip()
        title = _visible_text(raw.get("title") or raw.get("original_title"), limit=240)
        if not tmdb_id.isdigit() or not title:
            continue
        released = _release_date(raw.get("release_date"))
        if not normalized["include_upcoming"]:
            if not released:
                excluded_undated += 1
                continue
            if date.fromisoformat(released) > today:
                excluded_upcoming += 1
                continue
        item = items_by_id.setdefault(
            tmdb_id,
            {
                "tmdb_id": tmdb_id,
                "media_type": "movie",
                "title": title,
                "original_title": _visible_text(raw.get("original_title"), limit=240),
                "release_date": released,
                "year": released[:4] if released else "",
                "credit_jobs": [],
                "upcoming": bool(released and date.fromisoformat(released) > today),
            },
        )
        job = _visible_text(raw.get("__credit_job"), limit=80)
        if job and job not in item["credit_jobs"]:
            item["credit_jobs"].append(job)

    ordered = sorted(
        items_by_id.values(),
        key=lambda item: (item["release_date"] or "9999-99-99", item["title"]),
    )
    limit = normalized["limit"]
    selected_name = _visible_text(selected.get("name"), limit=160)
    alternatives = [
        {
            "tmdb_person_id": str(item.get("id") or ""),
            "name": _visible_text(item.get("name"), limit=160),
            "department": _visible_text(item.get("known_for_department"), limit=80),
        }
        for item in candidates
        if str(item.get("id") or "") != person_id
    ][:4]
    returned = ordered[:limit]
    return ToolResult(
        ok=True,
        status="success",
        summary=(
            f"已按上映日期列出 {selected_name} 的 {len(returned)} 部"
            f"{_ROLE_LABELS[normalized['role']]}电影作品"
        ),
        data={
            "requested_person": normalized["person"],
            "person": {
                "tmdb_person_id": person_id,
                "name": selected_name,
                "department": _visible_text(
                    selected.get("known_for_department"), limit=80
                ),
            },
            "alternatives": alternatives,
            "role": normalized["role"],
            "as_of": today.isoformat(),
            "include_upcoming": normalized["include_upcoming"],
            "count": len(returned),
            "total": len(ordered),
            "truncated": len(ordered) > limit,
            "excluded_upcoming": excluded_upcoming,
            "excluded_undated": excluded_undated,
            "items": returned,
            "library_check_items": [
                {
                    "tmdb_id": item["tmdb_id"],
                    "media_type": item["media_type"],
                    "title": item["title"],
                    "year": item["year"],
                }
                for item in returned
            ],
        },
        evidence=[
            Evidence(
                "tmdb_person_movie_credits",
                "读取 TMDB 人物电影演职员表并按上映日期排序。",
                _now(),
            )
        ],
        suggestions=[
            "如需核对本地收录，请把 library_check_items 一次传给媒体库批量核对。"
        ],
    )
