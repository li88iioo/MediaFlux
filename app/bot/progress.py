"""Telegram 长任务进度生命周期。

优先使用 Bot API 原生富草稿，其次文本草稿，最后降级为可编辑消息。
所有运行中操作写入 settings_kv，服务重启后会明确收尾，避免客户端永久显示
“正在搜索/正在处理”。
"""
from __future__ import annotations

import html
import json
import secrets
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from app import database as db
from app.logger import get_logger
from app.notifier import (
    TelegramSendResult,
    call_telegram_delivery,
    telegram_edit_fallback_allowed,
)

logger = get_logger(__name__)

_PENDING_KEY = "telegram_pending_operations_v1"
_pending_lock = threading.RLock()
_active_lock = threading.RLock()
_active: dict[str, "TelegramProgress"] = {}
_terminal_retry_lock = threading.RLock()
_terminal_retry_stop = threading.Event()
_terminal_retry_ids: set[str] = set()
_terminal_retry_threads: set[threading.Thread] = set()
_MAX_PENDING_OPERATIONS = 256
_MAX_ACTIVE_OPERATIONS = 128
_PENDING_RETENTION_SECONDS = 7 * 24 * 60 * 60


def _pending_sort_key(row: dict[str, Any], position: int) -> tuple[int, int]:
    for key in ("started_at", "deadline"):
        try:
            value = int(row.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value, position
    return 0, position


def _compact_pending_rows(
    rows: Iterable[dict[str, Any]], *, now: int | None = None,
) -> list[dict[str, Any]]:
    """保留近期、活跃和终态优先记录，同时给持久 JSON 明确上限。"""
    stamp = int(time.time()) if now is None else int(now)
    deduplicated: dict[str, tuple[int, dict[str, Any]]] = {}
    for position, raw in enumerate(rows):
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        operation_id = str(row.get("id") or "").strip()
        if not operation_id:
            continue
        try:
            expires_at = int(row.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at and expires_at <= stamp:
            continue
        deduplicated[operation_id] = (position, row)

    items = list(deduplicated.items())
    if len(items) <= _MAX_PENDING_OPERATIONS:
        return [row for _operation_id, (_position, row) in items]
    with _active_lock:
        active_ids = set(_active)
    ranked = sorted(
        items,
        key=lambda item: (
            str(item[0]) in active_ids,
            bool(item[1][1].get("terminal_pending")),
            _pending_sort_key(item[1][1], item[1][0]),
        ),
        reverse=True,
    )
    selected = ranked[:_MAX_PENDING_OPERATIONS]
    selected.sort(key=lambda item: item[1][0])
    return [row for _operation_id, (_position, row) in selected]


def _load_pending() -> list[dict[str, Any]]:
    try:
        payload = json.loads(db.kv_get(_PENDING_KEY, "[]") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    rows = [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
    return _compact_pending_rows(rows)


def _save_pending(items: list[dict[str, Any]]) -> None:
    compacted = _compact_pending_rows(items)
    if len(compacted) < len(items):
        logger.warning(
            "Telegram 挂起进度记录已按保留期或容量清理 removed=%s kept=%s",
            len(items) - len(compacted),
            len(compacted),
        )
    db.kv_set(
        _PENDING_KEY,
        json.dumps(compacted, ensure_ascii=False, separators=(",", ":")),
    )


def _register_pending(item: dict[str, Any]) -> None:
    try:
        with _pending_lock:
            rows = [row for row in _load_pending() if row.get("id") != item.get("id")]
            normalized = dict(item)
            normalized.setdefault(
                "expires_at", int(time.time()) + _PENDING_RETENTION_SECONDS
            )
            rows.append(normalized)
            # 终态意图优先于普通运行记录；超过七天或容量上限后显式清理，
            # 避免 settings_kv 与每次 JSON 编解码长期无界增长。
            _save_pending(rows)
    except Exception as exc:
        logger.info("Telegram 进度状态持久化失败 type=%s", type(exc).__name__)


def _remove_pending(operation_id: str) -> None:
    try:
        with _pending_lock:
            rows = [row for row in _load_pending() if row.get("id") != operation_id]
            _save_pending(rows)
    except Exception as exc:
        logger.info("Telegram 进度状态清理失败 type=%s", type(exc).__name__)


def _update_pending(operation_id: str, **fields: Any) -> bool:
    """更新已存在的挂起操作，不为过期 operation_id 创建幽灵记录。"""
    try:
        with _pending_lock:
            rows = _load_pending()
            updated = False
            for row in rows:
                if str(row.get("id") or "") != str(operation_id or ""):
                    continue
                row.update(fields)
                updated = True
                break
            if updated:
                _save_pending(rows)
            return updated
    except Exception as exc:
        logger.info("Telegram 进度关联状态保存失败 type=%s", type(exc).__name__)
        return False


def _rich_html(rendered: str) -> str:
    """把普通 Telegram HTML 的换行转换成 Rich Message 块结构。

    ``send_message(parse_mode="HTML")`` 会保留裸换行，但 ``InputRichMessage``
    按 HTML 文档规则折叠空白。这里用段落保留分组、用 ``<br>`` 保留组内
    换行，避免终态汇总在 Rich Message 通道中挤成一整段。
    """
    if not rendered:
        return ""

    paragraphs: list[str] = []
    lines: list[str] = []
    block_lines: list[str] = []
    block_close = ""

    def is_standalone_block(value: str) -> bool:
        stripped = value.strip()
        return (
            stripped.startswith("<blockquote")
            and stripped.endswith("</blockquote>")
        ) or (stripped.startswith("<pre") and stripped.endswith("</pre>"))

    def flush_lines() -> None:
        if not lines:
            return
        paragraph = "<br>".join(lines)
        paragraphs.append(
            paragraph if len(lines) == 1 and is_standalone_block(paragraph)
            else f"<p>{paragraph}</p>"
        )
        lines.clear()

    def flush_block() -> None:
        nonlocal block_close
        if not block_lines:
            return
        block = "\n".join(block_lines)
        if block_close == "</blockquote>":
            # Rich Message 按 HTML 规则折叠普通换行；引用块内部显式保留分行。
            block = block.replace("\n", "<br>")
        paragraphs.append(block)
        block_lines.clear()
        block_close = ""

    normalized = str(rendered).replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        stripped = line.strip()
        if block_close:
            block_lines.append(line)
            if block_close in stripped:
                flush_block()
            continue
        if stripped.startswith(("<blockquote", "<pre")):
            close = "</blockquote>" if stripped.startswith("<blockquote") else "</pre>"
            if close in stripped:
                flush_lines()
                paragraphs.append(line)
            else:
                flush_lines()
                block_close = close
                block_lines.append(line)
            continue
        if stripped:
            lines.append(line)
            continue
        flush_lines()
    flush_block()
    flush_lines()
    # Rich Message 的相邻块只会换行，不会保留源文本中的空白行。
    # 在段落之间补一个显式 <br>，使标题、正文和引用块保持一行呼吸空间。
    return "<br>".join(paragraphs)


def _rich_message(telebot: Any, rendered: str) -> Any | None:
    cls = getattr(getattr(telebot, "types", None), "InputRichMessage", None)
    if not callable(cls):
        return None
    try:
        return cls(html=_rich_html(rendered))
    except Exception:
        return None


def _normalized_message_thread_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def send_typing(
    bot: Any, chat_id: object, *, message_thread_id: int | None = None
) -> None:
    action = getattr(bot, "send_chat_action", None)
    if callable(action):
        try:
            kwargs: dict[str, Any] = {}
            normalized_thread_id = _normalized_message_thread_id(message_thread_id)
            if normalized_thread_id is not None:
                kwargs["message_thread_id"] = normalized_thread_id
            action(chat_id, "typing", **kwargs)
        except Exception:
            pass


def _register_active(operation: TelegramProgress) -> None:
    with _active_lock:
        _active[operation.operation_id] = operation
        while len(_active) > _MAX_ACTIVE_OPERATIONS:
            oldest_id = next(iter(_active))
            if oldest_id == operation.operation_id and len(_active) == 1:
                break
            _active.pop(oldest_id, None)
            logger.warning(
                "Telegram 活跃进度注册表达到上限，已释放最早引用 operation=%s",
                oldest_id,
            )


@dataclass
class TelegramProgress:
    bot: Any
    telebot: Any
    chat_id: object
    label: str
    source_message: Any | None = None
    timeout_seconds: float = 180.0
    prefer_persistent_message: bool = False
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    mode: str = ""
    draft_id: int | None = None
    message_id: int | None = None
    message_thread_id: int | None = None
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _io_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _finished: bool = False
    _terminal_result: TelegramSendResult | None = field(
        default=None, init=False, repr=False,
    )

    @property
    def finished_event(self) -> threading.Event:
        """供外部长链路等待感知进度已结束或服务正在停止。"""
        return self._stop

    @property
    def terminal_outcome_unknown(self) -> bool:
        return bool(self._terminal_result and self._terminal_result.outcome_unknown)

    def begin(self, rendered: str) -> "TelegramProgress":
        source = self.source_message
        self.message_thread_id = _normalized_message_thread_id(
            getattr(source, "message_thread_id", None)
        )
        send_typing(
            self.bot, self.chat_id, message_thread_id=self.message_thread_id
        )
        self.draft_id = secrets.randbelow(2_147_483_647) + 1

        # Agent 等需要稳定引用关系的交互应直接创建真实回复，再通过 edit
        # 增量更新。Telegram 的 draft API 不支持 reply_parameters，且长草稿
        # 持续扩高会造成客户端视口跳动；其他后台任务仍默认使用原生草稿。
        if not self.prefer_persistent_message:
            rich = _rich_message(self.telebot, rendered)
            send_rich_draft = getattr(self.bot, "send_rich_message_draft", None)
            send_rich = getattr(self.bot, "send_rich_message", None)
            if callable(send_rich_draft) and callable(send_rich) and rich is not None:
                try:
                    if send_rich_draft(
                        self.chat_id,
                        self.draft_id,
                        rich,
                        message_thread_id=self.message_thread_id,
                    ):
                        self.mode = "rich_draft"
                except Exception as exc:
                    logger.info("Telegram 富草稿启动失败 type=%s", type(exc).__name__)

            if not self.mode:
                send_draft = getattr(self.bot, "send_message_draft", None)
                if callable(send_draft):
                    try:
                        if send_draft(
                            self.chat_id,
                            self.draft_id,
                            rendered,
                            message_thread_id=self.message_thread_id,
                            parse_mode="HTML",
                        ):
                            self.mode = "draft"
                    except Exception as exc:
                        logger.info(
                            "Telegram 文本草稿启动失败 type=%s",
                            type(exc).__name__,
                        )

        if not self.mode:
            result = self._send_real(rendered)
            self.message_id = getattr(result, "message_id", None)
            if self.message_id is not None and callable(
                getattr(self.bot, "edit_message_text", None)
            ):
                self.mode = "edit"
            else:
                self.mode = "reply"

        _register_pending({
            "id": self.operation_id,
            "chat_id": str(self.chat_id),
            "label": str(self.label)[:160],
            "mode": self.mode,
            "message_id": self.message_id,
            "draft_id": self.draft_id,
            "message_thread_id": self.message_thread_id,
            "started_at": int(time.time()),
            "deadline": int(time.time() + max(30.0, float(self.timeout_seconds))),
        })
        _register_active(self)
        threading.Thread(
            target=self._typing_heartbeat,
            name=f"tg-progress-{self.operation_id[:8]}",
            daemon=True,
        ).start()
        return self

    def bind_task_run(self, task_name: str, run_id: object) -> bool:
        """把 Telegram 临时进度与持久化后台任务精确关联。"""
        normalized_name = str(task_name or "").strip()
        try:
            normalized_run_id = int(run_id or 0)
        except (TypeError, ValueError):
            return False
        if not normalized_name or normalized_run_id <= 0:
            return False
        return _update_pending(
            self.operation_id,
            task_name=normalized_name,
            task_run_id=normalized_run_id,
        )

    def _typing_heartbeat(self) -> None:
        deadline = time.monotonic() + max(30.0, float(self.timeout_seconds))
        while not self._stop.wait(4.0):
            if time.monotonic() >= deadline:
                with self._io_lock:
                    if self._finished:
                        return
                self.update(
                    f"<b>{html.escape(self.label)}状态等待超时</b>\n"
                    "后台任务可能仍在继续；完成后仍会发送结果，可使用 /status 查看运行状态。"
                )
                # 超时后不再由全局注册表强引用；任务最终仍可通过自身引用完成，
                # 持久 pending 也会在重启时收尾。
                with _active_lock:
                    if _active.get(self.operation_id) is self:
                        _active.pop(self.operation_id, None)
                return
            send_typing(
                self.bot, self.chat_id, message_thread_id=self.message_thread_id
            )

    def _rich_reply_parameters(self) -> Any | None:
        source_id = getattr(self.source_message, "message_id", None)
        if source_id is None:
            return None
        types = getattr(self.telebot, "types", None)
        reply_parameters = getattr(types, "ReplyParameters", None)
        if not callable(reply_parameters):
            return None
        try:
            return reply_parameters(
                message_id=int(source_id),
                allow_sending_without_reply=True,
            )
        except (TypeError, ValueError):
            return None

    def _send_real(self, rendered: str, *, reply_markup: Any = None) -> Any:
        kwargs: dict[str, Any] = {
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if self.message_thread_id is not None:
            kwargs["message_thread_id"] = self.message_thread_id
        source_id = getattr(self.source_message, "message_id", None)
        if source_id is not None:
            kwargs["reply_to_message_id"] = source_id
        sender = getattr(self.bot, "send_message", None)
        if callable(sender):
            return sender(self.chat_id, rendered, **kwargs)
        reply = getattr(self.bot, "reply_to", None)
        if callable(reply) and self.source_message is not None:
            return reply(self.source_message, rendered, **kwargs)
        return None

    def _send_real_result(
        self, rendered: str, *, reply_markup: Any = None,
    ) -> TelegramSendResult:
        sender = getattr(self.bot, "send_message", None)
        reply = getattr(self.bot, "reply_to", None)
        if not callable(sender) and not (
            callable(reply) and self.source_message is not None
        ):
            return TelegramSendResult(
                ok=False, error="TelegramSenderUnavailable", status_code=503,
            )
        result, value = call_telegram_delivery(
            lambda: self._send_real(rendered, reply_markup=reply_markup)
        )
        if result.ok and value is None:
            return TelegramSendResult(
                ok=False, error="TelegramSendReturnedNoMessage", status_code=503,
            )
        return result

    def _send_followup_result(
        self,
        rendered: str,
        *,
        reply_markup: Any = None,
    ) -> TelegramSendResult:
        """发送终态的后续分段，不重复引用原始用户消息。"""

        sender = getattr(self.bot, "send_message", None)
        if not callable(sender):
            return TelegramSendResult(
                ok=False,
                error="TelegramSenderUnavailable",
                status_code=503,
            )
        kwargs: dict[str, Any] = {
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if self.message_thread_id is not None:
            kwargs["message_thread_id"] = self.message_thread_id
        result, value = call_telegram_delivery(
            lambda: sender(self.chat_id, rendered, **kwargs)
        )
        if result.ok and value is None:
            return TelegramSendResult(
                ok=False,
                error="TelegramSendReturnedNoMessage",
                status_code=503,
            )
        return result

    def _mark_terminal_attempt(self, operation: str, rendered: str) -> None:
        _update_pending(
            self.operation_id,
            terminal_text=str(rendered),
            terminal_pending=True,
            terminal_delivery_state="sending",
            terminal_operation=str(operation or "send"),
        )

    def _settle_terminal_result(
        self, result: TelegramSendResult, rendered: str,
    ) -> None:
        self._terminal_result = result
        if result.ok:
            _remove_pending(self.operation_id)
            return
        if result.outcome_unknown:
            logger.warning(
                "Telegram 进度终态结果未知，停止自动重放 error=%s",
                result.error or "OutcomeUnknown",
            )
            # 当前进程已确认本次发送结果未知；继续保留在“待恢复”队列会在
            # 运行期或重启后制造重复终态。硬中断场景由发送前的 sending 标记
            # 在 recover_stale_operations() 中同样隔离。
            _remove_pending(self.operation_id)
            return
        if result.retryable:
            _update_pending(
                self.operation_id,
                terminal_text=str(rendered),
                terminal_pending=True,
                terminal_delivery_state="retry_wait",
            )
            return
        logger.warning(
            "Telegram 进度终态被明确拒绝，停止无意义重试 status=%s error=%s",
            result.status_code or "-", result.error or "DeliveryRejected",
        )
        _remove_pending(self.operation_id)

    def update(self, rendered: str) -> bool:
        with self._io_lock:
            if self._finished:
                return False
            try:
                if self.mode == "rich_draft" and self.draft_id is not None:
                    rich = _rich_message(self.telebot, rendered)
                    return bool(rich is not None and self.bot.send_rich_message_draft(
                        self.chat_id,
                        self.draft_id,
                        rich,
                        message_thread_id=self.message_thread_id,
                    ))
                if self.mode == "draft" and self.draft_id is not None:
                    return bool(self.bot.send_message_draft(
                        self.chat_id,
                        self.draft_id,
                        rendered,
                        message_thread_id=self.message_thread_id,
                        parse_mode="HTML",
                    ))
                if self.mode == "edit" and self.message_id is not None:
                    self.bot.edit_message_text(
                        rendered,
                        self.chat_id,
                        self.message_id,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    return True
            except Exception as exc:
                logger.info("Telegram 进度更新失败 type=%s", type(exc).__name__)
            return False

    def _clear_draft(self) -> bool:
        if self.mode not in {"draft", "rich_draft"} or self.draft_id is None:
            return False
        try:
            if self.mode == "rich_draft":
                send_rich_draft = getattr(self.bot, "send_rich_message_draft", None)
                rich = _rich_message(self.telebot, "")
                if callable(send_rich_draft) and rich is not None:
                    cleared = bool(send_rich_draft(
                        self.chat_id,
                        self.draft_id,
                        rich,
                        message_thread_id=self.message_thread_id,
                    ))
                    if cleared:
                        return True
            send_draft = getattr(self.bot, "send_message_draft", None)
            if callable(send_draft):
                return bool(send_draft(
                    self.chat_id,
                    self.draft_id,
                    "",
                    message_thread_id=self.message_thread_id,
                ))
        except Exception:
            pass
        return False

    def _claim_finished(self) -> bool:
        if self._finished:
            return False
        self._finished = True
        with _active_lock:
            _active.pop(self.operation_id, None)
        self._stop.set()
        return True

    def _delete_placeholder(self) -> bool:
        if self.message_id is None:
            return False
        delete = getattr(self.bot, "delete_message", None)
        if not callable(delete):
            return False
        try:
            delete(self.chat_id, self.message_id)
            return True
        except Exception:
            return False

    def dismiss_source_message(self) -> bool:
        """删除触发进度的原消息，并让后续终态作为独立消息发送。

        写操作确认消息在任务真正被接纳后已经失去交互价值。删除它可以避免
        已失效的确认按钮长期占据会话；先清空 ``source_message``，也能防止
        后续降级发送尝试回复一条已经被删除的 Telegram 消息。
        """
        with self._io_lock:
            source = self.source_message
            self.source_message = None
            if source is None:
                return False
            message_id = getattr(source, "message_id", None)
            chat_id = getattr(getattr(source, "chat", None), "id", self.chat_id)
            delete = getattr(self.bot, "delete_message", None)
            if message_id is None or not callable(delete):
                return False
            try:
                delete(chat_id, message_id)
                return True
            except Exception as exc:
                logger.info(
                    "Telegram 触发消息移除失败 type=%s",
                    type(exc).__name__,
                )
                return False

    def finish(
        self,
        rendered: str,
        *,
        reply_markup: Any = None,
        clear_reply_markup: bool = False,
    ) -> bool:
        return self.finish_many(
            (rendered,),
            reply_markup=reply_markup,
            clear_reply_markup=clear_reply_markup,
        )

    def finish_many(
        self,
        rendered_chunks: Iterable[str],
        *,
        reply_markup: Any = None,
        clear_reply_markup: bool = False,
    ) -> bool:
        """可靠结束进度，并把超长终态按顺序发送为多条完整消息。"""

        chunks = tuple(str(chunk) for chunk in rendered_chunks if str(chunk))
        if not chunks:
            chunks = ("任务已结束。",)
        rendered = chunks[0]
        first_markup = reply_markup if len(chunks) == 1 else None
        with self._io_lock:
            if not self._claim_finished():
                return False
            result = TelegramSendResult(
                ok=False, error="TelegramSenderUnavailable", status_code=503,
            )
            if self.mode == "edit" and self.message_id is not None:
                kwargs: dict[str, Any] = {
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                if reply_markup is not None or clear_reply_markup:
                    kwargs["reply_markup"] = first_markup
                self._mark_terminal_attempt("edit", rendered)
                result, _value = call_telegram_delivery(
                    lambda: self.bot.edit_message_text(
                        rendered, self.chat_id, self.message_id, **kwargs
                    ),
                    message_id=int(self.message_id),
                    edit=True,
                )
                if not result.ok and telegram_edit_fallback_allowed(result):
                    logger.info(
                        "Telegram 进度消息被明确拒绝编辑，降级为新消息 error=%s",
                        result.error,
                    )
                    self._mark_terminal_attempt("send", rendered)
                    result = self._send_real_result(
                        rendered, reply_markup=first_markup,
                    )
                    if result.ok:
                        self._delete_placeholder()
            elif self.mode == "rich_draft":
                sender = getattr(self.bot, "send_rich_message", None)
                rich = _rich_message(self.telebot, rendered)
                self._mark_terminal_attempt("send", rendered)
                if callable(sender) and rich is not None:
                    kwargs = {}
                    if first_markup is not None:
                        kwargs["reply_markup"] = first_markup
                    if self.message_thread_id is not None:
                        kwargs["message_thread_id"] = self.message_thread_id
                    reply_parameters = self._rich_reply_parameters()
                    if reply_parameters is not None:
                        kwargs["reply_parameters"] = reply_parameters
                    result, _value = call_telegram_delivery(
                        lambda: sender(self.chat_id, rich, **kwargs)
                    )
                else:
                    result = self._send_real_result(
                        rendered, reply_markup=first_markup,
                    )
                self._clear_draft()
            else:
                self._mark_terminal_attempt("send", rendered)
                result = self._send_real_result(
                    rendered, reply_markup=first_markup,
                )
                self._clear_draft()
                if self.mode == "reply" and result.ok:
                    self._delete_placeholder()

            settled_text = rendered
            if result.ok and len(chunks) > 1:
                for index, continuation in enumerate(chunks[1:], start=1):
                    continuation_markup = (
                        reply_markup if index == len(chunks) - 1 else None
                    )
                    settled_text = continuation
                    self._mark_terminal_attempt("send", continuation)
                    continuation_result = self._send_followup_result(
                        continuation,
                        reply_markup=continuation_markup,
                    )
                    if continuation_result.ok:
                        result = continuation_result
                        continue
                    result = TelegramSendResult(
                        ok=False,
                        retry_after_seconds=continuation_result.retry_after_seconds,
                        error=continuation_result.error,
                        status_code=continuation_result.status_code,
                        partially_delivered=True,
                        message_id=continuation_result.message_id,
                    )
                    logger.warning(
                        "Telegram 多段终态仅部分送达 part=%s/%s status=%s",
                        index,
                        len(chunks),
                        result.status_code or "-",
                    )
                    break
            self._settle_terminal_result(result, settled_text)
            return result.ok

    def dismiss(self, fallback_text: str = "任务已完成。") -> bool:
        """仅移除临时进度，不把清理失败升级为需要重投的业务终态。

        ``finish()`` 负责可靠投递真正的成功/失败结果；``dismiss()`` 只用于
        已由其他通道发送终态后的占位清理。即使 Telegram 暂时无法删除草稿，
        也必须移除持久化记录，避免服务重启后补发一条误导性的“已结束”回执。
        """
        with self._io_lock:
            if not self._claim_finished():
                return False
            success = False
            try:
                if self.mode in {"draft", "rich_draft"}:
                    success = self._clear_draft()
                elif self.mode in {"edit", "reply"} and self.message_id is not None:
                    success = self._delete_placeholder()
                    if not success and self.mode == "edit":
                        self.bot.edit_message_text(
                            html.escape(fallback_text), self.chat_id, self.message_id
                        )
                        success = True
            except Exception as exc:
                logger.info("Telegram 临时进度移除失败 type=%s", type(exc).__name__)
            finally:
                _remove_pending(self.operation_id)
            return success


def deliver_terminal_to_existing_message(
    bot: Any,
    telebot_module: Any,
    source_message: Any,
    rendered: str,
    *,
    label: str,
    reply_markup: Any = None,
    runtime_retry: bool = False,
    persist_retry: bool = True,
) -> bool:
    """可靠投递已有消息的终态。

    带一次性确认按钮的卡片不能通过纯文本 pending 记录重投；调用方应将
    ``persist_retry`` 设为 ``False``，投递失败后同步撤销未公开计划。
    """
    chat_id = getattr(getattr(source_message, "chat", None), "id", None)
    message_id = getattr(source_message, "message_id", None)
    if chat_id is None or message_id is None:
        return False
    operation = TelegramProgress(
        bot,
        telebot_module,
        chat_id,
        str(label or "操作结果"),
        source_message=source_message,
    )
    operation.mode = "edit"
    operation.message_id = int(message_id)
    operation.message_thread_id = _normalized_message_thread_id(
        getattr(source_message, "message_thread_id", None)
    )
    stamp = int(time.time())
    _register_pending({
        "id": operation.operation_id,
        "chat_id": str(chat_id),
        "label": str(operation.label)[:160],
        "mode": "edit",
        "message_id": operation.message_id,
        "message_thread_id": operation.message_thread_id,
        "started_at": stamp,
        "deadline": stamp + 180,
        "terminal_text": str(rendered),
        "terminal_pending": True,
        "terminal_delivery_state": "retry_wait",
        "terminal_operation": "edit",
        "clear_reply_markup": True,
    })
    delivered = operation.finish(
        str(rendered),
        reply_markup=reply_markup,
        clear_reply_markup=True,
    )
    if not delivered and not persist_retry:
        _remove_pending(operation.operation_id)
    elif (
        not delivered
        and runtime_retry
        and not operation.terminal_outcome_unknown
    ):
        schedule_terminal_delivery_retry(
            bot, telebot_module, operation.operation_id
        )
    return delivered


def _clear_stale_draft(bot: Any, telebot_module: Any, row: dict[str, Any]) -> bool:
    mode = str(row.get("mode") or "")
    draft_id = row.get("draft_id")
    chat_id = row.get("chat_id")
    if mode not in {"draft", "rich_draft"} or not draft_id or not chat_id:
        return False
    try:
        kwargs: dict[str, Any] = {}
        message_thread_id = _normalized_message_thread_id(row.get("message_thread_id"))
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        if mode == "rich_draft":
            rich = _rich_message(telebot_module, "")
            sender = getattr(bot, "send_rich_message_draft", None)
            if (
                callable(sender)
                and rich is not None
                and sender(chat_id, int(draft_id), rich, **kwargs)
            ):
                return True
        sender = getattr(bot, "send_message_draft", None)
        if callable(sender):
            return bool(sender(chat_id, int(draft_id), "", **kwargs))
    except Exception as exc:
        logger.info("Telegram 中断草稿清理失败 type=%s", type(exc).__name__)
    return False


def _generic_interrupted_text(label: str) -> str:
    safe_label = html.escape(str(label or "Telegram 任务"))
    return (
        f"<b>{safe_label}已中断</b>\n"
        "服务曾重启或异常退出，本次操作已结束，请重新发起。"
    )


def _task_run_recovery_text(row: dict[str, Any], label: str) -> str:
    """依据精确 task run 恢复终态；无法确认时保持保守中断文案。"""
    fallback = _generic_interrupted_text(label)
    if str(row.get("task_name") or "") != "guangya_organize":
        return fallback
    try:
        run_id = int(row.get("task_run_id") or 0)
    except (TypeError, ValueError):
        return fallback
    if run_id <= 0:
        return fallback
    try:
        task_run = db.get_task_run(run_id)
    except Exception as exc:
        logger.info("Telegram 整理终态读取失败 type=%s", type(exc).__name__)
        return fallback
    if task_run is None or str(task_run["task_name"] or "") != "guangya_organize":
        return fallback

    status = str(task_run["status"] or "").strip().lower()
    error = str(task_run["error"] or "").strip()
    interrupted = status == "failed" and "上次进程在整理任务运行期间中断" in error
    titles = {
        "success": "光鸭整理完成",
        "partial": "光鸭整理部分完成",
        "skipped": "光鸭整理已停止",
        "failed": "光鸭整理已中断" if interrupted else "光鸭整理失败",
    }
    title = titles.get(status)
    if not title:
        return fallback

    stats: dict[str, Any] = {}
    try:
        result = json.loads(str(task_run["result"] or "{}"))
        if isinstance(result, dict) and isinstance(result.get("stats"), dict):
            stats = result["stats"]
    except (TypeError, ValueError, json.JSONDecodeError):
        stats = {}
    def safe_count(key: str) -> int:
        try:
            return max(0, int(stats.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0

    lines = [f"<b>{html.escape(title)}</b>"]
    if stats:
        lines.append(html.escape(
            f"视频 {safe_count('total')} · "
            f"已移动 {safe_count('moved')} · "
            f"需确认 {safe_count('need_confirm')} · "
            f"失败 {safe_count('failed')}"
        ))
    if interrupted:
        lines.append("服务重启时任务仍在运行，已安全结束；请先查看整理日志再决定是否重试。")
    elif error:
        lines.append(f"说明：{html.escape(error[:500])}")
    return "\n".join(lines)


def pending_stale_operation_ids() -> tuple[str, ...]:
    """快照返回启动前遗留的操作 ID，避免恢复线程误收尾新任务。"""
    try:
        with _pending_lock:
            return tuple(
                operation_id
                for row in _load_pending()
                if (operation_id := str(row.get("id") or ""))
            )
    except Exception as exc:
        logger.info("Telegram 中断任务状态读取失败 type=%s", type(exc).__name__)
        return ()


def pending_stale_operation_count(
    operation_ids: Iterable[str] | None = None,
) -> int:
    """返回仍需收尾的持久化 Telegram 操作数；读取失败返回 -1。"""
    targets = (
        {str(operation_id) for operation_id in operation_ids if str(operation_id)}
        if operation_ids is not None else None
    )
    try:
        with _pending_lock:
            rows = _load_pending()
            if targets is None:
                return len(rows)
            return sum(
                1 for row in rows if str(row.get("id") or "") in targets
            )
    except Exception as exc:
        logger.info("Telegram 中断任务状态计数失败 type=%s", type(exc).__name__)
        return -1


def _is_legacy_strm_cleanup_receipt(row: dict[str, Any]) -> bool:
    """识别旧版 ``dismiss`` 遗留的非业务终态，启动时只清理不补发。"""
    return (
        str(row.get("label") or "") == "光鸭 STRM 完整同步"
        and str(row.get("mode") or "") in {"draft", "rich_draft"}
        and bool(row.get("terminal_pending"))
        and str(row.get("terminal_text") or "")
        == "光鸭 STRM 同步已结束，汇总消息已发送。"
    )


def recover_stale_operations(
    bot: Any,
    telebot_module: Any = None,
    *,
    operation_ids: Iterable[str] | None = None,
) -> int:
    """应用启动后依据持久化任务记录收尾上次 TG 长任务。"""
    try:
        with _pending_lock:
            rows = _load_pending()
    except Exception as exc:
        logger.info("Telegram 中断任务状态读取失败 type=%s", type(exc).__name__)
        return 0
    targets = (
        {str(operation_id) for operation_id in operation_ids if str(operation_id)}
        if operation_ids is not None else None
    )
    recovered_ids: set[str] = set()
    recovered = 0
    for row in rows:
        operation_id = str(row.get("id") or "")
        if targets is not None and operation_id not in targets:
            continue
        chat_id = row.get("chat_id")
        if not operation_id or not chat_id:
            continue
        _clear_stale_draft(bot, telebot_module, row)
        if _is_legacy_strm_cleanup_receipt(row):
            recovered_ids.add(operation_id)
            recovered += 1
            continue

        delivery_state = str(row.get("terminal_delivery_state") or "")
        terminal_operation = str(row.get("terminal_operation") or "")
        if delivery_state == "outcome_unknown" or (
            delivery_state == "sending" and terminal_operation == "send"
        ):
            # 新消息发送期间进程中断，无法判断 Telegram 是否已接收。宁可保留
            # 用户当前视图，也不能在启动恢复中盲目制造第二条终态。
            logger.warning(
                "Telegram 中断终态结果未知，停止自动重放 operation=%s",
                operation_id,
            )
            recovered_ids.add(operation_id)
            recovered += 1
            continue

        label = str(row.get("label") or "Telegram 任务")
        text = str(row.get("terminal_text") or "") or _task_run_recovery_text(row, label)
        result = TelegramSendResult(
            ok=False, error="TelegramSenderUnavailable", status_code=503,
        )
        message_id = row.get("message_id")
        edit = getattr(bot, "edit_message_text", None)
        if message_id and callable(edit):
            edit_kwargs: dict[str, Any] = {"parse_mode": "HTML"}
            if bool(row.get("clear_reply_markup")):
                edit_kwargs["reply_markup"] = None
            _update_pending(
                operation_id,
                terminal_text=text,
                terminal_pending=True,
                terminal_delivery_state="sending",
                terminal_operation="edit",
            )
            result, _value = call_telegram_delivery(
                partial(edit, text, chat_id, int(message_id), **edit_kwargs),
                message_id=int(message_id),
                edit=True,
            )
            if not result.ok and telegram_edit_fallback_allowed(result):
                logger.info(
                    "Telegram 中断任务原消息被明确拒绝编辑，改发新消息 error=%s",
                    result.error,
                )
                result = TelegramSendResult(
                    ok=False, error="TelegramSenderUnavailable", status_code=503,
                )
                message_id = None
        else:
            message_id = None

        if message_id is None and not result.outcome_unknown:
            sender = getattr(bot, "send_message", None)
            if callable(sender):
                send_kwargs: dict[str, Any] = {"parse_mode": "HTML"}
                message_thread_id = _normalized_message_thread_id(
                    row.get("message_thread_id")
                )
                if message_thread_id is not None:
                    send_kwargs["message_thread_id"] = message_thread_id
                _update_pending(
                    operation_id,
                    terminal_text=text,
                    terminal_pending=True,
                    terminal_delivery_state="sending",
                    terminal_operation="send",
                )
                result, _value = call_telegram_delivery(
                    partial(sender, chat_id, text, **send_kwargs)
                )

        if result.ok:
            recovered_ids.add(operation_id)
            recovered += 1
            continue
        if result.outcome_unknown:
            logger.warning(
                "Telegram 中断任务终态结果未知，停止自动重放 operation=%s",
                operation_id,
            )
            recovered_ids.add(operation_id)
            recovered += 1
            continue
        if result.retryable:
            _update_pending(
                operation_id,
                terminal_text=text,
                terminal_pending=True,
                terminal_delivery_state="retry_wait",
            )
            continue
        logger.warning(
            "Telegram 中断任务终态被明确拒绝 status=%s error=%s",
            result.status_code or "-", result.error or "DeliveryRejected",
        )
        recovered_ids.add(operation_id)
        recovered += 1

    if recovered_ids:
        try:
            with _pending_lock:
                current = _load_pending()
                _save_pending([
                    row for row in current
                    if str(row.get("id") or "") not in recovered_ids
                ])
        except Exception as exc:
            logger.info("Telegram 中断任务状态清理失败 type=%s", type(exc).__name__)
    return recovered


def _retry_terminal_until_delivered(
    bot: Any,
    telebot_module: Any,
    operation_id: str,
    stop_event: threading.Event,
    *,
    delays: tuple[float, ...] = (2.0, 8.0, 30.0, 120.0, 300.0),
) -> int:
    """在当前 Bot 代内重投一个已持久化终态；失败记录留给启动恢复。"""
    target = (str(operation_id or "").strip(),)
    if not target[0]:
        return 0
    for delay in delays:
        if stop_event.wait(max(0.0, float(delay))):
            return 0
        recovered = recover_stale_operations(
            bot, telebot_module, operation_ids=target
        )
        if recovered:
            return recovered
        if pending_stale_operation_count(target) == 0:
            return 0
    return 0


def reset_terminal_delivery_retry_generation() -> None:
    """切换 Bot 客户端代，停止旧客户端上的运行期重投。"""
    global _terminal_retry_stop
    with _terminal_retry_lock:
        _terminal_retry_stop.set()
        _terminal_retry_stop = threading.Event()
        _terminal_retry_ids.clear()
        alive_threads = {
            thread for thread in _terminal_retry_threads if thread.is_alive()
        }
        _terminal_retry_threads.clear()
        _terminal_retry_threads.update(alive_threads)


def schedule_terminal_delivery_retry(
    bot: Any,
    telebot_module: Any,
    operation_id: str,
    *,
    delays: tuple[float, ...] = (2.0, 8.0, 30.0, 120.0, 300.0),
) -> bool:
    """为运行期新产生的 pending 终态启动一次去重、可停止的后台重投。"""
    operation_id = str(operation_id or "").strip()
    if not operation_id:
        return False
    with _terminal_retry_lock:
        if operation_id in _terminal_retry_ids:
            return False
        stop_event = _terminal_retry_stop
        _terminal_retry_ids.add(operation_id)

        def worker() -> None:
            try:
                recovered = _retry_terminal_until_delivered(
                    bot,
                    telebot_module,
                    operation_id,
                    stop_event,
                    delays=delays,
                )
                if recovered:
                    logger.info("Telegram 运行期终态已恢复投递")
            finally:
                current = threading.current_thread()
                with _terminal_retry_lock:
                    _terminal_retry_ids.discard(operation_id)
                    _terminal_retry_threads.discard(current)

        thread = threading.Thread(
            target=worker,
            name=f"tg-terminal-retry-{operation_id[:12]}",
            daemon=True,
        )
        _terminal_retry_threads.add(thread)
        try:
            thread.start()
        except Exception:
            _terminal_retry_threads.discard(thread)
            _terminal_retry_ids.discard(operation_id)
            raise
        return True


def stop_terminal_delivery_retries(timeout: float = 5.0) -> None:
    """停止当前 Bot 代的运行期终态重投线程。"""
    with _terminal_retry_lock:
        _terminal_retry_stop.set()
        threads = tuple(_terminal_retry_threads)
    deadline = time.monotonic() + max(0.0, float(timeout))
    for thread in threads:
        if thread is threading.current_thread() or not thread.is_alive():
            continue
        thread.join(timeout=max(0.0, deadline - time.monotonic()))


def cancel_active_operations(reason: str = "服务正在停止，本次操作已结束。") -> int:
    with _active_lock:
        operations = list(_active.values())
    for operation in operations:
        operation.finish(
            f"<b>{html.escape(operation.label)}已中断</b>\n{html.escape(reason)}"
        )
    return len(operations)
