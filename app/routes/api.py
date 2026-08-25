"""API 路由：看板数据、配置读写。日志查询已迁移到 logs_api 蓝图。"""
from __future__ import annotations

from typing import Any
import re
import time

import requests
from fastapi import APIRouter, Body, Request

from app import config
from app.clients.douban_authenticated import normalize_dbcl2
from app.indexers.config import build_indexer_site_updates, encode_indexer_site_ids
from app.logger import configure_telebot_logging, get_logger
from app.security import redact_config
from app.services import build_dashboards
from app.web import api_error, config_write_api_error, require_api_login

router = APIRouter(prefix="/api")
logger = get_logger(__name__)


_CONFIG_MASK = "********"
_CLEARABLE_SECRET_KEYS = frozenset({
    "TG_BOT_TOKEN",
    "AGENT_LLM_API_KEY",
    "TAVILY_API_KEY",
    "TMDB_API_KEY",
    "DOUBAN_DBCL2",
    "QB_PASSWORD",
    "QB_API_KEY",
    "JELLYFIN_API_KEY",
    "EMBY_TOKEN",
    "GY_ORGANIZE_NSFW_METATUBE_TOKEN",
})
_INDEXER_RUNTIME_REFRESH_TIMEOUT_SECONDS = 5.0
_DISCOVERY_BOOLEAN_KEYS = {
    "DISCOVERY_ENABLED",
    "DISCOVERY_DOUBAN_ENABLED",
    "DISCOVERY_RESOURCE_RESULTS_ENABLED",
    "ORGANIZE_DOUBAN_HINTS_ENABLED",
    "ORGANIZE_BANGUMI_HINTS_ENABLED",
}
_DISCOVERY_TTL_LIMITS = {
    "DISCOVERY_CACHE_TTL_SECONDS": (60, 604_800),
    "DISCOVERY_STALE_TTL_SECONDS": (300, 2_592_000),
    "DOUBAN_CACHE_TTL_SECONDS": (300, 604_800),
}
_DISCOVERY_TTL_DEFAULTS = {
    "DISCOVERY_CACHE_TTL_SECONDS": 21_600,
    "DISCOVERY_STALE_TTL_SECONDS": 604_800,
    "DOUBAN_CACHE_TTL_SECONDS": 21_600,
}
_DISCOVERY_RUNTIME_KEYS = {
    "DISCOVERY_ENABLED", "DISCOVERY_CACHE_TTL_SECONDS", "DISCOVERY_STALE_TTL_SECONDS",
    "DISCOVERY_RESOURCE_RESULTS_ENABLED",
    "DISCOVERY_DOUBAN_ENABLED", "DOUBAN_DBCL2", "DOUBAN_CACHE_TTL_SECONDS", "BANGUMI_USER_AGENT",
    "ORGANIZE_DOUBAN_HINTS_ENABLED", "ORGANIZE_BANGUMI_HINTS_ENABLED",
    "TMDB_API_KEY", "TMDB_API_URL", "TMDB_MATCH_MODE",
    "PROXY_URL",
}

_WEB_SEARCH_KEYS = {
    "WEB_SEARCH_ENABLED", "TAVILY_API_KEY", "TAVILY_SEARCH_DEPTH",
    "TAVILY_MAX_RESULTS", "TAVILY_CACHE_TTL_SECONDS",
    "TAVILY_DAILY_CREDIT_LIMIT", "TAVILY_TIMEOUT_SECONDS",
}

_AGENT_LLM_KEYS = {
    "AGENT_LLM_ENABLED",
    "AGENT_LLM_API_URL",
    "AGENT_LLM_API_KEY",
    "AGENT_LLM_PROTOCOL",
    "AGENT_LLM_MODEL",
    "AGENT_LLM_TIMEOUT_SECONDS",
    "AGENT_LLM_REQUESTS_PER_MINUTE",
}

_AGENT_LIBRARY_PATROL_KEYS = {
    "AGENT_LIBRARY_PATROL_ENABLED",
    "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED",
    "AGENT_LIBRARY_PATROL_INTERVAL_HOURS",
    "AGENT_LIBRARY_PATROL_MAX_SERIES",
    "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED",
}

_AGENT_SETTINGS_MANAGED_KEYS = (
    {"AGENT_ENABLED"} | _AGENT_LLM_KEYS | _WEB_SEARCH_KEYS | _AGENT_LIBRARY_PATROL_KEYS
)
_MEDIA_SERVER_REFRESH_KEYS = {
    "JELLYFIN_PATH_MAPPINGS",
    "JELLYFIN_ALLOW_GLOBAL_REFRESH_FALLBACK",
    "EMBY_PATH_MAPPINGS",
    "EMBY_ALLOW_GLOBAL_REFRESH_FALLBACK",
}
_MEDIA_SERVER_PROFILE_KEYS = {
    "JELLYFIN_ENABLED", "JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_USER_ID",
    "EMBY_ENABLED", "EMBY_URL", "EMBY_TOKEN", "EMBY_USER_ID",
}
_CONFIG_UI_MANAGED_KEYS = (
    _AGENT_SETTINGS_MANAGED_KEYS
    | {"GY_STRM_BASE_URL"}
    | _MEDIA_SERVER_REFRESH_KEYS
    | _MEDIA_SERVER_PROFILE_KEYS
)
_AGENT_SETTINGS_DEFAULTS = {
    "AGENT_ENABLED": "0",
    "AGENT_LLM_ENABLED": "0",
    "AGENT_LLM_PROTOCOL": "auto",
    "AGENT_LLM_TIMEOUT_SECONDS": "12",
    "AGENT_LLM_REQUESTS_PER_MINUTE": "6",
    "WEB_SEARCH_ENABLED": "0",
    "TAVILY_SEARCH_DEPTH": "basic",
    "TAVILY_MAX_RESULTS": "5",
    "TAVILY_CACHE_TTL_SECONDS": "900",
    "TAVILY_DAILY_CREDIT_LIMIT": "100",
    "TAVILY_TIMEOUT_SECONDS": "10",
    "AGENT_LIBRARY_PATROL_ENABLED": "0",
    "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED": "0",
    "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED": "1",
    "AGENT_LIBRARY_PATROL_INTERVAL_HOURS": "24",
    "AGENT_LIBRARY_PATROL_MAX_SERIES": "50",
}

_NSFW_ORGANIZE_KEYS = {
    "GY_ORGANIZE_NSFW_ENABLED",
    "GY_ORGANIZE_NSFW_METATUBE_ENDPOINT",
    "GY_ORGANIZE_NSFW_METATUBE_TOKEN",
    "GY_ORGANIZE_NSFW_CATEGORY_NAME",
    "GY_ORGANIZE_NSFW_STRIP_DOMAINS",
    "GY_ORGANIZE_NSFW_TIMEOUT_SECONDS",
}
_AI_RECOGNITION_KEYS = {
    "AI_RECOGNITION_ENABLED",
    "AI_RECOGNITION_CONFIDENCE_THRESHOLD",
    "AI_RECOGNITION_REQUESTS_PER_MINUTE",
    "AI_RECOGNITION_DAILY_REQUEST_LIMIT",
    "AI_RECOGNITION_MAX_CONCURRENCY",
    "AI_RECOGNITION_CIRCUIT_BREAKER_SECONDS",
}
_ORGANIZE_TAVILY_KEYS = {
    "ORGANIZE_TAVILY_HINTS_ENABLED",
    "ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT",
}
_ORGANIZE_POLICY_KEYS = {
    "GY_ORGANIZE_AUTOMATIC_MATCH_PRESET",
}
_FIXED_ORGANIZE_NAMING_KEYS = frozenset({
    "GY_ORGANIZE_RENAME",
    "GY_ORGANIZE_MEDIAINFO",
    "GY_ORGANIZE_MEDIA_PROBE_ENABLED",
    "GY_ORGANIZE_MEDIA_PROBE_TIMEOUT_SECONDS",
    "MEDIA_MOVIE_DIR_TEMPLATE",
    "MEDIA_MOVIE_TEMPLATE",
    "MEDIA_TV_TEMPLATE",
    "MEDIA_SHOW_DIR_TEMPLATE",
    "MEDIA_NAMING_SCOPE",
})
_BOOLEAN_VALUES = {
    "1": "1", "true": "1", "yes": "1", "on": "1", "y": "1",
    "0": "0", "false": "0", "no": "0", "off": "0", "n": "0",
}
_INDEXER_SITE_ORDER = (
    "nyaa", "mikan", "btbtla", "1lou", "animetosho", "tpb", "sukebei",
)
_DEFAULT_INDEXER_SITES = _INDEXER_SITE_ORDER[:6]


