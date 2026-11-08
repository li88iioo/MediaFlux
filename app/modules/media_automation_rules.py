"""现有周期唤醒中的确定性主动摘要/任务跟踪，不运行 LLM 或第二套通知队列。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app import config
from app.agent.errors import AgentToolError
from app.agent.feature_gate import (
    AgentRuntimeDisabled,
    agent_runtime_admission,
    is_agent_enabled,
)
from app.agent.owner_routes import (
    normalize_delivery_route,
    parse_telegram_owner_route,
    telegram_owner_route_is_currently_authorized,
)
from app.agent.public_safety import sanitize_public_text
from app.logger import get_logger
from app.modules.telegram_notification_center import publish_notification_event
from app.modules.telegram_notification_policy import (
    NotificationImportance,
    NotificationTopic,
)
from app.notifier import NotificationEvent
from app.repositories import media_automation_rules as rules
from app.repositories.agent_jobs import agent_job_owner_digest
from app.repositories.media_experience import today_content_summary

logger = get_logger(__name__)


@dataclass(frozen=True)
class RuleDelivery:
    notification: NotificationEvent | None
    logical_key: str
    next_run_at: str
    importance: str = "result"
    terminal: bool = False
    chat_id: str = ""


RuleHandler = Callable[[dict[str, Any], datetime], RuleDelivery]
_HANDLERS: dict[str, RuleHandler] = {}


def register_rule_handler(kind: str, handler: RuleHandler) -> None:
    if kind not in rules.KINDS:
        raise ValueError("未支持的主动规则类型")
    _HANDLERS[kind] = handler


def notification_route_settings(
    owner: str, *, delivery_route: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """仅从已认证服务端 owner 绑定投递目标，禁止模型传 chat_id。"""
    route = parse_telegram_owner_route(owner)
    if route is not None:
        if not telegram_owner_route_is_currently_authorized(route):
            raise AgentToolError(
                "当前 Telegram 身份无权创建主动通知", code="identity_required"
            )
        try:
            delivery = normalize_delivery_route(delivery_route or {"chat_id": route.chat_id}, owner=owner)
        except ValueError as exc:
            raise AgentToolError("当前 Telegram 通知话题无效", code="identity_required") from exc
        settings: dict[str, Any] = {"notification_owner": owner, "notification_chat_id": delivery["chat_id"]}
        if "message_thread_id" in delivery:
            settings["notification_message_thread_id"] = delivery["message_thread_id"]
        return settings
    if not re.fullmatch(r"webk:v1:[0-9a-f]{64}", owner):
        raise AgentToolError("当前身份不支持持久主动通知", code="identity_required")
    chat = (config.get("TG_CHAT_ID", "") or "").strip()
    if not re.fullmatch(r"-?[1-9][0-9]*", chat):
        raise AgentToolError("请先配置 Telegram 通知目标", code="not_configured")
    return {"notification_owner": owner, "notification_chat_id": chat}


def _authorized_notification_chat(rule: dict[str, Any]) -> str:
    settings = rule["settings"]
    owner = str(settings.get("notification_owner", ""))
    chat = str(settings.get("notification_chat_id", ""))
    if not owner or not chat or agent_job_owner_digest(owner) != rule["owner_digest"]:
        return ""
    route = parse_telegram_owner_route(owner)
    if route is not None:
        return (
            chat
            if telegram_owner_route_is_currently_authorized(route, chat_id=chat)
            else ""
        )
    if (
        re.fullmatch(r"webk:v1:[0-9a-f]{64}", owner)
        and (config.get("TG_CHAT_ID", "") or "").strip() == chat
    ):
        return chat
    return ""


def next_summary_at(settings: dict[str, Any], now: datetime | None = None) -> str:
    clock = now or datetime.now().astimezone()
    candidate = clock.replace(
        hour=int(settings["hour"]),
        minute=int(settings["minute"]),
        second=0,
        microsecond=0,
    )
    if candidate <= clock:
        candidate += timedelta(days=1)
    return candidate.isoformat(timespec="seconds")


_DIGEST_DEFAULTS = {"hour": 0, "minute": 0, "errors_only": False, "send_empty": False}


def digest_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {"rule_id", "enabled", *_DIGEST_DEFAULTS} or "enabled" not in arguments:
        raise AgentToolError("规则参数缺失或包含不支持字段")
    for key in ("enabled", "errors_only", "send_empty"):
        if key in arguments and not isinstance(arguments[key], bool):
            raise AgentToolError(f"{key} 必须是布尔值")
    for key, maximum in (("hour", 23), ("minute", 59)):
        value = arguments.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise AgentToolError(f"{key} 超出有效时间范围")
    rule_id = arguments.get("rule_id", "")
    if not isinstance(rule_id, str) or len(rule_id) > 100:
        raise AgentToolError("rule_id 无效")
    if not rule_id and "hour" not in arguments:
        raise AgentToolError("新增摘要规则必须指定 hour；修改已有规则请提供 rule_id")
    return {"rule_id": rule_id, **arguments}


def _public_digest(rule: dict[str, Any]) -> dict[str, Any]:
    return {"rule_id": rule["id"], **{key: rule[key] for key in ("kind", "enabled", "revision", "next_run_at", "created_at", "updated_at")},
            "settings": {key: rule["settings"].get(key) for key in _DIGEST_DEFAULTS},
            **({"settings_error": rule["settings_error"]} if rule.get("settings_error") else {})}


def digests(*, owner: str) -> dict[str, Any]:
    rows = rules.list_rules(agent_job_owner_digest(owner), kind="daily_summary")
    return {"ok": True, "status": "completed", "summary": "已读取主动摘要规则",
            "data": {"items": [_public_digest(row) for row in rows]}}


def prepare_set_digest(arguments: dict[str, Any], *, owner: str,
                       delivery_route: Mapping[str, Any] | None = None) -> dict[str, Any]:
    args = digest_arguments(arguments)
    digest = agent_job_owner_digest(owner)
    current = rules.get_rule(digest, args["rule_id"]) if args["rule_id"] else None
    if args["rule_id"] and (current is None or current["kind"] != "daily_summary"):
        raise AgentToolError("摘要规则不存在", code="precondition_failed")
    previous = current["settings"] if current else {}
    settings = {key: args.get(key, previous.get(key, default)) for key, default in _DIGEST_DEFAULTS.items()}
    digest_arguments({"enabled": args["enabled"], **settings})
    # 停用沿用已保存目标；启用仍核对宿主真实身份与话题。
    route = ({key: previous[key] for key in ("notification_owner", "notification_chat_id", "notification_message_thread_id") if key in previous}
             if current and not args["enabled"] else notification_route_settings(owner, delivery_route=delivery_route))
    snapshot = {"owner": digest, "arguments": args, "revision": current["revision"] if current else 0, "route": route}
    return {"snapshot": snapshot, "settings": settings, "preview": {
        "ok": True, "status": "confirmation_required",
        "summary": "确认后修改每日主动摘要规则" if current else "确认后新增每日主动摘要规则",
        "data": {**args, **settings, "timezone": str(datetime.now().astimezone().tzinfo),
                 "delivery": "复用 Telegram 通知中心与既有全局通知开关",
                 "effects": ["每天到指定本地时刻汇总当前项目的媒体动态；停用后不再生成摘要。"]}}}


def set_digest(arguments: dict[str, Any], *, owner: str, expected_snapshot: dict[str, Any],
               delivery_route: Mapping[str, Any] | None = None,
               check_cancelled: Callable[[], None] = lambda: None) -> dict[str, Any]:
    args = digest_arguments(arguments)
    if expected_snapshot.get("owner") != agent_job_owner_digest(owner) or expected_snapshot.get("arguments") != args:
        raise AgentToolError("摘要确认上下文无效", code="confirmation_invalid")
    prepared = prepare_set_digest(args, owner=owner, delivery_route=delivery_route)
    if expected_snapshot != prepared["snapshot"]:
        raise AgentToolError("摘要规则或通知目标已变化，请重新预检", code="precondition_failed")
    settings = {**prepared["settings"], **expected_snapshot["route"]}
    check_cancelled()
    row = rules.save_rule(expected_snapshot["owner"], rule_id=args["rule_id"], kind="daily_summary", settings=settings,
                          enabled=args["enabled"], next_run_at=next_summary_at(settings), expected_revision=expected_snapshot["revision"])
    if row is None:
        raise AgentToolError("摘要规则已变化，请重新预检", code="precondition_failed")
    summary = "主动摘要规则已保存" if row["enabled"] else "主动摘要已停用"
    if not args["rule_id"]:
        summary = "主动摘要规则已新增" + ("（当前停用）" if not row["enabled"] else "")
    return {"ok": True, "status": "completed", "summary": summary, "data": _public_digest(row)}


def _daily_summary(rule: dict[str, Any], now: datetime) -> RuleDelivery:
    settings = rule["settings"]
    summary = today_content_summary()
    errors_only = bool(settings.get("errors_only", False))
    groups = (
        ("追更", "subscription_runs"),
        ("本地整理", "local_media_tasks"),
        ("RSS", "rss_entries"),
        ("下载", "downloads"),
    )
    fields = [("日期", str(summary.get("local_date", now.date().isoformat())))]
    total = 0
    for label, key in groups:
        counts = summary.get(key, {})
        if not isinstance(counts, dict):
            continue
        if errors_only:
            counts = {
                name: count
                for name, count in counts.items()
                if name in {"failed", "attention"}
            }
        count = sum(max(0, int(value)) for value in counts.values())
        total += count
        if count:
            labels = {
                "failed": "失败",
                "attention": "需关注",
                "completed": "完成",
                "success": "成功",
                "missing": "缺失",
                "satisfied": "完整",
                "submitted": "已提交",
                "downloaded": "已下载",
                "pending": "待处理",
                "processing": "处理中",
                "skipped": "跳过",
                "cancelled": "取消",
            }
            fields.append(
                (
                    label,
                    " · ".join(
                        f"{labels.get(key, key)} {value}"
                        for key, value in counts.items()
                    ),
                )
            )
    titles = [
        sanitize_public_text(item, limit=80)
        for item in summary.get("content_titles", [])[:8]
    ]
    if titles and not errors_only:
        fields.append(("相关作品", "、".join(title for title in titles if title)))
    event = None
    if total or bool(settings.get("send_empty", False)):
        event = NotificationEvent(
            "⚠️ 今日媒体异常摘要" if errors_only else "📋 今日媒体动态",
            fields=tuple(fields),
            footer="计数覆盖本地当日全部记录，相关作品按近期记录抽样；详细进展仍由原任务通知更新。"
            if total
            else "今天暂时没有符合条件的媒体动态。",
        )
    return RuleDelivery(
        event,
        f"{rule['id']}:{rule['revision']}:{now.date().isoformat()}",
        next_summary_at(settings, now),
        "error" if errors_only else "result",
    )


register_rule_handler("daily_summary", _daily_summary)


def drain_automation_rules(*, now: datetime | None = None, limit: int = 20) -> int:
    """由既有调度周期调用，入统一通知中心成功后才推进规则；失败保留可重试。"""
    if not is_agent_enabled():
        return 0
    clock = now or datetime.now().astimezone()
    completed = 0
    for rule in rules.claim_due_rules(clock, limit=limit):
        try:
            chat = _authorized_notification_chat(rule)
            if not chat:
                rules.finish_rule(
                    rule["id"],
                    rule["lease_token"],
                    (clock + timedelta(minutes=5)).isoformat(timespec="seconds"),
                )
                continue
            if rule["kind"] == "activity_follow":
                from app.modules.activity_follow_notifications import (
                    deliver_activity_follow,
                )

                register_rule_handler("activity_follow", deliver_activity_follow)
            handler = _HANDLERS.get(rule["kind"])
            if handler is None:
                # 模块未加载不可假装已执行；有限退避并保留规则。
                rules.finish_rule(
                    rule["id"],
                    rule["lease_token"],
                    (clock + timedelta(minutes=5)).isoformat(timespec="seconds"),
                )
                continue
            delivery = handler(rule, clock)
            if delivery.chat_id and delivery.chat_id != chat:
                raise ValueError("主动规则 handler 不得重定向通知目标")
            # 与确认路径保持同样锁顺序：运行态准入 → 规则发布锁。
            with (
                agent_runtime_admission(agent_enabled_check=is_agent_enabled),
                rules.publication_guard(),
            ):
                if (
                    not rules.owns_lease(rule["id"], rule["lease_token"])
                    or _authorized_notification_chat(rule) != chat
                ):
                    continue
                accepted = delivery.notification is None
                if delivery.notification is not None:
                    outcome = publish_notification_event(
                        "automation:" + delivery.logical_key,
                        delivery.notification,
                        topic=NotificationTopic.AGENT,
                        importance=NotificationImportance(delivery.importance),
                        chat_id=chat,
                        deliver_now=False,
                        delivery_route={"chat_id": chat, "message_thread_id": rule["settings"].get("notification_message_thread_id")},
                    )
                    accepted = bool(outcome) or outcome.status in {
                        "disabled",
                        "suppressed",
                    }
                if accepted:
                    completed += int(
                        rules.finish_rule(
                            rule["id"],
                            rule["lease_token"],
                            delivery.next_run_at,
                            disable=delivery.terminal,
                        )
                    )
        except AgentRuntimeDisabled:
            # 正常停用，保留领取记录等待租约过期；重新启用后继续核对。
            continue
        except Exception as exc:
            logger.exception(
                "主动媒体规则执行失败 kind=%s type=%s", rule["kind"], type(exc).__name__
            )
    return completed
