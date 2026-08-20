"""Media Agent 的外部影视探索只读工具。"""
from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Any
import unicodedata

from app import config
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.discovery.models import DiscoveryPage, MediaCard, ProviderError
from app.discovery.search import DiscoverySearchResult, get_discovery_search_service
from app.discovery.service import get_discovery_service

_ALLOWED_PROVIDERS = ("tmdb", "douban", "bangumi")
_ALLOWED_ARGUMENTS = {"query", "page", "providers", "limit"}
_RECOMMEND_ARGUMENTS = {"provider", "media_type", "page", "limit"}
_CALENDAR_ARGUMENTS = {"weekday", "page", "limit"}
_RECOMMEND_PROVIDERS = {"tmdb", "douban"}
_RECOMMEND_MEDIA_TYPES = {"movie", "tv"}
_PROVIDER_STATUSES = {"healthy", "degraded", "disabled", "unavailable", "not_configured"}
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")
_ERROR_MESSAGES = {
    "timeout": "数据源请求超时",
    "rate_limited": "数据源请求受限",
    "authentication": "数据源认证失败",
    "not_configured": "数据源未配置",
    "invalid_response": "数据源响应无效",
    "unavailable": "数据源暂不可用",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _visible_text(value: Any, *, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    cleaned = "".join(" " if unicodedata.category(char).startswith("C") else char for char in normalized)
    return " ".join(cleaned.split())[:limit]


def _public_identifier(value: Any, *, limit: int = 180) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()[:limit]
    return normalized if _PUBLIC_ID_RE.fullmatch(normalized) else ""


def _positive_int(value: Any, *, name: str, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentToolError(f"{name} 必须是整数")
    if value < 1 or value > maximum:
        raise AgentToolError(f"{name} 必须在 1 到 {maximum} 之间")
    return value


def search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _ALLOWED_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")

    query = arguments.get("query")
    if not isinstance(query, str):
        raise AgentToolError("query 必须是字符串")
    query = unicodedata.normalize("NFKC", query).strip()
    if (
        not query
        or len(query) > 120
        or any(unicodedata.category(char).startswith("C") for char in query)
    ):
        raise AgentToolError("搜索关键词必须为 1 到 120 个可见字符")

    page = _positive_int(arguments.get("page"), name="page", default=1, maximum=100)
    limit = _positive_int(arguments.get("limit"), name="limit", default=20, maximum=50)

    raw_providers = arguments.get("providers")
    providers: list[str] | None = None
    if raw_providers is not None:
        if not isinstance(raw_providers, list):
            raise AgentToolError("providers 必须是数组")
        if not raw_providers or len(raw_providers) > len(_ALLOWED_PROVIDERS):
            raise AgentToolError("providers 必须选择 1 到 3 个来源")
        providers = []
        for raw in raw_providers:
            if not isinstance(raw, str):
                raise AgentToolError("providers 只能包含字符串")
            name = unicodedata.normalize("NFKC", raw).strip().lower()
            if name not in _ALLOWED_PROVIDERS:
                raise AgentToolError("不支持的搜索来源")
            if name not in providers:
                providers.append(name)
        if not providers:
            raise AgentToolError("至少选择一个搜索来源")

    return {"query": query, "page": page, "providers": providers, "limit": limit}


def recommend_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _RECOMMEND_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")

    raw_provider = arguments.get("provider", "tmdb")
    if not isinstance(raw_provider, str):
        raise AgentToolError("provider 必须是字符串")
    provider = unicodedata.normalize("NFKC", raw_provider).strip().lower()
    if provider not in _RECOMMEND_PROVIDERS:
        raise AgentToolError("推荐来源仅支持 tmdb 或 douban")

    raw_media_type = arguments.get("media_type", "movie")
    if not isinstance(raw_media_type, str):
        raise AgentToolError("media_type 必须是字符串")
    media_type = unicodedata.normalize("NFKC", raw_media_type).strip().lower()
    if media_type not in _RECOMMEND_MEDIA_TYPES:
        raise AgentToolError("media_type 仅支持 movie 或 tv")

    page = _positive_int(arguments.get("page"), name="page", default=1, maximum=100)
    limit = _positive_int(arguments.get("limit"), name="limit", default=10, maximum=20)
    return {
        "provider": provider,
        "media_type": media_type,
        "page": page,
        "limit": limit,
    }


def calendar_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _CALENDAR_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")

    if "weekday" in arguments and arguments["weekday"] is None:
        raise AgentToolError("weekday 必须是整数")
    if "page" in arguments and arguments["page"] is None:
        raise AgentToolError("page 必须是整数")
    if "limit" in arguments and arguments["limit"] is None:
        raise AgentToolError("limit 必须是整数")

    weekday = arguments.get("weekday")
    if weekday is not None:
        if isinstance(weekday, bool) or not isinstance(weekday, int):
            raise AgentToolError("weekday 必须是整数")
        if weekday < 1 or weekday > 7:
            raise AgentToolError("weekday 必须在 1 到 7 之间")

    page = _positive_int(arguments.get("page"), name="page", default=1, maximum=100)
    limit = _positive_int(arguments.get("limit"), name="limit", default=10, maximum=20)
    normalized = {"page": page, "limit": limit}
    if weekday is not None:
        normalized["weekday"] = weekday
    return normalized


def _public_card(card: MediaCard) -> dict[str, Any]:
    rating = card.rating
    if rating is not None and not math.isfinite(rating):
        rating = None
    return {
        "stable_id": _public_identifier(card.stable_id),
        "provider": _public_identifier(card.provider, limit=20),
        "external_id": _public_identifier(card.external_id, limit=120),
        "media_type": _public_identifier(card.media_type, limit=20),
        "title": _visible_text(card.title, limit=240),
        "original_title": _visible_text(card.original_title, limit=240),
        "year": _visible_text(card.year, limit=16),
        "overview": _visible_text(card.overview, limit=500),
        "rating": rating,
        "rating_source": _visible_text(card.rating_source, limit=40),
        "release_date": _visible_text(card.release_date, limit=32),
        "tmdb_id": _public_identifier(card.tmdb_id, limit=32),
        "douban_id": _public_identifier(card.douban_id, limit=64),
        "bangumi_id": _public_identifier(card.bangumi_id, limit=32),
    }


def _safe_retry_after(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(86400, max(0, value))


def _public_error(raw: Any) -> dict[str, Any]:
    item = raw if isinstance(raw, dict) else {}
    retry_after = item.get("retry_after", 0)
    if isinstance(retry_after, bool) or not isinstance(retry_after, int):
        retry_after = 0
    code = _public_identifier(item.get("code"), limit=40) or "unavailable"
    if code not in _ERROR_MESSAGES:
        code = "unavailable"
    return {
        "provider": _public_identifier(item.get("provider"), limit=20),
        "code": code,
        "message": _ERROR_MESSAGES[code],
        "retry_after": _safe_retry_after(retry_after),
    }


def _result_payload(result: DiscoverySearchResult, *, limit: int) -> dict[str, Any]:
    items = [_public_card(card) for card in result.items[:limit] if isinstance(card, MediaCard)]
    return {
        "query": _visible_text(result.query, limit=120),
        "page": result.page,
        "total": len(result.items),
        "returned": len(items),
        "has_more": bool(result.has_more),
        "providers_attempted": [
            _public_identifier(name, limit=20) for name in result.providers_attempted
        ],
        "providers_succeeded": [
            _public_identifier(name, limit=20) for name in result.providers_succeeded
        ],
        "errors": [_public_error(error) for error in result.errors],
        "items": items,
    }


def search_discovery(arguments: dict[str, Any]) -> ToolResult:
    normalized = search_arguments(arguments)
    if not config.get_bool("DISCOVERY_ENABLED", False):
        return ToolResult(
            ok=False,
            status="disabled",
            summary="影视探索功能当前已关闭",
            data={
                "query": normalized["query"],
                "page": normalized["page"],
                "total": 0,
                "returned": 0,
                "has_more": False,
                "providers_attempted": [],
                "providers_succeeded": [],
                "errors": [],
                "items": [],
            },
            evidence=[Evidence("discovery_config", "检查影视探索总开关。", _now())],
            suggestions=["请由管理员在设置中启用影视探索后重试。"],
            error="影视探索功能未启用。",
        )

    try:
        result = get_discovery_search_service().search(
            normalized["query"],
            normalized["page"],
            normalized["providers"],
        )
    except ValueError as exc:
        raise AgentToolError(str(exc)) from exc
    except Exception:
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="外部影视数据源暂时不可用",
            data={
                "query": normalized["query"],
                "page": normalized["page"],
                "total": 0,
                "returned": 0,
                "has_more": False,
                "providers_attempted": list(normalized["providers"] or []),
                "providers_succeeded": [],
                "errors": [],
                "items": [],
            },
            evidence=[Evidence("discovery_search", "调用已启用的外部影视搜索服务。", _now())],
            suggestions=["请稍后重试，或改用媒体库搜索。"],
            error="外部影视数据源暂时不可用。",
        )

    payload = _result_payload(result, limit=normalized["limit"])
    succeeded = len(payload["providers_succeeded"])
    errors = len(payload["errors"])
    if not succeeded:
        status = "unavailable"
        ok = False
        summary = "外部影视数据源暂时不可用"
        suggestions = ["请稍后重试，或改用媒体库搜索。"]
        error = "所有已选影视数据源均未完成搜索。"
    elif errors:
        status = "partial"
        ok = True
        summary = f"外部影视搜索返回 {payload['total']} 项结果，部分数据源不可用"
        suggestions = ["可稍后重试失败的数据源。"]
        error = ""
    elif payload["total"]:
        status = "success"
        ok = True
        summary = f"外部影视搜索找到 {payload['total']} 项结果"
        suggestions = []
        error = ""
    else:
        status = "empty"
        ok = True
        summary = "外部影视数据源中没有找到匹配内容"
        suggestions = ["可尝试原名、英文名或更短的关键词。"]
        error = ""

    return ToolResult(
        ok=ok,
        status=status,
        summary=summary,
        data=payload,
        evidence=[
            Evidence(
                "discovery_search",
                f"查询 {len(payload['providers_attempted'])} 个已启用的外部影视数据源。",
                _now(),
            )
        ],
        suggestions=suggestions,
        error=error,
    )


def _recommend_payload(normalized: dict[str, Any], page: DiscoveryPage) -> dict[str, Any]:
    cards = [card for card in tuple(page.items or ()) if isinstance(card, MediaCard)]
    limit = normalized["limit"]
    items = [_public_card(card) for card in cards[:limit]]
    health = getattr(page, "provider", None)
    health_status = _public_identifier(getattr(health, "status", ""), limit=40)
    if health_status not in _PROVIDER_STATUSES:
        health_status = "unavailable"
    retry_after = _safe_retry_after(getattr(health, "retry_after", 0))
    return {
        "provider": normalized["provider"],
        "category": "discover" if normalized["provider"] == "tmdb" else "recommend",
        "media_type": normalized["media_type"],
        "page": normalized["page"],
        "total": len(cards),
        "returned": len(items),
        "has_more": bool(page.has_more or len(cards) > limit),
        "cached": bool(page.cached),
        "stale": bool(page.stale),
        "provider_status": health_status,
        "retry_after": retry_after,
        "items": items,
    }


def recommend_discovery(arguments: dict[str, Any]) -> ToolResult:
    normalized = recommend_arguments(arguments)
    empty_payload = {
        "provider": normalized["provider"],
        "category": "discover" if normalized["provider"] == "tmdb" else "recommend",
        "media_type": normalized["media_type"],
        "page": normalized["page"],
        "total": 0,
        "returned": 0,
        "has_more": False,
        "cached": False,
        "stale": False,
        "provider_status": "disabled",
        "retry_after": 0,
        "items": [],
    }
    if not config.get_bool("DISCOVERY_ENABLED", False):
        return ToolResult(
            ok=False,
            status="disabled",
            summary="影视探索功能当前已关闭",
            data=empty_payload,
            evidence=[Evidence("discovery_config", "检查影视探索总开关。", _now())],
            suggestions=["请由管理员在设置中启用影视探索后重试。"],
            error="影视探索功能未启用。",
        )

    category = empty_payload["category"]
    try:
        page = get_discovery_service().list_items(
            normalized["provider"],
            category,
            normalized["media_type"],
            normalized["page"],
            {},
        )
    except ValueError as exc:
        raise AgentToolError(str(exc)) from exc
    except ProviderError as exc:
        code = exc.code if exc.code in _ERROR_MESSAGES else "unavailable"
        payload = dict(empty_payload)
        provider_status = "not_configured" if code == "not_configured" else "unavailable"
        payload.update({
            "provider_status": provider_status,
            "retry_after": _safe_retry_after(exc.retry_after),
        })
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="外部影视推荐源暂时不可用",
            data=payload,
            evidence=[Evidence("discovery_recommend", "调用受控的外部影视榜单服务。", _now())],
            suggestions=["请稍后重试，或切换另一个推荐来源。"],
            error=_ERROR_MESSAGES[code] + "。",
        )
    except Exception:
        payload = dict(empty_payload)
        payload["provider_status"] = "unavailable"
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="外部影视推荐源暂时不可用",
            data=payload,
            evidence=[Evidence("discovery_recommend", "调用受控的外部影视榜单服务。", _now())],
            suggestions=["请稍后重试，或切换另一个推荐来源。"],
            error="外部影视推荐源暂时不可用。",
        )

    payload = _recommend_payload(normalized, page)
    if payload["returned"]:
        status = "success"
        summary = f"推荐列表返回 {payload['returned']} 项内容"
        suggestions: list[str] = []
    else:
        status = "empty"
        summary = "当前推荐列表暂无内容"
        suggestions = ["可切换电影或电视剧，也可以尝试另一个推荐来源。"]
    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data=payload,
        evidence=[Evidence(
            "discovery_recommend",
            f"读取 {normalized['provider']} 的受控推荐列表；结果可能来自本地缓存。",
            _now(),
        )],
        suggestions=suggestions,
    )


