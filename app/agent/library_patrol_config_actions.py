"""Media Agent 的全库缺集巡检策略读取与确认写入。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from typing import Any

from app import config
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.logger import get_logger

logger = get_logger(__name__)

_TARGETS = {
    "enabled": ("AGENT_LIBRARY_PATROL_ENABLED", False),
    "notify_enabled": ("AGENT_LIBRARY_PATROL_NOTIFY_ENABLED", False),
    "interval_hours": ("AGENT_LIBRARY_PATROL_INTERVAL_HOURS", 24),
    "max_series": ("AGENT_LIBRARY_PATROL_MAX_SERIES", 50),
}
_TARGET_KEYS = tuple(item[0] for item in _TARGETS.values())
_ALLOWED_ARGUMENTS = frozenset(_TARGETS)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def patrol_policy_summary_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(arguments))}")
    return {}


def patrol_policy_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _ALLOWED_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    if not arguments:
        raise AgentToolError("至少提供一项巡检策略修改")

    normalized: dict[str, Any] = {}
    for name in ("enabled", "notify_enabled"):
        if name not in arguments:
            continue
        value = arguments[name]
        if type(value) is not bool:
            raise AgentToolError(f"{name} 必须是布尔值")
        normalized[name] = value

    for name, minimum, maximum in (
        ("interval_hours", 1, 168),
        ("max_series", 1, 100),
    ):
        if name not in arguments:
            continue
        value = arguments[name]
        if type(value) is not int:
            raise AgentToolError(f"{name} 必须是整数")
        if value < minimum or value > maximum:
            raise AgentToolError(f"{name} 必须为 {minimum} 到 {maximum}")
        normalized[name] = value
    return normalized


def _current_policy() -> dict[str, Any]:
    return {
        "enabled": config.get_bool(_TARGETS["enabled"][0], False),
        "notify_enabled": config.get_bool(_TARGETS["notify_enabled"][0], False),
        "interval_hours": max(
            1,
            min(config.get_int(_TARGETS["interval_hours"][0], 24), 168),
        ),
        "max_series": max(
            1,
            min(config.get_int(_TARGETS["max_series"][0], 50), 100),
        ),
    }


def _managed_fields() -> list[str]:
    return [
        name
        for name, (key, _default) in _TARGETS.items()
        if config.has_external_override(key)
    ]


def _public_policy(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(policy["enabled"]),
        "notify_enabled": bool(policy["notify_enabled"]),
        "interval_hours": int(policy["interval_hours"]),
        "max_series": int(policy["max_series"]),
    }


def summarize_patrol_policy(_arguments: dict[str, Any]) -> ToolResult:
    policy = _current_policy()
    managed = _managed_fields()
    return ToolResult(
        ok=True,
        status="enabled" if policy["enabled"] else "disabled",
        summary=(
            f"全库缺集巡检已开启：每 {policy['interval_hours']} 小时最多检查 "
            f"{policy['max_series']} 部剧集"
            if policy["enabled"]
            else "全库缺集巡检当前已关闭"
        ),
        data={
            "policy": _public_policy(policy),
            "managed_by_environment": bool(managed),
            "managed_fields": managed,
        },
        evidence=[
            Evidence(
                "server_configuration",
                "仅返回全库缺集巡检的四项白名单策略与环境托管标记；未返回其他配置值。",
                _now(),
            )
        ],
        suggestions=[
            "修改策略需要先预检并使用一次性确认票据。",
            "策略修改不会同步执行巡检；当前进程只会重新安排下一次后台检查。",
        ],
    )


def _capture(arguments: dict[str, Any]) -> dict[str, Any]:
    snapshot, values = config.read_env_snapshot(config.ENV_FILE)
    current = _current_policy()
    requested = dict(current)
    requested.update(arguments)
    requested_keys = tuple(name for name in _TARGETS if name in arguments)
    return {
        "snapshot": snapshot,
        "snapshot_present": snapshot is not None,
        "snapshot_sha256": hashlib.sha256(snapshot or b"").hexdigest(),
        "persisted": {key: values.get(key, "<unset>") for key in _TARGET_KEYS},
        "current": current,
        "requested": requested,
        "requested_keys": requested_keys,
        "changed_keys": tuple(
            name for name in requested_keys if current[name] != requested[name]
        ),
        "external_overrides": tuple(
            name
            for name in requested_keys
            if config.has_external_override(_TARGETS[name][0])
        ),
    }


def _fingerprint(state: dict[str, Any]) -> str:
    payload = {
        "snapshot_present": state["snapshot_present"],
        "snapshot_sha256": state["snapshot_sha256"],
        "persisted": state["persisted"],
        "current": _public_policy(state["current"]),
        "requested": _public_policy(state["requested"]),
        "requested_keys": list(state["requested_keys"]),
        "changed_keys": list(state["changed_keys"]),
        "external_overrides": list(state["external_overrides"]),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _precondition_failure(state: dict[str, Any]) -> ToolResult | None:
    if state["external_overrides"]:
        return ToolResult(
            ok=False,
            status="environment_override",
            summary="巡检策略由运行环境管理，Agent 不会覆盖部署配置",
            error="目标配置由运行环境覆盖。",
            data={"managed_fields": list(state["external_overrides"])},
            suggestions=["请在部署环境中调整对应巡检策略后重启服务。"],
        )
    if state["current"] == state["requested"]:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary="全库巡检策略已经是目标状态",
            error="巡检策略没有变化。",
        )
    return None


def _effects(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    current = state["current"]
    requested = state["requested"]
    effects: list[str] = []
    if current["enabled"] != requested["enabled"]:
        effects.append(f"{'开启' if requested['enabled'] else '关闭'}后台全库缺集巡检")
    if current["notify_enabled"] != requested["notify_enabled"]:
        effects.append(
            f"{'开启' if requested['notify_enabled'] else '关闭'}巡检结果通知"
        )
    if current["interval_hours"] != requested["interval_hours"]:
        effects.append(f"将巡检间隔改为 {requested['interval_hours']} 小时")
    if current["max_series"] != requested["max_series"]:
        effects.append(f"将单轮检查上限改为 {requested['max_series']} 部剧集")
    effects.append("刷新当前进程调度器，并按新策略重新安排下一次后台检查")

    suggestions = ["本次写入不会同步执行巡检、搜索资源或下载。"]
    if not requested["notify_enabled"]:
        suggestions.append(
            "通知关闭时，若存在尚未发送的巡检通知积压，将被丢弃且无法恢复。"
        )
    return effects, suggestions


def _preview_from_state(state: dict[str, Any]) -> ToolResult:
    failed = _precondition_failure(state)
    if failed is not None:
        return failed
    effects, suggestions = _effects(state)
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary="确认后将更新全库缺集巡检策略",
        data={
            "current_policy": _public_policy(state["current"]),
            "requested_policy": _public_policy(state["requested"]),
            "changed_fields": list(state["changed_keys"]),
            "effects": effects,
        },
        evidence=[
            Evidence(
                "server_configuration",
                "仅比较全库缺集巡检白名单策略与配置快照；未返回其他配置值。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )


def prepare_patrol_policy_confirmation(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    """用同一份配置快照生成预检结果和确认上下文。"""
    state = _capture(arguments)
    return _preview_from_state(state), _fingerprint(state)


def _build_updates(arguments: dict[str, Any]) -> dict[str, str]:
    updates: dict[str, str] = {}
    for name, value in arguments.items():
        key, _default = _TARGETS[name]
        if name in {"enabled", "notify_enabled"}:
            updates[key] = "1" if value else "0"
        else:
            updates[key] = str(value)
    return updates


def set_patrol_policy_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    state = _capture(arguments)
    if not secrets.compare_digest(_fingerprint(state), str(expected_context or "")):
        raise AgentToolError(
            "巡检策略配置已变化，请重新预检",
            code="confirmation_stale",
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
            summary="巡检策略已被其他操作修改，请重新预检",
            error="配置已变化。",
        )
    except config.ExternalConfigOverrideError:
        return ToolResult(
            ok=False,
            status="environment_override",
            summary="巡检策略由运行环境管理，Agent 未修改配置",
            error="目标配置由运行环境覆盖。",
        )
    except (config.AtomicPublishError, OSError, ValueError):
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="全库巡检策略暂时无法保存",
            error="配置写入失败，请稍后重试。",
        )

    runtime_refreshed = True
    try:
        from app.modules.agent_library_patrol_scheduler import (
            get_agent_library_patrol_scheduler,
        )

        get_agent_library_patrol_scheduler().reload(immediate=False)
    except Exception as exc:
        runtime_refreshed = False
        logger.warning("Agent 全库缺集巡检运行时刷新失败 type=%s", type(exc).__name__)

    policy = _current_policy()
    _effects_value, suggestions = _effects(state)
    if not runtime_refreshed:
        suggestions.insert(0, "当前进程调度器未能及时刷新；请重启服务以应用新策略。")
    return ToolResult(
        ok=True,
        status="completed",
        summary="已保存全库缺集巡检策略",
        data={
            "policy": _public_policy(policy),
            "changed_fields": list(state["changed_keys"]),
            "runtime_refreshed": runtime_refreshed,
            "runtime_scope": "current_process",
        },
        evidence=[
            Evidence(
                "server_configuration",
                "使用一次性确认票据与配置快照原子更新全库缺集巡检白名单策略。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )
