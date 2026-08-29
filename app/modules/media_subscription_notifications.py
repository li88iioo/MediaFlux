"""媒体订阅通知 outbox 的幂等投递与恢复。"""
from __future__ import annotations

from app.agent.result_projection import sanitize_public_text
from app.logger import get_logger
from app.notifier import NotificationEvent
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
        return NotificationEvent(
            "📺 追更发现待补内容",
            fields=(("媒体", title), ("待补", f"{missing} 项")),
            footer="可在媒体追更中查看范围并决定是否下载。",
        )
    if event_type == "satisfied":
        return NotificationEvent(
            "✅ 追更已满足",
            fields=(("媒体", title), ("状态", "当前没有检测到缺失内容")),
        )
    return NotificationEvent(
        "⚠️ 追更检查异常",
        fields=(("媒体", title),),
        footer="本轮检查未能完成，可稍后重试。",
    )


def drain_media_subscription_notifications(*, limit: int = 20) -> bool:
    """把旧订阅 outbox 转交统一通知中心；旧记录只在接纳后确认。"""
    recover_notifications()
    claimed = claim_due_notifications(limit=limit)
    if not claimed:
        return True
    delivered = True
    from app.modules.telegram_notification_center import publish_notification_event
    from app.modules.telegram_notification_policy import (
        NotificationImportance, NotificationTopic, notifications_enabled,
    )

    for item in claimed:
        generation = int(item["lease_generation"])
        event_type = str(item.get("event_type") or "")
        outcome = publish_notification_event(
            f"media-subscription:{item['id']}:{event_type}",
            _event(item),
            topic=NotificationTopic.MEDIA_SUBSCRIPTION,
            importance=(
                NotificationImportance.ERROR
                if event_type not in {"missing", "satisfied"}
                else NotificationImportance.RESULT
            ),
        )
        # 用户主动关闭全局通知属于消费策略，不应让旧 outbox 无限重试。
        accepted = bool(outcome) or not notifications_enabled()
        if accepted:
            if not mark_notification_sent(item["id"], lease_generation=generation):
                delivered = False
            continue
        delivered = False
        retry_notification(
            item["id"],
            lease_generation=generation,
            error=str(outcome.status or "telegram_unavailable"),
        )
    return delivered


def recover_media_subscription_notifications() -> int:
    recovered = recover_notifications()
    try:
        drain_media_subscription_notifications()
    except Exception as exc:
        logger.warning("媒体订阅通知恢复投递失败 type=%s", type(exc).__name__)
    return recovered
