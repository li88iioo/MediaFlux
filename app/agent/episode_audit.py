"""剧集完整性审计：只比较可靠映射后的本地集号与 TMDB 已播集号。"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import threading
import unicodedata
import time
from typing import Any

from app.agent.models import Evidence, ToolResult
from app.clients.tmdb import TMDBClient, close_tmdb_client
from app.discovery.models import ProviderNotConfigured, ProviderError
from app.logger import get_logger
from app.services import inspect_series_episode_sources

logger = get_logger(__name__)

_CACHE_TTL_SECONDS = 120
_CACHE_MAX_ENTRIES = 64
_MAX_TMDB_SEASONS = 24
_MAX_TMDB_EPISODES = 2000
_MAX_MISSING_SAMPLE = 100
_MAX_RESOURCE_FOLLOWUPS = 12
_cache: dict[tuple[Any, ...], tuple[float, ToolResult]] = {}
_inflight: dict[tuple[Any, ...], threading.Event] = {}
_cache_lock = threading.Lock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _result(
    *,
    ok: bool,
    status: str,
    summary: str,
    data: dict[str, Any],
    suggestions: list[str] | None = None,
    error: str = "",
) -> ToolResult:
    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data=data,
        evidence=[
            Evidence(
                "media_servers+tmdb",
                "本地集号直接读取 Jellyfin/Emby；优先使用 ProviderIds.Tmdb，缺失时仅以严格标题和年份匹配 TMDB，再比较截止日期前已播普通剧集。",
                _now(),
            )
        ],
        suggestions=suggestions or [],
        error=error,
    )


def _base_data(arguments: dict[str, Any], sources: list[dict] | None = None) -> dict[str, Any]:
    return {
        "query": arguments["query"],
        "tmdb_id": arguments.get("tmdb_id", ""),
        "season": arguments.get("season"),
        "target_episode": arguments.get("target_episode"),
        "library_name": str(arguments.get("library_name") or ""),
        "as_of": arguments["as_of"],
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


def _resource_followups(data: dict[str, Any]) -> list[dict[str, Any]]:
    query = str(data.get("query") or "").strip()
    as_of = str(data.get("as_of") or "").strip()
    tmdb_id = str(data.get("tmdb_id") or "").strip()
    library_name = str(data.get("library_name") or "").strip()
    if not query or len(query) > 120 or not as_of:
        return []
    if tmdb_id and (not tmdb_id.isascii() or not tmdb_id.isdigit() or len(tmdb_id) > 10):
        tmdb_id = ""

    followups: list[dict[str, Any]] = []
    target_season = data.get("season")
    target_episode = data.get("target_episode")
    if (
        data.get("target_missing") is True
        and isinstance(target_season, int)
        and not isinstance(target_season, bool)
        and 1 <= target_season <= 100
        and isinstance(target_episode, int)
        and not isinstance(target_episode, bool)
        and 1 <= target_episode <= 1000
    ):
        label = f"S{target_season:02d}E{target_episode:02d}"
        arguments: dict[str, Any] = {
            "query": query,
            "season": target_season,
            "episode": target_episode,
            "as_of": as_of,
        }
        if tmdb_id:
            arguments["tmdb_id"] = tmdb_id
        if library_name:
            arguments["library_name"] = library_name
        followups.append({
            "tool": "library.search_missing_episode_resources",
            "label": f"搜索 {label} 资源",
            "episode_label": label,
            "arguments": arguments,
        })

    for item in data.get("missing_sample", []):
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
        label = f"S{season:02d}E{episode:02d}"
        if any(item.get("episode_label") == label for item in followups):
            continue
        arguments: dict[str, Any] = {
            "query": query,
            "season": season,
            "episode": episode,
            "as_of": as_of,
        }
        if tmdb_id:
            arguments["tmdb_id"] = tmdb_id
        if library_name:
            arguments["library_name"] = library_name
        followups.append({
            "tool": "library.search_missing_episode_resources",
            "label": f"搜索 {label} 资源",
            "episode_label": label,
            "arguments": arguments,
        })
        if len(followups) >= _MAX_RESOURCE_FOLLOWUPS:
            break
    return followups


def _normalize_identity_title(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _identity_year(value: object) -> str:
    year = str(value or "").strip()
    return year if len(year) == 4 and year.isascii() and year.isdigit() else ""


def _tmdb_year(item: dict[str, Any]) -> str:
    value = str(item.get("first_air_date") or "").strip()
    return value[:4] if len(value) >= 4 and value[:4].isdigit() else ""


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
    selected = source.get("selected") or {}
    return (
        _normalize_identity_title(selected.get("name")),
        _identity_year(selected.get("year")),
    )


def _details_match_source(source: dict[str, Any], details: dict[str, Any]) -> bool:
    title, year = _source_identity(source)
    if not title or not year:
        return False
    detail_year = _tmdb_year(details)
    return bool(detail_year == year and title in _tmdb_titles(details))


def _bind_unmapped_sources(
    sources: list[dict], tmdb_id: str, details: dict[str, Any]
) -> None:
    for source in sources:
        if source.get("status") != "unmapped" or not _details_match_source(source, details):
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


def _audit_uncached(arguments: dict[str, Any]) -> ToolResult:
    sources = inspect_series_episode_sources(
        arguments["query"],
        tmdb_id=arguments.get("tmdb_id", ""),
        max_episodes=2000,
        library_name=str(arguments.get("library_name") or ""),
    )
    data = _base_data(arguments, sources)
    if not sources:
        return _result(
            ok=False,
            status="not_configured",
            summary="没有可审计的媒体服务器",
            data=data,
            suggestions=["请先完整配置并启用 Jellyfin 或 Emby。"],
        )

    statuses = {str(source.get("status") or "unavailable") for source in sources}
    if statuses == {"unavailable"}:
        return _result(
            ok=False,
            status="unavailable",
            summary="媒体服务器暂时不可用，无法完成剧集审计",
            data=data,
            suggestions=["请检查媒体服务器连通性后重试。"],
            error="媒体服务器暂时不可用。",
        )
    if "ambiguous" in statuses:
        return _result(
            ok=False,
            status="ambiguous",
            summary="媒体库命中多个同名剧集，暂不做猜测",
            data=data,
            suggestions=["请提供明确的 TMDB ID 后重试。"],
        )
    if "conflict" in statuses:
        return _result(
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
        return _result(
            ok=False,
            status="ambiguous",
            summary=f"有多个媒体库与「{library_name}」相似，暂时无法确定范围",
            data=data,
            suggestions=["请使用媒体服务器中显示的完整媒体库名称后重试。"],
        )
    if not ready and not unmapped and "library_not_found" in statuses:
        library_name = str(arguments.get("library_name") or "指定媒体库")
        return _result(
            ok=False,
            status="not_found",
            summary=f"没有找到名为「{library_name}」的媒体库",
            data=data,
            suggestions=["请核对媒体库名称，或不指定媒体库以检查全部已配置服务器。"],
        )
    if not ready and not unmapped and "unavailable" in statuses:
        return _result(
            ok=False,
            status="unavailable",
            summary="媒体服务器数据不完整，无法完成剧集审计",
            data=data,
            suggestions=["请检查不可用的媒体服务器后重试。"],
            error="媒体服务器暂时不可用。",
        )
    if not ready and not unmapped:
        return _result(
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
        return _result(
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
                return _result(
                    ok=False,
                    status="ambiguous",
                    summary="TMDB 命中多个同名同年份剧集，暂不做猜测",
                    data=data,
                    suggestions=["请提供明确的 TMDB ID 后重试。"],
                )
            if not tmdb_id or resolved_details is None:
                return _result(
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
            return _result(
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
            raise ValueError("invalid seasons")
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
                data.update({"expected_aired": 0, "local_episode_count": 0, "missing_count": 0})
                return _result(
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
        future_count = 0
        unknown_air_date = 0
        remote_count = 0
        remote_truncated = False
        for season_number in season_numbers:
            payload = client.tv_season_detail(tmdb_id, season_number)
            episodes = payload.get("episodes", [])
            if not isinstance(episodes, list):
                raise ValueError("invalid episodes")
            for item in episodes:
                if not isinstance(item, dict):
                    continue
                if remote_count >= _MAX_TMDB_EPISODES:
                    remote_truncated = True
                    break
                remote_count += 1
                try:
                    episode_number = int(item.get("episode_number"))
                except (TypeError, ValueError):
                    continue
                if episode_number <= 0:
                    continue
                aired = _parse_air_date(item.get("air_date"))
                if aired is None:
                    unknown_air_date += 1
                elif aired <= as_of:
                    expected.add((season_number, episode_number))
                else:
                    future_count += 1
            if remote_truncated:
                break
    except ProviderNotConfigured:
        return _result(
            ok=False,
            status="not_configured",
            summary="TMDB 未配置，无法判断已播集数",
            data=data,
            suggestions=["请先在设置中配置 TMDB API Key。"],
        )
    except (ProviderError, ValueError) as exc:
        logger.warning("TMDB 剧集审计失败 type=%s", type(exc).__name__)
        return _result(
            ok=False,
            status="unavailable",
            summary="TMDB 暂时不可用，无法完成剧集审计",
            data=data,
            suggestions=["请稍后重试或检查 TMDB 连通性。"],
            error="TMDB 元数据暂时不可用。",
        )
    finally:
        close_tmdb_client(client)

    local: set[tuple[int, int]] = set()
    for source in ready:
        local.update(tuple(item) for item in source.get("episodes", []))
    if arguments.get("season") is not None:
        local = {item for item in local if item[0] == arguments["season"]}
    missing = sorted(expected - local)
    sample = [{"season": season, "episode": episode} for season, episode in missing[:_MAX_MISSING_SAMPLE]]
    data.update({
        "title": str(details.get("name") or (ready[0].get("selected") or {}).get("name") or arguments["query"]),
        "expected_aired": len(expected),
        "local_episode_count": len(local),
        "missing_count": len(missing),
        "missing_sample": sample,
        "missing_sample_truncated": len(missing) > len(sample),
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
        or remote_truncated
        or any(source.get("truncated") for source in ready)
        or "unavailable" in statuses
        or "unmapped" in statuses
    )
    if incomplete:
        return _result(
            ok=False,
            status="inconclusive",
            summary="审计数据不完整，当前结果仅供参考",
            data=data,
            suggestions=["存在不可用、未映射或被截断的数据源，请修复后重新审计。"],
        )
    if missing:
        followups = _resource_followups(data)
        if followups:
            data["resource_followups"] = followups
            data["resource_followups_truncated"] = len(sample) > len(followups) or bool(
                data.get("missing_sample_truncated")
            )
        return _result(
            ok=True,
            status="updates_available",
            summary=f"发现 {len(missing)} 集已播但本地尚未收录",
            data=data,
            suggestions=["可按缺失集号继续搜索资源；当前工具不会自动下载。"],
        )
    return _result(
        ok=True,
        status="up_to_date",
        summary="截至指定日期，已播普通剧集均已收录",
        data=data,
    )


def audit_series_episodes(arguments: dict[str, Any]) -> ToolResult:
    key = (
        arguments["query"].casefold(),
        arguments.get("tmdb_id", ""),
        str(arguments.get("library_name") or "").casefold(),
        arguments.get("season"),
        arguments.get("target_episode"),
        arguments["as_of"],
    )
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
            return _result(
                ok=False,
                status="unavailable",
                summary="相同审计任务仍在执行，请稍后重试",
                data=_base_data(arguments),
            )

    try:
        result = _audit_uncached(arguments)
        with _cache_lock:
            _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, deepcopy(result))
            if len(_cache) > _CACHE_MAX_ENTRIES:
                oldest = min(_cache, key=lambda item: _cache[item][0])
                _cache.pop(oldest, None)
        return result
    finally:
        with _cache_lock:
            pending = _inflight.pop(key, None)
            if pending:
                pending.set()


def invalidate_episode_audit_cache(arguments: dict[str, Any]) -> None:
    """在入库核验前仅失效目标审计缓存，避免读取下载前的旧缺集结果。"""
    key = (
        str(arguments.get("query") or "").casefold(),
        str(arguments.get("tmdb_id") or ""),
        str(arguments.get("library_name") or "").casefold(),
        arguments.get("season"),
        arguments.get("target_episode"),
        str(arguments.get("as_of") or ""),
    )
    with _cache_lock:
        _cache.pop(key, None)


def reset_episode_audit_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()
        for event in _inflight.values():
            event.set()
        _inflight.clear()
