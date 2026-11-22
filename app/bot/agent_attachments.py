"""接收明确交给 Telegram Agent 选集的种子。"""

from __future__ import annotations

import html
import logging
from io import BytesIO
from typing import Any

from app.agent.rate_limit import agent_rate_limiter
from app.bot.agent_adapter import (
    _identity,
    _session_for_source,
    _thread_kwargs,
    handle_agent_message,
    telegram_agent_access,
)
from app.modules.agent_files import store_file
from app.modules.download_dispatcher import TORRENT_MAX_BYTES

logger = logging.getLogger(__name__)


def _download_torrent(bot: Any, document: Any) -> bytes | None:
    size = getattr(document, "file_size", None)
    if (size or 0) > TORRENT_MAX_BYTES:
        return None
    info = bot.get_file(document.file_id)
    remote_size = getattr(info, "file_size", None)
    if (remote_size or 0) > TORRENT_MAX_BYTES:
        return None
    if not info.file_path:
        raise ValueError("Telegram 未返回文件路径")
    data = bot.download_file(info.file_path)
    if len(data) > TORRENT_MAX_BYTES:
        return None
    if any(expected is not None and expected != len(data) for expected in (size, remote_size)):
        raise ValueError("Telegram 种子大小与文件信息不一致")
    return data


def receive_agent_torrent(bot: Any, telebot_module: Any, message: Any, instruction: str) -> None:
    chat_id, user_id = _identity(message)
    reply_kwargs = _thread_kwargs(message)
    if telegram_agent_access(chat_id, user_id) != "allowed":
        bot.reply_to(message, "当前身份未获准使用 Agent 选集。", **reply_kwargs)
        return
    owner, session_id, _ = _session_for_source(chat_id, user_id, message)
    if not agent_rate_limiter.allow(
        f"{owner}:telegram-torrent:{message.message_id}", limit=1, window_seconds=300,
    ):
        return

    document = message.document
    try:
        data = _download_torrent(bot, document)
        if data is None:
            limit_mb = TORRENT_MAX_BYTES // (1024 * 1024)
            bot.reply_to(message, f"种子文件不能超过 {limit_mb} MB。", **reply_kwargs)
            return
        if telegram_agent_access(chat_id, user_id) != "allowed":
            bot.reply_to(message, "Agent 选集权限已变更。", **reply_kwargs)
            return
        with BytesIO(data) as stream:
            file = store_file(owner, session_id, document.file_name, stream)
    except Exception as exc:  # SDK、网络和本地文件异常只记录类型。
        logger.warning("Telegram Agent 种子接收失败 type=%s", type(exc).__name__)
        bot.reply_to(message, "无法接收种子，请检查文件后重新发送。", **reply_kwargs)
        return

    bot.reply_to(message, f"已将种子 {html.escape(file['name'])} 加入当前会话。", **reply_kwargs)
    handle_agent_message(bot, telebot_module, message, torrent=(session_id, file), instruction=instruction)
