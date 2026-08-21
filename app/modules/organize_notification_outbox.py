"""整理通知投递：先入队再发送，失败留给持久队列重试。"""
from __future__ import annotations

import hashlib
import uuid

from app.logger import get_logger
from app.notifier import TelegramSendResult
from app.repositories.organize_notifications import (
    claim_due_organize_notifications,
    count_pending_organize_notifications,
    enqueue_organize_notification,
    mark_organize_notification_sent,
    organize_notification_state,
    recover_stale_organize_notifications,
    retry_organize_notification,
)

logger = get_logger(__name__)


def summary_idempotency_key(task_id: str, *, chat_id: str = "") -> str:
    """只按任务 ID 建立幂等键。

    内容哈希不能作为跨任务幂等键：两次结果完全相同的整理是不同事件，
    按内容去重会让第二次通知被永久静音。
    """
    task = str(task_id or "").strip()
    if not task:
        return ""
    return f"organize-summary:{task}:{str(chat_id or '')}"


def _one_shot_key(body: str, chat_id: str) -> str:
    """无稳定幂等键时的一次性重试键，绝不与其他任务发生去重。"""
    digest = hashlib.sha256(
        f"{chat_id or ''}\x1f{body}".encode("utf-8")
    ).hexdigest()[:24]
    return f"organize-summary-once:{digest}:{uuid.uuid4().hex[:12]}"


def _send(body: str, chat_id: str, *, image_url: str = "") -> TelegramSendResult:
    from app.notifier import send_result

    if str(image_url or "").strip():
        return send_result(
            body, chat_id=chat_id or None, image_url=str(image_url).strip(),
        )
    return send_result(body, chat_id=chat_id or None)


def deliver_organize_notification(
    idempotency_key: str, body: str, *, chat_id: str = "", image_url: str = "",
) -> bool:
    """投递整理结果通知；失败留给持久队列按退避重试。"""
    text = str(body or "")
    if not text:
        return False
    key = str(idempotency_key or "").strip()
    if key:
        state = organize_notification_state(key)
        if state == "sent":
            # 已成功投递过同一事件，重试不得产生重复消息。
            logger.info("整理通知已成功投递，跳过重复发送 key=%s", key)
            return True
        if not state:
            enqueue_organize_notification(
                key, text, chat_id=chat_id, image_url=image_url,
            )
        return drain_organize_notifications(limit=5)

    try:
        outcome = _send(text, chat_id, image_url=image_url)
        if outcome.ok:
            return True
    except Exception as exc:
        logger.warning("整理通知投递异常 type=%s", type(exc).__name__)
    enqueue_organize_notification(
        _one_shot_key(text, chat_id), text,
        chat_id=chat_id, image_url=image_url,
    )
    return False


def drain_organize_notifications(*, limit: int = 20) -> bool:
    """投递到期通知；返回本轮是否全部成功。"""
    claimed = claim_due_organize_notifications(limit=limit)
    if not claimed:
        return True
    delivered = True
    for item in claimed:
        try:
            outcome = _send(
                item["body"], item["chat_id"], image_url=item.get("image_url", ""),
            )
        except Exception as exc:
            outcome = TelegramSendResult(
                ok=False, error=f"{type(exc).__name__}: Telegram 投递异常",
            )
            logger.warning(
                "整理通知投递异常 key=%s type=%s",
                item["idempotency_key"], type(exc).__name__,
            )
        generation = int(item["lease_generation"])
        if outcome.ok:
            if not mark_organize_notification_sent(
                item["id"], expected_lease_generation=generation,
            ):
                delivered = False
                logger.warning(
                    "整理通知成功回写已过期 key=%s generation=%s",
                    item["idempotency_key"], generation,
                )
            continue
        delivered = False
        state = retry_organize_notification(
            item["id"],
            expected_lease_generation=generation,
            error=outcome.error or "Telegram 投递失败，已排队重试",
            retry_after_seconds=outcome.retry_after_seconds,
        )
        logger.warning(
            "整理通知投递失败 key=%s state=%s attempts=%s retry_after=%s",
            item["idempotency_key"], state, item["attempts"] + 1,
            outcome.retry_after_seconds or "-",
        )
    return delivered


def recover_organize_notifications() -> int:
    """启动恢复：把中断的投递放回队列并立即尝试一次。"""
    recovered = recover_stale_organize_notifications()
    if recovered:
        logger.warning("已恢复中断的整理通知投递 count=%s", recovered)
    if count_pending_organize_notifications():
        drain_organize_notifications()
    return recovered
