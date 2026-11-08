"""剧集完整性审计：只比较可靠映射后的本地集号与 TMDB 已播集号。"""
from __future__ import annotations

import threading
import re
import time
import unicodedata
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any

from app import config
from app.clients.tmdb import TMDBClient, close_tmdb_client
from app.discovery.models import ProviderError, ProviderNotConfigured
from app.logger import get_logger
from app.modules.media_identity import normalize_audit_date, normalize_episode_verification
from app.services import (
    inspect_library_series_sources, inspect_series_episode_sources, media_server_config_key, search_media_servers,
)

logger = get_logger(__name__)

_CACHE_TTL_SECONDS = 120
_CACHE_MAX_ENTRIES = 64
_MAX_TMDB_SEASONS = 24
_MAX_TMDB_EPISODES = 2000
_DEADLINE_SECONDS = 30.0
_MAX_TMDB_REQUESTS = 200
_MAX_FINDINGS = 20
_MAX_MISSING_SAMPLE = 20


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_episode_audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """供页面、脚本和 Agent 共用的业务输入；默认按本地日期审计已播进度。"""
    extra = set(arguments) - {"query", "tmdb_id", "season", "target_episode", "as_of", "library_name"}
    if extra:
        raise ValueError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    raw_query = arguments.get("query")
    if not isinstance(raw_query, str):
        raise ValueError("query 必须是字符串")
    query = unicodedata.normalize("NFKC", raw_query).strip()
    if not query or len(query) > 120 or any(unicodedata.category(char).startswith("C") for char in query):
        raise ValueError("搜索关键词必须为 1 到 120 个可见字符")

    tmdb_id = arguments.get("tmdb_id", "")
    if not isinstance(tmdb_id, str):
        raise ValueError("tmdb_id 必须是字符串")
    tmdb_id = tmdb_id.strip()
    if tmdb_id and (
        not tmdb_id.isascii() or not tmdb_id.isdigit() or not 1 <= len(tmdb_id) <= 10
    ):
        raise ValueError("tmdb_id 必须是 1 到 10 位数字")

    season = arguments.get("season")
    target_episode = arguments.get("target_episode")
    for name, value, maximum in (("season", season, 100), ("target_episode", target_episode, 1000)):
        if value is not None and (type(value) is not int or not 1 <= value <= maximum):
            raise ValueError(f"{name} 必须是 1 到 {maximum} 的整数")
    if target_episode is not None and season is None:
        raise ValueError("target_episode 必须与 season 一起提供")

    library_name = arguments.get("library_name", "")
    if library_name is None:
        library_name = ""
    if not isinstance(library_name, str):
        raise ValueError("library_name 必须是字符串")
    if library_name:
        library_name = unicodedata.normalize("NFKC", library_name).strip(" ，。！？?、:：")
        if not library_name or len(library_name) > 80 or any(unicodedata.category(char).startswith("C") for char in library_name):
            raise ValueError("library_name 必须是 1 到 80 个可见字符")

    normalized = {
        "query": query,
        "tmdb_id": tmdb_id,
        "season": season,
        "as_of": normalize_audit_date(arguments.get("as_of")),
    }
    if library_name:
        normalized["library_name"] = library_name
    if target_episode is not None:
        normalized["target_episode"] = target_episode
    return normalized


@dataclass
class EpisodeAuditResult:
    """共享审计事实；调用方自行生成展示、证据及后续操作。"""

    ok: bool
    status: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    checked_at: str = field(default_factory=_now)
    mapping_context: dict[str, Any] = field(default_factory=dict)
    suggestions: list[str] = field(default_factory=list)
    error: str = ""


def missing_episode_verification(
    audit: EpisodeAuditResult, *, season: int, episode: int,
) -> dict[str, Any] | None:
    """仅投影权威审计确认的已播缺集；不证明某个资源文件包含该集。"""
    if not audit.ok or audit.status != "updates_available":
        return None
    if type(season) is not int or type(episode) is not int:
        return None
    data = audit.data
    if data.get("target_episode") is not None:
        missing = (
            data.get("season") == season and data["target_episode"] == episode
            and data.get("target_missing") is True
        )
    else:
        missing = data.get("missing_sample_truncated") is False and any(
            isinstance(item, dict) and type(item.get("season")) is int
            and type(item.get("episode")) is int
            and (item["season"], item["episode"]) == (season, episode)
            for item in data.get("missing_sample", [])
        )
    if not missing:
        return None
    return normalize_episode_verification({
        "title": data.get("title"), "tmdb_id": data.get("tmdb_id"),
        "season": season, "episode": episode, "as_of": data.get("as_of"),
        "library_name": data.get("library_name", ""),
    })


_cache: dict[tuple[Any, ...], tuple[float, EpisodeAuditResult]] = {}
_inflight: dict[tuple[Any, ...], threading.Event] = {}
_cache_lock = threading.Lock()


def _with_episode_mapping_context(
    result: EpisodeAuditResult, context: dict[str, Any]
) -> EpisodeAuditResult:
    if context:
        result.mapping_context = {**context, "as_of": result.data["as_of"]}
    return result


def _local_episodes(arguments: dict[str, Any], sources: list[dict]) -> set[tuple[int, int]]:
    season = arguments.get("season")
    return {tuple(item) for source in sources for item in source.get("episodes", [])
            if season is None or item[0] == season}


def _base_data(arguments: dict[str, Any], sources: list[dict] | None = None) -> dict[str, Any]:
    ready = [source for source in (sources or []) if source.get("status") == "ready"]
    mapped_ids = {str((source.get("selected") or {}).get("tmdb_id") or "") for source in ready} - {""}
    requested_id = str(arguments.get("tmdb_id") or "")
    verified = bool(ready) and len(mapped_ids) <= 1 and (not requested_id or not mapped_ids or mapped_ids == {requested_id})
    local = _local_episodes(arguments, ready) if verified else set()
    selected = (ready[0].get("selected") or {}) if verified else {}
    return {
        "query": arguments["query"],
        "title": str(selected.get("name") or arguments["query"]),
        "tmdb_id": next(iter(mapped_ids)) if verified and mapped_ids else requested_id,
        "season": arguments.get("season"),
        "target_episode": arguments.get("target_episode"),
        "library_name": str(arguments.get("library_name") or ""),
        "as_of": arguments["as_of"],
        "local_episode_count": len(local) if verified else None,
        "latest_local": dict(zip(("season", "episode"), max(local))) if local else None,
        "local_inventory_complete": verified and all(
            source.get("status") in {"ready", "not_found"} and not source.get("truncated") and not source.get("ignored_unknown")
            for source in (sources or [])
        ),
        "expected_aired": None, "latest_aired": None, "missing_count": None,
        "sources": [
            {
                "server_type": str(source.get("server_type") or ""),
                "server_name": str(source.get("server_name") or "媒体服务器"),
                "library_name": str(source.get("library_name") or ""),
                "status": str(source.get("status") or "unavailable"),
                "candidates": list(source.get("candidates") or [])[:6],
                "selected": source.get("selected"),
                "mapping": dict(source.get("mapping") or {}),
                "local_episode_count": len(source.get("episodes") or []),
                "ignored_specials": int(source.get("ignored_specials", 0) or 0),
                "ignored_unknown": int(source.get("ignored_unknown", 0) or 0),
                "truncated": bool(source.get("truncated")),
            }
            for source in (sources or [])
        ],
    }


