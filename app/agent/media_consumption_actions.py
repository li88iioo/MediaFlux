"""媒体消费体验：继续观看、显式偏好、今日摘要与订阅通知规则。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import secrets
from typing import Any

from app.agent.action_history import action_history_owner_digest
from app.agent.models import Evidence, ToolContext, ToolResult
from app.agent.registry import AgentToolError
from app.agent.result_projection import sanitize_public_text
from app.modules.media_server_profiles import list_configured_profiles
from app.repositories.media_experience import (
    clear_media_preferences,
    default_media_preferences,
    get_media_preferences,
    get_notification_rule,
    reset_notification_rule,
    set_media_preferences,
    set_notification_rule,
    today_content_summary,
)

_PREFERENCE_CHOICES = {
    "preferred_server": {"any", "jellyfin", "emby"},
    "preferred_download_target": {"qb", "guangya", "both"},
}
_RULE_FIELDS = {
    "enabled", "notify_on_missing", "notify_on_satisfied",
    "notify_on_error",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _owner_digest(context: ToolContext) -> str:
    owner = str(context.owner or "").strip()
    if not owner:
        raise AgentToolError("当前会话无法读取媒体偏好", code="precondition_failed")
    return action_history_owner_digest(owner)


def _public_preferences(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in (*_PREFERENCE_CHOICES, "explicit")
        if key in value
    }


def _public_notification_rule(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "subscription_number": int(value["subscription_number"]),
        "title": sanitize_public_text(value.get("title"), limit=120) or "媒体订阅",
        "subscription_enabled": bool(value.get("subscription_enabled")),
        "subscription_status": sanitize_public_text(
            value.get("subscription_status"), limit=32
        ) or "unknown",
        **{key: bool(value.get(key)) for key in _RULE_FIELDS},
        "explicit": bool(value.get("explicit")),
    }


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def empty_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError(f"不支持的工具参数：{', '.join(sorted(arguments))}")
    return {}


def continue_watching_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if set(arguments) - {"server", "limit"}:
        raise AgentToolError("继续观看只接受 server 和 limit")
    server = str(arguments.get("server") or "auto").strip().lower()
    if server not in {"auto", "jellyfin", "emby"}:
        raise AgentToolError("server 仅支持 auto、jellyfin 或 emby")
    limit = arguments.get("limit", 8)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 12:
        raise AgentToolError("limit 必须是 1 到 12 的整数")
    return {"server": server, "limit": limit}


def preferences_update_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if not arguments or set(arguments) - set(_PREFERENCE_CHOICES):
        raise AgentToolError("媒体偏好字段无效或为空")
    result: dict[str, Any] = {}
    for key, allowed in _PREFERENCE_CHOICES.items():
        if key not in arguments:
            continue
        value = str(arguments[key] or "").strip().lower()
        if value not in allowed:
            raise AgentToolError(f"{key} 的取值不受支持")
        result[key] = value
    return result


def notification_rule_arguments(arguments: dict[str, Any]) -> dict[str, int]:
    if not isinstance(arguments, dict) or set(arguments) != {"subscription_number"}:
        raise AgentToolError("只接受 subscription_number")
    number = arguments.get("subscription_number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise AgentToolError("subscription_number 必须是正整数")
    return {"subscription_number": number}


def notification_rule_update_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if "subscription_number" not in arguments or not (set(arguments) & _RULE_FIELDS):
        raise AgentToolError("必须指定订阅编号和至少一个通知规则字段")
    if set(arguments) - ({"subscription_number"} | _RULE_FIELDS):
        raise AgentToolError("通知规则包含不支持的字段")
    result = notification_rule_arguments({
        "subscription_number": arguments.get("subscription_number")
    })
    for key in _RULE_FIELDS:
        if key in arguments:
            if not isinstance(arguments[key], bool):
                raise AgentToolError(f"{key} 必须是布尔值")
            result[key] = arguments[key]
    return result


def _client(profile: Any) -> Any:
    if profile.server_type == "jellyfin":
        from app.clients.jellyfin import JellyfinClient
        return JellyfinClient(profile.url, profile.credential)
    if profile.server_type == "emby":
        from app.clients.emby import EmbyClient
        return EmbyClient(profile.url, profile.credential)
    raise AgentToolError("媒体服务器类型不受支持", code="precondition_failed")


def explicit_preferred_download_target(owner: str) -> str:
    """只返回 owner 显式保存的下载目标；没有显式记录时保持原有澄清行为。"""
    normalized_owner = str(owner or "").strip()
    if not normalized_owner:
        return ""
    preferences = get_media_preferences(action_history_owner_digest(normalized_owner))
    if not preferences.get("explicit"):
        return ""
    target = str(preferences.get("preferred_download_target") or "").strip()
    return target if target in {"qb", "guangya", "both"} else ""


def get_continue_watching(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    preferences = get_media_preferences(_owner_digest(context))
    requested = str(arguments["server"])
    if requested == "auto" and preferences["preferred_server"] != "any":
        requested = str(preferences["preferred_server"])
    profiles = [
        item for item in list_configured_profiles()
        if item.enabled and item.configured and str(item.user_id or "").strip()
        and (requested == "auto" or item.server_type == requested)
    ]
    if not profiles:
        return ToolResult(
            False, "precondition_failed", "尚未配置可用于继续观看的显式媒体用户",
            data={"server": requested, "items": [], "user_selection": "required"},
            error="请先配置 JELLYFIN_USER_ID 或 EMBY_USER_ID；不会回退读取管理员观看历史。",
        )
    if len(profiles) != 1:
        return ToolResult(
            False, "attention", "检测到多个可用媒体服务器，请明确指定 Jellyfin 或 Emby",
            data={"servers": [item.server_type for item in profiles], "items": []},
            suggestions=["可说：查看 Jellyfin 继续观看。", "可说：查看 Emby 继续观看。"],
        )
    profile = profiles[0]
    try:
        items = _client(profile).continue_watching(
            profile.user_id, limit=int(arguments["limit"])
        )
    except Exception:
        return ToolResult(
            False, "unavailable", f"暂时无法读取 {profile.label} 继续观看",
            data={"server": profile.server_type, "items": []},
            error="媒体服务器未返回可用的继续观看列表。",
        )
    public_items = []
    for item in items[: int(arguments["limit"])]:
        public_items.append({
            "title": sanitize_public_text(item.display_name, limit=120),
            "episode_title": sanitize_public_text(item.name, limit=120),
            "media_type": (
                str(item.type or "").strip().casefold()
                if str(item.type or "").strip().casefold()
                in {"movie", "series", "episode", "video"}
                else "unknown"
            ),
            "season": item.season_number if isinstance(item.season_number, int) else None,
            "episode": item.episode_number if isinstance(item.episode_number, int) else None,
            "progress": max(0.0, min(float(item.progress or 0.0), 100.0)),
            "last_played": sanitize_public_text(item.last_played, limit=40),
        })
    return ToolResult(
        True, "completed", f"已读取 {profile.label} 的继续观看列表",
        data={
            "server": profile.server_type,
            "server_label": profile.label,
            "user_selection": "explicit_config",
            "count": len(public_items),
            "items": public_items,
        },
        evidence=[Evidence(
            "media_server_resume",
            "只使用部署级明确配置的共享媒体用户读取 Resume 列表；未回退选择管理员或其他用户，也未返回用户 ID、媒体内部 ID、URL 或凭据。",
            _now(),
        )],
    )


def get_preferences(_arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    preferences = get_media_preferences(_owner_digest(context))
    return ToolResult(
        True, "completed", "已读取当前会话的显式媒体偏好",
        data=_public_preferences(preferences),
        evidence=[Evidence(
            "sqlite:agent_media_preferences",
            "媒体偏好按会话身份摘要独立保存，不从聊天摘要或模型记忆推断。",
            _now(),
        )],
    )


def prepare_set_preferences(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    current = get_media_preferences(_owner_digest(context))
    proposed = {key: arguments.get(key, current[key]) for key in _PREFERENCE_CHOICES}
    snapshot = {"current": current, "proposed": proposed}
    return ToolResult(
        True, "confirmation_required", "确认后将保存当前会话的显式媒体偏好",
        data={"current": _public_preferences(current), "proposed": proposed},
    ), _fingerprint(snapshot)



def set_preferences_confirmed(
    arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    digest = _owner_digest(context)
    current = get_media_preferences(digest)
    proposed = {key: arguments.get(key, current[key]) for key in _PREFERENCE_CHOICES}
    snapshot = {"current": current, "proposed": proposed}
    if not secrets.compare_digest(_fingerprint(snapshot), str(expected_context or "")):
        raise AgentToolError("媒体偏好已变化，请重新预检", code="confirmation_stale")
    updated = set_media_preferences(
        digest, expected_revision=int(current["revision"]), updates=arguments
    )
    if updated is None:
        raise AgentToolError("媒体偏好已变化，请重新预检", code="confirmation_stale")
    return ToolResult(
        True, "completed", "显式媒体偏好已保存",
        data={"operation": "set_preferences", "affected": 1, **_public_preferences(updated)},
    )


def prepare_clear_preferences(
    _arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    current = get_media_preferences(_owner_digest(context))
    if not current["explicit"]:
        raise AgentToolError("当前没有已保存的显式媒体偏好", code="precondition_failed")
    return ToolResult(
        True, "confirmation_required", "确认后将清除当前会话保存的媒体偏好并恢复默认值",
        data={"current": _public_preferences(current), "defaults": default_media_preferences()},
    ), _fingerprint(current)



def clear_preferences_confirmed(
    _arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    digest = _owner_digest(context)
    current = get_media_preferences(digest)
    if not secrets.compare_digest(_fingerprint(current), str(expected_context or "")):
        raise AgentToolError("媒体偏好已变化，请重新预检", code="confirmation_stale")
    if not clear_media_preferences(digest, expected_revision=int(current["revision"])):
        raise AgentToolError("媒体偏好已变化，请重新预检", code="confirmation_stale")
    return ToolResult(
        True, "completed", "显式媒体偏好已清除",
        data={"operation": "clear_preferences", "affected": 1},
    )


def get_today_summary(_arguments: dict[str, Any], _context: ToolContext) -> ToolResult:
    summary = today_content_summary()
    summary["content_titles"] = [
        title for title in (
            sanitize_public_text(item, limit=120)
            for item in summary.get("content_titles", [])
        ) if title
    ][:8]
    return ToolResult(
        True, "completed" if summary["event_count"] else "empty",
        "已汇总今天的媒体内容动态" if summary["event_count"] else "今天暂时没有媒体内容动态",
        data=summary,
        evidence=[Evidence(
            "sqlite:content_events",
            "按本机明确日期窗口聚合追更检查、整理完成、RSS 与下载事件；优先返回内容事件，不读取凭据、路径、磁力或错误正文。",
            _now(),
        )],
    )


def get_subscription_notification_rule(
    arguments: dict[str, Any], _context: ToolContext
) -> ToolResult:
    rule = get_notification_rule(int(arguments["subscription_number"]))
    if rule is None:
        raise AgentToolError("媒体订阅不存在", code="precondition_failed")
    return ToolResult(
        True, "completed", "已读取媒体订阅通知规则",
        data=_public_notification_rule(rule),
    )


def prepare_set_subscription_notification_rule(
    arguments: dict[str, Any], _context: ToolContext
) -> tuple[ToolResult, str]:
    number = int(arguments["subscription_number"])
    current = get_notification_rule(number)
    if current is None:
        raise AgentToolError("媒体订阅不存在", code="precondition_failed")
    proposed = {key: arguments.get(key, current[key]) for key in _RULE_FIELDS}
    snapshot = {"current": current, "proposed": proposed}
    return ToolResult(
        True, "confirmation_required", f"确认后将修改媒体订阅 {number} 的通知规则",
        data={
            "subscription_number": number,
            "title": _public_notification_rule(current)["title"],
            "current": {key: current[key] for key in _RULE_FIELDS},
            "proposed": proposed,
        },
    ), _fingerprint(snapshot)



def set_subscription_notification_rule_confirmed(
    arguments: dict[str, Any], expected_context: str, _context: ToolContext
) -> ToolResult:
    number = int(arguments["subscription_number"])
    current = get_notification_rule(number)
    if current is None:
        raise AgentToolError("媒体订阅已删除", code="confirmation_stale")
    proposed = {key: arguments.get(key, current[key]) for key in _RULE_FIELDS}
    if not secrets.compare_digest(
        _fingerprint({"current": current, "proposed": proposed}),
        str(expected_context or ""),
    ):
        raise AgentToolError("通知规则或订阅已变化，请重新预检", code="confirmation_stale")
    updated = set_notification_rule(
        number,
        expected_rule_revision=int(current["revision"]),
        expected_subscription_revision=int(current["subscription_revision"]),
        updates={key: bool(arguments[key]) for key in _RULE_FIELDS if key in arguments},
    )
    if updated is None:
        raise AgentToolError("通知规则或订阅已变化，请重新预检", code="confirmation_stale")
    return ToolResult(
        True, "completed", f"媒体订阅 {number} 的通知规则已更新",
        data={"operation": "set_notification_rule", "subscription_number": number, "affected": 1, "enabled": updated["enabled"]},
    )


def prepare_reset_subscription_notification_rule(
    arguments: dict[str, Any], _context: ToolContext
) -> tuple[ToolResult, str]:
    number = int(arguments["subscription_number"])
    current = get_notification_rule(number)
    if current is None:
        raise AgentToolError("媒体订阅不存在", code="precondition_failed")
    if not current["explicit"]:
        raise AgentToolError("该订阅当前使用默认通知规则", code="precondition_failed")
    return ToolResult(
        True, "confirmation_required", f"确认后将重置媒体订阅 {number} 的通知规则",
        data={
            "subscription_number": number,
            "title": _public_notification_rule(current)["title"],
        },
    ), _fingerprint(current)



def reset_subscription_notification_rule_confirmed(
    arguments: dict[str, Any], expected_context: str, _context: ToolContext
) -> ToolResult:
    number = int(arguments["subscription_number"])
    current = get_notification_rule(number)
    if current is None or not secrets.compare_digest(
        _fingerprint(current), str(expected_context or "")
    ):
        raise AgentToolError("通知规则或订阅已变化，请重新预检", code="confirmation_stale")
    if not reset_notification_rule(
        number,
        expected_rule_revision=int(current["revision"]),
        expected_subscription_revision=int(current["subscription_revision"]),
    ):
        raise AgentToolError("通知规则或订阅已变化，请重新预检", code="confirmation_stale")
    return ToolResult(
        True, "completed", f"媒体订阅 {number} 已恢复默认通知规则",
        data={"operation": "reset_notification_rule", "subscription_number": number, "affected": 1},
    )