def _public_calendar_card(card: MediaCard) -> dict[str, Any]:
    item = _public_card(card)
    weekday = card.weekday
    item["weekday"] = (
        weekday
        if isinstance(weekday, int) and not isinstance(weekday, bool) and 1 <= weekday <= 7
        else None
    )
    return item


def _calendar_payload(normalized: dict[str, Any], page: DiscoveryPage) -> dict[str, Any]:
    cards = [card for card in tuple(page.items or ()) if isinstance(card, MediaCard)]
    limit = normalized["limit"]
    items = [_public_calendar_card(card) for card in cards[:limit]]
    health = getattr(page, "provider", None)
    health_status = _public_identifier(getattr(health, "status", ""), limit=40)
    if health_status not in _PROVIDER_STATUSES:
        health_status = "unavailable"
    return {
        "provider": "bangumi",
        "category": "calendar",
        "media_type": "tv",
        "weekday": normalized.get("weekday"),
        "page": normalized["page"],
        "total": len(cards),
        "returned": len(items),
        "has_more": bool(page.has_more or len(cards) > limit),
        "cached": bool(page.cached),
        "stale": bool(page.stale),
        "provider_status": health_status,
        "retry_after": _safe_retry_after(getattr(health, "retry_after", 0)),
        "items": items,
    }


