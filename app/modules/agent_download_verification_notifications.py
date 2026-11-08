"""Agent 下载后媒体库复核的结构化安全通知。"""
from __future__ import annotations

import json
import unicodedata
from typing import TypedDict

from app import config
from app.agent.owner_routes import (
    parse_telegram_owner_route,
    telegram_owner_route_is_currently_authorized,
)
from app.notifier import TelegramSendResult


class DownloadVerificationTarget(TypedDict):
    title: str
    season: int
    episode: int
    result: str


class DownloadVerificationPayload(TypedDict):
    targets: list[DownloadVerificationTarget]
    status: str
    result: str
    attempts: int


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
    *, targets: list[dict], status: str, result: str, attempts: int,
) -> str:
    """投影全部已核对目标；不把缺失或未核对的目标写成成功通知。"""
    projected = [{
        "title": _safe_title(target.get("title")),
        "season": max(1, int(target.get("season") or 1)),
        "episode": max(1, int(target.get("episode") or 1)),
        "result": target.get("result") if target.get("result") in _RESULT_LABELS else "inconclusive",
    } for target in targets]
    status = status if status in {"visible", "attention"} else "attention"
    result = result if result in _RESULT_LABELS else "inconclusive"
    if (status == "visible" or result == "visible") and (
        not projected or any(target["result"] != "visible" for target in projected)
    ):
        status, result = "attention", "inconclusive"
    return json.dumps({"targets": projected, "status": status, "result": result,
                       "attempts": max(0, min(attempts, 100))}, ensure_ascii=False, separators=(",", ":"))


def load_download_verification_payload(value: object) -> DownloadVerificationPayload:
    """只读取新格式 outbox；旧单目标载荷由 v38 一次迁完，不保留双读。"""
    try:
        payload = json.loads(str(value or ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("通知载荷不是有效 JSON") from exc
    if (not isinstance(payload, dict) or set(payload) != {"targets", "status", "result", "attempts"}
            or not isinstance(payload["targets"], list)
            or any(not isinstance(target, dict) or set(target) != {"title", "season", "episode", "result"}
                   for target in payload["targets"])):
        raise ValueError("通知载荷结构无效")
    return json.loads(dump_download_verification_payload(**payload))


def notify_download_verification_terminal_result(
    *, owner: str, chat_id: str | int, request_id: int,
) -> TelegramSendResult:
    """授权后把请求的当前复核事实交给唯一下载事务消息。"""
    if not config.get_bool("AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED", True):
        return TelegramSendResult(False, error="NotificationsDisabled", status_code=503)
    route = parse_telegram_owner_route(owner)
    target = str(chat_id or "").strip()
    if route is None or not target or route.chat_id != target:
        return TelegramSendResult(False, error="InvalidRoute", status_code=403)
    if not telegram_owner_route_is_currently_authorized(route, chat_id=target):
        return TelegramSendResult(False, error="AuthorizationRevoked", status_code=403)
    from app.modules.telegram_download_lifecycle import publish_download_lifecycle

    outcome = publish_download_lifecycle(request_id, expected_chat_id=target)
    return TelegramSendResult(
        ok=bool(outcome),
        error="" if outcome else (outcome.status or "DeliveryFailed"),
        status_code=0 if outcome else (403 if outcome.status == "InvalidRoute" else 503),
    )
