"""Agent 下载后媒体库复核的结构化安全通知。"""
from __future__ import annotations

import hashlib
import json
import unicodedata

from app import config
from app.agent.owner_routes import (
    parse_telegram_owner_route,
    telegram_owner_route_is_currently_authorized,
)
from app.notifier import (
    NOTIFICATION_SECTION_BREAK,
    NotificationEvent,
    TelegramSendResult,
)

_RESULT_LABELS = {
    "visible": "目标剧集已在媒体库中可见",
    "missing": "目标剧集仍未在媒体库中出现",
    "inconclusive": "媒体库复核暂时无法得出可靠结论",
    "": "下载或后处理状态需要人工检查",
}

_SENSITIVE_TITLE_MARKERS = (
    "://", "magnet:", "token=", "password=", "passwd=", "cookie=",
    "authorization:", "/volume/", "\\volume\\",
)


def _safe_title(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text or any(unicodedata.category(char).startswith("C") for char in text):
        return "未命名剧集"
    lowered = text.casefold()
    if any(marker in lowered for marker in _SENSITIVE_TITLE_MARKERS):
        return "未命名剧集"
    return text[:120]



def dump_download_verification_payload(
    *,
    title: object,
    season: int,
    episode: int,
    status: str,
    result: str,
    attempts: int,
) -> str:
    """生成可持久化的安全通知投影，不保存下载链接或凭据。"""
    payload = {
        "title": _safe_title(title),
        "season": max(1, int(season)),
        "episode": max(1, int(episode)),
        "status": status if status in {"visible", "attention"} else "attention",
        "result": result if result in _RESULT_LABELS else "inconclusive",
        "attempts": max(0, min(int(attempts), 100)),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def load_download_verification_payload(value: object) -> dict[str, object]:
    """读取并再次收敛 outbox 载荷，拒绝畸形或扩展字段。"""
    try:
        payload = json.loads(str(value or ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("通知载荷不是有效 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "title", "season", "episode", "status", "result", "attempts"
    }:
        raise ValueError("通知载荷结构无效")
    return {
        "title": _safe_title(payload.get("title")),
        "season": max(1, int(payload.get("season") or 1)),
        "episode": max(1, int(payload.get("episode") or 1)),
        "status": (
            str(payload.get("status"))
            if str(payload.get("status")) in {"visible", "attention"}
            else "attention"
        ),
        "result": (
            str(payload.get("result"))
            if str(payload.get("result")) in _RESULT_LABELS
            else "inconclusive"
        ),
        "attempts": max(0, min(int(payload.get("attempts") or 0), 100)),
    }

def build_download_verification_event(
    *,
    title: object,
    season: int,
    episode: int,
    status: str,
    result: str,
    attempts: int,
) -> NotificationEvent:
    """将复核终态收敛为不含下载凭据和路径的固定通知。"""
    normalized_status = status if status in {"visible", "attention"} else "attention"
    normalized_result = result if result in _RESULT_LABELS else "inconclusive"
    target = f"S{max(1, int(season)):02d}E{max(1, int(episode)):02d}"
    fields = (
        ("目标媒体", _safe_title(title)),
        ("目标集", target),
        NOTIFICATION_SECTION_BREAK,
        ("复核结果", _RESULT_LABELS[normalized_result]),
        ("复核次数", str(max(0, int(attempts)))),
    )
    if normalized_status == "visible":
        return NotificationEvent(
            "Agent 媒体库复核完成", fields=fields, layout="relaxed",
        )
    return NotificationEvent(
        "Agent 媒体库复核需要处理",
        fields=fields,
        footer="可在 Agent 中查询最近下载状态获取安全诊断。",
        layout="relaxed",
    )


def notify_download_verification_terminal_result(
    *, owner: str, chat_id: str, request_id: int = 0, **payload
) -> TelegramSendResult:
    """向 owner 绑定 chat 投递终态通知，并保留结果未知语义。"""
    if not config.get_bool("AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED", True):
        return TelegramSendResult(False, error="NotificationsDisabled", status_code=503)
    route = parse_telegram_owner_route(owner)
    target = str(chat_id or "").strip()
    if route is None or not target or route.chat_id != target:
        return TelegramSendResult(False, error="InvalidRoute", status_code=403)
    if not telegram_owner_route_is_currently_authorized(route, chat_id=target):
        return TelegramSendResult(False, error="AuthorizationRevoked", status_code=403)
    if int(request_id or 0) > 0:
        from app.modules.telegram_download_lifecycle import publish_download_lifecycle

        normalized_status = str(payload.get("status") or "attention")
        normalized_result = str(payload.get("result") or "inconclusive")
        outcome = publish_download_lifecycle(
            int(request_id),
            verification_status=normalized_status,
            verification_result=_RESULT_LABELS.get(normalized_result, _RESULT_LABELS[""]),
        )
        return TelegramSendResult(
            ok=bool(outcome),
            error="" if outcome else str(outcome.status or "DeliveryFailed"),
            status_code=0 if outcome else 503,
        )

    from app.modules.telegram_notification_center import publish_notification_event
    from app.modules.telegram_notification_policy import (
        NotificationImportance, NotificationTopic,
    )
    safe_payload_json = dump_download_verification_payload(**payload)
    safe_payload = load_download_verification_payload(safe_payload_json)
    event = build_download_verification_event(**safe_payload)
    digest = hashlib.sha256(
        f"{owner}\x1f{safe_payload_json}".encode("utf-8")
    ).hexdigest()
    outcome = publish_notification_event(
        f"agent-download-verification:{digest}",
        event,
        topic=NotificationTopic.AGENT,
        importance=(
            NotificationImportance.RESULT
            if str(safe_payload.get("status") or "") == "visible"
            else NotificationImportance.ERROR
        ),
        chat_id=target,
        topic_enabled=True,
    )
    return TelegramSendResult(
        ok=bool(outcome),
        error="" if outcome else str(outcome.status or "DeliveryFailed"),
        status_code=0 if outcome else 503,
    )
