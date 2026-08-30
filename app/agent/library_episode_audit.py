"""全库剧集巡检：以媒体服务器本地集号为库存，和 TMDB 已播清单比较。"""
from __future__ import annotations

from datetime import date, datetime
import time
import unicodedata
from typing import Any

from app.agent.models import Evidence, ToolResult
from app.clients.tmdb import TMDBClient, close_tmdb_client
from app.discovery.models import ProviderError, ProviderNotConfigured
from app.logger import get_logger
from app.services import inspect_library_series_sources

logger = get_logger(__name__)

_DEADLINE_SECONDS = 30.0
_MAX_TMDB_SEASONS = 24
_MAX_TMDB_EPISODES = 2000
_MAX_TMDB_REQUESTS = 200
_MAX_FINDINGS = 20
_MAX_MISSING_SAMPLE = 20


class _RequestBudgetExceeded(RuntimeError):
    pass


def _consume_request_budget(budget: dict[str, int]) -> None:
    if budget["remaining"] <= 0:
        raise _RequestBudgetExceeded("TMDB request budget exhausted")
    budget["remaining"] -= 1


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
        evidence=[Evidence(
            "media_servers+tmdb",
            "先读取媒体服务器本地剧集与集号；优先使用 ProviderIds.Tmdb，必要时仅以唯一的标题+首播年份严格补全映射，再比较 TMDB 截止日期前已播普通集。",
            _now(),
        )],
        suggestions=suggestions or [],
        error=error,
    )


def _parse_air_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


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
        title
        for title in (
            _normalize_identity_title(item.get("name")),
            _normalize_identity_title(item.get("original_name")),
        )
        if title
    }


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
        if time.monotonic() >= deadline_at:
            deadline_exhausted = True
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
                episode_number = int(item.get("episode_number"))
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
        if remote_count >= _MAX_TMDB_EPISODES:
            break

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
) -> ToolResult:
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
            return _result(
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
            return _result(
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
        return _result(
            ok=False,
            status="not_configured",
            summary="没有可巡检的媒体服务器",
            data=data,
            suggestions=["请先完整配置并启用 Jellyfin 或 Emby。"],
        )
    if all(item["status"] == "unavailable" for item in safe_sources):
        return _result(
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
            return _result(
                ok=True,
                status="inconclusive",
                summary="媒体目录读取达到本批时限，将从当前剧集继续",
                data=data,
                suggestions=["后台巡检会自动重试当前剧集，不会从头重复。"],
            )
        if local_series_count > 0 and unmapped_count > 0:
            return _result(
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
            return _result(
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
        return _result(
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
            return _result(
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
        return _result(
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
        return _result(
            ok=False,
            status="inconclusive",
            summary=(
                f"全库巡检未完整结束；已核对 {data['checked_series_count']} 部，"
                f"确认 {data['updates_available_count']} 部存在缺集"
            ),
            data=data,
            suggestions=["请修复未映射、截断或不可用的数据源后重新巡检。"],
        )
    if data["updates_available_count"]:
        return _result(
            ok=True,
            status="updates_available",
            summary=(
                f"已核对 {data['checked_series_count']} 部剧集，"
                f"发现 {data['updates_available_count']} 部共缺 {data['missing_episode_count']} 集"
            ),
            data=data,
            suggestions=["可针对巡检结果中的剧集继续执行单剧缺集资源搜索。"],
        )
    return _result(
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
) -> ToolResult:
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


def audit_library_episodes(arguments: dict[str, Any]) -> ToolResult:
    """公开的一次性巡检工具；保持原有 30 秒完整性语义。"""
    return _audit_library_episodes(arguments)


def audit_library_episodes_batch(arguments: dict[str, Any]) -> ToolResult:
    """后台调度专用的可续跑批次；游标只使用已处理的 TMDB ID。"""
    cursor = str(arguments.get("after_tmdb_id") or "").strip()
    if cursor and (not cursor.isascii() or not cursor.isdigit() or len(cursor) > 10):
        raise ValueError("全库巡检游标无效")
    public_arguments = {
        "as_of": arguments["as_of"],
        "max_series": arguments["max_series"],
    }
    return _audit_library_episodes(
        public_arguments,
        after_tmdb_id=str(int(cursor)) if cursor else "",
        resumable=True,
    )
