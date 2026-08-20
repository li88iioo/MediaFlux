"""媒体更新核对：剧集审计已播集，电影核对本地存在性并给出安全资源跟进。"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

from app.agent.episode_audit import audit_series_episodes
from app.agent.models import Evidence, ToolResult
from app.services import search_media_servers


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


_TITLE_NOISE_RE = re.compile(r"[\W_]+", re.UNICODE)
_MOVIE_SEARCH_LIMIT = 50
_MOVIE_ITEM_LIMIT = 8


def _normalized_title(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _TITLE_NOISE_RE.sub("", text)


def _safe_movie_title(value: object) -> str:
    return " ".join(str(value or "").split())[:160]


def _safe_movie_year(value: object) -> str:
    year = str(value or "").strip()
    return year if re.fullmatch(r"(?:18|19|20|21|22)\d{2}", year) else ""


def _check_movie_updates(arguments: dict[str, Any]) -> ToolResult:
    """只做可证明的本地存在性核对，不把资源站候选误报成版本更新。"""
    query = arguments["query"]
    normalized_query = _normalized_title(query)
    try:
        raw_sources = search_media_servers(query, limit=_MOVIE_SEARCH_LIMIT)
    except Exception:
        raw_sources = []
        search_failed = True
    else:
        search_failed = False

    source_records: list[dict[str, Any]] = []
    exact_matches: list[dict[str, str]] = []
    possible_matches: list[dict[str, str]] = []
    unavailable = 0
    available = 0
    truncated_sources = 0
    for source in raw_sources:
        source_unavailable = bool(source.get("error"))
        if source_unavailable:
            unavailable += 1
        else:
            available += 1
        server_type = str(source.get("server_type") or "").strip().casefold()
        if server_type not in {"jellyfin", "emby"}:
            server_type = "media_server"
        server_name = _safe_movie_title(source.get("server_name") or "媒体服务器")[:80]
        source_items: list[dict[str, str]] = []
        raw_items = source.get("items", []) if not source_unavailable else []
        source_truncated = len(raw_items) >= _MOVIE_SEARCH_LIMIT
        if source_truncated:
            truncated_sources += 1
        if not source_unavailable:
            for item in raw_items:
                if str(getattr(item, "type", "") or "").strip().casefold() != "movie":
                    continue
                title = _safe_movie_title(getattr(item, "name", "") or getattr(item, "display_name", ""))
                if not title:
                    continue
                public = {
                    "title": title,
                    "year": _safe_movie_year(getattr(item, "year", "")),
                    "server_type": server_type,
                    "server_name": server_name,
                }
                exact = bool(normalized_query and _normalized_title(title) == normalized_query)
                public["match"] = "exact_title" if exact else "possible_title"
                source_items.append(public)
                (exact_matches if exact else possible_matches).append(public)
        source_records.append({
            "server_type": server_type,
            "server_name": server_name,
            "status": "unavailable" if source_unavailable else "ready",
            "truncated": source_truncated,
            "items": source_items,
        })

    # 核对结论扫描全部服务器结果；仅公共响应限制为 8 条，并优先展示精确同名。
    selected_matches = (exact_matches + possible_matches)[:_MOVIE_ITEM_LIMIT]
    selected_ids = {id(item) for item in selected_matches}
    sources: list[dict[str, Any]] = []
    for source in source_records:
        public_items = [item for item in source["items"] if id(item) in selected_ids]
        sources.append({
            "server_type": source["server_type"],
            "server_name": source["server_name"],
            "status": source["status"],
            "truncated": source["truncated"],
            "returned": len(public_items),
            "items": public_items,
        })

    exact_count = len(exact_matches)
    possible_count = len(possible_matches)
    if search_failed or (raw_sources and available == 0):
        ok = False
        status = "unavailable"
        local_match_status = "indeterminate"
        summary = "媒体服务器暂时不可用，无法核对电影是否已入库"
        suggestions = ["请检查媒体服务器连通性后重试。"]
    elif not raw_sources:
        ok = False
        status = "not_configured"
        local_match_status = "not_configured"
        summary = "没有可搜索的媒体服务器，无法核对电影是否已入库"
        suggestions = ["请先完整配置并启用 Jellyfin 或 Emby。"]
    elif exact_count:
        ok = True
        status = "comparison_unavailable"
        local_match_status = "found"
        summary = f"媒体库中找到 {exact_count} 个同名电影条目；版本更新仍需人工核对"
        suggestions = ["可继续搜索资源站候选；结果只代表可用资源，不代表一定优于本地版本。"]
    elif possible_count:
        ok = True
        status = "review_required"
        local_match_status = "possible"
        summary = f"找到 {possible_count} 个可能匹配的电影条目，标题未能精确核验"
        suggestions = ["请先核对片名和年份，再搜索资源站候选。"]
        if truncated_sources:
            suggestions.append(f"有 {truncated_sources} 个媒体服务器的搜索结果达到上限，线索可能不完整。")
    elif truncated_sources:
        ok = True
        status = "review_required"
        local_match_status = "indeterminate"
        summary = "媒体服务器搜索结果达到上限，无法证明电影未入库"
        suggestions = ["请使用更精确的片名或年份再次核对，再决定是否搜索资源站。"]
    else:
        ok = True
        status = "not_found"
        local_match_status = "not_found"
        summary = "可用媒体库中未找到同名电影条目"
        suggestions = ["可继续搜索资源站资源；未入库不等同于资源站一定有可用版本。"]
    if unavailable and available:
        status = "partial"
        suggestions.append(f"另有 {unavailable} 个媒体服务器暂时不可用，结论可能不完整。")

    candidate_years = {item["year"] for item in exact_matches if item.get("year")}
    resource_arguments: dict[str, Any] = {"title": query, "media_type": "movie"}
    if len(candidate_years) == 1:
        resource_arguments["year"] = next(iter(candidate_years))

    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "query": query,
            "media_type": "movie",
            "tmdb_id": arguments.get("tmdb_id", ""),
            "check_definition": "movie_library_presence_with_resource_followup",
            "local_match_status": local_match_status,
            "exact_match_count": exact_count,
            "possible_match_count": possible_count,
            "available_server_count": available,
            "unavailable_server_count": unavailable,
            "search_truncated_server_count": truncated_sources,
            "matches_truncated": max(0, exact_count + possible_count - _MOVIE_ITEM_LIMIT),
            "sources": sources,
            "resource_followups": [{
                "tool": "indexer.search_resources",
                "label": f"搜索《{_safe_movie_title(query)}》资源候选"[:80],
                "arguments": resource_arguments,
            }],
            "comparison": {
                "available": False,
                "reason": "媒体服务器当前未提供可可靠比较的文件版本、发行版、分辨率与编码基线。",
            },
        },
        evidence=[
            Evidence(
                "media_servers",
                "按标题读取已启用 Jellyfin / Emby 的电影结果；只将规范化标题完全一致标记为同名条目。",
                _now(),
            ),
            Evidence(
                "agent_capability",
                "未把资源站标题、文件大小或热度当作电影版本更新证明。",
                _now(),
            ),
        ],
        suggestions=suggestions,
    )


def check_library_updates(arguments: dict[str, Any]) -> ToolResult:
    """核对媒体更新；剧集检查缺集，电影提供本地存在性与安全资源跟进。"""
    media_type = arguments.get("media_type", "auto")
    if media_type == "movie":
        return _check_movie_updates(arguments)

    audit_arguments = {
        "query": arguments["query"],
        "tmdb_id": arguments.get("tmdb_id", ""),
        "season": arguments.get("season"),
        "as_of": arguments["as_of"],
    }
    result = audit_series_episodes(audit_arguments)
    result.data = dict(result.data)
    result.data.update({
        "media_type": "tv" if media_type == "tv" else "auto",
        "check_definition": "aired_normal_episodes_missing_from_enabled_media_servers",
    })
    if media_type == "auto" and result.status == "not_found":
        result.ok = False
        result.status = "cannot_determine"
        result.summary = "未找到可审计剧集，无法可靠判断目标是否有更新"
        result.suggestions = [
            "如果目标是电视剧，请补充“剧集”或提供 TMDB ID。",
            "如果目标是电影，可改用电影核对：检查是否入库，并继续搜索需人工判断的资源候选。",
        ]
    return result
