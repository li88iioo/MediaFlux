"""Telegram 与 Media Agent 的最小安全适配层。

该模块只投影 Agent 的公开摘要、建议，以及严格白名单化的资源标题元数据；
不把其余工具 ``data``、异常、确认票据或用户凭据发送到 Telegram。所有写操作
仍由 Agent 自身确认门控制，Telegram callback 只携带短期 opaque action id。
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Callable

from app.agent.async_bridge import run_awaitable_sync
from app.agent.conversation_compaction import schedule_conversation_compaction
from app.agent.conversation_history import get_agent_conversation_history_repository
from app.agent.confirmation_contract import sanitize_confirmation_contract
from app.agent.feature_gate import is_agent_enabled
from app.agent.llm_router import (
    normalize_streamed_answer,
    stream_existing_answer,
    stream_tool_answer,
)
from app.agent.operation_coordinator import (
    AgentOperationCancelled,
    get_agent_operation_coordinator,
    get_telegram_message_deduplicator,
)
from app.agent.query_lifecycle import begin_query_confirmation_epoch
from app.agent.presentation_stream import (
    PublicNarrativeProjector,
    apply_streamed_answer,
    select_agent_answer_stream,
)
from app.agent.rate_limit import agent_rate_limiter, allow_agent_tool
from app.agent.registry import AgentToolError
from app.agent.result_projection import (
    project_agent_result_for_user,
    project_public_guidance,
    public_tool_label,
    replace_internal_identifiers,
    sanitize_public_multiline_text,
    sanitize_public_text,
)
from app.agent.service import get_agent_service
from app.agent.workspace_next_actions import (
    resolve_workspace_action_handoff,
    workspace_action_handoff_arguments,
)
from app.bot.progress import deliver_terminal_to_existing_message
from app.config import get
from app.logger import get_logger

logger = get_logger(__name__)

_ACTION_TTL_SECONDS = 60
_ACTION_MAX_ENTRIES = 512
_MAX_MESSAGE_LENGTH = 3900


_READ_PLAN_LABELS = {
    "workspace.health": "工作区健康",
    "workspace.todo": "待办概览",
    "workspace.briefing": "系统简报",
    "workspace.next_actions": "建议行动",
    "config.diagnose": "项目配置",
    "config.diagnose_media_servers": "媒体服务器",
    "config.feature_summary": "功能状态",
    "downloads.diagnose_queue": "下载队列",
    "rss.diagnose": "RSS 订阅",
    "strm.status": "STRM 状态",
    "strm.diagnose": "STRM 诊断",
    "strm.triage_failures": "STRM 失败分诊",
    "local_media.diagnose": "本地媒体",
    "indexer.diagnose_readiness": "资源站",
    "automation.diagnose_pipeline": "自动化链路",
    "library.patrol_status": "媒体库巡检",
    "agent.action_history": "操作历史",
}
_MAX_SUMMARY_LENGTH = 900
_MAX_NARRATIVE_LENGTH = 1000
_MAX_SUGGESTION_LENGTH = 280
_MAX_TRACE_ITEMS = 5
_TELEGRAM_QUERY_LIMIT_PER_MINUTE = 12
_TELEGRAM_CALLBACK_LIMIT_PER_MINUTE = 12
_RESOURCE_RESULT_LIMIT = 3
_EPISODE_FOLLOWUP_LIMIT = 3
_WORKSPACE_ACTION_LIMIT = 5
_TELEGRAM_STREAM_UPDATE_INTERVAL_SECONDS = 0.35

_URL_RE = re.compile(r"(?i)\b(?:https?://|magnet:\?|ed2k://)\S+")
_UNIX_PATH_RE = re.compile(r"(?<![\w.])/(?:[^\s/]+/)+[^\s]*")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b(?:[a-z]:\\|\\\\)[^\s]+")
_SECRET_RE = re.compile(
    r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|authorization|password|secret)\b\s*[:=]?\s*\S*"
)
_GENERIC_TOKEN_RE = re.compile(r"(?i)\btoken\s*[:=]\s*\S+")
_OPAQUE_RE = re.compile(r"\b[A-Za-z0-9_-]{48,}\b")
_ALLOWED_ID_RE = re.compile(r"^-?\d{1,24}$")
_RESULT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_ACTION_GROUP_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@dataclass(frozen=True)
class _StreamMessage:
    mode: str
    chat_id: object
    source_message_id: int | None = None
    message_thread_id: int | None = None
    draft_id: int | None = None
    message_id: int | None = None


@dataclass(frozen=True)
class _TelegramPublishResult:
    sent: bool
    delivery: _StreamMessage | None = None


@dataclass(frozen=True)
class _AgentAction:
    action_id: str
    owner: str
    action: str
    expires_at: float
    group_id: str
    confirmation_id: str = ""
    result_id: str = ""
    target: str = ""
    tool_name: str = ""
    arguments_json: str = ""
    action_key: str = ""


class TelegramAgentActionStore:
    """有界、一次性、按 Telegram 身份隔离的交互操作仓库。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = _ACTION_TTL_SECONDS,
        max_entries: int = _ACTION_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18),
    ) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_entries = max(2, int(max_entries))
        self._clock = clock
        self._token_factory = token_factory
        self._items: dict[str, _AgentAction] = {}
        self._lock = threading.RLock()

    def _prune_locked(self, now: float) -> None:
        for action_id in [
            key for key, value in self._items.items() if value.expires_at <= now
        ]:
            self._items.pop(action_id, None)

    def _store_locked(self, item: _AgentAction) -> str:
        if not item.action_id or ":" in item.action_id:
            raise RuntimeError("无法生成 Telegram Agent 操作标识")
        self._items[item.action_id] = item
        while len(self._items) > self._max_entries:
            self._items.pop(next(iter(self._items)), None)
        return item.action_id

    def _new_action_id_locked(self) -> str:
        for _ in range(8):
            action_id = str(self._token_factory() or "").strip()
            if action_id and ":" not in action_id and action_id not in self._items:
                return action_id
        raise RuntimeError("无法生成 Telegram Agent 操作标识")

    def create(self, *, owner: str, confirmation_id: str, action: str) -> str:
        owner_key = str(owner or "").strip()
        ticket = str(confirmation_id or "").strip()
        action_name = str(action or "").strip()
        if not owner_key or not ticket or action_name not in {"confirm", "cancel"}:
            raise ValueError("无法创建 Telegram Agent 操作")
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            return self._store_locked(
                _AgentAction(
                    action_id=self._new_action_id_locked(),
                    owner=owner_key,
                    action=action_name,
                    expires_at=now + self._ttl_seconds,
                    group_id=f"confirmation:{ticket}",
                    confirmation_id=ticket,
                )
            )

    def create_resource_prepare(
        self,
        *,
        owner: str,
        result_id: str,
        target: str,
        group_id: str,
    ) -> str:
        """创建只在服务端保存资源句柄的短期预检操作。"""
        owner_key = str(owner or "").strip()
        resource_id = str(result_id or "").strip()
        target_name = str(target or "").strip().lower()
        group_key = str(group_id or "").strip()
        if (
            not owner_key
            or not _RESULT_ID_RE.fullmatch(resource_id)
            or target_name not in {"qb", "guangya"}
            or not _ACTION_GROUP_RE.fullmatch(group_key)
        ):
            raise ValueError("无法创建 Telegram 资源操作")
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            return self._store_locked(
                _AgentAction(
                    action_id=self._new_action_id_locked(),
                    owner=owner_key,
                    action="prepare_resource",
                    expires_at=now + self._ttl_seconds,
                    group_id=f"resource:{group_key}",
                    result_id=resource_id,
                    target=target_name,
                )
            )

    def create_read_tool(
        self,
        *,
        owner: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """保存严格白名单化的只读工具调用，callback 仅暴露 opaque id。"""
        owner_key = str(owner or "").strip()
        tool = str(tool_name or "").strip()
        if not owner_key or tool != "library.search_missing_episode_resources":
            raise ValueError("无法创建 Telegram 只读操作")

        from app.agent.episode_resource_actions import (
            missing_episode_resource_arguments,
        )

        normalized = missing_episode_resource_arguments(arguments)
        arguments_json = json.dumps(
            normalized,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(arguments_json) > 2048:
            raise ValueError("Telegram 只读操作参数过长")
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            action_id = self._new_action_id_locked()
            return self._store_locked(
                _AgentAction(
                    action_id=action_id,
                    owner=owner_key,
                    action="invoke_read_tool",
                    expires_at=now + self._ttl_seconds,
                    group_id=f"read:{action_id}",
                    tool_name=tool,
                    arguments_json=arguments_json,
                )
            )

    def create_workspace_action(self, *, owner: str, action_key: str) -> str:
        """保存固定白名单行动；Telegram callback 只携带 opaque id。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise ValueError("无法创建 Telegram 工作区操作")
        try:
            normalized = workspace_action_handoff_arguments({"action_key": action_key})
        except AgentToolError as exc:
            raise ValueError("无法创建 Telegram 工作区操作") from exc
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            action_id = self._new_action_id_locked()
            return self._store_locked(
                _AgentAction(
                    action_id=action_id,
                    owner=owner_key,
                    action="invoke_workspace_action",
                    expires_at=now + self._ttl_seconds,
                    group_id=f"workspace:{action_id}",
                    action_key=normalized["action_key"],
                )
            )

    def claim_workspace_action(
        self, action_id: str, *, owner: str
    ) -> dict[str, Any]:
        """原子领取工作区行动，确保并发 callback 只会占用一次目标额度。"""
        key = str(action_id or "").strip()
        owner_key = str(owner or "").strip()
        with self._lock:
            self._prune_locked(self._clock())
            item = self._items.get(key)
            if (
                item is None
                or item.action != "invoke_workspace_action"
                or not secrets.compare_digest(item.owner, owner_key)
            ):
                raise ValueError("操作已过期或无效")
            self._items.pop(key, None)
            return {
                "action": item.action,
                "action_key": item.action_key,
                "expires_at": item.expires_at,
            }

    def restore_workspace_action(
        self,
        action_id: str,
        *,
        owner: str,
        action_key: str,
        expires_at: float,
    ) -> bool:
        """目标限流拒绝时恢复原票据，但不延长其原始有效期。"""
        key = str(action_id or "").strip()
        owner_key = str(owner or "").strip()
        if not key or ":" in key or not owner_key:
            return False
        try:
            normalized = workspace_action_handoff_arguments({"action_key": action_key})
            expiry = float(expires_at)
        except (AgentToolError, TypeError, ValueError, OverflowError):
            return False
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            if expiry <= now or key in self._items:
                return False
            self._store_locked(
                _AgentAction(
                    action_id=key,
                    owner=owner_key,
                    action="invoke_workspace_action",
                    expires_at=expiry,
                    group_id=f"workspace:{key}",
                    action_key=normalized["action_key"],
                )
            )
            return True

    def validate(self, action_id: str, *, owner: str) -> None:
        """确认操作仍有效且属于当前身份，但不消费一次性票据。"""
        self.inspect(action_id, owner=owner)

    def inspect(self, action_id: str, *, owner: str) -> dict[str, str]:
        """返回限流所需的最小操作元数据，但不消费一次性票据。"""
        key = str(action_id or "").strip()
        owner_key = str(owner or "").strip()
        with self._lock:
            self._prune_locked(self._clock())
            item = self._items.get(key)
            if item is None or not secrets.compare_digest(item.owner, owner_key):
                raise ValueError("操作已过期或无效")
            return {
                "action": item.action,
                "tool_name": item.tool_name,
                "action_key": item.action_key,
            }

    def resolve(self, action_id: str, *, owner: str) -> dict[str, Any]:
        key = str(action_id or "").strip()
        owner_key = str(owner or "").strip()
        with self._lock:
            self._prune_locked(self._clock())
            item = self._items.get(key)
            if item is None or not secrets.compare_digest(item.owner, owner_key):
                raise ValueError("操作已过期或无效")
            self._items.pop(key, None)
            # 任一按钮被合法所有者使用后，同一交互组的其他按钮同步失效。
            for sibling_id in [
                candidate_id
                for candidate_id, candidate in self._items.items()
                if candidate.owner == item.owner
                and candidate.group_id == item.group_id
            ]:
                self._items.pop(sibling_id, None)
            if item.action == "prepare_resource":
                return {
                    "action": item.action,
                    "result_id": item.result_id,
                    "target": item.target,
                }
            if item.action == "invoke_read_tool":
                arguments = json.loads(item.arguments_json)
                if not isinstance(arguments, dict):
                    raise ValueError("操作已过期或无效")
                return {
                    "action": item.action,
                    "tool_name": item.tool_name,
                    "arguments": arguments,
                }
            if item.action == "invoke_workspace_action":
                return {
                    "action": item.action,
                    "action_key": item.action_key,
                }
            return {
                "confirmation_id": item.confirmation_id,
                "action": item.action,
            }

    def revoke_owner(self, *, owner: str) -> int:
        """撤销当前 Telegram 身份尚未消费的全部回调操作。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            return 0
        with self._lock:
            self._prune_locked(self._clock())
            revoked = [
                action_id
                for action_id, item in self._items.items()
                if secrets.compare_digest(item.owner, owner_key)
            ]
            for action_id in revoked:
                self._items.pop(action_id, None)
            return len(revoked)


_action_store = TelegramAgentActionStore()


def get_telegram_agent_action_store() -> TelegramAgentActionStore:
    return _action_store


def _enabled(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _agent_llm_enabled() -> bool:
    return _enabled(get("AGENT_LLM_ENABLED", "0"))


def _allowed_user_ids() -> set[str]:
    raw = str(get("TG_AGENT_ALLOWED_USER_IDS", "") or "")
    return {
        part
        for part in re.split(r"[,;\s]+", raw.strip())
        if _ALLOWED_ID_RE.fullmatch(part)
    }


def telegram_user_is_allowed(user_id: object) -> bool:
    """检查 Telegram 用户是否位于共享的受信任用户白名单。"""
    user = str(user_id or "").strip()
    return bool(_ALLOWED_ID_RE.fullmatch(user) and user in _allowed_user_ids())


def telegram_agent_access(chat_id: object, user_id: object) -> str:
    """返回 ``disabled``、``unauthorized`` 或 ``allowed``，默认拒绝。"""
    if not is_agent_enabled():
        return "disabled"
    if not _enabled(get("TG_AGENT_ENABLED", "0")):
        return "disabled"
    chat = str(chat_id or "").strip()
    user = str(user_id or "").strip()
    configured_chat = str(get("TG_CHAT_ID", "") or "").strip()
    allowed_users = _allowed_user_ids()
    if (
        not configured_chat
        or not _ALLOWED_ID_RE.fullmatch(chat)
        or not _ALLOWED_ID_RE.fullmatch(user)
        or chat != configured_chat
        or user not in allowed_users
    ):
        return "unauthorized"
    return "allowed"


def telegram_agent_owner(chat_id: object, user_id: object) -> str:
    chat = str(chat_id or "").strip()
    user = str(user_id or "").strip()
    if not _ALLOWED_ID_RE.fullmatch(chat) or not _ALLOWED_ID_RE.fullmatch(user):
        raise ValueError("Telegram Agent 身份无效")
    return f"tg:v1:{chat}\x1f{user}"


def _redact_text(value: object, *, limit: int) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    text = _URL_RE.sub("[链接已隐藏]", text)
    text = _WINDOWS_PATH_RE.sub("[路径已隐藏]", text)
    text = _UNIX_PATH_RE.sub("[路径已隐藏]", text)
    text = _GENERIC_TOKEN_RE.sub("[凭据已隐藏]", text)
    text = _SECRET_RE.sub("[凭据已隐藏]", text)
    text = _OPAQUE_RE.sub("[标识已隐藏]", text)
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return html.escape(text, quote=False)


def _public_text(value: object, *, limit: int) -> str:
    """Telegram 公开文本：隐藏凭据、路径、链接和内部工具标识。"""
    return _redact_text(replace_internal_identifiers(value), limit=limit)


def _public_multiline_html(
    value: object,
    *,
    limit: int,
    promote_first: bool = False,
) -> str:
    """把公开文本转成 Telegram 可读的短段落/项目列表，不解析任意 HTML。"""
    text = str(value or "").replace("\x00", " ")
    text = _URL_RE.sub("[链接已隐藏]", text)
    text = _WINDOWS_PATH_RE.sub("[路径已隐藏]", text)
    text = _UNIX_PATH_RE.sub("[路径已隐藏]", text)
    text = _GENERIC_TOKEN_RE.sub("[凭据已隐藏]", text)
    text = _SECRET_RE.sub("[凭据已隐藏]", text)
    text = _OPAQUE_RE.sub("[标识已隐藏]", text)
    text = sanitize_public_multiline_text(text, limit=limit)
    if not text:
        return ""
    rendered: list[str] = []
    previous_blank = False
    first_paragraph_rendered = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if rendered and not previous_blank:
                rendered.append("")
            previous_blank = True
            continue
        previous_blank = False
        bullet = re.match(r"^[-*•]\s+(.+)$", line)
        if bullet:
            rendered.append(f"• {html.escape(bullet.group(1).strip(), quote=False)}")
            continue
        escaped = html.escape(line, quote=False)
        if promote_first and not first_paragraph_rendered and len(line) <= 120:
            escaped = f"<b>{escaped}</b>"
        rendered.append(escaped)
        first_paragraph_rendered = True
    return "\n".join(rendered).strip()


def _truncate_telegram_html(value: object, *, limit: int = _MAX_MESSAGE_LENGTH) -> str:
    """在 Telegram 长度限制内截断受控 HTML，并保持标签与实体完整。"""
    text = str(value or "")
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]

    truncation_notice = "\n\n（内容过长，已截断）"
    target = max(1, limit - len(truncation_notice) - 12)
    cut = target
    for separator in ("\n\n", "\n", " "):
        position = text.rfind(separator, 0, target + 1)
        if position >= target // 2:
            cut = position
            break

    last_open = text.rfind("<", 0, cut)
    last_close = text.rfind(">", 0, cut)
    if last_open > last_close:
        cut = last_open
    last_entity = text.rfind("&", 0, cut)
    last_semicolon = text.rfind(";", 0, cut)
    if last_entity > last_semicolon:
        cut = last_entity

    fragment = text[:cut].rstrip()
    while fragment:
        open_count = max(0, fragment.count("<b>") - fragment.count("</b>"))
        suffix = ("</b>" * open_count) + truncation_notice
        if len(fragment) + len(suffix) <= limit:
            return fragment + suffix
        fragment = fragment[:-1].rstrip()
    return truncation_notice.strip()[:limit]


def _telegram_public_status(
    display: dict[str, Any], result: dict[str, Any]
) -> tuple[str, str, str, bool]:
    """读取公开 display 状态；存在公开状态时不再被内部 ok/status 覆盖。"""
    raw = display.get("status") if isinstance(display.get("status"), dict) else {}
    explicit = bool(raw)
    key = str(raw.get("key") or "").strip().lower()
    tone = str(raw.get("tone") or "").strip().lower()
    label = _public_text(raw.get("label"), limit=80)
    if key not in {"success", "attention", "unavailable", "in_progress"}:
        key = ""
    if tone not in {"good", "warning", "error", "neutral"}:
        tone = ""
    if explicit:
        if not key:
            key = {
                "good": "success",
                "warning": "attention",
                "error": "unavailable",
                "neutral": "in_progress",
            }.get(tone, "")
        return key, tone, label, True

    result_status = str(result.get("status") or "").strip().lower()
    if result_status in {"clarification_required", "selection_required"}:
        return result_status, "neutral", "", False
    if bool(result.get("ok")):
        return "success", "good", "", False
    return "unavailable", "error", "", False


def _telegram_streaming_enabled() -> bool:
    return _enabled(get("TG_AGENT_STREAMING_ENABLED", "1"))


def _message_context(message: Any) -> tuple[object | None, int | None, int | None]:
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    source_message_id = getattr(message, "message_id", None)
    message_thread_id = getattr(message, "message_thread_id", None)
    return (
        chat_id,
        source_message_id if isinstance(source_message_id, int) else None,
        message_thread_id if isinstance(message_thread_id, int) else None,
    )


def _rich_message(telebot: Any, html_text: str) -> Any | None:
    input_rich_message = getattr(getattr(telebot, "types", None), "InputRichMessage", None)
    if not callable(input_rich_message):
        return None
    try:
        return input_rich_message(html=html_text)
    except Exception:
        return None


def _begin_agent_stream(
    bot: Any, telebot: Any, message: Any
) -> _StreamMessage | None:
    """优先启动 Telegram 原生草稿流；不支持时降级为可编辑消息。"""
    if not _telegram_streaming_enabled():
        return None
    chat_id, source_message_id, message_thread_id = _message_context(message)
    if chat_id is None:
        return None

    send_action = getattr(bot, "send_chat_action", None)
    if callable(send_action):
        try:
            send_action(chat_id, "typing")
        except Exception:
            pass

    draft_id = secrets.randbelow(2_147_483_647) + 1
    send_rich_draft = getattr(bot, "send_rich_message_draft", None)
    send_rich = getattr(bot, "send_rich_message", None)
    thinking = _rich_message(
        telebot, "<tg-thinking>正在理解请求…</tg-thinking>"
    )
    if callable(send_rich_draft) and callable(send_rich) and thinking is not None:
        try:
            if send_rich_draft(
                chat_id,
                draft_id,
                thinking,
                message_thread_id=message_thread_id,
            ):
                return _StreamMessage(
                    mode="rich_draft",
                    chat_id=chat_id,
                    source_message_id=source_message_id,
                    message_thread_id=message_thread_id,
                    draft_id=draft_id,
                )
        except Exception as exc:
            logger.info(
                "Telegram Agent 富草稿流启动失败 type=%s", type(exc).__name__
            )

    send_draft = getattr(bot, "send_message_draft", None)
    if callable(send_draft):
        try:
            if send_draft(
                chat_id,
                draft_id,
                "",
                message_thread_id=message_thread_id,
            ):
                return _StreamMessage(
                    mode="draft",
                    chat_id=chat_id,
                    source_message_id=source_message_id,
                    message_thread_id=message_thread_id,
                    draft_id=draft_id,
                )
        except Exception as exc:
            logger.info(
                "Telegram Agent 文本草稿流启动失败 type=%s", type(exc).__name__
            )

    send_message = getattr(bot, "send_message", None)
    edit_message = getattr(bot, "edit_message_text", None)
    if not callable(send_message) or not callable(edit_message):
        return None
    kwargs: dict[str, Any] = {
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if source_message_id is not None:
        kwargs["reply_to_message_id"] = source_message_id
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
    try:
        placeholder = send_message(
            chat_id,
            "<b>Media Agent</b>\n正在理解请求…",
            **kwargs,
        )
    except Exception as exc:
        logger.info("Telegram Agent 流式占位发送失败 type=%s", type(exc).__name__)
        return None
    message_id = getattr(placeholder, "message_id", None)
    target_chat = getattr(getattr(placeholder, "chat", None), "id", chat_id)
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        return None
    return _StreamMessage(
        mode="edit",
        chat_id=target_chat,
        source_message_id=source_message_id,
        message_thread_id=message_thread_id,
        message_id=message_id,
    )


def _update_agent_stream(
    bot: Any,
    telebot: Any,
    target: _StreamMessage,
    text: str,
    *,
    reply_markup: Any = None,
    parse_mode: str | None = "HTML",
) -> bool:
    try:
        if target.mode == "rich_draft":
            rich = _rich_message(telebot, text)
            if rich is None or target.draft_id is None:
                return False
            return bool(
                bot.send_rich_message_draft(
                    target.chat_id,
                    target.draft_id,
                    rich,
                    message_thread_id=target.message_thread_id,
                )
            )
        if target.mode == "draft":
            if target.draft_id is None:
                return False
            return bool(
                bot.send_message_draft(
                    target.chat_id,
                    target.draft_id,
                    text,
                    message_thread_id=target.message_thread_id,
                    parse_mode=parse_mode,
                )
            )
        if target.mode == "edit" and target.message_id is not None:
            kwargs: dict[str, Any] = {
                "reply_markup": reply_markup,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            bot.edit_message_text(
                text,
                target.chat_id,
                target.message_id,
                **kwargs,
            )
            return True
    except Exception as exc:
        logger.info("Telegram Agent 流式消息更新失败 type=%s", type(exc).__name__)
    return False


def _rich_reply_parameters(telebot: Any, source_message_id: int | None) -> Any:
    reply_parameters = getattr(getattr(telebot, "types", None), "ReplyParameters", None)
    if source_message_id is None or not callable(reply_parameters):
        return None
    try:
        return reply_parameters(message_id=source_message_id)
    except Exception:
        return None


def _delivery_from_message(
    value: Any, *, fallback_chat_id: object
) -> _StreamMessage | None:
    message_id = getattr(value, "message_id", None)
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        return None
    chat_id = getattr(getattr(value, "chat", None), "id", fallback_chat_id)
    if chat_id is None:
        return None
    return _StreamMessage(mode="sent", chat_id=chat_id, message_id=message_id)


def _persist_agent_stream(
    bot: Any,
    telebot: Any,
    target: _StreamMessage,
    rendered: str,
    *,
    reply_markup: Any = None,
) -> _TelegramPublishResult:
    """把临时草稿固化为真实消息，并保留可供迟到清理的消息引用。"""
    edit_fallback = target.mode == "edit"
    if edit_fallback:
        sent = _update_agent_stream(
            bot, telebot, target, rendered, reply_markup=reply_markup
        )
        if sent:
            return _TelegramPublishResult(sent=True, delivery=target)

    if target.mode == "rich_draft":
        send_rich = getattr(bot, "send_rich_message", None)
        rich = _rich_message(telebot, rendered)
        if callable(send_rich) and rich is not None:
            kwargs: dict[str, Any] = {"reply_markup": reply_markup}
            if target.message_thread_id is not None:
                kwargs["message_thread_id"] = target.message_thread_id
            reply_parameters = _rich_reply_parameters(
                telebot, target.source_message_id
            )
            if reply_parameters is not None:
                kwargs["reply_parameters"] = reply_parameters
            try:
                message = send_rich(target.chat_id, rich, **kwargs)
                return _TelegramPublishResult(
                    sent=True,
                    delivery=_delivery_from_message(
                        message, fallback_chat_id=target.chat_id
                    ),
                )
            except Exception as exc:
                logger.info(
                    "Telegram Agent 富消息固化失败 type=%s", type(exc).__name__
                )

    send_message = getattr(bot, "send_message", None)
    if not callable(send_message):
        return _TelegramPublishResult(sent=False)
    kwargs = {
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup,
    }
    if target.source_message_id is not None:
        kwargs["reply_to_message_id"] = target.source_message_id
    if target.message_thread_id is not None:
        kwargs["message_thread_id"] = target.message_thread_id
    try:
        message = send_message(target.chat_id, rendered, **kwargs)
        result = _TelegramPublishResult(
            sent=True,
            delivery=_delivery_from_message(
                message, fallback_chat_id=target.chat_id
            ),
        )
        if edit_fallback:
            # 最终编辑失败后会降级发送新消息；成功后删除旧占位，避免聊天中
            # 同时残留“正在处理”和最终答案。删除失败不影响最终消息。
            _delete_stale_telegram_delivery(bot, target)
        return result
    except Exception as exc:
        logger.info("Telegram Agent 草稿固化失败 type=%s", type(exc).__name__)
        return _TelegramPublishResult(sent=False)


def _reply_agent_message(
    bot: Any, message: Any, rendered: str, **kwargs: Any
) -> _TelegramPublishResult:
    sent = bot.reply_to(message, rendered, **kwargs)
    chat_id, _source_message_id, _message_thread_id = _message_context(message)
    return _TelegramPublishResult(
        sent=True,
        delivery=(
            _delivery_from_message(sent, fallback_chat_id=chat_id)
            if chat_id is not None
            else None
        ),
    )


def _delete_stale_telegram_delivery(
    bot: Any, delivery: _StreamMessage | None
) -> bool:
    """尽力删除已失去租约后才完成的 Bot 消息；草稿和失败删除保持静默。"""
    if delivery is None or delivery.message_id is None:
        return False
    delete_message = getattr(bot, "delete_message", None)
    if not callable(delete_message):
        return False
    try:
        deleted = delete_message(delivery.chat_id, delivery.message_id)
        if deleted is False:
            logger.info("Telegram Agent 迟到消息未能删除")
            return False
        return True
    except Exception as exc:
        logger.info(
            "Telegram Agent 迟到消息清理失败 type=%s", type(exc).__name__
        )
        return False


def _finish_agent_stream(
    bot: Any,
    telebot: Any,
    target: _StreamMessage | None,
    rendered: str,
    *,
    reply_markup: Any = None,
    show_progress: bool = True,
) -> _TelegramPublishResult:
    """更新一次阶段状态后立即固化最终结果，不阻塞 Telegram handler。"""
    if target is None:
        return _TelegramPublishResult(sent=False)
    if show_progress:
        _update_agent_stream(
            bot,
            telebot,
            target,
            "<b>Media Agent</b>\n正在整理结果…",
            reply_markup=None,
        )
    return _persist_agent_stream(
        bot, telebot, target, rendered, reply_markup=reply_markup
    )


def _stream_answer_source(
    message: str,
    response: dict[str, Any],
    *,
    owner: str,
) -> AsyncIterator[str] | None:
    """兼容旧私有入口；实际资格判断由共享投影层维护。"""
    return select_agent_answer_stream(
        message,
        response,
        owner=owner,
        tool_stream_factory=stream_tool_answer,
        conversation_stream_factory=stream_existing_answer,
    )


def _apply_streamed_answer(
    response: dict[str, Any], answer: str
) -> dict[str, Any]:
    """兼容旧私有入口；实际合并规则由共享投影层维护。"""
    return apply_streamed_answer(
        response,
        answer,
        result_projector=project_agent_result_for_user,
    )


def _stream_preview_html(value: str, *, interrupted: bool = False) -> str:
    """Telegram 草稿使用与最终消息一致的安全分段，避免流式阶段出现文本墙。"""
    body = _public_multiline_html(value, limit=1800, promote_first=True)
    body = body or "正在组织答复…"
    if interrupted:
        return f"{body}\n\n<i>生成中断，已保留上方已生成内容。</i>"
    return f"{body}\n\n<code>▍</code>"


def _publish_telegram_io_if_current(
    coordinator: Any,
    operation: Any,
    callback: Callable[[], Any],
) -> tuple[bool, Any | None]:
    """在不持有 owner 生命周期锁的前提下执行一次 Telegram I/O。

    外部网络调用无法可靠中断，因此发送前后都检查租约。新消息和 reset 可以在
    请求阻塞期间立即撤销旧租约；旧请求返回后不得继续写历史或发送后续内容。
    """
    if not coordinator.is_current(operation):
        return False, None
    result = callback()
    return coordinator.is_current(operation), result


def _trace_operation_id(operation: Any) -> str:
    value = str(getattr(operation, "operation_id", "") or "").strip()
    return value or f"tg_trace_{secrets.token_urlsafe(12)}"


def _telegram_callback_operation_id(owner: str, call: Any, *, action: str) -> str:
    """为 callback 派生稳定且不泄露身份/票据的短操作标识。"""
    source = "\x00".join((owner, str(getattr(call, "id", "") or ""), action))
    digest = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"tg_callback_{digest}"


def _publish_telegram_callback_response(
    bot: Any,
    message: Any,
    *,
    coordinator: Any,
    operation: Any,
    owner: str,
    response: Any,
    history_generation: int | None,
    history_message: str,
    fallback_summary: str,
    prepare_output: Callable[[], tuple[str, Any | None]],
) -> bool:
    """只让当前 callback 构造动作、发布消息并写入会话历史。"""
    allowed, prepared = coordinator.publish_if_current(operation, prepare_output)
    if not allowed or prepared is None:
        return False
    rendered, markup = prepared
    allowed, publish_result = _publish_telegram_io_if_current(
        coordinator,
        operation,
        lambda: _reply_agent_message(
            bot,
            message,
            rendered,
            reply_markup=markup,
            parse_mode="HTML",
        ),
    )
    if not allowed:
        if isinstance(publish_result, _TelegramPublishResult):
            _delete_stale_telegram_delivery(bot, publish_result.delivery)
        return False

    finalized, _ = coordinator.finalize_if_current(
        operation,
        lambda: _record_telegram_callback_conversation(
            owner,
            message=history_message,
            response=response,
            generation=history_generation,
            fallback_summary=fallback_summary,
        ),
    )
    if not finalized and isinstance(publish_result, _TelegramPublishResult):
        _delete_stale_telegram_delivery(bot, publish_result.delivery)
    return finalized


async def _consume_agent_stream(
    bot: Any,
    telebot: Any,
    target: _StreamMessage,
    source: AsyncIterator[str],
    *,
    is_current: Callable[[], bool] | None = None,
    publish: Callable[[Callable[[], Any]], tuple[bool, Any | None]] | None = None,
) -> tuple[str, bool, bool]:
    """节流更新同一条 Telegram 草稿，并阻止旧操作继续发布。"""
    projector = PublicNarrativeProjector()
    published = ""
    last_preview = ""
    last_update_at = 0.0
    emitted = False

    def ensure_current() -> None:
        if is_current is not None and not is_current():
            raise AgentOperationCancelled("Telegram Agent 操作已失效")

    def update_preview(*, force: bool = False, interrupted: bool = False) -> None:
        nonlocal last_preview, last_update_at
        ensure_current()
        preview = published.strip()
        if not preview:
            return
        now = time.monotonic()
        if (
            not force
            and last_preview
            and now - last_update_at < _TELEGRAM_STREAM_UPDATE_INTERVAL_SECONDS
        ):
            return
        if not interrupted and preview == last_preview:
            return

        callback = lambda: _update_agent_stream(
            bot,
            telebot,
            target,
            _stream_preview_html(preview, interrupted=interrupted),
            parse_mode="HTML",
        )
        if publish is not None:
            allowed, _ = publish(callback)
            if not allowed:
                _delete_stale_telegram_delivery(bot, target)
                raise AgentOperationCancelled("Telegram Agent 操作已失效")
        else:
            callback()
        last_preview = preview
        last_update_at = now

    try:
        async for delta in source:
            ensure_current()
            normalized_delta = str(delta or "")
            if not normalized_delta:
                continue
            projected = projector.feed(normalized_delta)
            if projected is not None:
                published = projected.cumulative
                emitted = True
                update_preview()
            projector.raise_pending_error()

        ensure_current()
        answer = projector.finalize(require_emitted=True)
        if not answer:
            return "", False, False
        published = answer
        emitted = True
        update_preview(force=True)
        return answer, True, False
    except AgentOperationCancelled:
        raise
    except Exception as exc:
        logger.warning(
            "Telegram Agent Provider 流中断 emitted=%s type=%s",
            emitted,
            type(exc).__name__,
        )
        if not emitted:
            return "", False, False
        partial = projector.published_answer()
        if not partial:
            return "", True, True
        published = partial
        update_preview(force=True, interrupted=True)
        return partial, True, True


def _telegram_guidance(response: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[Any] = []
    presentation = response.get("presentation")
    if isinstance(presentation, dict) and isinstance(presentation.get("guidance"), list):
        candidates.extend(presentation["guidance"])
    display = response.get("display")
    if isinstance(display, dict) and isinstance(display.get("guidance"), list):
        candidates.extend(display["guidance"])
    if isinstance(response.get("guidance"), list):
        candidates.extend(response["guidance"])
    result = response.get("result")
    if not candidates and isinstance(result, dict):
        candidates.extend(project_public_guidance(result.get("suggestions")))

    projected: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        prompt = sanitize_public_text(item.get("prompt"), limit=_MAX_SUGGESTION_LENGTH)
        label = sanitize_public_text(item.get("label"), limit=100) or prompt
        if not prompt or prompt in seen:
            continue
        seen.add(prompt)
        projected.append({
            "prompt": prompt,
            "label": label,
            "kind": "read" if item.get("kind") == "read" else "draft",
        })
        if len(projected) >= 3:
            break
    return projected


_LIBRARY_AUDIT_TELEGRAM_TOOLS = {
    "library.audit_library_episodes",
    "library.start_episode_audit",
    "agent.job_status",
    "library.patrol_status",
}


def _safe_public_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _public_episode_code(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    season = _safe_public_count(value.get("season"))
    episode = _safe_public_count(value.get("episode"))
    if episode <= 0 or season > 999 or episode > 9999:
        return ""
    return f"S{season:02d}E{episode:02d}"


def _telegram_library_audit_details(response: dict[str, Any]) -> str:
    """只投影缺集巡检的安全计数与少量剧名，不暴露任务 ID 或服务端细节。"""
    tool_call = response.get("tool_call")
    name = str(tool_call.get("name") or "") if isinstance(tool_call, dict) else ""
    if name not in _LIBRARY_AUDIT_TELEGRAM_TOOLS:
        return ""
    result = response.get("result")
    if not isinstance(result, dict):
        return ""
    data = result.get("data")
    if not isinstance(data, dict):
        return ""
    status = str(result.get("status") or data.get("outcome") or "").strip().casefold()
    task_status = str(data.get("task_status") or "").strip().casefold()

    if name == "library.start_episode_audit":
        if status == "confirmation_required":
            return ""
        max_series = _safe_public_count(data.get("max_series"))
        if data.get("reused"):
            return "<b>后台任务</b>\n相同范围的巡检已在运行，没有重复创建。"
        if data.get("accepted"):
            suffix = f"；每批最多核对 {max_series} 部剧集" if max_series else ""
            return f"<b>后台任务</b>\n任务已加入队列{suffix}。完成前不会把零值解释为没有缺集。"
        return ""

    checked = _safe_public_count(data.get("checked_series_count") or data.get("progress_current"))
    total = _safe_public_count(data.get("progress_total"))
    updates = _safe_public_count(data.get("updates_available_count"))
    missing = _safe_public_count(data.get("missing_episode_count"))
    inconclusive = _safe_public_count(data.get("inconclusive_count"))
    unmapped = _safe_public_count(data.get("unmapped_series_count"))

    if task_status == "pending":
        return "<b>巡检进度</b>\n任务正在排队，尚未形成缺集结论。"
    if task_status == "running":
        progress = f"{checked}/{total}" if total else (f"已核对 {checked} 部" if checked else "正在统计媒体库范围")
        return f"<b>巡检进度</b>\n{progress}；完成前不会把零值解释为没有缺集。"
    if task_status == "retry_wait":
        return "<b>巡检进度</b>\n当前批次暂时无法完成，系统会自动重试；这不是最终结论。"
    if task_status == "cancelled":
        return "<b>巡检进度</b>\n任务已取消，已保存结果不会触发下载或整理。"
    if task_status == "failed":
        return "<b>巡检进度</b>\n任务未完成，请检查媒体服务器与 TMDB 连接。"
    if task_status in {"not_created", "not_run"} or status == "not_run":
        return "<b>巡检状态</b>\n尚无巡检结果；这不代表媒体库没有缺集。"

    lines = ["<b>核对范围</b>", f"已实际核对 {checked} 部；确认 {updates} 部共缺 {missing} 集。"]
    coverage = []
    if unmapped:
        coverage.append(f"{unmapped} 部缺少可靠 TMDB 映射")
    if inconclusive:
        coverage.append(f"{inconclusive} 部暂时无法确认")
    if data.get("continuation_pending"):
        coverage.append("本批进度已保存，后台将继续下一批")
    if coverage:
        lines.extend(["", "<b>覆盖说明</b>", "；".join(coverage) + "。"])

    findings = data.get("findings") if isinstance(data.get("findings"), list) else []
    finding_lines: list[str] = []
    for item in findings[:3]:
        if not isinstance(item, dict):
            continue
        title = _public_text(item.get("title"), limit=90) or "未命名剧集"
        item_missing = _safe_public_count(item.get("missing_count"))
        samples = [
            code for code in (_public_episode_code(value) for value in (item.get("missing_sample") or [])[:6])
            if code
        ]
        suffix = f"（{'、'.join(samples)}{' 等' if item.get('missing_sample_truncated') else ''}）" if samples else ""
        label = "暂时无法确认" if str(item.get("status") or "") == "inconclusive" else f"缺 {item_missing} 集"
        finding_lines.append(f"• 《{title}》{label}{suffix}")
    if finding_lines:
        lines.extend(["", "<b>缺集摘要</b>", *finding_lines])
        if data.get("findings_truncated") or len(findings) > 3:
            lines.append("仅展示前 3 项，完整结果保留在巡检记录中。")
    return "\n".join(lines)


def _telegram_agent_trace_details(response: dict[str, Any]) -> str:
    """展示模型实际核对过的公开数据源，不暴露工具名、参数或内部标识。"""
    raw_trace = response.get("agent_trace")
    if not isinstance(raw_trace, list):
        return ""
    partial_payload = response.get("agent_partial")
    partial = (
        isinstance(partial_payload, dict)
        and partial_payload.get("complete") is False
    )
    projected: list[tuple[str, bool, str]] = []
    for item in raw_trace[:_MAX_TRACE_ITEMS]:
        if not isinstance(item, dict):
            continue
        label = _public_text(item.get("label"), limit=70)
        if not label:
            continue
        projected.append((
            label,
            item.get("ok") is True,
            _public_text(item.get("summary"), limit=150),
        ))
    if not projected or (len(projected) == 1 and not partial):
        return ""

    title = "已完成部分核对" if partial else "本次核对"
    lines = [f"<b>{title}</b>"]
    for label, ok, summary in projected:
        lines.append(f"• <b>{label}</b> · {'完成' if ok else '需关注'}")
        if summary:
            lines.append(f"  {summary}")
    remaining = max(0, len(raw_trace) - len(projected))
    if remaining:
        lines.append(f"另有 {remaining} 项已核对。")
    return "\n".join(lines)


def render_agent_response(response: Any, *, confirmation: bool = False) -> str:
    """将 Agent 回复排成自然短段落，避免重复栏目和内部诊断细节。"""
    payload = response if isinstance(response, dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    display = payload.get("display") if isinstance(payload.get("display"), dict) else {}
    ok = bool(result.get("ok"))
    status = str(result.get("status") or "").strip()
    public_key, public_tone, public_label, explicit_public_status = (
        _telegram_public_status(display, result)
    )
    summary = _public_multiline_html(
        display.get("summary") or result.get("summary"),
        limit=_MAX_SUMMARY_LENGTH,
        promote_first=True,
    )

    presentation = payload.get("presentation")
    narrative = ""
    if isinstance(presentation, dict) and presentation.get("source") == "llm":
        narrative = _public_multiline_html(
            presentation.get("narrative"),
            limit=_MAX_NARRATIVE_LENGTH,
            promote_first=True,
        )

    structured_details = _telegram_library_audit_details(payload)
    trace_details = _telegram_agent_trace_details(payload)
    body_parts = [
        item for item in (narrative or summary, structured_details, trace_details) if item
    ]
    body = "\n\n".join(body_parts) or "当前没有可安全展示的结果摘要。"
    error = _public_multiline_html(
        display.get("error") or result.get("error"), limit=500
    )
    if confirmation:
        lines: list[str] = ["<b>需要你确认</b>", body]
    elif status in {"clarification_required", "selection_required"}:
        # 补充信息并不是失败，不用错误标题制造不必要的挫败感。
        lines = [body]
    elif explicit_public_status and (
        public_key == "unavailable" or public_tone == "error"
    ):
        lines = [f"<b>{public_label or '暂时无法完成'}</b>", body]
        if error and error != body:
            lines.extend(["", error])
    elif explicit_public_status and (
        public_key == "attention" or public_tone == "warning"
    ):
        lines = [f"<b>{public_label or '需要留意'}</b>", body]
        if error and error != body:
            lines.extend(["", error])
    elif explicit_public_status and public_key == "in_progress" and public_label:
        lines = [f"<b>{public_label}</b>", body]
    elif not ok:
        lines = ["<b>没能完成这次请求</b>", body]
        if error and error != body:
            lines.extend(["", error])
    else:
        lines = [body]

    if payload.get("mode") == "read_plan":
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        safe_steps = []
        for index, step in enumerate(steps[:4], start=1):
            if not isinstance(step, dict):
                continue
            step_result = step.get("result") if isinstance(step.get("result"), dict) else {}
            label = _READ_PLAN_LABELS.get(str(step.get("tool_name") or ""), "诊断步骤")
            step_summary = _public_text(step_result.get("summary"), limit=180)
            step_status = "完成" if step_result.get("ok") is True else "需关注"
            safe_steps.append(
                f"{index}. <b>{html.escape(label, quote=False)}</b> · {step_status}"
                + (f"\n   {step_summary}" if step_summary else "")
            )
        if safe_steps:
            lines.extend(["", "<b>检查步骤</b>", *safe_steps])

    guidance = _telegram_guidance(payload)
    if guidance:
        lines.extend(["", "<b>接下来可以</b>"])
        for item in guidance:
            lines.append(f"• {item['label']}")

    if confirmation:
        confirmation_payload = payload.get("confirmation")
        contract = sanitize_confirmation_contract(
            confirmation_payload.get("contract")
            if isinstance(confirmation_payload, dict)
            else {}
        )
        if contract:
            lines.extend([
                "",
                "<b>操作对象</b>",
                _public_text(contract.get("object"), limit=160) or "当前预检选中的对象",
                "",
                "<b>影响</b>",
                _public_text(contract.get("impact"), limit=220) or "确认后会执行预检通过的写操作。",
                "",
                "<b>撤销方式</b>",
                _public_text(contract.get("reversibility"), limit=220) or "执行后可能需要手动撤销。",
            ])
            preview = _public_text(contract.get("preflight_summary"), limit=180)
            if preview:
                lines.extend(["", "<b>预检</b>", preview])
        lines.extend(["", "只有点击下方确认按钮才会执行；请在 60 秒内完成确认，超时后需重新发起。"])

    return _truncate_telegram_html("\n".join(lines))


def _identity(message_or_call: Any) -> tuple[str, str]:
    message = getattr(message_or_call, "message", None) or message_or_call
    chat = getattr(message, "chat", None)
    user = getattr(message_or_call, "from_user", None) or getattr(
        message, "from_user", None
    )
    return str(getattr(chat, "id", "")), str(getattr(user, "id", ""))


def _telegram_history_identity(owner: str) -> tuple[str, str]:
    digest = hashlib.sha256(
        b"mediaflux-agent-telegram-history:v1\0" + owner.encode("utf-8")
    ).hexdigest()
    return f"telegram:{owner}", digest[:32]


def _telegram_conversation_context(owner: str) -> tuple[list[dict[str, Any]], int | None]:
    principal, session_id = _telegram_history_identity(owner)
    repository = get_agent_conversation_history_repository()
    try:
        generation = repository.session_generation(
            principal=principal, session_id=session_id
        )
        context = repository.get_llm_context(
            principal=principal, session_id=session_id, tail_limit=10
        )
    except Exception as exc:
        logger.warning("Telegram Agent 对话上下文读取失败 type=%s", type(exc).__name__)
        return [], None
    return (context if isinstance(context, list) else []), generation


def _telegram_history_generation(owner: str) -> int | None:
    principal, session_id = _telegram_history_identity(owner)
    try:
        return get_agent_conversation_history_repository().session_generation(
            principal=principal,
            session_id=session_id,
        )
    except Exception as exc:
        logger.warning("Telegram Agent 对话代次读取失败 type=%s", type(exc).__name__)
        return None


_CALLBACK_MEDIA_CONTEXT_TOOLS = frozenset({
    "library.search",
    "library.count_series_episodes",
    "library.audit_episodes",
    "library.audit_library_episodes",
    "library.check_updates",
    "library.search_missing_episode_resources",
    "library.search_missing_season_resources",
    "media.subscription_updates",
    "discovery.search",
    "discovery.recommend",
    "discovery.lookup_rating",
    "discovery.add_watchlist",
    "indexer.search_resources",
})


def _safe_callback_media_history(
    payload: dict[str, Any], result: dict[str, Any]
) -> tuple[str, dict[str, str]]:
    """只保留 callback 续问所需的媒体身份，不保留票据、内部 ID 或路径。"""
    tool_call = payload.get("tool_call") if isinstance(payload.get("tool_call"), dict) else {}
    tool_name = str(tool_call.get("name") or "").strip()[:120]
    if tool_name not in _CALLBACK_MEDIA_CONTEXT_TOOLS:
        return "", {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    arguments = (
        tool_call.get("arguments")
        if isinstance(tool_call.get("arguments"), dict)
        else {}
    )
    title = sanitize_public_text(
        data.get("title")
        or data.get("query")
        or arguments.get("title")
        or arguments.get("query"),
        limit=160,
    )
    if not title:
        return tool_name, {}
    media: dict[str, str] = {"title": title}
    original_title = sanitize_public_text(
        data.get("original_title") or arguments.get("original_title"), limit=160
    )
    if original_title:
        media["original_title"] = original_title
    year = str(data.get("year") or arguments.get("year") or "").strip()
    if re.fullmatch(r"(?:19|20)\d{2}", year):
        media["year"] = year
    media_type = str(
        data.get("media_type") or arguments.get("media_type") or ""
    ).strip().lower()
    if media_type in {"movie", "tv"}:
        media["media_type"] = media_type
    return tool_name, media


def _safe_callback_history_response(
    response: Any, *, fallback_summary: str
) -> dict[str, Any]:
    """把按钮动作投影为不可重放、无内部标识的安全会话结果。"""
    payload = response if isinstance(response, dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    ok = bool(result.get("ok", True))
    summary = sanitize_public_text(result.get("summary"), limit=600) or sanitize_public_text(
        fallback_summary, limit=600
    )
    suggestions: list[str] = []
    raw_suggestions = result.get("suggestions")
    if isinstance(raw_suggestions, list):
        for value in raw_suggestions[:3]:
            safe = sanitize_public_text(value, limit=180)
            if safe and safe not in suggestions:
                suggestions.append(safe)
    tool_name, media_context = _safe_callback_media_history(payload, result)
    safe_response: dict[str, Any] = {
        "mode": "conversation",
        "result": {
            "ok": ok,
            "status": "completed" if ok else "needs_attention",
            "summary": summary or ("操作已完成" if ok else "操作未能完成"),
            "error": "",
            "suggestions": suggestions,
            "evidence": [],
        },
    }
    if tool_name:
        safe_response["tool_call"] = {"name": tool_name, "arguments": {}}
        if media_context:
            safe_response["result"]["data"] = media_context
    return safe_response


def _record_telegram_callback_conversation(
    owner: str,
    *,
    message: str,
    response: Any,
    generation: int | None,
    fallback_summary: str,
) -> None:
    _record_telegram_conversation(
        owner,
        message=message,
        response=_safe_callback_history_response(
            response, fallback_summary=fallback_summary
        ),
        generation=generation,
    )


def _telegram_reply_context(message: Any) -> dict[str, Any]:
    """只接受同一聊天中由 Bot 发出的被回复消息，作为有限追问上下文。"""
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return {}
    current_chat = getattr(getattr(message, "chat", None), "id", None)
    replied_chat = getattr(getattr(replied, "chat", None), "id", None)
    author = getattr(replied, "from_user", None)
    if current_chat is None or replied_chat != current_chat or not bool(getattr(author, "is_bot", False)):
        return {}
    raw_text = getattr(replied, "text", None) or getattr(replied, "caption", None)
    safe_text = sanitize_public_text(raw_text, limit=600)
    if not safe_text:
        return {}
    message_id = getattr(replied, "message_id", None)
    context: dict[str, Any] = {"text": safe_text}
    if isinstance(message_id, int):
        context["message_id"] = message_id
    return context


def _record_telegram_conversation(
    owner: str, *, message: str, response: dict[str, Any], generation: int | None
) -> None:
    if generation is None:
        return
    principal, session_id = _telegram_history_identity(owner)
    repository = get_agent_conversation_history_repository()
    try:
        persisted = repository.append_query_turn(
            principal=principal,
            session_id=session_id,
            message=message,
            response=response,
            expected_generation=generation,
        )
        if persisted:
            schedule_conversation_compaction(
                principal=principal,
                session_id=session_id,
                llm_owner=owner,
                repository=repository,
            )
    except Exception as exc:
        logger.warning("Telegram Agent 对话历史写入失败 type=%s", type(exc).__name__)


def handle_agent_guide(bot: Any, message: Any) -> None:
    """展示 Telegram Agent 使用入口，不触发 Agent 查询或外部调用。"""
    chat_id, user_id = _identity(message)
    access = telegram_agent_access(chat_id, user_id)
    if access == "disabled":
        bot.reply_to(message, "Media Agent 当前未启用。请在控制台启用后再试。")
        return
    if access != "allowed":
        bot.reply_to(message, "当前身份未获准使用 Media Agent。")
        return
    bot.reply_to(
        message,
        "<b>Media Agent 已就绪</b>\n\n"
        "直接发送问题即可查询媒体库、下载、RSS 或整理状态，也可搜索资源并选择"
        " qBittorrent / 光鸭；"
        "下载链接仍按原下载流程处理。写操作会先要求确认。\n\n"
        "使用 /agent_reset 可清除当前 Agent 会话和待确认操作。",
        parse_mode="HTML",
    )


def handle_agent_reset(bot: Any, message: Any) -> None:
    """重置当前 Telegram 身份隔离的 Agent 会话。"""
    chat_id, user_id = _identity(message)
    access = telegram_agent_access(chat_id, user_id)
    if access == "disabled":
        bot.reply_to(message, "Media Agent 当前未启用，无法重置会话。")
        return
    if access != "allowed":
        bot.reply_to(message, "当前身份未获准使用 Media Agent。")
        return
    try:
        owner = telegram_agent_owner(chat_id, user_id)
        service = get_agent_service()

        def reset_runtime_and_actions() -> None:
            try:
                service.reset_session(owner=owner)
            finally:
                # 即使持久化上下文清理失败，也不能留下仍可点击的旧写操作。
                get_telegram_agent_action_store().revoke_owner(owner=owner)
            principal, session_id = _telegram_history_identity(owner)
            get_agent_conversation_history_repository().delete_session(
                principal=principal,
                session_id=session_id,
            )

        get_agent_operation_coordinator().invalidate_owner(
            owner=owner,
            reason="session_reset",
            invalidate=reset_runtime_and_actions,
        )
        bot.reply_to(
            message,
            "Agent 会话已重置。已清除当前会话上下文和待确认操作。",
        )
    except AgentToolError as exc:
        logger.info("Telegram Agent 会话重置被拒绝 code=%s", exc.code)
        bot.reply_to(message, "Agent 会话暂时无法重置，请稍后重试。")
    except Exception as exc:
        logger.warning("Telegram Agent 会话重置失败 type=%s", type(exc).__name__)
        bot.reply_to(message, "Agent 会话暂时无法重置，请稍后重试。")


def _confirmation_markup(
    telebot: Any,
    *,
    owner: str,
    confirmation_id: str,
    action: str = "",
):
    store = get_telegram_agent_action_store()
    confirm_id = store.create(
        owner=owner, confirmation_id=confirmation_id, action="confirm"
    )
    cancel_id = store.create(
        owner=owner, confirmation_id=confirmation_id, action="cancel"
    )
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    action_label = sanitize_public_text(action, limit=24)
    markup.add(
        telebot.types.InlineKeyboardButton(
            f"确认：{action_label}" if action_label else "确认执行",
            callback_data=f"aga:{confirm_id}",
        ),
        telebot.types.InlineKeyboardButton(
            "取消", callback_data=f"aga:{cancel_id}"
        ),
    )
    return markup


def _episode_resource_followups(response: Any) -> list[dict[str, Any]]:
    """提取单剧审计返回的结构化缺集搜索动作，不解析自然语言建议。"""
    payload = response if isinstance(response, dict) else {}
    tool_call = payload.get("tool_call")
    if not isinstance(tool_call, dict) or tool_call.get("name") != "library.audit_episodes":
        return []
    result = payload.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    raw_followups = data.get("resource_followups") if isinstance(data, dict) else None
    if not isinstance(raw_followups, list):
        return []

    from app.agent.episode_resource_actions import missing_episode_resource_arguments

    followups: list[dict[str, Any]] = []
    for item in raw_followups:
        if not isinstance(item, dict) or item.get("tool") != "library.search_missing_episode_resources":
            continue
        label = str(item.get("episode_label") or "").strip().upper()
        if not re.fullmatch(r"S\d{2}E\d{2,4}", label):
            continue
        arguments = item.get("arguments")
        if not isinstance(arguments, dict):
            continue
        try:
            normalized = missing_episode_resource_arguments(arguments)
        except AgentToolError:
            continue
        if label != f"S{normalized['season']:02d}E{normalized['episode']:02d}":
            continue
        followups.append({
            "tool_name": "library.search_missing_episode_resources",
            "arguments": normalized,
            "label": f"{label} 找资源",
        })
        if len(followups) >= _EPISODE_FOLLOWUP_LIMIT:
            break
    return followups


def _episode_followup_markup(
    telebot: Any,
    *,
    owner: str,
    followups: list[dict[str, Any]],
):
    if not followups:
        return None
    store = get_telegram_agent_action_store()
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for followup in followups:
        action_id = store.create_read_tool(
            owner=owner,
            tool_name=followup["tool_name"],
            arguments=followup["arguments"],
        )
        markup.add(
            telebot.types.InlineKeyboardButton(
                followup["label"], callback_data=f"aga:{action_id}"
            )
        )
    return markup


def _workspace_next_actions(response: Any) -> list[dict[str, str]]:
    """提取服务端白名单化的工作区行动，不信任客户端目标或参数。"""
    payload = response if isinstance(response, dict) else {}
    tool_call = payload.get("tool_call")
    if not isinstance(tool_call, dict) or tool_call.get("name") != "workspace.next_actions":
        return []
    result = payload.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    raw_actions = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(raw_actions, list):
        return []

    actions: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_actions:
        if not isinstance(item, dict):
            continue
        if item.get("risk") != "read" or item.get("requires_confirmation") is not False:
            continue
        try:
            normalized = workspace_action_handoff_arguments(
                {"action_key": item.get("action_key")}
            )
        except AgentToolError:
            continue
        action_key = normalized["action_key"]
        if action_key in seen:
            continue
        label = _redact_text(item.get("label"), limit=48)
        if not label:
            continue
        seen.add(action_key)
        actions.append({"action_key": action_key, "label": label})
        if len(actions) >= _WORKSPACE_ACTION_LIMIT:
            break
    return actions


def _workspace_next_actions_markup(
    telebot: Any,
    *,
    owner: str,
    actions: list[dict[str, str]],
):
    if not actions:
        return None
    store = get_telegram_agent_action_store()
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for action in actions:
        action_id = store.create_workspace_action(
            owner=owner, action_key=action["action_key"]
        )
        markup.add(
            telebot.types.InlineKeyboardButton(
                action["label"], callback_data=f"aga:{action_id}"
            )
        )
    return markup


def _resource_item_groups(response: Any) -> list[tuple[str, list[Any]]]:
    payload = response if isinstance(response, dict) else {}
    tool_call = payload.get("tool_call")
    tool_name = str(tool_call.get("name") or "") if isinstance(tool_call, dict) else ""
    result = payload.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return []

    if tool_name == "agent.read_plan":
        groups: list[tuple[str, list[Any]]] = []
        steps = data.get("steps")
        if not isinstance(steps, list):
            return []
        for step in steps[:8]:
            if not isinstance(step, dict):
                continue
            step_tool = str(step.get("tool_name") or "").strip()
            step_result = step.get("result")
            if not step_tool or not isinstance(step_result, dict):
                continue
            groups.extend(_resource_item_groups({
                "tool_call": {"name": step_tool},
                "result": step_result,
            }))
        return groups

    if tool_name == "indexer.search_resources":
        items = data.get("items")
        return [("", items)] if isinstance(items, list) else []

    if tool_name == "library.search_missing_episode_resources":
        search = data.get("search")
        verification = data.get("verification")
        if not isinstance(search, dict) or not isinstance(verification, dict):
            return []
        season = verification.get("season")
        episode = verification.get("episode")
        if (
            isinstance(season, bool)
            or not isinstance(season, int)
            or not 1 <= season <= 100
            or isinstance(episode, bool)
            or not isinstance(episode, int)
            or not 1 <= episode <= 1000
        ):
            return []
        items = search.get("items")
        return [(f"S{season:02d}E{episode:02d}", items)] if isinstance(items, list) else []

    if tool_name == "library.search_missing_season_resources":
        groups: list[tuple[str, list[Any]]] = []
        episodes = data.get("episodes")
        if not isinstance(episodes, list):
            return []
        for entry in episodes[:3]:
            if not isinstance(entry, dict):
                continue
            season = entry.get("season")
            episode = entry.get("episode")
            search = entry.get("search")
            if (
                isinstance(season, bool)
                or not isinstance(season, int)
                or not 1 <= season <= 100
                or isinstance(episode, bool)
                or not isinstance(episode, int)
                or not 1 <= episode <= 1000
                or not isinstance(search, dict)
            ):
                continue
            items = search.get("items")
            if isinstance(items, list):
                groups.append((f"S{season:02d}E{episode:02d}", items))
        return groups
    return []


def _resource_candidates(response: Any) -> list[dict[str, str]]:
    """只提取可下载资源的最小安全展示字段。"""
    candidates: list[dict[str, str]] = []
    seen_result_ids: set[str] = set()
    for episode_label, items in _resource_item_groups(response):
        for item in items:
            if not isinstance(item, dict):
                continue
            result_id = str(item.get("result_id") or "").strip()
            state = str(item.get("download_state") or "").strip().lower()
            kinds = item.get("download_kinds")
            safe_kinds = (
                {str(kind or "").strip().lower() for kind in kinds}
                if isinstance(kinds, list)
                else set()
            )
            if (
                result_id in seen_result_ids
                or not _RESULT_ID_RE.fullmatch(result_id)
                or state not in {"ready", "resolvable"}
                or not safe_kinds.intersection({"magnet", "torrent"})
            ):
                continue
            title = _redact_text(item.get("title"), limit=180)
            if not title:
                continue
            seen_result_ids.add(result_id)
            candidates.append(
                {
                    "result_id": result_id,
                    "title": title,
                    "episode": episode_label,
                    "site": _redact_text(
                        item.get("site_name") or item.get("site_id"), limit=60
                    ),
                    "size": _redact_text(item.get("size_text"), limit=32),
                }
            )
            if len(candidates) >= _RESOURCE_RESULT_LIMIT:
                return candidates
    return candidates


def _resource_markup(
    telebot: Any,
    *,
    owner: str,
    candidates: list[dict[str, str]],
):
    if not candidates:
        return None
    store = get_telegram_agent_action_store()
    group_id = secrets.token_urlsafe(12)
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    for position, candidate in enumerate(candidates, start=1):
        qb_id = store.create_resource_prepare(
            owner=owner,
            result_id=candidate["result_id"],
            target="qb",
            group_id=group_id,
        )
        guangya_id = store.create_resource_prepare(
            owner=owner,
            result_id=candidate["result_id"],
            target="guangya",
            group_id=group_id,
        )
        markup.add(
            telebot.types.InlineKeyboardButton(
                f"{position} · qB", callback_data=f"aga:{qb_id}"
            ),
            telebot.types.InlineKeyboardButton(
                f"{position} · 光鸭", callback_data=f"aga:{guangya_id}"
            ),
        )
    return markup


def _render_resource_candidates(
    response: Any, candidates: list[dict[str, str]]
) -> str:
    if not candidates:
        return render_agent_response(response)

    payload = response if isinstance(response, dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    display = payload.get("display") if isinstance(payload.get("display"), dict) else {}
    # 候选标题由下方确定性结构化列表统一展示；这里不采用模型叙述，
    # 避免模型先复述一遍资源、按钮区域再重复一遍。
    summary = _public_multiline_html(
        display.get("summary") or result.get("summary"),
        limit=900,
        promote_first=True,
    )
    lines = [summary or "已找到可下载资源。", "", "<b>候选资源</b>"]
    for position, candidate in enumerate(candidates, start=1):
        if position > 1:
            lines.append("")
        metadata = " · ".join(
            value for value in (candidate["site"], candidate["size"]) if value
        )
        episode = candidate.get("episode")
        prefix = f"{episode} · " if episode else ""
        lines.append(f"<b>{position}.</b> {prefix}{candidate['title']}")
        if metadata:
            lines.append(f"   {metadata}")
    example_position = 2 if len(candidates) > 1 else 1
    lines.extend([
        "",
        f"直接点按钮，或回复“第 {example_position} 个到 qB / 光鸭 / 两边”；"
        "提交前仍会确认。",
    ])
    return _truncate_telegram_html("\n".join(lines))


def handle_agent_message(bot: Any, telebot: Any, message: Any) -> bool:
    """处理普通文本；同一身份只允许最新请求发布最终结果。"""
    chat_id, user_id = _identity(message)
    access = telegram_agent_access(chat_id, user_id)
    if access == "disabled":
        return False
    if access != "allowed":
        bot.reply_to(message, "当前身份未获准使用 Media Agent。")
        return True

    owner = telegram_agent_owner(chat_id, user_id)
    user_message = str(getattr(message, "text", "") or "")
    message_id = getattr(message, "message_id", None)
    identity_material = f"{owner}\0{message_id}\0{user_message.strip()}"
    event_key = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
    if not get_telegram_message_deduplicator().claim(event_key):
        logger.info("忽略重复 Telegram Agent 消息 owner=%s", owner)
        return True

    if not agent_rate_limiter.allow(
        f"{owner}:telegram-agent-query",
        limit=_TELEGRAM_QUERY_LIMIT_PER_MINUTE,
        window_seconds=60,
    ):
        bot.reply_to(message, "请求过于频繁，请稍后重试。")
        return True

    service = get_agent_service()
    coordinator = get_agent_operation_coordinator()

    def initialize_query() -> int | None:
        # 新消息是同一 Telegram 身份的最新意图；旧确认/资源按钮必须与
        # confirmation epoch 在同一个 owner 临界区中同步失效。
        get_telegram_agent_action_store().revoke_owner(owner=owner)
        return begin_query_confirmation_epoch(service, owner=owner)

    operation, confirmation_epoch = coordinator.begin_with_context(
        owner=owner,
        operation_id=f"tg_{event_key[:32]}",
        initialize=initialize_query,
    )
    stream_target = None
    try:
        # Telegram 调用属于不可控外部 I/O，不能占用 owner 生命周期锁；否则
        # 草稿接口卡顿会同时卡住新消息抢占与 /agent_reset。
        allowed, stream_target = _publish_telegram_io_if_current(
            coordinator,
            operation,
            lambda: _begin_agent_stream(bot, telebot, message),
        )
        if not allowed:
            _delete_stale_telegram_delivery(bot, stream_target)
            return True

        conversation_context, history_generation = _telegram_conversation_context(owner)
        _principal, trace_session_id = _telegram_history_identity(owner)
        query_kwargs: dict[str, Any] = {
            "owner": owner,
            "query_tool_rate_identity": owner,
            "llm_tool_rate_identity": owner,
            "request_id": _trace_operation_id(operation),
            "session_id": trace_session_id,
        }
        # 历史上下文同时服务于确定性追问消歧，不能与 LLM 开关耦合。
        if conversation_context:
            query_kwargs["conversation_context"] = conversation_context
        reply_context = _telegram_reply_context(message)
        if reply_context:
            query_kwargs["reply_context"] = reply_context
        if confirmation_epoch is not None:
            query_kwargs["confirmation_owner_generation"] = confirmation_epoch
        if stream_target is not None:
            # 工具选择与参数必须先完整校验；只把最终公开自然语言改成 Provider 流。
            query_kwargs["present"] = False
        response = service.query(user_message, **query_kwargs)
        if not coordinator.is_current(operation):
            raise AgentOperationCancelled("Telegram Agent 操作已失效")

        streamed = False
        if stream_target is not None and isinstance(response, dict):
            source = _stream_answer_source(
                user_message,
                response,
                owner=owner,
            )
            if source is not None:
                answer, emitted, interrupted = run_awaitable_sync(
                    _consume_agent_stream(
                        bot,
                        telebot,
                        stream_target,
                        source,
                        is_current=lambda: coordinator.is_current(operation),
                        publish=lambda callback: _publish_telegram_io_if_current(
                            coordinator, operation, callback
                        ),
                    )
                )
                if interrupted:
                    rendered = (
                        _stream_preview_html(answer, interrupted=True)
                        if answer
                        else "Agent 回答生成中断，请重新发送本次问题。"
                    )

                    def publish_interruption() -> _TelegramPublishResult:
                        result = _persist_agent_stream(
                            bot, telebot, stream_target, rendered
                        )
                        if result.sent:
                            return result
                        return _reply_agent_message(
                            bot, message, rendered, parse_mode="HTML"
                        )

                    allowed, publish_result = _publish_telegram_io_if_current(
                        coordinator, operation, publish_interruption
                    )
                    if not allowed and isinstance(
                        publish_result, _TelegramPublishResult
                    ):
                        _delete_stale_telegram_delivery(
                            bot, publish_result.delivery
                        )
                    if allowed:
                        finalized, _ = coordinator.finalize_if_current(
                            operation, lambda: None
                        )
                        if not finalized and isinstance(
                            publish_result, _TelegramPublishResult
                        ):
                            _delete_stale_telegram_delivery(
                                bot, publish_result.delivery
                            )
                    return True
                if emitted and answer:
                    response = _apply_streamed_answer(response, answer)
                    streamed = True

        def prepare_final_output() -> tuple[str, Any]:
            confirmation = (
                response.get("confirmation")
                if isinstance(response, dict)
                and isinstance(response.get("confirmation"), dict)
                else None
            )
            markup = None
            rendered = render_agent_response(response)
            if response.get("mode") == "confirmation_required" and confirmation:
                confirmation_id = str(confirmation.get("confirmation_id") or "").strip()
                if confirmation_id:
                    contract = sanitize_confirmation_contract(confirmation.get("contract"))
                    markup = _confirmation_markup(
                        telebot,
                        owner=owner,
                        confirmation_id=confirmation_id,
                        action=str(contract.get("action") or ""),
                    )
                    rendered = render_agent_response(response, confirmation=True)
            else:
                candidates = _resource_candidates(response)
                if candidates:
                    markup = _resource_markup(
                        telebot,
                        owner=owner,
                        candidates=candidates,
                    )
                    rendered = _render_resource_candidates(response, candidates)
                else:
                    followups = _episode_resource_followups(response)
                    if followups:
                        markup = _episode_followup_markup(
                            telebot,
                            owner=owner,
                            followups=followups,
                        )
                    else:
                        workspace_actions = _workspace_next_actions(response)
                        if workspace_actions:
                            markup = _workspace_next_actions_markup(
                                telebot,
                                owner=owner,
                                actions=workspace_actions,
                            )
            return rendered, markup

        # 只在 owner 临界区内构造短期 action token；真正的 Telegram 网络调用
        # 放在锁外。若发送期间被新请求取代，发送后的检查会阻止历史落库。
        allowed, prepared_output = coordinator.publish_if_current(
            operation, prepare_final_output
        )
        if not allowed or prepared_output is None:
            return True
        rendered, markup = prepared_output

        def publish_final_message() -> _TelegramPublishResult:
            result = _finish_agent_stream(
                bot,
                telebot,
                stream_target,
                rendered,
                reply_markup=markup,
                show_progress=not streamed,
            )
            if result.sent:
                return result
            return _reply_agent_message(
                bot,
                message,
                rendered,
                reply_markup=markup,
                parse_mode="HTML",
            )

        allowed, publish_result = _publish_telegram_io_if_current(
            coordinator, operation, publish_final_message
        )
        if not allowed and isinstance(publish_result, _TelegramPublishResult):
            _delete_stale_telegram_delivery(bot, publish_result.delivery)
        if allowed:
            finalized, _ = coordinator.finalize_if_current(
                operation,
                lambda: _record_telegram_conversation(
                    owner,
                    message=user_message,
                    response=response,
                    generation=history_generation,
                ),
            )
            if not finalized and isinstance(
                publish_result, _TelegramPublishResult
            ):
                _delete_stale_telegram_delivery(bot, publish_result.delivery)
    except AgentOperationCancelled:
        _delete_stale_telegram_delivery(bot, stream_target)
        logger.info("Telegram Agent 旧请求已停止发布 owner=%s", owner)
    except AgentToolError as exc:
        logger.info("Telegram Agent 请求被拒绝 code=%s", exc.code)
        rendered = "Agent 无法处理该请求，请调整问题后重试。"

        def publish_tool_error() -> _TelegramPublishResult:
            result = _finish_agent_stream(
                bot, telebot, stream_target, rendered
            )
            if result.sent:
                return result
            return _reply_agent_message(bot, message, rendered)

        allowed, publish_result = _publish_telegram_io_if_current(
            coordinator, operation, publish_tool_error
        )
        if not allowed and isinstance(publish_result, _TelegramPublishResult):
            _delete_stale_telegram_delivery(bot, publish_result.delivery)
        if allowed:
            finalized, _ = coordinator.finalize_if_current(operation, lambda: None)
            if not finalized and isinstance(
                publish_result, _TelegramPublishResult
            ):
                _delete_stale_telegram_delivery(bot, publish_result.delivery)
    except Exception as exc:
        logger.warning("Telegram Agent 请求失败 type=%s", type(exc).__name__)
        rendered = "Agent 暂时不可用，请稍后重试。"

        def publish_error() -> _TelegramPublishResult:
            result = _finish_agent_stream(
                bot, telebot, stream_target, rendered
            )
            if result.sent:
                return result
            return _reply_agent_message(bot, message, rendered)

        allowed, publish_result = _publish_telegram_io_if_current(
            coordinator, operation, publish_error
        )
        if not allowed and isinstance(publish_result, _TelegramPublishResult):
            _delete_stale_telegram_delivery(bot, publish_result.delivery)
        if allowed:
            finalized, _ = coordinator.finalize_if_current(operation, lambda: None)
            if not finalized and isinstance(
                publish_result, _TelegramPublishResult
            ):
                _delete_stale_telegram_delivery(bot, publish_result.delivery)
    finally:
        coordinator.finish(operation)
    return True


_PATROL_CALLBACK_QUERIES = {
    "agp:summary": "查看最近全库巡检结果",
    "agp:resources": "把刚才巡检发现的缺集找资源",
}


def _agent_result_ok(response: Any) -> bool:
    result = response.get("result") if isinstance(response, dict) else None
    return isinstance(result, dict) and bool(result.get("ok"))


def _query_patrol_action(
    service: Any, action: str, *, owner: str, request_id: str = "",
    session_id: str = "",
) -> Any:
    """固定只读巡检动作；资源接力前先刷新同一 owner 的安全快照。"""
    query_kwargs = {
        "owner": owner,
        "query_tool_rate_identity": owner,
        "llm_tool_rate_identity": owner,
        "request_id": request_id,
        "session_id": session_id,
    }
    summary = service.query(_PATROL_CALLBACK_QUERIES["agp:summary"], **query_kwargs)
    if action == "agp:summary" or not _agent_result_ok(summary):
        return summary
    return service.query(_PATROL_CALLBACK_QUERIES[action], **query_kwargs)


def handle_agent_patrol_callback(
    bot: Any,
    call: Any,
    telebot_module: Any = None,
) -> None:
    """处理通知上的固定巡检动作；只读、身份绑定、严格白名单并共享回调限流。"""
    chat_id, user_id = _identity(call)
    callback_answered = False
    coordinator = get_agent_operation_coordinator()
    operation = None
    if telegram_agent_access(chat_id, user_id) != "allowed":
        bot.answer_callback_query(call.id, "操作已过期或无效", show_alert=True)
        return
    owner = telegram_agent_owner(chat_id, user_id)
    try:
        action = str(getattr(call, "data", "") or "").strip()
        if action not in _PATROL_CALLBACK_QUERIES:
            raise ValueError("操作已过期或无效")
        if not agent_rate_limiter.allow(
            f"{owner}:telegram-agent-callback",
            limit=_TELEGRAM_CALLBACK_LIMIT_PER_MINUTE,
            window_seconds=60,
        ):
            bot.answer_callback_query(
                call.id, "请求过于频繁，请稍后重试", show_alert=True
            )
            return

        operation = coordinator.begin(
            owner=owner,
            operation_id=_telegram_callback_operation_id(
                owner, call, action=action
            ),
        )
        bot.answer_callback_query(call.id, "正在查询，请稍候")
        callback_answered = True
        history_generation = _telegram_history_generation(owner)
        _principal, trace_session_id = _telegram_history_identity(owner)
        response = _query_patrol_action(
            get_agent_service(),
            action,
            owner=owner,
            request_id=_trace_operation_id(operation),
            session_id=trace_session_id,
        )

        def prepare_output() -> tuple[str, Any | None]:
            candidates = _resource_candidates(response)
            if not candidates:
                return render_agent_response(response), None
            module = telebot_module
            if module is None:
                import telebot as module
            return (
                _render_resource_candidates(response, candidates),
                _resource_markup(
                    module,
                    owner=owner,
                    candidates=candidates,
                ),
            )

        _publish_telegram_callback_response(
            bot,
            call.message,
            coordinator=coordinator,
            operation=operation,
            owner=owner,
            response=response,
            history_generation=history_generation,
            history_message=_PATROL_CALLBACK_QUERIES[action],
            fallback_summary=(
                "已完成缺集资源检查"
                if action == "agp:resources"
                else "已完成全库巡检结果检查"
            ),
            prepare_output=prepare_output,
        )
    except (AgentToolError, ValueError):
        if operation is not None and not coordinator.is_current(operation):
            if not callback_answered:
                bot.answer_callback_query(
                    call.id, "操作已过期或无效", show_alert=True
                )
            return
        if callback_answered:
            bot.reply_to(
                call.message,
                "<b>无法继续查询</b>\n操作已过期或当前条件不满足，请重新发起。",
                parse_mode="HTML",
            )
        else:
            bot.answer_callback_query(call.id, "操作已过期或无效", show_alert=True)
    except Exception as exc:
        logger.warning("Telegram Agent 巡检动作失败 type=%s", type(exc).__name__)
        if operation is not None and not coordinator.is_current(operation):
            return
        if callback_answered:
            bot.reply_to(
                call.message,
                "<b>查询没有完成</b>\n服务暂时不可用，请稍后重试。",
                parse_mode="HTML",
            )
        else:
            bot.answer_callback_query(call.id, "Agent 暂时不可用", show_alert=True)
    finally:
        if operation is not None:
            coordinator.finish(operation)


def handle_agent_callback(bot: Any, call: Any, telebot_module: Any = None) -> None:
    chat_id, user_id = _identity(call)
    callback_answered = False
    confirmed_action_completed = False
    coordinator = get_agent_operation_coordinator()
    operation = None
    if telegram_agent_access(chat_id, user_id) != "allowed":
        bot.answer_callback_query(call.id, "操作已过期或无效", show_alert=True)
        return
    owner = telegram_agent_owner(chat_id, user_id)
    try:
        prefix, action_id = str(getattr(call, "data", "") or "").split(":", 1)
        if prefix != "aga":
            raise ValueError("操作已过期或无效")
        store = get_telegram_agent_action_store()
        action_metadata = store.inspect(action_id, owner=owner)
        if not agent_rate_limiter.allow(
            f"{owner}:telegram-agent-callback",
            limit=_TELEGRAM_CALLBACK_LIMIT_PER_MINUTE,
            window_seconds=60,
        ):
            bot.answer_callback_query(
                call.id, "请求过于频繁，请稍后重试", show_alert=True
            )
            return
        if (
            action_metadata["action"] == "invoke_read_tool"
            and not allow_agent_tool(owner, action_metadata["tool_name"])
        ):
            bot.answer_callback_query(
                call.id, "请求过于频繁，请稍后重试", show_alert=True
            )
            return

        history_generation = _telegram_history_generation(owner)
        service = get_agent_service()
        action_kind = action_metadata["action"]

        if action_kind in {"cancel", "prepare_resource", "confirm"}:
            # Telegram 网络调用永远放在 owner 临界区外；只有一次性 action 的
            # 消费、确认票据操作、受控写入与历史落库参与线性化。
            if action_kind == "confirm":
                bot.answer_callback_query(call.id, "正在执行，请稍候")
                callback_answered = True
            elif action_kind == "prepare_resource":
                bot.answer_callback_query(call.id, "正在准备，请稍候")
                callback_answered = True

            prepare_error: AgentToolError | None = None
            with get_agent_operation_coordinator().owner_window(owner):
                action = store.resolve(action_id, owner=owner)
                if action["action"] == "cancel":
                    service.discard_confirmation(
                        action["confirmation_id"],
                        owner=owner,
                    )
                    response = {
                        "mode": "conversation",
                        "result": {
                            "ok": True,
                            "status": "cancelled",
                            "summary": "操作已取消，未执行任何写入。",
                            "suggestions": [],
                            "evidence": [],
                        },
                    }
                    text = "<b>操作已取消</b>\n未执行任何写入。"
                    markup = None
                    history_message = "取消待处理操作"
                    fallback_summary = "操作已取消，未执行任何写入。"
                elif action["action"] == "prepare_resource":
                    try:
                        response = service.prepare(
                            "indexer.submit_resource",
                            {
                                "result_id": action["result_id"],
                                "target": action["target"],
                            },
                            owner=owner,
                            request_id=_trace_operation_id(operation),
                            session_id=_telegram_history_identity(owner)[1],
                        )
                    except AgentToolError as exc:
                        prepare_error = exc
                    if prepare_error is None:
                        confirmation = (
                            response.get("confirmation")
                            if isinstance(response, dict)
                            and isinstance(response.get("confirmation"), dict)
                            else None
                        )
                        confirmation_id = (
                            str(confirmation.get("confirmation_id") or "").strip()
                            if confirmation
                            else ""
                        )
                        if (
                            response.get("mode") != "confirmation_required"
                            or not confirmation_id
                        ):
                            raise ValueError("资源预检未返回确认票据")
                        if telebot_module is None:
                            import telebot as telebot_module
                        markup = _confirmation_markup(
                            telebot_module,
                            owner=owner,
                            confirmation_id=confirmation_id,
                            action=str(
                                sanitize_confirmation_contract(
                                    confirmation.get("contract")
                                ).get("action")
                                or ""
                            ),
                        )
                        text = render_agent_response(response, confirmation=True)
                        history_message = "准备提交所选资源"
                        fallback_summary = "资源提交已完成预检，等待确认。"
                else:
                    response = service.confirm(
                        action["confirmation_id"],
                        owner=owner,
                        request_id=_trace_operation_id(operation),
                        session_id=_telegram_history_identity(owner)[1],
                    )
                    confirmed_action_completed = True
                    text = render_agent_response(response)
                    markup = None
                    history_message = "确认执行待处理操作"
                    fallback_summary = "待处理操作已执行。"

                if prepare_error is None:
                    _record_telegram_callback_conversation(
                        owner,
                        message=history_message,
                        response=response,
                        generation=history_generation,
                        fallback_summary=fallback_summary,
                    )

            if prepare_error is not None:
                logger.info("Telegram 资源预检被拒绝 code=%s", prepare_error.code)
                bot.edit_message_text(
                    "<b>无法准备资源提交</b>\n"
                    "资源可能已过期，或所选下载目标尚未就绪。请重新搜索后再试。",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="HTML",
                    reply_markup=None,
                )
                return

            delivered = deliver_terminal_to_existing_message(
                bot,
                telebot_module,
                call.message,
                text,
                label="Agent 操作结果",
                reply_markup=markup,
                runtime_retry=True,
            )
            if not delivered:
                logger.warning("Telegram Agent 终态待恢复投递")
            if not callback_answered:
                bot.answer_callback_query(call.id, "已处理")
            return

        if action_kind == "invoke_workspace_action":
            resolution = resolve_workspace_action_handoff(
                {"action_key": action_metadata["action_key"]}
            )
            # inspect 仅用于限流元数据，不能据此抢占当前操作。一次性 action
            # 的领取与 callback lease 的创建必须处于同一 owner 窗口：若新消息
            # 已撤销旧按钮，claim 会先失败，失效 callback 不会反向取消新请求。
            with coordinator.owner_window(owner):
                action = store.claim_workspace_action(action_id, owner=owner)
                try:
                    allowed = allow_agent_tool(
                        owner, str(resolution["target_tool"])
                    )
                except Exception:
                    store.restore_workspace_action(
                        action_id,
                        owner=owner,
                        action_key=action["action_key"],
                        expires_at=action["expires_at"],
                    )
                    raise
                if not allowed:
                    store.restore_workspace_action(
                        action_id,
                        owner=owner,
                        action_key=action["action_key"],
                        expires_at=action["expires_at"],
                    )
                else:
                    operation = coordinator.begin(
                        owner=owner,
                        operation_id=_telegram_callback_operation_id(
                            owner, call, action=action_kind
                        ),
                    )
            if not allowed:
                bot.answer_callback_query(
                    call.id, "请求过于频繁，请稍后重试", show_alert=True
                )
                return
            bot.answer_callback_query(call.id, "正在执行，请稍候")
            callback_answered = True
        else:
            # resolve 与 begin 同样必须线性化，避免按钮在 inspect 后被新消息
            # 撤销，而旧 callback 随后仍创建 lease 并抢占新消息。
            with coordinator.owner_window(owner):
                action = store.resolve(action_id, owner=owner)
                operation = coordinator.begin(
                    owner=owner,
                    operation_id=_telegram_callback_operation_id(
                        owner, call, action=action_kind
                    ),
                )

        if action["action"] == "invoke_read_tool":
            bot.answer_callback_query(call.id, "正在查询，请稍候")
            callback_answered = True
            response = service.invoke(
                action["tool_name"],
                action["arguments"],
                owner=owner,
                request_id=_trace_operation_id(operation),
                session_id=_telegram_history_identity(owner)[1],
            )
            label = public_tool_label(action["tool_name"])

            def prepare_read_output() -> tuple[str, Any | None]:
                candidates = _resource_candidates(response)
                if not candidates:
                    return render_agent_response(response), None
                module = telebot_module
                if module is None:
                    import telebot as module
                return (
                    _render_resource_candidates(response, candidates),
                    _resource_markup(
                        module,
                        owner=owner,
                        candidates=candidates,
                    ),
                )

            _publish_telegram_callback_response(
                bot,
                call.message,
                coordinator=coordinator,
                operation=operation,
                owner=owner,
                response=response,
                history_generation=history_generation,
                history_message=f"查看{label}",
                fallback_summary=f"已完成{label}",
                prepare_output=prepare_read_output,
            )
            return
        if action["action"] == "invoke_workspace_action":
            response = service.invoke_workspace_action(
                action["action_key"],
                owner=owner,
                rate_identity="",
                request_id=_trace_operation_id(operation),
                session_id=_telegram_history_identity(owner)[1],
            )
            label = sanitize_public_text(resolution.get("label"), limit=120) or "建议检查"
            _publish_telegram_callback_response(
                bot,
                call.message,
                coordinator=coordinator,
                operation=operation,
                owner=owner,
                response=response,
                history_generation=history_generation,
                history_message=label,
                fallback_summary=f"已完成{label}",
                prepare_output=lambda: (render_agent_response(response), None),
            )
            return
        raise ValueError("操作已过期或无效")
    except (AgentToolError, ValueError):
        if operation is not None and not coordinator.is_current(operation):
            if not callback_answered:
                bot.answer_callback_query(
                    call.id, "操作已过期或无效", show_alert=True
                )
            return
        if callback_answered:
            try:
                bot.edit_message_text(
                    "<b>执行失败</b>\n操作已过期或当前条件不满足，请重新发起。",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "操作已过期或无效", show_alert=True)
    except Exception as exc:
        logger.warning("Telegram Agent 确认失败 type=%s", type(exc).__name__)
        if operation is not None and not coordinator.is_current(operation):
            if not callback_answered:
                bot.answer_callback_query(
                    call.id, "Agent 暂时不可用", show_alert=True
                )
            return
        if callback_answered:
            if confirmed_action_completed:
                delivered = deliver_terminal_to_existing_message(
                    bot,
                    telebot_module,
                    call.message,
                    "<b>操作已执行</b>\n结果通知暂未送达，请在操作记录中核对最终状态。",
                    label="Agent 操作结果",
                    runtime_retry=True,
                )
                if not delivered:
                    logger.warning("Telegram Agent 已执行终态待恢复投递")
            else:
                try:
                    bot.edit_message_text(
                        "<b>执行失败</b>\n服务暂时不可用，请稍后重试。",
                        call.message.chat.id,
                        call.message.message_id,
                        parse_mode="HTML",
                        reply_markup=None,
                    )
                except Exception:
                    pass
        else:
            bot.answer_callback_query(call.id, "Agent 暂时不可用", show_alert=True)
    finally:
        if operation is not None:
            coordinator.finish(operation)
