"""Telegram 对新 Agent Kernel 的薄适配器。

只负责身份、传输、事件呈现和显式按钮协议。光鸭分享、磁力/种子、离线下载
等传统 Telegram 流程仍由 ``app.bot.handlers`` 在进入本模块前处理。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import threading
import time
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any, TypeVar

from app import config
from app.agent.feature_gate import (
    agent_runtime_transition,
    invalidate_agent_runtime_generation,
    is_agent_enabled,
)
from app.agent.kernel.adapters import ApprovalView, TurnView
from app.agent.kernel.bootstrap import get_agent_kernel_runtime
from app.agent.kernel.events import AgentEvent, AgentEventType
from app.agent.kernel.transports import EffectEnvelope, QueryEnvelope
from app.agent.rate_limit import agent_rate_limiter
from app.bot.progress import TelegramProgress, send_typing
from app.bot.telegram_markdown import render_telegram_markdown
from app.modules.telegram_write_confirmations import (
    TelegramWriteConfirmationError,
    get_telegram_write_confirmation_store,
)

logger = logging.getLogger(__name__)

_ALLOWED_ID_RE = re.compile(r"^-?[1-9][0-9]*$")
_ALLOWED_USER_RE = re.compile(r"^[1-9][0-9]*$")
_MAX_MESSAGE = 3900
_QUERY_LIMIT_PER_MINUTE = 12
_CALLBACK_LIMIT_PER_MINUTE = 16
_CALLBACK_RE = re.compile(r"^agk:(?P<action>[cx]):(?P<plan>[A-Za-z0-9_-]{16,96})$")
_PATROL_PROMPTS = {
    "agp:summary": "查看最近一次全库缺集巡检的完整结果。",
    "agp:resources": "根据最近一次全库缺集巡检结果，为发现的缺集搜索可用资源。",
}
_TOOL_PROGRESS_LABELS = {
    "cloud": "正在读取光鸭云盘",
    "guangya": "正在读取光鸭云盘",
    "library": "正在查询媒体库",
    "provider": "正在查询实时服务",
    "downloads": "正在查询下载任务",
    "download": "正在处理下载任务",
    "indexer": "正在搜索资源",
    "resource": "正在搜索资源",
    "rss": "正在检查 RSS",
    "media": "正在检查媒体订阅",
    "discovery": "正在检索媒体信息",
    "web": "正在查询公开信息",
    "strm": "正在检查 STRM",
    "local_media": "正在检查本地媒体",
    "automation": "正在检查自动化任务",
}
_T = TypeVar("_T")


def _enabled(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _allowed_user_ids() -> set[str]:
    raw = str(config.get("TG_AGENT_ALLOWED_USER_IDS", "") or "")
    return {
        item
        for item in re.split(r"[,;，；\s]+", raw.strip())
        if _ALLOWED_USER_RE.fullmatch(item)
    }


def telegram_user_is_allowed(user_id: object) -> bool:
    user = str(user_id or "").strip()
    return bool(_ALLOWED_USER_RE.fullmatch(user) and user in _allowed_user_ids())


def telegram_agent_control_access(chat_id: object, user_id: object) -> str:
    chat = str(chat_id or "").strip()
    user = str(user_id or "").strip()
    configured_chat = str(config.get("TG_CHAT_ID", "") or "").strip()
    if (
        not _ALLOWED_ID_RE.fullmatch(chat)
        or not _ALLOWED_USER_RE.fullmatch(user)
        or chat != configured_chat
        or user not in _allowed_user_ids()
    ):
        return "unauthorized"
    return "allowed"


def telegram_agent_access(chat_id: object, user_id: object) -> str:
    if not is_agent_enabled() or not _enabled(config.get("TG_AGENT_ENABLED", "0")):
        return "disabled"
    return telegram_agent_control_access(chat_id, user_id)


def telegram_agent_owner(chat_id: object, user_id: object) -> str:
    chat = str(chat_id or "").strip()
    user = str(user_id or "").strip()
    if not _ALLOWED_ID_RE.fullmatch(chat) or not _ALLOWED_USER_RE.fullmatch(user):
        raise ValueError("Telegram Agent 身份无效")
    return f"tg:v1:{chat}\x1f{user}"


def telegram_agent_session_id(chat_id: object, user_id: object) -> str:
    owner = telegram_agent_owner(chat_id, user_id)
    digest = hashlib.sha256(
        b"mediaflux-agent-tg-session:v1\0" + owner.encode()
    ).hexdigest()
    return f"tg_{digest[:32]}"


def _identity(source: Any) -> tuple[str, str]:
    chat = getattr(source, "chat", None)
    if chat is None:
        message = getattr(source, "message", None)
        chat = getattr(message, "chat", None)
    sender = getattr(source, "from_user", None)
    return str(getattr(chat, "id", "") or ""), str(getattr(sender, "id", "") or "")


def _request_id(source: Any, text: str) -> str:
    message_id = getattr(source, "message_id", None)
    if message_id is None:
        message_id = getattr(getattr(source, "message", None), "message_id", "0")
    digest = hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12]
    return f"tg_{message_id}_{digest}"[:150]


def _run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """让同步 TeleBot handler 安全消费异步 Kernel。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[_T] = []
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001 - 需跨线程原样传播
            errors.append(exc)

    thread = threading.Thread(target=runner, name="telegram-agent-kernel", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


def _thread_kwargs(source: Any) -> dict[str, Any]:
    thread_id = getattr(source, "message_thread_id", None)
    return {"message_thread_id": thread_id} if thread_id is not None else {}


def _safe_text(value: object, *, limit: int = _MAX_MESSAGE) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _tool_progress(tool_name: object) -> str:
    prefix = str(tool_name or "").partition(".")[0].casefold()
    return _TOOL_PROGRESS_LABELS.get(prefix, "正在调用项目能力")


def _public_summary(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("summary", "message", "title", "status"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _safe_text(candidate, limit=1200)
    if isinstance(value, str):
        return _safe_text(value, limit=1200)
    return ""


def _preview_lines(approval: ApprovalView) -> list[str]:
    preview = dict(approval.preview)
    result = dict(approval.result)
    summary = (
        _public_summary(preview) or _public_summary(result) or "系统已完成写入前检查。"
    )
    lines = ["⚠️ <b>等待确认</b>", html.escape(summary)]
    data = preview.get("data")
    if isinstance(data, dict):
        shown = 0
        for raw_key, raw_value in data.items():
            if shown >= 6 or isinstance(raw_value, (dict, list, tuple)):
                continue
            value = _safe_text(raw_value, limit=240)
            if not value:
                continue
            lines.append(
                f"<b>{html.escape(str(raw_key)[:40])}</b>：{html.escape(value)}"
            )
            shown += 1
    lines.append("\n确认后才会执行；取消或发送新任务都不会写入。")
    return lines


def _effect_result_text(view: TurnView) -> str:
    summary = _public_summary(view.effect_result)
    return f"✅ {summary or '操作已完成并通过写后校验。'}"


def _turn_text(view: TurnView) -> str:
    if view.status == "success":
        return _safe_text(view.answer or "查询已完成。")
    if view.status == "effect_completed":
        return _effect_result_text(view)
    if view.status == "cancelled":
        return "已停止本次任务。"
    if view.status == "failed":
        return _safe_text(view.error_message or "Agent 暂时无法完成该请求。")
    if view.status == "approval_required":
        return "等待确认。"
    return _safe_text(view.answer or "任务已结束。")


class _ExistingMessageProgress:
    """让确认回调复用事件观察器，同时只更新原确认消息。"""

    mode = "edit"

    def __init__(self, bot: Any, target: Any) -> None:
        self.bot = bot
        self.target = target

    def update(self, rendered: str) -> bool:
        try:
            self.bot.edit_message_text(
                rendered,
                self.target.chat.id,
                self.target.message_id,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=None,
            )
            return True
        except Exception:  # noqa: BLE001 - 进度展示失败不应中断真实执行
            return False


class _TelegramEventObserver:
    """把 Kernel 真实事件流投影到一个 TelegramProgress，不另建状态机。"""

    def __init__(self, progress: Any) -> None:
        self.progress = progress
        self.last_status_at = 0.0
        self.last_status = ""
        self.last_stream_at = 0.0
        self.last_stream = ""
        self.model_text = ""
        self.model_round: int | None = None
        self.active_tool = ""

    async def __call__(self, event: AgentEvent) -> None:
        if event.type is AgentEventType.MODEL_STARTED:
            self.model_round = _positive_int(event.payload.get("round"))
            self.model_text = ""
            self.last_stream = ""
            await self._publish_status("正在规划下一步…")
            return

        if event.type is AgentEventType.MODEL_DELTA:
            event_round = _positive_int(event.payload.get("round"))
            if event_round is not None and event_round != self.model_round:
                self.model_round = event_round
                self.model_text = ""
                self.last_stream = ""
            delta = str(event.payload.get("delta") or "")
            if not delta:
                return
            first_delta = not self.model_text
            self.model_text += delta
            await self._publish_stream(force=first_delta)
            return

        text = ""
        force = False
        if event.type is AgentEventType.CAPABILITIES_SELECTED:
            text = "正在理解任务…"
        elif event.type is AgentEventType.MODEL_TOOL_CALL:
            self.model_text = ""
            self.last_stream = ""
            self.active_tool = str(event.payload.get("tool") or "")
            text = _tool_progress(self.active_tool) + "…"
            force = True
        elif event.type is AgentEventType.TOOL_STARTED:
            self.active_tool = str(event.payload.get("tool") or self.active_tool)
            text = _tool_progress(self.active_tool) + "…"
        elif event.type is AgentEventType.TOOL_PROGRESS:
            text = _tool_progress(event.payload.get("tool") or self.active_tool) + "…"
        elif event.type is AgentEventType.TOOL_COMPLETED:
            text = "正在整理查询结果…"
        elif event.type is AgentEventType.TOOL_FAILED:
            text = "当前方法不可用，正在调整方案…"
            force = True
        elif event.type is AgentEventType.EFFECT_PREVIEW_STARTED:
            text = "正在生成安全变更预览…"
            force = True
        elif event.type is AgentEventType.EFFECT_COMPLETED:
            text = "正在校验执行结果…"
        elif event.type is AgentEventType.EFFECT_FAILED:
            text = "执行未完成，正在整理结果…"
        if text:
            await self._publish_status(text, force=force)

    async def _publish_status(self, text: str, *, force: bool = False) -> None:
        if text == self.last_status:
            return
        now = time.monotonic()
        if not force and now - self.last_status_at < 0.65:
            return
        self.last_status_at = now
        self.last_status = text
        rendered = f"<b>Media Agent</b>\n{html.escape(text)}"
        await asyncio.to_thread(self.progress.update, rendered)

    async def _publish_stream(self, *, force: bool = False) -> None:
        source = _safe_text(self.model_text, limit=3600)
        if not source:
            return
        rendered = render_telegram_markdown(source)
        if not rendered or rendered == self.last_stream:
            return
        now = time.monotonic()
        mode = str(getattr(self.progress, "mode", "") or "")
        interval = 0.2 if mode in {"draft", "rich_draft"} else 0.85
        if not force and now - self.last_stream_at < interval:
            return
        self.last_stream_at = now
        self.last_stream = rendered
        await asyncio.to_thread(
            self.progress.update,
            rendered + "\n\n<i>正在输出…</i>",
        )


def _positive_int(value: object) -> int | None:
    try:
        normalized = int(value or 0)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _edit_final(
    bot: Any,
    target: Any,
    text: str,
    *,
    reply_markup: Any = None,
    rendered_html: bool = False,
) -> None:
    body = str(text) if rendered_html else html.escape(_safe_text(text))
    kwargs: dict[str, Any] = {
        "reply_markup": reply_markup,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        bot.edit_message_text(
            body,
            target.chat.id,
            target.message_id,
            **kwargs,
        )
    except Exception:  # noqa: BLE001 - Telegram transport fallback
        send_kwargs = _thread_kwargs(target)
        send_kwargs.update(
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if reply_markup is not None:
            send_kwargs["reply_markup"] = reply_markup
        bot.send_message(target.chat.id, body, **send_kwargs)


def _approval_markup(telebot_module: Any, approval: ApprovalView) -> Any:
    markup = telebot_module.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot_module.types.InlineKeyboardButton(
            "确认执行", callback_data=f"agk:c:{approval.plan_id}"
        ),
        telebot_module.types.InlineKeyboardButton(
            "取消", callback_data=f"agk:x:{approval.plan_id}"
        ),
    )
    return markup


def _reply_context(message: Any) -> dict[str, Any]:
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return {}
    text = str(
        getattr(replied, "text", "") or getattr(replied, "caption", "") or ""
    ).strip()
    return {"text": text[:1200]} if text else {}


def _execute_query(
    bot: Any,
    telebot_module: Any,
    source: Any,
    *,
    chat_id: str,
    user_id: str,
    text: str,
) -> TurnView:
    owner = telegram_agent_owner(chat_id, user_id)
    session_id = telegram_agent_session_id(chat_id, user_id)
    if not agent_rate_limiter.allow(
        f"{owner}:telegram-kernel-query",
        limit=_QUERY_LIMIT_PER_MINUTE,
        window_seconds=60,
    ):
        raise RuntimeError("请求过于频繁，请稍后重试。")
    event_key = hashlib.sha256(
        f"{owner}\0{getattr(source, 'message_id', '')}\0{text}".encode()
    ).hexdigest()
    if not agent_rate_limiter.allow(
        f"telegram-kernel-message:{event_key}",
        limit=1,
        window_seconds=300,
    ):
        raise RuntimeError("该消息已经处理，请勿重复发送。")

    progress = TelegramProgress(
        bot,
        telebot_module,
        chat_id,
        "Media Agent",
        source_message=source,
        timeout_seconds=300,
    ).begin("<b>Media Agent</b>\n正在理解任务…")
    observer = _TelegramEventObserver(progress)
    try:
        view = _run_async(
            get_agent_kernel_runtime().telegram.query(
                QueryEnvelope(
                    owner=owner,
                    session_id=session_id,
                    message=text,
                    request_id=_request_id(source, text),
                    channel="telegram",
                    reply_context=_reply_context(source),
                ),
                observe=observer,
            )
        )
        if view.approval is not None:
            body = "\n".join(_preview_lines(view.approval))
            progress.finish(
                body,
                reply_markup=_approval_markup(telebot_module, view.approval),
            )
        else:
            progress.finish(render_telegram_markdown(_turn_text(view)))
        return view
    except Exception:
        with suppress(Exception):
            progress.finish("Agent 暂时无法完成该请求，请稍后重试。")
        raise


def handle_agent_message(bot: Any, telebot_module: Any, message: Any) -> bool:
    chat_id, user_id = _identity(message)
    access = telegram_agent_access(chat_id, user_id)
    if access == "disabled":
        return False
    if access != "allowed":
        bot.reply_to(message, "当前身份未获准使用 Media Agent。")
        return True
    text = str(getattr(message, "text", "") or "").strip()
    if not text:
        return False
    try:
        _execute_query(
            bot,
            telebot_module,
            message,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
        )
    except RuntimeError as exc:
        if "频繁" in str(exc) or "重复" in str(exc):
            bot.reply_to(message, str(exc))
        else:
            logger.warning("Telegram Agent 请求失败 type=%s", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - Telegram transport boundary
        logger.warning("Telegram Agent 请求失败 type=%s", type(exc).__name__)
    return True


def handle_agent_callback(bot: Any, call: Any, telebot_module: Any = None) -> None:
    chat_id, user_id = _identity(call)
    if telegram_agent_access(chat_id, user_id) != "allowed":
        bot.answer_callback_query(
            call.id, "当前身份无权使用 Media Agent", show_alert=True
        )
        return
    owner = telegram_agent_owner(chat_id, user_id)
    if not agent_rate_limiter.allow(
        f"{owner}:telegram-kernel-callback",
        limit=_CALLBACK_LIMIT_PER_MINUTE,
        window_seconds=60,
    ):
        bot.answer_callback_query(call.id, "操作过于频繁，请稍后重试", show_alert=True)
        return
    match = _CALLBACK_RE.fullmatch(str(getattr(call, "data", "") or ""))
    if match is None:
        bot.answer_callback_query(call.id, "旧操作已失效，请重新发起", show_alert=True)
        with suppress(Exception):
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
            )
        return
    session_id = telegram_agent_session_id(chat_id, user_id)
    envelope = EffectEnvelope(
        owner=owner,
        session_id=session_id,
        plan_id=match.group("plan"),
        request_id=f"tgcb_{getattr(call, 'id', '')}"[:150],
        channel="telegram",
    )
    if match.group("action") == "x":
        try:
            discarded = _run_async(
                get_agent_kernel_runtime().telegram.cancel_effect(envelope)
            )
        except Exception as exc:  # noqa: BLE001 - Telegram transport boundary
            logger.warning("Telegram Agent 取消计划失败 type=%s", type(exc).__name__)
            discarded = False
        _edit_final(
            bot,
            call.message,
            "已取消，本次没有执行任何写操作。"
            if discarded
            else "该确认已过期或已处理。",
        )
        bot.answer_callback_query(call.id, "已取消" if discarded else "确认已失效")
        return

    bot.answer_callback_query(call.id, "正在执行")
    send_typing(
        bot,
        call.message.chat.id,
        message_thread_id=getattr(call.message, "message_thread_id", None),
    )
    observer = _TelegramEventObserver(_ExistingMessageProgress(bot, call.message))
    try:
        view = _run_async(
            get_agent_kernel_runtime().telegram.confirm(
                envelope,
                observe=observer,
            )
        )
        _edit_final(
            bot,
            call.message,
            render_telegram_markdown(_turn_text(view)),
            rendered_html=True,
        )
    except Exception as exc:  # noqa: BLE001 - Telegram transport boundary
        logger.warning("Telegram Agent 确认执行失败 type=%s", type(exc).__name__)
        _edit_final(bot, call.message, "确认执行失败；操作可能未开始，请重新查询状态。")


def handle_agent_patrol_callback(
    bot: Any,
    call: Any,
    telebot_module: Any = None,
) -> None:
    prompt = _PATROL_PROMPTS.get(str(getattr(call, "data", "") or ""))
    if not prompt:
        bot.answer_callback_query(call.id, "操作已失效", show_alert=True)
        return
    chat_id, user_id = _identity(call)
    if telegram_agent_access(chat_id, user_id) != "allowed":
        bot.answer_callback_query(
            call.id, "当前身份无权使用 Media Agent", show_alert=True
        )
        return
    bot.answer_callback_query(call.id, "正在交给 Media Agent")
    try:
        _execute_query(
            bot,
            telebot_module,
            call.message,
            chat_id=chat_id,
            user_id=user_id,
            text=prompt,
        )
    except Exception as exc:  # noqa: BLE001 - Telegram transport boundary
        logger.warning("Telegram Agent 巡检续接失败 type=%s", type(exc).__name__)


def _control_markup(bot_module: Any, *, chat_id: str, user_id: str) -> Any:
    globally_enabled = is_agent_enabled()
    telegram_enabled = _enabled(config.get("TG_AGENT_ENABLED", "0"))
    actions: list[tuple[str, str, dict[str, Any]]] = []
    if not globally_enabled:
        actions.append(("开启全部", "preview", {"action": "enable_all"}))
    else:
        actions.append(
            (
                "关闭 Telegram" if telegram_enabled else "开启 Telegram",
                "apply",
                {
                    "action": "disable_telegram"
                    if telegram_enabled
                    else "enable_telegram"
                },
            )
        )
        actions.append(("关闭全部", "preview", {"action": "disable_all"}))
    ids = get_telegram_write_confirmation_store().create_group(
        chat_id=chat_id,
        user_id=user_id,
        operation="agent_control",
        actions=[(decision, value) for _label, decision, value in actions],
    )
    markup = bot_module.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        *[
            bot_module.types.InlineKeyboardButton(
                label, callback_data=f"tgc:{action_id}"
            )
            for (label, _decision, _value), action_id in zip(actions, ids)
        ]
    )
    return markup


def handle_agent_guide(
    bot: Any,
    message: Any,
    telebot_module: Any | None = None,
) -> None:
    chat_id, user_id = _identity(message)
    if telegram_agent_control_access(chat_id, user_id) != "allowed":
        bot.reply_to(message, "当前身份未获准管理 Media Agent。")
        return
    global_status = "已开启" if is_agent_enabled() else "已关闭"
    telegram_status = (
        "已开启" if _enabled(config.get("TG_AGENT_ENABLED", "0")) else "已关闭"
    )
    text = (
        "<b>Media Agent</b>\n"
        f"全局：{global_status}\nTelegram：{telegram_status}\n\n"
        "直接发送自然语言即可查询和规划；真实写操作会先显示预览并等待按钮确认。"
    )
    markup = (
        _control_markup(
            telebot_module,
            chat_id=chat_id,
            user_id=user_id,
        )
        if telebot_module is not None
        else None
    )
    bot.reply_to(message, text, parse_mode="HTML", reply_markup=markup)


def handle_agent_reset(bot: Any, message: Any) -> None:
    chat_id, user_id = _identity(message)
    access = telegram_agent_access(chat_id, user_id)
    if access == "disabled":
        bot.reply_to(message, "Media Agent 当前未启用，无法重置会话。")
        return
    if access != "allowed":
        bot.reply_to(message, "当前身份未获准使用 Media Agent。")
        return
    owner = telegram_agent_owner(chat_id, user_id)
    session_id = telegram_agent_session_id(chat_id, user_id)
    try:
        runtime = get_agent_kernel_runtime()
        _run_async(runtime.telegram.cancel(owner=owner, session_id=session_id))
        _run_async(runtime.store.reset_session(owner=owner, session_id=session_id))
        bot.reply_to(message, "Media Agent 会话已重置。")
    except Exception as exc:  # noqa: BLE001 - Telegram transport boundary
        logger.warning("Telegram Agent 会话重置失败 type=%s", type(exc).__name__)
        bot.reply_to(message, "Agent 会话暂时无法重置，请稍后重试。")


def _apply_agent_control_action(action_name: str) -> str:
    updates: dict[str, str]
    if action_name == "enable_all":
        updates = {"AGENT_ENABLED": "1", "TG_AGENT_ENABLED": "1"}
        notice = "Media Agent 已开启"
    elif action_name == "disable_all":
        updates = {"AGENT_ENABLED": "0", "TG_AGENT_ENABLED": "0"}
        notice = "Media Agent 已关闭"
    elif action_name == "enable_telegram":
        if not is_agent_enabled():
            raise ValueError("请先开启 Media Agent 全局开关")
        updates = {"TG_AGENT_ENABLED": "1"}
        notice = "Telegram Agent 已开启"
    elif action_name == "disable_telegram":
        updates = {"TG_AGENT_ENABLED": "0"}
        notice = "Telegram Agent 已关闭"
    else:
        raise ValueError("不支持的 Agent 控制操作")
    with agent_runtime_transition():
        config.set_and_save(updates)
        invalidate_agent_runtime_generation()
    with suppress(Exception):
        from app.modules.agent_runtime import request_agent_runtime_reconcile

        request_agent_runtime_reconcile()
    with suppress(Exception):
        from app.bot.handlers import request_command_menu_refresh

        request_command_menu_refresh()
    return notice


def handle_agent_control_action(
    bot: Any,
    call: Any,
    telebot_module: Any,
    action: dict[str, Any],
) -> None:
    chat_id, user_id = _identity(call)
    if telegram_agent_control_access(chat_id, user_id) != "allowed":
        bot.answer_callback_query(
            call.id, "当前身份无权管理 Media Agent", show_alert=True
        )
        return
    decision = str(action.get("decision") or "")
    value = action.get("value") if isinstance(action.get("value"), dict) else {}
    action_name = str(value.get("action") or "")
    if decision == "cancel":
        _edit_final(bot, call.message, "操作已取消。")
        bot.answer_callback_query(call.id, "操作已取消")
        return
    if decision == "preview" and action_name in {"enable_all", "disable_all"}:
        confirm_id, cancel_id = get_telegram_write_confirmation_store().create_pair(
            chat_id=chat_id,
            user_id=user_id,
            operation="agent_control",
            value={"action": action_name, "confirmed": True},
        )
        markup = telebot_module.types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            telebot_module.types.InlineKeyboardButton(
                "确认开启全部" if action_name == "enable_all" else "确认关闭全部",
                callback_data=f"tgc:{confirm_id}",
            ),
            telebot_module.types.InlineKeyboardButton(
                "取消", callback_data=f"tgc:{cancel_id}"
            ),
        )
        _edit_final(
            bot,
            call.message,
            "<b>确认开启 Media Agent</b>\n将启用 Web、Telegram 和后台任务。"
            if action_name == "enable_all"
            else "<b>确认关闭 Media Agent</b>\n将停止 Agent 入口与后台任务；传统 Telegram 功能不受影响。",
            reply_markup=markup,
            rendered_html=True,
        )
        bot.answer_callback_query(call.id, "请再次确认")
        return
    if decision not in {"apply", "confirm"}:
        raise TelegramWriteConfirmationError("Agent 控制操作无效")
    if action_name in {"enable_all", "disable_all"} and not value.get("confirmed"):
        raise TelegramWriteConfirmationError("全局开关需要再次确认")
    try:
        notice = _apply_agent_control_action(action_name)
    except Exception as exc:  # noqa: BLE001 - Telegram transport boundary
        logger.warning("Telegram Agent 开关更新失败 type=%s", type(exc).__name__)
        notice = "操作未完成，请稍后重试。"
        _edit_final(bot, call.message, notice)
        bot.answer_callback_query(call.id, notice, show_alert=True)
        return
    _edit_final(bot, call.message, notice)
    bot.answer_callback_query(call.id, notice)