def _parse_air_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _normalize_identity_title(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _identity_year(value: object) -> str:
    year = str(value or "").strip()
    return year if len(year) == 4 and year.isascii() and year.isdigit() else ""


def _tmdb_year(item: dict[str, Any]) -> str:
    value = str(item.get("first_air_date") or "").strip()
    return value[:4] if len(value) >= 4 and value[:4].isascii() and value[:4].isdigit() else ""


def _tmdb_titles(item: dict[str, Any]) -> set[str]:
    return {
        normalized
        for normalized in (
            _normalize_identity_title(item.get("name")),
            _normalize_identity_title(item.get("original_name")),
        )
        if normalized
    }


def _source_identity(source: dict[str, Any]) -> tuple[str, str]:
    return _series_identity(source.get("selected") or {})


def _bind_unmapped_sources(
    sources: list[dict], tmdb_id: str, details: dict[str, Any]
) -> None:
    for source in sources:
        if source.get("status") != "unmapped" or not _details_match_series(source.get("selected") or {}, details):
            continue
        source["status"] = "ready"
        selected = source.get("selected")
        if isinstance(selected, dict):
            selected["tmdb_id"] = tmdb_id
        source["mapping"] = {"status": "title_year_fallback", "tmdb_id": tmdb_id}


def _resolve_unmapped_tmdb_id(
    client: TMDBClient, sources: list[dict]
) -> tuple[str, str, dict[str, Any] | None]:
    identities = {_source_identity(source) for source in sources}
    if len(identities) != 1:
        return "", "ambiguous", None
    title_key, year = next(iter(identities))
    if not title_key or not year:
        return "", "unmapped", None
    selected = sources[0].get("selected") or {}
    title = str(selected.get("name") or "").strip()
    matches = [
        item for item in client.search(title, year, "tv")
        if _tmdb_year(item) == year and title_key in _tmdb_titles(item)
    ]
    unique: dict[str, dict[str, Any]] = {}
    for item in matches:
        tmdb_id = str(item.get("id") or "").strip()
        if tmdb_id.isascii() and tmdb_id.isdigit() and 1 <= len(tmdb_id) <= 10:
            unique[tmdb_id] = item
    if not unique:
        return "", "unmapped", None
    if len(unique) > 1:
        return "", "ambiguous", None
    tmdb_id = next(iter(unique))
    return tmdb_id, "title_year_fallback", client.detail(tmdb_id, "tv")


def _audit_uncached(arguments: dict[str, Any]) -> EpisodeAuditResult:
    sources = inspect_series_episode_sources(
        arguments["query"],
        tmdb_id=arguments.get("tmdb_id", ""),
        max_episodes=2000,
        library_name=str(arguments.get("library_name") or ""),
    )
    data = _base_data(arguments, sources)
    if not sources:
        return EpisodeAuditResult(
            ok=False,
            status="not_configured",
            summary="没有可审计的媒体服务器",
            data=data,
            suggestions=["请先完整配置并启用 Jellyfin 或 Emby。"],
        )

    statuses = {str(source.get("status") or "unavailable") for source in sources}
    if statuses == {"unavailable"}:
        return EpisodeAuditResult(
            ok=False,
            status="unavailable",
            summary="媒体服务器暂时不可用，无法完成剧集审计",
            data=data,
            suggestions=["请检查媒体服务器连通性后重试。"],
            error="媒体服务器暂时不可用。",
        )
    if "ambiguous" in statuses:
        return EpisodeAuditResult(
            ok=False,
            status="ambiguous",
            summary="媒体库命中多个同名剧集，暂不做猜测",
            data=data,
            suggestions=["请提供明确的 TMDB ID 后重试。"],
        )
    if "conflict" in statuses:
        return EpisodeAuditResult(
            ok=False,
            status="conflict",
            summary="媒体库中的剧集身份与指定 TMDB ID 冲突",
            data=data,
            suggestions=["请核对剧名、年份和 TMDB ID 后重试。"],
        )

    ready = [source for source in sources if source.get("status") == "ready"]
    unmapped = [source for source in sources if source.get("status") == "unmapped"]
    if not ready and not unmapped and "library_ambiguous" in statuses:
        library_name = str(arguments.get("library_name") or "指定媒体库")
        return EpisodeAuditResult(
            ok=False,
            status="ambiguous",
            summary=f"有多个媒体库与「{library_name}」相似，暂时无法确定范围",
            data=data,
            suggestions=["请使用媒体服务器中显示的完整媒体库名称后重试。"],
        )
    if not ready and not unmapped and "library_not_found" in statuses:
        library_name = str(arguments.get("library_name") or "指定媒体库")
        return EpisodeAuditResult(
            ok=False,
            status="not_found",
            summary=f"没有找到名为「{library_name}」的媒体库",
            data=data,
            suggestions=["请核对媒体库名称，或不指定媒体库以检查全部已配置服务器。"],
        )
    if not ready and not unmapped and (
        "unavailable" in statuses or any(source.get("truncated") for source in sources)
    ):
        return EpisodeAuditResult(
            ok=False,
            status="unavailable" if "unavailable" in statuses else "inconclusive",
            summary="媒体服务器数据不完整，无法完成剧集审计",
            data=data,
            suggestions=["请检查不可用的媒体服务器后重试。"],
            error="媒体服务器库存未完整读取。",
        )
    if not ready and not unmapped:
        return EpisodeAuditResult(
            ok=False,
            status="not_found",
            summary="媒体库中没有找到可审计的剧集",
            data=data,
            suggestions=["可尝试剧集原名，或提供 TMDB ID。"],
        )

    explicit_tmdb_id = str(arguments.get("tmdb_id") or "").strip()
    mapped_ids = {
        str((source.get("selected") or {}).get("tmdb_id") or "") for source in ready
    } - {""}
    if len(mapped_ids) > 1 or (explicit_tmdb_id and mapped_ids and mapped_ids != {explicit_tmdb_id}):
        return EpisodeAuditResult(
            ok=False,
            status="conflict",
            summary="不同媒体服务器的 TMDB 映射不一致",
            data=data,
            suggestions=["请先在各媒体服务器中核对该剧的 TMDB Provider ID。"],
        )

    client = TMDBClient()
    try:
        mapping_status = "provider_id"
        details: dict[str, Any]
        if explicit_tmdb_id:
            mapping_status = "explicit_tmdb_id"
            tmdb_id = explicit_tmdb_id
            details = client.detail(tmdb_id, "tv")
        elif mapped_ids:
            tmdb_id = next(iter(mapped_ids))
            details = client.detail(tmdb_id, "tv")
        else:
            tmdb_id, mapping_status, resolved_details = _resolve_unmapped_tmdb_id(
                client, unmapped
            )
            if mapping_status == "ambiguous":
                return EpisodeAuditResult(
                    ok=False,
                    status="ambiguous",
                    summary="TMDB 命中多个同名同年份剧集，暂不做猜测",
                    data=data,
                    suggestions=["请提供明确的 TMDB ID 后重试。"],
                )
            if not tmdb_id or resolved_details is None:
                return EpisodeAuditResult(
                    ok=False,
                    status="unmapped",
                    summary="已读取本地集号，但无法可靠匹配 TMDB 剧集清单",
                    data=data,
                    suggestions=["请提供剧集原名、首播年份或 TMDB ID 后重试。"],
                )
            details = resolved_details

        _bind_unmapped_sources(sources, tmdb_id, details)
        ready = [source for source in sources if source.get("status") == "ready"]
        if not ready:
            return EpisodeAuditResult(
                ok=False,
                status="not_found" if explicit_tmdb_id else "unmapped",
                summary=(
                    "指定的 TMDB 剧集与媒体库中的剧名和年份不一致"
                    if explicit_tmdb_id
                    else "已读取本地集号，但无法可靠匹配 TMDB 剧集清单"
                ),
                data=_base_data(arguments, sources),
                suggestions=["请核对剧集原名、首播年份或 TMDB ID 后重试。"],
            )

        data = _base_data(arguments, sources)
        data["tmdb_id"] = tmdb_id
        data["mapping_status"] = mapping_status
        raw_seasons = details.get("seasons", [])
        if not isinstance(raw_seasons, list):
            raise TypeError("invalid seasons")
        season_numbers = sorted({
            int(item.get("season_number"))
            for item in raw_seasons
            if isinstance(item, dict)
            and not isinstance(item.get("season_number"), bool)
            and str(item.get("season_number", "")).lstrip("-").isdigit()
            and int(item.get("season_number")) > 0
        })
        requested_season = arguments.get("season")
        if requested_season is not None:
            if requested_season not in season_numbers:
                data.update({"expected_aired": 0, "missing_count": 0})
                return EpisodeAuditResult(
                    ok=False,
                    status="not_found",
                    summary=f"TMDB 中没有找到第 {requested_season} 季",
                    data=data,
                )
            season_numbers = [requested_season]
        seasons_truncated = len(season_numbers) > _MAX_TMDB_SEASONS
        season_numbers = season_numbers[:_MAX_TMDB_SEASONS]

        as_of = date.fromisoformat(arguments["as_of"])
        expected: set[tuple[int, int]] = set()
        episode_airing: dict[tuple[int, int], bool | None] = {}
        future_count = 0
        unknown_air_date = 0
        remote_count = 0
        remote_truncated = False
        mapping_context: dict[str, Any] = {}
        for season_number in season_numbers:
            payload = client.tv_season_detail(tmdb_id, season_number)
            episodes = payload.get("episodes", [])
            if not isinstance(episodes, list):
                raise TypeError("invalid episodes")
            if requested_season == season_number:
                mapping_context = {
                    "detail": {
                        "name": str(details.get("name") or ""),
                        "original_name": str(details.get("original_name") or ""),
                        "seasons": [
                            {
                                "season_number": item.get("season_number"),
                                "episode_count": item.get("episode_count"),
                            }
                            for item in raw_seasons
                            if isinstance(item, dict)
                        ]
                    },
                    "season_detail": {
                        "season_number": payload.get("season_number", season_number),
                        "episodes": [
                            {
                                "episode_number": item.get("episode_number"),
                                "air_date": item.get("air_date"),
                            }
                            for item in episodes
                            if isinstance(item, dict)
                        ],
                    },
                }
            for item in episodes:
                if not isinstance(item, dict):
                    continue
                if remote_count >= _MAX_TMDB_EPISODES:
                    remote_truncated = True
                    break
                remote_count += 1
                try:
                    episode_number = int(item.get("episode_number") or 0)
                except (TypeError, ValueError):
                    continue
                if episode_number <= 0:
                    continue
                aired = _parse_air_date(item.get("air_date"))
                episode_airing[(season_number, episode_number)] = None if aired is None else aired <= as_of
                if aired is None:
                    unknown_air_date += 1
                elif aired <= as_of:
                    expected.add((season_number, episode_number))
                else:
                    future_count += 1
            if remote_truncated:
                break
    except ProviderNotConfigured as exc:
        data["metadata_error"] = {"provider": "tmdb", "code": exc.code}
        return EpisodeAuditResult(
            ok=False,
            status="not_configured",
            summary="TMDB 未配置，无法判断已播集数",
            data=data,
            suggestions=["请先在设置中配置 TMDB API Key。"],
        )
    except (ProviderError, TypeError, ValueError) as exc:
        data["metadata_error"] = {"provider": "tmdb", "code": exc.code if isinstance(exc, ProviderError) else "invalid_data"}
        logger.warning("TMDB 剧集审计失败 type=%s", type(exc).__name__)
        return EpisodeAuditResult(
            ok=False,
            status="unavailable",
            summary="TMDB 暂时不可用，无法完成剧集审计",
            data=data,
            suggestions=["请稍后重试或检查 TMDB 连通性。"],
            error="TMDB 元数据暂时不可用。",
        )
    finally:
        close_tmdb_client(client)

    local = _local_episodes(arguments, ready)
    missing = sorted(expected - local)
    sample = [{"season": season, "episode": episode} for season, episode in missing]
    latest_aired = max(expected) if expected else None
    data.update({
        "latest_aired": dict(zip(("season", "episode"), latest_aired)) if latest_aired else None,
        "title": str(details.get("name") or (ready[0].get("selected") or {}).get("name") or arguments["query"]),
        "expected_aired": len(expected),
        "missing_count": len(missing),
        "missing_sample": sample,
        "missing_sample_truncated": False,
        "episodes": [
            {"season": season, "episode": episode,
             "aired": episode_airing.get((season, episode)), "local": (season, episode) in local}
            for season, episode in sorted(episode_airing.keys() | local)
        ],
        "future_episode_count": future_count,
        "unknown_air_date_count": unknown_air_date,
        "ignored_specials": sum(int(source.get("ignored_specials", 0) or 0) for source in ready),
        "ignored_unknown_local": sum(int(source.get("ignored_unknown", 0) or 0) for source in ready),
    })
    target_episode = arguments.get("target_episode")
    target_season = arguments.get("season")
    if isinstance(target_season, int) and isinstance(target_episode, int):
        target = (target_season, target_episode)
        data.update({
            "target_episode": target_episode,
            "target_aired": target in expected,
            "target_local": target in local,
            "target_missing": target in missing,
        })

    statuses = {str(source.get("status") or "unavailable") for source in sources}
    incomplete = (
        seasons_truncated
        or bool(missing and data["ignored_unknown_local"])
        or remote_truncated
        or any(source.get("truncated") for source in sources)
        or "unavailable" in statuses
        or "unmapped" in statuses
    )
    if incomplete:
        return _with_episode_mapping_context(
            EpisodeAuditResult(
                ok=False,
                status="inconclusive",
                summary="审计数据不完整，当前结果仅供参考",
                data=data,
                suggestions=["存在不可用、未映射、截断或无法编号的本地记录，请修复后重新审计。"],
            ),
            mapping_context,
        )
    if missing:
        return _with_episode_mapping_context(
            EpisodeAuditResult(
                ok=True,
                status="updates_available",
                summary=f"发现 {len(missing)} 集已播但本地尚未收录",
                data=data,
                suggestions=["可按缺失集号继续搜索资源；当前工具不会自动下载。"],
            ),
            mapping_context,
        )
    return _with_episode_mapping_context(
        EpisodeAuditResult(
            ok=True,
            status="up_to_date",
            summary=("已知播出日期与有效编号记录内暂无缺集，仍有未确定记录"
                     if unknown_air_date or data["ignored_unknown_local"] else "截至指定日期，已播普通剧集均已收录"),
            data=data,
            suggestions=(["存在播出日期未知或本地缺少有效集号的条目，不能据此判断全部最新或全集齐全。"]
                         if unknown_air_date or data["ignored_unknown_local"] else []),
        ),
        mapping_context,
    )


def _audit_cache_key(arguments: dict[str, Any]) -> tuple[Any, ...]:
    return (
        media_server_config_key(),
        tuple(config.get_many(("TMDB_API_KEY", "TMDB_API_URL", "PROXY_URL")).values()),
        str(arguments.get("query") or "").casefold(),
        str(arguments.get("tmdb_id") or ""),
        str(arguments.get("library_name") or "").casefold(),
        arguments.get("season"),
        arguments.get("target_episode"),
        str(arguments.get("as_of") or ""),
    )


def audit_series_episodes(arguments: dict[str, Any], *, refresh: bool = False) -> EpisodeAuditResult:
    """默认复用短期审计；显式刷新撤销旧查询发布权，不承诺并发刷新合并。"""
    arguments = normalize_episode_audit_arguments(arguments)
    if not isinstance(refresh, bool):
        raise ValueError("refresh 必须是布尔值")
    if refresh:
        invalidate_episode_audit_cache(arguments)
    key = _audit_cache_key(arguments)
    while True:
        now = time.monotonic()
        with _cache_lock:
            cached = _cache.get(key)
            if cached and cached[0] > now:
                return deepcopy(cached[1])
            event = _inflight.get(key)
            if event is None:
                event = threading.Event()
                _inflight[key] = event
                owner = True
            else:
                owner = False
        if owner:
            break
        if not event.wait(timeout=30):
            return EpisodeAuditResult(
                ok=False,
                status="unavailable",
                summary="相同审计任务仍在执行，请稍后重试",
                data=_base_data(arguments),
            )

    try:
        result = _audit_uncached(arguments)
        with _cache_lock:
            # 强制刷新可撤销旧在途查询的缓存发布权；旧查询仅返回给原调用者。
            if _inflight.get(key) is event:
                _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, deepcopy(result))
            if len(_cache) > _CACHE_MAX_ENTRIES:
                oldest = min(_cache, key=lambda item: _cache[item][0])
                _cache.pop(oldest, None)
        return result
    finally:
        with _cache_lock:
            if _inflight.get(key) is event:
                _inflight.pop(key, None)
            event.set()