def _is_config_mask(value: Any) -> bool:
    return str(value).strip() == _CONFIG_MASK


def _normalize_discovery_boolean(key: str, value: Any) -> str:
    normalized = _BOOLEAN_VALUES.get(str(value).strip().lower())
    if normalized is None:
        raise ValueError(f"{key} 必须是布尔值（0/1、true/false）")
    return normalized


def _normalize_telegram_agent_user_ids(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized: list[str] = []
    invalid: list[str] = []
    for token in re.split(r"[,;，；\s]+", raw):
        item = token.strip()
        if not item:
            continue
        if not re.fullmatch(r"-?\d{1,24}", item):
            invalid.append(item)
            continue
        if item not in normalized:
            normalized.append(item)
    if invalid:
        raise ValueError(
            f"Telegram Agent 用户白名单包含无效用户 ID: {', '.join(invalid[:5])}"
        )
    return ",".join(normalized)


def _validate_telegram_agent_updates(data: dict[str, Any]) -> dict[str, str]:
    relevant = {
        "TG_BOT_TOKEN", "TG_CHAT_ID", "TG_AGENT_ENABLED",
        "TG_AGENT_ALLOWED_USER_IDS",
    }
    if not relevant & data.keys():
        return {}

    token_value = data.get("TG_BOT_TOKEN", _CONFIG_MASK)
    effective_token = (
        config.get("TG_BOT_TOKEN", "").strip()
        if _is_config_mask(token_value)
        else str(token_value or "").strip()
    )
    chat_value = data.get("TG_CHAT_ID", _CONFIG_MASK)
    effective_chat = (
        config.get("TG_CHAT_ID", "").strip()
        if _is_config_mask(chat_value)
        else str(chat_value or "").strip()
    )
    if effective_chat and not re.fullmatch(r"-?\d{1,20}", effective_chat):
        raise ValueError("Telegram Chat ID 必须是数字会话 ID")

    updates: dict[str, str] = {}
    if "TG_AGENT_ENABLED" in data:
        updates["TG_AGENT_ENABLED"] = _normalize_discovery_boolean(
            "TG_AGENT_ENABLED", data["TG_AGENT_ENABLED"]
        )
    effective_enabled = updates.get(
        "TG_AGENT_ENABLED",
        _BOOLEAN_VALUES.get(
            config.get("TG_AGENT_ENABLED", "0").strip().lower(), "0"
        ),
    ) == "1"

    if "TG_AGENT_ALLOWED_USER_IDS" in data:
        updates["TG_AGENT_ALLOWED_USER_IDS"] = _normalize_telegram_agent_user_ids(
            data["TG_AGENT_ALLOWED_USER_IDS"]
        )
    effective_users = updates.get("TG_AGENT_ALLOWED_USER_IDS", "")
    if effective_enabled and "TG_AGENT_ALLOWED_USER_IDS" not in updates:
        effective_users = _normalize_telegram_agent_user_ids(
            config.get("TG_AGENT_ALLOWED_USER_IDS", "")
        )

    if (
        {"TG_BOT_TOKEN", "TG_CHAT_ID"} & data.keys()
        and effective_token
        and not effective_chat
    ):
        raise ValueError("配置 Telegram Bot Token 时必须同时配置 Chat ID")
    if effective_enabled:
        if not effective_token:
            raise ValueError("启用 Telegram Agent 前必须配置 Bot Token")
        if not effective_chat:
            raise ValueError("启用 Telegram Agent 前必须配置 Chat ID")
        if not effective_users:
            raise ValueError("启用 Telegram Agent 前至少配置一个允许的用户 ID")
    return updates


def _normalize_discovery_ttl(key: str, value: Any) -> str:
    text = str(value).strip()
    try:
        seconds = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是整数秒数") from exc
    minimum, maximum = _DISCOVERY_TTL_LIMITS[key]
    if not minimum <= seconds <= maximum:
        raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 秒之间")
    return str(seconds)


def _existing_ttl(key: str) -> int:
    default = _DISCOVERY_TTL_DEFAULTS[key]
    try:
        return int(config.get(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _normalize_indexer_sites(value: Any) -> str:
    """兼容既有设置校验入口，实际白名单由 Indexer 共享模块维护。"""
    return encode_indexer_site_ids(str(value or ""))


def _validate_indexer_updates(
    data: dict[str, Any], discovery_updates: dict[str, str]
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if "INDEXER_SEARCH_ENABLED" in data:
        normalized["INDEXER_SEARCH_ENABLED"] = _normalize_discovery_boolean(
            "INDEXER_SEARCH_ENABLED", data["INDEXER_SEARCH_ENABLED"]
        )
    if "INDEXER_1LOU_GOOGLE_ENABLED" in data:
        normalized["INDEXER_1LOU_GOOGLE_ENABLED"] = _normalize_discovery_boolean(
            "INDEXER_1LOU_GOOGLE_ENABLED", data["INDEXER_1LOU_GOOGLE_ENABLED"]
        )
    interval_labels = {
        "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS": "BTBTLA",
        "INDEXER_1LOU_MIN_INTERVAL_SECONDS": "1LOU",
    }
    for key, label in interval_labels.items():
        if key not in data:
            continue
        try:
            interval = int(str(data[key]).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} 最小请求间隔必须是 0 到 60 的整数") from exc
        maximum = 10 if key == "INDEXER_1LOU_MIN_INTERVAL_SECONDS" else 60
        if not 0 <= interval <= maximum:
            raise ValueError(f"{label} 最小请求间隔必须在 0 到 {maximum} 秒之间")
        normalized[key] = str(interval)
    if "INDEXER_ENABLED_SITES" in data:
        sites = _normalize_indexer_sites(data.get("INDEXER_ENABLED_SITES"))
        resource_enabled = discovery_updates.get(
            "DISCOVERY_RESOURCE_RESULTS_ENABLED",
            "1" if config.get_bool("DISCOVERY_RESOURCE_RESULTS_ENABLED", True) else "0",
        )
        if resource_enabled == "1" and not sites:
            raise ValueError("启用站点资源时至少选择一个资源站点")
        normalized.update(build_indexer_site_updates(sites))
    elif "INDEXER_SUKEBEI_ENABLED" in data:
        normalized["INDEXER_SUKEBEI_ENABLED"] = _normalize_discovery_boolean(
            "INDEXER_SUKEBEI_ENABLED", data["INDEXER_SUKEBEI_ENABLED"]
        )
    return normalized


def _validate_discovery_updates(data: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key in _DISCOVERY_BOOLEAN_KEYS & data.keys():
        value = data[key]
        # 整理规则页的整表保存契约会把未勾选复选框序列化为空串；
        # 对这两个页面级开关按关闭处理，避免整表保存被探索设置校验误伤。
        if key in {
            "ORGANIZE_DOUBAN_HINTS_ENABLED",
            "ORGANIZE_BANGUMI_HINTS_ENABLED",
        } and str(value or "").strip() == "":
            value = "0"
        normalized[key] = _normalize_discovery_boolean(key, value)
    for key in _DISCOVERY_TTL_LIMITS.keys() & data.keys():
        normalized[key] = _normalize_discovery_ttl(key, data[key])

    if "BANGUMI_USER_AGENT" in data:
        raw_user_agent = str(data["BANGUMI_USER_AGENT"] or "")
        user_agent = raw_user_agent.strip()
        if not user_agent or "\r" in raw_user_agent or "\n" in raw_user_agent:
            raise ValueError("BANGUMI_USER_AGENT 不能为空且不能包含换行")
        if len(user_agent) > 256:
            raise ValueError("BANGUMI_USER_AGENT 不能超过 256 个字符")
        normalized["BANGUMI_USER_AGENT"] = user_agent

    if "DOUBAN_DBCL2" in data and not _is_config_mask(data["DOUBAN_DBCL2"]):
        normalized["DOUBAN_DBCL2"] = normalize_dbcl2(data["DOUBAN_DBCL2"])

    if {"DISCOVERY_CACHE_TTL_SECONDS", "DISCOVERY_STALE_TTL_SECONDS"} & data.keys():
        cache_ttl = int(normalized.get(
            "DISCOVERY_CACHE_TTL_SECONDS",
            _existing_ttl("DISCOVERY_CACHE_TTL_SECONDS"),
        ))
        stale_ttl = int(normalized.get(
            "DISCOVERY_STALE_TTL_SECONDS",
            _existing_ttl("DISCOVERY_STALE_TTL_SECONDS"),
        ))
        if stale_ttl < cache_ttl:
            raise ValueError("DISCOVERY_STALE_TTL_SECONDS 不能小于 DISCOVERY_CACHE_TTL_SECONDS")

    return normalized



def _validate_web_search_updates(data: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if "WEB_SEARCH_ENABLED" in data:
        normalized["WEB_SEARCH_ENABLED"] = _normalize_discovery_boolean(
            "WEB_SEARCH_ENABLED", data["WEB_SEARCH_ENABLED"]
        )
    if "TAVILY_API_KEY" in data and not _is_config_mask(data["TAVILY_API_KEY"]):
        key = str(data["TAVILY_API_KEY"] or "").strip()
        if "\r" in key or "\n" in key or len(key) > 512:
            raise ValueError("TAVILY_API_KEY 格式无效")
        normalized["TAVILY_API_KEY"] = key
    if "TAVILY_SEARCH_DEPTH" in data:
        depth = str(data["TAVILY_SEARCH_DEPTH"] or "basic").strip().lower()
        if depth not in {"basic", "advanced"}:
            raise ValueError("TAVILY_SEARCH_DEPTH 仅支持 basic 或 advanced")
        normalized["TAVILY_SEARCH_DEPTH"] = depth
    limits = {
        "TAVILY_MAX_RESULTS": (1, 10),
        "TAVILY_CACHE_TTL_SECONDS": (30, 86400),
        "TAVILY_DAILY_CREDIT_LIMIT": (1, 100000),
        "TAVILY_TIMEOUT_SECONDS": (2, 30),
    }
    for key, (minimum, maximum) in limits.items():
        if key not in data:
            continue
        try:
            value = int(str(data[key]).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须是整数") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
        normalized[key] = str(value)
    return normalized


def _validate_agent_llm_updates(data: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if "AGENT_ENABLED" in data:
        normalized["AGENT_ENABLED"] = _normalize_discovery_boolean(
            "AGENT_ENABLED", data["AGENT_ENABLED"]
        )
    if "AGENT_LLM_ENABLED" in data:
        normalized["AGENT_LLM_ENABLED"] = _normalize_discovery_boolean(
            "AGENT_LLM_ENABLED", data["AGENT_LLM_ENABLED"]
        )

    inferred_protocol = ""
    if "AGENT_LLM_API_URL" in data:
        from app.clients.openai_compatible import (
            infer_protocol_from_url,
            normalize_provider_location,
        )

        raw_url = str(data["AGENT_LLM_API_URL"] or "")
        api_url = raw_url.strip()
        if api_url:
            inferred_protocol = infer_protocol_from_url(api_url) or ""
            try:
                location = normalize_provider_location(
                    api_url, https_only=True, public_only=True
                )
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            api_url = location.base_url
        normalized["AGENT_LLM_API_URL"] = api_url

    if "AGENT_LLM_PROTOCOL" in data:
        from app.clients.openai_compatible import (
            PROTOCOLS,
            SUPPORTED_PROTOCOLS_TEXT,
            normalize_protocol,
        )

        raw_protocol = str(data["AGENT_LLM_PROTOCOL"] or "auto").strip().lower().replace("-", "_")
        protocol = normalize_protocol(raw_protocol)
        if raw_protocol not in PROTOCOLS:
            raise ValueError(
                f"AGENT_LLM_PROTOCOL 仅支持 {SUPPORTED_PROTOCOLS_TEXT}"
            )
        normalized["AGENT_LLM_PROTOCOL"] = (
            inferred_protocol if protocol == "auto" and inferred_protocol else protocol
        )
    elif inferred_protocol:
        normalized["AGENT_LLM_PROTOCOL"] = inferred_protocol

    if "AGENT_LLM_API_KEY" in data and not _is_config_mask(data["AGENT_LLM_API_KEY"]):
        raw_key = str(data["AGENT_LLM_API_KEY"] or "")
        api_key = raw_key.strip()
        if "\r" in raw_key or "\n" in raw_key or len(api_key) > 512:
            raise ValueError("AGENT_LLM_API_KEY 格式无效")
        normalized["AGENT_LLM_API_KEY"] = api_key

    if "AGENT_LLM_MODEL" in data:
        raw_model = str(data["AGENT_LLM_MODEL"] or "")
        model = raw_model.strip()
        if "\r" in raw_model or "\n" in raw_model or len(model) > 200:
            raise ValueError("AGENT_LLM_MODEL 不能包含换行且不能超过 200 个字符")
        normalized["AGENT_LLM_MODEL"] = model

    limits = {
        "AGENT_LLM_TIMEOUT_SECONDS": (2, 30),
        "AGENT_LLM_REQUESTS_PER_MINUTE": (1, 30),
    }
    for key, (minimum, maximum) in limits.items():
        if key not in data:
            continue
        try:
            value = int(str(data[key]).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须是整数") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
        normalized[key] = str(value)
    return normalized



def _validate_organize_policy_updates(data: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    key = "GY_ORGANIZE_AUTOMATIC_MATCH_PRESET"
    if key not in data:
        return normalized
    from app.modules.recognition_policy import (
        AUTOMATIC_MATCH_POLICIES,
        normalize_automatic_match_preset,
    )

    raw = str(data[key] or "").strip().lower()
    if raw not in AUTOMATIC_MATCH_POLICIES:
        allowed = "、".join(AUTOMATIC_MATCH_POLICIES)
        raise ValueError(f"{key} 必须是以下预设之一：{allowed}")
    normalized[key] = normalize_automatic_match_preset(raw)
    return normalized

def _validate_ai_recognition_updates(data: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if "AI_RECOGNITION_ENABLED" in data:
        normalized["AI_RECOGNITION_ENABLED"] = _normalize_discovery_boolean(
            "AI_RECOGNITION_ENABLED",
            data["AI_RECOGNITION_ENABLED"],
        )

    if "AI_RECOGNITION_CONFIDENCE_THRESHOLD" in data:
        try:
            confidence = float(
                str(data["AI_RECOGNITION_CONFIDENCE_THRESHOLD"]).strip()
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "AI_RECOGNITION_CONFIDENCE_THRESHOLD 必须是 0.5 到 0.99 的数字"
            ) from exc
        if not 0.5 <= confidence <= 0.99:
            raise ValueError(
                "AI_RECOGNITION_CONFIDENCE_THRESHOLD 必须在 0.5 到 0.99 之间"
            )
        normalized["AI_RECOGNITION_CONFIDENCE_THRESHOLD"] = (
            f"{confidence:.2f}".rstrip("0").rstrip(".")
        )

    limits = {
        "AI_RECOGNITION_REQUESTS_PER_MINUTE": (1, 30),
        "AI_RECOGNITION_DAILY_REQUEST_LIMIT": (1, 100_000),
        "AI_RECOGNITION_MAX_CONCURRENCY": (1, 8),
        "AI_RECOGNITION_CIRCUIT_BREAKER_SECONDS": (10, 600),
    }
    for key, (minimum, maximum) in limits.items():
        if key not in data:
            continue
        try:
            value = int(str(data[key]).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须是整数") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
        normalized[key] = str(value)
    return normalized


def _validate_organize_tavily_updates(data: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if "ORGANIZE_TAVILY_HINTS_ENABLED" in data:
        normalized["ORGANIZE_TAVILY_HINTS_ENABLED"] = _normalize_discovery_boolean(
            "ORGANIZE_TAVILY_HINTS_ENABLED",
            data["ORGANIZE_TAVILY_HINTS_ENABLED"],
        )
    if "ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT" in data:
        try:
            daily_limit = int(
                str(data["ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT"]).strip()
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT 必须是整数"
            ) from exc
        if not 1 <= daily_limit <= 100_000:
            raise ValueError(
                "ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT 必须在 1 到 100000 之间"
            )
        normalized["ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT"] = str(daily_limit)
    return normalized


def _validate_nsfw_organize_updates(data: dict[str, Any]) -> dict[str, str]:
    if not (_NSFW_ORGANIZE_KEYS & data.keys()):
        return {}
    normalized: dict[str, str] = {}
    if "GY_ORGANIZE_NSFW_ENABLED" in data:
        raw_enabled = data["GY_ORGANIZE_NSFW_ENABLED"]
        normalized["GY_ORGANIZE_NSFW_ENABLED"] = (
            "0" if str(raw_enabled or "").strip() == "" else _normalize_discovery_boolean(
                "GY_ORGANIZE_NSFW_ENABLED", raw_enabled
            )
        )
    if "GY_ORGANIZE_NSFW_METATUBE_ENDPOINT" in data:
        endpoint = str(data.get("GY_ORGANIZE_NSFW_METATUBE_ENDPOINT") or "").strip()
        if endpoint:
            from app.modules.media_proxy import validate_upstream_url
            endpoint = validate_upstream_url(endpoint)
        normalized["GY_ORGANIZE_NSFW_METATUBE_ENDPOINT"] = endpoint
    if "GY_ORGANIZE_NSFW_METATUBE_TOKEN" in data and not _is_config_mask(
        data["GY_ORGANIZE_NSFW_METATUBE_TOKEN"]
    ):
        token = str(data.get("GY_ORGANIZE_NSFW_METATUBE_TOKEN") or "").strip()
        if len(token) > 2048:
            raise ValueError("MetaTube Token 不能超过 2048 个字符")
        normalized["GY_ORGANIZE_NSFW_METATUBE_TOKEN"] = token
    if "GY_ORGANIZE_NSFW_CATEGORY_NAME" in data:
        from app.modules.nsfw import validate_category_name
        normalized["GY_ORGANIZE_NSFW_CATEGORY_NAME"] = validate_category_name(
            str(data.get("GY_ORGANIZE_NSFW_CATEGORY_NAME") or "成人内容")
        )
    if "GY_ORGANIZE_NSFW_STRIP_DOMAINS" in data:
        raw = str(data.get("GY_ORGANIZE_NSFW_STRIP_DOMAINS") or "")
        values: list[str] = []
        for item in re.split(r"[,，\s]+", raw):
            domain = item.strip().lower().removeprefix("www.").strip(".")
            if not domain:
                continue
            if len(domain) > 120 or not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}", domain
            ):
                raise ValueError(f"无效的成人文件名清理域名: {domain[:40]}")
            if domain not in values:
                values.append(domain)
        normalized["GY_ORGANIZE_NSFW_STRIP_DOMAINS"] = ",".join(values[:100])
    if "GY_ORGANIZE_NSFW_TIMEOUT_SECONDS" in data:
        raw_timeout = str(data["GY_ORGANIZE_NSFW_TIMEOUT_SECONDS"] or "").strip()
        try:
            timeout = int(raw_timeout or str(config.get("GY_ORGANIZE_NSFW_TIMEOUT_SECONDS", "8") or "8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("MetaTube 超时必须是 2 到 30 的整数") from exc
        if not 2 <= timeout <= 30:
            raise ValueError("MetaTube 超时必须在 2 到 30 秒之间")
        normalized["GY_ORGANIZE_NSFW_TIMEOUT_SECONDS"] = str(timeout)
    enabled = normalized.get(
        "GY_ORGANIZE_NSFW_ENABLED",
        "1" if config.get_bool("GY_ORGANIZE_NSFW_ENABLED", False) else "0",
    ) == "1"
    endpoint = normalized.get(
        "GY_ORGANIZE_NSFW_METATUBE_ENDPOINT",
        str(config.get("GY_ORGANIZE_NSFW_METATUBE_ENDPOINT", "") or ""),
    )
    if enabled and not endpoint:
        raise ValueError("启用成人内容识别前必须配置 MetaTube 服务地址")
    return normalized


def _validate_login_wallpaper_updates(data: dict[str, Any]) -> dict[str, str]:
    if not {"LOGIN_WALLPAPER_MODE", "TMDB_API_KEY"} & data.keys():
        return {}
    mode = str(
        data.get(
            "LOGIN_WALLPAPER_MODE",
            config.get("LOGIN_WALLPAPER_MODE", "default") or "default",
        )
        or ""
    ).strip().lower()
    if mode not in {"default", "tmdb"}:
        raise ValueError("登录页壁纸模式无效")
    submitted_key = data.get("TMDB_API_KEY")
    if submitted_key is None or _is_config_mask(submitted_key):
        api_key = config.get("TMDB_API_KEY", "").strip()
    else:
        api_key = str(submitted_key or "").strip()
    if mode == "tmdb" and not api_key:
        raise ValueError("启用电影海报前请先配置 TMDB API Key")
    return (
        {"LOGIN_WALLPAPER_MODE": mode}
        if "LOGIN_WALLPAPER_MODE" in data
        else {}
    )


def _media_test_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, requests.Timeout):
        return "连接超时，请检查服务地址、网络或代理设置", "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "无法连接服务器，请检查地址、端口和网络可达性", "connection"
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        if status in {401, 403}:
            return "服务器已响应，但凭据无效或权限不足", "authentication"
        if status == 404:
            return "服务器已响应，但 API 路径不可用，请确认服务类型和地址", "not_found"
        return f"服务器返回 HTTP {status or '错误'}", "http"
    if isinstance(exc, ValueError):
        return str(exc), "validation"
    return "连接测试失败，请检查配置后重试", "unknown"


@router.post("/telegram/test")
def test_telegram_message(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("测试参数必须是 JSON 对象", 400)
    token = str(data.get("token") or "").strip()
    chat_id = str(data.get("chat_id") or "").strip()
    # 密钥字段保存后不会回填真实值，前端会提交空字符串或脱敏占位符。
    # 两种情况都应测试当前已保存的 Bot Token；有新输入时仍优先使用新值。
    if not token or token == "********":
        token = config.get("TG_BOT_TOKEN", "").strip()
    if not chat_id:
        chat_id = config.get("TG_CHAT_ID", "").strip()
    if not token or ":" not in token:
        return api_error("请输入有效的 Bot Token", 400)
    if not chat_id or len(chat_id) > 64:
        return api_error("请输入有效的 Chat ID", 400)

    try:
        import telebot

        configure_telebot_logging()
        bot = telebot.TeleBot(token, parse_mode="HTML", threaded=False)
        sent = bot.send_message(
            chat_id,
            "<b>MediaFlux 连接测试</b>\nTelegram Bot 通知通道工作正常。",
        )
        return {
            "success": True,
            "message_id": getattr(sent, "message_id", None),
            "status": "测试消息已发送",
        }
    except Exception as exc:
        status = getattr(exc, "error_code", None)
        if status in {401, 404}:
            message = "Bot Token 无效或 Bot 不存在"
        elif status == 403:
            message = "Bot 无权向该会话发送消息，请先与 Bot 建立会话或检查权限"
        elif status == 400:
            message = "Chat ID 无效，或该会话无法接收 Bot 消息"
        elif isinstance(exc, requests.Timeout):
            message = "Telegram 请求超时，请检查网络代理"
        elif isinstance(exc, requests.ConnectionError):
            message = "无法连接 Telegram，请检查网络代理"
        else:
            message = "测试消息发送失败，请检查 Token、Chat ID 与网络代理"
        return api_error(message, 502)


@router.post("/media/test")
def test_media_connection(request: Request, data: Any = Body(default=None)):
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("测试参数必须是 JSON 对象", 400)

    server_type = str(data.get("server_type") or "").strip().lower()
    if server_type not in {"jellyfin", "emby"}:
        return api_error("仅支持 Jellyfin 12 或 Emby / Jellyfin 10.x", 400)
    from app.modules.media_proxy import validate_upstream_url
    from app.modules.media_server_profiles import list_configured_profiles

    try:
        url = validate_upstream_url(str(data.get("url") or ""))
    except (TypeError, ValueError):
        return api_error("请输入安全有效的 HTTP(S) 服务地址", 400)

    token = str(data.get("token") or "").strip()
    if token == _CONFIG_MASK:
        profile = next(
            (item for item in list_configured_profiles() if item.server_type == server_type),
            None,
        )
        saved_url = str(getattr(profile, "url", "") or "").strip().rstrip("/")
        if profile is None or url != saved_url:
            return api_error("修改服务地址后，请重新输入 API Key 或 Token", 400)
        token = str(profile.credential or "").strip()
    if not token:
        return api_error("请输入有效的访问凭据", 400)

    headers = {"Accept": "application/json"}
    params = None
    if server_type == "jellyfin":
        headers["Authorization"] = f'MediaBrowser Token="{token}"'
    else:
        headers["X-Emby-Token"] = token
        headers["Authorization"] = f'MediaBrowser Token="{token}"'

    started = time.perf_counter()
    try:
        response = requests.get(
            f"{url}/System/Info",
            headers=headers,
            params=params,
            timeout=(3.5, 8),
            allow_redirects=False,
        )
        if 300 <= int(response.status_code) < 400:
            return api_error("媒体服务器返回了重定向，请检查服务地址", 502)
        response.raise_for_status()
        info = response.json()
        if not isinstance(info, dict):
            raise ValueError("服务器响应格式异常")
        latency_ms = max(1, round((time.perf_counter() - started) * 1000))
        product_hint = str(info.get("ProductName") or info.get("Product") or "").strip()
        fingerprint = " ".join((product_hint, str(info.get("ServerName") or ""))).lower()
        if server_type == "jellyfin" or "jellyfin" in fingerprint:
            product = "Jellyfin"
        else:
            product = "Emby"
        return {
            "success": True,
            "server_type": server_type,
            "server_name": str(info.get("ServerName") or info.get("Name") or server_type.title()),
            "product": product,
            "version": str(info.get("Version") or info.get("ServerVersion") or "未知"),
            "latency_ms": latency_ms,
            "url": url,
        }
    except Exception as exc:
        message, _error_type = _media_test_error(exc)
        return api_error(message, 502)


@router.get("/dashboard")
def dashboard_data(request: Request, refresh: bool = False):
    require_api_login(request)
    boards = build_dashboards(force=refresh)
    return [
        {
                "server_name": b.server_name,
                "server_type": b.server_type,
                "web_url": b.web_url,
                "online": b.online,
                "error": b.error,
                "server_product": b.server_product,
                "server_version": b.server_version,
                "partial_errors": b.partial_errors,
                "total_items": b.total_items,
                "movie_count": b.movie_count,
                "series_count": b.series_count,
                "episode_count": b.episode_count,
                "total_plays": b.total_plays,
                "libraries": [
                    {
                        "id": library.id,
                        "name": library.name,
                        "type": library.item_type,
                        "count": library.count,
                        "primary_image": library.primary_image,
                        "web_url": library.web_url,
                    }
                    for library in b.libraries
                ],
                "recent_played": [
                    {
                        "id": item.id,
                        "name": item.name,
                        "display_name": item.display_name,
                        "type": item.type,
                        "year": item.year,
                        "last_played": item.last_played,
                        "primary_image": item.primary_image,
                        "web_url": item.web_url,
                        "series_name": item.series_name,
                        "season_number": item.season_number,
                        "episode_number": item.episode_number,
                        "episode_label": item.episode_label,
                        "progress": item.progress,
                    }
                    for item in b.recent_played
                ],
                "recent_added": [
                    {
                        "id": item.id,
                        "name": item.name,
                        "display_name": item.display_name,
                        "type": item.type,
                        "year": item.year,
                        "date_added": item.date_added,
                        "overview": item.overview,
                        "primary_image": item.primary_image,
                        "web_url": item.web_url,
                        "series_name": item.series_name,
                        "season_number": item.season_number,
                        "episode_number": item.episode_number,
                        "episode_label": item.episode_label,
                    }
                    for item in b.recent_added
                ],
        }
        for b in boards
    ]


@router.get("/config")
def get_config(request: Request):
    require_api_login(request)
    items = {
        key: value
        for key, value in config.all_items().items()
        if key not in ({
            "DOUBAN_FRODO_API_KEY", "DOUBAN_FRODO_API_SECRET",
        } | _FIXED_ORGANIZE_NAMING_KEYS)
    }
    # 运行目录只作为缺省值展示；用户保存的 STRM_ROOT（包括显式空值）仍优先。
    items.setdefault("STRM_ROOT", config.get("STRM_ROOT", ""))
    # 旧版整单降级开关已停用；即使数据库仍有历史值也不再暴露给前端。
    items.pop("OFFLINE_MAGNET_UNVERIFIED_FALLBACK", None)
    # 离线选择固定为仅视频；历史音频/附件扩展名在展示与执行时都会被剔除。
    from app.modules.offline import DEFAULT_MEDIA_EXTS_CSV, normalize_video_extensions

    items["OFFLINE_ALLOWED_EXTS"] = ",".join(
        normalize_video_extensions(str(items.get("OFFLINE_ALLOWED_EXTS") or ""))
    ) or DEFAULT_MEDIA_EXTS_CSV
    managed_fields = sorted(
        key for key in _CONFIG_UI_MANAGED_KEYS
        if config.has_external_override(key)
    )
    for key in managed_fields:
        items[key] = config.get(key, _AGENT_SETTINGS_DEFAULTS.get(key, ""))
    redacted = redact_config(items)
    if items.get("DOUBAN_DBCL2"):
        redacted["DOUBAN_DBCL2"] = _CONFIG_MASK
    redacted["__managed_fields"] = managed_fields
    return redacted


def _normalize_organize_extensions(value: object, *, defaults: tuple[str, ...], label: str) -> str:
    """规范化整理扩展名，保持顺序并拒绝不可用输入。"""
    raw = str(value or "").strip()
    if not raw:
        return ",".join(defaults)
    normalized: list[str] = []
    invalid: list[str] = []
    for token in re.split(r"[,，\s]+", raw):
        item = token.strip().lower().lstrip(".")
        if not item:
            continue
        if not re.fullmatch(r"[a-z0-9]{1,10}", item):
            invalid.append(token.strip() or token)
            continue
        if item not in normalized:
            normalized.append(item)
    if invalid:
        raise ValueError(f"{label}包含无效扩展名: {', '.join(invalid[:5])}")
    if not normalized:
        raise ValueError(f"{label}至少保留一个扩展名")
    return ",".join(normalized)


@router.post("/config")
def save_config(request: Request, data: Any = Body(default=None)):
    request_started = time.perf_counter()
    require_api_login(request)
    if not isinstance(data, dict):
        return api_error("配置必须是 JSON 对象", 400)
    data = dict(data)
    clear_secrets = data.pop("__clear_secrets", [])
    if clear_secrets is None:
        clear_secrets = []
    if not isinstance(clear_secrets, list):
        return api_error("__clear_secrets 必须是数组", 400)
    clear_keys = {str(key or "").strip() for key in clear_secrets}
    invalid_clear_keys = sorted(clear_keys - _CLEARABLE_SECRET_KEYS)
    if invalid_clear_keys:
        return api_error(
            f"包含不允许清除的敏感配置: {', '.join(invalid_clear_keys[:5])}",
            400,
        )
    for key in clear_keys:
        data[key] = ""
    allowed = {
        "ENV_WEB_PASSPORT", "ENV_WEB_PASSWORD", "LOGIN_WALLPAPER_MODE",
        "EMBY_ENABLED", "EMBY_URL", "EMBY_TOKEN", "EMBY_USER_ID",
        "JELLYFIN_ENABLED", "JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_USER_ID",
        *_MEDIA_SERVER_REFRESH_KEYS,
        "QB_URL", "QB_USERNAME", "QB_PASSWORD", "QB_API_KEY",
        "TG_QB_CATEGORY", "TG_QB_SAVE_PATH",
        "TMDB_API_KEY", "TMDB_API_URL",
        "TMDB_MATCH_MODE", "PROXY_URL", "TG_BOT_TOKEN", "TG_CHAT_ID",
        "TG_AGENT_ENABLED", "TG_AGENT_ALLOWED_USER_IDS", "AGENT_ENABLED",
        *_AI_RECOGNITION_KEYS,
        *_ORGANIZE_TAVILY_KEYS,
        *_ORGANIZE_POLICY_KEYS,
        *_AGENT_LLM_KEYS,
        *_WEB_SEARCH_KEYS,
        *_NSFW_ORGANIZE_KEYS,
        "DISCOVERY_ENABLED", "DISCOVERY_CACHE_TTL_SECONDS", "DISCOVERY_STALE_TTL_SECONDS",
        "DISCOVERY_RESOURCE_RESULTS_ENABLED",
        "DISCOVERY_DOUBAN_ENABLED", "DOUBAN_DBCL2", "DOUBAN_CACHE_TTL_SECONDS", "BANGUMI_USER_AGENT",
        "ORGANIZE_DOUBAN_HINTS_ENABLED", "ORGANIZE_BANGUMI_HINTS_ENABLED",
        "INDEXER_SEARCH_ENABLED", "INDEXER_ENABLED_SITES", "INDEXER_SUKEBEI_ENABLED",
        "INDEXER_SITE_TIMEOUT_SECONDS", "INDEXER_TOTAL_TIMEOUT_SECONDS",
        "INDEXER_MAX_RESULTS_PER_SITE", "INDEXER_MAX_CONCURRENCY",
        "INDEXER_CACHE_TTL_SECONDS", "INDEXER_RESULT_TTL_SECONDS", "INDEXER_USER_AGENT",
        "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS", "INDEXER_1LOU_MIN_INTERVAL_SECONDS",
        "INDEXER_1LOU_GOOGLE_ENABLED",
        "GY_STRM_SOURCE_DIRS", "GY_STRM_BASE_URL", "STRM_ROOT",
        "STRM_VIDEO_EXTS", "STRM_SKIP_THRESHOLD_MB", "STRM_METADATA_ENABLED",
        "STRM_METADATA_EXTS", "STRM_SCHEDULE_ENABLED", "STRM_SCHEDULE_CRON",
        "STRM_NOTIFY_ENABLED", "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED",
        "AGENT_LIBRARY_PATROL_ENABLED", "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED",
        "AGENT_LIBRARY_PATROL_INTERVAL_HOURS",
        "AGENT_LIBRARY_PATROL_MAX_SERIES",
        "OFFLINE_MAGNET_ENABLED", "OFFLINE_ED2K_ENABLED",
        "OFFLINE_HTTP_ENABLED", "OFFLINE_TARGET_DIR", "OFFLINE_TARGET_DIR_NAME",
        "OFFLINE_SECONDARY_ENABLED", "OFFLINE_SECONDARY_DIR", "OFFLINE_SECONDARY_DIR_NAME",
        "OFFLINE_SECONDARY_KEYWORDS", "OFFLINE_EXCLUDE_KEYWORDS", "OFFLINE_MIN_FILE_MB",
        "OFFLINE_ALLOWED_EXTS", "RSS_DOWNLOAD_METHOD", "RSS_QB_CATEGORY", "RSS_QB_SAVE_PATH",
        "RSS_GY_TARGET_DIR",
        "GY_SHARE_TARGET_DIR", "GY_SHARE_TARGET_DIR_NAME",
        "GY_ORGANIZE_SOURCE_DIRS", "GY_ORGANIZE_TARGET_DIR",
        "GY_ORGANIZE_TARGET_DIR_NAME", "GY_ORGANIZE_REGION_SPLIT", "GY_ORGANIZE_YEAR_SPLIT",
        "GY_ORGANIZE_ADD_KIDS", "GY_ORGANIZE_ADD_CONCERT", "GY_ORGANIZE_CONFLICT_STRATEGY",
        "GY_ORGANIZE_REMUX_FIRST", "GY_ORGANIZE_RESOLUTION_FIRST", "GY_ORGANIZE_DOLBY_FIRST",
        "GY_ORGANIZE_KEEP_MULTI_VERSIONS", "GY_ORGANIZE_KEEP_REMUX_VARIANT",
        "GY_ORGANIZE_RECYCLE_REPLACED_ENABLED",
        "GY_ORGANIZE_SMALL_FILE_MB", "GY_ORGANIZE_CLEAN_EMPTY", "GY_ORGANIZE_LINK_STRM",
        "GY_ORGANIZE_VIDEO_EXTS", "GY_ORGANIZE_METADATA_EXTS",
        "GY_ORGANIZE_NOTIFY_ENABLED",
        "GY_ORGANIZE_LIBRARY_NOTIFY",
        "GY_ORGANIZE_STRM_DETAIL_NOTIFY", "GY_ORGANIZE_EMBY_REFRESH",
        "GY_ORGANIZE_SCHEDULE_ENABLED", "GY_ORGANIZE_SCHEDULE_CRON",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        return api_error(f"包含不允许的配置项: {', '.join(unknown[:5])}", 400)
    managed_updates = sorted(
        key for key in data
        if key in _AGENT_SETTINGS_MANAGED_KEYS and config.has_external_override(key)
    )
    if managed_updates:
        return api_error(
            "以下配置由部署环境管理，不能在页面修改: "
            + ", ".join(managed_updates[:5]),
            409,
        )

    web_updates: dict[str, str] = {}
    if {"ENV_WEB_PASSPORT", "ENV_WEB_PASSWORD"} & data.keys():
        from app.modules.first_run import _validate_credentials

        current_username, current_password = config.web_credentials()
        raw_username = data.get("ENV_WEB_PASSPORT", _CONFIG_MASK)
        raw_password = data.get("ENV_WEB_PASSWORD", _CONFIG_MASK)
        username_masked = _is_config_mask(raw_username)
        password_masked = _is_config_mask(raw_password)
        username = current_username if username_masked else str(raw_username or "")
        password_text = str(raw_password or "")
        password_unchanged = password_masked or not password_text.strip()
        password = current_password if password_unchanged else password_text
        if not username_masked and not username.strip():
            return api_error("管理员用户名不能为空", 400)
        try:
            normalized_username = _validate_credentials(username, password)
        except ValueError as exc:
            return api_error(str(exc), 400)
        if (
            config.get("APP_ENV", "development").strip().lower() == "production"
            and password == "123456"
        ):
            return api_error("生产模式禁止使用默认 Web 密码", 400)
        if not username_masked:
            web_updates["ENV_WEB_PASSPORT"] = normalized_username
        if not password_unchanged:
            web_updates["ENV_WEB_PASSWORD"] = password

    try:
        telegram_agent_updates = _validate_telegram_agent_updates(data)
        discovery_updates = _validate_discovery_updates(data)
        indexer_updates = _validate_indexer_updates(data, discovery_updates)
        ai_recognition_updates = _validate_ai_recognition_updates(data)
        organize_tavily_updates = _validate_organize_tavily_updates(data)
        organize_policy_updates = _validate_organize_policy_updates(data)
        agent_llm_updates = _validate_agent_llm_updates(data)
        web_search_updates = _validate_web_search_updates(data)
        login_wallpaper_updates = _validate_login_wallpaper_updates(data)
        nsfw_organize_updates = _validate_nsfw_organize_updates(data)
    except ValueError as exc:
        return api_error(str(exc), 400)
    for key in ("JELLYFIN_USER_ID", "EMBY_USER_ID"):
        if key not in data:
            continue
        raw_user_id = str(data[key] or "").strip()
        if not raw_user_id:
            data[key] = ""
            continue
        try:
            from app.clients.base import normalize_explicit_media_user_id

            data[key] = normalize_explicit_media_user_id(raw_user_id)
        except ValueError as exc:
            label = "Jellyfin" if key.startswith("JELLYFIN") else "Emby"
            return api_error(f"{label} 共享用户 ID 无效: {exc}", 400)
    media_server_refresh_updates: dict[str, str] = {}
    for key in ("JELLYFIN_PATH_MAPPINGS", "EMBY_PATH_MAPPINGS"):
        if key not in data:
            continue
        try:
            from app.modules.media_server_path_mapping import (
                encode_media_server_path_mappings,
            )

            media_server_refresh_updates[key] = encode_media_server_path_mappings(
                data[key]
            )
        except ValueError as exc:
            label = "Jellyfin" if key.startswith("JELLYFIN") else "Emby"
            return api_error(f"{label} 路径映射无效: {exc}", 400)
    agent_patrol_updates = {}
    for key in (
        "AGENT_LIBRARY_PATROL_ENABLED",
        "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED",
    ):
        if key not in data:
            continue
        try:
            agent_patrol_updates[key] = _normalize_discovery_boolean(key, data[key])
        except ValueError as exc:
            return api_error(str(exc), 400)
    media_workflow_updates = {}
    extension_keys = {"GY_ORGANIZE_VIDEO_EXTS", "GY_ORGANIZE_METADATA_EXTS"}
    if extension_keys & data.keys():
        try:
            from app.modules.organize import (
                DEFAULT_ORGANIZE_METADATA_EXTS, DEFAULT_ORGANIZE_VIDEO_EXTS,
                METADATA_EXTS, VIDEO_EXTS, Organizer,
            )

            if "GY_ORGANIZE_VIDEO_EXTS" in data:
                media_workflow_updates["GY_ORGANIZE_VIDEO_EXTS"] = _normalize_organize_extensions(
                    data["GY_ORGANIZE_VIDEO_EXTS"],
                    defaults=DEFAULT_ORGANIZE_VIDEO_EXTS,
                    label="视频文件类型",
                )
            if "GY_ORGANIZE_METADATA_EXTS" in data:
                media_workflow_updates["GY_ORGANIZE_METADATA_EXTS"] = _normalize_organize_extensions(
                    data["GY_ORGANIZE_METADATA_EXTS"],
                    defaults=DEFAULT_ORGANIZE_METADATA_EXTS,
                    label="伴随文件类型",
                )
            video_set = set(media_workflow_updates["GY_ORGANIZE_VIDEO_EXTS"].split(",")) if "GY_ORGANIZE_VIDEO_EXTS" in media_workflow_updates else Organizer._parse_exts(
                config.get("GY_ORGANIZE_VIDEO_EXTS", ""), VIDEO_EXTS,
            )
            metadata_set = set(media_workflow_updates["GY_ORGANIZE_METADATA_EXTS"].split(",")) if "GY_ORGANIZE_METADATA_EXTS" in media_workflow_updates else Organizer._parse_exts(
                config.get("GY_ORGANIZE_METADATA_EXTS", ""), METADATA_EXTS,
            )
            overlap = video_set & metadata_set
            if overlap:
                raise ValueError(f"视频与伴随文件类型不能重复: {', '.join(sorted(overlap))}")
        except ValueError as exc:
            return api_error(str(exc), 400)
    old_strm_sources: list[dict[str, str]] = []
    new_strm_sources: list[dict[str, str]] | None = None
    old_strm_root = config.get("STRM_ROOT", "").strip()
    strm_source_config_changed = "GY_STRM_SOURCE_DIRS" in data
    if "GY_ORGANIZE_SOURCE_DIRS" in data:
        from app.modules.organize_sources import encode_organize_sources, normalize_organize_sources

        sources, error = normalize_organize_sources(data["GY_ORGANIZE_SOURCE_DIRS"])
        if error:
            return api_error(error, 400)
        media_workflow_updates["GY_ORGANIZE_SOURCE_DIRS"] = encode_organize_sources(sources)
    if strm_source_config_changed:
        import json
        from app.modules.strm import parse_strm_sources

        current_canonical = config.get("GY_STRM_SOURCE_DIRS", "")
        old_strm_sources, _ = parse_strm_sources(
            current_canonical, require_nonempty=False,
        )
        prospective_canonical = data.get("GY_STRM_SOURCE_DIRS", current_canonical)
        sources, error = parse_strm_sources(
            prospective_canonical, require_nonempty=False
        )
        if error:
            return api_error(error, 400)
        new_strm_sources = sources
        if "GY_STRM_SOURCE_DIRS" in data:
            media_workflow_updates["GY_STRM_SOURCE_DIRS"] = json.dumps(
                sources, ensure_ascii=False, separators=(",", ":")
            )
    if "GY_STRM_BASE_URL" in data:
        raw_base_url = str(data.get("GY_STRM_BASE_URL") or "").strip()
        if raw_base_url:
            from app.modules.media_proxy import validate_upstream_url

            try:
                media_workflow_updates["GY_STRM_BASE_URL"] = validate_upstream_url(
                    raw_base_url
                )
            except (TypeError, ValueError):
                return api_error(
                    "播放服务地址必须是安全有效的 HTTP(S) 地址，且不能使用 0.0.0.0",
                    400,
                )
        else:
            media_workflow_updates["GY_STRM_BASE_URL"] = ""
    cron_validators = {
        "STRM_SCHEDULE_CRON": ("app.modules.scheduler", "STRMScheduler"),
        "GY_ORGANIZE_SCHEDULE_CRON": ("app.modules.organize_scheduler", "OrganizeScheduler"),
    }
    for key, (module_name, class_name) in cron_validators.items():
        if key not in data:
            continue
        value = str(data[key] or "").strip()
        if value:
            import importlib

            validator = getattr(importlib.import_module(module_name), class_name)
            if not validator.validate_cron(value):
                return api_error(f"{key} 必须是有效的 5 段 cron 表达式", 400)
    integer_limits = {
        "STRM_SKIP_THRESHOLD_MB": (0, None),
        "GY_ORGANIZE_SMALL_FILE_MB": (0, None),
        "AGENT_LIBRARY_PATROL_INTERVAL_HOURS": (1, 168),
        "AGENT_LIBRARY_PATROL_MAX_SERIES": (1, 100),
        "GY_ORGANIZE_NSFW_TIMEOUT_SECONDS": (2, 30),
    }
    for key, (minimum, maximum) in integer_limits.items():
        if key not in data:
            continue
        if str(data[key] or "").strip() == "":
            continue
        try:
            number = int(str(data[key]).strip())
        except (TypeError, ValueError):
            return api_error(f"{key} 必须是整数", 400)
        if number < minimum or (maximum is not None and number > maximum):
            scope = f"{minimum} 到 {maximum}" if maximum is not None else f"不小于 {minimum}"
            return api_error(f"{key} 必须{scope}", 400)
    updates = dict(web_updates)
    if "OFFLINE_ALLOWED_EXTS" in data:
        try:
            from app.modules.offline import DEFAULT_MEDIA_EXTS

            normalized_exts = _normalize_organize_extensions(
                data.get("OFFLINE_ALLOWED_EXTS"),
                defaults=DEFAULT_MEDIA_EXTS,
                label="离线允许扩展名",
            )
            unsupported = [
                item for item in normalized_exts.split(",") if item not in DEFAULT_MEDIA_EXTS
            ]
            if unsupported:
                raise ValueError(
                    "离线转存仅允许视频扩展名，不支持: " + ", ".join(unsupported[:5])
                )
            data["OFFLINE_ALLOWED_EXTS"] = normalized_exts
        except ValueError as exc:
            return api_error(str(exc), 400)
    special_updates: dict[str, str] = {}
    for source in (
        discovery_updates,
        media_workflow_updates,
        indexer_updates,
        web_search_updates,
        ai_recognition_updates,
        organize_tavily_updates,
        organize_policy_updates,
        agent_llm_updates,
        login_wallpaper_updates,
        nsfw_organize_updates,
        agent_patrol_updates,
        telegram_agent_updates,
        media_server_refresh_updates,
    ):
        special_updates.update(source)
    for key, value in data.items():
        if key in {"ENV_WEB_PASSPORT", "ENV_WEB_PASSWORD"}:
            continue
        text = special_updates.get(key, str(value))
        if _is_config_mask(text):
            continue
        if len(text) > 10_000:
            return api_error(f"配置项 {key} 过长", 400)
        normalized = text.replace("\r\n", ",").replace("\r", ",").replace("\n", ",")
        updates[key] = normalized
    for key, value in indexer_updates.items():
        updates.setdefault(key, value)
    # 允许从用户粘贴的完整 endpoint 派生协议，即使该请求未显式提交协议字段。
    for key, value in agent_llm_updates.items():
        updates.setdefault(key, value)
    persisted_updates = {
        key: value
        for key, value in updates.items()
        if config.get(key, "") != value
    }
    if not persisted_updates:
        logger.info(
            "配置保存完成 changed=0 total_ms=%s",
            max(1, round((time.perf_counter() - request_started) * 1000)),
        )
        return {"success": True}

    persist_started = time.perf_counter()
    try:
        config.set_and_save(persisted_updates)
    except (config.AtomicPublishError, OSError) as exc:
        return config_write_api_error(
            exc,
            logger=logger,
            operation="save_settings",
        )
    persist_ms = max(1, round((time.perf_counter() - persist_started) * 1000))

    retired_source_ids: list[str] = []
    if new_strm_sources is not None and "GY_STRM_SOURCE_DIRS" in updates:
        new_ids = {str(item["id"]) for item in new_strm_sources}
        from app import database as db

        db.cancel_strm_retired_sources(sorted(new_ids))
        for source in old_strm_sources:
            source_id = str(source["id"])
            if source_id in new_ids:
                continue
            db.enqueue_strm_retired_source(
                source_id, str(source.get("name") or source_id), old_strm_root
            )
            retired_source_ids.append(source_id)
    if _AI_RECOGNITION_KEYS & updates.keys():
        try:
            from app.modules.ai_recognition_governance import (
                clear_ai_recognition_governance,
            )

            clear_ai_recognition_governance()
        except Exception as exc:
            logger.warning(
                "AI 识别治理状态重置失败 type=%s", type(exc).__name__
            )
    recognition_web_runtime_keys = _ORGANIZE_TAVILY_KEYS | {
        "TAVILY_API_KEY",
        "TAVILY_TIMEOUT_SECONDS",
        "TAVILY_CACHE_TTL_SECONDS",
    }
    if recognition_web_runtime_keys & updates.keys():
        try:
            from app.modules.recognition_web_hints import (
                clear_recognition_web_hint_cache,
            )

            clear_recognition_web_hint_cache()
        except Exception as exc:
            logger.warning(
                "整理标题线索缓存重置失败 type=%s", type(exc).__name__
            )
    if {"LOGIN_WALLPAPER_MODE", "TMDB_API_KEY"} & updates.keys():
        from app.modules.login_wallpaper import schedule_login_wallpaper_refresh

        schedule_login_wallpaper_refresh(force=True)
    if _DISCOVERY_RUNTIME_KEYS & updates.keys():
        from app.discovery.service import shutdown_discovery_service
        from app.discovery.search import shutdown_discovery_search_service

        shutdown_discovery_service()
        shutdown_discovery_search_service()
        try:
            from app.modules.recognition_hints import clear_recognition_hint_cache
            clear_recognition_hint_cache()
        except Exception:
            logger.warning("自动识别线索缓存重置失败", exc_info=True)
    if any(key.startswith("INDEXER_") for key in updates):
        try:
            from app.indexers.runtime import (
                run_indexer_awaitable_sync,
                shutdown_indexer_service,
            )

            run_indexer_awaitable_sync(
                shutdown_indexer_service(),
                timeout_seconds=_INDEXER_RUNTIME_REFRESH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "Web Indexer 配置热更新失败 type=%s", type(exc).__name__
            )
        try:
            from app.modules.telegram_resource_search import (
                shutdown_telegram_indexer_worker,
            )

            if not shutdown_telegram_indexer_worker(
                timeout=_INDEXER_RUNTIME_REFRESH_TIMEOUT_SECONDS
            ):
                logger.warning("Telegram Indexer 配置热更新超时")
        except Exception as exc:
            logger.warning(
                "Telegram Indexer 配置热更新失败 type=%s", type(exc).__name__
            )
    bot_restart_ms = 0
    warnings: list[str] = []
    background_services_enabled = getattr(
        request.app.state, "background_services_enabled", False
    )
    changed_keys = persisted_updates.keys()
    bot_connection_keys = {"TG_BOT_TOKEN", "TG_CHAT_ID"}
    bot_agent_menu_keys = {"AGENT_ENABLED", "TG_AGENT_ENABLED"}
    if bot_connection_keys & changed_keys:
        bot_restart_started = time.perf_counter()
        try:
            if background_services_enabled:
                from app.bot import restart_bot

                if not restart_bot():
                    warning = (
                        "Telegram Bot 配置已保存，但旧连接未及时退出；"
                        "请稍后重试或重启 MediaFlux 服务"
                    )
                    warnings.append(warning)
                    logger.warning("Telegram Bot 配置热更新未完成：旧 polling 仍在退出")
            else:
                from app.notifier import reset

                reset()
        except Exception as exc:
            warnings.append(
                "Telegram Bot 配置已保存，但运行中实例热更新失败；"
                "请稍后重试或重启 MediaFlux 服务"
            )
            logger.warning("Telegram Bot 配置热更新失败 type=%s", type(exc).__name__)
        finally:
            bot_restart_ms = max(1, round((time.perf_counter() - bot_restart_started) * 1000))
    elif background_services_enabled and bot_agent_menu_keys & changed_keys:
        try:
            from app.bot.handlers import request_command_menu_refresh

            request_command_menu_refresh()
        except Exception as exc:
            logger.warning(
                "Telegram Bot 命令菜单后台刷新失败 type=%s", type(exc).__name__
            )
    if "AGENT_ENABLED" in changed_keys and background_services_enabled:
        try:
            from app.modules.agent_runtime import request_agent_runtime_reconcile

            request_agent_runtime_reconcile()
        except Exception as exc:
            logger.warning(
                "Agent 总开关后台热更新启动失败 type=%s", type(exc).__name__
            )
    from app.services import clear_dashboard_cache

    clear_dashboard_cache()
    media_proxy_keys = {
        "JELLYFIN_ENABLED", "JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_USER_ID",
        "EMBY_ENABLED", "EMBY_URL", "EMBY_TOKEN", "EMBY_USER_ID",
    }
    if media_proxy_keys & updates.keys():
        manager = getattr(request.app.state, "media_proxy_manager", None)
        if manager is not None and getattr(request.app.state, "background_services_enabled", False):
            try:
                manager.request_reconcile()
            except Exception as exc:
                logger.warning("媒体反代配置热加载失败 type=%s", type(exc).__name__)
    strm_keys = {key for key in updates if key.startswith("STRM_") or key.startswith("GY_STRM_")}
    organize_keys = {key for key in updates if key.startswith("GY_ORGANIZE_")}
    agent_patrol_keys = {
        key for key in updates if key.startswith("AGENT_LIBRARY_PATROL_")
    }
    if strm_keys:
        try:
            from app.modules.scheduler import get_scheduler
            scheduler = get_scheduler()
            scheduler.reload()
            if retired_source_ids and getattr(
                request.app.state, "background_services_enabled", False
            ):
                result = scheduler.trigger("config-retirement")
                if not result.get("ok"):
                    logger.info(
                        "STRM 退役来源已入队，等待下次同步处理: %s",
                        result.get("error", "任务繁忙"),
                    )
        except Exception as exc:
            logger.warning("STRM 调度配置热加载失败: %s", exc)
    if organize_keys:
        try:
            from app.modules.organize_scheduler import get_organize_scheduler
            get_organize_scheduler().reload()
        except Exception as exc:
            logger.warning("整理调度配置热加载失败: %s", exc)
        if _NSFW_ORGANIZE_KEYS & updates.keys():
            try:
                from app.modules.nsfw import clear_nsfw_cache
                clear_nsfw_cache()
            except Exception as exc:
                logger.warning("MetaTube 识别缓存重置失败: %s", exc)
    patrol_reload_ms = 0
    if agent_patrol_keys:
        patrol_reload_started = time.perf_counter()
        try:
            from app.modules.agent_library_patrol_scheduler import (
                get_agent_library_patrol_scheduler,
            )

            get_agent_library_patrol_scheduler().reload(immediate=False)
        except Exception as exc:
            logger.warning(
                "Agent 全库缺集巡检配置热加载失败 type=%s",
                type(exc).__name__,
            )
        finally:
            patrol_reload_ms = max(1, round((time.perf_counter() - patrol_reload_started) * 1000))
    total_ms = max(1, round((time.perf_counter() - request_started) * 1000))
    logger.info(
        "配置保存完成 changed=%s persist_ms=%s bot_restart_ms=%s patrol_reload_ms=%s total_ms=%s",
        len(persisted_updates), persist_ms, bot_restart_ms, patrol_reload_ms, total_ms,
    )
    result: dict[str, object] = {"success": True}
    if warnings:
        result["warnings"] = warnings
    return result


@router.get("/update/check", name="api.update_check")
def api_update_check(request: Request):
    require_api_login(request)
    from app.modules.update_check import UpdateCheckError, check_for_updates

    try:
        update_info = check_for_updates(timeout=8.0)
        return {"success": True, "update": update_info.as_dict()}
    except UpdateCheckError as exc:
        return api_error(str(exc), 502)
    except Exception as exc:
        return api_error(f"检查更新失败：{type(exc).__name__}", 500)
