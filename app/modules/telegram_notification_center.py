"""Telegram 主动通知统一入口、持久投递与可更新消息线程。"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import Any, TypedDict

from cryptography.fernet import InvalidToken

from app import config
from app.agent.feature_gate import is_agent_enabled
from app.agent.owner_routes import normalize_delivery_route, telegram_owner_route_access
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
    AGENT_CONTINUATION_EVENT_PREFIX,
    bind_agent_continuation_group,
    bind_agent_effect_delivery,
    claim_due_notifications,
    complete_notification,
    fail_notification,
    get_notification,
    get_agent_continuation_group,
    purge_notifications,
    recover_notifications,
    retry_notification,
    suppress_notification,
    suppress_agent_continuation_group,
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
_AGENT_INTERACTION_LOGICAL_KEY_PREFIX = "agent-effect:"
_AGENT_INTERACTION_EVENT_KEY_PREFIX = "tg:event:agent:agent-effect:"
_AGENT_INTERACTION_STATE = "agent_effect_reply"
_AGENT_CONTINUATION_KEY_PREFIX = "agent-continuation:"
_AGENT_CONTINUATION_STATE = "agent_continuation_reply"
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


@dataclass(frozen=True)
class NotificationThreadSnapshot:
    """通知线程的期望内容与真实投递进度快照。"""

    event: NotificationEvent
    status: str
    revision: int
    delivered_revision: int
    message_id: int
    last_error: str = ""

    @property
    def current_revision_delivered(self) -> bool:
        """最新 revision 已原位送达，而不是仅写入 outbox。"""
        return (
            self.status == "sent"
            and self.revision > 0
            and self.delivered_revision >= self.revision
            and self.message_id > 0
        )


def serialize_notification_event(event: NotificationEvent) -> str:
    """序列化固定安全投影；动态展示值统一收敛为字符串。"""
    payload: dict[str, object] = {
        "title": str(event.title or ""),
        "fields": [
            [str(label or ""), str(value or "")]
            for label, value in tuple(event.fields or ())
        ],
        "lines": [str(item or "") for item in tuple(event.lines or ())],
        "image_url": (event.image_url or ""),
        "footer": str(event.footer or ""),
        "actions": [
            {
                "label": str(action.label or ""),
                "callback_data": (action.callback_data or ""),
            }
            for action in tuple(event.actions or ())
        ],
        "layout": (event.layout or "default"),
        "field_emojis": event.field_emojis,
        "state": (event.state or ""),
    }
    if event.lifecycle:
        payload["lifecycle"] = event.lifecycle
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def deserialize_notification_event(payload: str) -> NotificationEvent:
    try:
        data = json.loads(payload or "{}")
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
        state=str(data.get("state") or ""),
        lifecycle=dict(data["lifecycle"]) if isinstance(data.get("lifecycle"), dict) else {},
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
    return hashlib.sha256((chat_id or "default").encode("utf-8")).hexdigest()[:16]


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
    snapshot = get_notification_thread_snapshot(
        thread_key, topic=topic, chat_id=chat_id,
    )
    return snapshot.event if snapshot is not None else None


def get_notification_thread_snapshot(
    thread_key: str,
    *,
    topic: NotificationTopic | str,
    chat_id: str = "",
) -> NotificationThreadSnapshot | None:
    """读取线程最新内容及其是否已经真实送达 Telegram。"""
    key = notification_thread_event_key(thread_key, topic=topic, chat_id=chat_id)
    row = get_notification(key)
    if row is None:
        return None
    try:
        event = deserialize_notification_event(str(row.get("event_json") or ""))
    except ValueError:
        return None
    return NotificationThreadSnapshot(
        event=event,
        status=str(row.get("status") or ""),
        revision=max(0, int(row.get("revision") or 0)),
        delivered_revision=max(0, int(row.get("delivered_revision") or 0)),
        message_id=max(0, int(row.get("message_id") or 0)),
        last_error=str(row.get("last_error") or ""),
    )


def _edit_can_fallback_to_new_message(result: TelegramSendResult) -> bool:
    if (result.status_code or 0) != 400 or result.outcome_unknown:
        return False
    description = (result.error or "").casefold()
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
    callback_data = (action.callback_data or "").strip()
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


def _agent_interaction_enabled() -> bool:
    """交互回执受 Agent/TG Agent 开关控制，不受主动通知策略控制。"""
    return is_agent_enabled() and config.get_bool("TG_AGENT_ENABLED", False)


def _is_agent_interaction_item(item: dict, event: NotificationEvent) -> bool:
    return (
        str(item.get("topic") or "") == NotificationTopic.AGENT.value
        and str(item.get("event_key") or "").startswith(
            _AGENT_INTERACTION_EVENT_KEY_PREFIX
        )
        and (event.state or "") == _AGENT_INTERACTION_STATE
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


def _continuation_digest(owner: str, session_id: str, delivery_id: str) -> str:
    return hashlib.sha256(json.dumps([owner, session_id, delivery_id], ensure_ascii=False).encode()).hexdigest()


def _agent_delivery_identity(delivery: Mapping[str, Any]) -> dict[str, Any]:
    if (not isinstance(delivery, Mapping) or delivery.get("version") != 1 or any(
        not isinstance(delivery.get(key), str) or not delivery[key].strip()
        for key in ("owner", "session_id", "delivery_id", "lifecycle_id")
    )):
        raise ValueError("invalid Agent delivery identity")
    route = normalize_delivery_route(delivery["delivery_route"], owner=delivery["owner"])
    if not route:
        raise ValueError("missing Agent delivery route")
    return {**delivery, "delivery_route": route}


def _effect_delivery_key(delivery: Mapping[str, Any]) -> str:
    from app.agent.kernel.persistence import SQLiteKernelStore

    owner, session = SQLiteKernelStore()._scope(delivery["owner"], delivery["session_id"])
    plan = hashlib.sha256(delivery["delivery_id"].encode()).hexdigest()
    return f"agent-effect:{owner}:{session}:{plan}"


def _legacy_effect_delivery(item, state=None):
    """旧回执只能从原签名状态的加密受理事实恢复身份，不能猜测 owner。"""
    from app import database as db
    from app.agent.kernel.completion_receipts import _completion_cipher
    from app.agent.kernel.persistence import SQLiteKernelStore

    reason = "AgentInteractionLegacyIdentityUnavailable"
    match = re.fullmatch(
        r"tg:event:agent:agent-effect:([0-9a-f]{64}):([0-9a-f]{64}):([0-9a-f]{64}):[0-9a-f]{16}",
        item["event_key"],
    )
    if match is None:
        raise ValueError(reason)
    owner_digest, session_digest, plan_digest = match.groups()
    store = SQLiteKernelStore()
    source = None
    if state is None:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT generation,state_json,state_hmac FROM agent_kernel_sessions "
                "WHERE owner_digest=? AND session_digest=?", (owner_digest, session_digest),
            ).fetchone()
        if row is None:
            raise ValueError(reason)
        payload = store._decode(row["state_json"], row["state_hmac"],
            domain=f"state:v1:{owner_digest}:{session_digest}:{row['generation']}".encode(), expected_type=dict)
        source = row["generation"], payload
        metadata = payload.get("metadata") or {}
    else:
        metadata = state.metadata
    matches = []
    for plan_id, record in (metadata.get("effect_waits") or {}).items():
        if (hashlib.sha256(plan_id.encode()).hexdigest() != plan_digest or record.get("state") != "terminal"
                or (record.get("receipt_message") or {}).get("completion_receipt_id") != plan_id):
            continue
        try:
            seed = json.loads(_completion_cipher().decrypt(record["sealed"].encode()))
            if (seed["plan_id"] != plan_id or seed["channel"] != "telegram"
                    or store._scope(seed["owner"], seed["session_id"]) != (owner_digest, session_digest)):
                continue
            if source is not None:
                state = store._state_from_payload(owner=seed["owner"], session_id=seed["session_id"],
                    generation=source[0], payload=source[1])
            if state is None:
                raise ValueError(reason)
            if (state.owner, state.session_id) != (seed["owner"], seed["session_id"]):
                continue
            route = (seed.get("origin") or {}).get("delivery_route") or {"chat_id": item["chat_id"]}
            delivery = _agent_delivery_identity({
                "version": 1, "owner": state.owner, "session_id": state.session_id,
                "delivery_id": plan_id, "lifecycle_id": state.lifecycle_id, "delivery_route": route,
            })
            stored = json.loads(item["event_json"])
            if (delivery["delivery_route"] != normalize_delivery_route(
                    stored.get("_delivery_route") or {"chat_id": item["chat_id"]}, owner=state.owner)
                    or item["event_key"] != _event_key("event", "agent", _effect_delivery_key(delivery), item["chat_id"])):
                continue
        except (InvalidToken, KeyError, TypeError, ValueError):
            continue
        matches.append(delivery)
    if len(matches) != 1:
        raise ValueError(reason)
    return matches[0]


def _normalize_legacy_effect_delivery(item, delivery, state):
    if _legacy_effect_delivery(item, state) != delivery:
        raise ValueError("AgentInteractionLegacyIdentityUnavailable")
    stored = json.loads(item["event_json"])
    stored["_agent_effect_delivery"] = delivery
    normalized = bind_agent_effect_delivery(
        item["id"], lease_generation=item["lease_generation"], claimed_revision=item["revision"],
        expected_event_json=item["event_json"], event_json=json.dumps(stored, ensure_ascii=False),
    )
    return json.loads(normalized["event_json"])["_agent_effect_delivery"] if normalized is not None else None


def _normalize_legacy_continuation(item, delivery, state):
    """已领取旧组先一次性补全持久身份，之后只走完整身份的发送链。"""
    from app.agent.kernel.continuation import retained_delivery_routes

    if "delivery_id" in delivery or "lifecycle_id" in delivery:
        raise ValueError("InvalidAgentContinuationIdentity")
    owner, session_id = state.owner, state.session_id
    matches = {}
    for delivery_id, raw_route in retained_delivery_routes(state):
        digest = _continuation_digest(owner, session_id, delivery_id)
        if not item["event_key"].startswith(AGENT_CONTINUATION_EVENT_PREFIX + digest + ":"):
            continue
        if not isinstance(raw_route, Mapping):
            continue
        route = normalize_delivery_route(raw_route, owner=owner)
        if route:
            matches[delivery_id, json.dumps(route, sort_keys=True)] = route
    if len(matches) != 1:
        raise ValueError("AgentContinuationLegacyIdentityUnavailable")
    (delivery_id, _), route = next(iter(matches.items()))
    if route != normalize_delivery_route(delivery["delivery_route"], owner=owner):
        raise ValueError("AgentContinuationLegacyIdentityUnavailable")
    parts = {}
    for row in get_agent_continuation_group(item["event_key"]):
        stored = json.loads(row["event_json"])
        part = stored["_agent_continuation_delivery"]
        if (not isinstance(part, dict) or (part.get("owner"), part.get("session_id")) != (owner, session_id)
                or normalize_delivery_route(part["delivery_route"], owner=owner) != route):
            raise ValueError("AgentContinuationLegacyIdentityUnavailable")
        if "version" in part:
            if (part["version"] != 1 or part.get("delivery_id") != delivery_id
                    or part.get("lifecycle_id") != state.lifecycle_id):
                raise ValueError("InvalidAgentContinuationIdentity")
        elif "delivery_id" in part or "lifecycle_id" in part:
            raise ValueError("InvalidAgentContinuationIdentity")
        else:
            part.update(version=1, delivery_id=delivery_id, lifecycle_id=state.lifecycle_id)
        parts[row["id"]] = (row["revision"], row["event_json"], json.dumps(stored, ensure_ascii=False))
    normalized = bind_agent_continuation_group(
        item["id"], lease_generation=item["lease_generation"], claimed_revision=item["revision"], parts=parts,
    )
    if normalized is None:
        return None
    logger.info("Telegram Agent 旧续行投递身份已归一 parts=%s", len(parts))
    return json.loads(normalized["event_json"])["_agent_continuation_delivery"]


def _send_agent_delivery_item(
    item, delivery, *, continuation: bool,
    send: Callable[[Mapping[str, Any]], TelegramSendResult],
):
    from app.agent.kernel.persistence import SQLiteKernelStore
    from app.agent.kernel.session_guard import session_scope_guard

    label = "AgentContinuation" if continuation else "AgentInteraction"
    def suppress(reason):
        suppress_call = suppress_agent_continuation_group if continuation else suppress_notification
        suppress_call(
            item["id"], lease_generation=item["lease_generation"], claimed_revision=item["revision"], reason=reason,
        )

    legacy_effect = delivery is None and not continuation
    if legacy_effect:
        try:
            delivery = _legacy_effect_delivery(item)
        except (KeyError, TypeError, ValueError):
            suppress("AgentInteractionLegacyIdentityUnavailable")
            return None
        except Exception as exc:
            return TelegramSendResult(False, error=type(exc).__name__, status_code=503)
    owner, session_id = delivery.get("owner"), delivery.get("session_id")
    if not isinstance(owner, str) or not owner or not isinstance(session_id, str) or not session_id:
        suppress(f"Invalid{label}Identity")
        return None
    with ExitStack() as scope:
        try:
            scope.enter_context(session_scope_guard(owner, session_id, kind="delivery"))
            try:
                state = SQLiteKernelStore().load_existing_session(owner=owner, session_id=session_id)
            except (TypeError, ValueError):
                suppress(f"{label}SessionInvalid")
                return None
            if state is None:
                suppress(f"{label}SessionMissing")
                return None
            try:
                if legacy_effect:
                    delivery = _normalize_legacy_effect_delivery(item, delivery, state)
                elif continuation and "version" not in delivery:
                    delivery = _normalize_legacy_continuation(item, delivery, state)
                if delivery is None:
                    return TelegramSendResult(False, error=f"{label}IdentityChanged", status_code=503)
                delivery = _agent_delivery_identity(delivery)
                key_matches = (item["event_key"].startswith(AGENT_CONTINUATION_EVENT_PREFIX + _continuation_digest(
                    owner, session_id, delivery["delivery_id"],
                ) + ":") if continuation else item["event_key"] == _event_key(
                    "event", "agent", _effect_delivery_key(delivery), item["chat_id"],
                ))
                stored_route = json.loads(item["event_json"]).get("_delivery_route") or {"chat_id": item["chat_id"]}
                if (not key_matches or delivery["delivery_route"] != normalize_delivery_route(stored_route, owner=owner)):
                    raise ValueError(f"Invalid{label}Identity")
            except (KeyError, TypeError, ValueError) as exc:
                suppress(str(exc) if str(exc).startswith(label) else f"Invalid{label}Identity")
                return None
            if delivery["lifecycle_id"] != state.lifecycle_id:
                suppress(f"{label}LifecycleChanged")
                return None
            access = telegram_owner_route_access(owner, chat_id=delivery["delivery_route"]["chat_id"])
            if access != "allowed":
                return TelegramSendResult(
                    False, error="AgentDeliveryUnauthorized" if access == "unauthorized" else "AgentDeliveryDisabled",
                    status_code=403 if access == "unauthorized" else 503,
                )
        except Exception as exc:
            # 尚未触达 Bot；锁忙或临时读取/补身份失败不属于发送结果未知。
            return TelegramSendResult(False, error=type(exc).__name__, status_code=503)
        return send(delivery)


def _dispatch_item(item: dict) -> bool:
    notification_id = int(item["id"])
    generation = int(item["lease_generation"])
    claimed_revision = int(item["revision"])
    event = None
    continuation_delivery = None
    interaction_delivery = None
    agent_interaction = False
    continuation_candidate = (
        str(item.get("topic") or "") == NotificationTopic.AGENT.value
        and str(item.get("event_key") or "").startswith(AGENT_CONTINUATION_EVENT_PREFIX)
    )
    if continuation_candidate:
        try:
            stored = json.loads(str(item.get("event_json") or "{}"))
            event = deserialize_notification_event(str(item.get("event_json") or ""))
            continuation_delivery = stored["_agent_continuation_delivery"]
            route = normalize_delivery_route(
                continuation_delivery["delivery_route"], owner=continuation_delivery["owner"],
            )
            if event.state != _AGENT_CONTINUATION_STATE or route.get("chat_id") != str(item.get("chat_id") or ""):
                raise ValueError("Agent continuation route mismatch")
        except (KeyError, TypeError, ValueError):
            suppress_agent_continuation_group(
                notification_id, lease_generation=generation, claimed_revision=claimed_revision,
                reason="InvalidAgentContinuationDelivery",
            )
            return False
    interaction_candidate = (
        str(item.get("topic") or "") == NotificationTopic.AGENT.value
        and str(item.get("event_key") or "").startswith(
            _AGENT_INTERACTION_EVENT_KEY_PREFIX
        )
    )
    if interaction_candidate:
        try:
            candidate = deserialize_notification_event(
                str(item.get("event_json") or "")
            )
        except ValueError:
            pass
        else:
            if _is_agent_interaction_item(item, candidate):
                event = candidate
                agent_interaction = True
                if not _agent_interaction_enabled():
                    suppress_notification(
                        notification_id,
                        lease_generation=generation,
                        claimed_revision=claimed_revision,
                        reason="AgentInteractionDisabled",
                    )
                    return True
                stored = json.loads(item["event_json"])
                interaction_delivery = stored.get("_agent_effect_delivery")
                if "_agent_effect_delivery" in stored and not isinstance(interaction_delivery, Mapping):
                    suppress_notification(
                        notification_id,
                        lease_generation=generation,
                        claimed_revision=claimed_revision,
                        reason="InvalidAgentInteractionIdentity",
                    )
                    return True

    if event is None and not _allows_dispatch(item):
        suppress_notification(
            notification_id,
            lease_generation=generation,
            claimed_revision=claimed_revision,
            reason="NotificationPolicyDisabled",
        )
        return True
    if event is None:
        try:
            event = deserialize_notification_event(str(item.get("event_json") or ""))
        except ValueError as exc:
            fail_notification(
                notification_id,
                lease_generation=generation,
                claimed_revision=claimed_revision,
                error=f"InvalidEventPayload:{type(exc).__name__}",
            )
            return False

    if str(item.get("topic") or "") == NotificationTopic.DOWNLOAD.value:
        try:
            from app.modules.telegram_download_lifecycle import (
                download_notification_obsolescence,
            )

            stale = download_notification_obsolescence(str(item.get("thread_key") or ""), event)
        except Exception as exc:
            retry_notification(
                notification_id, lease_generation=generation, claimed_revision=claimed_revision,
                error=f"DownloadStateUnavailable:{type(exc).__name__}",
            )
            return False
        if stale == "cancelled":
            suppress_notification(
                notification_id, lease_generation=generation, claimed_revision=claimed_revision,
                reason="DownloadRequestCancelled",
            )
            return True
        if stale == "stale":
            retry_notification(
                notification_id, lease_generation=generation, claimed_revision=claimed_revision,
                error="AwaitingDownloadStateRefresh", retry_after_seconds=1,
            )
            return False

    chat_id = str(item.get("chat_id") or "")
    message_id = int(item.get("message_id") or 0)
    message_thread_id = None
    try:
        stored = json.loads(str(item.get("event_json") or "{}"))
        if "_delivery_route" in stored:
            route = normalize_delivery_route(stored["_delivery_route"])
            if route.get("chat_id") != chat_id:
                raise ValueError("Telegram delivery route mismatch")
            message_thread_id = route.get("message_thread_id")
    except (TypeError, ValueError):
        fail_notification(
            notification_id, lease_generation=generation, claimed_revision=claimed_revision,
            error="InvalidTelegramDeliveryRoute",
        )
        return False
    if message_thread_id is None and str(item.get("topic") or "") == NotificationTopic.DOWNLOAD.value:
        thread_key = str(item.get("thread_key") or "")
        if thread_key.startswith("download:"):
            from app.modules.telegram_topic_routing import download_request_thread

            message_thread_id = download_request_thread(
                thread_key[len("download:"):]
            )
    class SendOptions(TypedDict, total=False):
        chat_id: str | None
        message_thread_id: int

    send_kwargs: SendOptions = {"chat_id": chat_id or None}
    if message_thread_id is not None:
        send_kwargs["message_thread_id"] = message_thread_id
    outcome: TelegramSendResult
    fallback_to_new_message = False

    def send_event(_delivery):
        nonlocal fallback_to_new_message
        if message_id > 0:
            result = edit_event_result(event, chat_id=chat_id, message_id=message_id)
            if not result.ok and _edit_can_fallback_to_new_message(result):
                # 旧消息身份已被 Telegram 明确拒绝；后续新发送若结果未知，
                # 绝不能继续沿用旧 message_id 或自动重放。
                fallback_to_new_message = True
                result = send_event_result(event, **send_kwargs)
            return result
        return send_event_result(event, **send_kwargs)

    try:
        if continuation_delivery is not None:
            from app.bot.agent_adapter import _send_agent_continuation_part

            continuation_outcome = _send_agent_delivery_item(
                item, continuation_delivery, continuation=True, send=_send_agent_continuation_part,
            )
            if continuation_outcome is None:
                return False
            outcome = continuation_outcome
            if not outcome.ok:
                logger.warning(
                    "Telegram Agent 续行投递未完成 key=%s status=%s error=%s",
                    item.get("event_key"), outcome.status_code, outcome.error,
                )
        elif agent_interaction:
            interaction_outcome = _send_agent_delivery_item(
                item, interaction_delivery, continuation=False, send=send_event,
            )
            if interaction_outcome is None:
                return True
            outcome = interaction_outcome
        else:
            outcome = send_event(None)
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
            message_id=(
                outcome.message_id
                or (0 if fallback_to_new_message else message_id)
                or 0
            ),
        )
    if (
        not outcome.outcome_unknown
        and 400 <= (outcome.status_code or 0) < 500
        and outcome.status_code not in {408, 429}
    ):
        fail_notification(
            notification_id,
            lease_generation=generation,
            claimed_revision=claimed_revision,
            error=outcome.error or "TelegramRequestRejected",
            clear_message_id=fallback_to_new_message,
        )
        return False
    retry_notification(
        notification_id,
        lease_generation=generation,
        claimed_revision=claimed_revision,
        error=outcome.error or "TelegramUnavailable",
        retry_after_seconds=outcome.retry_after_seconds,
        clear_message_id=fallback_to_new_message,
        outcome_unknown=outcome.outcome_unknown,
        message_id=(outcome.message_id or 0),
    )
    return False


def drain_telegram_notifications(
    *, limit: int = 20, event_key: str = "",
) -> bool:
    processed: int = 0
    delivered = True
    for _ in range(max(1, min((limit or 1), 100))):
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
    agent_interaction: bool = False,
    agent_interaction_attempt_id: str = "",
    delivery_route: Mapping[str, Any] | None = None,
    agent_continuation_delivery: Mapping[str, Any] | None = None,
    agent_effect_delivery: Mapping[str, Any] | None = None,
    expected_confirmation_delivery: tuple[int, int] | None = None,
    expected_revision: int | None = None,
    expected_download_state: tuple[dict[str, Any], dict[str, Any] | None] | None = None,
) -> NotificationPublishResult:
    normalized_importance = NotificationImportance(_importance(importance))
    normalized_topic = _topic(topic)
    agent_continuation = agent_continuation_delivery is not None
    if agent_continuation:
        if (
            normalized_topic != NotificationTopic.AGENT.value or thread
            or not logical_key.startswith(_AGENT_CONTINUATION_KEY_PREFIX)
            or event.state != _AGENT_CONTINUATION_STATE
        ):
            return NotificationPublishResult(False, status="invalid_continuation")
        if not _agent_interaction_enabled():
            return NotificationPublishResult(False, status="disabled")
    if agent_interaction:
        normalized_logical_key = _bounded_logical_key(logical_key)
        if (
            normalized_topic != NotificationTopic.AGENT.value
            or thread
            or not normalized_logical_key.startswith(
                _AGENT_INTERACTION_LOGICAL_KEY_PREFIX
            )
            or (event.state or "") != _AGENT_INTERACTION_STATE
        ):
            return NotificationPublishResult(False, status="invalid_interaction")
        if not _agent_interaction_enabled():
            return NotificationPublishResult(False, status="disabled")
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
    if not agent_interaction and not agent_continuation:
        allowed = allows_notification(
            normalized_importance, topic_enabled=topic_enabled,
        )
        if not allowed:
            from app.modules.telegram_notification_policy import notifications_enabled

            # 已经展示给用户的线程需要完成终态更新，避免 essential 等级下
            # 候选按钮、错误或“等待后处理”永久停留。
            allowed = (thread and continuation and notifications_enabled())
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
        if existing and delivery_route is None:
            # 生命周期更新只重建展示字段；物理话题地址沿用 outbox 中的原地址。
            delivery_route = json.loads(existing["event_json"]).get("_delivery_route")
        event_json = serialize_notification_event(event)
        if agent_interaction_attempt_id or delivery_route or agent_continuation or agent_effect_delivery:
            payload = json.loads(event_json)
            if agent_interaction_attempt_id:
                payload["_agent_interaction_attempt_id"] = agent_interaction_attempt_id
            if delivery_route:
                route = normalize_delivery_route(delivery_route)
                if route.get("chat_id") != target:
                    raise ValueError("Telegram delivery route mismatch")
                payload["_delivery_route"] = route
            if agent_continuation:
                assert agent_continuation_delivery is not None
                payload["_agent_continuation_delivery"] = dict(agent_continuation_delivery)
            if agent_effect_delivery is not None:
                payload["_agent_effect_delivery"] = dict(agent_effect_delivery)
            event_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
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
        preferred_message_id=(preferred_message_id or 0),
        replace=thread,
        expected_confirmation_delivery=expected_confirmation_delivery,
        expected_revision=expected_revision,
        expected_download_state=expected_download_state,
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
    delivery_route: Mapping[str, Any] | None = None,
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
        delivery_route=delivery_route,
    )


def publish_agent_interaction_reply(
    logical_key: str,
    event: NotificationEvent,
    *,
    delivery: Mapping[str, Any],
    deliver_now: bool = False,
) -> NotificationPublishResult:
    """持久化交互回执；身份来自保存终态的同一事务，发送时持锁复核。"""
    key = (logical_key or "").strip()
    try:
        encoded_key = key.encode("utf-8")
    except UnicodeEncodeError:
        return NotificationPublishResult(False, status="invalid_key")
    if (
        not key.startswith(_AGENT_INTERACTION_LOGICAL_KEY_PREFIX)
        or not key[len(_AGENT_INTERACTION_LOGICAL_KEY_PREFIX):].strip()
        or len(encoded_key) > _MAX_LOGICAL_KEY_BYTES
    ):
        return NotificationPublishResult(False, status="invalid_key")
    if not isinstance(event, NotificationEvent):
        return NotificationPublishResult(False, status="invalid_event")
    try:
        delivery = _agent_delivery_identity(delivery)
    except (KeyError, TypeError, ValueError):
        return NotificationPublishResult(False, status="invalid_delivery_route")
    if key != _effect_delivery_key(delivery):
        return NotificationPublishResult(False, status="invalid_key")
    route = delivery["delivery_route"]
    target = route["chat_id"]
    if not _agent_interaction_enabled():
        return NotificationPublishResult(False, status="disabled")

    event_key = _event_key(
        "event", NotificationTopic.AGENT.value, key, target,
    )
    existing = get_notification(event_key)
    if existing is not None:
        status = str(existing.get("status") or "")
        return NotificationPublishResult(
            True,
            delivered=False,
            queued=status in {"pending", "retry_wait", "sending"},
            status=status or "duplicate",
            event_key=event_key,
        )

    task_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    fixed_event = replace(
        event,
        lines=(*tuple(event.lines or ()), f"任务标识：agent-effect-{task_id}"),
        state=_AGENT_INTERACTION_STATE,
    )
    attempt_id = secrets.token_urlsafe(12)
    published = _publish(
        key,
        fixed_event,
        topic=NotificationTopic.AGENT,
        importance=NotificationImportance.RESULT,
        chat_id=target,
        thread=False,
        topic_enabled=True,
        deliver_now=deliver_now,
        agent_interaction=True,
        agent_interaction_attempt_id=attempt_id,
        delivery_route=route,
        agent_effect_delivery=delivery,
    )
    if not published.accepted:
        return published

    row = get_notification(event_key)
    try:
        stored = json.loads(str(row.get("event_json") or "{}")) if row else {}
    except (TypeError, ValueError):
        stored = {}
    if stored.get("_agent_interaction_attempt_id") != attempt_id:
        status = str(row.get("status") or "") if row else ""
        return NotificationPublishResult(
            True,
            delivered=False,
            queued=status in {"pending", "retry_wait", "sending"},
            status=status or "duplicate",
            event_key=event_key,
        )
    return published


def publish_agent_continuation_parts(payload: Mapping[str, Any]) -> NotificationPublishResult:
    """续行结果只复用现有 outbox；分段分别幂等，部分入队失败可用原 payload 重试。"""
    try:
        if payload["version"] != 1:
            raise ValueError("unsupported delivery version")
        owner, session_id = payload["owner"], payload["session_id"]
        identity, lifecycle_id = payload["delivery_id"], payload["lifecycle_id"]
        if any(not isinstance(value, str) or not value.strip() for value in (owner, session_id, identity, lifecycle_id)):
            raise ValueError("invalid delivery identity")
        route = normalize_delivery_route(payload["delivery_route"], owner=owner)
        parts = payload["parts"]
        if not route or not session_id or not identity or not isinstance(parts, list) or not parts:
            raise ValueError("invalid delivery payload")
        if any(not isinstance(part, dict) or not isinstance(part.get("html"), str) or not part["html"] for part in parts):
            raise ValueError("invalid delivery parts")
        digest = _continuation_digest(owner, session_id, identity)
    except (KeyError, TypeError, ValueError):
        return NotificationPublishResult(False, status="invalid_delivery")
    results = []
    for index, part in enumerate(parts):
        delivery = {**part, "version": 1, "owner": owner, "session_id": session_id,
                    "delivery_id": identity, "lifecycle_id": lifecycle_id, "delivery_route": route}
        result = _publish(
            f"{_AGENT_CONTINUATION_KEY_PREFIX}{digest}:{index}",
            NotificationEvent("Media Agent", state=_AGENT_CONTINUATION_STATE),
            topic=NotificationTopic.AGENT, importance=NotificationImportance.RESULT,
            chat_id=route["chat_id"], thread=False, topic_enabled=True, deliver_now=False,
            delivery_route=route, agent_continuation_delivery=delivery,
        )
        if not result.accepted:
            return result
        results.append(result)
    status = next((r.status for r in results if r.status in {"failed", "outcome_unknown", "suppressed"}), results[-1].status)
    return NotificationPublishResult(
        True, delivered=all(r.delivered for r in results), queued=any(r.queued for r in results),
        status=status, event_key=results[-1].event_key,
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
    delivery_route: Mapping[str, Any] | None = None,
    expected_confirmation_delivery: tuple[int, int] | None = None,
    expected_revision: int | None = None,
    expected_download_state: tuple[dict[str, Any], dict[str, Any] | None] | None = None,
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
        delivery_route=delivery_route,
        expected_confirmation_delivery=expected_confirmation_delivery,
        expected_revision=expected_revision,
        expected_download_state=expected_download_state,
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
