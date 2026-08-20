"""项目配置组件解释：只返回固定标签、能力影响和安全下一步。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app import config
from app.agent.feature_actions import summarize_feature_states
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError


@dataclass(frozen=True)
class ComponentDefinition:
    label: str
    purpose: str
    required_fields: tuple[tuple[str, str], ...]
    blocked_capabilities: tuple[str, ...]
    enabled: Callable[[Callable[[str], str]], bool]
    ready: Callable[[Callable[[str], str]], bool]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _state(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _has(value: Callable[[str], str], key: str) -> bool:
    return bool(value(key))


def _qb_auth(value: Callable[[str], str]) -> bool:
    return _has(value, "QB_API_KEY") or (
        _has(value, "QB_USERNAME") and _has(value, "QB_PASSWORD")
    )


def _strm_enabled(value: Callable[[str], str]) -> bool:
    return _state(value("STRM_SCHEDULE_ENABLED")) or bool(
        value("GY_STRM_SOURCE_DIRS")
    )


def _strm_ready(value: Callable[[str], str]) -> bool:
    return bool(value("GY_STRM_SOURCE_DIRS")) and all((
        _has(value, "GY_STRM_BASE_URL"),
        _has(value, "STRM_ROOT"),
    ))


def _ai_provider_ready(value: Callable[[str], str]) -> bool:
    return _has(value, "AGENT_LLM_API_URL") and _has(value, "AGENT_LLM_MODEL")


_COMPONENTS: dict[str, ComponentDefinition] = {
    "jellyfin": ComponentDefinition(
        label="Jellyfin",
        purpose="连接 Jellyfin 12 媒体库，用于媒体搜索、缺集审计和更新核对。",
        required_fields=(("JELLYFIN_URL", "服务地址"), ("JELLYFIN_API_KEY", "API Key")),
        blocked_capabilities=("Jellyfin 媒体库搜索", "剧集缺集审计", "剧集更新核对"),
        enabled=lambda value: _state(value("JELLYFIN_ENABLED")),
        ready=lambda value: _has(value, "JELLYFIN_URL") and _has(value, "JELLYFIN_API_KEY"),
    ),
    "emby": ComponentDefinition(
        label="Emby / Jellyfin 10.x",
        purpose="连接 Emby 或 Jellyfin 10.x 兼容节点，用于媒体库读取和兼容性诊断。",
        required_fields=(("EMBY_URL", "服务地址"), ("EMBY_TOKEN", "访问 Token")),
        blocked_capabilities=("Emby 媒体库搜索", "兼容节点诊断", "剧集缺集审计"),
        enabled=lambda value: _state(value("EMBY_ENABLED")),
        ready=lambda value: _has(value, "EMBY_URL") and _has(value, "EMBY_TOKEN"),
    ),
    "tmdb": ComponentDefinition(
        label="TMDB 元数据",
        purpose="提供影视映射、分集播出信息、更新核对和 TMDB 探索数据。",
        required_fields=(("TMDB_API_KEY", "API Key"),),
        blocked_capabilities=("TMDB 影视探索", "分集播出核验", "影视映射与更新核对"),
        enabled=lambda value: _has(value, "TMDB_API_KEY"),
        ready=lambda value: _has(value, "TMDB_API_KEY"),
    ),
    "qbittorrent": ComponentDefinition(
        label="qBittorrent",
        purpose="接收资源站结果并查看下载队列、传输速度与停滞任务。",
        required_fields=(("QB_URL", "服务地址"), ("__qb_auth__", "API Key 或用户名/密码")),
        blocked_capabilities=("提交资源到 qBittorrent", "下载队列诊断", "下载任务状态读取"),
        enabled=lambda value: _has(value, "QB_URL") or _qb_auth(value),
        ready=lambda value: _has(value, "QB_URL") and _qb_auth(value),
    ),
    "strm": ComponentDefinition(
        label="STRM",
        purpose="把光鸭云盘目录同步为本地 STRM 索引，并维护调度与失败账本。",
        required_fields=(
            ("__strm_source__", "光鸭源目录"),
            ("GY_STRM_BASE_URL", "播放服务地址"),
            ("STRM_ROOT", "本地 STRM 输出目录"),
        ),
        blocked_capabilities=("STRM 手动同步", "STRM 定时调度", "STRM 失败诊断"),
        enabled=_strm_enabled,
        ready=_strm_ready,
    ),
    "ai_recognition": ComponentDefinition(
        label="AI 识别回退",
        purpose="在确定性规则无法识别媒体时，调用受控的 AI 服务提供候选结果。",
        required_fields=(("__shared_ai_provider__", "Media Agent 模型连接配置"),),
        blocked_capabilities=("AI 媒体识别回退",),
        enabled=lambda value: _state(value("AI_RECOGNITION_ENABLED")),
        ready=_ai_provider_ready,
    ),
}

_FEATURE_DETAILS: dict[str, dict[str, Any]] = {
    "discovery": {
        "purpose": "统一浏览放映排期、豆瓣、TMDB 与 Bangumi 榜单。",
        "blocked_capabilities": ("媒体探索页面", "豆瓣与 TMDB 榜单", "Bangumi 放送内容"),
    },
    "douban": {
        "purpose": "在媒体探索中读取豆瓣电影与电视剧榜单。",
        "blocked_capabilities": ("豆瓣电影榜单", "豆瓣电视剧榜单"),
    },
    "resource_results": {
        "purpose": "在媒体档案中展示多站资源检索结果和安全下载入口。",
        "blocked_capabilities": ("媒体档案站点资源结果", "资源结果下载入口"),
    },
    "indexer_search": {
        "purpose": "跨已启用站点搜索公开资源，并返回可确认提交的临时结果。",
        "blocked_capabilities": ("多站资源搜索", "缺集资源定向搜索", "资源提交预检"),
    },
}

CONFIG_COMPONENTS = tuple((*_COMPONENTS.keys(), *_FEATURE_DETAILS.keys()))
_ALLOWED_ARGUMENTS = {"component"}
_COMPONENT_CONTROL_FIELDS: dict[str, tuple[str, ...]] = {
    "jellyfin": ("JELLYFIN_ENABLED",),
    "emby": ("EMBY_ENABLED",),
    "strm": ("STRM_SCHEDULE_ENABLED",),
    "ai_recognition": ("AI_RECOGNITION_ENABLED",),
}


def config_component_arguments(arguments: dict[str, Any]) -> dict[str, str]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _ALLOWED_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    component = arguments.get("component")
    if not isinstance(component, str):
        raise AgentToolError("component 必须是字符串")
    component = component.strip()
    if component not in CONFIG_COMPONENTS:
        raise AgentToolError("component 不在允许的配置组件列表中")
    return {"component": component}


def _effective_value() -> Callable[[str], str]:
    items = config.all_items()

    def value(key: str) -> str:
        return str(config.get(key, items.get(key, "")) or "").strip()

    return value


def _field_present(value: Callable[[str], str], field: str) -> bool:
    if field == "__qb_auth__":
        return _qb_auth(value)
    if field == "__strm_source__":
        return bool(value("GY_STRM_SOURCE_DIRS"))
    if field == "__shared_ai_provider__":
        return _ai_provider_ready(value)
    return _has(value, field)


def _field_override_keys(field: str) -> tuple[str, ...]:
    if field == "__qb_auth__":
        return ("QB_API_KEY", "QB_USERNAME", "QB_PASSWORD")
    if field == "__strm_source__":
        return ("GY_STRM_SOURCE_DIRS",)
    if field == "__shared_ai_provider__":
        return (
            "AGENT_LLM_API_URL", "AGENT_LLM_API_KEY", "AGENT_LLM_MODEL",
        )
    return (field,)


def _component_managed_by_environment(
    component: str,
    definition: ComponentDefinition,
) -> bool:
    keys = [
        key
        for field, _label in definition.required_fields
        for key in _field_override_keys(field)
    ]
    keys.extend(_COMPONENT_CONTROL_FIELDS.get(component, ()))
    return any(config.has_external_override(key) for key in keys)


def _base_component_payload(component: str) -> dict[str, Any]:
    definition = _COMPONENTS[component]
    value = _effective_value()
    enabled = definition.enabled(value)
    ready = definition.ready(value)
    managed = _component_managed_by_environment(component, definition)
    has_enable_switch = component in {"jellyfin", "emby", "ai_recognition"}
    if has_enable_switch and not enabled:
        status = "disabled"
    elif ready:
        status = "ready"
    elif enabled:
        status = "incomplete"
    else:
        status = "not_configured"

    missing = [
        label for field, label in definition.required_fields
        if not _field_present(value, field)
    ]
    if status == "disabled":
        missing = []

    if status == "ready":
        next_steps = ["当前必要配置已具备；如仍不可用，请运行对应的状态或连通性诊断。"]
    elif managed:
        next_steps = ["该组件至少一项必要设置由运行环境管理，请在部署配置中核对后重启服务。"]
    elif status == "disabled":
        next_steps = [f"先在设置页启用{definition.label}，再填写必要配置。"]
    else:
        next_steps = ["前往设置页补全缺少的字段，然后重新运行配置诊断。"]
    if component == "ai_recognition" and status == "incomplete":
        next_steps = ["前往设置页的 Media Agent 分区补全模型连接，再重新运行配置诊断。"]
    if component in {"jellyfin", "emby"}:
        next_steps.append(f"配置保存后，可让 Agent 测试{definition.label}连接。")

    return {
        "component": component,
        "label": definition.label,
        "status": status,
        "enabled": enabled,
        "purpose": definition.purpose,
        "required_field_labels": [label for _field, label in definition.required_fields],
        "missing_field_labels": missing,
        "blocked_capabilities": list(definition.blocked_capabilities) if status != "ready" else [],
        "next_steps": next_steps,
        "managed_by_environment": managed,
        "agent_action": None,
    }


def _feature_payload(component: str) -> dict[str, Any]:
    summary = summarize_feature_states({})
    feature = next(
        item for item in summary.data.get("features", [])
        if item.get("feature") == component
    )
    availability = str(feature.get("availability") or "blocked")
    status = "ready" if availability == "available" else availability
    reasons = list(feature.get("reason_codes") or [])
    managed = bool(feature.get("managed_by_environment"))
    next_steps: list[str] = []
    reason_steps = {
        "feature_disabled": "该功能当前已关闭，可由 Agent 发起受控开启并在确认后保存。",
        "parent_disabled": "先开启媒体探索，再重新检查该功能。",
        "search_disabled": "先开启多站资源搜索，再重新检查探索页资源结果。",
        "no_enabled_sites": "请先在设置页至少启用一个资源站点。",
    }
    next_steps.extend(reason_steps[reason] for reason in reasons if reason in reason_steps)
    if managed:
        next_steps = ["该开关由运行环境管理，请在部署配置中调整并重启服务。"]
    if not next_steps:
        next_steps = ["当前功能及其依赖均可用，无需修改配置。"]

    agent_action = None
    if availability == "disabled" and not managed:
        agent_action = {
            "supported": True,
            "tool": "config.set_feature_state",
            "feature": component,
            "enabled": True,
            "requires_confirmation": True,
            "prompt": f"开启{feature.get('label') or component}",
        }

    details = _FEATURE_DETAILS[component]
    return {
        "component": component,
        "label": feature.get("label") or component,
        "status": status,
        "enabled": bool(feature.get("enabled")),
        "purpose": details["purpose"],
        "required_field_labels": [],
        "missing_field_labels": [],
        "blocked_capabilities": list(details["blocked_capabilities"]) if status != "ready" else [],
        "next_steps": next_steps,
        "managed_by_environment": managed,
        "agent_action": agent_action,
    }


def explain_config_component(arguments: dict[str, Any]) -> ToolResult:
    """解释一个白名单配置组件，不返回配置键或配置值。"""
    component = arguments["component"]
    payload = (
        _feature_payload(component)
        if component in _FEATURE_DETAILS
        else _base_component_payload(component)
    )
    label = payload["label"]
    status = payload["status"]
    status_labels = {
        "ready": "已就绪",
        "incomplete": "配置不完整",
        "disabled": "已关闭",
        "not_configured": "未配置",
        "blocked": "依赖受阻",
    }
    summary = f"{label}：{status_labels.get(status, status)}"
    return ToolResult(
        ok=True,
        status=status,
        summary=summary,
        data=payload,
        evidence=[Evidence(
            "server_configuration",
            "仅解释白名单组件的配置状态、字段标签和能力影响；未返回配置键、配置值、地址、路径或凭据。",
            _now(),
        )],
        suggestions=payload["next_steps"],
    )
