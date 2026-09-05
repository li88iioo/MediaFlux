"""把自然语言选择的规则映射到既有订阅服务和确定性摘要规则。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.agent.errors import AgentToolError
from app.agent.media_subscription_actions import (
    _create_snapshot,
    _decode_create_context,
    _reload_scheduler,
    get_media_subscription_service,
    media_subscription_create_arguments,
    prepare_create_media_subscription,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.public_safety import sanitize_public_text
from app.indexers.config import normalize_indexer_site_ids
from app.indexers.runtime import run_indexer_awaitable_sync
from app.modules.media_automation_rules import (
    next_summary_at,
    notification_route_settings,
)
from app.modules.media_subscriptions import MediaSubscriptionError
from app.repositories import media_automation_rules as rules
from app.repositories.agent_jobs import agent_job_owner_digest


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strict_args(
    arguments: dict[str, Any], allowed: set[str], required: set[str] = frozenset()
) -> None:
    if (
        not isinstance(arguments, dict)
        or set(arguments) - allowed
        or not required <= set(arguments)
    ):
        raise AgentToolError("规则参数缺失或包含不支持字段")


def create_media_rule_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "tmdb_id",
        "media_type",
        "season",
        "check_interval_minutes",
        "action",
        "download_target",
        "sites",
        "enabled",
    }
    _strict_args(arguments, allowed, {"tmdb_id", "media_type"})
    base = media_subscription_create_arguments(
        {
            "provider": "tmdb",
            "external_id": str(arguments["tmdb_id"]),
            "media_type": arguments["media_type"],
            **{
                key: arguments[key]
                for key in ("season", "check_interval_minutes")
                if key in arguments
            },
        }
    )
    if (
        not base["external_id"].isascii()
        or not base["external_id"].isdigit()
        or int(base["external_id"]) < 1
    ):
        raise AgentToolError("tmdb_id 必须是有效 TMDB 编号")
    action = arguments.get("action", "confirm")
    target = arguments.get("download_target", "guangya")
    if action not in {"notify", "confirm", "auto"} or target not in {
        "guangya",
        "qb",
        "both",
    }:
        raise AgentToolError("action 或 download_target 无效")
    if not isinstance(arguments.get("enabled", True), bool):
        raise AgentToolError("enabled 必须是布尔值")
    try:
        sites = list(normalize_indexer_site_ids(arguments.get("sites", [])))
    except ValueError as exc:
        raise AgentToolError(str(exc)) from exc
    return {
        "tmdb_id": str(int(base["external_id"])),
        "media_type": base["media_type"],
        "season": base.get("season"),
        "check_interval_minutes": base.get("check_interval_minutes", 10080),
        "action": action,
        "download_target": target,
        "sites": sites,
        "enabled": arguments.get("enabled", True),
    }


def prepare_create_media_rule(arguments: dict[str, Any]) -> tuple[ToolResult, str]:
    args = create_media_rule_arguments(arguments)
    base = {
        "provider": "tmdb",
        "external_id": args["tmdb_id"],
        "media_type": args["media_type"],
        "check_interval_minutes": args["check_interval_minutes"],
    }
    if args["season"] is not None:
        base["season"] = args["season"]
    preview, token = prepare_create_media_subscription(base)
    frozen = _decode_create_context(token)
    if frozen["snapshot"].get("exists") and not frozen["snapshot"].get("deleted_at"):
        raise AgentToolError(
            "该媒体已存在订阅，请修改现有订阅策略而不是重复创建",
            code="precondition_failed",
        )
    preview.summary = "确认后创建持续运行的媒体追更规则"
    preview.data.update(
        {
            "action": args["action"],
            "download_target": args["download_target"],
            "sites": args["sites"],
            "enabled": args["enabled"],
            "schedule": f"每 {args['check_interval_minutes'] // 1440} 天（不是固定星期或时刻）",
            "effects": [
                "由既有媒体订阅调度器检查媒体库缺失并搜索资源站，不定时调用 LLM。",
                "自动模式会持续自动提交符合既有严格匹配规则的资源，后续每次下载不再弹 Agent 确认。"
                if args["action"] == "auto"
                else "候选资源按提醒或人工确认策略处理，不会绕过后续确认。",
                "下载后的整理、STRM 和媒体库刷新只复用已经配置的联动；不会创建或打开未配置的联动。",
            ],
            "selection_policy": "复用既有资源站质量规则；本工具不支持独立的4K/字幕硬过滤或固定星期调度。",
        }
    )
    return preview, _encode({"arguments": args, "subscription_context": frozen})


def create_media_rule_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    args = create_media_rule_arguments(arguments)
    frozen = _decode_create_context(expected_context)
    if frozen.get("arguments") != args:
        raise AgentToolError("自动化计划参数已变化", code="confirmation_invalid")
    context = frozen.get("subscription_context", {})
    if _create_snapshot(args["tmdb_id"], args["media_type"]) != context.get("snapshot"):
        raise AgentToolError(
            "订阅状态在确认前已变化，请重新预检", code="precondition_failed"
        )
    payload = {
        key: args[key]
        for key in (
            "tmdb_id",
            "media_type",
            "action",
            "download_target",
            "sites",
            "enabled",
            "check_interval_minutes",
        )
    }
    payload.update(
        {
            "provider": "tmdb",
            "external_id": args["tmdb_id"],
            "include_specials": False,
            "monitor_mode": "selected" if args["season"] else "missing",
            "seasons": [args["season"]] if args["season"] else [],
        }
    )
    try:
        service = get_media_subscription_service()
        result = run_indexer_awaitable_sync(
            service.create_subscription(payload), timeout_seconds=35.0
        )
    except MediaSubscriptionError as exc:
        raise AgentToolError(str(exc), code=exc.code) from exc
    except (RuntimeError, TimeoutError) as exc:
        raise AgentToolError(
            "订阅服务未能完成写入，请读取订阅列表核实后重试", code="unavailable"
        ) from exc
    row = result.get("subscription", {}) if isinstance(result, dict) else {}
    if not row.get("id"):
        return ToolResult(False, "unavailable", "订阅服务未返回可核实的规则编号")
    verified = service.get_subscription(int(row["id"]))
    fields = (
        "tmdb_id",
        "media_type",
        "action",
        "download_target",
        "sites",
        "enabled",
        "check_interval_minutes",
    )
    ok = all(verified.get(key) == payload[key] for key in fields)
    return ToolResult(
        ok,
        "completed" if ok else "partial",
        "媒体追更规则已创建并回读核对" if ok else "规则写入后回读不一致，请核对设置",
        data={
            "subscription_number": int(row["id"]),
            "title": sanitize_public_text(verified.get("title"), limit=120),
            **{key: verified.get(key) for key in fields},
            "runtime_refreshed": _reload_scheduler(),
        },
    )


def digest_list_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _strict_args(arguments, set())
    return {}


def digest_set_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    _strict_args(
        arguments,
        {"rule_id", "enabled", "hour", "minute", "errors_only", "send_empty"},
        {"enabled", "hour"},
    )
    for key, default in (
        ("enabled", False),
        ("errors_only", False),
        ("send_empty", False),
    ):
        if not isinstance(arguments.get(key, default), bool):
            raise AgentToolError(f"{key} 必须是布尔值")
    for key, maximum in (("hour", 23), ("minute", 59)):
        value = arguments.get(key, 0)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= maximum
        ):
            raise AgentToolError(f"{key} 超出有效时间范围")
    rule_id = arguments.get("rule_id", "")
    if not isinstance(rule_id, str) or len(rule_id) > 100:
        raise AgentToolError("rule_id 无效")
    return {
        "rule_id": rule_id,
        "enabled": arguments["enabled"],
        "hour": arguments["hour"],
        "minute": arguments.get("minute", 0),
        "errors_only": arguments.get("errors_only", False),
        "send_empty": arguments.get("send_empty", False),
    }


def _public_rule(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule_id": rule["id"],
        "kind": rule["kind"],
        "enabled": rule["enabled"],
        "revision": rule["revision"],
        "next_run_at": rule["next_run_at"],
        "settings": {
            key: rule["settings"].get(key)
            for key in ("hour", "minute", "errors_only", "send_empty")
        },
    }


def list_digest_rules(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    digest_list_arguments(arguments)
    rows = rules.list_rules(agent_job_owner_digest(context.owner))
    return ToolResult(
        True,
        "completed",
        "已读取主动摘要规则",
        data={
            "items": [
                _public_rule(row) for row in rows if row["kind"] == "daily_summary"
            ]
        },
    )


def prepare_set_digest(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    args = digest_set_arguments(arguments)
    owner = agent_job_owner_digest(context.owner)
    route = notification_route_settings(context.owner)
    current = rules.get_rule(owner, args["rule_id"]) if args["rule_id"] else None
    if args["rule_id"] and (current is None or current["kind"] != "daily_summary"):
        raise AgentToolError("摘要规则不存在", code="precondition_failed")
    frozen = {
        "owner": owner,
        "arguments": args,
        "revision": current["revision"] if current else 0,
        "route": route,
    }
    return ToolResult(
        True,
        "confirmation_required",
        "确认后保存每日主动摘要规则",
        data={
            **args,
            "timezone": str(datetime.now().astimezone().tzinfo),
            "delivery": "复用 Telegram 通知中心与既有全局通知开关",
            "effects": [
                "每天到指定本地时刻汇总当前项目的媒体动态；停用后不再生成摘要。"
            ],
        },
    ), _encode(frozen)


def set_digest_confirmed(
    arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    args = digest_set_arguments(arguments)
    owner = agent_job_owner_digest(context.owner)
    frozen = _decode_create_context(expected_context)
    if frozen.get("owner") != owner or frozen.get("arguments") != args:
        raise AgentToolError("摘要确认上下文无效", code="confirmation_invalid")
    settings = {
        key: args[key] for key in ("hour", "minute", "errors_only", "send_empty")
    }
    route = notification_route_settings(context.owner)
    if frozen.get("route") != route:
        raise AgentToolError("通知目标已变化，请重新预检", code="precondition_failed")
    settings.update(route)
    row = rules.save_rule(
        owner,
        rule_id=args["rule_id"],
        kind="daily_summary",
        settings=settings,
        enabled=args["enabled"],
        next_run_at=next_summary_at(settings),
        expected_revision=int(frozen["revision"]),
    )
    if row is None:
        raise AgentToolError("摘要规则已变化，请重新预检", code="precondition_failed")
    return ToolResult(
        True,
        "completed",
        "主动摘要规则已保存" if row["enabled"] else "主动摘要已停用",
        data=_public_rule(row),
    )
