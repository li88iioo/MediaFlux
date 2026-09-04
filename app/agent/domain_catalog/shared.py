"""领域目录共享的无副作用参数校验与安全值规范化。"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any

from app.agent.errors import AgentToolError
from app.agent.workspace_actions import _contains_sensitive_text


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now().astimezone().date()


def _reject_extra(arguments: dict[str, Any], allowed: set[str]) -> None:
    extra = set(arguments) - allowed
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")


def _no_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, set())
    return {}


def guangya_organize_status_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"operation_ref"})
    reference = str(arguments.get("operation_ref") or "").strip().upper()
    if reference and not re.fullmatch(r"GY-(?:[0-9A-F]{4}-){7}[0-9A-F]{4}", reference):
        raise AgentToolError("operation_ref 不是有效的光鸭操作编号")
    return {"operation_ref": reference} if reference else {}


def _normalize_search_query(value: str) -> str:
    query = unicodedata.normalize("NFKC", value).strip()
    if (
        not query
        or len(query) > 120
        or any(unicodedata.category(char).startswith("C") for char in query)
    ):
        raise AgentToolError("搜索关键词必须为 1 到 120 个可见字符")
    if _contains_sensitive_text(query):
        raise AgentToolError("搜索关键词疑似包含路径、链接、凭据、哈希或业务标识")
    return query


def _optional_visible_text(value: Any, *, name: str, maximum: int = 80) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise AgentToolError(f"{name} 必须是字符串")
    text = unicodedata.normalize("NFKC", value).strip(" ，。！？?、:：")
    if (
        not text
        or len(text) > maximum
        or any(unicodedata.category(char).startswith("C") for char in text)
    ):
        raise AgentToolError(f"{name} 必须是 1 到 {maximum} 个可见字符")
    if _contains_sensitive_text(text):
        raise AgentToolError(f"{name} 疑似包含路径、链接、凭据、哈希或业务标识")
    return text


def _search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"query", "limit"})
    raw_query = arguments.get("query")
    if not isinstance(raw_query, str):
        raise AgentToolError("query 必须是字符串")
    query = _normalize_search_query(raw_query)
    limit = arguments.get("limit", 8)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise AgentToolError("limit 必须是 1 到 50 的整数")
    return {"query": query, "limit": limit}


def _episode_audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(
        arguments,
        {"query", "tmdb_id", "season", "target_episode", "as_of", "library_name"},
    )
    raw_query = arguments.get("query")
    if not isinstance(raw_query, str):
        raise AgentToolError("query 必须是字符串")
    query = _normalize_search_query(raw_query)

    tmdb_id = arguments.get("tmdb_id", "")
    if not isinstance(tmdb_id, str):
        raise AgentToolError("tmdb_id 必须是字符串")
    tmdb_id = tmdb_id.strip()
    if tmdb_id and (
        not tmdb_id.isascii() or not tmdb_id.isdigit() or not 1 <= len(tmdb_id) <= 10
    ):
        raise AgentToolError("tmdb_id 必须是 1 到 10 位数字")

    season = arguments.get("season")
    if season is not None and (
        isinstance(season, bool)
        or not isinstance(season, int)
        or not 1 <= season <= 100
    ):
        raise AgentToolError("season 必须是 1 到 100 的整数")

    target_episode = arguments.get("target_episode")
    if target_episode is not None and (
        isinstance(target_episode, bool)
        or not isinstance(target_episode, int)
        or not 1 <= target_episode <= 1000
    ):
        raise AgentToolError("target_episode 必须是 1 到 1000 的整数")
    if target_episode is not None and season is None:
        raise AgentToolError("target_episode 必须与 season 一起提供")

    library_name = _optional_visible_text(
        arguments.get("library_name", ""),
        name="library_name",
        maximum=80,
    )

    as_of = arguments.get("as_of", _today().isoformat())
    if not isinstance(as_of, str):
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期")
    try:
        parsed_as_of = date.fromisoformat(as_of.strip())
    except ValueError as exc:
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期") from exc
    if parsed_as_of > _today():
        raise AgentToolError("as_of 不能晚于今天")
    normalized = {
        "query": query,
        "tmdb_id": tmdb_id,
        "season": season,
        "as_of": parsed_as_of.isoformat(),
    }
    if library_name:
        normalized["library_name"] = library_name
    if target_episode is not None:
        normalized["target_episode"] = target_episode
    return normalized


def _library_episode_audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"as_of", "max_series"})
    as_of = arguments.get("as_of", _today().isoformat())
    if not isinstance(as_of, str):
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期")
    try:
        parsed_as_of = date.fromisoformat(as_of.strip())
    except ValueError as exc:
        raise AgentToolError("as_of 必须是 YYYY-MM-DD 日期") from exc
    if parsed_as_of > _today():
        raise AgentToolError("as_of 不能晚于今天")
    max_series = arguments.get("max_series", 50)
    if (
        isinstance(max_series, bool)
        or not isinstance(max_series, int)
        or not 1 <= max_series <= 100
    ):
        raise AgentToolError("max_series 必须是 1 到 100 的整数")
    return {"as_of": parsed_as_of.isoformat(), "max_series": max_series}


def _library_update_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_extra(arguments, {"query", "media_type", "tmdb_id", "season", "as_of"})
    media_type = arguments.get("media_type", "auto")
    if not isinstance(media_type, str) or media_type not in {"auto", "tv", "movie"}:
        raise AgentToolError("media_type 必须是 auto、tv 或 movie")
    normalized = _episode_audit_arguments(
        {key: value for key, value in arguments.items() if key != "media_type"}
    )
    if media_type == "movie" and normalized.get("season") is not None:
        raise AgentToolError("电影更新核对不支持 season 参数")
    normalized["media_type"] = media_type
    return normalized


def _bounded_int(value: Any, *, maximum: int = 1_000_000_000) -> int:
    try:
        return max(0, min(int(value or 0), maximum))
    except (TypeError, ValueError):
        return 0


def _safe_timestamp(value: Any) -> str:
    text = str(value or "").strip()[:32]
    if not text:
        return ""
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return text


def _safe_choice(value: Any, allowed: set[str], default: str = "") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default
