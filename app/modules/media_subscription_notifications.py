"""媒体订阅事务 outbox 到统一 Telegram 通知中心的可靠移交。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.modules.media_subscriptions import MediaSubscriptionError, strict_subscription_id
from app.repositories import media_experience as repository


from app.agent.public_safety import sanitize_public_text
from app.logger import get_logger
from app.notifier import NOTIFICATION_SECTION_BREAK, NotificationEvent
from app.repositories.media_experience import (
    claim_due_notifications,
    mark_notification_sent,
    recover_notifications,
    retry_notification,
)

logger = get_logger(__name__)


def _event(item: dict) -> NotificationEvent:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    title = sanitize_public_text(payload.get("title"), limit=120) or "媒体订阅"
    event_type = str(item.get("event_type") or "")
    if event_type == "missing":
        missing = max(0, int(payload.get("missing_count") or 0))
        candidates = max(0, int(payload.get("candidate_count") or 0))
        submitted = max(0, int(payload.get("auto_submitted") or 0))
        action = str(payload.get("action") or "notify").strip().lower()
        fields = [
            ("目标媒体", title),
            NOTIFICATION_SECTION_BREAK,
            ("待补内容", f"{missing} 项"),
        ]
        if candidates:
            fields.append(("候选", f"{candidates} 个"))
        if submitted:
            fields.append(("已提交", f"{submitted} 项"))
            return NotificationEvent(
                "✅ 追更已自动提交下载",
                fields=tuple(fields),
                footer="下载与后续入库状态会继续由对应任务通知更新。",
                layout="relaxed",
            )
        if action == "confirm" and candidates:
            footer = "已找到候选，请在媒体追更中确认后再提交下载。"
            heading = "⚠️ 追更候选待确认"
        elif candidates:
            footer = "已找到可用候选，请在媒体追更中查看。"
            heading = "📺 追更发现待补内容"
        else:
            footer = "本轮未找到可用候选，可稍后重试或调整资源检索条件。"
            heading = "⚠️ 追更暂无可用资源"
        return NotificationEvent(
            heading,
            fields=tuple(fields),
            footer=footer,
            layout="relaxed",
        )
    if event_type == "satisfied":
        return NotificationEvent(
            "✅ 追更已满足",
            fields=(
                ("目标媒体", title),
                NOTIFICATION_SECTION_BREAK,
                ("状态", "当前没有检测到缺失内容"),
            ),
            layout="relaxed",
        )
    if event_type == "inconclusive":
        reason = sanitize_public_text(payload.get("summary"), limit=220)
        return NotificationEvent(
            "⚠️ 追更检查无法得出结论",
            fields=(("目标媒体", title),),
            footer=reason or "请检查媒体服务器连接、媒体库映射与 TMDB 数据后重试。",
            layout="relaxed",
        )
    return NotificationEvent(
        "⚠️ 追更检查异常",
        fields=(("目标媒体", title),),
        footer="本轮检查未能完成，可稍后重试。",
        layout="relaxed",
    )


def drain_media_subscription_notifications(*, limit: int = 20) -> bool:
    """移交订阅事务事件；领域记录只在统一通知中心接纳后确认。"""
    # 主动摘要复用同一周期唤醒与统一通知 outbox，不建立 Agent 专用线程。
    from app.modules.media_automation_rules import drain_automation_rules

    try:
        drain_automation_rules(limit=limit)
    except Exception as exc:
        logger.exception("主动媒体摘要调度失败 type=%s", type(exc).__name__)
    recover_notifications()
    claimed = claim_due_notifications(limit=limit)
    if not claimed:
        return True
    delivered = True
    from app.modules.telegram_notification_center import publish_notification_event
    from app.modules.telegram_notification_policy import (
        NotificationImportance,
        NotificationTopic,
        notifications_enabled,
    )

    for item in claimed:
        generation = int(item["lease_generation"])
        try:
            event_type = str(item.get("event_type") or "")
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            missing_actionable = (
                event_type == "missing" and int(payload.get("auto_submitted") or 0) <= 0
            )
            outcome = publish_notification_event(
                f"media-subscription:{item['id']}:{event_type}",
                _event(item),
                topic=NotificationTopic.MEDIA_SUBSCRIPTION,
                importance=(
                    NotificationImportance.ERROR
                    if event_type not in {"missing", "satisfied"}
                    else NotificationImportance.ACTION
                    if missing_actionable
                    else NotificationImportance.RESULT
                ),
            )
            # 全局开关或通知等级主动抑制都属于已执行的消费策略；事务 outbox
            # 不应把同一条被策略拒绝的结果反复移交和重试。
            accepted = (
                bool(outcome)
                or (outcome.status or "") in {"disabled", "suppressed"}
                or not notifications_enabled()
            )
            if accepted:
                if not mark_notification_sent(item["id"], lease_generation=generation):
                    delivered = False
                continue
            delivered = False
            retry_notification(
                item["id"],
                lease_generation=generation,
                error=(outcome.status or "telegram_unavailable"),
            )
        except Exception as exc:
            # 单条内容、移交或 ACK 失败不能扣住同批次的其他领取项。
            delivered = False
            logger.warning(
                "媒体追更通知移交失败 notification=%s type=%s",
                item["id"], type(exc).__name__,
            )
            try:
                retry_notification(
                    item["id"], lease_generation=generation,
                    error="telegram_exception",
                )
            except Exception as retry_exc:
                # 重试状态也无法落盘时保留 sending，由既有租约恢复接管。
                logger.warning(
                    "媒体追更通知重试状态保存失败 notification=%s type=%s",
                    item["id"], type(retry_exc).__name__,
                )
    return delivered


def recover_media_subscription_notifications() -> int:
    recovered = recover_notifications()
    try:
        drain_media_subscription_notifications()
    except Exception as exc:
        logger.exception("媒体订阅通知恢复投递失败 type=%s", type(exc).__name__)
    return recovered


_RULE_FIELDS = {"enabled", "notify_on_missing", "notify_on_satisfied", "notify_on_error"}


def notification_arguments(arguments: dict[str, Any], *, reset: bool = False) -> dict[str, Any]:
    if (not isinstance(arguments, dict) or "subscription_id" not in arguments
            or set(arguments) - {"subscription_id", *_RULE_FIELDS}
            or (reset and set(arguments) != {"subscription_id"}) or (not reset and len(arguments) < 2)):
        raise MediaSubscriptionError("必须指定订阅编号和至少一个通知字段；重置仅接受订阅编号")
    for key in _RULE_FIELDS & arguments.keys():
        if not isinstance(arguments[key], bool):
            raise MediaSubscriptionError(f"{key} 必须是布尔值")
    return {**arguments, "subscription_id": strict_subscription_id(arguments["subscription_id"])}


def notification_rule(subscription_id: int) -> dict[str, Any]:
    rule = repository.get_notification_rule(strict_subscription_id(subscription_id))
    if rule is None:
        raise MediaSubscriptionError("媒体订阅不存在", code="precondition_failed")
    return rule


def prepare_rule_change(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if operation not in {"set_notification", "reset_notification"}:
        raise MediaSubscriptionError("通知规则操作无效")
    args = notification_arguments(arguments, reset=operation == "reset_notification")
    current = notification_rule(args["subscription_id"])
    if operation == "reset_notification" and not current["explicit"]:
        raise MediaSubscriptionError("该订阅当前使用默认通知规则", code="precondition_failed")
    proposed = {key: args.get(key, current[key]) for key in _RULE_FIELDS} if operation == "set_notification" else None
    return {"arguments": args, "snapshot": {"operation": operation, "arguments": args, "current": current},
            "preview": {"ok": True, "status": "confirmation_required", "summary": "确认后修改全局媒体订阅通知规则",
                        "data": {"subscription_number": args["subscription_id"], "title": current["title"], "current": current, "proposed": proposed,
                                 "effects": ["只改变此订阅通知开关或恢复默认，不改巡检策略；通知仍沿既有通知中心及投递授权。"]}}}


def apply_rule_change(operation: str, arguments: dict[str, Any], *, expected_snapshot: dict[str, Any],
                      check_cancelled: Callable[[], None] = lambda: None) -> dict[str, Any]:
    prepared = prepare_rule_change(operation, arguments)
    if prepared["snapshot"] != expected_snapshot:
        raise MediaSubscriptionError("通知规则或订阅已变化，请重新预检", code="confirmation_stale")
    current = expected_snapshot["current"]
    args = prepared["arguments"]
    scope = {"expected_rule_revision": current["revision"], "expected_subscription_revision": current["subscription_revision"], "expected_rule": current,
             "check_cancelled": check_cancelled}
    check_cancelled()
    if operation == "set_notification":
        updated = repository.set_notification_rule(args["subscription_id"], **scope, updates={key: args[key] for key in args.keys() & _RULE_FIELDS})
    else:
        updated = repository.reset_notification_rule(args["subscription_id"], **scope)
    if not updated:
        raise MediaSubscriptionError("通知规则或订阅已变化，请重新预检", code="confirmation_stale")
    if operation == "reset_notification":
        # 返回本次事务的默认规则事实；不把后来另一写者的规则冒充重置回执。
        updated = {**current, **repository._DEFAULT_RULE, "revision": 0, "explicit": False}
    return {"ok": True, "status": "completed", "summary": "媒体订阅通知规则已更新" if operation == "set_notification" else "媒体订阅已恢复默认通知规则", "data": updated}


def today_summary() -> dict[str, Any]:
    from app.agent.public_safety import sanitize_public_text

    summary = repository.today_content_summary()
    summary["content_titles"] = [title for item in summary["content_titles"] if (title := sanitize_public_text(item, limit=120))][:8]
    return summary
