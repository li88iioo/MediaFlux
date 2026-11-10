"""非敏感策略白名单、预检和配置 CAS；与设置页共用运行时刷新。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any

from app import config
from app.modules.feature_config import reload_runtime


class PolicyConflict(ValueError):
    """确认事实或文件 CAS 已变化，未提交本次写入。"""


@dataclass(frozen=True)
class SafePolicyDefinition:
    key: str
    label: str
    kind: str
    default: str | int
    choices: tuple[str, ...] = ()
    minimum: int = 0
    maximum: int = 0
    choice_labels: tuple[tuple[str, str], ...] = ()

    def public_label(self, value: str | int) -> str:
        labels = dict(self.choice_labels)
        return labels.get(str(value), str(value))


_POLICIES: dict[str, SafePolicyDefinition] = {
    "tmdb_match_mode": SafePolicyDefinition(
        key="TMDB_MATCH_MODE",
        label="TMDB 匹配模式",
        kind="choice",
        default="strict",
        choices=("strict", "loose"),
        choice_labels=(("strict", "严格"), ("loose", "宽松")),
    ),
    "login_wallpaper_mode": SafePolicyDefinition(
        key="LOGIN_WALLPAPER_MODE",
        label="登录页壁纸",
        kind="choice",
        default="default",
        choices=("default", "tmdb"),
        choice_labels=(("default", "默认"), ("tmdb", "TMDB 每日电影")),
    ),
    "web_search_depth": SafePolicyDefinition(
        key="TAVILY_SEARCH_DEPTH",
        label="网页搜索深度",
        kind="choice",
        default="basic",
        choices=("basic", "advanced"),
        choice_labels=(("basic", "基础"), ("advanced", "高级")),
    ),
    "web_search_max_results": SafePolicyDefinition(
        key="TAVILY_MAX_RESULTS",
        label="网页搜索单次结果上限",
        kind="integer",
        default=5,
        minimum=1,
        maximum=10,
    ),
    "web_search_timeout_seconds": SafePolicyDefinition(
        key="TAVILY_TIMEOUT_SECONDS",
        label="网页搜索请求超时",
        kind="integer",
        default=10,
        minimum=2,
        maximum=30,
    ),
    "web_search_cache_ttl_seconds": SafePolicyDefinition(
        key="TAVILY_CACHE_TTL_SECONDS",
        label="网页搜索缓存时间",
        kind="integer",
        default=900,
        minimum=30,
        maximum=86400,
    ),
    "web_read_timeout_seconds": SafePolicyDefinition(
        key="WEB_READ_TIMEOUT_SECONDS",
        label="本地网页读取总超时",
        kind="integer",
        default=10,
        minimum=2,
        maximum=30,
    ),
    "web_read_cache_ttl_seconds": SafePolicyDefinition(
        key="WEB_READ_CACHE_TTL_SECONDS",
        label="本地网页读取缓存时间",
        kind="integer",
        default=900,
        minimum=30,
        maximum=86400,
    ),
    "web_search_daily_credit_limit": SafePolicyDefinition(
        key="TAVILY_DAILY_CREDIT_LIMIT",
        label="网页搜索每日额度",
        kind="integer",
        default=100,
        minimum=1,
        maximum=100000,
    ),
    "discovery_cache_ttl_seconds": SafePolicyDefinition(
        key="DISCOVERY_CACHE_TTL_SECONDS",
        label="媒体探索缓存时间",
        kind="integer",
        default=21600,
        minimum=60,
        maximum=604800,
    ),
    "discovery_stale_ttl_seconds": SafePolicyDefinition(
        key="DISCOVERY_STALE_TTL_SECONDS",
        label="媒体探索旧缓存保留时间",
        kind="integer",
        default=604800,
        minimum=300,
        maximum=2592000,
    ),
    "douban_cache_ttl_seconds": SafePolicyDefinition(
        key="DOUBAN_CACHE_TTL_SECONDS",
        label="豆瓣探索缓存时间",
        kind="integer",
        default=21600,
        minimum=300,
        maximum=604800,
    ),
    "indexer_btbtla_min_interval_seconds": SafePolicyDefinition(
        key="INDEXER_BTBTLA_MIN_INTERVAL_SECONDS",
        label="BTBTLA 最小请求间隔",
        kind="integer",
        default=5,
        minimum=0,
        maximum=60,
    ),
}
POLICY_BY_KEY: dict[str, SafePolicyDefinition] = {item.key: item for item in _POLICIES.values()}


def _read_value(definition: SafePolicyDefinition) -> str | int:
    if definition.kind == "choice":
        value = config.get(definition.key, str(definition.default)) or ""
        normalized = value.strip().lower()
        return normalized if normalized in definition.choices else definition.default
    return config.get_int_clamped(definition.key, int(definition.default), definition.minimum, definition.maximum)


def policy_int(key: str) -> int:
    """运行时、Web 校验与 Agent 写入共用的整数策略读取。"""
    return int(_read_value(POLICY_BY_KEY[key]))


def _public_item(policy: str, value: str | int) -> dict[str, Any]:
    definition = _POLICIES[policy]
    return {
        "policy": policy,
        "label": definition.label,
        "value": value,
        "display_value": definition.public_label(value),
        "managed_by_environment": config.has_external_override(definition.key),
    }


def _capture(arguments: dict[str, Any]) -> dict[str, Any]:
    policy = arguments["policy"]
    requested = arguments["value"]
    definition = _POLICIES[policy]
    snapshot, values = config.read_env_snapshot(config.ENV_FILE)
    current = _read_value(definition)
    peer_policy = ""
    if policy == "discovery_cache_ttl_seconds":
        peer_policy = "discovery_stale_ttl_seconds"
    elif policy == "discovery_stale_ttl_seconds":
        peer_policy = "discovery_cache_ttl_seconds"
    peer_definition = _POLICIES.get(peer_policy)
    peer_value = _read_value(peer_definition) if peer_definition is not None else None
    return {
        "snapshot": snapshot,
        "snapshot_present": snapshot is not None,
        "snapshot_sha256": hashlib.sha256(snapshot or b"").hexdigest(),
        "policy": policy,
        "persisted": values.get(definition.key, "<unset>"),
        "current": current,
        "requested": requested,
        "external_override": config.has_external_override(definition.key),
        "peer_policy": peer_policy,
        "peer_value": peer_value,
        "peer_external_override": config.has_external_override(peer_definition.key)
        if peer_definition is not None
        else False,
        "tmdb_key_configured": bool((config.get("TMDB_API_KEY", "") or "").strip()),
    }


def _precondition_failure(state: dict[str, Any]) -> dict | None:
    definition = _POLICIES[state["policy"]]
    if state["external_override"]:
        return {
            "ok": False,
            "status": "environment_override",
            "summary": f"{definition.label}由运行环境管理，Agent 未修改配置",
            "error": "目标策略由运行环境覆盖。",
        }
    if state["current"] == state["requested"]:
        return {
            "ok": True,
            "status": "no_changes",
            "summary": f"{definition.label}已经是{definition.public_label(state['requested'])}",
        }
    if (
        state["policy"] == "login_wallpaper_mode"
        and state["requested"] == "tmdb"
        and (not state["tmdb_key_configured"])
    ):
        return {
            "ok": False,
            "status": "precondition_failed",
            "summary": "登录页暂时不能切换为 TMDB 每日电影",
            "error": "请先在设置中配置 TMDB API Key。",
        }
    if state["policy"] == "discovery_cache_ttl_seconds" and int(
        state["requested"]
    ) > int(state["peer_value"]):
        return {
            "ok": False,
            "status": "precondition_failed",
            "summary": "媒体探索缓存时间不能超过旧缓存保留时间",
            "error": "请先增大旧缓存保留时间，或使用更小的缓存时间。",
        }
    if state["policy"] == "discovery_stale_ttl_seconds" and int(
        state["requested"]
    ) < int(state["peer_value"]):
        return {
            "ok": False,
            "status": "precondition_failed",
            "summary": "媒体探索旧缓存保留时间不能短于缓存时间",
            "error": "请使用不小于当前媒体探索缓存时间的值。",
        }
    return None


def _effects(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    policy = state["policy"]
    definition = _POLICIES[policy]
    requested = state["requested"]
    effects = [
        f"将{definition.label}从{definition.public_label(state['current'])}改为{definition.public_label(requested)}"
    ]
    suggestions = ["本次写入仅修改这一项固定白名单策略，不会读取或改写其他配置。"]
    if policy == "tmdb_match_mode":
        effects.append("刷新当前进程的探索与识别缓存；不会立即刮削或移动媒体")
        if requested == "loose":
            suggestions.append(
                "宽松模式会降低匹配门槛，请留意识别结果可能出现更多相似标题候选。"
            )
    elif policy == "login_wallpaper_mode":
        effects.append(
            "重新安排登录页壁纸刷新；TMDB 模式可能在后台请求一张公开图片，不会影响登录凭据或当前会话"
        )
    elif policy in {
        "web_search_depth",
        "web_search_max_results",
        "web_search_timeout_seconds",
        "web_search_cache_ttl_seconds",
        "web_read_timeout_seconds",
        "web_read_cache_ttl_seconds",
    }:
        effects.append("清空共享的网页搜索与读取缓存；不会发起联网请求或消耗额度")
        if policy == "web_search_depth" and requested == "advanced":
            suggestions.append("高级搜索每次请求会消耗 2 个 Tavily 额度。")
    elif policy == "web_search_daily_credit_limit":
        effects.append("新额度会在下一次网页搜索请求时生效；不会立即联网或消耗额度")
    elif policy in {
        "discovery_cache_ttl_seconds",
        "discovery_stale_ttl_seconds",
        "douban_cache_ttl_seconds",
    }:
        effects.append("重建当前进程的探索与探索搜索服务；不会立即访问外部数据源")
    elif policy == "indexer_btbtla_min_interval_seconds":
        effects.append("刷新 Web 与 Telegram 的资源站点运行时；不会立即搜索或提交下载")
        if requested == 0:
            suggestions.append("0 秒会取消最小间隔，可能更容易触发站点限流或临时封禁。")
    return (effects, suggestions)


def _preview_from_state(state: dict[str, Any]) -> dict:
    failed = _precondition_failure(state)
    if failed is not None:
        return failed
    definition = _POLICIES[state["policy"]]
    effects, suggestions = _effects(state)
    return {
        "ok": True,
        "status": "confirmation_required",
        "summary": f"确认后将把{definition.label}改为{definition.public_label(state['requested'])}",
        "data": {
            "policy": state["policy"],
            "current_value": state["current"],
            "requested_value": state["requested"],
            "effects": effects,
        },
        "suggestions": suggestions,
    }


def normalize_policy(policy: str, value: str | int) -> dict[str, Any]:
    if not isinstance(policy, str) or policy not in _POLICIES:
        raise ValueError("policy 不在允许的策略列表中")
    definition = _POLICIES[policy]
    if definition.kind == "choice":
        if (
            not isinstance(value, str)
            or value.strip().lower() not in definition.choices
        ):
            raise ValueError(
                f"{definition.label}仅支持：{', '.join(definition.choices)}"
            )
        value = value.strip().lower()
    elif (
        type(value) is not int or not definition.minimum <= value <= definition.maximum
    ):
        raise ValueError(
            f"{definition.label}必须为 {definition.minimum} 到 {definition.maximum} 的整数"
        )
    return {"policy": policy, "value": value}


def policies() -> dict[str, Any]:
    with (
        config.runtime_config_read_guard(),
        config.use_runtime_config_snapshot(config.capture_runtime_config_snapshot()),
    ):
        items = [
            _public_item(policy, _read_value(definition))
            for policy, definition in _POLICIES.items()
        ]
    return {
        "ok": True,
        "status": "ready",
        "summary": f"当前可管理 {len(items)} 项非敏感策略",
        "data": {"policies": items, "policy_count": len(items)},
    }


def _prepare(policy: str, value: str | int) -> tuple[bytes | None, dict[str, Any]]:
    arguments = normalize_policy(policy, value)
    with config.use_runtime_config_snapshot(config.capture_runtime_config_snapshot()):
        state = _capture(arguments)
    fingerprint = hashlib.sha256(
        json.dumps(
            {key: value for key, value in state.items() if key != "snapshot"},
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return (
        state["snapshot"],
        {
            "arguments": arguments,
            "fingerprint": fingerprint,
            "preview": _preview_from_state(state),
        },
    )


def prepare_policy(policy: str, value: str | int) -> dict[str, Any]:
    with config.runtime_config_read_guard():
        return _prepare(policy, value)[1]


def set_policy(
    policy: str,
    value: str | int,
    *,
    expected_fingerprint: str,
    check_write: Callable[[], None] | None = None,
    admission: Callable[[], AbstractContextManager] = nullcontext,
) -> dict[str, Any]:
    with admission(), config.runtime_config_read_guard():
        raw, prepared = _prepare(policy, value)
        if prepared["fingerprint"] != expected_fingerprint:
            raise PolicyConflict("策略配置已变化，请重新预检")
        if prepared["preview"]["status"] != "confirmation_required":
            return prepared["preview"]
        definition = _POLICIES[policy]
        requested = str(prepared["arguments"]["value"])
        if check_write:
            check_write()
        try:
            config.update_runtime_env_file(
                config.ENV_FILE, {definition.key: requested}, expected=raw
            )
        except (
            config.ConcurrentConfigUpdateError,
            config.ExternalConfigOverrideError,
        ) as exc:
            raise PolicyConflict("策略配置或部署覆盖已变化，请重新预检") from exc
    # Agent 写入只发生在运行中的服务内。
    warnings, restart = reload_runtime({definition.key}, background_services=True)
    _, persisted = config.read_env_snapshot(config.ENV_FILE)
    verified = persisted.get(definition.key) == requested
    ok = verified and (not restart)
    return {
        "ok": ok,
        "status": "completed" if ok else "outcome_unknown",
        "summary": f"已将{definition.label}改为{definition.public_label(requested)}"
        if ok
        else "策略已提交，配置或运行时状态待核验",
        "data": {
            "policy": policy,
            "value": prepared["arguments"]["value"],
            "saved": verified,
            "runtime_refreshed": not restart,
            "runtime_scope": "current_process",
            "verification_state": "verified" if verified else "pending",
        },
        "suggestions": [*warnings, *prepared["preview"].get("suggestions", [])],
        **({"error": "配置或运行时状态待核验"} if not ok else {}),
    }


POLICY_IDS = tuple(_POLICIES)
