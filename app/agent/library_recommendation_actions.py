"""基于本地媒体库元数据与观看历史的个性化推荐。"""

from __future__ import annotations

import math
import unicodedata
from typing import Any

from app.agent.errors import AgentToolError
from app.agent.media_consumption_actions import select_media_profile
from app.agent.media_preference_policy import owner_media_preferences
from app.agent.models import ToolContext, ToolResult
from app.agent.provider_actions import get_provider_gateway
from app.agent.provider_models import ProviderGatewayError

_ALLOWED_ARGUMENTS = {
    "server",
    "media_type",
    "must_match",
    "prefer",
    "exclude",
    "min_rating",
    "exclude_played",
    "limit",
}


def _visible_text(value: object, *, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        return ""
    return " ".join(normalized.split())[:limit]


def _term_list(value: object, *, name: str, maximum: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise AgentToolError(f"{name} 必须是最多 {maximum} 项的数组")
    result: list[str] = []
    for raw in value:
        term = _visible_text(raw, limit=80)
        alternatives = [item.strip() for item in term.split("|")]
        if not term or any(not item for item in alternatives):
            raise AgentToolError(f"{name} 中包含无效匹配词")
        if term not in result:
            result.append(term)
    return result


def library_recommendation_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _ALLOWED_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    server = str(arguments.get("server") or "auto").strip().casefold()
    if server not in {"auto", "jellyfin", "emby"}:
        raise AgentToolError("server 仅支持 auto、jellyfin 或 emby")
    media_type = str(arguments.get("media_type") or "any").strip().casefold()
    if media_type not in {"any", "movie", "tv"}:
        raise AgentToolError("media_type 仅支持 any、movie 或 tv")
    min_rating = arguments.get("min_rating", 0)
    if isinstance(min_rating, bool) or not isinstance(min_rating, (int, float)):
        raise AgentToolError("min_rating 必须是数字")
    min_rating = float(min_rating)
    if not math.isfinite(min_rating) or not 0 <= min_rating <= 10:
        raise AgentToolError("min_rating 必须在 0 到 10 之间")
    exclude_played = arguments.get("exclude_played", True)
    if not isinstance(exclude_played, bool):
        raise AgentToolError("exclude_played 必须是布尔值")
    limit = arguments.get("limit", 8)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise AgentToolError("limit 必须是 1 到 20 的整数")
    result = {
        "server": server,
        "media_type": media_type,
        "must_match": _term_list(
            arguments.get("must_match"), name="must_match", maximum=6
        ),
        "prefer": _term_list(arguments.get("prefer"), name="prefer", maximum=12),
        "exclude": _term_list(arguments.get("exclude"), name="exclude", maximum=8),
        "min_rating": min_rating,
        "exclude_played": exclude_played,
        "limit": limit,
    }

    for key in ("prefer", "exclude", "min_rating", "exclude_played"):
        if key not in arguments:
            result.pop(key)
    return result


def get_library_recommendations(
    arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    normalized = library_recommendation_arguments(arguments)
    preferences = owner_media_preferences(context.owner)
    for argument, preference in (
        ("prefer", "preferred_genres"), ("exclude", "excluded_genres"),
        ("min_rating", "min_rating"), ("exclude_played", "exclude_played"),
    ):
        normalized.setdefault(argument, preferences[preference])
    profile, failure = select_media_profile(
        normalized,
        context,
        purpose="本地媒体推荐",
    )
    if failure is not None:
        return failure
    assert profile is not None
    provider_arguments = {
        key: value for key, value in normalized.items() if key != "server"
    }
    try:
        return get_provider_gateway().query(
            profile_ref=profile.source,
            operation="media.items.recommend_from_library",
            arguments=provider_arguments,
            context=context,
        )
    except ProviderGatewayError as exc:
        return ToolResult(
            False,
            exc.code,
            f"暂时无法从 {profile.label} 生成本地推荐",
            data={"server": profile.server_type, "items": []},
            error=exc.safe_message,
        )
