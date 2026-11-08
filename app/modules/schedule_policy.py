"""STRM、光鸭整理与全库巡检共用的策略读取、预检和原子提交。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from typing import Any

from app import config
from app.logger import get_logger
from app.modules.organize_scheduler import OrganizeScheduler, get_organize_scheduler
from app.modules.scheduler import STRMScheduler

logger = get_logger(__name__)
_TARGETS: dict[str, dict[str, tuple[str, Any]]] = {
    "strm": {
        "enabled": ("STRM_SCHEDULE_ENABLED", False),
        "cron": ("STRM_SCHEDULE_CRON", "0 4 * * *"),
        "notify_enabled": ("STRM_NOTIFY_ENABLED", True),
    },
    "organize": {
        "enabled": ("GY_ORGANIZE_SCHEDULE_ENABLED", False),
        "cron": ("GY_ORGANIZE_SCHEDULE_CRON", "0 4 * * *"),
        "notify_enabled": ("GY_ORGANIZE_NOTIFY_ENABLED", True),
    },
    "patrol": {
        "enabled": ("AGENT_LIBRARY_PATROL_ENABLED", False),
        "notify_enabled": ("AGENT_LIBRARY_PATROL_NOTIFY_ENABLED", False),
        "interval_hours": ("AGENT_LIBRARY_PATROL_INTERVAL_HOURS", 24),
        "max_series": ("AGENT_LIBRARY_PATROL_MAX_SERIES", 50),
    },
}
_TITLES = {"strm": "STRM 定时同步", "organize": "光鸭定时整理", "patrol": "全库缺集巡检"}
_CHANGE_TITLES = {"strm": "STRM 调度策略", "organize": "光鸭定时整理策略", "patrol": "巡检策略"}
_ORGANIZE_LABELS = {"enabled": "定时整理开关", "cron": "计划表达式", "notify_enabled": "整理通知开关"}
_BOUNDS = {"interval_hours": (1, 168), "max_series": (1, 100)}


class PolicySnapshotChanged(ValueError):
    """写入前确认快照不一致；尚未调用配置发布。"""


def normalize_changes(kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
    targets = _TARGETS[kind]
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    extra = set(arguments) - targets.keys()
    if extra:
        raise ValueError(f"不支持的工具参数：{', '.join(sorted(extra))}")
    if not arguments:
        raise ValueError(f"至少提供一项{_CHANGE_TITLES[kind]}修改")
    normalized = {}
    for name, value in arguments.items():
        if name in {"enabled", "notify_enabled"}:
            if type(value) is not bool:
                raise ValueError(f"{name} 必须是布尔值")
        elif name == "cron":
            if not isinstance(value, str):
                raise ValueError("cron 必须是字符串")
            value = " ".join(value.strip().split())
            scheduler = OrganizeScheduler if kind == "organize" else STRMScheduler
            if len(value) > 128 or not scheduler.validate_cron(value):
                raise ValueError("cron 表达式无效，需使用 5 段格式：分 时 日 月 周")
        else:
            minimum, maximum = _BOUNDS[name]
            if type(value) is not int:
                raise ValueError(f"{name} 必须是整数")
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} 必须为 {minimum} 到 {maximum}")
        normalized[name] = value
    return normalized


def current_policy(kind: str) -> dict[str, Any]:
    policy: dict[str, Any] = {}
    for name, (key, default) in _TARGETS[kind].items():
        if type(default) is bool:
            policy[name] = config.get_bool(key, default)
        elif type(default) is int:
            minimum, maximum = _BOUNDS[name]
            policy[name] = max(minimum, min(config.get_int(key, default), maximum))
        else:
            policy[name] = config.get(key, default).strip() or default
    return policy


def _public_fields(kind: str, names) -> list[str]:
    return [_ORGANIZE_LABELS[name] for name in names] if kind == "organize" else list(names)


def _result(ok: bool, status: str, summary: str, *, data=None, suggestions=(), error="", evidence="") -> dict:
    return {
        "ok": ok,
        "status": status,
        "summary": summary,
        "data": data or {},
        "suggestions": list(suggestions),
        "error": error,
        "evidence": (
            [
                {
                    "source": "server_configuration",
                    "description": evidence,
                    "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            ]
            if evidence
            else []
        ),
    }


def summarize_policy(kind: str) -> dict:
    policy = current_policy(kind)
    managed = [name for name, (key, _) in _TARGETS[kind].items() if config.has_external_override(key)]
    status = "enabled" if policy["enabled"] else "disabled"
    summary = f"{_TITLES[kind]}当前已关闭"
    data: dict[str, Any] = {
        "policy": policy,
        "managed_by_environment": bool(managed),
        "managed_fields": _public_fields(kind, managed),
    }
    if kind == "organize":
        data.update(configured=True, cron_valid=OrganizeScheduler.validate_cron(policy["cron"]), next_run=None)
        try:
            runtime = get_organize_scheduler().status()
            data.update(configured=not bool(runtime.get("config_error")), cron_valid=bool(runtime.get("cron_valid")))
            value = runtime.get("next_run")
            if isinstance(value, str) and value.strip():
                data["next_run"] = value.strip()
        except Exception as exc:
            logger.warning("光鸭整理调度状态读取失败 type=%s", type(exc).__name__)
        if policy["enabled"] and not (data["configured"] and data["cron_valid"]):
            status, summary = "attention", "光鸭定时整理已开启，但运行配置尚未就绪"
    if status == "enabled":
        summary = (
            f"全库缺集巡检已开启：每 {policy['interval_hours']} 小时最多检查 {policy['max_series']} 部剧集"
            if kind == "patrol"
            else f"{_TITLES[kind]}已开启，计划表达式为 {policy['cron']}"
        )
    return _result(
        status != "attention",
        status,
        summary,
        data=data,
        evidence=f"仅返回{_TITLES[kind]}白名单策略、运行状态与环境托管标记；未返回其他配置值。",
        suggestions=["修改策略需要先预检并使用一次性确认票据。", _effects(kind, policy, policy)[1][0]],
    )


def capture_policy_state(kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
    snapshot, values = config.read_env_snapshot(config.ENV_FILE)
    targets = _TARGETS[kind]
    current = current_policy(kind)
    requested = {**current, **arguments}
    requested_keys = tuple(name for name in targets if name in arguments)
    return {
        "snapshot": snapshot,
        "snapshot_present": snapshot is not None,
        "snapshot_sha256": hashlib.sha256(snapshot or b"").hexdigest(),
        "persisted": {key: values.get(key, "<unset>") for key, _ in targets.values()},
        "current": current,
        "requested": requested,
        "requested_keys": requested_keys,
        "changed_keys": tuple(name for name in requested_keys if current[name] != requested[name]),
        "external_overrides": tuple(name for name in requested_keys if config.has_external_override(targets[name][0])),
    }


def policy_state_fingerprint(state: dict[str, Any]) -> str:
    # 摘要域及 JSON 字节与旧版一致：None/空文件不同，预检只读一次快照。
    payload = {
        name: state[name] for name in ("snapshot_present", "snapshot_sha256", "persisted", "current", "requested")
    }
    payload.update({name: list(state[name]) for name in ("requested_keys", "changed_keys", "external_overrides")})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _precondition(kind: str, state: dict) -> dict | None:
    title = _CHANGE_TITLES[kind]
    if state["external_overrides"]:
        return _result(
            False,
            "environment_override",
            f"{title}由运行环境管理，Agent 不会覆盖部署配置",
            error="目标配置由运行环境覆盖。",
            data={"managed_fields": _public_fields(kind, state["external_overrides"])},
            suggestions=[f"请在部署环境中调整对应{title}后重启服务。"],
        )
    if state["current"] == state["requested"]:
        return _result(True, "no_changes", f"{title}已经是目标状态")
    return None


def _effects(kind: str, current: dict, requested: dict) -> tuple[list[str], list[str]]:
    effects = []
    if current["enabled"] != requested["enabled"]:
        effects.append(
            f"{'开启' if requested['enabled'] else '关闭'}{'后台全库缺集巡检' if kind == 'patrol' else '后续' + _TITLES[kind]}"
        )
    if current["notify_enabled"] != requested["notify_enabled"]:
        label = {"strm": " STRM 任务通知", "organize": "光鸭整理通知", "patrol": "巡检结果通知"}[kind]
        effects.append(f"{'开启' if requested['notify_enabled'] else '关闭'}{label}")
    if kind == "patrol":
        if current["interval_hours"] != requested["interval_hours"]:
            effects.append(f"将巡检间隔改为 {requested['interval_hours']} 小时")
        if current["max_series"] != requested["max_series"]:
            effects.append(f"将单轮检查上限改为 {requested['max_series']} 部剧集")
        effects.append("刷新当前进程调度器，并按新策略重新安排下一次后台检查")
        suggestions = ["本次写入不会同步执行巡检、搜索资源或下载。"]
        if not requested["notify_enabled"]:
            suggestions.append("通知关闭时，若存在尚未发送的巡检通知积压，将被丢弃且无法恢复。")
    else:
        if current["cron"] != requested["cron"]:
            effects.append(f"将后续{'同步' if kind == 'strm' else '整理'}计划表达式改为 {requested['cron']}")
        effects.append("刷新当前进程调度器，并按新策略重新计算下一次运行时间")
        suggestions = [
            f"本次写入不会立即执行{'STRM 同步' if kind == 'strm' else '光鸭整理'}，也不会中断正在运行的任务。"
        ]
        if requested["enabled"]:
            suggestions.append(
                "只有 STRM 源目录、播放地址等运行配置完整时，计划任务才会实际执行。"
                if kind == "strm"
                else "只有整理来源和目标目录配置完整时，计划任务才会实际执行。"
            )
    return effects, suggestions


def prepare_policy_change(kind: str, arguments: dict[str, Any]) -> tuple[dict, str]:
    arguments = normalize_changes(kind, arguments)
    state = capture_policy_state(kind, arguments)
    fingerprint = policy_state_fingerprint(state)
    failed = _precondition(kind, state)
    if failed is not None:
        return failed, fingerprint
    effects, suggestions = _effects(kind, state["current"], state["requested"])
    return _result(
        True,
        "confirmation_required",
        f"确认后将更新{_TITLES[kind]}策略",
        data={
            "current_policy": state["current"],
            "requested_policy": state["requested"],
            "changed_fields": _public_fields(kind, state["changed_keys"]),
            "effects": effects,
        },
        suggestions=suggestions,
        evidence=f"仅比较{_TITLES[kind]}白名单策略与配置快照；未返回其他配置值。",
    ), fingerprint


def reload_schedule(kind: str) -> None:
    if kind == "patrol":
        from app.modules.agent_library_patrol_scheduler import get_agent_library_patrol_scheduler

        get_agent_library_patrol_scheduler().reload(immediate=False)
    elif kind == "organize":
        get_organize_scheduler().reload()
    else:
        from app.modules.scheduler import get_scheduler

        get_scheduler().reload()


def apply_policy_change(kind: str, arguments: dict[str, Any], *, expected_fingerprint: str) -> dict:
    arguments = normalize_changes(kind, arguments)
    state = capture_policy_state(kind, arguments)
    title = _CHANGE_TITLES[kind]
    if not secrets.compare_digest(policy_state_fingerprint(state), expected_fingerprint or ""):
        raise PolicySnapshotChanged(f"{title}配置已变化，请重新预检")
    failed = _precondition(kind, state)
    if failed is not None:
        return failed
    updates = {
        _TARGETS[kind][name][0]: ("1" if value else "0") if type(value) is bool else str(value)
        for name, value in arguments.items()
    }
    try:
        config.update_runtime_env_file(config.ENV_FILE, updates, expected=state["snapshot"])
    except config.ConcurrentConfigUpdateError:
        return _result(False, "conflict", f"{title}已被其他操作修改，请重新预检", error="配置已变化。")
    except config.ExternalConfigOverrideError:
        return _result(
            False, "environment_override", f"{title}由运行环境管理，Agent 未修改配置", error="目标配置由运行环境覆盖。"
        )
    except (config.AtomicPublishError, OSError, ValueError):
        return _result(
            False, "unavailable", f"{title}保存结果暂无法核验", error="请先读取当前策略核实保存状态，勿重复提交。"
        )
    runtime_refreshed = True
    try:
        reload_schedule(kind)
    except Exception as exc:
        runtime_refreshed = False
        logger.warning("%s调度器运行时刷新失败 type=%s", _TITLES[kind], type(exc).__name__)
    _, suggestions = _effects(kind, state["current"], state["requested"])
    if not runtime_refreshed:
        suggestions.insert(0, "当前进程调度器未能及时刷新；请重启服务以应用新策略。")
    return _result(
        True,
        "completed",
        f"已保存{_TITLES[kind]}策略",
        data={
            "policy": current_policy(kind),
            "changed_fields": _public_fields(kind, state["changed_keys"]),
            "runtime_refreshed": runtime_refreshed,
            "runtime_scope": "当前进程" if kind == "organize" else "current_process",
        },
        suggestions=suggestions,
        evidence=f"使用一次性确认票据与配置快照原子更新{_TITLES[kind]}白名单策略。",
    )
