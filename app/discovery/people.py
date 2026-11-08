"""TMDB演职员与明确人物ID的电影作品表；默认完整读取，不承担展示或人物猜选。"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

from app import config
from app.clients.tmdb import TMDBClient, close_tmdb_client
from app.discovery.models import ProviderError, ProviderNotConfigured
from app.modules.media_identity import normalize_tmdb_id

_ROLES = {"all", "acting", "directing", "writing", "production"}
_DEPARTMENTS = {"directing": "Directing", "writing": "Writing", "production": "Production"}
_CREDIT_FIELDS = {"character", "job", "department", "credit_id", "order", "roles", "jobs"}


def _paging(limit: int | None, offset: int) -> None:
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("limit必须为正整数或None（完整表）")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset必须为非负整数")


def _page(items: list, limit: int | None, offset: int) -> dict:
    selected = items[offset:] if limit is None else items[offset:offset + limit]
    has_more = offset + len(selected) < len(items)
    return {"items": selected, "total": len(items), "returned": len(selected), "has_more": has_more,
            "next_offset": offset + len(selected) if has_more else None}


def _fetch(method: str, *args, **kwargs) -> tuple[dict | None, dict]:
    if not config.get_bool("DISCOVERY_ENABLED"):
        return None, {"ok": False, "status": "disabled", "query_state": "not_queried", "source": "tmdb"}
    client = None
    try:
        client = TMDBClient()
        result = getattr(client, method)(*args, **kwargs, deadline_at=time.monotonic() + 20.0, retries=0)
        return result, {"ok": True, "status": "completed", "source": "tmdb"}
    except ProviderError as exc:
        return None, {"ok": False, "status": exc.code, "source": "tmdb", "error": exc.safe_message,
                      "query_state": "not_queried" if isinstance(exc, ProviderNotConfigured) else "failed"}
    except (OSError, RuntimeError, ValueError):
        return None, {"ok": False, "status": "unavailable", "source": "tmdb",
                      "query_state": "failed", "error": "TMDB查询未完成"}
    finally:
        close_tmdb_client(client)


def _credit_complete(row: Any, kind: str) -> bool:
    if not isinstance(row, dict) or not row.get("name"):
        return False
    try:
        normalize_tmdb_id(row.get("id"))
    except ValueError:
        return False
    key, field = ("roles", "character") if kind == "cast" else ("jobs", "job")
    roles = row.get(key)
    if roles is None:
        return bool(row.get(field))
    return isinstance(roles, list) and bool(roles) and all(
        isinstance(item, dict) and item.get(field)
        and (item.get("episode_count") is None or (type(item["episode_count"]) is int and item["episode_count"] >= 0))
        for item in roles)


def people(query: str, page: int = 1) -> dict:
    """姓名发现只返回完整候选页；由调用者核对人物身份，不自动选择同名人物。"""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("人物姓名不能为空")
    if type(page) is not int or page < 1:
        raise ValueError("page必须为正整数")
    query = query.strip()
    payload, state = _fetch("search_people", query, page=page)
    request = {"query": query, "page": page}
    if payload is None:
        return {**state, "request": request}
    items = payload["results"]
    incomplete = False
    for item in items:
        try:
            normalize_tmdb_id(item.get("id") if isinstance(item, dict) else None)
        except ValueError:
            incomplete = True
    has_more = page < payload["total_pages"]
    return {**state, "request": request, "items": items, "returned": len(items),
            "total": payload["total_results"], "page": payload["page"], "total_pages": payload["total_pages"],
            "has_more": has_more, "next_page": page + 1 if has_more else None,
            "source_incomplete": incomplete, "status": "partial" if incomplete else "completed",
            "query_state": "partial" if incomplete else "available" if items else "empty"}


def credits(tmdb_id: str, media_type: str, season_number: int | None = None,
            limit: int | None = None, offset: int = 0) -> dict:
    """按精确作品/季读取完整cast、crew及roles/jobs；空表仅指本次TMDB响应。"""
    tmdb_id = normalize_tmdb_id(tmdb_id)
    if not isinstance(media_type, str) or media_type not in {"movie", "tv"}:
        raise ValueError("media_type仅支持movie或tv")
    if season_number is not None and (media_type != "tv" or type(season_number) is not int or season_number < 0):
        raise ValueError("season_number仅适用于tv，必须为非负整数（含0特别篇）")
    _paging(limit, offset)
    request = {"tmdb_id": tmdb_id, "media_type": media_type, "season_number": season_number}
    payload, state = _fetch("media_credits", tmdb_id, media_type, season_number=season_number)
    if payload is None:
        return {**state, "request": request}
    coverage = {kind: _page(payload[kind], limit, offset) for kind in ("cast", "crew")}
    incomplete = any(not _credit_complete(row, kind) for kind in coverage for row in payload[kind])
    has_more = any(page["has_more"] for page in coverage.values())
    empty = not any(payload.values())
    return {**state, "request": request, "scope": "season_aggregate" if season_number is not None else
            "series_aggregate" if media_type == "tv" else "movie",
            "query_state": "empty" if empty else "partial" if incomplete else "available",
            "status": "partial" if incomplete else "completed", "source_incomplete": incomplete,
            "cast": coverage["cast"]["items"], "crew": coverage["crew"]["items"],
            "cast_total": coverage["cast"]["total"], "crew_total": coverage["crew"]["total"],
            "cast_returned": coverage["cast"]["returned"], "crew_returned": coverage["crew"]["returned"],
            "has_more": has_more, "next_offset": offset + limit if has_more and limit is not None else None,
            "complete": not incomplete and not has_more and offset == 0}


def filmography(person_id: str, role: str = "all", include_upcoming: bool = True,
                limit: int | None = None, offset: int = 0) -> dict:
    """明确人物ID的完整电影作品/角色表；默认包含未来及未知日期，不按热度猜选人物。"""
    person_id = normalize_tmdb_id(person_id)
    if not isinstance(role, str) or role not in _ROLES or type(include_upcoming) is not bool:
        raise ValueError("role仅支持all/acting/directing/writing/production，include_upcoming必须为布尔值")
    _paging(limit, offset)
    request = {"person_id": person_id, "role": role, "include_upcoming": include_upcoming}
    payload, state = _fetch("person_movie_credits", person_id)
    if payload is None:
        return {**state, "request": request}
    today = datetime.now().astimezone().date()
    movies: dict[str, dict] = {}
    invalid = excluded_upcoming = excluded_undated = 0
    for kind in ("cast", "crew"):
        for raw in payload[kind]:
            if not isinstance(raw, dict):
                invalid += 1
                continue
            if role != "all" and kind != ("cast" if role == "acting" else "crew"):
                continue
            if role in _DEPARTMENTS:
                department = raw.get("department")
                if not isinstance(department, str) or not department:
                    invalid += 1
                    continue
                if department != _DEPARTMENTS[role]:
                    continue
                if role == "directing":
                    job = raw.get("job")
                    if not isinstance(job, str) or not job:
                        invalid += 1
                        continue
                    if job != "Director":
                        continue
            try:
                identity = normalize_tmdb_id(raw.get("id"))
            except ValueError:
                invalid += 1
                continue
            try:
                released = date.fromisoformat(str(raw.get("release_date") or ""))
            except ValueError:
                released = None
            if not include_upcoming and (released is None or released > today):
                excluded_undated += int(released is None)
                excluded_upcoming += int(released is not None)
                continue
            item = movies.setdefault(identity, {
                **{key: value for key, value in raw.items() if key not in _CREDIT_FIELDS},
                "tmdb_id": identity, "media_type": "movie", "title": raw.get("title") or raw.get("original_title") or "",
                "release_date": released.isoformat() if released else "", "year": str(released.year) if released else "",
                "upcoming": released > today if released else None, "roles": [],
            })
            credit = {"kind": kind, **{key: value for key, value in raw.items() if key in _CREDIT_FIELDS}}
            if credit not in item["roles"]:
                item["roles"].append(credit)
    items = sorted(movies.values(), key=lambda item: (item["release_date"] or "9999-99-99", item["title"]))
    page = _page(items, limit, offset)
    return {**state, "request": request, **page, "as_of": today.isoformat(),
            "query_state": "partial" if invalid else "available" if items else "empty",
            "status": "partial" if invalid else "completed", "source_incomplete": bool(invalid),
            "library_check_items": [{key: item[key] for key in ("tmdb_id", "media_type", "title", "year")} for item in page["items"]],
            "source_counts": {key: len(payload[key]) for key in ("cast", "crew")}, "invalid_credits": invalid,
            "excluded_upcoming": excluded_upcoming, "excluded_undated": excluded_undated,
            "complete": not invalid and not page["has_more"] and offset == 0}
