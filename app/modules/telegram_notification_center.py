"""Telegram 主动通知统一入口、持久投递与可更新消息线程。"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace

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
    render_event,
    send_event_result,
    telegram_text_length,
    truncate_telegram_text,
)
from app.repositories.telegram_notifications import (
    claim_due_notifications,
    complete_notification,
    fail_notification,
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
_delivery_lock = threading.Lock()
_THREAD_MESSAGE_LIMIT = 3800
_MAX_LOGICAL_KEY_BYTES = 240
_PURGE_INTERVAL_SECONDS = 6 * 60 * 60
_PURGE_RETRY_SECONDS = 60
_maintenance_lock = threading.Lock()
_next_purge_at = 0.0


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
    """序列化固定安全投影；动态展示值统一收敛为字符串。"""
    payload = {
        "title": str(event.title or ""),
        "fields": [
            [str(label or ""), str(value or "")]
            for label, value in tuple(event.fields or ())
        ],
        "lines": [str(item or "") for item in tuple(event.lines or ())],
        "image_url": str(event.image_url or ""),
        "footer": str(event.footer or ""),
        "actions": [
            {
                "label": str(action.label or ""),
                "callback_data": str(action.callback_data or ""),
            }
            for action in tuple(event.actions or ())
        ],
        "layout": str(event.layout or "default"),
        "field_emojis": bool(event.field_emojis),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def deserialize_notification_event(payload: str) -> NotificationEvent:
    try:
        data = json.loads(str(payload or "{}"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Telegram 通知数据损坏") from exc
    if not isinstance(data, dict):
        raise ValueError("Telegram 通知数据损坏")

    raw_fields = data.get("fields", [])
    raw_lines = data.get("lines", [])
    raw_actions = data.get("actions", [])
    raw_fields = [] if raw_fields is None else raw_fields
    raw_lines = [] if raw_lines is None else raw_lines
    raw_actions = [] if raw_actions is None else raw_actions
    if (
        not isinstance(raw_fields, list)
        or not isinstance(raw_lines, list)
        or not isinstance(raw_actions, list)
        or any(not isinstance(item, list) or len(item) != 2 for item in raw_fields)
        or any(not isinstance(item, dict) for item in raw_actions)
    ):
        raise ValueError("Telegram 通知数据损坏")

    fields = tuple(
        (str(item[0] or ""), str(item[1] or ""))
        for item in raw_fields
    )
    actions = tuple(
        NotificationAction(
            label=str(item.get("label") or ""),
            callback_data=str(item.get("callback_data") or ""),
        )
        for item in raw_actions
    )
    return NotificationEvent(
        title=str(data.get("title") or "Telegram 通知"),
        fields=fields,
        lines=tuple(str(item or "") for item in raw_lines),
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


def _bounded_logical_key(logical_key: object) -> str:
    normalized = str(logical_key or "").strip()
    if not normalized:
        raise ValueError("Telegram 通知幂等键不能为空")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError:
        encoded = normalized.encode("utf-8", errors="surrogatepass")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    if len(encoded) > _MAX_LOGICAL_KEY_BYTES:
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    return normalized


def _event_key(kind: str, topic: str, logical_key: str, chat_id: str) -> str:
    normalized = _bounded_logical_key(logical_key)
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
        "there is no text in the message to edit",
        "message has no text",
        "message is not a text message",
        "messagetoolongforedit",
    ))


def _normalize_action(action: NotificationAction) -> NotificationAction | None:
    label = truncate_telegram_text(str(action.label or "").strip(), 64)
    callback_data = str(action.callback_data or "").strip()
    if not label or not callback_data or len(callback_data.encode("utf-8")) > 64:
        return None
    return NotificationAction(label=label, callback_data=callback_data)


def _normalized_actions(actions) -> tuple[NotificationAction, ...]:
    normalized = tuple(
        candidate
        for action in tuple(actions or ())
        if (candidate := _normalize_action(action)) is not None
    )
    return normalized


def _bounded_thread_event(event: NotificationEvent) -> NotificationEvent:
    """保证生命周期线程始终可由单条纯文本消息原位更新。"""
    valid_actions = _normalized_actions(event.actions)
    bounded = replace(event, image_url="", actions=valid_actions)
    if telegram_text_length(render_event(bounded)) <= _THREAD_MESSAGE_LIMIT:
        return bounded

    clipped = replace(
        bounded,
        title=truncate_telegram_text(bounded.title or "Telegram 通知", 180),
        fields=tuple(
            (
                truncate_telegram_text(label, 64),
                truncate_telegram_text(value, 520),
            )
            for label, value in tuple(bounded.fields or ())[:16]
        ),
        lines=tuple(
            truncate_telegram_text(line, 700)
            for line in tuple(bounded.lines or ())[:12]
        ),
        footer=truncate_telegram_text(bounded.footer, 360),
    )
    truncation_note = "内容过长已截断，完整详情请在 Web 运行记录中查看。"
    lines = list(clipped.lines)
    fields = list(clipped.fields)
    while lines and telegram_text_length(render_event(replace(
        clipped, lines=tuple(lines), footer=truncation_note,
    ))) > _THREAD_MESSAGE_LIMIT:
        lines.pop()
    while fields and telegram_text_length(render_event(replace(
        clipped, fields=tuple(fields), lines=tuple(lines), footer=truncation_note,
    ))) > _THREAD_MESSAGE_LIMIT:
        fields.pop()
    return replace(
        clipped,
        fields=tuple(fields),
        lines=tuple(lines),
        footer=truncation_note,
    )


def _allows_dispatch(item: dict) -> bool:
    importance = str(item.get("importance") or "result")
    if allows_notification(importance):
        return True
    # 通知等级只决定是否创建新消息。已经投递过的生命周期线程必须允许
    # 收敛终态并清除旧按钮；全局通知总开关关闭时仍不发送。
    from app.modules.telegram_notification_policy import notifications_enabled

    return bool(
        notifications_enabled()
        and str(item.get("thread_key") or "").strip()
        and int(item.get("message_id") or 0) > 0
    )


def _dispatch_item(item: dict) -> bool:
    notification_id = int(item["id"])
    generation = int(item["lease_generation"])
    claimed_revision = int(item["revision"])
    if not _allows_dispatch(item):
        suppress_notification(
            notification_id, lease_generation=generation,
            reason="NotificationPolicyDisabled",
        )
        return True
    try:
        event = deserialize_notification_event(str(item.get("event_json") or ""))
    except ValueError as exc:
        fail_notification(
            notification_id,
            lease_generation=generation,
            error=f"InvalidEventPayload:{type(exc).__name__}",
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
            claimed_revision=claimed_revision,
            error=outcome.error or "OutcomeUnknown",
            message_id=int(outcome.message_id or message_id or 0),
        )
        return False
    if 400 <= int(outcome.status_code or 0) < 500 and int(outcome.status_code) not in {408, 429}:
        fail_notification(
            notification_id,
            lease_generation=generation,
            error=outcome.error or "TelegramRequestRejected",
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
    processed = 0
    delivered = True
    for _ in range(max(1, min(int(limit or 1), 100))):
        # 只在真正发送前领取一条，并串行化进程内 transport；避免批量预领取
        # 的后排记录等待超过租约后被另一个 drain 重领。
        if not _delivery_lock.acquire(blocking=False):
            break
        try:
            claimed = claim_due_notifications(limit=1, event_key=event_key)
            if not claimed:
                break
            processed += 1
            delivered = _dispatch_item(claimed[0]) and delivered
        finally:
            _delivery_lock.release()
    if processed:
        return delivered
    row = get_notification(event_key) if event_key else None
    return bool(row and str(row.get("status") or "") == "sent")


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
    normalized_topic = _topic(topic)
    target = notification_target_chat_id(chat_id)
    if not target:
        return NotificationPublishResult(False, status="unconfigured")
    try:
        normalized_logical_key = _bounded_logical_key(logical_key)
        key = _event_key(
            "thread" if thread else "event",
            normalized_topic,
            normalized_logical_key,
            target,
        )
    except (TypeError, ValueError) as exc:
        logger.warning(
            "拒绝无效 Telegram 通知幂等键 topic=%s type=%s",
            normalized_topic,
            type(exc).__name__,
        )
        return NotificationPublishResult(False, status="invalid_key")
    existing = get_notification(key) if thread else None
    continuation = bool(
        existing
        and int(existing.get("message_id") or 0) > 0
        and int(existing.get("delivered_revision") or 0) > 0
    )
    allowed = allows_notification(
        normalized_importance, topic_enabled=topic_enabled,
    )
    if not allowed:
        from app.modules.telegram_notification_policy import notifications_enabled

        # 已经展示给用户的线程需要完成终态更新，避免 essential 等级下
        # 候选按钮、错误或“等待后处理”永久停留。
        allowed = bool(thread and continuation and notifications_enabled())
    if not allowed:
        return NotificationPublishResult(False, status="disabled")
    try:
        if event.actions:
            valid_actions = _normalized_actions(event.actions)
            if not valid_actions:
                logger.error(
                    "拒绝没有有效按钮的 Telegram 操作通知 topic=%s",
                    normalized_topic,
                )
                return NotificationPublishResult(
                    False, status="invalid_actions", event_key=key,
                )
            if valid_actions != tuple(event.actions):
                event = replace(event, actions=valid_actions)
        if thread:
            event = _bounded_thread_event(event)
        event_json = serialize_notification_event(event)
    except Exception as exc:
        logger.warning(
            "拒绝无法序列化的 Telegram 通知 topic=%s type=%s",
            normalized_topic,
            type(exc).__name__,
        )
        return NotificationPublishResult(
            False, status="invalid_event", event_key=key,
        )
    row = upsert_notification(
        key,
        thread_key=normalized_logical_key if thread else "",
        topic=normalized_topic,
        importance=normalized_importance.value,
        chat_id=target,
        event_json=event_json,
        preferred_message_id=int(preferred_message_id or 0),
        replace=thread,
    )
    if row is None:
        return NotificationPublishResult(False, status="invalid", event_key=key)
    status = str(row.get("status") or "pending")
    if deliver_now and not _dispatch_stop.is_set() and status in {"pending", "retry_wait"}:
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


def _maybe_purge_notifications(*, force: bool = False) -> int:
    """为长期运行实例节流清理终态记录，失败后短间隔重试。"""
    global _next_purge_at
    now = time.monotonic()
    with _maintenance_lock:
        if not force and now < _next_purge_at:
            return 0
        _next_purge_at = now + _PURGE_INTERVAL_SECONDS
    try:
        return purge_notifications()
    except Exception:
        with _maintenance_lock:
            _next_purge_at = min(
                _next_purge_at,
                time.monotonic() + _PURGE_RETRY_SECONDS,
            )
        raise


def _dispatch_loop() -> None:
    while not _dispatch_stop.is_set():
        try:
            _maybe_purge_notifications()
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
        if _dispatch_thread is not None and _dispatch_thread.is_alive():
            if not _dispatch_stop.is_set():
                _dispatch_accepting = True
                _dispatch_wakeup.set()
            else:
                logger.warning("Telegram 通知中心仍在停止中，暂缓重复启动")
            return
        _dispatch_accepting = True
        _dispatch_stop.clear()
        recover_notifications()
        _maybe_purge_notifications(force=True)
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
