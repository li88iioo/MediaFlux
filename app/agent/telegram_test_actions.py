"""Telegram 通知通道的固定、受确认连接测试。

该工具只向服务端已经配置的 Telegram 会话发送一条固定测试消息。工具参数为空，
不会接受或返回 Bot Token、Chat ID、任意消息正文或 Telegram 响应对象。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime
from typing import Any

import requests

from app import config
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.logger import configure_telebot_logging, get_logger

logger = get_logger(__name__)
_FIXED_TEST_MESSAGE = "<b>MediaFlux 连接测试</b>\nTelegram Bot 通知通道工作正常。"
_CHAT_ID_PATTERN = re.compile(r"^-?\d{1,63}$")
_SEND_TIMEOUT_SECONDS = 8


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def telegram_test_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    if arguments:
        raise AgentToolError(
            f"Telegram 测试通知不接受参数：{', '.join(sorted(arguments))}"
        )
    return {}


def _private_state() -> dict[str, Any]:
    token = str(config.get("TG_BOT_TOKEN", "") or "").strip()
    chat_id = str(config.get("TG_CHAT_ID", "") or "").strip()
    token_valid = bool(token and ":" in token and len(token) <= 256)
    chat_id_valid = bool(_CHAT_ID_PATTERN.fullmatch(chat_id))
    # 指纹只保存不可逆摘要；预览、票据、审计与模型上下文均不会包含原值。
    return {
        "configured": bool(token and chat_id),
        "token_valid": token_valid,
        "chat_id_valid": chat_id_valid,
        "token_digest": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "chat_digest": hashlib.sha256(chat_id.encode("utf-8")).hexdigest(),
        "token": token,
        "chat_id": chat_id,
    }


def _fingerprint(state: dict[str, Any]) -> str:
    payload = "\0".join(
        (
            "telegram-test-v1",
            "1" if state["configured"] else "0",
            "1" if state["token_valid"] else "0",
            "1" if state["chat_id_valid"] else "0",
            str(state["token_digest"]),
            str(state["chat_digest"]),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _precondition_failure(state: dict[str, Any]) -> ToolResult | None:
    if not state["configured"]:
        return ToolResult(
            ok=False,
            status="not_configured",
            summary="Telegram 通知通道尚未完整配置",
            error="请先在设置中配置 Bot Token 与 Chat ID。",
        )
    if not state["token_valid"] or not state["chat_id_valid"]:
        return ToolResult(
            ok=False,
            status="precondition_failed",
            summary="Telegram 通知配置格式无效",
            error="请先在设置中修正 Bot Token 或 Chat ID。",
        )
    return None


def _preview_from_state(state: dict[str, Any]) -> ToolResult:
    failed = _precondition_failure(state)
    if failed is not None:
        return failed
    return ToolResult(
        ok=True,
        status="confirmation_required",
        summary="确认后将发送一条固定的 Telegram 连接测试消息",
        data={
            "configured": True,
            "effects": [
                "只会向当前已配置的通知会话发送一条固定测试消息。",
                "不会发送业务数据、工具结果、路径、链接、Token 或 Chat ID。",
                "不会修改 Telegram Agent、通知策略或其它项目配置。",
            ],
        },
        evidence=[
            Evidence(
                source="server_configuration",
                description="仅核对 Telegram 通知配置是否完整及其不可逆摘要；未返回凭据或会话标识。",
                collected_at=_now(),
            )
        ],
        suggestions=["发送后的消息无法由 MediaFlux 撤回，但可在 Telegram 中手动删除。"],
    )


def prepare_telegram_test_notification(
    _arguments: dict[str, Any],
) -> tuple[ToolResult, str]:
    state = _private_state()
    return _preview_from_state(state), _fingerprint(state)


def _failure_from_exception(exc: Exception) -> ToolResult:
    status_code = getattr(exc, "error_code", None)
    if status_code in {401, 404}:
        summary = "Telegram Bot 身份验证失败"
        error = "请检查 Bot Token 是否仍然有效。"
    elif status_code == 403:
        summary = "Telegram Bot 无权向目标会话发送消息"
        error = "请先与 Bot 建立会话并检查发送权限。"
    elif status_code == 400:
        summary = "Telegram 目标会话无法接收测试消息"
        error = "请检查 Chat ID 与会话状态。"
    elif isinstance(exc, requests.Timeout):
        summary = "Telegram 未在限定时间内确认发送结果"
        error = "消息可能已经送达，请先检查 Telegram；如未收到，再重新发送测试消息。"
        return ToolResult(
            ok=False,
            status="outcome_unknown",
            summary=summary,
            error=error,
        )
    elif isinstance(exc, requests.ConnectionError):
        summary = "暂时无法连接 Telegram"
        error = "请检查网络代理后重试。"
    else:
        summary = "Telegram 测试消息发送失败"
        error = "请检查通知配置与网络代理后重试。"
    return ToolResult(
        ok=False,
        status="unavailable",
        summary=summary,
        error=error,
    )


def send_telegram_test_notification_confirmed(
    arguments: dict[str, Any], expected_context: str
) -> ToolResult:
    telegram_test_arguments(arguments)
    state = _private_state()
    if not hmac.compare_digest(_fingerprint(state), str(expected_context or "")):
        raise AgentToolError(
            "Telegram 通知配置已变化，请重新预检",
            code="confirmation_stale",
        )
    failed = _precondition_failure(state)
    if failed is not None:
        return failed

    try:
        import telebot

        configure_telebot_logging()
        bot = telebot.TeleBot(
            state["token"],
            parse_mode="HTML",
            threaded=False,
        )
        bot.send_message(
            state["chat_id"],
            _FIXED_TEST_MESSAGE,
            timeout=_SEND_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "Agent Telegram 测试通知发送失败 type=%s",
            type(exc).__name__,
        )
        return _failure_from_exception(exc)

    return ToolResult(
        ok=True,
        status="completed",
        summary="Telegram 测试消息已发送",
        data={"sent": True},
        evidence=[
            Evidence(
                source="server_configuration",
                description="使用一次性确认票据向当前已配置会话发送固定连接测试消息；未记录或返回凭据与会话标识。",
                collected_at=_now(),
            )
        ],
        suggestions=["如未收到消息，请检查 Bot 会话权限、通知静音状态与网络代理。"],
    )


__all__ = [
    "prepare_telegram_test_notification",
    "send_telegram_test_notification_confirmed",
    "telegram_test_arguments",
]