def bangumi_calendar(arguments: dict[str, Any]) -> ToolResult:
    normalized = calendar_arguments(arguments)
    empty_payload = {
        "provider": "bangumi",
        "category": "calendar",
        "media_type": "tv",
        "weekday": normalized.get("weekday"),
        "page": normalized["page"],
        "total": 0,
        "returned": 0,
        "has_more": False,
        "cached": False,
        "stale": False,
        "provider_status": "disabled",
        "retry_after": 0,
        "items": [],
    }
    if not config.get_bool("DISCOVERY_ENABLED", False):
        return ToolResult(
            ok=False,
            status="disabled",
            summary="影视探索功能当前已关闭",
            data=empty_payload,
            evidence=[Evidence("discovery_config", "检查影视探索总开关。", _now())],
            suggestions=["请由管理员在设置中启用影视探索后重试。"],
            error="影视探索功能未启用。",
        )

    filters = {} if normalized.get("weekday") is None else {"weekday": str(normalized["weekday"])}
    try:
        page = get_discovery_service().list_items(
            "bangumi", "calendar", "tv", normalized["page"], filters
        )
    except ValueError as exc:
        raise AgentToolError(str(exc)) from exc
    except ProviderError as exc:
        code = exc.code if exc.code in _ERROR_MESSAGES else "unavailable"
        payload = dict(empty_payload)
        payload.update({
            "provider_status": "not_configured" if code == "not_configured" else "unavailable",
            "retry_after": _safe_retry_after(exc.retry_after),
        })
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="Bangumi 放送日历暂时不可用",
            data=payload,
            evidence=[Evidence("bangumi_calendar", "调用受控的 Bangumi 放送日历服务。", _now())],
            suggestions=["请稍后重试。"],
            error=_ERROR_MESSAGES[code] + "。",
        )
    except Exception:
        payload = dict(empty_payload)
        payload["provider_status"] = "unavailable"
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="Bangumi 放送日历暂时不可用",
            data=payload,
            evidence=[Evidence("bangumi_calendar", "调用受控的 Bangumi 放送日历服务。", _now())],
            suggestions=["请稍后重试。"],
            error="Bangumi 放送日历暂时不可用。",
        )

    payload = _calendar_payload(normalized, page)
    weekday = normalized.get("weekday")
    scope = f"星期{'一二三四五六日'[weekday - 1]}" if weekday else "本周"
    if payload["returned"]:
        status = "success"
        summary = f"Bangumi {scope}放送日历返回 {payload['returned']} 项内容"
        suggestions: list[str] = []
    else:
        status = "empty"
        summary = f"Bangumi {scope}放送日历暂无内容"
        suggestions = ["可查看本周完整放送日历，或稍后重试。"] if weekday else ["可指定星期查看对应放送内容。"]
    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data=payload,
        evidence=[Evidence(
            "bangumi_calendar",
            "读取 Bangumi 的受控放送日历；结果可能来自本地缓存。",
            _now(),
        )],
        suggestions=suggestions,
    )
