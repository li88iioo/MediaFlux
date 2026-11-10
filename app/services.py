"""服务聚合层：媒体服务器看板、库存统计、客户端工厂。"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import combinations, groupby
from typing import Any
from urllib.parse import urlsplit

from app import database as db
from app.clients.base import (
    DashboardData,
    MediaIdentityBatch,
    MediaItem,
    MediaSearchResult,
    MediaServerClient,
    SeriesSearchResult,
    close_media_server_client,
)
from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.config import get, get_bool, get_int
from app.discovery.models import MediaCard, ProviderError
from app.discovery.registry import COLLECTION_CATEGORIES, list_filter_definitions, validate_filters
from app.logger import get_logger, log_throttled
from app.modules.media_server_profiles import list_configured_profiles
from app.sensitive_data import contains_sensitive_credential
from app.modules.media_library import visible_text

logger = get_logger(__name__)


def _empty_automation_summary(*, error: str = "") -> dict:
    return {
        "downloads_active": 0,
        "downloads_review": 0,
        "rss_subscriptions": 0,
        "media_subscriptions": 0,
        "subscriptions_total": 0,
        "rss_pending": 0,
        "rss_failed": 0,
        "organize_issues": 0,
        "strm_failures": 0,
        "strm_last_status": "",
        "strm_last_at": "",
        "issues": 0,
        "issue_source": "none",
        "healthy": True,
        "error": error,
    }


def build_automation_summary() -> dict:
    """聚合下载、RSS、整理和 STRM 的本地状态。"""
    try:
        summary = {**_empty_automation_summary(), **db.get_dashboard_automation_summary()}
    except Exception as exc:
        log_throttled(
            logger, logging.WARNING, f"dashboard-automation:{type(exc).__name__}",
            "读取看板自动化状态失败 type=%s", type(exc).__name__,
        )
        return _empty_automation_summary(error="自动化状态暂不可用")
    issue_fields = (
        ("downloads", "downloads_review"),
        ("rss", "rss_failed"),
        ("organize", "organize_issues"),
        ("strm", "strm_failures"),
    )
    issue_counts = {
        source: int(summary.get(field, 0) or 0)
        for source, field in issue_fields
    }
    active_sources = [source for source, count in issue_counts.items() if count > 0]
    summary["issues"] = sum(issue_counts.values())
    summary["issue_source"] = (
        active_sources[0]
        if len(active_sources) == 1
        else "mixed" if active_sources else "none"
    )
    summary["healthy"] = summary["issues"] == 0
    return summary


@dataclass
class _MediaSnapshot:
    key: tuple[str, ...]
    expires_at: float
    retain_until: float
    data: list
    forced: bool = False


class _MediaSnapshotCache:
    """一个有界快照槽；页面只读，XHR 刷新合并，失效后不接收旧请求回填。"""

    def __init__(self):
        self._entry: _MediaSnapshot | None = None
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._generation = 0

    def peek(self, key: tuple[str, ...], *, stale: bool = False) -> _MediaSnapshot | None:
        with self._lock:
            entry = self._entry
            now = time.monotonic()
            if entry and entry.key == key and now < entry.retain_until and (stale or now < entry.expires_at):
                return entry
        return None

    def invalidate(self, *, keep_stale: bool = False) -> None:
        with self._lock:
            self._generation += 1
            self._entry = (
                replace(self._entry, expires_at=0)
                if keep_stale and self._entry else None
            )

    def load(
        self, key: tuple[str, ...], loader: Callable[[], list],
        merge: Callable[[list | None, list], tuple[list, bool, bool]],
        *, force: bool = False,
    ) -> list:
        with self._lock:
            observed = self._entry
            generation = self._generation
        with self._refresh_lock:
            cached = self.peek(key)
            # 排队中的强刷也复用同一轮刚完成的读取，不串行重复访问上游。
            if cached and (not force or (cached is not observed and cached.forced)):
                return cached.data
            previous = self.peek(key, stale=True)
            try:
                data = loader()
            except Exception as exc:
                if previous is None:
                    raise
                logger.warning("媒体概览刷新失败，保留限龄快照 type=%s", type(exc).__name__)
                data = None
            # 读取期间旧快照可能过期，失败不能无限续期旧内容。
            previous = self.peek(key, stale=True)
            data, retained, failed = merge(data, previous.data if previous else [])
            now = time.monotonic()
            ttl = max(1, min(get_int("DASHBOARD_CACHE_TTL_SECONDS", 30), 300))
            entry = _MediaSnapshot(
                key=key,
                expires_at=now + (min(ttl, 5) if failed else ttl),
                retain_until=previous.retain_until if retained and previous else now + 300,
                data=data,
                forced=force,
            )
            with self._lock:
                if generation != self._generation:
                    raise RuntimeError("媒体配置或内容已更新，请重试")
                self._entry = entry
            return entry.data


_dashboard_cache = _MediaSnapshotCache()
_recent_media_cache = _MediaSnapshotCache()


def media_server_config_key() -> tuple[str, ...]:
    """配置变化自动生成新缓存键，避免返回旧服务器数据。"""
    return (
        get("EMBY_ENABLED"),
        get("EMBY_URL"),
        get("EMBY_TOKEN"),
        get("JELLYFIN_ENABLED"),
        get("JELLYFIN_URL"),
        get("JELLYFIN_API_KEY"),
        get("EMBY_USER_ID"),
        get("JELLYFIN_USER_ID"),
    )


def clear_dashboard_cache(*, keep_stale: bool = False) -> None:
    """同时失效两页快照；入库可保留旧内容，配置变更必须清空。"""
    # 先失效被复用的内容，再失效依赖它的看板，避免并发刷新重新注入旧内容。
    _recent_media_cache.invalidate(keep_stale=keep_stale)
    _dashboard_cache.invalidate(keep_stale=keep_stale)


def _decorate_dashboard(
    board: DashboardData,
    *,
    server_type: str,
    web_url: str,
) -> DashboardData:
    """附加稳定的服务器标识，供看板切换和外部跳转使用。"""
    board.server_type = server_type
    board.web_url = web_url
    return board


def _load_dashboard(
    client: MediaServerClient,
    *,
    server_type: str,
    web_url: str,
    reuse_recent: bool = True,
) -> DashboardData:
    """读取一次看板并确定性释放该请求独占的连接池。"""
    try:
        recent = _recent_media_cache.peek((*media_server_config_key(), "72")) if reuse_recent else None
        source = next(
            (item for item in recent.data if item["server_type"] == server_type), None,
        ) if recent else None
        # 只复用完整、新鲜且同配置的作品列表；冷看板仍只读取原有的 8 项。
        options = (
            {"recent_added": source["items"][:8]}
            if source and not source.get("error") and not source.get("stale") else {}
        )
        return _decorate_dashboard(
            client.get_dashboard(**options),
            server_type=server_type,
            web_url=web_url,
        )
    finally:
        close_media_server_client(client)


def _dashboard_jobs(*, reuse_recent: bool = True) -> list[Callable[[], DashboardData]]:
    jobs: list[Callable[[], DashboardData]] = []
    if get_bool("EMBY_ENABLED"):
        url, token = get("EMBY_URL"), get("EMBY_TOKEN")
        if url and token:
            jobs.append(
                lambda url=url, token=token: _load_dashboard(
                    EmbyClient(url, token),
                    server_type="emby",
                    web_url=url,
                    reuse_recent=reuse_recent,
                )
            )
        else:
            jobs.append(
                lambda url=url: DashboardData(
                    server_name="Emby / Jellyfin 10.x",
                    server_type="emby",
                    web_url=url,
                    error="未配置 URL/Token 或 API Key",
                )
            )

    if get_bool("JELLYFIN_ENABLED"):
        url, key = get("JELLYFIN_URL"), get("JELLYFIN_API_KEY")
        if url and key:
            jobs.append(
                lambda url=url, key=key: _load_dashboard(
                    JellyfinClient(url, key),
                    server_type="jellyfin",
                    web_url=url,
                    reuse_recent=reuse_recent,
                )
            )
        else:
            jobs.append(
                lambda url=url: DashboardData(
                    server_name="Jellyfin",
                    server_type="jellyfin",
                    web_url=url,
                    error="未配置 URL/API Key",
                )
            )
    return jobs


def _fetch_dashboards(*, reuse_recent: bool = True) -> list[DashboardData]:
    jobs = _dashboard_jobs(reuse_recent=reuse_recent)
    if len(jobs) <= 1:
        return [job() for job in jobs]
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="media-dashboard") as pool:
        futures = [pool.submit(job) for job in jobs]
        return [future.result() for future in futures]


def _merge_dashboards(
    boards: list[DashboardData] | None, previous: list[DashboardData],
) -> tuple[list[DashboardData], bool, bool]:
    """返回结果、是否保留旧值、是否读取失败；成功空值不会被旧数据覆盖。"""
    if boards is None:
        if not previous:
            raise RuntimeError("媒体服务器暂时不可用")
        return [replace(board, online=False, stale=True, refresh_error="读取失败，显示上次数据") for board in previous], True, True
    old_by_type = {board.server_type: board for board in previous if board.updated_at}
    retained = failed = False
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    for index, board in enumerate(boards):
        old = old_by_type.get(board.server_type)
        unavailable = bool(board.error or not board.online)
        failed = failed or unavailable or bool(board.partial_errors)
        if unavailable and old:
            boards[index] = replace(old, online=False, stale=True, refresh_error="读取失败，显示上次数据")
            retained = True
            continue
        board.updated_at = stamp if board.online else ""
        if board.partial_errors and old:
            for part in board.partial_errors:
                fields = ("movie_count", "series_count", "episode_count") if part == "media_counts" else (part,)
                for field in fields:
                    if field in {"libraries", "recent_added", "recent_played", "total_items", "total_plays", "movie_count", "series_count", "episode_count"}:
                        setattr(board, field, getattr(old, field))
            board.stale = True
            board.updated_at = old.updated_at
            board.refresh_error = "部分数据读取失败，保留上次结果"
            retained = True
    return boards, retained, failed


def build_dashboards(*, force: bool = False) -> list[DashboardData]:
    """XHR 读取最新看板，同轮请求共享结果；失败不清空仍可用的快照。"""
    return _dashboard_cache.load(
        media_server_config_key(),
        (lambda: _fetch_dashboards(reuse_recent=False)) if force else _fetch_dashboards,
        _merge_dashboards, force=force,
    )


def get_cached_dashboards_or_stubs() -> tuple[list[DashboardData], bool]:
    """页面只读取快照，不访问上游；第二值表示有快照而非仍然新鲜。"""
    cached = _dashboard_cache.peek(media_server_config_key(), stale=True)
    if cached:
        expired = cached.expires_at <= time.monotonic()
        return [replace(board, stale=board.stale or expired) for board in cached.data], True

    stubs: list[DashboardData] = []
    if get_bool("JELLYFIN_ENABLED"):
        stubs.append(DashboardData(
            server_name="Jellyfin",
            server_type="jellyfin",
            web_url=get("JELLYFIN_URL", ""),
            online=False,
            server_product="Jellyfin",
        ))
    if get_bool("EMBY_ENABLED"):
        stubs.append(DashboardData(
            server_name="Emby",
            server_type="emby",
            web_url=get("EMBY_URL", ""),
            online=False,
            server_product="Emby",
        ))
    return stubs, False


def get_media_server_urls() -> dict[str, str]:
    """返回各媒体服务器跳转地址（看板点击用）。"""
    urls: dict[str, str] = {}
    if get_bool("EMBY_ENABLED") and get("EMBY_URL"):
        urls["Emby"] = get("EMBY_URL")
    if get_bool("JELLYFIN_ENABLED") and get("JELLYFIN_URL"):
        urls["Jellyfin"] = get("JELLYFIN_URL")
    return urls


def _configured_media_sources() -> list[tuple[str, str, str, MediaServerClient]]:
    """构造已完整配置的媒体服务器客户端，不返回凭证。"""
    sources: list[tuple[str, str, str, MediaServerClient]] = []
    try:
        if get_bool("EMBY_ENABLED"):
            url, token = get("EMBY_URL"), get("EMBY_TOKEN")
            if url and token:
                sources.append(("emby", "Emby", url, EmbyClient(url, token)))
        if get_bool("JELLYFIN_ENABLED"):
            url, token = get("JELLYFIN_URL"), get("JELLYFIN_API_KEY")
            if url and token:
                sources.append(("jellyfin", "Jellyfin", url, JellyfinClient(url, token)))
        return sources
    except Exception:
        for _server_type, _server_name, _web_url, client in sources:
            close_media_server_client(client)
        raise


def _media_source_payload(
    server_type: str,
    server_name: str,
    web_url: str,
    loader: Callable[[], list],
) -> dict:
    try:
        items = loader()
        return {
            "server_type": server_type,
            "server_name": server_name,
            "web_url": web_url,
            "items": items,
            "error": "",
        }
    except Exception as exc:
        logger.warning(
            "媒体服务器内容读取失败 server=%s type=%s",
            server_type,
            type(exc).__name__,
        )
        return {
            "server_type": server_type,
            "server_name": server_name,
            "web_url": web_url,
            "items": [],
            "error": "媒体服务器暂时不可用",
        }


def _run_media_source_jobs(jobs: list[Callable[[], dict]]) -> list[dict]:
    if len(jobs) <= 1:
        return [job() for job in jobs]
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="media-content") as pool:
        futures = [pool.submit(job) for job in jobs]
        return [future.result() for future in futures]


def _run_owned_media_source_job(
    client: MediaServerClient,
    loader: Callable[[], dict],
) -> dict:
    """执行一次媒体源读取，并在成功、降级或异常后统一释放客户端。"""
    try:
        return loader()
    finally:
        close_media_server_client(client)


def get_library_counts(server: str = "all", library_id: str = "") -> dict[str, Any]:
    """按字段保留未知；多源记录数求和，不去重。user_selection 仅说明请求用户选择。"""
    if server not in ("all", "jellyfin", "emby"):
        raise ValueError("server 必须是 all / jellyfin / emby")
    if (not isinstance(library_id, str) or len(library_id) > 256
            or any(ord(char) < 32 for char in library_id) or (library_id and not library_id.strip())):
        raise ValueError("媒体库标识无效")
    library_id = library_id.strip()
    if library_id and server == "all":
        raise ValueError("指定 library_id 时必须明确 jellyfin 或 emby")
    fields = ("total_items", "movie_count", "series_count", "episode_count")

    def read(profile):
        row = {"server_type": profile.server_type, "server_name": profile.label,
               "user_selection": "configured" if profile.user_id else "server_default",
               "status": "not_configured", "counts": dict.fromkeys(fields)}
        if not profile.enabled or not profile.configured:
            return row
        try:
            with profile.create_client() as client:
                if library_id and not any(str(folder["id"]) == library_id for folder in client.list_virtual_folders()):
                    return {**row, "status": "not_found"}
                raw = client.get_library_media_counts(library_id) if library_id else client.get_media_counts()
                counts = {key: max(0, value) if (value := raw.get(key)) is not None else None for key in fields}
                return {**row, "status": "partial" if None in counts.values() else "success", "counts": counts}
        except Exception as exc:
            logger.warning("媒体统计读取失败 server=%s type=%s", profile.server_type, type(exc).__name__)
            return {**row, "status": "unavailable"}

    profiles = [profile for profile in list_configured_profiles()
                if (profile.enabled if server == "all" else profile.server_type == server)]
    sources = _run_media_source_jobs([lambda profile=profile: read(profile) for profile in profiles])
    available = [row for row in sources if row["status"] in {"success", "partial"}]
    status = "not_configured" if not sources else "unavailable"
    if available:
        status = "success" if all(row["status"] == "success" for row in sources) else "partial"
    elif len({row["status"] for row in sources}) == 1:
        status = sources[0]["status"]
    counts, known = {}, {}
    for key in fields:
        values = [row["counts"][key] for row in sources if row["counts"][key] is not None]
        known[key] = sum(values) if values else None
        counts[key] = known[key] if len(values) == len(sources) else None
    return {"ok": bool(available), "status": status, "data": {
        "server": server, "library_id": library_id, "scope": "library" if library_id else "all_libraries",
        "count_scope": "server_records_sum_not_deduplicated",
        "counts": counts,
        "known_counts": known, "sources": sources,
    }}


def _fetch_recent_media(*, limit: int = 72) -> list[dict]:
    """并发聚合所有已配置媒体服务器的最近入库内容。"""
    normalized_limit = max(1, min((limit or 72), 200))
    jobs: list[Callable[[], dict]] = []
    for server_type, server_name, web_url, client in _configured_media_sources():
        jobs.append(
            lambda server_type=server_type, server_name=server_name, web_url=web_url, client=client: _run_owned_media_source_job(
                client,
                lambda: _media_source_payload(
                    server_type,
                    server_name,
                    web_url,
                    lambda: client.recent_media(normalized_limit),
                ),
            )
        )
    results = _run_media_source_jobs(jobs)
    for source in results:
        source["items"] = sorted(
            source["items"],
            key=lambda item: str(getattr(item, "date_added", "") or ""),
            reverse=True,
        )
    return results


def _merge_recent_media(sources: list[dict] | None, previous: list[dict]) -> tuple[list[dict], bool, bool]:
    if sources is None:
        if not previous:
            raise RuntimeError("媒体服务器暂时不可用")
        return [{**source, "stale": True, "error": "读取失败，显示上次数据"} for source in previous], True, True
    old_by_type = {source["server_type"]: source for source in previous if source.get("updated_at")}
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    retained = failed = False
    results = []
    for source in sources:
        old = old_by_type.get(source["server_type"])
        error = source.get("error", "")
        failed = failed or bool(error)
        if error and old:
            results.append({**old, "error": error, "stale": True})
            retained = True
        else:
            results.append({**source, "updated_at": "" if error else stamp, "stale": False})
    return results, retained, failed


def build_recent_media(*, limit: int = 72) -> list[dict]:
    normalized_limit = max(1, min((limit or 72), 200))
    return _recent_media_cache.load(
        (*media_server_config_key(), str(normalized_limit)),
        lambda: _fetch_recent_media(limit=normalized_limit), _merge_recent_media,
    )


def get_cached_recent_media_or_stubs(*, limit: int = 72) -> tuple[list[dict], bool]:
    """与看板使用相同的限龄快照，未加载时只返回服务器标识。"""
    normalized_limit = max(1, min((limit or 72), 200))
    cached = _recent_media_cache.peek((*media_server_config_key(), str(normalized_limit)), stale=True)
    if cached:
        expired = cached.expires_at <= time.monotonic()
        return [{**source, "stale": source.get("stale", False) or expired} for source in cached.data], True
    return [
        {"server_type": server_type, "server_name": name, "web_url": get(url_key),
         "items": [], "error": "", "updated_at": "", "stale": False}
        for server_type, name, url_key, token_key in (
            ("emby", "Emby", "EMBY_URL", "EMBY_TOKEN"),
            ("jellyfin", "Jellyfin", "JELLYFIN_URL", "JELLYFIN_API_KEY"),
        )
        if get_bool(f"{server_type.upper()}_ENABLED") and get(url_key) and get(token_key)
    ], False


def _media_search_source_payload(
    server_type: str, server_name: str, web_url: str,
    client: MediaServerClient, query: str, options: dict[str, Any],
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    result = MediaSearchResult(**options)
    error = ""
    try:
        result = client.search_media(query, **options, cancelled=cancelled)
    except Exception as exc:
        logger.warning("媒体服务器搜索失败 server=%s type=%s", server_type, type(exc).__name__)
        error = "媒体服务器暂时不可用"
    return {
        "server_type": server_type, "server_name": server_name, "web_url": web_url,
        "items": result.items, **result.metadata(), "error": error,
    }


def search_media_servers(
    query: str, *, limit: int = 8, offset: int = 0,
    search_by: str = "title", media_type: str = "all", count_only: bool = False, server: str = "all",
    cancelled: Callable[[], bool] | None = None,
) -> list[dict]:
    """并发搜索，offset/limit 按服务器应用；总数是记录数，不跨服务器去重。"""
    normalized_limit = max(1, min((limit or 8), 50))
    options = {
        "limit": normalized_limit, "offset": offset, "search_by": search_by,
        "media_type": media_type, "count_only": count_only,
    }
    jobs: list[Callable[[], dict]] = []
    for server_type, server_name, web_url, client in _configured_media_sources():
        if server != "all" and server_type != server:
            close_media_server_client(client)
            continue
        jobs.append(
            lambda server_type=server_type, server_name=server_name, web_url=web_url, client=client: _run_owned_media_source_job(
                client,
                lambda: _media_search_source_payload(
                    server_type, server_name, web_url, client, query, options, cancelled,
                ),
            )
        )
    return _run_media_source_jobs(jobs)


_SEARCH_CACHE_TTL_SECONDS = 15
_SEARCH_CACHE_MAX_ENTRIES = 128
_search_cache: dict[tuple[tuple[str, ...], str, int, int, str, str, bool, str], tuple[float, list[dict[str, Any]]]] = {}
_search_cache_lock = threading.Lock()


def _search_sources(
    query: str, limit: int, *, offset: int = 0, search_by: str = "title",
    media_type: str = "all", count_only: bool = False, server: str = "all",
) -> list[dict[str, Any]]:
    cache_key = (media_server_config_key(), query.casefold(), limit, offset, search_by, media_type, count_only, server)
    now = time.monotonic()
    with _search_cache_lock:
        cached = _search_cache.get(cache_key)
        if cached and now - cached[0] < _SEARCH_CACHE_TTL_SECONDS:
            return cached[1]
    sources = search_media_servers(
        query, limit=limit, offset=offset, search_by=search_by,
        media_type=media_type, count_only=count_only, server=server,
    )
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


def clear_library_search_cache() -> None:
    """清空共用媒体库搜索缓存，不重置 Agent 或其他业务状态。"""
    with _search_cache_lock:
        _search_cache.clear()


def _library_search_url(value: object) -> str:
    """只公开无凭据的 HTTP(S) 链接或现有媒体图片代理地址。"""
    raw = str(value or "").strip()
    if not raw or any(char.isspace() for char in raw) or contains_sensitive_credential(raw):
        return ""
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme and not parsed.netloc:
            return raw if parsed.path.startswith("/media-image/") else ""
        if (parsed.scheme.casefold() in {"http", "https"} and parsed.hostname
                and parsed.username is None and parsed.password is None
                and 1 <= (parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)) <= 65535):
            return raw
    except ValueError:
        pass
    return ""


def _library_search_item(item: MediaItem) -> dict[str, Any]:
    # 显式列出业务字段，避免 dataclass 后续新增私有路径或凭据时自动进入 SDK。
    return {
        **{key: getattr(item, key) for key in (
            "id", "name", "type", "year", "runtime", "date_added", "overview", "tmdb_id",
            "series_name", "series_id", "season_number", "episode_number", "last_played", "progress",
        )},
        "display_name": item.display_name,
        "genres": list(item.genres or ()),
        **{key: _library_search_url(getattr(item, key)) for key in (
            "primary_image", "web_url", "series_web_url",
        )},
        "open_url": _library_search_url(item.web_url or item.series_web_url),
    }


def search_library(
    query: str, *, limit: int = 8, offset: int = 0,
    search_by: str = "title", media_type: str = "all", count_only: bool = False, server: str = "all",
) -> dict[str, Any]:
    """聚合各源搜索页；total_known 仅表示总数可靠，分页进度由 has_more 等字段表达。"""
    if not isinstance(query, str):
        raise ValueError("query 必须是字符串")
    query = unicodedata.normalize("NFKC", query).strip()
    if not query or len(query) > 120 or any(unicodedata.category(char).startswith("C") for char in query):
        raise ValueError("搜索关键词必须为 1 到 120 个可见字符")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit 必须是 1 到 50 的整数")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset 必须是非负整数")
    if search_by not in ("title", "actor"):
        raise ValueError("search_by 必须是 title / actor")
    if media_type not in ("all", "movie", "series", "episode"):
        raise ValueError("media_type 必须是 all / movie / series / episode")
    if type(count_only) is not bool:
        raise ValueError("count_only 必须是布尔值")
    if server not in {"all", "jellyfin", "emby"}:
        raise ValueError("server 必须是 all/jellyfin/emby")
    sources = _search_sources(query, limit, offset=offset, search_by=search_by,
                              media_type=media_type, count_only=count_only, **({"server": server} if server != "all" else {}))
    serialized_sources = []
    known_total = known_sources = returned = unavailable = 0
    for source in sources:
        source_unavailable = bool(source.get("error"))
        unavailable += int(source_unavailable)
        items = [] if source_unavailable else [_library_search_item(item) for item in source.get("items", [])]
        returned += len(items)
        source_total = source.get("total")
        if source_unavailable or type(source_total) is not int or source_total < 0:
            source_total = None
        else:
            known_total += source_total
            known_sources += 1
        source_match = "found" if items or (source_total or 0) > 0 else "not_found" if source_total == 0 else "unknown"
        serialized_sources.append({
            "server_type": source.get("server_type"),
            "server_name": source.get("server_name"),
            "web_url": _library_search_url(source.get("web_url")),
            "status": "unavailable" if source_unavailable else "ready",
            "match_status": source_match,
            "total": source_total,
            "returned": len(items),
            "has_more": source.get("has_more") if not source_unavailable else None,
            "offset": source.get("offset", offset),
            "limit": source.get("limit", limit),
            "next_offset": source.get("next_offset") if not source_unavailable else None,
            "total_source": source.get("total_source", "unknown") if not source_unavailable else "unknown",
            "person_match": source.get("person_match", ""),
            "included_item_types": deepcopy(source.get("included_item_types", [])),
            "items": items,
            "total_known": source_total is not None,
        })
    available = len(sources) - unavailable
    total = known_total if sources and known_sources == len(sources) else None
    has_more = (
        True if any(source["has_more"] is True for source in serialized_sources)
        else False if sources and all(source["has_more"] is False for source in serialized_sources)
        else None
    )
    next_offsets = [source["next_offset"] for source in serialized_sources if type(source["next_offset"]) is int]
    if not sources:
        status, match_status = "not_configured", "not_configured"
    elif not available:
        status, match_status = "unavailable", "indeterminate"
    elif returned or known_total:
        status, match_status = "success" if total is not None else "partial", "found"
    else:
        status, match_status = ("empty", "not_found") if total == 0 else ("partial", "indeterminate")
    episode_titles = search_by == "title" and media_type == "episode"
    return {
        "ok": available > 0,
        "status": status,
        "data": {
            "query": query, "offset": offset, "limit": limit, "count_only": count_only,
            "search_by": search_by, "media_type": media_type,
            "match_basis": "actor_metadata" if search_by == "actor" else "title_search",
            "search_scope": "episode_title" if episode_titles else "actor_metadata" if search_by == "actor" else "item_title",
            "count_scope": "server_records_sum_not_deduplicated", "pagination_scope": "per_server",
            "total": total, "known_total": known_total, "returned": returned,
            "has_more": has_more, "next_offset": min(next_offsets, default=None),
            "match_status": match_status, "sources": serialized_sources,
            "total_known": total is not None,
            "available_sources": available, "unavailable_sources": unavailable,
        },
    }


def _media_identity_source_payload(
    server_type: str,
    server_name: str,
    client: MediaServerClient,
    tmdb_id: str,
    media_type: str,
    identity_batch: MediaIdentityBatch | None = None,
) -> dict:
    """读取单个媒体源的精确 TMDB 身份，不依赖标题模糊搜索。"""
    try:
        return {
            "server_type": server_type,
            "server_name": server_name,
            "status": "ready",
            "present": client.has_tmdb_media(tmdb_id, media_type, identity_batch=identity_batch),
            "error": "",
        }
    except Exception as exc:
        logger.warning(
            "媒体服务器身份审计失败 server=%s type=%s",
            server_type,
            type(exc).__name__,
        )
        return {
            "server_type": server_type,
            "server_name": server_name,
            "status": "unavailable",
            "present": False,
            "error": "媒体服务器暂时不可用",
        }


def _normalize_series_identity_title(value: object) -> str:
    """用于本地候选消歧的严格标题口径；仅保留 Unicode 字母和数字。"""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _media_identity_inventory_source_payload(
    server_type: str, server_name: str, client: MediaServerClient,
    media_types: tuple[str, ...], deadline_at: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict:
    """一次读取并索引一个媒体源；索引只属于当前请求，不持有客户端。"""
    inventories = {}
    error = ""
    for media_type in dict.fromkeys(media_types):
        if (cancelled and cancelled()) or (deadline_at is not None and time.monotonic() >= deadline_at):
            break
        try:
            options: dict[str, Any] = {"max_items": 5_000, "page_size": 200}
            if deadline_at is not None:
                options["deadline_at"] = deadline_at
            if cancelled is not None:
                options["cancelled"] = cancelled
            inventory = client.list_media_identity_inventory(media_type, **options)
            inventories[media_type] = {
                "total": inventory.total, "truncated": inventory.truncated, "unmapped": inventory.unmapped,
                "exact_ids": frozenset(item.tmdb_id for item in inventory.candidates if item.tmdb_id),
                "unmapped_titles": frozenset(
                    (_normalize_series_identity_title(item.name), (item.year or "").strip())
                    for item in inventory.candidates if not item.tmdb_id and _normalize_series_identity_title(item.name)),
            }
        except Exception as exc:  # noqa: BLE001 - 按类型隔离故障，保留同源已完成的库存
            logger.warning("媒体服务器批量身份审计失败 server=%s media_type=%s type=%s",
                           server_type, media_type, type(exc).__name__)
            error = "媒体服务器暂时不可用"
    status = ("partial" if inventories else "unavailable") if error else "ready"
    return {"server_type": server_type, "server_name": server_name, "status": status,
            "inventories": inventories, "error": error}


def _complete_series_matches(search, query: str) -> list:
    """只在候选集合完整且身份唯一时选择；绝不从截断结果中猜测。"""
    candidates = list(search.candidates or [])
    if not candidates or search.truncated or int(search.total or 0) > len(candidates):
        return []
    target = _normalize_series_identity_title(query)
    exact = [
        item for item in candidates
        if target and _normalize_series_identity_title(item.name) == target
    ]
    selected = exact or (candidates if int(search.total or 0) == 1 else [])
    if len(selected) <= 1:
        return selected
    mapped_ids = {str(item.tmdb_id or "") for item in selected}
    years = {str(item.year or "") for item in selected}
    # 同一服务器中同 TMDB ID、同年份的多媒体库副本可以安全合并。
    if len(mapped_ids) == 1 and "" not in mapped_ids and len(years) == 1:
        return selected
    return []


def _public_series_candidates(*searches) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for search in searches:
        if search is None:
            continue
        for item in search.candidates or []:
            key = (str(item.name or ""), str(item.year or ""), str(item.tmdb_id or ""))
            if key in seen:
                continue
            seen.add(key)
            result.append({"name": key[0], "year": key[1], "tmdb_id": key[2]})
    return result[:20]


def _normalize_library_scope(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _resolve_series_library_scope(
    client: MediaServerClient,
    library_name: str,
) -> tuple[str, str, str]:
    """将用户可见媒体库名称解析为内部 ParentId；绝不向调用方泄露内部 ID。"""
    requested = (library_name or "").strip()
    if not requested:
        return "", "", ""
    target = _normalize_library_scope(requested)
    folders = client.list_virtual_folders()
    matches = [
        folder for folder in folders
        if _normalize_library_scope(folder.get("name")) == target
    ]
    if not matches:
        matches = [
            folder for folder in folders
            if target
            and (
                target in _normalize_library_scope(folder.get("name"))
                or _normalize_library_scope(folder.get("name")) in target
            )
        ]
    if not matches:
        return "library_not_found", requested, ""
    if len(matches) != 1:
        return "library_ambiguous", requested, ""
    selected = matches[0]
    return "", str(selected.get("name") or requested).strip(), str(selected.get("id") or "").strip()


def _series_inventory_source_result(
    *,
    server_type: str,
    server_name: str,
    client: MediaServerClient,
    selected_items: list,
    candidates: list[dict],
    status: str,
    mapping_status: str,
    max_episodes: int,
    include_specials: bool,
    library_name: str = "",
    search_truncated: bool = False,
) -> dict:
    episode_union: set[tuple[int, int]] = set()
    local_total = 0
    truncated = search_truncated
    ignored_specials = 0
    ignored_unknown = 0
    for item in selected_items:
        inventory = client.list_series_episode_inventory(
            item.id, max_episodes=max_episodes, page_size=200,
            include_specials=include_specials,
        )
        episode_union.update(inventory.episodes)
        local_total += (inventory.total or 0)
        truncated = truncated or inventory.truncated
        ignored_specials += (inventory.ignored_specials or 0)
        ignored_unknown += (inventory.ignored_unknown or 0)
    selected = selected_items[0]
    return {
        "server_type": server_type,
        "server_name": server_name,
        "library_name": library_name,
        "status": status,
        "candidates": candidates,
        "selected": {
            "name": selected.name,
            "year": selected.year,
            "tmdb_id": selected.tmdb_id,
        },
        "mapping": {
            "status": mapping_status,
            "tmdb_id": str(selected.tmdb_id or ""),
        },
        "episodes": sorted(episode_union),
        "local_total": local_total,
        "truncated": truncated,
        "ignored_specials": ignored_specials,
        "ignored_unknown": ignored_unknown,
        "error": "",
    }


def _empty_series_source_result(
    *,
    server_type: str,
    server_name: str,
    status: str,
    candidates: list[dict],
    truncated: bool = False,
    library_name: str = "",
) -> dict:
    return {
        "server_type": server_type,
        "server_name": server_name,
        "library_name": library_name,
        "status": status,
        "candidates": candidates,
        "selected": None,
        "mapping": {"status": status, "tmdb_id": ""},
        "episodes": [],
        "local_total": 0,
        "truncated": truncated,
        "ignored_specials": 0,
        "ignored_unknown": 0,
        "error": "",
    }


def _series_source_payload(
    server_type: str,
    server_name: str,
    client: MediaServerClient,
    query: str,
    explicit_tmdb_id: str,
    max_episodes: int,
    include_specials: bool = False,
    library_name: str = "",
    identity_batch: MediaIdentityBatch | None = None,
) -> dict:
    """先读取本地 Series 与集清单；Provider ID 仅用于确认身份。"""
    try:
        scope_status, resolved_library_name, parent_id = _resolve_series_library_scope(
            client, library_name
        )
        if scope_status:
            return _empty_series_source_result(
                server_type=server_type,
                server_name=server_name,
                status=scope_status,
                candidates=[],
                library_name=resolved_library_name,
            )
        provider_search = None
        if explicit_tmdb_id:
            provider_search = client.find_series_candidates_by_tmdb(
                explicit_tmdb_id, limit=20, parent_id=parent_id, identity_batch=identity_batch,
            )
            provider_matches = [
                item for item in provider_search.candidates
                if (item.tmdb_id or "") == explicit_tmdb_id
            ]
            if provider_matches:
                return _series_inventory_source_result(
                    server_type=server_type,
                    server_name=server_name,
                    client=client,
                    selected_items=provider_matches,
                    candidates=_public_series_candidates(provider_search),
                    status="ready",
                    mapping_status="provider_id",
                    max_episodes=max_episodes,
                    include_specials=include_specials,
                    library_name=resolved_library_name,
                    search_truncated=provider_search.truncated,
                )

        # 有稳定身份时先精确查找；未命中且提供了标题，才进行既有标题回退。
        title_search = client.search_series_candidates(
            query, limit=6, **({"parent_id": parent_id} if parent_id else {}),
        ) if query else SeriesSearchResult()
        candidates = _public_series_candidates(title_search, provider_search)
        # 标题回退不能抹去精确身份枚举尚未完成的事实（别名、多副本均可能漏读）。
        search_truncated = title_search.truncated or bool(provider_search and provider_search.truncated)
        selected_items = _complete_series_matches(title_search, query)
        if not selected_items:
            return _empty_series_source_result(
                server_type=server_type,
                server_name=server_name,
                status="not_found" if not title_search.total or not title_search.candidates else "ambiguous",
                candidates=candidates,
                truncated=search_truncated,
                library_name=resolved_library_name,
            )

        mapped_ids = {str(item.tmdb_id or "") for item in selected_items} - {""}
        if explicit_tmdb_id and mapped_ids and mapped_ids != {explicit_tmdb_id}:
            return _empty_series_source_result(
                server_type=server_type,
                server_name=server_name,
                status="conflict",
                candidates=candidates,
                truncated=search_truncated,
                library_name=resolved_library_name,
            )
        status = "ready" if mapped_ids else "unmapped"
        mapping_status = "provider_id" if mapped_ids else "unmapped"
        return _series_inventory_source_result(
            server_type=server_type,
            server_name=server_name,
            client=client,
            selected_items=selected_items,
            candidates=candidates,
            status=status,
            mapping_status=mapping_status,
            max_episodes=max_episodes,
            include_specials=include_specials,
            library_name=resolved_library_name,
            search_truncated=search_truncated,
        )
    except Exception as exc:
        logger.warning(
            "媒体服务器剧集审计读取失败 server=%s type=%s",
            server_type,
            type(exc).__name__,
        )
        result = _empty_series_source_result(
            server_type=server_type,
            server_name=server_name,
            status="unavailable",
            candidates=[],
            library_name=(library_name or "").strip(),
        )
        result["error"] = "媒体服务器暂时不可用"
        return result

def inspect_library_series_sources(
    *,
    max_series: int = 50,
    max_episodes: int = 2000,
    deadline_at: float | None = None,
    after_tmdb_id: str = "",
    scan_all: bool = False,
) -> list[dict]:
    """枚举各媒体服务器的剧集与本地集号；返回前移除内部 ID 和连接信息。"""
    normalized_series = max(1, min((max_series or 50), 100))
    normalized_episodes = max(1, min((max_episodes or 2000), 2000))
    cursor = int(after_tmdb_id or 0) if (after_tmdb_id or "").isdigit() else 0
    catalog_cap = 5000 if scan_all else normalized_series
    results: list[dict] = []

    for server_type, server_name, _web_url, client in _configured_media_sources():
        source = {
            "server_type": server_type,
            "server_name": server_name,
            "status": "ready",
            "series_total": 0,
            "series_enumerated": 0,
            "truncated": False,
            "catalog_truncated": False,
            "batch_remaining": False,
            "next_tmdb_id": "",
            "deadline_exhausted": False,
            "unmapped_count": 0,
            "series": [],
        }
        try:
            if deadline_at is not None and time.monotonic() >= deadline_at:
                source.update(
                    status="incomplete",
                    truncated=True,
                    batch_remaining=scan_all,
                    deadline_exhausted=True,
                )
                results.append(source)
                continue
            try:
                search = client.list_library_series(
                    max_series=catalog_cap,
                    page_size=100,
                    deadline_at=deadline_at,
                )
                source["series_total"] = (search.total or 0)
                source["series_enumerated"] = len(search.candidates)
                source["catalog_truncated"] = search.truncated
                source["truncated"] = search.truncated
                candidates = list(search.candidates)
                if scan_all:
                    mapped = [
                        candidate for candidate in candidates
                        if (candidate.tmdb_id or "").isdigit()
                        and int(candidate.tmdb_id) > cursor
                    ]
                    mapped.sort(key=lambda candidate: int(candidate.tmdb_id))
                    # 巡检游标按 TMDB 媒体组推进，而不是按媒体服务器条目推进。
                    # 同一剧集可能在多个媒体库或多个版本中重复出现；若按候选条目
                    # 切批，重复 ID 恰好跨越边界时会让 next_tmdb_id 等于当前组，
                    # 审计层因此无法处理该组并最终把它误判为停滞后跳过。
                    mapped_tmdb_ids = list(dict.fromkeys(
                        candidate.tmdb_id for candidate in mapped
                    ))
                    selected_tmdb_ids = set(mapped_tmdb_ids[:normalized_series])
                    source["batch_remaining"] = len(mapped_tmdb_ids) > normalized_series
                    selected = [
                        candidate for candidate in mapped
                        if candidate.tmdb_id in selected_tmdb_ids
                    ]
                    if source["batch_remaining"]:
                        source["next_tmdb_id"] = mapped_tmdb_ids[normalized_series]
                    source["unmapped_count"] = sum(
                        not (candidate.tmdb_id or "").isdigit()
                        for candidate in candidates
                    )
                else:
                    selected = candidates
                for index, candidate in enumerate(selected):
                    if deadline_at is not None and time.monotonic() >= deadline_at:
                        source.update(
                            status="incomplete",
                            truncated=True,
                            batch_remaining=True,
                            next_tmdb_id=(candidate.tmdb_id or ""),
                            deadline_exhausted=True,
                        )
                        break
                    if not candidate.tmdb_id:
                        # 一次性全库审计仍要读取本地集号，后续才能以严格的标题+年份
                        # 规则尝试补全映射；后台可续跑批次继续只处理稳定的 TMDB 游标。
                        source["unmapped_count"] += 1
                        if scan_all:
                            continue
                    try:
                        inventory = client.list_series_episode_inventory(
                            candidate.id,
                            max_episodes=normalized_episodes,
                            page_size=200,
                            deadline_at=deadline_at,
                        )
                    except Exception as exc:
                        deadline_hit = isinstance(exc, TimeoutError) or (
                            deadline_at is not None and time.monotonic() >= deadline_at
                        )
                        logger.warning(
                            "媒体服务器剧集清单读取失败 server=%s type=%s",
                            server_type,
                            type(exc).__name__,
                        )
                        source.update(
                            status="incomplete",
                            truncated=True,
                            batch_remaining=(
                                source["batch_remaining"]
                                or index + 1 < len(selected)
                                or bool(candidate.tmdb_id)
                            ),
                            next_tmdb_id=(candidate.tmdb_id or ""),
                            deadline_exhausted=deadline_hit,
                        )
                        break
                    source["series"].append({
                        "name": candidate.name,
                        "year": candidate.year,
                        "tmdb_id": candidate.tmdb_id,
                        "episodes": list(inventory.episodes),
                        "local_total": (inventory.total or 0),
                        "truncated": inventory.truncated,
                        "ignored_specials": (inventory.ignored_specials or 0),
                        "ignored_unknown": (inventory.ignored_unknown or 0),
                    })
                    if inventory.truncated:
                        source["truncated"] = True
            except Exception as exc:
                logger.warning(
                    "媒体服务器全库剧集巡检读取失败 server=%s type=%s",
                    server_type,
                    type(exc).__name__,
                )
                deadline_hit = isinstance(exc, TimeoutError) or (
                    deadline_at is not None and time.monotonic() >= deadline_at
                )
                source.update(
                    status="incomplete" if deadline_hit else "unavailable",
                    truncated=True,
                    catalog_truncated=True,
                    deadline_exhausted=deadline_hit,
                    series=[],
                )
            results.append(source)
        finally:
            close_media_server_client(client)
    return results


def _strict_series_inventory_source_payload(
    server_type: str,
    server_name: str,
    client: MediaServerClient,
    tmdb_id: str,
    max_episodes: int,
    include_specials: bool,
) -> dict:
    """只按 ProviderIds.Tmdb 枚举剧集库存，不回退标题匹配。"""
    try:
        search = client.find_series_candidates_by_tmdb(tmdb_id, limit=20)
        selected = list(search.candidates or [])
        if not selected:
            return _empty_series_source_result(
                server_type=server_type, server_name=server_name, status="not_found",
                candidates=[], truncated=search.truncated,
            )
        return _series_inventory_source_result(
            server_type=server_type, server_name=server_name, client=client,
            selected_items=selected, candidates=_public_series_candidates(search),
            status="ready", mapping_status="provider_id",
            max_episodes=max_episodes, include_specials=include_specials,
            search_truncated=search.truncated,
        )
    except Exception:
        result = _empty_series_source_result(
            server_type=server_type, server_name=server_name, status="unavailable",
            candidates=[], truncated=False,
        )
        result["error"] = "媒体服务器暂时不可用"
        return result


def inspect_series_episode_inventory_by_tmdb(
    tmdb_id: str,
    *,
    max_episodes: int = 2000,
    include_specials: bool = False,
) -> list[dict]:
    """按精确 TMDB Provider ID 并发读取本地季集库存。"""
    jobs: list[Callable[[], dict]] = []
    for server_type, server_name, _web_url, client in _configured_media_sources():
        jobs.append(
            lambda server_type=server_type, server_name=server_name, client=client: _run_owned_media_source_job(
                client,
                lambda: _strict_series_inventory_source_payload(
                    server_type, server_name, client, tmdb_id, max_episodes, include_specials
                ),
            )
        )
    return _run_media_source_jobs(jobs)


def inspect_series_episode_sources(
    query: str,
    *,
    tmdb_id: str = "",
    max_episodes: int = 2000,
    include_specials: bool = False,
    library_name: str = "",
    identity_batch: MediaIdentityBatch | None = None,
) -> list[dict]:
    """并发核对各媒体服务器的剧集候选和本地季集集合。"""
    jobs: list[Callable[[], dict]] = []
    for server_type, server_name, _web_url, client in _configured_media_sources():
        jobs.append(
            lambda server_type=server_type, server_name=server_name, client=client: _run_owned_media_source_job(
                client,
                lambda: _series_source_payload(
                    server_type, server_name, client, query, tmdb_id, max_episodes,
                    include_specials,
                    library_name,
                    identity_batch,
                ),
            )
        )
    return _run_media_source_jobs(jobs)


def _series_episode_candidates(sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in sources:
        for candidate in [source.get("selected"), *(source.get("candidates") or [])]:
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("name") or "").strip()
            year = str(candidate.get("year") or "").strip()
            tmdb_id = str(candidate.get("tmdb_id") or "").strip()
            if not name or (name, year, tmdb_id) in seen:
                continue
            seen.add((name, year, tmdb_id))
            candidates.append({"name": name, "year": year, **({"tmdb_id": tmdb_id} if tmdb_id else {})})
            if len(candidates) >= 8:
                return candidates
    return candidates


def _series_episode_inventory(source: dict[str, Any]) -> tuple[dict[str, Any], set[tuple[int, int]]]:
    episodes = {
        (int(item[0]), int(item[1]))
        for item in source.get("episodes") or []
        if isinstance(item, (list, tuple))
        and len(item) == 2
        and str(item[0]).isdigit()
        and str(item[1]).isdigit()
        and int(item[0]) > 0
        and int(item[1]) > 0
    }
    selected = (
        source.get("selected") if isinstance(source.get("selected"), dict) else {}
    )
    return {
        "server_type": str(source.get("server_type") or ""),
        "server_name": str(source.get("server_name") or "媒体服务器"),
        "library_name": str(source.get("library_name") or ""),
        "status": str(source.get("status") or "unavailable"),
        "title": str(selected.get("name") or ""),
        "year": str(selected.get("year") or ""),
        "tmdb_id": str(selected.get("tmdb_id") or "").strip(),
        "local_episode_count": len(episodes),
        "season_count": len({season for season, _episode in episodes}),
        "truncated": bool(source.get("truncated")),
        "ignored_specials": max(0, int(source.get("ignored_specials", 0) or 0)),
        "ignored_unknown": max(0, int(source.get("ignored_unknown", 0) or 0)),
    }, episodes


def _series_episode_seasons(episodes: set[tuple[int, int]]) -> list[dict[str, Any]]:
    seasons: dict[int, list[int]] = {}
    for season, episode in sorted(episodes):
        seasons.setdefault(season, []).append(episode)
    return [
        {
            "season": season,
            "count": len(numbers),
            "first_episode": min(numbers),
            "last_episode": max(numbers),
            # 首末集只表示边界，断号必须保留；每个范围都是实际观察到的闭区间。
            "episode_ranges": [
                [run[0][1], run[-1][1]]
                for _, group in groupby(enumerate(numbers), lambda item: item[1] - item[0])
                if (run := list(group))
            ],
        }
        for season, numbers in seasons.items()
    ]


def count_series_episodes(
    query: str = "",
    *,
    tmdb_id: str = "",
    library_name: str = "",
) -> dict[str, Any]:
    """汇总本地普通季集库存；返回普通字典，不访问 TMDB 或推断缺集。"""
    if not isinstance(query, str):
        raise ValueError("query 必须是字符串")
    query = unicodedata.normalize("NFKC", query).strip(" ，。！？?、:：")
    if len(query) > 120:
        raise ValueError("query 最多 120 个字符")
    if any(ord(char) < 32 or ord(char) == 127 for char in query):
        raise ValueError("query 包含不允许的控制字符")
    if not isinstance(tmdb_id, str):
        raise ValueError("tmdb_id 必须是字符串")
    tmdb_id = tmdb_id.strip()
    if tmdb_id and (
        not tmdb_id.isascii() or not tmdb_id.isdigit() or not 1 <= len(tmdb_id) <= 10
    ):
        raise ValueError("tmdb_id 必须是 1 到 10 位数字")
    if not query and not tmdb_id:
        raise ValueError("query 与 tmdb_id 至少提供一项")
    if library_name is None:
        library_name = ""
    if not isinstance(library_name, str):
        raise ValueError("library_name 必须是字符串")
    if library_name:
        library_name = unicodedata.normalize("NFKC", library_name).strip(" ，。！？?、:：")
        if not library_name or len(library_name) > 80:
            raise ValueError("library_name 必须是 1 到 80 个字符")
        if any(ord(char) < 32 or ord(char) == 127 for char in library_name):
            raise ValueError("library_name 包含不允许的控制字符")
    found_statuses = {"ready", "unmapped"}
    inspect_kwargs: dict[str, Any] = {
        "tmdb_id": tmdb_id,
        "max_episodes": 2000,
        "include_specials": False,
    }
    if library_name:
        inspect_kwargs["library_name"] = library_name
    sources = inspect_series_episode_sources(query, **inspect_kwargs)
    if not sources:
        return dict(
            ok=False,
            status="not_configured",
            summary="尚未配置可查询的 Jellyfin 或 Emby 媒体服务器",
            data={
                "query": query,
                "library_name": library_name,
                "local_episode_count": None,
                "inventory_complete": False,
                "matched_source_count": 0,
                "source_count": 0,
                "sources": [],
            },
            suggestions=["请先在设置中启用并配置 Jellyfin 或 Emby，然后重试。"],
            error="media_server_not_configured",
        )

    inventories = [_series_episode_inventory(source) for source in sources]
    safe_sources = [source for source, _episodes in inventories]
    matched = [(source, episodes) for source, episodes in inventories if source["status"] in found_statuses]
    identities = [
        (source["tmdb_id"], _normalize_series_identity_title(source["title"]), source["year"].strip())
        for source, _episodes in matched
    ]
    # 双方均有 Provider ID 时以 ID 为准；缺少 ID 才核对标题与双方已知年份。
    identity_conflict = any(
        left_id != right_id if left_id and right_id else (
            (left_title and right_title and left_title != right_title)
            or (left_year and right_year and left_year != right_year)
        )
        for (left_id, left_title, left_year), (right_id, right_title, right_year)
        in combinations(identities, 2)
    )
    if not matched or identity_conflict:
        statuses = {"ambiguous"} if identity_conflict else {source["status"] for source in safe_sources}
        candidates = _series_episode_candidates(sources)
        if statuses == {"library_not_found"}:
            summary = f"没有找到名为「{library_name}」的媒体库"
            status = "library_not_found"
            suggestions = ["请核对媒体库名称，或先列出媒体服务器中的媒体库。"]
        elif "library_ambiguous" in statuses:
            summary = f"有多个媒体库与「{library_name}」相似，暂时无法确定范围"
            status = "library_ambiguous"
            suggestions = ["请使用媒体服务器中显示的完整媒体库名称重试。"]
        elif statuses == {"not_found"}:
            scope_label = (
                f"「{library_name}」媒体库" if library_name else "已配置的媒体库"
            )
            summary = f"没有在{scope_label}中找到《{query or 'TMDB ' + tmdb_id}》"
            status = "not_found"
            suggestions = ["请尝试官方名称、原始名称，或提供 TMDB ID。"]
        elif "ambiguous" in statuses or "conflict" in statuses:
            summary = f"找到多个可能的《{query}》，暂时无法确定要统计哪一部"
            status = "ambiguous"
            suggestions = ["请补充首播年份、完整剧名或 TMDB ID。"]
        else:
            summary = "媒体服务器当前不可用，暂时无法读取本地集数"
            status = "unavailable"
            suggestions = ["请检查 Jellyfin / Emby 连接状态后重试。"]
        return dict(
            ok=False,
            status=status,
            summary=summary,
            data={
                "query": query,
                "library_name": library_name,
                "local_episode_count": None,
                "inventory_complete": False,
                "matched_source_count": len(matched),
                "source_count": len(sources),
                "candidates": candidates,
                "sources": safe_sources,
            },
            suggestions=suggestions,
            error=status,
        )

    episode_union: set[tuple[int, int]] = set()
    ignored_specials = 0
    ignored_unknown = 0
    truncated = False
    title = query
    year = ""
    for source, episodes in matched:
        title = source["title"] or title
        year = source["year"] or year
        ignored_specials += source["ignored_specials"]
        ignored_unknown += source["ignored_unknown"]
        truncated = truncated or source["truncated"]
        episode_union.update(episodes)

    seasons = _series_episode_seasons(episode_union)
    count = len(episode_union)
    incomplete = any(
        source["truncated"] or source["ignored_unknown"]
        or source["status"] not in found_statuses | {"not_found"}
        for source in safe_sources
    )
    status = "partial" if incomplete else "success"
    ok = True
    source_label = matched[0][0]["server_name"]
    if len(matched) > 1:
        source_label = f"{len(matched)} 个媒体服务器"
    summary = f"{source_label} 中《{title}》本地收录 {count} 集"
    if incomplete:
        summary += "，但部分来源不可用、有歧义、未完整编号或读取被截断"

    suggestions: list[str] = []
    if incomplete:
        suggestions.append(
            "当前数量是已确认的本地普通集下限；范围仅代表已确认收录，未列出的季集不能据此认定不存在，请核对来源状态。"
        )
    elif ignored_specials:
        suggestions.append(
            f"默认未计入特别篇（第 0 季），共忽略 {ignored_specials} 项。"
        )

    return dict(
        ok=ok,
        status=status,
        summary=summary,
        data={
            "query": query,
            "library_name": library_name,
            "title": title,
            "year": year,
            "local_episode_count": count,
            "inventory_complete": not incomplete,
            "season_count": len(seasons),
            "seasons": seasons,
            "matched_source_count": len(matched),
            "source_count": len(sources),
            "ignored_specials": ignored_specials,
            "ignored_unknown": ignored_unknown,
            "truncated": truncated,
            "sources": safe_sources,
            "count_definition": "Jellyfin / Emby 中已识别季号和集号的本地普通集；跨媒体服务器重复季集号只计一次。",
        },
        suggestions=suggestions,
        error="",
    )


def inspect_media_identity_sources(
    tmdb_id: str, media_type: str, *, identity_batch: MediaIdentityBatch | None = None,
) -> list[dict]:
    """并发核对电影或剧集是否已按精确 TMDB ID 入库。"""
    jobs: list[Callable[[], dict]] = []
    for server_type, server_name, _web_url, client in _configured_media_sources():
        jobs.append(
            lambda server_type=server_type, server_name=server_name, client=client: _run_owned_media_source_job(
                client,
                lambda: _media_identity_source_payload(
                    server_type, server_name, client, tmdb_id, media_type, identity_batch
                ),
            )
        )
    return _run_media_source_jobs(jobs)


def load_media_identity_snapshot(
    media_types: tuple[str, ...], *, deadline_at: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict]:
    """每个请求读取自己的库存快照；仅保存身份索引，不全局缓存库存。"""
    jobs: list[Callable[[], dict]] = []
    for server_type, server_name, _web_url, client in _configured_media_sources():
        jobs.append(
            lambda server_type=server_type, server_name=server_name, client=client: _run_owned_media_source_job(
                client,
                lambda: _media_identity_inventory_source_payload(
                    server_type, server_name, client, media_types, deadline_at, cancelled,
                ),
            )
        )
    return _run_media_source_jobs(jobs)


def inspect_media_identity_batch(
    identities: list[dict[str, str]], *, snapshot: list[dict] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict]:
    """统一身份匹配；独立核验读取新快照，续页显式复用当前请求的快照。"""
    if snapshot is None:
        snapshot = load_media_identity_snapshot(
            tuple(dict.fromkeys(item["media_type"] for item in identities)), cancelled=cancelled,
        )
    sources = []
    for source in snapshot:
        results = []
        for identity in identities:
            inventory = source["inventories"].get(identity["media_type"])
            status, match = "indeterminate", "none"
            if source["status"] in {"ready", "partial"} and inventory is not None:
                if identity["tmdb_id"] in inventory["exact_ids"]:
                    status, match = "present", "provider_id"
                elif (_normalize_series_identity_title(identity.get("title")), (identity.get("year") or "").strip()) in inventory["unmapped_titles"]:
                    status, match = "possible", "title_year"
                elif not inventory["truncated"] and (identity.get("title") or not inventory["unmapped"]):
                    status = "missing"
            results.append({"tmdb_id": identity["tmdb_id"], "media_type": identity["media_type"],
                            "status": status, "match": match})
        sources.append({**{key: source[key] for key in ("server_type", "server_name", "status", "error")},
                        "inventories": {kind: {key: value[key] for key in ("total", "truncated", "unmapped")}
                                        for kind, value in source["inventories"].items()}, "items": results})
    return sources


def summarize_media_identity_batch(
    identities: list[dict[str, str]], sources: list[dict]
) -> dict:
    """仅聚合已取得的批量身份数据；无媒体源时保留无法判断的身份。"""
    allowed_statuses = {"present", "possible", "missing", "indeterminate"}
    source_items = {
        (str(source.get("server_type") or ""), str(source.get("server_name") or "")): {
            (str(item.get("media_type") or ""), str(item.get("tmdb_id") or "")): item
            for item in source.get("items", [])
            if isinstance(item, dict)
        }
        for source in sources
    }
    results: list[dict[str, Any]] = []
    counts = {status: 0 for status in allowed_statuses}
    for identity in identities:
        key = (identity["media_type"], identity["tmdb_id"])
        per_source: list[dict[str, str]] = []
        statuses: list[str] = []
        for source in sources:
            source_key = (
                str(source.get("server_type") or ""),
                str(source.get("server_name") or ""),
            )
            item = source_items.get(source_key, {}).get(key, {})
            status = str(item.get("status") or "indeterminate")
            if status not in allowed_statuses:
                status = "indeterminate"
            statuses.append(status)
            per_source.append(
                {
                    "server_type": source_key[0],
                    "server_name": source_key[1],
                    "status": status,
                    "match": str(item.get("match") or "none"),
                }
            )
        if "present" in statuses:
            status = "present"
        elif "possible" in statuses:
            status = "possible"
        elif not sources or "indeterminate" in statuses:
            status = "indeterminate"
        else:
            status = "missing"
        counts[status] += 1
        results.append({**identity, "library_status": status, "sources": per_source})

    return {
        "total": len(results),
        "counts": counts,
        "items": results,
        "sources": [
            {
                "server_type": str(source.get("server_type") or ""),
                "server_name": str(source.get("server_name") or ""),
                "status": str(source.get("status") or "unavailable"),
                "inventories": source.get("inventories", {}),
            }
            for source in sources
        ],
    }


def normalize_media_presence_items(raw_items: object) -> list[dict[str, str]]:
    """从完整媒体条目校验、提取身份；忽略描述与旧库存状态，批量不受展示条数限制。"""
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items 必须是非空媒体身份列表")
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("媒体身份字段无效")
        tmdb_id = str(raw.get("tmdb_id") or "").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,9}", tmdb_id):
            raise ValueError("tmdb_id 必须是 1 到 10 位正整数")
        media_type = str(raw.get("media_type") or "").strip().casefold()
        if media_type not in {"movie", "tv"}:
            raise ValueError("media_type 仅支持 movie 或 tv")
        title = visible_text(raw.get("title"), limit=240)
        if raw.get("title") and not title:
            raise ValueError("提供 title 时必须是可见文本")
        year = str(raw.get("year") or "").strip()
        if year and not re.fullmatch(r"(?:19|20)[0-9]{2}", year):
            raise ValueError("year 必须是 1900 到 2099 的四位年份")
        key = (media_type, tmdb_id)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "title": title,
                "year": year,
            }
        )
    if not items:
        raise ValueError("items 中没有可核对的唯一媒体身份")
    return items


def batch_library_presence(
    items: list[dict[str, Any]], *, cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """一次读取每个媒体源/类型的库存，返回可序列化的批量身份核对结果。"""
    requested = normalize_media_presence_items(items)
    sources = inspect_media_identity_batch(requested, cancelled=cancelled)
    if not sources:
        return dict(
            ok=False,
            status="not_configured",
            summary="没有可核对的媒体服务器",
            data={"total": len(requested), "items": [], "sources": []},
            suggestions=["请先在设置中启用并配置 Jellyfin 或 Emby。"],
            error="没有可核对的媒体服务器。",
        )

    data = summarize_media_identity_batch(requested, sources)
    counts = data["counts"]
    unavailable = sum(
        1 for source in sources if str(source.get("status") or "") != "ready"
    )
    status = (
        "partial"
        if unavailable or counts["possible"] or counts["indeterminate"]
        else "success"
    )
    return dict(
        ok=True,
        status=status,
        summary=(
            f"已批量核对 {data['total']} 部媒体：在库 {counts['present']}、"
            f"缺失 {counts['missing']}、待确认 {counts['possible']}、"
            f"无法判断 {counts['indeterminate']}"
        ),
        data=data,
        suggestions=[
            "present 为精确 TMDB 身份命中；possible 表示同名同年但本地条目缺少 TMDB 映射。"
        ],
        error="",
    )


_REGION_COUNTRY_ALIASES = {
    "美国": "US", "美剧": "US", "英国": "GB", "英剧": "GB",
    "中国": "CN", "中国大陆": "CN", "国产": "CN", "国产剧": "CN",
    "中国香港": "HK", "香港": "HK", "中国台湾": "TW", "台湾": "TW",
    "日本": "JP", "日剧": "JP", "韩国": "KR", "韩剧": "KR",
    "法国": "FR", "德国": "DE", "印度": "IN", "意大利": "IT", "西班牙": "ES",
}


def _recommendation_filters(normalized: dict[str, Any]) -> dict[str, str]:
    """把用户语义筛选转换为榜单 Provider 的受控参数。"""
    provider = str(normalized["provider"])
    media_type = str(normalized["media_type"])
    year = str(normalized.get("year") or "")
    region = str(normalized.get("region") or "")
    genre = str(normalized.get("genre") or "")
    # Provider 注册表是唯一的选项来源；公共收集入口仅翻译口语别名，不另养类型目录。
    options = {
        field["key"]: {item["label"]: item["value"] for item in field["options"]}
        for field in list_filter_definitions(provider, media_type)["filters"]
    }
    country = _REGION_COUNTRY_ALIASES.get(region, "")
    if not country and re.fullmatch(r"[A-Za-z]{2}", region):
        country = region.upper()
    genre = "动画" if genre == "动漫" else genre
    filters: dict[str, str] = {}
    if provider == "tmdb":
        date = "release_date" if media_type == "movie" else "first_air_date"
        for field, suffix, day in (("year_from", "gte", "01-01"), ("year_to", "lte", "12-31")):
            if normalized.get(field):
                filters[f"{date}.{suffix}"] = f"{normalized[field]}-{day}"
        if year:
            filters[
                "primary_release_year"
                if media_type == "movie"
                else "first_air_date_year"
            ] = year
        if region and not country:
            raise ValueError("region 无法转换为明确产地；请使用国家/地区名称或双字母代码，不以语言代替地区")
        if country:
            filters["with_origin_country"] = country
        if media_type == "tv":
            genre = {
                "动作": "动作冒险", "冒险": "动作冒险", "科幻": "科幻奇幻",
                "奇幻": "科幻奇幻", "战争": "战争政治", "历史": "战争政治",
            }.get(genre, genre)
        key, value = "with_genres", genre
    else:
        if (media_type == "movie" and region) or (media_type == "tv" and genre):
            raise ValueError("豆瓣推荐接口仅支持电影题材或剧集地区筛选；请改用 tmdb 并保留全部原筛选条件")
        key = "tags"
        value = genre if media_type == "movie" else {
            "CN": "国产剧", "US": "美剧", "GB": "英剧", "JP": "日剧", "KR": "韩剧",
        }.get(country, region)
    if value:
        canonical = options[key].get(value, value)
        if canonical not in options[key].values():
            raise ValueError(f"{provider} 不支持该推荐筛选 {value}；可用选项：{'、'.join(options[key])}")
        filters[key] = canonical
    return filters


def collect_discovery_recommendations(
    service_factory: Callable[[], Any], *, provider: str, media_type: str,
    page: int, limit: int, filters: dict[str, str] | None = None, offset: int = 0,
    year_from: str = "", year_to: str = "", exclude_library: bool = False,
    category: str = "", year: str = "", region: str = "", genre: str = "",
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """有界收集推荐；共享单页来源和身份核验，不改变探索页的显式分页。"""
    if not isinstance(provider, str) or not isinstance(media_type, str):
        raise ValueError("provider 和 media_type 必须是字符串")
    provider, media_type = provider.strip().lower(), media_type.strip().lower()
    expected_category = COLLECTION_CATEGORIES.get(provider, "")
    if provider not in COLLECTION_CATEGORIES or media_type not in {"movie", "tv"}:
        raise ValueError("推荐仅支持 tmdb/douban 的 movie/tv")
    if category and category != expected_category:
        raise ValueError("收集推荐使用 tmdb/discover 或 douban/recommend")
    category = expected_category
    if type(page) is not int or not 1 <= page <= 100 or type(limit) is not int or limit < 1:
        raise ValueError("page 必须在 1 到 100，limit 必须是正整数")
    if type(offset) is not int or offset < 0 or type(exclude_library) is not bool:
        raise ValueError("offset 必须是非负整数，exclude_library 必须是布尔值")
    years = [year, year_from, year_to]
    if any(not isinstance(value, str) or (value and not re.fullmatch(r"(?:19|20)[0-9]{2}", value)) for value in years):
        raise ValueError("推荐年份必须是 1900 到 2099 的四位年份")
    if (year_from or "1900") > (year_to or "2099") or (year and not (year_from or "1900") <= year <= (year_to or "2099")):
        raise ValueError("推荐年份与范围冲突")
    if any(not isinstance(value, str) or any(unicodedata.category(char).startswith("C") for char in value) for value in (region, genre)):
        raise ValueError("地区和题材必须是可见文本")
    translated = _recommendation_filters({
        "provider": provider, "media_type": media_type, "year": year,
        "year_from": year_from, "year_to": year_to,
        "region": unicodedata.normalize("NFKC", region).strip(),
        "genre": unicodedata.normalize("NFKC", genre).strip(),
    })
    if filters is not None and not isinstance(filters, dict):
        raise ValueError("filters 必须是对象")
    raw_filters = filters or {}
    if any(key in raw_filters and raw_filters[key] != value for key, value in translated.items()):
        raise ValueError("语义筛选与 filters 冲突")
    filters = validate_filters(provider, category, media_type, {**raw_filters, **translated})
    year_from, year_to = year or year_from, year or year_to
    deadline_at = time.monotonic() + 20
    service = None
    snapshot = None
    cards: list[MediaCard] = []
    accepted: list[MediaCard] = []
    seen: set[tuple[str, str]] = set()
    positions: dict[tuple[str, str], tuple[int, int]] = {}
    pages_read: list[int] = []
    candidates_seen = duplicates = retry_after = 0
    cached, stale, has_more = True, False, False
    provider_status, error_code = "healthy", ""
    library_check = None
    checked: list[dict] = []
    unverified: dict | None = None

    def interrupted() -> str:
        if cancelled and cancelled():
            return "cancelled"
        return "timeout" if time.monotonic() >= deadline_at else ""

    stop_reason = interrupted()
    while not stop_reason:
        try:
            if service is None:
                service = service_factory()
            stop_reason = interrupted()
            if stop_reason:
                break
            source_page = service.list_items(provider, category, media_type, page, filters)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - 隔离 Provider 故障，保留已接收的分页事实
            error_code = exc.code if isinstance(exc, ProviderError) else "unavailable"
            retry_after = exc.retry_after if isinstance(exc, ProviderError) else 0
            provider_status = "not_configured" if error_code == "not_configured" else "unavailable"
            stop_reason = interrupted() or ("timeout" if error_code == "timeout" or isinstance(exc, TimeoutError) else "provider_error")
            break
        pages_read.append(page)
        has_more = bool(source_page.has_more)
        cached, stale = cached and bool(source_page.cached), stale or bool(source_page.stale)
        if source_page.provider.status != "healthy":
            provider_status = source_page.provider.status
        retry_after = max(retry_after, source_page.provider.retry_after)
        page_start = len(cards)
        for index, card in enumerate(source_page.items):
            if not isinstance(card, MediaCard) or card.media_type != media_type:
                continue
            key = (card.media_type, card.tmdb_id or card.stable_id)
            if index < offset:
                seen.add(key)
                continue
            candidates_seen += 1
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            year = (card.year or "").strip()
            if (year_from or year_to) and (not re.fullmatch(r"(?:19|20)[0-9]{2}", year)
                    or (year_from and year < year_from) or (year_to and year > year_to)):
                continue
            cards.append(card)
            positions[key] = (page, index + 1)
        stop_reason = interrupted()
        if exclude_library:
            new_cards = cards[page_start:]
            identities = [{"tmdb_id": card.tmdb_id, "media_type": card.media_type,
                           "title": card.title, "year": (card.year or "").strip()}
                          for card in new_cards if re.fullmatch(r"[1-9][0-9]{0,9}", card.tmdb_id)]
            if identities and snapshot is None and not stop_reason:
                try:
                    snapshot = load_media_identity_snapshot(
                        (media_type,), deadline_at=deadline_at, cancelled=cancelled,
                    )
                except Exception:  # noqa: BLE001 - 库存读取失败只能降为未知
                    # 无法建立库存时仍返回候选范围，但绝不把未知状态当成未入库。
                    snapshot = []
                stop_reason = interrupted()
            sources = inspect_media_identity_batch(identities, snapshot=snapshot or []) if identities else []
            batch_check = summarize_media_identity_batch(identities, sources)
            checked.extend(batch_check["items"])
            if identities or library_check is None:
                library_check = batch_check
            missing = {(item["media_type"], item["tmdb_id"]) for item in batch_check["items"]
                       if item["library_status"] == "missing"}
            accepted.extend(card for card in new_cards if (card.media_type, card.tmdb_id) in missing)
            if identities and (not snapshot or any(
                source["inventories"].get(media_type, {}).get("truncated", True) for source in snapshot
            )):
                unverified = next((item for item in batch_check["items"] if item["library_status"] != "present"), None)
        else:
            accepted = cards
        stop_reason = interrupted() or stop_reason or ("library_incomplete" if unverified else
            "target_reached" if len(accepted) >= limit else
            "source_exhausted" if not has_more else "page_limit" if len(pages_read) >= 8 or page >= 100 else "")
        if not stop_reason:
            page += 1
            offset = 0

    if library_check is not None:
        library_check.update(items=checked, total=len(checked), unmapped_candidates=len(cards) - len(checked),
                             counts={status: sum(item["library_status"] == status for item in checked)
                                     for status in library_check["counts"]})
    returned = min(len(accepted), limit)
    next_page = pages_read[-1] + 1 if pages_read else page
    next_offset = offset if not pages_read else 0
    if next_page > 100 or (pages_read and not has_more):
        next_page = None
    if len(accepted) > limit:
        last = accepted[returned - 1]
        # 续取必须从最后返回条目的源位置开始，而非跳过已读取但未返回的半页。
        next_page, next_offset = positions[(last.media_type, last.tmdb_id or last.stable_id)]
    elif unverified:
        # 库存未读完时继续翻来源页也无法排库；恢复后重查首个未核验候选。
        next_page, position = positions[(unverified["media_type"], unverified["tmdb_id"])]
        next_offset, has_more = position - 1, True
    unresolved = (library_check["counts"]["possible"] + library_check["counts"]["indeterminate"]
                  + library_check["unmapped_candidates"]) if library_check is not None else 0
    status = "success" if returned else "empty"
    if stop_reason == "cancelled":
        status = "cancelled"
    elif error_code and not pages_read:
        status = "unavailable"
    elif unresolved or (returned < limit and (returned or stop_reason != "source_exhausted")):
        status = "partial"
    return {
        "ok": status not in {"cancelled", "unavailable"}, "status": status, "partial": status == "partial",
        "items": [{**card.to_dict(), **({"library_status": "missing"} if exclude_library else {})}
                  for card in accepted[:limit]], "total": len(accepted), "returned": returned,
        "has_more": has_more or len(accepted) > limit,
        "cached": bool(pages_read) and cached, "stale": stale,
        "provider_status": provider_status, "retry_after": retry_after,
        "error_code": error_code,
        **({"library_check": library_check} if library_check is not None else {}),
        "scope": {
            "requested": limit, "returned": returned, "shortfall": limit - returned,
            "pages_read": pages_read, "candidates_seen": candidates_seen, "duplicates": duplicates,
            "stop_reason": stop_reason, "complete": stop_reason == "target_reached",
            "next_page": next_page, "next_offset": next_offset,
            "max_pages": 8, "timeout_seconds": 20,
        },
    }
