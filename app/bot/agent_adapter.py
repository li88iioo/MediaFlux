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
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import suppress
from contextvars import ContextVar, copy_context
from typing import Any

from app import config
from app.agent.feature_gate import (
    agent_runtime_transition,
    invalidate_agent_runtime_generation,
    is_agent_enabled,
)
from app.agent.kernel.adapters import ApprovalView, TurnView
from app.agent.kernel.bootstrap import get_agent_kernel_runtime
from app.agent.kernel.events import AgentEvent, AgentEventType
from app.agent.kernel.state import (
    CancellationToken,
    SelectionInvalidError,
    SessionBusyError,
)
from app.agent.kernel.transports import EffectEnvelope, QueryEnvelope
from app.agent.owner_routes import configured_telegram_user_ids
from app.agent.public_safety import public_tool_label
from app.agent.public_view import format_public_result
from app.agent.rate_limit import agent_rate_limiter
from app.bot.progress import TelegramProgress, send_typing
from app.bot.telegram_markdown import (
    render_telegram_markdown,
    split_telegram_html,
    telegram_html_text_length,
)
from app.modules.telegram_write_confirmations import (
    TelegramWriteConfirmationError,
    get_telegram_write_confirmation_store,
)

logger = logging.getLogger(__name__)

_ALLOWED_ID_RE = re.compile(r"^-?[1-9][0-9]*$")
_ALLOWED_USER_RE = re.compile(r"^[1-9][0-9]*$")
_TELEGRAM_MESSAGE_LIMIT = 4096
_MAX_MESSAGE = 3900
_STREAM_PREVIEW_MAX_CHARS = 720
_STREAM_PREVIEW_MAX_LINES = 16
_QUERY_LIMIT_PER_MINUTE = 12
_CALLBACK_LIMIT_PER_MINUTE = 40
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


