"""现有周期唤醒中的确定性主动摘要/任务跟踪，不运行 LLM 或第二套通知队列。"""

from __future__ import annotations

import re
from collections.abc import Callable
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


def notification_route_settings(owner: str) -> dict[str, str]:
    """仅从已认证服务端 owner 绑定投递目标，禁止模型传 chat_id。"""
    route = parse_telegram_owner_route(owner)
    if route is not None:
        if not telegram_owner_route_is_currently_authorized(route):
            raise AgentToolError(
                "当前 Telegram 身份无权创建主动通知", code="identity_required"
            )
        return {"notification_owner": owner, "notification_chat_id": route.chat_id}
    if not re.fullmatch(r"webk:v1:[0-9a-f]{64}", owner):
        raise AgentToolError("当前身份不支持持久主动通知", code="identity_required")
    chat = str(config.get("TG_CHAT_ID", "") or "").strip()
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
        and str(config.get("TG_CHAT_ID", "") or "").strip() == chat
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
            footer="按本地日期汇总近期记录（每类最多 50 条）；详细进展仍由原任务通知更新。"
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
                    )
                    accepted = bool(outcome) or str(outcome.status) in {
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
