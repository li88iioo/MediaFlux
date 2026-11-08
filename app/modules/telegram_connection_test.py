"""Web 与 SDK 共用的固定 Telegram 连接测试；不接受业务消息。"""
from __future__ import annotations

import hashlib
import hmac
import re

import requests

from app import config
from app.logger import configure_telebot_logging, get_logger

logger = get_logger(__name__)
TEST_MESSAGE = "<b>MediaFlux 连接测试</b>\nTelegram Bot 通知通道工作正常。"


class NotificationConfigChanged(ValueError):
    """写入前配置已变化，未发送。"""


def send_test_message(token: str, chat_id: str, *, timeout: int | None = None):
    import telebot

    configure_telebot_logging()
    bot = telebot.TeleBot(token, parse_mode="HTML", threaded=False)
    return bot.send_message(chat_id, TEST_MESSAGE, timeout=timeout)


def _configured_test():
    values = config.get_many(("TG_BOT_TOKEN", "TG_CHAT_ID"))
    token, chat_id = ((values[key] or "").strip() for key in ("TG_BOT_TOKEN", "TG_CHAT_ID"))
    fingerprint = hashlib.sha256((token + "\0" + chat_id).encode()).hexdigest()
    if not token or not chat_id:
        preview = {"ok": False, "status": "not_configured", "summary": "Telegram 通知通道尚未完整配置",
                   "error": "请先在设置中配置 Bot Token 与 Chat ID。"}
    elif ":" not in token or len(token) > 256 or not re.fullmatch(r"-?\d{1,63}", chat_id):
        preview = {"ok": False, "status": "precondition_failed", "summary": "Telegram 通知配置格式无效",
                   "error": "请先在设置中修正 Bot Token 或 Chat ID。"}
    else:
        preview = {"ok": True, "status": "confirmation_required", "summary": "确认后将发送一条固定的 Telegram 连接测试消息",
                   "data": {"configured": True, "effects": ["仅向已配置的通知会话发送固定连接测试，不发送业务数据或修改配置。"]}}
    return preview, fingerprint, token, chat_id


def prepare_test() -> tuple[dict, str]:
    preview, fingerprint, _, _ = _configured_test()
    return preview, fingerprint


def send_configured_test(expected_fingerprint: str, *, check_cancelled=None) -> dict:
    preview, fingerprint, token, chat_id = _configured_test()
    if not hmac.compare_digest(fingerprint, expected_fingerprint):
        raise NotificationConfigChanged("Telegram 通知配置已变化，请重新预检")
    if not preview["ok"]:
        return preview
    if check_cancelled:
        check_cancelled()
    try:
        send_test_message(token, chat_id, timeout=8)
    except Exception as exc:
        logger.warning("Telegram 连接测试失败 type=%s", type(exc).__name__)
        code = getattr(exc, "error_code", None)
        errors = {401: "请检查 Bot Token 是否仍然有效。", 404: "请检查 Bot Token 是否仍然有效。",
                  403: "请先与 Bot 建立会话并检查发送权限。", 400: "请检查 Chat ID 与会话状态。"}
        if code in errors:
            return {"ok": False, "status": "unavailable", "summary": "Telegram 拒绝测试消息", "error": errors[code]}
        # 连接中断也可能发生在服务端已收件后，不能作为未发送的证据。
        return {"ok": False, "status": "outcome_unknown",
                "summary": "Telegram 未在限定时间内确认发送结果" if isinstance(exc, requests.Timeout) else "Telegram 发送结果待核验",
                "error": "消息可能已经送达，请先检查 Telegram；不要重复确认原计划。"}
    return {"ok": True, "status": "completed", "summary": "Telegram 测试消息已发送", "data": {"sent": True}}
