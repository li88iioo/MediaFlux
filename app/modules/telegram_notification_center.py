"""Telegram 主动通知统一入口、持久投递与可更新消息线程。"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass

from app.logger import get_logger
from app.modules.telegram_notification_policy import (
    NotificationImportance,
    NotificationTopic,
    allows_notification,
)
from app.notifier import (
    NotificationAction,
    NotificationEvent,
    TelegramSendResult,
    edit_event_result,
    notification_target_chat_id,
    send_event_result,
)
from app.repositories.telegram_notifications import (
    claim_due_notifications,
    complete_notification,
    get_notification,
    mark_outcome_unknown,
    purge_notifications,
    recover_notifications,
    retry_notification,
    suppress_notification,
    upsert_notification,
)

logger = get_logger(__name__)
_DISPATCH_WAIT_SECONDS = 2.0
_dispatch_lock = threading.Lock()
_dispatch_stop = threading.Event()
_dispatch_wakeup = threading.Event()
_dispatch_thread: threading.Thread | None = None
_dispatch_accepting = False


@dataclass(frozen=True)
class NotificationPublishResult:
    accepted: bool
    delivered: bool = False
    queued: bool = False
    status: str = ""
    event_key: str = ""

    def __bool__(self) -> bool:
        return self.accepted


def serialize_notification_event(event: NotificationEvent) -> str:
    return json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":"))


def deserialize_notification_event(payload: str) -> NotificationEvent:
    try:
        data = json.loads(str(payload or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Telegram 通知数据损坏") from exc
    if not isinstance(data, dict):
        raise ValueError("Telegram 通知数据损坏")
    fields = tuple(
        (str(item[0] or ""), str(item[1] or ""))
        for item in (data.get("fields") or [])
        if isinstance(item, (list, tuple)) and len(item) == 2
    )
    actions = tuple(
        NotificationAction(
            label=str(item.get("label") or ""),
            callback_data=str(item.get("callback_data") or ""),
        )
        for item in (data.get("actions") or [])
        if isinstance(item, dict)
    )
    return NotificationEvent(
        title=str(data.get("title") or "Telegram 通知"),
        fields=fields,
        lines=tuple(str(item or "") for item in (data.get("lines") or [])),
        image_url=str(data.get("image_url") or ""),
        footer=str(data.get("footer") or ""),
        actions=actions,
        layout=str(data.get("layout") or "default"),
        field_emojis=bool(data.get("field_emojis", True)),
    )


def _topic(value: NotificationTopic | str) -> str:
    try:
        return NotificationTopic(str(value)).value
    except ValueError:
        return NotificationTopic.SYSTEM.value


def _importance(value: NotificationImportance | str) -> str:
    try:
        return NotificationImportance(str(value)).value
    except ValueError:
        return NotificationImportance.RESULT.value


def _scope_digest(chat_id: str) -> str:
    return hashlib.sha256(str(chat_id or "default").encode("utf-8")).hexdigest()[:16]


def _event_key(kind: str, topic: str, logical_key: str, chat_id: str) -> str:
    normalized = str(logical_key or "").strip()
    if not normalized:
        raise ValueError("Telegram 通知幂等键不能为空")
    return f"tg:{kind}:{topic}:{normalized}:{_scope_digest(chat_id)}"


def notification_thread_event_key(
    thread_key: str,
    *,
    topic: NotificationTopic | str,
    chat_id: str = "",
) -> str:
    target = notification_target_chat_id(chat_id)
    return _event_key("thread", _topic(topic), thread_key, target)


def get_notification_thread_event(
    thread_key: str,
    *,
    topic: NotificationTopic | str,
    chat_id: str = "",
) -> NotificationEvent | None:
    key = notification_thread_event_key(thread_key, topic=topic, chat_id=chat_id)
    row = get_notification(key)
    if row is None:
        return None
    try:
        return deserialize_notification_event(str(row.get("event_json") or ""))
    except ValueError:
        return None


def _edit_can_fallback_to_new_message(result: TelegramSendResult) -> bool:
    if int(result.status_code or 0) != 400 or result.outcome_unknown:
        return False
    description = str(result.error or "").casefold()
    return any(marker in description for marker in (
        "message to edit not found",
        "message can't be edited",
        "message can not be edited",
        "message identifier is not specified",
        "messagetoolongforedit",
    ))


def _dispatch_item(item: dict) -> bool:
    notification_id = int(item["id"])
    generation = int(item["lease_generation"])
    claimed_revision = int(item["revision"])
    if not allows_notification(str(item.get("importance") or "result")):
        suppress_notification(
            notification_id, lease_generation=generation,
            reason="NotificationPolicyDisabled",
        )
        return True
    try:
        event = deserialize_notification_event(str(item.get("event_json") or ""))
    except ValueError as exc:
        retry_notification(
            notification_id,
            lease_generation=generation,
            error=type(exc).__name__,
        )
        return False

    chat_id = str(item.get("chat_id") or "")
    message_id = int(item.get("message_id") or 0)
    outcome: TelegramSendResult
    try:
        if message_id > 0:
            outcome = edit_event_result(
                event, chat_id=chat_id, message_id=message_id,
            )
            if not outcome.ok and _edit_can_fallback_to_new_message(outcome):
                outcome = send_event_result(event, chat_id=chat_id or None)
        else:
            outcome = send_event_result(event, chat_id=chat_id or None)
    except Exception as exc:
        logger.warning(
            "Telegram 通知投递异常 topic=%s type=%s",
            item.get("topic") or "system",
            type(exc).__name__,
        )
        outcome = TelegramSendResult(ok=False, error=type(exc).__name__)

    if outcome.ok:
        return complete_notification(
            notification_id,
            lease_generation=generation,
            claimed_revision=claimed_revision,
            message_id=int(outcome.message_id or message_id or 0),
        )
    if outcome.outcome_unknown:
        mark_outcome_unknown(
            notification_id,
            lease_generation=generation,
            error=outcome.error or "OutcomeUnknown",
            message_id=int(outcome.message_id or message_id or 0),
        )
        return False
    retry_notification(
        notification_id,
        lease_generation=generation,
        error=outcome.error or "TelegramUnavailable",
        retry_after_seconds=outcome.retry_after_seconds,
    )
    return False


def drain_telegram_notifications(
    *, limit: int = 20, event_key: str = "",
) -> bool:
    claimed = claim_due_notifications(limit=limit, event_key=event_key)
    if not claimed:
        row = get_notification(event_key) if event_key else None
        return bool(row and str(row.get("status") or "") == "sent")
    delivered = True
    for item in claimed:
        delivered = _dispatch_item(item) and delivered
    return delivered


def _publish(
    logical_key: str,
    event: NotificationEvent,
    *,
    topic: NotificationTopic | str,
    importance: NotificationImportance | str,
    chat_id: str = "",
    thread: bool,
    topic_enabled: bool,
    preferred_message_id: int = 0,
    deliver_now: bool = True,
) -> NotificationPublishResult:
    normalized_importance = NotificationImportance(_importance(importance))
    if not allows_notification(
        normalized_importance, topic_enabled=topic_enabled,
    ):
        return NotificationPublishResult(False, status="disabled")
    target = notification_target_chat_id(chat_id)
    if not target:
        return NotificationPublishResult(False, status="unconfigured")
    normalized_topic = _topic(topic)
    key = _event_key(
        "thread" if thread else "event", normalized_topic, logical_key, target
    )
    row = upsert_notification(
        key,
        thread_key=str(logical_key or "") if thread else "",
        topic=normalized_topic,
        importance=normalized_importance.value,
        chat_id=target,
        event_json=serialize_notification_event(event),
        preferred_message_id=int(preferred_message_id or 0),
        replace=thread,
    )
    if row is None:
        return NotificationPublishResult(False, status="invalid", event_key=key)
    status = str(row.get("status") or "pending")
    if deliver_now and status in {"pending", "retry_wait"}:
        drain_telegram_notifications(limit=1, event_key=key)
        row = get_notification(key) or row
        status = str(row.get("status") or status)
    if status in {"pending", "retry_wait", "sending"}:
        wake_telegram_notification_dispatcher()
    return NotificationPublishResult(
        True,
        delivered=status == "sent",
        queued=status in {"pending", "retry_wait", "sending"},
        status=status,
        event_key=key,
    )


def publish_notification_event(
    event_key: str,
    event: NotificationEvent,
    *,
    topic: NotificationTopic | str,
    importance: NotificationImportance | str = NotificationImportance.RESULT,
    chat_id: str = "",
    topic_enabled: bool = True,
    deliver_now: bool = True,
) -> NotificationPublishResult:
    return _publish(
        event_key,
        event,
        topic=topic,
        importance=importance,
        chat_id=chat_id,
        thread=False,
        topic_enabled=topic_enabled,
        deliver_now=deliver_now,
    )


def publish_notification_thread(
    thread_key: str,
    event: NotificationEvent,
    *,
    topic: NotificationTopic | str,
    importance: NotificationImportance | str = NotificationImportance.RESULT,
    chat_id: str = "",
    topic_enabled: bool = True,
    preferred_message_id: int = 0,
    deliver_now: bool = True,
) -> NotificationPublishResult:
    return _publish(
        thread_key,
        event,
        topic=topic,
        importance=importance,
        chat_id=chat_id,
        thread=True,
        topic_enabled=topic_enabled,
        preferred_message_id=preferred_message_id,
        deliver_now=deliver_now,
    )


def _dispatch_loop() -> None:
    while not _dispatch_stop.is_set():
        try:
            if drain_telegram_notifications(limit=20):
                continue
        except Exception as exc:
            logger.error(
                "Telegram 通知队列调度异常 type=%s",
                type(exc).__name__,
                exc_info=True,
            )
        _dispatch_wakeup.wait(_DISPATCH_WAIT_SECONDS)
        _dispatch_wakeup.clear()


def start_telegram_notification_dispatcher() -> None:
    global _dispatch_thread, _dispatch_accepting
    with _dispatch_lock:
        _dispatch_accepting = True
        _dispatch_stop.clear()
        recover_notifications()
        purge_notifications()
        if _dispatch_thread is None or not _dispatch_thread.is_alive():
            _dispatch_thread = threading.Thread(
                target=_dispatch_loop,
                name="telegram-notification-outbox",
                daemon=True,
            )
            _dispatch_thread.start()
    _dispatch_wakeup.set()


def wake_telegram_notification_dispatcher() -> bool:
    with _dispatch_lock:
        if not _dispatch_accepting or _dispatch_stop.is_set():
            return False
        if _dispatch_thread is None or not _dispatch_thread.is_alive():
            return False
        _dispatch_wakeup.set()
        return True


def stop_telegram_notification_dispatcher(timeout: float = 3.0) -> bool:
    global _dispatch_thread, _dispatch_accepting
    with _dispatch_lock:
        _dispatch_accepting = False
        _dispatch_stop.set()
        _dispatch_wakeup.set()
        thread = _dispatch_thread
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(max(0.0, float(timeout)))
    stopped = thread is None or not thread.is_alive()
    with _dispatch_lock:
        if _dispatch_thread is thread and stopped:
            _dispatch_thread = None
    return stopped