def invalidate_episode_audit_cache(arguments: dict[str, Any]) -> None:
    """仅失效目标审计缓存并撤销旧查询发布权，供实时库存核对复用。"""
    arguments = normalize_episode_audit_arguments(arguments)
    key = _audit_cache_key(arguments)
    with _cache_lock:
        _cache.pop(key, None)
        previous = _inflight.pop(key, None)
        if previous is not None:
            previous.set()


def reset_episode_audit_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()
        for event in _inflight.values():
            event.set()
        _inflight.clear()


class _RequestBudgetExceeded(RuntimeError):
    pass


def _consume_request_budget(budget: dict[str, int]) -> None:
    if budget["remaining"] <= 0:
        raise _RequestBudgetExceeded("TMDB request budget exhausted")
    budget["remaining"] -= 1


def _safe_sources(sources: list[dict]) -> list[dict[str, Any]]:
    return [
        {
            "server_type": str(source.get("server_type") or ""),
            "server_name": str(source.get("server_name") or "媒体服务器"),
            "status": str(source.get("status") or "unavailable"),
            "series_total": int(source.get("series_total", 0) or 0),
            "series_enumerated": int(source.get("series_enumerated", 0) or 0),
            "local_series_count": len(source.get("series") or []),
            "mapped_series_count": sum(
                1
                for item in source.get("series") or []
                if isinstance(item, dict) and _normalized_tmdb_id(item.get("tmdb_id"))
            ),
            "unmapped_count": int(source.get("unmapped_count", 0) or 0),
            "truncated": bool(source.get("truncated")),
            "catalog_truncated": bool(
                source.get("catalog_truncated", source.get("truncated"))
            ),
            "batch_remaining": bool(source.get("batch_remaining")),
            "next_tmdb_id": str(source.get("next_tmdb_id") or ""),
            "deadline_exhausted": bool(source.get("deadline_exhausted")),
        }
        for source in sources
    ]


