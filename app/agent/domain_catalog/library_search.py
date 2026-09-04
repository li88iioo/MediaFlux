"""媒体服务器聚合搜索及其短时只读缓存。"""

from __future__ import annotations

import threading
import time
import unicodedata
from typing import Any

from app.agent.episode_audit import reset_episode_audit_cache_for_tests
from app.agent.models import Evidence, ToolResult
from app.agent.provider_actions import reset_provider_gateway_for_tests
from app.agent.workspace_actions import (
    _contains_sensitive_text,
    _safe_status,
    _safe_title,
    _safe_year,
)
from app.clients.base import normalize_playback_progress
from app.services import search_media_servers

from .shared import _now

_SEARCH_CACHE_TTL_SECONDS = 15
_SEARCH_CACHE_MAX_ENTRIES = 128
_search_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
_search_cache_lock = threading.Lock()


def _search_sources(query: str, limit: int) -> list[dict[str, Any]]:
    cache_key = (query.casefold(), limit)
    now = time.monotonic()
    with _search_cache_lock:
        cached = _search_cache.get(cache_key)
        if cached and now - cached[0] < _SEARCH_CACHE_TTL_SECONDS:
            return cached[1]
    sources = search_media_servers(query, limit=limit)
    with _search_cache_lock:
        expired = [
            key
            for key, value in _search_cache.items()
            if now - value[0] >= _SEARCH_CACHE_TTL_SECONDS
        ]
        for key in expired:
            _search_cache.pop(key, None)
        while len(_search_cache) >= _SEARCH_CACHE_MAX_ENTRIES:
            oldest = min(_search_cache, key=lambda key: _search_cache[key][0])
            _search_cache.pop(oldest, None)
        _search_cache[cache_key] = (now, sources)
    return sources


def reset_agent_tool_caches_for_tests() -> None:
    with _search_cache_lock:
        _search_cache.clear()
    reset_episode_audit_cache_for_tests()
    reset_provider_gateway_for_tests()


def _safe_optional_index(value: Any, *, allow_zero: bool) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    minimum = 0 if allow_zero else 1
    return value if minimum <= value <= 10_000 else None


def _safe_runtime_minutes(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        runtime = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return runtime if 0 <= runtime <= 525_600 else 0


def _safe_overview(value: Any) -> str:
    overview = unicodedata.normalize("NFKC", str(value or "")).strip()
    overview = " ".join(overview.split())
    if not overview or _contains_sensitive_text(overview):
        return ""
    return overview[:500]


def search_library(arguments: dict[str, Any]) -> ToolResult:
    query = arguments["query"]
    sources = _search_sources(query, arguments["limit"])
    serialized_sources: list[dict[str, Any]] = []
    total = 0
    unavailable = 0
    for source in sources:
        items: list[dict[str, Any]] = []
        source_unavailable = bool(source.get("error"))
        if source_unavailable:
            unavailable += 1
        else:
            server_type = _safe_status(source.get("server_type"), {"jellyfin", "emby"})
            if server_type == "unknown":
                server_type = "media_server"
            for item in source.get("items", []):
                title = _safe_title(item.name or item.display_name, "媒体条目")
                display_name = _safe_title(item.display_name, title)
                series_name = (
                    _safe_title(item.series_name, "") if item.series_name else ""
                )
                items.append(
                    {
                        "title": title,
                        "display_name": display_name,
                        "media_type": _safe_status(
                            item.type,
                            {"movie", "series", "episode", "season", "video"},
                        ),
                        "year": _safe_year(item.year),
                        "series_name": series_name,
                        "season": _safe_optional_index(
                            item.season_number, allow_zero=True
                        ),
                        "episode": _safe_optional_index(
                            item.episode_number, allow_zero=False
                        ),
                        "runtime_minutes": _safe_runtime_minutes(item.runtime),
                        "playback_progress_percent": normalize_playback_progress(
                            item.progress
                        ),
                        "overview": _safe_overview(item.overview),
                    }
                )
        total += len(items)
        server_type = _safe_status(source.get("server_type"), {"jellyfin", "emby"})
        if server_type == "unknown":
            server_type = "media_server"
        source_match = (
            "unknown" if source_unavailable else ("found" if items else "not_found")
        )
        serialized_sources.append(
            {
                "server_type": server_type,
                "server_name": _safe_title(source.get("server_name"), "媒体服务器"),
                "status": "unavailable" if source_unavailable else "ready",
                "match_status": source_match,
                "returned": len(items),
                "items": items,
            }
        )
    if not sources:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="没有可搜索的媒体服务器",
            data={
                "query": query,
                "total": 0,
                "match_status": "not_configured",
                "sources": [],
            },
            evidence=[
                Evidence(
                    "media_servers",
                    "检查已启用且凭据完整的 Jellyfin / Emby 配置。",
                    _now(),
                )
            ],
            suggestions=["请先在设置中完整配置并启用 Jellyfin 或 Emby。"],
        )
    available = len(sources) - unavailable
    if unavailable == len(sources):
        status = "unavailable"
        match_status = "indeterminate"
        summary = "媒体服务器暂时不可用，无法判断是否存在匹配内容"
        suggestions = ["请检查媒体服务器连通性后重试。"]
    elif total:
        status = "partial" if unavailable else "success"
        match_status = "found"
        summary = f"在媒体库中找到 {total} 项结果"
        suggestions = (
            [f"有 {unavailable} 个媒体服务器暂时不可用。"] if unavailable else []
        )
    else:
        status = "partial" if unavailable else "empty"
        match_status = "indeterminate" if unavailable else "not_found"
        summary = (
            f"已查询 {available} 个媒体服务器，另有 {unavailable} 个暂时不可用；可用来源未找到匹配内容"
            if unavailable
            else "媒体库中没有找到匹配内容"
        )
        suggestions = (["请检查不可用的媒体服务器后重试。"] if unavailable else []) + [
            "可尝试中文名、原名或去掉季集编号后重新搜索。",
            f"搜索《{query}》的资源。",
            f"在网上找《{query}》。",
        ]
    return ToolResult(
        ok=available > 0,
        status=status,
        summary=summary,
        data={
            "query": query,
            "total": total,
            "match_status": match_status,
            "sources": serialized_sources,
        },
        evidence=[
            Evidence(
                "media_servers", f"查询 {len(sources)} 个已配置媒体服务器。", _now()
            )
        ],
        suggestions=suggestions,
    )
