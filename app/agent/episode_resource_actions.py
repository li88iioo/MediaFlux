"""已播缺集的定向资源搜索：先审计，再复用多站安全搜索。"""

from __future__ import annotations

import time
import unicodedata
from datetime import date, datetime
from typing import Any

from app.agent.episode_audit import audit_series_episodes
from app.agent.errors import AgentToolError
from app.agent.indexer_actions import (
    normalize_search_sites,
    search_resources,
    validate_enabled_search_sites,
)
from app.agent.indexer_actions import search_arguments as indexer_search_arguments
from app.agent.models import Evidence, ToolResult
from app.agent.resource_recommendation import rank_episode_search


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _reject_extra(arguments: dict[str, Any], allowed: set[str]) -> None:
    extra = set(arguments) - allowed
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")


def _visible_text(value: Any, *, name: str, maximum: int = 120) -> str:
    if not isinstance(value, str):
        raise AgentToolError(f"{name} 必须是字符串")
    text = unicodedata.normalize("NFKC", value).strip()
    if (
        not text
        or len(text) > maximum
        or any(unicodedata.category(char).startswith("C") for char in text)
    ):
        raise AgentToolError(f"{name} 必须是 1 到 {maximum} 个可见字符")
    return text


def _positive_int(value: Any, *, name: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise AgentToolError(f"{name} 必须是 1 到 {maximum} 的整数")
    return value


def _optional_visible_text(value: Any, *, name: str, maximum: int) -> str:
    if value in (None, ""):
        return ""
    return _visible_text(value, name=name, maximum=maximum)


def missing_episode_resource_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(
        arguments,
        {
            "query",
            "tmdb_id",
            "season",
            "episode",
            "as_of",
            "sites",
            "limit",
            "library_name",
        },
    )
    query = _visible_text(arguments.get("query"), name="query")
    library_name = _optional_visible_text(
        arguments.get("library_name", ""),
        name="library_name",
        maximum=80,
    )

    tmdb_id = arguments.get("tmdb_id", "")
    if not isinstance(tmdb_id, str):
        raise AgentToolError("tmdb_id 必须是字符串")
    tmdb_id = tmdb_id.strip()
    if tmdb_id and (
        not tmdb_id.isascii() or not tmdb_id.isdigit() or len(tmdb_id) > 10
    ):
        raise AgentToolError("tmdb_id 必须是 1 到 10 位数字")

    season = _positive_int(arguments.get("season"), name="season", maximum=100)
    episode = _positive_int(arguments.get("episode"), name="episode", maximum=1000)

    as_of = arguments.get("as_of", date.today().isoformat())
    if not isinstance(as_of, str):
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期")
    try:
        parsed_as_of = date.fromisoformat(as_of.strip())
    except ValueError as exc:
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期") from exc
    if parsed_as_of > date.today():
        raise AgentToolError("as_of 不能晚于今天")

    sites = normalize_search_sites(arguments.get("sites", []))
    validate_enabled_search_sites(sites)

    limit = arguments.get("limit", 20)
    _positive_int(limit, name="limit", maximum=50)
    normalized = {
        "query": query,
        "tmdb_id": tmdb_id,
        "season": season,
        "episode": episode,
        "as_of": parsed_as_of.isoformat(),
        "sites": sites,
        "limit": limit,
    }
    if library_name:
        normalized["library_name"] = library_name
    return normalized


def missing_season_resource_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(
        arguments,
        {
            "query",
            "tmdb_id",
            "season",
            "as_of",
            "sites",
            "max_episodes",
            "limit_per_episode",
            "library_name",
        },
    )
    query = _visible_text(arguments.get("query"), name="query")
    library_name = _optional_visible_text(
        arguments.get("library_name", ""),
        name="library_name",
        maximum=80,
    )

    tmdb_id = arguments.get("tmdb_id", "")
    if not isinstance(tmdb_id, str):
        raise AgentToolError("tmdb_id 必须是字符串")
    tmdb_id = tmdb_id.strip()
    if "tmdb_id" in arguments and not tmdb_id:
        raise AgentToolError("tmdb_id 必须是 1 到 10 位数字")
    if tmdb_id and (
        not tmdb_id.isascii() or not tmdb_id.isdigit() or len(tmdb_id) > 10
    ):
        raise AgentToolError("tmdb_id 必须是 1 到 10 位数字")

    season = _positive_int(arguments.get("season"), name="season", maximum=100)
    as_of = arguments.get("as_of", date.today().isoformat())
    if not isinstance(as_of, str):
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期")
    try:
        parsed_as_of = date.fromisoformat(as_of.strip())
    except ValueError as exc:
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期") from exc
    if parsed_as_of > date.today():
        raise AgentToolError("as_of 不能晚于今天")

    sites = normalize_search_sites(arguments.get("sites", []))
    validate_enabled_search_sites(sites)
    max_episodes = arguments.get("max_episodes", 3)
    limit_per_episode = arguments.get("limit_per_episode", 8)
    _positive_int(max_episodes, name="max_episodes", maximum=3)
    _positive_int(limit_per_episode, name="limit_per_episode", maximum=10)
    normalized = {
        "query": query,
        "tmdb_id": tmdb_id,
        "season": season,
        "as_of": parsed_as_of.isoformat(),
        "sites": sites,
        "max_episodes": max_episodes,
        "limit_per_episode": limit_per_episode,
    }
    if library_name:
        normalized["library_name"] = library_name
    return normalized


def _bounded_count(value: Any, maximum: int = 100_000) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return 0


def _verification(
    arguments: dict[str, Any], audit: ToolResult, *, verified: bool
) -> dict[str, Any]:
    data = audit.data if isinstance(audit.data, dict) else {}
    title = " ".join(str(data.get("title") or arguments["query"]).split())[:120]
    tmdb_id = str(data.get("tmdb_id") or arguments.get("tmdb_id") or "")
    if not tmdb_id.isascii() or not tmdb_id.isdigit():
        tmdb_id = ""
    return {
        "title": title,
        "tmdb_id": tmdb_id[:10],
        "season": arguments["season"],
        "episode": arguments["episode"],
        "as_of": arguments["as_of"],
        "audit_status": audit.status,
        "library_name": str(
            data.get("library_name") or arguments.get("library_name") or ""
        )[:80],
        "missing_count": _bounded_count(data.get("missing_count")),
        "verified_missing": verified,
    }


def _season_verification(
    arguments: dict[str, Any], audit: ToolResult
) -> dict[str, Any]:
    data = audit.data if isinstance(audit.data, dict) else {}
    title = " ".join(str(data.get("title") or arguments["query"]).split())[:120]
    tmdb_id = str(data.get("tmdb_id") or arguments.get("tmdb_id") or "")
    if not tmdb_id.isascii() or not tmdb_id.isdigit():
        tmdb_id = ""
    return {
        "title": title,
        "tmdb_id": tmdb_id[:10],
        "season": arguments["season"],
        "as_of": arguments["as_of"],
        "audit_status": audit.status,
        "library_name": str(
            data.get("library_name") or arguments.get("library_name") or ""
        )[:80],
        "missing_count": _bounded_count(data.get("missing_count")),
        "verified_missing": False,
    }


def _episode_search_arguments(arguments: dict[str, Any], title: str) -> dict[str, Any]:
    season = arguments["season"]
    episode = arguments["episode"]
    code = f"S{season:02d}E{episode:02d}"
    alternate = f"{season}x{episode:02d}"
    chinese = f"第{season}季 第{episode}集"
    base = " ".join(str(title or arguments["query"]).split()) or arguments["query"]
    input_title = arguments["query"]

    def scoped(value: str, suffix: str) -> str:
        prefix = value[: max(1, 119 - len(suffix))].rstrip()
        return f"{prefix} {suffix}"[:120]

    if input_title.casefold() != base.casefold():
        # 下游每个站点最多尝试三条查询：主标题 + 两个有序别名。
        aliases = [scoped(base, alternate), scoped(input_title, code)]
    else:
        aliases = [scoped(base, alternate), scoped(base, chinese)]
    return indexer_search_arguments(
        {
            "title": scoped(base, code),
            "aliases": aliases,
            "media_type": "tv",
            "sites": arguments["sites"],
            "limit": arguments["limit"],
        }
    )


def search_missing_episode_resources(arguments: dict[str, Any]) -> ToolResult:
    audit_arguments: dict[str, Any] = {
        "query": arguments["query"],
        "tmdb_id": arguments["tmdb_id"],
        "season": arguments["season"],
        "target_episode": arguments["episode"],
        "as_of": arguments["as_of"],
    }
    if arguments.get("library_name"):
        audit_arguments["library_name"] = arguments["library_name"]
    audit = audit_series_episodes(audit_arguments)
    target = {"season": arguments["season"], "episode": arguments["episode"]}
    verification = _verification(arguments, audit, verified=False)

    if not audit.ok or audit.status != "updates_available":
        if audit.status == "up_to_date":
            return ToolResult(
                False,
                "not_missing",
                f"第 {arguments['season']} 季第 {arguments['episode']} 集未被确认缺失",
                data={"verification": verification},
                evidence=list(audit.evidence),
                suggestions=["可重新核对季集编号，或直接进行普通资源搜索。"],
            )
        return ToolResult(
            False,
            audit.status,
            "缺集状态尚未可靠确认，因此未搜索资源",
            data={"verification": verification},
            evidence=list(audit.evidence),
            suggestions=list(audit.suggestions),
            error=audit.error,
        )

    audit_data = audit.data if isinstance(audit.data, dict) else {}
    target_missing = audit_data.get("target_missing")
    if target_missing is not True:
        # 新版审计会直接比较指定季集，不再依赖最多 100 条的展示样本。
        # 保留样本回退仅用于兼容旧结果或测试替身。
        raw_sample = audit_data.get("missing_sample", [])
        sample = [item for item in raw_sample if isinstance(item, dict)]
        if target_missing is None and target in sample:
            pass
        elif target_missing is None and bool(
            audit_data.get("missing_sample_truncated")
        ):
            return ToolResult(
                False,
                "inconclusive",
                "缺集清单已截断，无法可靠确认指定集",
                data={"verification": verification},
                evidence=list(audit.evidence),
                suggestions=["请缩小到明确季度后重新搜索。"],
            )
        else:
            return ToolResult(
                False,
                "not_missing",
                f"第 {arguments['season']} 季第 {arguments['episode']} 集未被确认缺失",
                data={"verification": verification},
                evidence=list(audit.evidence),
                suggestions=["可重新核对季集编号，或直接进行普通资源搜索。"],
            )

    verification["verified_missing"] = True
    search_args = _episode_search_arguments(arguments, verification["title"])
    searched = search_resources(search_args)
    search_data = searched.data if isinstance(searched.data, dict) else {}
    ranked_search = rank_episode_search(
        search_data,
        season=arguments["season"],
        episode=arguments["episode"],
    )
    data = {
        "verification": verification,
        "search": ranked_search,
    }
    if searched.ok:
        summary = (
            f"已确认 {search_args['title'].rsplit(' ', 1)[-1]} 缺失；{searched.summary}"
        )
    else:
        summary = f"已确认指定集缺失，但{searched.summary}"
    return ToolResult(
        searched.ok,
        searched.status,
        summary,
        data=data,
        evidence=list(audit.evidence)
        + list(searched.evidence)
        + [
            Evidence(
                "agent_verification",
                "资源站搜索仅在媒体库与 TMDB 审计确认目标为已播缺集后执行；未自动提交下载。",
                _now(),
            )
        ],
        suggestions=list(searched.suggestions)
        + (
            [
                "已按季集匹配、可提交性、发布规格和站点活跃度生成只读推荐；确认后才会提交下载。"
            ]
            if ranked_search.get("recommendation", {}).get("selected")
            else []
        ),
        error=searched.error,
    )


_MISSING_SEASON_SEARCH_DEADLINE_SECONDS = 30.0


def search_missing_season_resources(arguments: dict[str, Any]) -> ToolResult:
    deadline_at = time.monotonic() + _MISSING_SEASON_SEARCH_DEADLINE_SECONDS
    audit_arguments: dict[str, Any] = {
        "query": arguments["query"],
        "tmdb_id": arguments["tmdb_id"],
        "season": arguments["season"],
        "as_of": arguments["as_of"],
    }
    if arguments.get("library_name"):
        audit_arguments["library_name"] = arguments["library_name"]
    audit = audit_series_episodes(audit_arguments)
    verification = _season_verification(arguments, audit)
    audit_data = audit.data if isinstance(audit.data, dict) else {}

    if not audit.ok or audit.status != "updates_available":
        if audit.status == "up_to_date":
            return ToolResult(
                False,
                "not_missing",
                f"第 {arguments['season']} 季没有确认缺集",
                data={"verification": verification, "episodes": []},
                evidence=list(audit.evidence),
                suggestions=["可重新核对季度，或直接进行普通资源搜索。"],
            )
        return ToolResult(
            False,
            audit.status,
            "该季缺集状态尚未可靠确认，因此未搜索资源",
            data={"verification": verification, "episodes": []},
            evidence=list(audit.evidence),
            suggestions=list(audit.suggestions),
            error=audit.error,
        )

    if bool(audit_data.get("missing_sample_truncated")):
        return ToolResult(
            False,
            "inconclusive",
            "该季缺集清单已截断，因此未执行批量资源搜索",
            data={"verification": verification, "episodes": []},
            evidence=list(audit.evidence),
            suggestions=["请缩小审计范围或逐集搜索明确的季集编号。"],
        )

    missing: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for item in audit_data.get("missing_sample", []):
        if not isinstance(item, dict):
            continue
        season = item.get("season")
        episode = item.get("episode")
        if (
            isinstance(season, bool)
            or not isinstance(season, int)
            or season != arguments["season"]
            or isinstance(episode, bool)
            or not isinstance(episode, int)
            or not 1 <= episode <= 1000
            or (season, episode) in seen
        ):
            continue
        seen.add((season, episode))
        missing.append({"season": season, "episode": episode})
    missing.sort(key=lambda item: item["episode"])
    if not missing:
        return ToolResult(
            False,
            "not_missing",
            f"第 {arguments['season']} 季没有确认缺集",
            data={"verification": verification, "episodes": []},
            evidence=list(audit.evidence),
            suggestions=["可重新核对季度，或直接进行普通资源搜索。"],
        )

    verification["verified_missing"] = True
    verification["missing_count"] = len(missing)
    selected = missing[: arguments["max_episodes"]]
    episodes: list[dict[str, Any]] = []
    evidence = list(audit.evidence)
    total_candidates = 0
    failed = 0
    completed = 0
    suggestions: list[str] = []
    deadline_exhausted = False
    for target in selected:
        remaining_seconds = deadline_at - time.monotonic()
        if remaining_seconds <= 0:
            deadline_exhausted = True
            break
        per_episode_arguments = {
            **arguments,
            "episode": target["episode"],
            "limit": arguments["limit_per_episode"],
        }
        search_args = _episode_search_arguments(
            per_episode_arguments, verification["title"]
        )
        searched = search_resources(search_args, timeout_seconds=remaining_seconds)
        raw_search_data = searched.data if isinstance(searched.data, dict) else {}
        search_data = rank_episode_search(
            raw_search_data,
            season=target["season"],
            episode=target["episode"],
        )
        items = search_data.get("items", [])
        item_count = len(items) if isinstance(items, list) else 0
        total_candidates += item_count
        completed += int(bool(searched.ok))
        failed += int(not searched.ok)
        episode_label = f"S{target['season']:02d}E{target['episode']:02d}"
        episodes.append(
            {
                "season": target["season"],
                "episode": target["episode"],
                "episode_label": episode_label,
                "ok": bool(searched.ok),
                "status": searched.status,
                "summary": searched.summary[:160],
                "search": search_data,
            }
        )
        evidence.extend(searched.evidence)
        if time.monotonic() >= deadline_at:
            deadline_exhausted = True
            break

    remaining = max(0, len(missing) - len(episodes))
    if deadline_exhausted:
        suggestions.append("批量搜索已达到本次耗时上限；可再次按季度检索其余缺集。")
    if failed or remaining:
        status = "partial"
    elif total_candidates:
        status = "success"
    else:
        status = "empty"
    ok = bool(completed)
    summary = f"已核验第 {arguments['season']} 季 {len(missing)} 个缺集，并搜索其中 {len(episodes)} 集"
    if total_candidates:
        summary += f"，找到 {total_candidates} 项候选资源"
    elif completed:
        summary += "，暂未找到候选资源"
    if remaining:
        summary += f"；其余 {remaining} 集未在本批次检索"

    if total_candidates:
        suggestions.append("可逐项选择候选资源，并预检推送到 qBittorrent 或光鸭。")
    if remaining:
        suggestions.append(f"本次最多处理 3 集；剩余 {remaining} 集可再次按季度检索。")
    if failed:
        suggestions.append("部分集的资源站搜索未完成；这不代表资源站中没有相关资源。")
    return ToolResult(
        ok,
        status,
        summary,
        data={
            "verification": verification,
            "missing_total": len(missing),
            "processed": len(episodes),
            "remaining": remaining,
            "failed": failed,
            "truncated": bool(remaining),
            "episodes": episodes,
        },
        evidence=evidence[:16]
        + [
            Evidence(
                "agent_verification",
                "批量资源站搜索仅处理本次媒体库与 TMDB 审计确认的已播缺集；未自动提交下载。",
                _now(),
            )
        ],
        suggestions=suggestions,
        error="" if ok else "该批次资源站搜索未完成。",
    )
