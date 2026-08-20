"""Media Agent 的通用非敏感策略读取与确认写入。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import secrets
from typing import Any, Callable

from app import config
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.logger import get_logger

logger = get_logger(__name__)


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
_ALLOWED_ARGUMENTS = frozenset({"policy", "value"})
SAFE_POLICY_IDS = tuple(_POLICIES)
_RUNTIME_REFRESH_TIMEOUT_SECONDS = 5.0


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_policy_summary_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(arguments))}")
    return {}


def safe_policy_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _ALLOWED_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    if set(arguments) != _ALLOWED_ARGUMENTS:
        raise AgentToolError("policy 与 value 均为必填项")

    policy = arguments.get("policy")
    if not isinstance(policy, str) or policy not in _POLICIES:
        raise AgentToolError("policy 不在允许的安全策略列表中")
    definition = _POLICIES[policy]
    value = arguments.get("value")
    if definition.kind == "choice":
        if not isinstance(value, str):
            raise AgentToolError("value 必须是字符串")
        normalized_value = value.strip().lower()
        if normalized_value not in definition.choices:
            raise AgentToolError(
                f"{definition.label}仅支持：{', '.join(definition.choices)}"
            )
        value = normalized_value
    elif definition.kind == "integer":
        if type(value) is not int:
            raise AgentToolError("value 必须是整数")
        if value < definition.minimum or value > definition.maximum:
            raise AgentToolError(
                f"{definition.label}必须为 {definition.minimum} 到 {definition.maximum}"
            )
    else:  # pragma: no cover - 静态定义防御
        raise AgentToolError("目标策略定义无效", code="unavailable")
    return {"policy": policy, "value": value}


def _read_value(definition: SafePolicyDefinition) -> str | int:
    if definition.kind == "choice":
        value = str(config.get(definition.key, str(definition.default)) or "")
        normalized = value.strip().lower()
        return normalized if normalized in definition.choices else definition.default
    value = config.get_int(definition.key, int(definition.default))
    return max(definition.minimum, min(value, definition.maximum))


def _public_item(policy: str, value: str | int) -> dict[str, Any]:
    definition = _POLICIES[policy]
    return {
        "policy": policy,
        "label": definition.label,
        "value": value,
        "display_value": definition.public_label(value),
        "managed_by_environment": config.has_external_override(definition.key),
    }


def summarize_safe_policies(_arguments: dict[str, Any]) -> ToolResult:
    items = [
        _public_item(policy, _read_value(definition))
        for policy, definition in _POLICIES.items()
    ]
    return ToolResult(
        ok=True,
        status="ready",
        summary=f"当前可安全管理 {len(items)} 项非敏感策略",
        data={"policies": items, "policy_count": len(items)},
        evidence=[Evidence(
            "server_configuration",
            "仅返回固定白名单策略的公开值与环境托管标记；未读取或返回凭据、地址、路径及其他配置。",
            _now(),
        )],
        suggestions=["修改任一策略都需要先预检并使用一次性确认票据。"],
    )


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
        "peer_external_override": (
            config.has_external_override(peer_definition.key)
            if peer_definition is not None
            else False
        ),
        "tmdb_key_configured": bool(str(config.get("TMDB_API_KEY", "") or "").strip()),
    }


def _fingerprint(state: dict[str, Any]) -> str:
    payload = {
        "snapshot_present": state["snapshot_present"],
        "snapshot_sha256": state["snapshot_sha256"],
        "policy": state["policy"],
        "persisted": state["persisted"],
        "current": state["current"],
        "requested": state["requested"],
        "external_override": state["external_override"],
        "peer_policy": state["peer_policy"],
        "peer_value": state["peer_value"],
        "peer_external_override": state["peer_external_override"],
        "tmdb_key_configured": state["tmdb_key_configured"],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _precondition_failure(state: dict[str, Any]) -> ToolResult | None:
    definition = _POLICIES[state["policy"]]
    if state["external_override"]:
        return ToolResult(
            ok=False,
            status="environment_override",
            summary=f"{definition.label}由运行环境管理，Agent 未修改配置",
            error="目标策略由运行环境覆盖。",
        )
    if state["current"] == state["requested"]:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary=f"{definition.label}已经是{definition.public_label(state['requested'])}",
            error="目标策略无需修改。",
        )
    if (
        state["policy"] == "login_wallpaper_mode"
        and state["requested"] == "tmdb"
        and not state["tmdb_key_configured"]
    ):
        return ToolResult(
            ok=False,
            status="precondition_failed",
            summary="登录页暂时不能切换为 TMDB 每日电影",
            error="请先在设置中配置 TMDB API Key。",
        )
    if (
        state["policy"] == "discovery_cache_ttl_seconds"
        and int(state["requested"]) > int(state["peer_value"])
    ):
        return ToolResult(
            ok=False,
            status="precondition_failed",
            summary="媒体探索缓存时间不能超过旧缓存保留时间",
            error="请先增大旧缓存保留时间，或使用更小的缓存时间。",
        )
    if (
        state["policy"] == "discovery_stale_ttl_seconds"
        and int(state["requested"]) < int(state["peer_value"])
    ):
        return ToolResult(
            ok=False,
            status="precondition_failed",
            summary="媒体探索旧缓存保留时间不能短于缓存时间",
            error="请使用不小于当前媒体探索缓存时间的值。",
        )
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
            suggestions.append("宽松模式会降低匹配门槛，请留意识别结果可能出现更多相似标题候选。")
    elif policy == "login_wallpaper_mode":
        effects.append(
            "重新安排登录页壁纸刷新；TMDB 模式可能在后台请求一张公开图片，"
            "不会影响登录凭据或当前会话"
        )
    elif policy in {
        "web_search_depth",
        "web_search_max_results",
        "web_search_timeout_seconds",
        "web_search_cache_ttl_seconds",
    }:
        effects.append("清空当前进程的网页搜索结果缓存；不会发起联网搜索或消耗额度")
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
    return effects, suggestions


def _preview_from_state(state: dict[str, Any]) -> ToolResult:
    failed = _precondition_failure(state)
    if failed is not None:
        return failed
    definition = _POLICIES[state["policy"]]
    effects, suggestions = _effects(state)
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=(
            f"确认后将把{definition.label}改为"
            f"{definition.public_label(state['requested'])}"
        ),
        data={
            "policy": state["policy"],
            "current_value": state["current"],
            "requested_value": state["requested"],
            "effects": effects,
        },
        evidence=[Evidence(
            "server_configuration",
            "仅比较目标白名单策略、必要前置条件与配置快照；未返回内部配置键或任何敏感值。",
            _now(),
        )],
        suggestions=suggestions,
    )


def preview_set_safe_policy(arguments: dict[str, Any]) -> ToolResult:
    return _preview_from_state(_capture(arguments))


def safe_policy_confirmation_context(arguments: dict[str, Any]) -> str:
    return _fingerprint(_capture(arguments))


def prepare_safe_policy_confirmation(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    state = _capture(arguments)
    return _preview_from_state(state), _fingerprint(state)


def _build_updates(arguments: dict[str, Any]) -> dict[str, str]:
    definition = _POLICIES[arguments["policy"]]
    return {definition.key: str(arguments["value"])}


def _refresh_tmdb_runtime() -> bool:
    try:
        from app.discovery.search import shutdown_discovery_search_service
        from app.discovery.service import shutdown_discovery_service
        from app.modules.recognition_hints import clear_recognition_hint_cache

        shutdown_discovery_service()
        shutdown_discovery_search_service()
        clear_recognition_hint_cache()
        return True
    except Exception as exc:
        logger.warning("Agent TMDB 策略运行时刷新失败 type=%s", type(exc).__name__)
        return False


def _refresh_wallpaper_runtime() -> bool:
    try:
        from app.modules.login_wallpaper import schedule_login_wallpaper_refresh

        return bool(schedule_login_wallpaper_refresh(force=True))
    except Exception as exc:
        logger.warning("Agent 登录壁纸运行时刷新失败 type=%s", type(exc).__name__)
        return False


def _refresh_web_search_runtime() -> bool:
    try:
        from app.agent.web_search_actions import clear_web_search_cache

        clear_web_search_cache()
        return True
    except Exception as exc:
        logger.warning("Agent网页搜索策略运行时刷新失败 type=%s", type(exc).__name__)
        return False


def _refresh_discovery_runtime() -> bool:
    try:
        from app.discovery.search import shutdown_discovery_search_service
        from app.discovery.service import shutdown_discovery_service

        shutdown_discovery_service()
        shutdown_discovery_search_service()
        return True
    except Exception as exc:
        logger.warning("Agent 探索缓存策略运行时刷新失败 type=%s", type(exc).__name__)
        return False


def _refresh_not_required() -> bool:
    # 每日额度在每次搜索请求时读取配置，无需主动重建运行时对象。
    return True


def _refresh_indexer_runtime() -> bool:
    web_refreshed = True
    telegram_refreshed = True
    try:
        from app.indexers.runtime import (
            run_indexer_awaitable_sync,
            shutdown_indexer_service,
        )

        run_indexer_awaitable_sync(
            shutdown_indexer_service(),
            timeout_seconds=_RUNTIME_REFRESH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        web_refreshed = False
        logger.warning("Agent BTBTLA Web 运行时刷新失败 type=%s", type(exc).__name__)
    try:
        from app.modules.telegram_resource_search import shutdown_telegram_indexer_worker

        telegram_refreshed = shutdown_telegram_indexer_worker(
            timeout=_RUNTIME_REFRESH_TIMEOUT_SECONDS
        )
        if not telegram_refreshed:
            logger.warning("Agent BTBTLA Telegram 运行时刷新超时")
    except Exception as exc:
        telegram_refreshed = False
        logger.warning("Agent BTBTLA Telegram 运行时刷新失败 type=%s", type(exc).__name__)
    return web_refreshed and telegram_refreshed


_RUNTIME_REFRESHERS: dict[str, Callable[[], bool]] = {
    "tmdb_match_mode": _refresh_tmdb_runtime,
    "login_wallpaper_mode": _refresh_wallpaper_runtime,
    "web_search_depth": _refresh_web_search_runtime,
    "web_search_max_results": _refresh_web_search_runtime,
    "web_search_timeout_seconds": _refresh_web_search_runtime,
    "web_search_cache_ttl_seconds": _refresh_web_search_runtime,
    "web_search_daily_credit_limit": _refresh_not_required,
    "discovery_cache_ttl_seconds": _refresh_discovery_runtime,
    "discovery_stale_ttl_seconds": _refresh_discovery_runtime,
    "douban_cache_ttl_seconds": _refresh_discovery_runtime,
    "indexer_btbtla_min_interval_seconds": _refresh_indexer_runtime,
}


def set_safe_policy_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    state = _capture(arguments)
    if not secrets.compare_digest(_fingerprint(state), str(expected_context or "")):
        return ToolResult(
            ok=False,
            status="conflict",
            summary="目标策略已被其他操作修改，请重新预检",
            error="配置已变化。",
        )
    failed = _precondition_failure(state)
    if failed is not None:
        return failed

    try:
        config.update_runtime_env_file(
            config.ENV_FILE,
            _build_updates(arguments),
            expected=state["snapshot"],
        )
    except config.ConcurrentConfigUpdateError:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="目标策略已被其他操作修改，请重新预检",
            error="配置已变化。",
        )
    except config.ExternalConfigOverrideError:
        return ToolResult(
            ok=False,
            status="environment_override",
            summary="目标策略由运行环境管理，Agent 未修改配置",
            error="目标策略由运行环境覆盖。",
        )
    except (config.AtomicPublishError, OSError, ValueError):
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="目标策略暂时无法保存",
            error="配置写入失败，请稍后重试。",
        )

    policy = arguments["policy"]
    runtime_refreshed = _RUNTIME_REFRESHERS[policy]()
    definition = _POLICIES[policy]
    _effects_value, suggestions = _effects(state)
    if not runtime_refreshed:
        suggestions.insert(0, "配置已保存，但当前进程未能完整刷新；请重启服务以确保新策略生效。")
    return ToolResult(
        ok=True,
        status="completed",
        summary=(
            f"已将{definition.label}改为"
            f"{definition.public_label(arguments['value'])}"
        ),
        data={
            "policy": policy,
            "runtime_refreshed": runtime_refreshed,
            "runtime_scope": "current_process",
        },
        evidence=[Evidence(
            "server_configuration",
            "使用一次性确认票据与配置快照原子更新一项固定白名单策略。",
            _now(),
        )],
        suggestions=suggestions,
    )


def set_safe_policy(arguments: dict[str, Any]) -> ToolResult:
    del arguments
    raise AgentToolError("该工具需要确认，不能直接执行", code="confirmation_required")


__all__ = [
    "SAFE_POLICY_IDS",
    "prepare_safe_policy_confirmation",
    "preview_set_safe_policy",
    "safe_policy_arguments",
    "safe_policy_confirmation_context",
    "safe_policy_summary_arguments",
    "set_safe_policy",
    "set_safe_policy_confirmed",
    "summarize_safe_policies",
]
