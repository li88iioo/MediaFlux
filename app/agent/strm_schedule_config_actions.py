"""Media Agent 的 STRM 定时同步策略读取与确认写入。"""

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
from app.modules.scheduler import STRMScheduler

logger = get_logger(__name__)

_TARGETS = {
    "enabled": ("STRM_SCHEDULE_ENABLED", False),
    "cron": ("STRM_SCHEDULE_CRON", "0 4 * * *"),
    "notify_enabled": ("STRM_NOTIFY_ENABLED", True),
}
_TARGET_KEYS = tuple(item[0] for item in _TARGETS.values())
_ALLOWED_ARGUMENTS = frozenset(_TARGETS)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def strm_schedule_policy_summary_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(arguments))}")
    return {}


def strm_schedule_policy_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    extra = set(arguments) - _ALLOWED_ARGUMENTS
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    if not arguments:
        raise AgentToolError("至少提供一项 STRM 调度策略修改")

    normalized: dict[str, Any] = {}
    for name in ("enabled", "notify_enabled"):
        if name not in arguments:
            continue
        value = arguments[name]
        if type(value) is not bool:
            raise AgentToolError(f"{name} 必须是布尔值")
        normalized[name] = value

    if "cron" in arguments:
        value = arguments["cron"]
        if not isinstance(value, str):
            raise AgentToolError("cron 必须是字符串")
        cron = " ".join(value.strip().split())
        if len(cron) > 128 or not STRMScheduler.validate_cron(cron):
            raise AgentToolError("cron 表达式无效，需使用 5 段格式：分 时 日 月 周")
        normalized["cron"] = cron
    return normalized


def _current_policy() -> dict[str, Any]:
    cron = config.get(_TARGETS["cron"][0], "0 4 * * *").strip()
    return {
        "enabled": config.get_bool(_TARGETS["enabled"][0], False),
        "cron": cron or "0 4 * * *",
        "notify_enabled": config.get_bool(_TARGETS["notify_enabled"][0], True),
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
        "cron": str(policy["cron"]),
        "notify_enabled": bool(policy["notify_enabled"]),
    }


def summarize_strm_schedule_policy(_arguments: dict[str, Any]) -> ToolResult:
    policy = _current_policy()
    managed = _managed_fields()
    return ToolResult(
        ok=True,
        status="enabled" if policy["enabled"] else "disabled",
        summary=(
            f"STRM 定时同步已开启，计划表达式为 {policy['cron']}"
            if policy["enabled"]
            else "STRM 定时同步当前已关闭"
        ),
        data={
            "policy": _public_policy(policy),
            "managed_by_environment": bool(managed),
            "managed_fields": managed,
        },
        evidence=[
            Evidence(
                "server_configuration",
                "仅返回 STRM 定时同步的三项白名单策略与环境托管标记；未返回目录、地址或凭据。",
                _now(),
            )
        ],
        suggestions=[
            "修改策略需要先预检并使用一次性确认票据。",
            "策略修改只会重新安排后续定时同步，不会立即启动或中断同步任务。",
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
            summary="STRM 调度策略由运行环境管理，Agent 不会覆盖部署配置",
            error="目标配置由运行环境覆盖。",
            data={"managed_fields": list(state["external_overrides"])},
            suggestions=["请在部署环境中调整对应 STRM 调度策略后重启服务。"],
        )
    if state["current"] == state["requested"]:
        return ToolResult(
            ok=False,
            status="no_changes",
            summary="STRM 调度策略已经是目标状态",
            error="STRM 调度策略没有变化。",
        )
    return None


def _effects(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    current = state["current"]
    requested = state["requested"]
    effects: list[str] = []
    if current["enabled"] != requested["enabled"]:
        effects.append(
            f"{'开启' if requested['enabled'] else '关闭'}后续 STRM 定时同步"
        )
    if current["cron"] != requested["cron"]:
        effects.append(f"将后续同步计划表达式改为 {requested['cron']}")
    if current["notify_enabled"] != requested["notify_enabled"]:
        effects.append(
            f"{'开启' if requested['notify_enabled'] else '关闭'} STRM 任务通知"
        )
    effects.append("刷新当前进程调度器，并按新策略重新计算下一次运行时间")

    suggestions = ["本次写入不会立即执行 STRM 同步，也不会中断正在运行的任务。"]
    if requested["enabled"]:
        suggestions.append(
            "只有 STRM 源目录、播放地址等运行配置完整时，计划任务才会实际执行。"
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
        summary="确认后将更新 STRM 定时同步策略",
        data={
            "current_policy": _public_policy(state["current"]),
            "requested_policy": _public_policy(state["requested"]),
            "changed_fields": list(state["changed_keys"]),
            "effects": effects,
        },
        evidence=[
            Evidence(
                "server_configuration",
                "仅比较 STRM 定时同步白名单策略与配置快照；未返回其他配置值。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )


def prepare_strm_schedule_policy_confirmation(
    arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
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


def set_strm_schedule_policy_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    state = _capture(arguments)
    if not secrets.compare_digest(_fingerprint(state), str(expected_context or "")):
        raise AgentToolError(
            "STRM 调度策略配置已变化，请重新预检",
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
            summary="STRM 调度策略已被其他操作修改，请重新预检",
            error="配置已变化。",
        )
    except config.ExternalConfigOverrideError:
        return ToolResult(
            ok=False,
            status="environment_override",
            summary="STRM 调度策略由运行环境管理，Agent 未修改配置",
            error="目标配置由运行环境覆盖。",
        )
    except (config.AtomicPublishError, OSError, ValueError):
        return ToolResult(
            ok=False,
            status="unavailable",
            summary="STRM 调度策略暂时无法保存",
            error="配置写入失败，请稍后重试。",
        )

    runtime_refreshed = True
    try:
        from app.modules.scheduler import get_scheduler

        get_scheduler().reload()
    except Exception as exc:
        runtime_refreshed = False
        logger.warning("Agent STRM 调度器运行时刷新失败 type=%s", type(exc).__name__)

    policy = _current_policy()
    _effects_value, suggestions = _effects(state)
    if not runtime_refreshed:
        suggestions.insert(0, "当前进程调度器未能及时刷新；请重启服务以应用新策略。")
    return ToolResult(
        ok=True,
        status="completed",
        summary="已保存 STRM 定时同步策略",
        data={
            "policy": _public_policy(policy),
            "changed_fields": list(state["changed_keys"]),
            "runtime_refreshed": runtime_refreshed,
            "runtime_scope": "current_process",
        },
        evidence=[
            Evidence(
                "server_configuration",
                "使用一次性确认票据与配置快照原子更新 STRM 定时同步白名单策略。",
                _now(),
            )
        ],
        suggestions=suggestions,
    )