def _enabled(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def telegram_user_is_allowed(user_id: object) -> bool:
    user = str(user_id or "").strip()
    return bool(_ALLOWED_USER_RE.fullmatch(user) and user in configured_telegram_user_ids())


def telegram_agent_control_access(chat_id: object, user_id: object) -> str:
    chat = str(chat_id or "").strip()
    user = str(user_id or "").strip()
    configured_chat = str(config.get("TG_CHAT_ID", "") or "").strip()
    if (
        not _ALLOWED_ID_RE.fullmatch(chat)
        or not _ALLOWED_USER_RE.fullmatch(user)
        or chat != configured_chat
        or user not in configured_telegram_user_ids()
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


class TelegramAgentExecutor:
    """有界执行同步 Telegram 业务；普通查询可取消，确认写入只等待结束。"""

    def __init__(self, *, max_queries: int = 8, max_controls: int = 2):
        self._limits = (max_queries, max_controls)
        self._lock = threading.RLock()
        self._pool: ThreadPoolExecutor | None = None
        self._jobs: dict[Future, bool] = {}
        self._closing = True
        self._cancellation = CancellationToken()

    def start(self) -> bool:
        with self._lock:
            if self._pool is not None:
                return not self._closing
            self._pool = ThreadPoolExecutor(
                max_workers=sum(self._limits), thread_name_prefix="telegram-agent",
            )
            self._cancellation = CancellationToken()
            self._closing = False
            return True

    def submit(self, function, *args, control: bool = False) -> Future | None:
        with self._lock:
            if self._closing or sum(kind == control for kind in self._jobs.values()) >= self._limits[control]:
                return None
            context = copy_context()
            context.run(AGENT_CANCELLATION.set, self._cancellation)
            future = self._pool.submit(context.run, function, *args)
            self._jobs[future] = control
            future.add_done_callback(self._finished)
            return future

    def _finished(self, future: Future) -> None:
        with self._lock:
            self._jobs.pop(future, None)
        if not future.cancelled() and (exc := future.exception()) is not None:
            logger.warning("Telegram Agent 后台处理失败 type=%s", type(exc).__name__)

    def stop(self, *, timeout: float = 5.0, cancel_queries: bool = True) -> bool:
        with self._lock:
            self._closing = True
            pool = self._pool
            jobs = tuple(self._jobs)
            if cancel_queries:
                self._cancellation.cancel("service_stopping")
        if wait(jobs, timeout=max(0.0, timeout)).not_done:
            return False
        if pool is not None:
            pool.shutdown(wait=True)
        with self._lock:
            self._pool = None
        return True


AGENT_CANCELLATION: ContextVar[CancellationToken | None] = ContextVar("telegram_agent_cancellation", default=None)
AGENT_EXECUTOR = TelegramAgentExecutor()


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


def _public_summary(value: dict[str, Any]) -> str:
    for key in ("summary", "message", "title", "status"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return _safe_text(candidate, limit=1200)
    return ""


def _tool_chain_line(tool_calls: tuple[str, ...] | list[str]) -> str:
    labels: list[str] = []
    for tool_name in tool_calls:
        label = public_tool_label(tool_name)
        if label not in labels:
            labels.append(label)
        if len(labels) >= 6:
            break
    return " → ".join(labels)


def _stream_preview_source(value: object) -> tuple[str, bool]:
    """截取流式回答的最新窗口，避免 Telegram 消息持续扩高推挤视口。"""

    source = str(value or "").replace("\x00", "")
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    if not source:
        return "", False

    line_starts = [match.end() for match in re.finditer("\n", source)]
    line_start = (
        line_starts[-_STREAM_PREVIEW_MAX_LINES]
        if len(line_starts) >= _STREAM_PREVIEW_MAX_LINES
        else 0
    )
    char_start = max(0, len(source) - _STREAM_PREVIEW_MAX_CHARS)
    start = max(line_start, char_start)
    if start <= 0:
        return source, False

    # 不从正文行中间开始，优先把窗口推进到下一条完整 Markdown 行。
    if source[start - 1 : start] != "\n":
        next_line = source.find("\n", start)
        if next_line >= 0:
            start = next_line + 1
    preview = source[start:].lstrip("\n")
    if not preview:
        preview = source[-_STREAM_PREVIEW_MAX_CHARS:]

    # 若窗口落在代码围栏内部，补回开围栏，让局部 Markdown 仍可安全渲染。
    prefix = source[:start]
    fences = list(re.finditer(r"(?m)^\s*(`{3,}|~{3,})[^\n]*$", prefix))
    if len(fences) % 2 == 1:
        preview = f"{fences[-1].group(1)}\n{preview}"
    return preview, True


def _truncate_stream_overflow_preview(rendered: object) -> str:
    """保证每个 Telegram 流式预览都低于单条消息的硬上限。"""

    body = str(rendered or "").strip()
    suffix = "\n\n<i>正在输出…</i>"
    candidate = body + suffix
    if telegram_html_text_length(candidate) <= _TELEGRAM_MESSAGE_LIMIT:
        return candidate

    marker = "<i>前文已生成，完成后将分段发送；下面显示最新内容</i>\n\n"
    budget = max(
        256,
        _TELEGRAM_MESSAGE_LIMIT
        - telegram_html_text_length(marker)
        - telegram_html_text_length(suffix),
    )
    latest = split_telegram_html(body, limit=budget)[-1]
    candidate = marker + latest + suffix
    if telegram_html_text_length(candidate) <= _TELEGRAM_MESSAGE_LIMIT:
        return candidate
    return split_telegram_html(candidate, limit=_TELEGRAM_MESSAGE_LIMIT)[-1]


def _preview_lines(
    approval: ApprovalView,
    *,
    tool_calls: tuple[str, ...] | list[str] = (),
) -> list[str]:
    preview = dict(approval.preview)
    result = dict(approval.result)
    confirmation = dict(approval.confirmation)
    action = _safe_text(confirmation.get("action"), limit=120) or "执行受控操作"
    summary = (
        _safe_text(confirmation.get("preflight_summary"), limit=360)
        or _public_summary(preview)
        or _public_summary(result)
        or "系统已完成写入前检查。"
    )
    lines = ["⚠️ <b>等待确认</b>", f"<b>{html.escape(action)}</b>", html.escape(summary)]
    data = preview.get("data")
    if isinstance(data, dict):
        target = str(data.get("target") or "").strip().lower()
        target_label = {
            "guangya": "光鸭云盘",
            "qb": "qBittorrent",
            "qbittorrent": "qBittorrent",
        }.get(target, _safe_text(target, limit=40))
        if target_label:
            lines.append(f"<b>目标</b>：{html.escape(target_label)}")
        for folder in data.get("receiving_folders", [])[:3]:
            lines.append(f"<b>接收目录</b>：{html.escape(_safe_text(folder, limit=240))}")
        count = data.get("count")
        if type(count) is int:
            lines.append(f"<b>对象</b>：{count} 项")
        resources = data.get("resources")
        if isinstance(resources, list) and resources:
            lines.append("\n<b>将处理</b>")
            for item in resources[:5]:
                if not isinstance(item, dict):
                    continue
                title = _safe_text(item.get("title"), limit=180) or "未命名资源"
                site = _safe_text(item.get("site_name"), limit=60)
                suffix = f" · {site}" if site else ""
                lines.append(f"• {html.escape(title + suffix)}")
            if len(resources) > 5:
                lines.append(f"• 另有 {len(resources) - 5} 项")
        effects = data.get("effects")
        if isinstance(effects, list) and effects:
            lines.append("\n<b>执行内容</b>")
            for item in effects[:4]:
                value = _safe_text(item, limit=180)
                if value:
                    lines.append(f"• {html.escape(value)}")
    impact = _safe_text(confirmation.get("impact"), limit=300)
    reversibility = _safe_text(confirmation.get("reversibility"), limit=300)
    if impact:
        lines.append(f"\n<b>影响</b>：{html.escape(impact)}")
    if reversibility:
        lines.append(f"<b>撤销</b>：{html.escape(reversibility)}")
    chain = _tool_chain_line(tool_calls)
    if chain:
        lines.append(f"\n<b>执行链</b>：{html.escape(chain)}")
    lines.append("\n确认后才会执行；取消不会写入任何变更。")
    return lines


def _render_turn(view: TurnView) -> str:
    if view.status in {"success", "partial"}:
        chain = _tool_chain_line(view.tool_calls)
        answer = str(view.answer or "Agent 未返回可显示的回答，请重试。").replace("\x00", "").strip()
        receipt = format_public_result(view.effect_result) if view.effect_result else ""
        if receipt and receipt not in answer:
            answer = receipt + "\n\n" + answer
        body = f"{answer}\n\n🔎 执行：{chain}" if chain else answer
    elif view.status == "effect_completed":
        body = (format_public_result(view.effect_result, fallback="操作已结束。")
                if view.effect_result else "执行结果尚未确认，请先查询实际业务状态，勿直接重复提交。")
    elif view.status == "cancelled":
        body = (format_public_result(view.effect_result) + "\n\n已停止后续处理；已完成操作不会撤销。"
                if view.effect_result else "已停止本次任务。")
    elif view.status == "failed":
        body = (format_public_result(
            {**view.effect_result, "ok": False,
             "error": view.effect_result.get("error") or view.error_message},
            fallback=view.error_message or "确认执行未能完成。",
        ) if view.effect_result else _safe_text(view.error_message or "Agent 暂时无法完成该请求。"))
    elif view.status == "approval_required":
        body = "等待确认。"
    else:
        body = _safe_text(view.answer or "任务已结束。")
    return render_telegram_markdown(body)


class _ExistingMessageProgress(TelegramProgress):
    """确认回调复用进度去重/限流，只更新原确认消息，不创建占位或执行器。"""

    def __init__(self, bot: Any, target: Any) -> None:
        super().__init__(
            bot, None, target.chat.id, "Agent 确认执行",
            mode="edit", message_id=target.message_id, source_message=target,
            message_thread_id=_thread_kwargs(target).get("message_thread_id"),
            timeout_seconds=1800, prefer_persistent_message=True,
            preserve_on_stop=True,
        )
        self._started = False

    def update(self, rendered: str) -> bool:
        if not self._started:
            self._started = True
            self.begin(rendered)
            self._last_rendered = None  # 原消息仍是确认卡，首次进度必须真实编辑。
        return super().update(rendered, clear_reply_markup=True)


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
            status = (
                "正在整理执行结果…"
                if event.payload.get("phase") == "confirmed_synthesis"
                else "正在规划下一步…"
            )
            await self._publish_status(status)
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

        text, force = {
            AgentEventType.CAPABILITIES_SELECTED: ("正在理解任务…", False),
            AgentEventType.TOOL_COMPLETED: ("正在整理查询结果…", False),
            AgentEventType.TOOL_FAILED: ("当前方法不可用，正在调整方案…", True),
            AgentEventType.EFFECT_PREVIEW_STARTED: ("正在生成安全变更预览…", True),
            AgentEventType.EFFECT_COMPLETED: ("正在校验执行结果…", False),
            AgentEventType.EFFECT_FAILED: ("执行未完成，正在整理结果…", False),
        }.get(event.type, ("", False))
        if event.type is AgentEventType.MODEL_TOOL_CALL:
            self.model_text = ""
            self.last_stream = ""
            self.active_tool = str(event.payload.get("tool") or "")
            text = _tool_progress(self.active_tool) + "…"
            force = True
        elif event.type is AgentEventType.TOOL_STARTED:
            self.active_tool = str(event.payload.get("tool") or self.active_tool)
            text = "正在执行已确认操作…" if event.payload.get("kind") == "confirmed_effect" else _tool_progress(self.active_tool) + "…"
        elif event.type is AgentEventType.TOOL_PROGRESS:
            text = (_safe_text(event.payload.get("summary"), limit=240)
                    if event.payload.get("phase") == "background_job" else "")
            text = text or _tool_progress(event.payload.get("tool") or self.active_tool) + "…"
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
        source, clipped = _stream_preview_source(self.model_text)
        if not source:
            return
        rendered = render_telegram_markdown(source)
        if clipped:
            rendered = "<i>回答较长，下面显示最新生成内容</i>\n\n" + rendered
        if not rendered or rendered == self.last_stream:
            return
        now = time.monotonic()
        mode = str(getattr(self.progress, "mode", "") or "")
        interval = 0.2 if mode in {"draft", "rich_draft"} else 0.85
        if not force and now - self.last_stream_at < interval:
            return
        self.last_stream_at = now
        self.last_stream = rendered
        preview = _truncate_stream_overflow_preview(rendered)
        await asyncio.to_thread(self.progress.update, preview)


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
) -> bool:
    body = str(text) if rendered_html else html.escape(_safe_text(text))
    chunks = split_telegram_html(body, limit=_MAX_MESSAGE) or ("任务已结束。",)
    return TelegramProgress(
        bot, None, target.chat.id, "Agent 回执", mode="edit",
        message_id=target.message_id,
        message_thread_id=_thread_kwargs(target).get("message_thread_id"),
    ).finish_many(chunks, reply_markup=reply_markup, clear_reply_markup=True)


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
    # Telegram 选择性引用可能来自消息末尾或外部消息；优先使用用户选中的片段。
    quote = getattr(message, "quote", None)
    replied = getattr(message, "reply_to_message", None)
    text = str(
        getattr(quote, "text", "")
        or getattr(replied, "text", "")
        or getattr(replied, "caption", "")
        or ""
    ).strip()
    return {"text": text[:2_000]} if text else {}


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

    from app.bot.agent_candidates import reply_selection_ref

    runtime = get_agent_kernel_runtime()
    candidate_context = asyncio.run(reply_selection_ref(runtime, owner=owner, session_id=session_id, message=source))
    progress = TelegramProgress(
        bot,
        telebot_module,
        chat_id,
        "Media Agent",
        source_message=source,
        timeout_seconds=300,
        prefer_persistent_message=True,
    ).begin("<b>Media Agent</b>\n正在理解任务…")
    observer = _TelegramEventObserver(progress)
    try:
        view = asyncio.run(
            runtime.telegram.query(
                QueryEnvelope(
                    owner=owner,
                    session_id=session_id,
                    message=text,
                    request_id=_request_id(source, text),
                    channel="telegram",
                    reply_context=_reply_context(source),
                    metadata={"candidate_context": candidate_context} if candidate_context else {},
                ),
                observe=observer,
                cancellation=AGENT_CANCELLATION.get(),
            )
        )
        if view.approval is not None:
            body = "\n".join(
                _preview_lines(view.approval, tool_calls=view.tool_calls)
            )
            progress.finish(
                body,
                reply_markup=_approval_markup(telebot_module, view.approval),
            )
        else:
            chunks = split_telegram_html(_render_turn(view), limit=_MAX_MESSAGE) or ["Agent 未返回可显示的回答，请重试。"]
            markup = None
            candidates = dict(view.candidate_view or {})
            if view.status in {"success", "partial"} and candidates.get("items") and (
                candidates.get("recommended_positions") or candidates.get("explicit_selection") is True
            ):
                from app.bot.agent_candidates import render, start_draft

                draft = asyncio.run(start_draft(runtime, owner=owner, session_id=session_id, view=candidates))
                body, markup = render(telebot_module, candidates, draft)
                chunks = [*chunks, body]
            progress.finish_many(chunks, reply_markup=markup)
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
    except Exception as exc:  # noqa: BLE001 - Telegram transport boundary
        if isinstance(exc, SelectionInvalidError) or (
            isinstance(exc, RuntimeError) and ("频繁" in str(exc) or "重复" in str(exc))
        ):
            bot.reply_to(message, str(exc))
        else:
            logger.warning("Telegram Agent 请求失败 type=%s", type(exc).__name__)
    return True



def _settle_candidate_draft(owner: str, session_id: str, plan_id: str, body: str, *, next_plan_id: str = "") -> None:
    from app.bot.agent_candidates import settle_draft

    try:
        asyncio.run(settle_draft(
            get_agent_kernel_runtime(), owner=owner, session_id=session_id,
            plan_id=plan_id, result_html=body, next_plan_id=next_plan_id,
        ))
    except Exception:  # noqa: BLE001 - UI 恢复不可覆盖已确认的真实业务结果
        return None

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
    if str(getattr(call, "data", "") or "").startswith("agk:s:"):
        from app.bot.agent_candidates import handle_callback

        handle_callback(bot, call, telebot_module, owner=owner, session_id=telegram_agent_session_id(chat_id, user_id))
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
    runtime = get_agent_kernel_runtime()
    plan_verified = False
    if getattr(runtime, "store", None) is not None:
        try:
            state = asyncio.run(runtime.store.load(owner=owner, session_id=session_id))
            if state.pending_effect_plan_id != envelope.plan_id:
                if getattr(call.message, "reply_markup", None) is not None:
                    with suppress(Exception):
                        bot.edit_message_reply_markup(
                            call.message.chat.id,
                            call.message.message_id,
                            reply_markup=None,
                        )
                bot.answer_callback_query(call.id, "该计划已处理或被替代，请使用当前消息中的按钮。", show_alert=True)
                return
            plan_verified = True
        except Exception:  # noqa: BLE001 - 无法核对当前计划时不触发任何写入
            bot.answer_callback_query(call.id, "当前计划暂时无法核对，请稍后重试。", show_alert=True)
            return
    if match.group("action") == "x":
        try:
            discarded = asyncio.run(
                runtime.telegram.cancel_effect(envelope)
            )
        except Exception as exc:  # noqa: BLE001 - Telegram transport boundary
            logger.warning("Telegram Agent 取消计划失败 type=%s", type(exc).__name__)
            discarded = False
        body = "已取消，本次没有执行任何写操作。" if discarded else "该确认已过期或已处理。"
        _settle_candidate_draft(owner, session_id, envelope.plan_id, html.escape(body))
        _edit_final(bot, call.message, body)
        bot.answer_callback_query(call.id, "已取消" if discarded else "确认已失效")
        return

    bot.answer_callback_query(call.id, "正在核对确认计划")
    send_typing(
        bot,
        call.message.chat.id,
        message_thread_id=getattr(call.message, "message_thread_id", None),
    )
    progress = _ExistingMessageProgress(bot, call.message)
    if plan_verified:
        progress.update("<b>Media Agent</b>\n正在核对确认计划，确认接管后将继续显示执行进度…")
    observer = _TelegramEventObserver(progress)
    try:
        view = asyncio.run(
            runtime.telegram.confirm(
                envelope,
                observe=observer,
            )
        )
        if not view.effect_result and view.error_code in {"effect_in_progress", "confirmation_invalid", "confirmation_stale", "stale_generation"}:
            notice = "⚠️ 这次确认未被接受\n" + _render_turn(view)
            notice += "\n\n请先查询任务状态；若尚未执行，请重新生成预览后确认。"
            _settle_candidate_draft(owner, session_id, envelope.plan_id, notice)
            progress.finish(notice, clear_reply_markup=True)
            return
        if view.approval is not None:
            receipt = render_telegram_markdown(format_public_result(view.effect_result)) if view.effect_result else ""
            body = (receipt + "\n\n" if receipt else "") + "\n".join(_preview_lines(view.approval, tool_calls=view.tool_calls))
            markup = _approval_markup(telebot_module, view.approval)
        else:
            body = _render_turn(view)
            markup = None
        _settle_candidate_draft(owner, session_id, envelope.plan_id, body,
            next_plan_id=view.approval.plan_id if view.approval else "")
        progress.finish_many(split_telegram_html(body, limit=_MAX_MESSAGE), reply_markup=markup, clear_reply_markup=True)
    except Exception as exc:  # noqa: BLE001 - Telegram transport boundary
        logger.warning("Telegram Agent 确认执行失败 type=%s", type(exc).__name__)
        progress.finish("确认执行状态尚未核实，请先查询任务状态，勿重复提交。", clear_reply_markup=True)


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
        asyncio.run(runtime.lifecycle.reset(owner=owner, session_id=session_id))
        bot.reply_to(message, "Media Agent 会话已重置。")
    except SessionBusyError:
        bot.reply_to(message, "已确认写操作正在执行，当前会话暂不能重置。")
    except Exception as exc:  # noqa: BLE001 - Telegram transport boundary
        logger.warning("Telegram Agent 会话重置失败 type=%s", type(exc).__name__)
        bot.reply_to(message, "Agent 会话暂时无法重置，请稍后重试。")


def _apply_agent_control_action(action_name: str) -> str:
    if action_name == "enable_telegram" and not is_agent_enabled():
        raise ValueError("请先开启 Media Agent 全局开关")
    actions = {
        "enable_all": ({"AGENT_ENABLED": "1", "TG_AGENT_ENABLED": "1"}, "Media Agent 已开启"),
        "disable_all": ({"AGENT_ENABLED": "0", "TG_AGENT_ENABLED": "0"}, "Media Agent 已关闭"),
        "enable_telegram": ({"TG_AGENT_ENABLED": "1"}, "Telegram Agent 已开启"),
        "disable_telegram": ({"TG_AGENT_ENABLED": "0"}, "Telegram Agent 已关闭"),
    }
    try:
        updates, notice = actions[action_name]
    except KeyError as exc:
        raise ValueError("不支持的 Agent 控制操作") from exc
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
