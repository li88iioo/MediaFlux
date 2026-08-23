"""媒体订阅通知 outbox 的幂等投递与恢复。"""
from __future__ import annotations

from html import escape

from app.agent.result_projection import sanitize_public_text
from app.logger import get_logger
from app.notifier import TelegramSendResult
from app.repositories.media_experience import (
    claim_due_notifications,
    mark_notification_sent,
    recover_notifications,
    retry_notification,
)

logger = get_logger(__name__)


def _body(item: dict) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    title = escape(
        sanitize_public_text(payload.get("title"), limit=120) or "媒体订阅",
        quote=False,
    )
    event_type = str(item.get("event_type") or "")
    if event_type == "missing":
        missing = max(0, int(payload.get("missing_count") or 0))
        return f"📺 追更更新\n{title}\n检测到 {missing} 项待补内容。"
    if event_type == "satisfied":
        return f"✅ 追更已满足\n{title}\n当前没有检测到缺失内容。"
    return f"⚠️ 追更检查异常\n{title}\n本轮检查未能完成，可稍后重试。"


def drain_media_subscription_notifications(*, limit: int = 20) -> bool:
    """至少一次投递：事件入队幂等；过期 lease 可恢复，崩溃窗口可能重复通知。"""
    recover_notifications()
    claimed = claim_due_notifications(limit=limit)
    if not claimed:
        return True
    delivered = True
    from app.notifier import send_result
    for item in claimed:
        try:
            outcome = send_result(_body(item))
        except Exception:
            outcome = TelegramSendResult(
                ok=False, error="telegram_exception"
            )
        generation = int(item["lease_generation"])
        if outcome.ok:
            if not mark_notification_sent(item["id"], lease_generation=generation):
                delivered = False
            continue
        delivered = False
        retry_notification(
            item["id"],
            lease_generation=generation,
            error=(
                "telegram_rate_limited"
                if int(outcome.retry_after_seconds or 0) > 0
                else "telegram_unavailable"
            ),
            retry_after_seconds=outcome.retry_after_seconds,
        )
    return delivered


def recover_media_subscription_notifications() -> int:
    recovered = recover_notifications()
    try:
        drain_media_subscription_notifications()
    except Exception as exc:
        logger.warning("媒体订阅通知恢复投递失败 type=%s", type(exc).__name__)
    return recovered