def _source_inventory_counts(
    sources: list[dict],
    safe_sources: list[dict[str, Any]],
) -> tuple[int, int]:
    """返回媒体服务器已枚举的剧集数与已读取的本地普通集数。"""
    local_series_count = sum(
        max(0, int(source.get("series_enumerated", 0) or 0))
        for source in safe_sources
    )
    local_episode_count = 0
    for source in sources:
        for item in source.get("series") or []:
            if not isinstance(item, dict):
                continue
            try:
                local_episode_count += max(0, int(item.get("local_total", 0) or 0))
            except (TypeError, ValueError):
                continue
    return local_series_count, local_episode_count


def _normalized_tmdb_id(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized.isascii()
        or not normalized.isdigit()
        or not 1 <= len(normalized) <= 10
    ):
        return ""
    normalized = str(int(normalized))
    return "" if normalized == "0" else normalized


def _series_identity(item: dict[str, Any]) -> tuple[str, str]:
    return (
        _normalize_identity_title(item.get("name")),
        _identity_year(item.get("year")),
    )


def _details_match_series(item: dict[str, Any], details: dict[str, Any]) -> bool:
    title, year = _series_identity(item)
    return bool(title and year and _tmdb_year(details) == year and title in _tmdb_titles(details))


def _resolve_unmapped_series(
    sources: list[dict],
    client: TMDBClient,
    *,
    deadline_at: float,
    request_budget: dict[str, int],
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """仅以唯一且严格匹配的标题+首播年份补全本批本地剧集映射。"""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    known_ids: dict[tuple[str, str], set[str]] = {}
    invalid_identity_count = 0
    for source in sources:
        for item in source.get("series") or []:
            if not isinstance(item, dict):
                continue
            identity = _series_identity(item)
            tmdb_id = _normalized_tmdb_id(item.get("tmdb_id"))
            if tmdb_id:
                item["tmdb_id"] = tmdb_id
                if all(identity):
                    known_ids.setdefault(identity, set()).add(tmdb_id)
                continue
            if not all(identity):
                invalid_identity_count += 1
                continue
            buckets.setdefault(identity, []).append(item)

    stats = {
        "attempted": sum(len(items) for items in buckets.values()),
        "resolved": 0,
        "ambiguous": 0,
        "unmatched": invalid_identity_count,
    }
    original_unmapped_counts = {
        id(source): int(source.get("unmapped_count", 0) or 0)
        for source in sources
    }
    details_cache: dict[str, dict[str, Any]] = {}
    bucket_items = list(buckets.items())
    for bucket_index, (identity, items) in enumerate(bucket_items):
        if time.monotonic() >= deadline_at:
            stats["unmatched"] += sum(
                len(pending_items)
                for _pending_identity, pending_items in bucket_items[bucket_index:]
            )
            break
        try:
            candidate_ids = set(known_ids.get(identity, set()))
            if len(candidate_ids) > 1:
                stats["ambiguous"] += len(items)
                continue
            if candidate_ids:
                tmdb_id = next(iter(candidate_ids))
            else:
                _consume_request_budget(request_budget)
                title = str(items[0].get("name") or "").strip()
                year = identity[1]
                matches = [
                    item
                    for item in client.search(
                        title,
                        year,
                        "tv",
                        deadline_at=deadline_at,
                        retries=0,
                    )
                    if _tmdb_year(item) == year and identity[0] in _tmdb_titles(item)
                ]
                unique_ids = {
                    tmdb_id
                    for item in matches
                    if (tmdb_id := _normalized_tmdb_id(item.get("id")))
                }
                if not unique_ids:
                    stats["unmatched"] += len(items)
                    continue
                if len(unique_ids) > 1:
                    stats["ambiguous"] += len(items)
                    continue
                tmdb_id = next(iter(unique_ids))

            details = details_cache.get(tmdb_id)
            if details is None:
                _consume_request_budget(request_budget)
                details = client.detail(
                    tmdb_id,
                    "tv",
                    deadline_at=deadline_at,
                    retries=0,
                )
                details_cache[tmdb_id] = details
            if not all(_details_match_series(item, details) for item in items):
                stats["unmatched"] += len(items)
                continue
            for item in items:
                item["tmdb_id"] = tmdb_id
                item["mapping_status"] = "title_year_fallback"
            stats["resolved"] += len(items)
        except _RequestBudgetExceeded:
            stats["unmatched"] += sum(
                len(pending_items)
                for _pending_identity, pending_items in bucket_items[bucket_index:]
            )
            break

    for source in sources:
        remaining_items = sum(
            1
            for item in source.get("series") or []
            if isinstance(item, dict) and not _normalized_tmdb_id(item.get("tmdb_id"))
        )
        resolved_items = sum(
            1
            for item in source.get("series") or []
            if isinstance(item, dict)
            and item.get("mapping_status") == "title_year_fallback"
        )
        source["unmapped_count"] = max(
            remaining_items,
            original_unmapped_counts[id(source)] - resolved_items,
        )
    return stats, details_cache


def _series_groups(sources: list[dict]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for source in sources:
        source_key = str(source.get("server_type") or source.get("server_name") or "media")
        for item in source.get("series") or []:
            if not isinstance(item, dict):
                continue
            tmdb_id = _normalized_tmdb_id(item.get("tmdb_id"))
            if not tmdb_id:
                continue
            group = groups.setdefault(tmdb_id, {
                "tmdb_id": tmdb_id,
                "names": [],
                "episodes": set(),
                "sources": set(),
                "local_truncated": False,
                "ignored_specials": 0,
                "ignored_unknown": 0,
            })
            name = str(item.get("name") or "").strip()
            if name and name not in group["names"]:
                group["names"].append(name[:120])
            group["sources"].add(source_key)
            for episode in item.get("episodes") or []:
                if (
                    isinstance(episode, (list, tuple))
                    and len(episode) == 2
                    and all(isinstance(value, int) and not isinstance(value, bool) for value in episode)
                    and episode[0] > 0
                    and episode[1] > 0
                ):
                    group["episodes"].add((episode[0], episode[1]))
            group["local_truncated"] = group["local_truncated"] or bool(item.get("truncated"))
            group["ignored_specials"] += int(item.get("ignored_specials", 0) or 0)
            group["ignored_unknown"] += int(item.get("ignored_unknown", 0) or 0)
    return groups


def _tmdb_snapshot(
    client: TMDBClient,
    tmdb_id: str,
    *,
    as_of: date,
    deadline_at: float,
    request_budget: dict[str, int],
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if details is None:
        _consume_request_budget(request_budget)
        details = client.detail(
            tmdb_id,
            "tv",
            deadline_at=deadline_at,
            retries=0,
        )
    if "seasons" not in details:
        raise ValueError("missing seasons")
    raw_seasons = details["seasons"]
    if not isinstance(raw_seasons, list) or not raw_seasons:
        raise ValueError("invalid seasons")
    season_numbers = sorted({
        int(item.get("season_number"))
        for item in raw_seasons
        if isinstance(item, dict)
        and not isinstance(item.get("season_number"), bool)
        and str(item.get("season_number", "")).lstrip("-").isdigit()
        and int(item.get("season_number")) > 0
    })
    if not season_numbers:
        raise ValueError("missing regular seasons")
    truncated = len(season_numbers) > _MAX_TMDB_SEASONS
    expected: set[tuple[int, int]] = set()
    future_count = 0
    unknown_air_date_count = 0
    remote_count = 0
    deadline_exhausted = False

    for season_number in season_numbers[:_MAX_TMDB_SEASONS]:
        deadline_exhausted = time.monotonic() >= deadline_at
        if deadline_exhausted or remote_count >= _MAX_TMDB_EPISODES:
            truncated = True
            break
        _consume_request_budget(request_budget)
        payload = client.tv_season_detail(
            tmdb_id,
            season_number,
            deadline_at=deadline_at,
            retries=0,
        )
        if "episodes" not in payload:
            raise ValueError("missing episodes")
        episodes = payload["episodes"]
        if not isinstance(episodes, list) or not episodes:
            raise ValueError("invalid episodes")
        for item in episodes:
            if not isinstance(item, dict):
                continue
            if remote_count >= _MAX_TMDB_EPISODES:
                truncated = True
                break
            remote_count += 1
            try:
                episode_number = int(item.get("episode_number") or 0)
            except (TypeError, ValueError):
                continue
            if episode_number <= 0:
                continue
            aired = _parse_air_date(item.get("air_date"))
            if aired is None:
                unknown_air_date_count += 1
            elif aired <= as_of:
                expected.add((season_number, episode_number))
            else:
                future_count += 1
    return {
        "title": str(details.get("name") or "").strip()[:120],
        "expected": expected,
        "future_episode_count": future_count,
        "unknown_air_date_count": unknown_air_date_count,
        "truncated": truncated,
        "deadline_exhausted": deadline_exhausted,
    }


def _audit_library_episodes_with_client(
    arguments: dict[str, Any],
    *,
    client: TMDBClient,
    after_tmdb_id: str = "",
    resumable: bool = False,
) -> EpisodeAuditResult:
    deadline_at = time.monotonic() + _DEADLINE_SECONDS
    inspect_arguments: dict[str, Any] = {
        "max_series": arguments["max_series"],
        "max_episodes": 2000,
        "deadline_at": deadline_at,
    }
    if resumable:
        inspect_arguments.update({
            "after_tmdb_id": after_tmdb_id,
            "scan_all": True,
        })
    sources = inspect_library_series_sources(**inspect_arguments)
    request_budget = {"remaining": _MAX_TMDB_REQUESTS}
    mapping_stats = {"attempted": 0, "resolved": 0, "ambiguous": 0, "unmatched": 0}
    prefetched_details: dict[str, dict[str, Any]] = {}
    if not resumable and sources:
        try:
            mapping_stats, prefetched_details = _resolve_unmapped_series(
                sources,
                client,
                deadline_at=deadline_at,
                request_budget=request_budget,
            )
        except ProviderNotConfigured:
            safe_sources = _safe_sources(sources)
            local_series_count, local_episode_count = _source_inventory_counts(
                sources, safe_sources
            )
            data = {
                "as_of": arguments["as_of"],
                "max_series": arguments["max_series"],
                "local_series_count": local_series_count,
                "local_episode_count": local_episode_count,
                "comparison_eligible_count": 0,
                "checked_series_count": 0,
                "mapped_series_count": 0,
                "unmapped_series_count": sum(
                    item["unmapped_count"] for item in safe_sources
                ),
                "mapping_fallback": mapping_stats,
                "sources": safe_sources,
            }
            return EpisodeAuditResult(
                ok=False,
                status="not_configured",
                summary="TMDB 未配置，无法判断媒体库应有的已播集数",
                data=data,
                suggestions=["请先在设置中配置 TMDB API Key。"],
            )
        except (ProviderError, ValueError) as exc:
            logger.warning("TMDB 全库映射补全失败 type=%s", type(exc).__name__)
            safe_sources = _safe_sources(sources)
            local_series_count, local_episode_count = _source_inventory_counts(
                sources, safe_sources
            )
            data = {
                "as_of": arguments["as_of"],
                "max_series": arguments["max_series"],
                "local_series_count": local_series_count,
                "local_episode_count": local_episode_count,
                "comparison_eligible_count": 0,
                "checked_series_count": 0,
                "mapped_series_count": 0,
                "unmapped_series_count": sum(
                    item["unmapped_count"] for item in safe_sources
                ),
                "mapping_fallback": mapping_stats,
                "sources": safe_sources,
            }
            return EpisodeAuditResult(
                ok=False,
                status="unavailable",
                summary="已读取本地剧集，但 TMDB 暂时不可用，无法完成全库缺集比较",
                data=data,
                suggestions=["请稍后重试或检查 TMDB 连通性。"],
                error="TMDB 元数据暂时不可用。",
            )

    safe_sources = _safe_sources(sources)
    groups = _series_groups(sources)
    local_series_count, local_episode_count = _source_inventory_counts(
        sources, safe_sources
    )
    unmapped_count = sum(item["unmapped_count"] for item in safe_sources)
    source_incomplete = any(
        item["status"] != "ready" or item["truncated"] or item["deadline_exhausted"]
        for item in safe_sources
    )
    source_resume_blocked = any(
        item["catalog_truncated"]
        or item["status"] == "unavailable"
        or (item["status"] != "ready" and not item["batch_remaining"])
        for item in safe_sources
    )
    source_batch_remaining = any(item["batch_remaining"] for item in safe_sources)
    source_boundary_tmdb_ids = sorted(
        {
            int(item["next_tmdb_id"])
            for item in safe_sources
            if item["batch_remaining"]
            and item["next_tmdb_id"].isascii()
            and item["next_tmdb_id"].isdigit()
            and int(item["next_tmdb_id"]) > int(after_tmdb_id or 0)
        }
    )
    earliest_source_boundary = (
        source_boundary_tmdb_ids[0] if source_boundary_tmdb_ids else None
    )
    deadline_exhausted = any(item["deadline_exhausted"] for item in safe_sources)
    data: dict[str, Any] = {
        "as_of": arguments["as_of"],
        "max_series": arguments["max_series"],
        "local_series_count": local_series_count,
        "local_episode_count": local_episode_count,
        "comparison_eligible_count": len(groups),
        "checked_series_count": 0,
        "mapped_series_count": len(groups),
        "unmapped_series_count": unmapped_count,
        "updates_available_count": 0,
        "up_to_date_count": 0,
        "inconclusive_count": 0,
        "missing_episode_count": 0,
        "unknown_air_date_count": 0,
        "findings": [],
        "findings_truncated": False,
        "deadline_exhausted": deadline_exhausted,
        "tmdb_request_budget": _MAX_TMDB_REQUESTS,
        "tmdb_requests_used": _MAX_TMDB_REQUESTS - request_budget["remaining"],
        "request_budget_exhausted": request_budget["remaining"] <= 0,
        "mapping_fallback": mapping_stats,
        "sources": safe_sources,
        "continuation_pending": False,
        "last_processed_tmdb_id": after_tmdb_id,
        "stalled_tmdb_id": "",
        "total_group_count": len(groups),
    }
    if not sources:
        return EpisodeAuditResult(
            ok=False,
            status="not_configured",
            summary="没有可巡检的媒体服务器",
            data=data,
            suggestions=["请先完整配置并启用 Jellyfin 或 Emby。"],
        )
    if all(item["status"] == "unavailable" for item in safe_sources):
        return EpisodeAuditResult(
            ok=False,
            status="unavailable",
            summary="媒体服务器暂时不可用，无法进行全库巡检",
            data=data,
            suggestions=["请检查 Jellyfin / Emby 连通性后重试。"],
            error="媒体服务器暂时不可用。",
        )
    if not groups:
        next_tmdb_id = (
            str(earliest_source_boundary) if earliest_source_boundary is not None else ""
        )
        if resumable and source_batch_remaining and not source_resume_blocked and next_tmdb_id:
            data["continuation_pending"] = True
            data["stalled_tmdb_id"] = next_tmdb_id
            return EpisodeAuditResult(
                ok=True,
                status="inconclusive",
                summary="媒体目录读取达到本批时限，将从当前剧集继续",
                data=data,
                suggestions=["后台巡检会自动重试当前剧集，不会从头重复。"],
            )
        if local_series_count > 0 and unmapped_count > 0:
            return EpisodeAuditResult(
                ok=False,
                status="inconclusive",
                summary=(
                    f"已从 Jellyfin / Emby 读取 {local_series_count} 部本地剧集，"
                    f"但其中 {unmapped_count} 部缺少可靠 TMDB 映射，暂时无法判断缺集"
                ),
                data=data,
                suggestions=[
                    "请在媒体服务器中补充 TMDB Provider ID，或提供准确剧名与首播年份后再检查。"
                ],
            )
        if local_series_count > 0 or source_incomplete:
            return EpisodeAuditResult(
                ok=False,
                status="inconclusive",
                summary=(
                    f"已从 Jellyfin / Emby 读取 {local_series_count} 部本地剧集，"
                    "但媒体目录或集号读取不完整，暂时无法判断缺集"
                    if local_series_count > 0
                    else "媒体服务器目录读取未完成，尚无法确认是否存在可巡检剧集"
                ),
                data=data,
                suggestions=["请检查媒体服务器连通性与媒体库权限后重新巡检。"],
            )
        return EpisodeAuditResult(
            ok=True,
            status="empty",
            summary="已连接 Jellyfin / Emby，但媒体库中没有读取到剧集条目",
            data=data,
            suggestions=["请确认剧集媒体库已启用、当前账号可见，并已完成媒体库扫描。"],
        )

    as_of = date.fromisoformat(arguments["as_of"])
    findings: list[dict[str, Any]] = []
    incomplete = source_incomplete or unmapped_count > 0 or request_budget["remaining"] <= 0
    all_tmdb_ids = sorted(groups, key=lambda value: int(value))
    cursor_value = int(after_tmdb_id) if after_tmdb_id else 0
    selected_tmdb_ids = [
        tmdb_id
        for tmdb_id in all_tmdb_ids
        if int(tmdb_id) > cursor_value
        and (
            earliest_source_boundary is None
            or int(tmdb_id) < earliest_source_boundary
        )
    ][: arguments["max_series"]]
    if resumable and earliest_source_boundary is not None:
        data["stalled_tmdb_id"] = str(earliest_source_boundary)
    audit_failure_types: dict[str, int] = {}
    for tmdb_id in selected_tmdb_ids:
        if time.monotonic() >= deadline_at:
            deadline_exhausted = True
            incomplete = True
            if resumable:
                data["stalled_tmdb_id"] = tmdb_id
            break
        group = groups[tmdb_id]
        try:
            remote = _tmdb_snapshot(
                client,
                tmdb_id,
                as_of=as_of,
                deadline_at=deadline_at,
                request_budget=request_budget,
                details=prefetched_details.get(tmdb_id),
            )
        except ProviderNotConfigured:
            data["deadline_exhausted"] = deadline_exhausted
            return EpisodeAuditResult(
                ok=False,
                status="not_configured",
                summary="TMDB 未配置，无法进行全库缺集巡检",
                data=data,
                suggestions=["请先在设置中配置 TMDB API Key。"],
            )
        except _RequestBudgetExceeded:
            data["request_budget_exhausted"] = True
            incomplete = True
            if resumable:
                data["stalled_tmdb_id"] = tmdb_id
            break
        except (ProviderError, ValueError) as exc:
            error_type = type(exc).__name__
            audit_failure_types[error_type] = audit_failure_types.get(error_type, 0) + 1
            incomplete = True
            if time.monotonic() >= deadline_at:
                deadline_exhausted = True
                if resumable:
                    data["stalled_tmdb_id"] = tmdb_id
                break
            data["checked_series_count"] += 1
            data["inconclusive_count"] += 1
            data["last_processed_tmdb_id"] = tmdb_id
            continue

        data["checked_series_count"] += 1
        data["last_processed_tmdb_id"] = tmdb_id
        local = set(group["episodes"])
        missing = sorted(remote["expected"] - local)
        data["unknown_air_date_count"] += remote["unknown_air_date_count"]
        group_incomplete = bool(
            group["local_truncated"]
            or bool(missing and group["ignored_unknown"])
            or remote["truncated"]
            or remote["unknown_air_date_count"]
        )
        if group_incomplete:
            data["inconclusive_count"] += 1
            incomplete = True
        elif missing:
            data["updates_available_count"] += 1
        else:
            data["up_to_date_count"] += 1
        if remote["deadline_exhausted"]:
            deadline_exhausted = True
            incomplete = True

        if missing:
            data["missing_episode_count"] += len(missing)
        if missing or group_incomplete:
            findings.append({
                "title": remote["title"] or (group["names"][0] if group["names"] else "未知剧集"),
                "tmdb_id": tmdb_id,
                "source_count": len(group["sources"]),
                "expected_aired": len(remote["expected"]),
                "local_episode_count": len(local),
                "ignored_unknown_local": group["ignored_unknown"],
                "missing_count": len(missing),
                "missing_sample": [
                    {"season": season, "episode": episode}
                    for season, episode in missing[:_MAX_MISSING_SAMPLE]
                ],
                "missing_sample_truncated": len(missing) > _MAX_MISSING_SAMPLE,
                "future_episode_count": remote["future_episode_count"],
                "unknown_air_date_count": remote["unknown_air_date_count"],
                "status": "inconclusive" if group_incomplete else "updates_available",
            })
        if deadline_exhausted:
            break

    if audit_failure_types:
        logger.warning(
            "TMDB 全库剧集巡检存在失败 total=%s types=%s",
            sum(audit_failure_types.values()),
            ",".join(
                f"{name}:{count}" for name, count in sorted(audit_failure_types.items())
            ),
        )
    data["deadline_exhausted"] = deadline_exhausted
    data["tmdb_requests_used"] = _MAX_TMDB_REQUESTS - request_budget["remaining"]
    data["findings"] = findings[:_MAX_FINDINGS]
    data["findings_truncated"] = len(findings) > _MAX_FINDINGS
    processed_cursor = int(data["last_processed_tmdb_id"] or 0)
    remaining_count = sum(int(tmdb_id) > processed_cursor for tmdb_id in all_tmdb_ids)
    if (
        resumable
        and (remaining_count > 0 or source_batch_remaining)
        and not source_resume_blocked
    ):
        data["continuation_pending"] = True
        remaining_label = (
            f"至少 {remaining_count} 部"
            if source_batch_remaining
            else f"{remaining_count} 部"
        )
        return EpisodeAuditResult(
            ok=True,
            status="inconclusive",
            summary=(
                f"全库巡检本批已核对 {data['checked_series_count']} 部，"
                f"剩余 {remaining_label}将在下一批继续"
            ),
            data=data,
            suggestions=["后台巡检会自动从当前进度继续，不会从头重复。"],
        )
    if not resumable and data["checked_series_count"] < len(groups):
        incomplete = True
        data["inconclusive_count"] += len(groups) - data["checked_series_count"]

    if incomplete:
        return EpisodeAuditResult(
            ok=False,
            status="inconclusive",
            summary=(
                f"全库巡检未完整结束；已核对 {data['checked_series_count']} 部，"
                f"确认 {data['updates_available_count']} 部存在缺集"
            ),
            data=data,
            suggestions=["请补齐本地季集编号，或修复未映射、截断和不可用的数据源后重新巡检。"],
        )
    if data["updates_available_count"]:
        return EpisodeAuditResult(
            ok=True,
            status="updates_available",
            summary=(
                f"已核对 {data['checked_series_count']} 部剧集，"
                f"发现 {data['updates_available_count']} 部共缺 {data['missing_episode_count']} 集"
            ),
            data=data,
            suggestions=["可针对巡检结果中的剧集继续执行单剧缺集资源搜索。"],
        )
    return EpisodeAuditResult(
        ok=True,
        status="up_to_date",
        summary=f"已核对 {data['checked_series_count']} 部剧集，暂未发现已播缺集",
        data=data,
    )


def _audit_library_episodes(
    arguments: dict[str, Any],
    *,
    after_tmdb_id: str = "",
    resumable: bool = False,
) -> EpisodeAuditResult:
    extra = set(arguments) - {"as_of", "max_series"}
    if extra:
        raise ValueError(f"不支持的审计参数：{', '.join(sorted(extra))}")
    max_series = arguments.get("max_series", 50)
    if type(max_series) is not int or not 1 <= max_series <= 100:
        raise ValueError("max_series 必须是 1 到 100 的整数")
    arguments = {
        "as_of": normalize_audit_date(arguments.get("as_of")),
        "max_series": max_series,
    }
    client = TMDBClient()
    try:
        return _audit_library_episodes_with_client(
            arguments,
            client=client,
            after_tmdb_id=after_tmdb_id,
            resumable=resumable,
        )
    finally:
        close_tmdb_client(client)


def audit_library_episodes(arguments: dict[str, Any]) -> EpisodeAuditResult:
    """有界单次巡检；默认本地当日、最多 50 个媒体组，保留 30 秒完整性边界。"""
    return _audit_library_episodes(arguments)


def audit_library_episodes_batch(arguments: dict[str, Any]) -> EpisodeAuditResult:
    """可续跑批次；默认本地当日、50 个媒体组，after_tmdb_id 是已处理的组。"""
    public_arguments = dict(arguments)
    cursor = str(public_arguments.pop("after_tmdb_id", "") or "").strip()
    if cursor and (not cursor.isascii() or not cursor.isdigit() or len(cursor) > 10):
        raise ValueError("全库巡检游标无效")
    return _audit_library_episodes(
        public_arguments,
        after_tmdb_id=str(int(cursor)) if cursor else "",
        resumable=True,
    )


# 更新查询共用同一审计与媒体客户端；并发限制保护上游，不截断作品列表。
_TITLE_NOISE_RE = re.compile(r"[\W_]+", re.UNICODE)
_MOVIE_SEARCH_LIMIT = 50

def _normalized_title(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _TITLE_NOISE_RE.sub("", text)

def _safe_movie_year(value: object) -> str:
    year = str(value or "").strip()
    return year if re.fullmatch(r"(?:18|19|20|21|22)\d{2}", year) else ""

def _check_movie_updates(arguments: dict[str, Any], *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """只做可证明的本地存在性核对，不把资源站候选误报成版本更新。"""
    query = arguments["query"]
    normalized_query = _normalized_title(query)
    tmdb_id = arguments.get("tmdb_id", "")
    try:
        raw_sources = search_media_servers(
            tmdb_id or query, limit=_MOVIE_SEARCH_LIMIT, media_type="movie",
            search_by="tmdb" if tmdb_id else "title", cancelled=cancelled,
        )
    except Exception:  # noqa: BLE001 - Provider异常只标记不可用，不能伪装成库内没有
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
        server_name = str(source.get("server_name") or "媒体服务器")
        source_items: list[dict[str, str]] = []
        raw_items = source.get("items", []) if not source_unavailable else []
        source_truncated = not source_unavailable and source.get("has_more") is not False
        if source_truncated:
            truncated_sources += 1
        if not source_unavailable:
            for item in raw_items:
                if str(getattr(item, "type", "") or "").strip().casefold() != "movie":
                    continue
                title = str(getattr(item, "name", "") or getattr(item, "display_name", "") or "")
                if not title:
                    continue
                public = {
                    "title": title,
                    "year": _safe_movie_year(getattr(item, "year", "")),
                    "server_type": server_type,
                    "server_name": server_name,
                }
                exact = bool(tmdb_id or normalized_query and _normalized_title(title) == normalized_query)
                public["match"] = "tmdb_id" if tmdb_id else "exact_title" if exact else "possible_title"
                source_items.append(public)
                (exact_matches if exact else possible_matches).append(public)
        source_records.append({
            "server_type": server_type,
            "server_name": server_name,
            "status": "unavailable" if source_unavailable else "ready",
            "truncated": source_truncated,
            "items": source_items,
        })

    sources = [{**source, "returned": len(source["items"])} for source in source_records]

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
        summary = f"媒体库中找到 {exact_count} 个{'同一 TMDB 身份' if tmdb_id else '同名'}电影条目；版本更新仍需人工核对"
        suggestions = ["可继续搜索资源站候选；结果只代表可用资源，不代表一定优于本地版本。"]
    elif possible_count:
        ok = True
        status = "review_required"
        local_match_status = "possible"
        summary = f"找到 {possible_count} 个可能匹配的电影条目，标题未能精确核验"
        suggestions = ["请先核对片名和年份，再搜索资源站候选。"]
        if truncated_sources:
            suggestions.append(f"有 {truncated_sources} 个媒体服务器的搜索范围未完整核验。")
    elif truncated_sources:
        ok = True
        status = "review_required"
        local_match_status = "indeterminate"
        summary = "媒体服务器搜索范围未完整核验，无法证明电影未入库"
        suggestions = ["需完成媒体库核对后再确认是否已入库。"]
    else:
        ok = True
        status = "not_found"
        local_match_status = "not_found"
        summary = "可用媒体库中未找到" + ("指定 TMDB 身份" if tmdb_id else "同名") + "电影条目"
        suggestions = ["可继续搜索资源站资源；未入库不等同于资源站一定有可用版本。"]
    if unavailable and available:
        status = "partial"
        suggestions.append(f"另有 {unavailable} 个媒体服务器暂时不可用，结论可能不完整。")


    return dict(
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
            "matches_truncated": 0,
            "sources": sources,
            "comparison": {
                "available": False,
                "reason": "媒体服务器当前未提供可可靠比较的文件版本、发行版、分辨率与编码基线。",
            },
        },
        checked_at=_now(),
        suggestions=suggestions,
    )


def normalize_library_update_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    extra = set(arguments) - {"query", "queries", "media_type", "tmdb_id", "season", "as_of", "refresh"}
    if extra:
        raise ValueError(f"不支持的参数：{', '.join(sorted(extra))}")
    refresh = arguments.get("refresh", True)
    if not isinstance(refresh, bool):
        raise ValueError("refresh 必须是布尔值")
    if "queries" in arguments:
        if set(arguments) & {"query", "tmdb_id", "season"}:
            raise ValueError("queries 不能同时指定 query、tmdb_id 或 season；歧义项请单独核对")
        queries = arguments["queries"]
        if not isinstance(queries, list) or not queries:
            raise ValueError("queries 必须是非空剧名或片名列表")
        shared = {key: value for key, value in arguments.items() if key != "queries"}
        items = [normalize_library_update_arguments({**shared, "query": query}) for query in queries]
        distinct = {}
        for item in items:
            distinct.setdefault(item["query"].casefold(), item["query"])
        return {"queries": list(distinct.values()), "media_type": items[0]["media_type"],
                "as_of": items[0]["as_of"], "refresh": refresh}
    media_type = arguments.get("media_type", "auto")
    if not isinstance(media_type, str) or media_type not in {"auto", "tv", "movie"}:
        raise ValueError("media_type 必须是 auto、tv 或 movie")
    normalized = normalize_episode_audit_arguments(
        {key: value for key, value in arguments.items() if key not in {"media_type", "refresh"}}
    )
    if media_type == "movie" and normalized.get("season") is not None:
        raise ValueError("电影更新核对不支持 season 参数")
    return {**normalized, "media_type": media_type, "refresh": refresh}


def _check_library_update(arguments: dict[str, Any], cancelled: Callable[[], bool] | None) -> dict[str, Any]:
    media_type = arguments["media_type"]
    if cancelled is not None and cancelled():
        return dict(ok=False, status="cancelled", summary="本项已停止，未读取库存", checked_at=_now(),
                    data={"query": arguments["query"], "media_type": media_type})
    if media_type == "movie":
        return _check_movie_updates(arguments, cancelled=cancelled)
    audit_arguments = {key: value for key, value in arguments.items() if key not in {"media_type", "refresh"}}
    result = asdict(audit_series_episodes(audit_arguments, refresh=arguments["refresh"]))
    result["data"].update(media_type=media_type,
                          check_definition="aired_normal_episodes_missing_from_enabled_media_servers")
    if media_type == "auto" and result["status"] == "not_found":
        result.update(ok=False, status="cannot_determine", summary="未找到可审计剧集，无法可靠判断目标是否有更新",
                      suggestions=["如果目标是电视剧，请补充剧集类型或 TMDB ID；电影可明确类型后核对本地存在性。"])
    return result


def _batch_update_item(arguments: dict[str, Any], cancelled: Callable[[], bool] | None) -> dict[str, Any]:
    try:
        result: dict[str, Any] = _check_library_update(arguments, cancelled)
    except Exception as exc:  # 单项读取失败不丢弃同批已核实的作品。
        logger.warning("批量更新核对单项失败 type=%s", type(exc).__name__)
        result = dict(ok=False, status="unavailable", summary="本部查询失败，暂时无法判断更新", data={}, checked_at=_now())
    data = result["data"]
    row = {**data, "query": arguments["query"], "title": str(data.get("title") or arguments["query"]),
           **{key: value for key, value in result.items() if key != "data"}}
    for key in ("expected_aired", "local_episode_count", "missing_count", "latest_local", "latest_aired",
                "unknown_air_date_count", "ignored_unknown_local"):
        row.setdefault(key, None)
    row.setdefault("tmdb_id", "")
    row.setdefault("missing_sample", [])
    row.setdefault("missing_sample_truncated", False)
    row.setdefault("sources", [])
    return row


def _library_updates_result(arguments: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    updates = sum(row["status"] == "updates_available" for row in rows)
    current = sum(row["status"] == "up_to_date" for row in rows)
    uncertain = len(rows) - updates - current
    completed = sum(row["status"] not in {"pending", "cancelled"} for row in rows)
    checked_at = min((row["checked_at"] for row in rows if "checked_at" in row), default=_now())
    return dict(
        ok=any(row["ok"] for row in rows), status="partial" if uncertain else "success",
        summary=f"已核对 {completed} 部：{updates} 部存在已播缺集，{current} 部无已播缺集，{uncertain} 部待确认",
        checked_at=_now(),
        data={"as_of": arguments["as_of"], "checked_at": checked_at, "refresh": arguments["refresh"], "resource_search_status": "not_checked",
              "check_definition": "library_inventory_vs_tmdb_not_resource_release_availability",
              "counts": {"requested": len(rows), "updates_available": updates, "up_to_date": current, "uncertain": uncertain},
              "items": list(rows)},
        suggestions=["保留未知日期、未编号与来源失败事实，不能宣称全部最新或全集齐全；本次没有检索资源站，也不证明官方平台实时进度。"],
    )


def check_library_updates(
    arguments: dict[str, Any], *, cancelled: Callable[[], bool] | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """公共单项/批量更新查询；默认刷新，保留完整事实，取消后的排队项不再读取。"""
    arguments = normalize_library_update_arguments(arguments)
    if "queries" not in arguments:
        return _check_library_update(arguments, cancelled)
    shared = {key: value for key, value in arguments.items() if key != "queries"}
    items = [{**shared, "query": query} for query in arguments["queries"]]
    rows = [{"query": item["query"], "ok": False, "status": "pending"} for item in items]
    with ThreadPoolExecutor(max_workers=min(3, len(items)), thread_name_prefix="library-updates") as pool:
        futures = {pool.submit(_batch_update_item, item, cancelled): index for index, item in enumerate(items)}
        for future in as_completed(futures):
            rows[futures[future]] = future.result()
            if checkpoint is not None and not (cancelled is not None and cancelled()):
                checkpoint(_library_updates_result(arguments, rows))
    return _library_updates_result(arguments, rows)
