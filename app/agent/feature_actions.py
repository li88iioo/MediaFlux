"""Media Agent 的非敏感功能开关预检与确认写入。"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app import config
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.defaults import DEFAULT_DISCOVERY_ENABLED, DEFAULT_INDEXER_SEARCH_ENABLED
from app.indexers.config import DEFAULT_INDEXER_SITE_IDS, INDEXER_SITE_ORDER
from app.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class FeatureDefinition:
    key: str
    label: str
    default: bool
    effect_note: str = ""
    restart_discovery: bool = False
    requires_indexer_sites: bool = False
    requires_tavily_key: bool = False


_FEATURES: dict[str, FeatureDefinition] = {
    "discovery": FeatureDefinition(
        key="DISCOVERY_ENABLED",
        label="媒体探索",
        default=DEFAULT_DISCOVERY_ENABLED,
        restart_discovery=True,
    ),
    "douban": FeatureDefinition(
        key="DISCOVERY_DOUBAN_ENABLED",
        label="豆瓣探索",
        default=True,
        restart_discovery=True,
    ),
    "resource_results": FeatureDefinition(
        key="DISCOVERY_RESOURCE_RESULTS_ENABLED",
        label="探索页站点资源结果",
        default=True,
        requires_indexer_sites=True,
    ),
    "indexer_search": FeatureDefinition(
        key="INDEXER_SEARCH_ENABLED",
        label="多站资源搜索",
        default=DEFAULT_INDEXER_SEARCH_ENABLED,
        requires_indexer_sites=True,
    ),
    "web_search": FeatureDefinition(
        key="WEB_SEARCH_ENABLED",
        label="联网搜索",
        default=False,
        effect_note="仅影响后续显式联网搜索；本次配置写入不会访问 Tavily 或消耗搜索额度",
        requires_tavily_key=True,
    ),
    "offline_magnet": FeatureDefinition(
        key="OFFLINE_MAGNET_ENABLED",
        label="光鸭磁力链接离线转存",
        default=True,
        effect_note="仅影响后续收到的 magnet 链接，不会重新处理历史消息",
    ),
    "offline_ed2k": FeatureDefinition(
        key="OFFLINE_ED2K_ENABLED",
        label="光鸭 ED2K 离线转存",
        default=True,
        effect_note="仅影响后续收到的 ED2K 链接，不会重新处理历史消息",
    ),
    "offline_http": FeatureDefinition(
        key="OFFLINE_HTTP_ENABLED",
        label="光鸭 HTTP 链接离线转存",
        default=False,
        effect_note="仅影响后续收到的 HTTP(S) 下载链接，不会重新处理历史消息",
    ),
    "strm_metadata": FeatureDefinition(
        key="STRM_METADATA_ENABLED",
        label="STRM 伴随元数据同步",
        default=False,
        effect_note="从下一次 STRM 同步起生效，不会立即启动同步任务",
    ),
    "download_verification_notify": FeatureDefinition(
        key="AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED",
        label="下载后入库复核通知",
        default=True,
        effect_note="仅影响后续复核结果通知，不会启动、暂停或删除下载任务",
    ),
}
_INDEXER_SITE_IDS = frozenset(INDEXER_SITE_ORDER)
_ALLOWED_ARGUMENTS = {"feature", "enabled"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def feature_state_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _ALLOWED_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    feature = arguments.get("feature")
    enabled = arguments.get("enabled")
    if not isinstance(feature, str):
        raise AgentToolError("feature 必须是字符串")
    if feature not in _FEATURES:
        raise AgentToolError("feature 不在允许的功能开关列表中")
    if not isinstance(enabled, bool):
        raise AgentToolError("enabled 必须是布尔值")
    return {"feature": feature, "enabled": enabled}


def feature_summary_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(arguments))}")
    return {}


def _enabled_sites() -> tuple[str, ...]:
    configured = config.get(
        "INDEXER_ENABLED_SITES",
        ",".join(DEFAULT_INDEXER_SITE_IDS),
    )
    requested = {
        part.strip().lower()
        for part in str(configured or "").split(",")
        if part.strip()
    }
    if config.get_bool("INDEXER_SUKEBEI_ENABLED", False):
        requested.add("sukebei")
    return tuple(sorted(requested & _INDEXER_SITE_IDS))


def summarize_feature_states(_arguments: dict[str, Any]) -> ToolResult:
    """投影非敏感功能状态，不暴露配置键、配置值或站点明细。"""
    discovery_enabled = config.get_bool(
        _FEATURES["discovery"].key, _FEATURES["discovery"].default
    )
    indexer_enabled = config.get_bool(
        _FEATURES["indexer_search"].key,
        _FEATURES["indexer_search"].default,
    )
    has_indexer_sites = bool(_enabled_sites())
    tavily_configured = bool(str(config.get("TAVILY_API_KEY", "") or "").strip())
    features: list[dict[str, Any]] = []

    for feature, definition in _FEATURES.items():
        enabled = config.get_bool(definition.key, definition.default)
        reasons: list[str] = []
        if not enabled:
            availability = "disabled"
            reasons.append("feature_disabled")
        else:
            if feature in {"douban", "resource_results"} and not discovery_enabled:
                reasons.append("parent_disabled")
            if feature == "resource_results" and not indexer_enabled:
                reasons.append("search_disabled")
            if definition.requires_indexer_sites and not has_indexer_sites:
                reasons.append("no_enabled_sites")
            if definition.requires_tavily_key and not tavily_configured:
                reasons.append("provider_not_configured")
            availability = "blocked" if reasons else "available"

        features.append(
            {
                "feature": feature,
                "label": definition.label,
                "enabled": enabled,
                "availability": availability,
                "reason_codes": reasons,
                "managed_by_environment": config.has_external_override(definition.key),
            }
        )

    available_count = sum(item["availability"] == "available" for item in features)
    disabled_count = sum(item["availability"] == "disabled" for item in features)
    blocked_count = sum(item["availability"] == "blocked" for item in features)
    enabled_count = sum(item["enabled"] for item in features)
    if blocked_count:
        status = "attention"
    elif available_count == 0:
        status = "disabled"
    else:
        status = "ready"
    summary_parts = [f"{available_count} 项可用"]
    if disabled_count:
        summary_parts.append(f"{disabled_count} 项已关闭")
    if blocked_count:
        summary_parts.append(f"{blocked_count} 项受依赖阻塞")

    suggestions: list[str] = []
    if any("parent_disabled" in item["reason_codes"] for item in features):
        suggestions.append("依赖媒体探索的功能需先开启媒体探索。")
    if any("search_disabled" in item["reason_codes"] for item in features):
        suggestions.append("探索页资源结果需同时开启多站资源搜索。")
    if any("no_enabled_sites" in item["reason_codes"] for item in features):
        suggestions.append("资源搜索相关功能需至少启用一个资源站点。")
    if any("provider_not_configured" in item["reason_codes"] for item in features):
        suggestions.append("联网搜索需先在设置页配置 Tavily API Key。")

    return ToolResult(
        ok=True,
        status=status,
        summary="功能状态：" + "，".join(summary_parts),
        data={
            "feature_count": len(features),
            "enabled_count": enabled_count,
            "available_count": available_count,
            "disabled_count": disabled_count,
            "attention_count": blocked_count,
            "features": features,
        },
        evidence=[
            Evidence(
                "server_configuration",
                "仅汇总白名单功能的布尔状态、依赖可用性与环境托管标记；未返回配置键或配置值。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )


def _capture(arguments: dict[str, Any]) -> dict[str, Any]:
    feature = arguments["feature"]
    definition = _FEATURES[feature]
    snapshot, values = config.read_env_snapshot(config.ENV_FILE)
    current_enabled = config.get_bool(definition.key, definition.default)
    sites = _enabled_sites() if definition.requires_indexer_sites else ()
    return {
        "feature": feature,
        "definition": definition,
        "snapshot": snapshot,
        "snapshot_present": snapshot is not None,
        "snapshot_sha256": hashlib.sha256(snapshot or b"").hexdigest(),
        "persisted": values.get(definition.key, "<unset>"),
        "current_enabled": current_enabled,
        "requested_enabled": arguments["enabled"],
        "external_override": config.has_external_override(definition.key),
        "enabled_site_count": len(sites),
        "discovery_enabled": config.get_bool("DISCOVERY_ENABLED"),
        "indexer_enabled": config.get_bool("INDEXER_SEARCH_ENABLED"),
        "tavily_configured": bool(str(config.get("TAVILY_API_KEY", "") or "").strip()),
    }


def _precondition_failure(state: dict[str, Any]) -> ToolResult | None:
    definition: FeatureDefinition = state["definition"]
    if state["external_override"]:
        return ToolResult(
            ok=False,
            status="environment_override",
            summary=f"{definition.label}由运行环境管理，Agent 不会覆盖部署配置",
            suggestions=["请在部署或服务环境中调整该开关后重启服务。"],
            error="目标功能由运行环境覆盖。",
        )
    if state["current_enabled"] == state["requested_enabled"]:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary=f"{definition.label}已经处于目标状态",
            error="功能状态没有变化。",
        )
    if (
        state["requested_enabled"]
        and definition.requires_indexer_sites
        and state["enabled_site_count"] < 1
    ):
        return ToolResult(
            ok=False,
            status="not_configured",
            summary=f"无法启用{definition.label}：当前没有可用资源站点",
            suggestions=["请先在设置中至少选择一个资源站点。"],
            error="当前没有可用资源站点。",
        )
    if (
        state["requested_enabled"]
        and definition.requires_tavily_key
        and not state["tavily_configured"]
    ):
        return ToolResult(
            ok=False,
            status="not_configured",
            summary=f"无法启用{definition.label}：Tavily 尚未配置",
            suggestions=["请先在设置页配置 Tavily API Key；Agent 不会读取或回显密钥。"],
            error="联网搜索供应商尚未配置。",
        )
    return None


def _effects(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    definition: FeatureDefinition = state["definition"]
    target = state["requested_enabled"]
    effects = [f"将{definition.label}{'开启' if target else '关闭'}"]
    suggestions: list[str] = []
    if definition.restart_discovery:
        effects.append("重建当前进程的探索数据服务，使新状态用于后续请求")
    if definition.effect_note:
        effects.append(definition.effect_note)
    if (
        state["feature"] in {"douban", "resource_results"}
        and target
        and not state["discovery_enabled"]
    ):
        suggestions.append(
            "媒体探索总开关当前关闭；此设置会保存，但需开启媒体探索后才会生效。"
        )
    if (
        state["feature"] == "resource_results"
        and target
        and not state["indexer_enabled"]
    ):
        suggestions.append("多站资源搜索当前关闭；资源结果区域需同时开启多站资源搜索。")
    if not target:
        suggestions.append("关闭功能不会取消已经发出的外部请求。")
    return effects, suggestions


def _preview_feature_state(state: dict[str, Any]) -> ToolResult:
    failed = _precondition_failure(state)
    if failed is not None:
        return failed
    definition: FeatureDefinition = state["definition"]
    effects, suggestions = _effects(state)
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary=f"确认后将{'开启' if state['requested_enabled'] else '关闭'}{definition.label}",
        data={
            "feature": state["feature"],
            "label": definition.label,
            "current_enabled": state["current_enabled"],
            "requested_enabled": state["requested_enabled"],
            "effects": effects,
        },
        evidence=[
            Evidence(
                "server_configuration",
                "仅检查目标功能的状态与非敏感前置条件；未返回配置键或配置值。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )


def _fingerprint_payload(state: dict[str, Any]) -> str:
    payload = {
        "feature": state["feature"],
        "requested_enabled": state["requested_enabled"],
        "current_enabled": state["current_enabled"],
        "snapshot_present": state["snapshot_present"],
        "snapshot_sha256": state["snapshot_sha256"],
        "persisted": state["persisted"],
        "external_override": state["external_override"],
        "enabled_site_count": state["enabled_site_count"],
        "discovery_enabled": state["discovery_enabled"],
        "indexer_enabled": state["indexer_enabled"],
        "tavily_configured": state["tavily_configured"],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def prepare_feature_state_confirmation(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    """在同一配置快照上生成预览和确认指纹。"""
    state = _capture(arguments)
    preview = _preview_feature_state(state)
    return preview, _fingerprint_payload(state) if preview.ok else ""


def _refresh_runtime(definition: FeatureDefinition) -> bool:
    if not definition.restart_discovery:
        return True
    try:
        from app.discovery.search import shutdown_discovery_search_service
        from app.discovery.service import shutdown_discovery_service

        shutdown_discovery_service()
        shutdown_discovery_search_service()
        return True
    except Exception as exc:
        logger.warning("Agent 功能开关运行时刷新失败 type=%s", type(exc).__name__)
        return False


def verify_feature_state_write(
    arguments: dict[str, Any],
    result: ToolResult,
) -> ToolResult:
    """回读持久化快照；不返回配置键、原始值或部署路径。"""
    definition = _FEATURES[arguments["feature"]]
    _snapshot, values = config.read_env_snapshot(config.ENV_FILE)
    expected = "1" if arguments["enabled"] else "0"
    verified = values.get(definition.key) == expected
    data = dict(result.data)
    data["verification_state"] = "verified" if verified else "pending"
    suggestions = list(result.suggestions)
    evidence = list(result.evidence)
    if verified:
        evidence.append(
            Evidence(
                "server_configuration",
                "已从服务端持久化配置快照回读目标功能状态；未返回配置键或配置值。",
                _now(),
            )
        )
    else:
        message = "配置写入已提交，但持久化回读尚未确认目标状态；请刷新设置页复核。"
        if message not in suggestions:
            suggestions.append(message)
    return ToolResult(
        ok=True,
        status=result.status,
        summary=result.summary,
        data=data,
        evidence=evidence,
        suggestions=suggestions,
        error="",
    )


def set_feature_state_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    """只按已确认的快照指纹执行一次白名单配置写入。"""
    state = _capture(arguments)
    if not secrets.compare_digest(
        _fingerprint_payload(state), str(expected_context or "")
    ):
        raise AgentToolError(
            "配置已变化，请重新预检",
            code="confirmation_stale",
        )

    failed = _precondition_failure(state)
    if failed is not None:
        return failed
    definition: FeatureDefinition = state["definition"]
    target = "1" if arguments["enabled"] else "0"
    try:
        config.update_runtime_env_file(
            config.ENV_FILE,
            {definition.key: target},
            expected=state["snapshot"],
        )
    except config.ConcurrentConfigUpdateError:
        return ToolResult(
            ok=False,
            status="conflict",
            summary="配置已被其他操作修改，请重新预检",
            error="配置已变化。",
        )
    except config.ExternalConfigOverrideError:
        return ToolResult(
            ok=False,
            status="environment_override",
            summary=f"{definition.label}由运行环境管理，Agent 未修改配置",
            error="目标功能由运行环境覆盖。",
        )
    except (config.AtomicPublishError, OSError, ValueError):
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="功能状态暂时无法保存",
            error="配置写入失败，请稍后重试。",
        )

    runtime_refreshed = _refresh_runtime(definition)
    suggestions = (
        []
        if runtime_refreshed
        else ["配置已保存；相关服务将在下次请求或重启后使用新状态。"]
    )
    return ToolResult(
        ok=True,
        status="completed",
        summary=f"{definition.label}配置已{'开启' if arguments['enabled'] else '关闭'}",
        data={
            "feature": arguments["feature"],
            "label": definition.label,
            "enabled": arguments["enabled"],
            "runtime_refreshed": runtime_refreshed,
            "runtime_scope": "current_process",
        },
        evidence=[
            Evidence(
                "server_configuration",
                "使用确认票据与配置快照原子更新一个白名单功能开关。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )
