"""服务聚合层：媒体服务器看板、客户端工厂。"""
from __future__ import annotations

import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from app import database as db
from app.clients.base import DashboardData, MediaServerClient
from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.config import get, get_bool, get_int
from app.logger import get_logger

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
        logger.warning(f"读取看板自动化状态失败: {exc}")
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
class _DashboardCacheEntry:
    key: tuple[str, ...]
    expires_at: float
    boards: list[DashboardData]


_dashboard_cache: _DashboardCacheEntry | None = None
_dashboard_cache_lock = threading.Lock()
_dashboard_refresh_lock = threading.Lock()


def _dashboard_config_key() -> tuple[str, ...]:
    """配置变化自动生成新缓存键，避免返回旧服务器数据。"""
    return (
        get("EMBY_ENABLED"),
        get("EMBY_URL"),
        get("EMBY_TOKEN"),
        get("JELLYFIN_ENABLED"),
        get("JELLYFIN_URL"),
        get("JELLYFIN_API_KEY"),
    )


def clear_dashboard_cache() -> None:
    """配置保存或媒体库刷新后主动失效看板缓存。"""
    global _dashboard_cache
    with _dashboard_cache_lock:
        _dashboard_cache = None


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


def _dashboard_jobs() -> list[Callable[[], DashboardData]]:
    jobs: list[Callable[[], DashboardData]] = []
    if get_bool("EMBY_ENABLED"):
        url, token = get("EMBY_URL"), get("EMBY_TOKEN")
        if url and token:
            jobs.append(
                lambda url=url, token=token: _decorate_dashboard(
                    EmbyClient(url, token).get_dashboard(),
                    server_type="emby",
                    web_url=url,
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
                lambda url=url, key=key: _decorate_dashboard(
                    JellyfinClient(url, key).get_dashboard(),
                    server_type="jellyfin",
                    web_url=url,
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


def _fetch_dashboards() -> list[DashboardData]:
    jobs = _dashboard_jobs()
    if len(jobs) <= 1:
        return [job() for job in jobs]
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="media-dashboard") as pool:
        futures = [pool.submit(job) for job in jobs]
        return [future.result() for future in futures]


def build_dashboards(*, force: bool = False) -> list[DashboardData]:
    """构造媒体服务器看板；短时缓存降低外部 API 聚合延迟。"""
    global _dashboard_cache
    key = _dashboard_config_key()
    now = time.monotonic()
    if not force:
        with _dashboard_cache_lock:
            cached = _dashboard_cache
            if cached and cached.key == key and cached.expires_at > now:
                return cached.boards

    # 同一时刻只允许一个请求刷新，其他请求等待后复用结果，避免缓存击穿。
    with _dashboard_refresh_lock:
        now = time.monotonic()
        if not force:
            with _dashboard_cache_lock:
                cached = _dashboard_cache
                if cached and cached.key == key and cached.expires_at > now:
                    return cached.boards
        boards = _fetch_dashboards()
        ttl = max(1, min(get_int("DASHBOARD_CACHE_TTL_SECONDS", 30), 300))
        if any(board.error or board.partial_errors or not board.online for board in boards):
            ttl = min(ttl, 5)
        with _dashboard_cache_lock:
            _dashboard_cache = _DashboardCacheEntry(
                key=key,
                expires_at=time.monotonic() + ttl,
                boards=boards,
            )
        return boards


def get_cached_dashboards_or_stubs() -> tuple[list[DashboardData], bool]:
    """返回 (dashboards, is_cached)。
    若已有热缓存，直接返回 (cached.boards, True)；
    若无缓存，快速返回已配置服务的骨架列表 (stubs, False)，让页面 0ms 秒开。
    """
    global _dashboard_cache
    key = _dashboard_config_key()
    now = time.monotonic()
    with _dashboard_cache_lock:
        cached = _dashboard_cache
        if cached and cached.key == key and cached.expires_at > now:
            return cached.boards, True

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
    if get_bool("EMBY_ENABLED"):
        url, token = get("EMBY_URL"), get("EMBY_TOKEN")
        if url and token:
            sources.append(("emby", "Emby", url, EmbyClient(url, token)))
    if get_bool("JELLYFIN_ENABLED"):
        url, token = get("JELLYFIN_URL"), get("JELLYFIN_API_KEY")
        if url and token:
            sources.append(("jellyfin", "Jellyfin", url, JellyfinClient(url, token)))
    return sources


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


def build_recent_media(*, limit: int = 72) -> list[dict]:
    """并发聚合所有已配置媒体服务器的最近入库内容。"""
    normalized_limit = max(1, min(int(limit or 72), 200))
    jobs: list[Callable[[], dict]] = []
    for server_type, server_name, web_url, client in _configured_media_sources():
        jobs.append(
            lambda server_type=server_type, server_name=server_name, web_url=web_url, client=client: _media_source_payload(
                server_type,
                server_name,
                web_url,
                lambda: client.recent_media(normalized_limit),
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


def search_media_servers(query: str, *, limit: int = 8) -> list[dict]:
    """并发搜索所有已配置 Jellyfin / Emby 服务器。"""
    normalized_limit = max(1, min(int(limit or 8), 50))
    jobs: list[Callable[[], dict]] = []
    for server_type, server_name, web_url, client in _configured_media_sources():
        jobs.append(
            lambda server_type=server_type, server_name=server_name, web_url=web_url, client=client: _media_source_payload(
                server_type,
                server_name,
                web_url,
                lambda: client.search_media(query, normalized_limit),
            )
        )
    return _run_media_source_jobs(jobs)


def _media_identity_source_payload(
    server_type: str,
    server_name: str,
    client: MediaServerClient,
    tmdb_id: str,
    media_type: str,
) -> dict:
    """读取单个媒体源的精确 TMDB 身份，不依赖标题模糊搜索。"""
    try:
        return {
            "server_type": server_type,
            "server_name": server_name,
            "status": "ready",
            "present": bool(client.has_tmdb_media(tmdb_id, media_type)),
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
    requested = str(library_name or "").strip()
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
    truncated = bool(search_truncated)
    ignored_specials = 0
    ignored_unknown = 0
    for item in selected_items:
        inventory = client.list_series_episode_inventory(
            item.id, max_episodes=max_episodes, page_size=200,
            include_specials=include_specials,
        )
        episode_union.update(inventory.episodes)
        local_total += int(inventory.total or 0)
        truncated = truncated or bool(inventory.truncated)
        ignored_specials += int(inventory.ignored_specials or 0)
        ignored_unknown += int(inventory.ignored_unknown or 0)
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
        "truncated": bool(truncated),
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
        if parent_id:
            title_search = client.search_series_candidates(
                query, limit=6, parent_id=parent_id
            )
        else:
            title_search = client.search_series_candidates(query, limit=6)
        provider_search = None
        if explicit_tmdb_id:
            if parent_id:
                provider_search = client.find_series_candidates_by_tmdb(
                    explicit_tmdb_id, limit=20, parent_id=parent_id
                )
            else:
                provider_search = client.find_series_candidates_by_tmdb(
                    explicit_tmdb_id, limit=20
                )
            provider_matches = [
                item for item in provider_search.candidates
                if str(item.tmdb_id or "") == explicit_tmdb_id
            ]
            if provider_matches:
                return _series_inventory_source_result(
                    server_type=server_type,
                    server_name=server_name,
                    client=client,
                    selected_items=provider_matches,
                    candidates=_public_series_candidates(title_search, provider_search),
                    status="ready",
                    mapping_status="provider_id",
                    max_episodes=max_episodes,
                    include_specials=include_specials,
                    library_name=resolved_library_name,
                    search_truncated=bool(provider_search.truncated),
                )

        candidates = _public_series_candidates(title_search, provider_search)
        selected_items = _complete_series_matches(title_search, query)
        if not selected_items:
            if int(title_search.total or 0) == 0 or not title_search.candidates:
                return _empty_series_source_result(
                    server_type=server_type,
                    server_name=server_name,
                    status="not_found",
                    candidates=candidates,
                    truncated=bool(title_search.truncated),
                    library_name=resolved_library_name,
                )
            return _empty_series_source_result(
                server_type=server_type,
                server_name=server_name,
                status="ambiguous",
                candidates=candidates,
                truncated=bool(title_search.truncated),
                library_name=resolved_library_name,
            )

        mapped_ids = {str(item.tmdb_id or "") for item in selected_items} - {""}
        if explicit_tmdb_id and mapped_ids and mapped_ids != {explicit_tmdb_id}:
            return _empty_series_source_result(
                server_type=server_type,
                server_name=server_name,
                status="conflict",
                candidates=candidates,
                truncated=bool(title_search.truncated),
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
            search_truncated=bool(title_search.truncated),
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
            library_name=str(library_name or "").strip(),
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
    normalized_series = max(1, min(int(max_series or 50), 100))
    normalized_episodes = max(1, min(int(max_episodes or 2000), 2000))
    cursor = int(after_tmdb_id or 0) if str(after_tmdb_id or "").isdigit() else 0
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
            source["series_total"] = int(search.total or 0)
            source["series_enumerated"] = len(search.candidates)
            source["catalog_truncated"] = bool(search.truncated)
            source["truncated"] = bool(search.truncated)
            candidates = list(search.candidates)
            if scan_all:
                mapped = [
                    candidate for candidate in candidates
                    if str(candidate.tmdb_id or "").isdigit()
                    and int(str(candidate.tmdb_id)) > cursor
                ]
                mapped.sort(key=lambda candidate: int(str(candidate.tmdb_id)))
                # 巡检游标按 TMDB 媒体组推进，而不是按媒体服务器条目推进。
                # 同一剧集可能在多个媒体库或多个版本中重复出现；若按候选条目
                # 切批，重复 ID 恰好跨越边界时会让 next_tmdb_id 等于当前组，
                # 审计层因此无法处理该组并最终把它误判为停滞后跳过。
                mapped_tmdb_ids = list(dict.fromkeys(
                    str(candidate.tmdb_id) for candidate in mapped
                ))
                selected_tmdb_ids = set(mapped_tmdb_ids[:normalized_series])
                source["batch_remaining"] = len(mapped_tmdb_ids) > normalized_series
                selected = [
                    candidate for candidate in mapped
                    if str(candidate.tmdb_id) in selected_tmdb_ids
                ]
                if source["batch_remaining"]:
                    source["next_tmdb_id"] = mapped_tmdb_ids[normalized_series]
                source["unmapped_count"] = sum(
                    not str(candidate.tmdb_id or "").isdigit()
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
                        next_tmdb_id=str(candidate.tmdb_id or ""),
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
                        next_tmdb_id=str(candidate.tmdb_id or ""),
                        deadline_exhausted=deadline_hit,
                    )
                    break
                source["series"].append({
                    "name": candidate.name,
                    "year": candidate.year,
                    "tmdb_id": candidate.tmdb_id,
                    "episodes": list(inventory.episodes),
                    "local_total": int(inventory.total or 0),
                    "truncated": bool(inventory.truncated),
                    "ignored_specials": int(inventory.ignored_specials or 0),
                    "ignored_unknown": int(inventory.ignored_unknown or 0),
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
                candidates=[], truncated=bool(search.truncated),
            )
        return _series_inventory_source_result(
            server_type=server_type, server_name=server_name, client=client,
            selected_items=selected, candidates=_public_series_candidates(search),
            status="ready", mapping_status="provider_id",
            max_episodes=max_episodes, include_specials=include_specials,
            search_truncated=bool(search.truncated),
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
            lambda server_type=server_type, server_name=server_name, client=client: _strict_series_inventory_source_payload(
                server_type, server_name, client, tmdb_id, max_episodes, include_specials
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
) -> list[dict]:
    """并发核对各媒体服务器的剧集候选和本地季集集合。"""
    jobs: list[Callable[[], dict]] = []
    for server_type, server_name, _web_url, client in _configured_media_sources():
        jobs.append(
            lambda server_type=server_type, server_name=server_name, client=client: _series_source_payload(
                server_type, server_name, client, query, tmdb_id, max_episodes,
                include_specials,
                library_name,
            )
        )
    return _run_media_source_jobs(jobs)


def inspect_media_identity_sources(tmdb_id: str, media_type: str) -> list[dict]:
    """并发核对电影或剧集是否已按精确 TMDB ID 入库。"""
    jobs: list[Callable[[], dict]] = []
    for server_type, server_name, _web_url, client in _configured_media_sources():
        jobs.append(
            lambda server_type=server_type, server_name=server_name, client=client: _media_identity_source_payload(
                server_type, server_name, client, tmdb_id, media_type
            )
        )
    return _run_media_source_jobs(jobs)
