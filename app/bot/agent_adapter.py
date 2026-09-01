"""Telegram 与 Media Agent 的最小安全适配层。

该模块只投影 Agent 的公开摘要、建议，以及严格白名单化的资源标题元数据；
不把其余工具 ``data``、异常、确认票据或用户凭据发送到 Telegram。所有写操作
仍由 Agent 自身确认门控制，Telegram callback 只携带短期 opaque action id。
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import hmac
import html
import json
import math
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Callable

from app import database as db
from app.agent.action_plan import sanitize_action_plan
from app.agent.action_plan_id import normalize_action_plan_id
from app.agent.async_bridge import run_awaitable_sync
from app.agent.conversation_compaction import schedule_conversation_compaction
from app.agent.conversation_history import get_agent_conversation_history_repository
from app.agent.confirmation import confirmation_reply_intent
from app.agent.feature_gate import (
    AgentRuntimeDisabled,
    agent_runtime_admission,
    agent_runtime_generation_is_current,
    agent_runtime_transition,
    current_agent_runtime_generation,
    invalidate_agent_runtime_generation,
    is_agent_enabled,
)
from app.agent.media_case import media_case_stage_for_tool
from app.agent.llm_router import (
    begin_llm_request_budget,
    reset_llm_request_budget,
    stream_existing_answer,
    stream_tool_answer,
)
from app.agent.operation_coordinator import (
    AgentOperationCancelled,
    get_agent_operation_coordinator,
    get_telegram_message_deduplicator,
)
from app.agent.query_lifecycle import (
    begin_query_confirmation_epoch,
    invalidate_query_confirmation_epoch,
)
from app.agent.presentation_stream import (
    PublicNarrativeProjector,
    PublicNarrativeValidationError,
    apply_streamed_answer,
    select_agent_answer_stream,
)
from app.agent.progress_events import (
    AgentProgressEvent,
    bind_agent_progress_listener,
)
from app.agent.rate_limit import agent_rate_limiter, allow_agent_tool
from app.agent.registry import AgentToolError
from app.agent.response_contract import build_response_contract, response_contract
from app.agent.result_projection import (
    attach_public_fallback_presentation,
    project_agent_result_for_user,
    project_public_guidance,
    project_public_notices,
    public_tool_label,
    replace_internal_identifiers,
    sanitize_public_multiline_text,
    sanitize_public_text,
)
from app.agent.service import get_agent_service
from app.agent.state_commit import (
    AgentStateCommitBuffer,
    defer_agent_state_commits,
)
from app.agent.workspace_next_actions import (
    resolve_workspace_action_handoff,
    workspace_action_handoff_arguments,
)
from app.bot.progress import (
    _normalized_message_thread_id,
    deliver_terminal_to_existing_message,
    send_typing,
)
from app.config import get
from app.logger import get_logger
from app.modules.web_secret import get_web_secret
from app.notifier import (
    TelegramSendResult,
    call_telegram_delivery,
    telegram_edit_fallback_allowed,
)

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
    "library.count_series_episodes": "统计本地集数",
    "agent.action_history": "操作历史",
}
_MAX_SUMMARY_LENGTH = 900
_MAX_NARRATIVE_LENGTH = 1000
_MAX_SUGGESTION_LENGTH = 280
_MAX_TRACE_ITEMS = 5
_TELEGRAM_QUERY_LIMIT_PER_MINUTE = 12
_TELEGRAM_CALLBACK_LIMIT_PER_MINUTE = 12
_RESOURCE_PAGE_SIZE = 3
_RESOURCE_RESULT_LIMIT = 9
_RESOURCE_PAGE_PAYLOAD_LIMIT = 32768
_EPISODE_FOLLOWUP_LIMIT = 3
_WORKSPACE_ACTION_LIMIT = 5
_TELEGRAM_STREAM_UPDATE_INTERVAL_SECONDS = 0.35
_TELEGRAM_PROGRESS_UPDATE_INTERVAL_SECONDS = 0.45
_TELEGRAM_PROGRESS_MAX_COMPLETED_STEPS = 4
_TELEGRAM_TYPING_INTERVAL_SECONDS = 4.0
_TELEGRAM_TYPING_TIMEOUT_SECONDS = 120.0
_TELEGRAM_TYPING_REGISTRY_LOCK = threading.RLock()
_TELEGRAM_TYPING_REGISTRY: dict[str, Any] = {}

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
    transport: TelegramSendResult
    delivery: _StreamMessage | None = None
    fallback_allowed: bool = False

    @property
    def sent(self) -> bool:
        return self.transport.ok

    @property
    def outcome_unknown(self) -> bool:
        return self.transport.outcome_unknown


@dataclass(frozen=True)
class _AgentAction:
    action_id: str
    owner: str
    action: str
    expires_at: float
    group_id: str
    plan_id: str = ""
    result_id: str = ""
    target: str = ""
    tool_name: str = ""
    arguments_json: str = ""
    action_key: str = ""


class _TelegramTypingHeartbeat:
    """在同步 Agent 查询期间续期 Telegram typing，不依赖流式草稿开关。"""

    def __init__(
        self,
        bot: Any,
        chat_id: object,
        *,
        is_current: Callable[[], bool],
        message_thread_id: int | None = None,
        interval_seconds: float = _TELEGRAM_TYPING_INTERVAL_SECONDS,
        timeout_seconds: float = _TELEGRAM_TYPING_TIMEOUT_SECONDS,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.is_current = is_current
        self.message_thread_id = _normalized_message_thread_id(message_thread_id)
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.timeout_seconds = max(self.interval_seconds, float(timeout_seconds))
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread: threading.Thread | None = None
        self._waiting_for_handoff = False
        self._registry_key = f"{chat_id}:{self.message_thread_id or 0}"

    def start(self) -> "_TelegramTypingHeartbeat":
        if self.chat_id is None or not callable(getattr(self.bot, "send_chat_action", None)):
            return self
        if not self._still_current():
            return self
        thread: threading.Thread | None = None
        with _TELEGRAM_TYPING_REGISTRY_LOCK:
            existing = _TELEGRAM_TYPING_REGISTRY.get(self._registry_key)
            if isinstance(existing, _TelegramTypingHeartbeat) and existing is not self:
                existing._stop.set()
                existing_thread = existing._thread
                if (
                    (existing_thread is not None and existing_thread.is_alive())
                    or existing._waiting_for_handoff
                ):
                    # 同一 chat/topic 最多保留一个外部 typing I/O。旧调用若阻塞，
                    # 仅登记最新 successor；旧 worker 退出时再把执行权交给它。
                    self._waiting_for_handoff = True
                    _TELEGRAM_TYPING_REGISTRY[self._registry_key] = self
                    return self
            thread = self._new_thread()
            self._thread = thread
            _TELEGRAM_TYPING_REGISTRY[self._registry_key] = self
        self._launch_thread(thread)
        # 只等待后台 worker 获得调度，不等待 Telegram 外部 I/O 返回。
        self._started.wait(timeout=0.05)
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        with _TELEGRAM_TYPING_REGISTRY_LOCK:
            if (
                _TELEGRAM_TYPING_REGISTRY.get(self._registry_key) is self
                and (thread is None or not thread.is_alive())
            ):
                _TELEGRAM_TYPING_REGISTRY.pop(self._registry_key, None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.1)

    def _new_thread(self) -> threading.Thread:
        return threading.Thread(
            target=self._run,
            name="mediaflux-agent-telegram-typing",
            daemon=True,
        )

    def _launch_thread(self, thread: threading.Thread) -> None:
        try:
            thread.start()
        except RuntimeError:
            with _TELEGRAM_TYPING_REGISTRY_LOCK:
                if _TELEGRAM_TYPING_REGISTRY.get(self._registry_key) is self:
                    _TELEGRAM_TYPING_REGISTRY.pop(self._registry_key, None)
            self._thread = None

    def _still_current(self) -> bool:
        try:
            return bool(self.is_current())
        except Exception:
            return False

    def _run(self) -> None:
        try:
            self._started.set()
            if self._stop.is_set() or not self._still_current():
                return
            send_typing(
                self.bot, self.chat_id, message_thread_id=self.message_thread_id
            )
            deadline = time.monotonic() + self.timeout_seconds
            while not self._stop.wait(self.interval_seconds):
                if time.monotonic() >= deadline or not self._still_current():
                    return
                send_typing(
                    self.bot, self.chat_id, message_thread_id=self.message_thread_id
                )
        finally:
            self._unregister()

    def _unregister(self) -> None:
        successor: _TelegramTypingHeartbeat | None = None
        successor_thread: threading.Thread | None = None
        with _TELEGRAM_TYPING_REGISTRY_LOCK:
            current = _TELEGRAM_TYPING_REGISTRY.get(self._registry_key)
            if current is self:
                _TELEGRAM_TYPING_REGISTRY.pop(self._registry_key, None)
            elif (
                isinstance(current, _TelegramTypingHeartbeat)
                and current._thread is None
                and not current._stop.is_set()
                and current._still_current()
            ):
                successor = current
                successor_thread = current._new_thread()
                current._waiting_for_handoff = False
                current._thread = successor_thread
        if successor is not None and successor_thread is not None:
            successor._launch_thread(successor_thread)


def _normalize_resource_page_payload(
    candidates: Any, page: Any, *, strict_result_ids: bool = True
) -> dict[str, Any]:
    """校验分页 callback 中的最小公开候选快照。"""
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("无法创建 Telegram 资源分页")
    if len(candidates) > _RESOURCE_RESULT_LIMIT:
        raise ValueError("无法创建 Telegram 资源分页")
    if isinstance(page, bool):
        raise ValueError("无法创建 Telegram 资源分页")
    try:
        page_number = int(page)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("无法创建 Telegram 资源分页") from exc

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("无法创建 Telegram 资源分页")
        result_id = str(candidate.get("result_id") or "").strip()
        title = _redact_text(
            html.unescape(str(candidate.get("title") or "")), limit=180
        )
        if (
            (strict_result_ids and not _RESULT_ID_RE.fullmatch(result_id))
            or (not strict_result_ids and not result_id)
            or result_id in seen
            or not title
        ):
            raise ValueError("无法创建 Telegram 资源分页")
        seen.add(result_id)
        normalized.append(
            {
                "result_id": result_id,
                "title": title,
                "episode": _redact_text(
                    html.unescape(str(candidate.get("episode") or "")), limit=20
                ),
                "site": _redact_text(
                    html.unescape(str(candidate.get("site") or "")), limit=60
                ),
                "size": _redact_text(
                    html.unescape(str(candidate.get("size") or "")), limit=32
                ),
                "explanation": _redact_text(
                    html.unescape(str(candidate.get("explanation") or "")), limit=180
                ),
            }
        )

    total_pages = (len(normalized) + _RESOURCE_PAGE_SIZE - 1) // _RESOURCE_PAGE_SIZE
    if page_number < 0 or page_number >= total_pages:
        raise ValueError("无法创建 Telegram 资源分页")
    return {"page": page_number, "candidates": normalized}


def _resource_interaction_layout(
    candidates: Any, page: Any
) -> dict[str, Any]:
    """构造单张资源卡的规范化页面与导航快照。"""
    payload = _normalize_resource_page_payload(candidates, page)
    all_candidates = payload["candidates"]
    page_number = payload["page"]
    total_pages = (
        len(all_candidates) + _RESOURCE_PAGE_SIZE - 1
    ) // _RESOURCE_PAGE_SIZE
    page_start = page_number * _RESOURCE_PAGE_SIZE
    page_candidates = all_candidates[page_start : page_start + _RESOURCE_PAGE_SIZE]
    navigation_pages: list[tuple[str, int]] = []
    if page_number > 0:
        navigation_pages.append(("previous", page_number - 1))
    if page_number + 1 < total_pages:
        navigation_pages.append(("next", page_number + 1))

    navigation_payloads: dict[str, str] = {}
    for direction, target_page in navigation_pages:
        arguments_json = json.dumps(
            {"page": target_page, "candidates": all_candidates},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(arguments_json) > _RESOURCE_PAGE_PAYLOAD_LIMIT:
            raise ValueError("Telegram 资源分页参数过长")
        navigation_payloads[direction] = arguments_json

    return {
        "page": page_number,
        "total_pages": total_pages,
        "page_start": page_start,
        "page_candidates": page_candidates,
        "navigation_payloads": navigation_payloads,
        "action_count": len(page_candidates) * 2 + len(navigation_payloads),
    }


_TELEGRAM_ACTION_KINDS = frozenset({
    "confirm",
    "cancel",
    "prepare_resource",
    "paginate_resources",
    "invoke_read_tool",
    "invoke_workspace_action",
})
_MISSING_EPISODE_READ_TOOL = "library.search_missing_episode_resources"


def _reject_non_finite_action_json(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _load_action_json_object(value: Any) -> dict[str, Any]:
    """严格读取 callback JSON；禁止宽松常量、非对象和损坏 Unicode。"""
    if not isinstance(value, str) or not value:
        raise ValueError("操作已过期或无效")
    try:
        payload = json.loads(
            value, parse_constant=_reject_non_finite_action_json
        )
        if not isinstance(payload, dict):
            raise ValueError("操作已过期或无效")
        json.dumps(
            payload, ensure_ascii=False, allow_nan=False
        ).encode("utf-8", errors="strict")
    except (
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeEncodeError,
    ) as exc:
        raise ValueError("操作已过期或无效") from exc
    return payload


def _active_action_expiry(value: Any, *, now: float) -> float:
    try:
        expiry = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("操作已过期或无效") from exc
    if not math.isfinite(expiry) or expiry <= now:
        raise ValueError("操作已过期或无效")
    return expiry


def _action_metadata(
    *,
    action: Any,
    plan_id: Any = "",
    result_id: Any = "",
    target: Any = "",
    tool_name: Any = "",
    action_key: Any = "",
) -> dict[str, Any]:
    """统一验证内存与 SQLite callback 元数据。"""
    action_kind = str(action or "")
    if action_kind not in _TELEGRAM_ACTION_KINDS:
        raise ValueError("操作已过期或无效")

    metadata: dict[str, Any] = {
        "action": action_kind,
        "tool_name": "",
        "action_key": "",
    }
    if action_kind in {"confirm", "cancel"}:
        ticket = normalize_action_plan_id(plan_id)
        if not ticket:
            raise ValueError("操作已过期或无效")
        metadata["plan_id"] = ticket
    elif action_kind == "prepare_resource":
        resource_id = str(result_id or "")
        target_name = str(target or "")
        if (
            not _RESULT_ID_RE.fullmatch(resource_id)
            or target_name not in {"qb", "guangya"}
        ):
            raise ValueError("操作已过期或无效")
        metadata.update({"result_id": resource_id, "target": target_name})
    elif action_kind == "invoke_read_tool":
        tool = str(tool_name or "")
        if tool != _MISSING_EPISODE_READ_TOOL:
            raise ValueError("操作已过期或无效")
        metadata["tool_name"] = tool
    elif action_kind == "invoke_workspace_action":
        try:
            normalized = workspace_action_handoff_arguments(
                {"action_key": str(action_key or "")}
            )
        except AgentToolError as exc:
            raise ValueError("操作已过期或无效") from exc
        metadata["action_key"] = normalized["action_key"]
    return metadata


def _resolved_action_payload(
    *, metadata: dict[str, Any], arguments_json: Any = ""
) -> dict[str, Any]:
    """统一构造一次性 callback 的公开执行负载。"""
    action = metadata["action"]
    if action == "prepare_resource":
        arguments = _load_action_json_object(arguments_json)
        position = arguments.get("position")
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 1 <= position <= _RESOURCE_RESULT_LIMIT
        ):
            raise ValueError("操作已过期或无效")
        return {
            "action": action,
            "result_id": metadata["result_id"],
            "position": position,
            "target": metadata["target"],
        }
    if action == "invoke_read_tool":
        arguments = _load_action_json_object(arguments_json)
        from app.agent.episode_resource_actions import (
            missing_episode_resource_arguments,
        )

        try:
            normalized = missing_episode_resource_arguments(arguments)
        except AgentToolError as exc:
            raise ValueError("操作已过期或无效") from exc
        return {
            "action": action,
            "tool_name": metadata["tool_name"],
            "arguments": normalized,
        }
    if action == "paginate_resources":
        payload = _load_action_json_object(arguments_json)
        normalized = _normalize_resource_page_payload(
            payload.get("candidates"), payload.get("page")
        )
        return {"action": action, **normalized}
    if action == "invoke_workspace_action":
        return {"action": action, "action_key": metadata["action_key"]}
    return {"plan_id": metadata["plan_id"], "action": action}


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

    def _trim_capacity_locked(self) -> None:
        """按完整交互组淘汰，避免只留下执行或取消中的单个按钮。"""
        while len(self._items) > self._max_entries:
            oldest = next(iter(self._items.values()), None)
            if oldest is None:
                return
            sibling_ids = [
                action_id
                for action_id, candidate in self._items.items()
                if candidate.owner == oldest.owner
                and candidate.group_id == oldest.group_id
            ]
            if not sibling_ids:
                self._items.pop(oldest.action_id, None)
                continue
            for action_id in sibling_ids:
                self._items.pop(action_id, None)

    def _remove_group_locked(self, item: _AgentAction) -> None:
        for action_id in [
            candidate_id
            for candidate_id, candidate in self._items.items()
            if candidate.owner == item.owner
            and candidate.group_id == item.group_id
        ]:
            self._items.pop(action_id, None)

    def _store_locked(self, item: _AgentAction) -> str:
        if not item.action_id or ":" in item.action_id:
            raise RuntimeError("无法生成 Telegram Agent 操作标识")
        self._items[item.action_id] = item
        self._trim_capacity_locked()
        return item.action_id

    def _new_action_ids_locked(self, count: int) -> tuple[str, ...]:
        reserved: list[str] = []
        for _ in range(max(1, int(count))):
            for _attempt in range(8):
                action_id = str(self._token_factory() or "").strip()
                if (
                    action_id
                    and ":" not in action_id
                    and action_id not in self._items
                    and action_id not in reserved
                ):
                    reserved.append(action_id)
                    break
            else:
                raise RuntimeError("无法生成 Telegram Agent 操作标识")
        return tuple(reserved)

    def _new_action_id_locked(self) -> str:
        return self._new_action_ids_locked(1)[0]

    def _create_single_action(
        self,
        *,
        owner: str,
        action: str,
        group_prefix: str,
        tool_name: str = "",
        arguments_json: str = "{}",
        action_key: str = "",
    ) -> str:
        """原子创建单按钮操作；不同存储后端只实现这一持久化边界。"""
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            action_id = self._new_action_id_locked()
            return self._store_locked(
                _AgentAction(
                    action_id=action_id,
                    owner=owner,
                    action=action,
                    expires_at=now + self._ttl_seconds,
                    group_id=f"{group_prefix}:{action_id}",
                    tool_name=tool_name,
                    arguments_json=arguments_json,
                    action_key=action_key,
                )
            )

    def create_confirmation_pair(
        self, *, owner: str, plan_id: str
    ) -> tuple[str, str]:
        """原子创建同一行动计划的执行/取消 callback。"""
        owner_key = str(owner or "").strip()
        ticket = normalize_action_plan_id(plan_id)
        if not owner_key or not ticket:
            raise ValueError("无法创建 Telegram Agent 操作")
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            confirm_id, cancel_id = self._new_action_ids_locked(2)
            group_id = f"confirmation:{ticket}"
            expires_at = now + self._ttl_seconds
            self._items.update({
                confirm_id: _AgentAction(
                    action_id=confirm_id,
                    owner=owner_key,
                    action="confirm",
                    expires_at=expires_at,
                    group_id=group_id,
                    plan_id=ticket,
                ),
                cancel_id: _AgentAction(
                    action_id=cancel_id,
                    owner=owner_key,
                    action="cancel",
                    expires_at=expires_at,
                    group_id=group_id,
                    plan_id=ticket,
                ),
            })
            self._trim_capacity_locked()
            return confirm_id, cancel_id

    def create_resource_interaction(
        self,
        *,
        owner: str,
        candidates: list[dict[str, Any]],
        page: int = 0,
    ) -> dict[str, Any]:
        """原子创建单张资源卡的预检与分页 callback 组。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise ValueError("无法创建 Telegram 资源操作")
        layout = _resource_interaction_layout(candidates, page)
        action_count = int(layout["action_count"])
        if action_count <= 0 or action_count > self._max_entries:
            raise RuntimeError("Telegram 资源操作组超出容量")

        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            action_ids = iter(self._new_action_ids_locked(action_count))
            group_id = f"resource:{secrets.token_urlsafe(12)}"
            expires_at = now + self._ttl_seconds
            items: dict[str, _AgentAction] = {}
            result_items: list[dict[str, Any]] = []
            for offset, candidate in enumerate(layout["page_candidates"]):
                position = int(layout["page_start"]) + offset + 1
                qb_id = next(action_ids)
                guangya_id = next(action_ids)
                arguments_json = json.dumps(
                    {
                        "position": position,
                        "result_id": candidate["result_id"],
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                for action_id, target in ((qb_id, "qb"), (guangya_id, "guangya")):
                    items[action_id] = _AgentAction(
                        action_id=action_id,
                        owner=owner_key,
                        action="prepare_resource",
                        expires_at=expires_at,
                        group_id=group_id,
                        result_id=candidate["result_id"],
                        target=target,
                        arguments_json=arguments_json,
                    )
                result_items.append({
                    "position": position,
                    "qb_action_id": qb_id,
                    "guangya_action_id": guangya_id,
                })

            navigation_ids = {"previous": "", "next": ""}
            for direction, arguments_json in layout["navigation_payloads"].items():
                action_id = next(action_ids)
                navigation_ids[direction] = action_id
                items[action_id] = _AgentAction(
                    action_id=action_id,
                    owner=owner_key,
                    action="paginate_resources",
                    expires_at=expires_at,
                    group_id=group_id,
                    arguments_json=arguments_json,
                )

            self._items.update(items)
            self._trim_capacity_locked()
            guard_action_id = next(iter(items), "")
            if not guard_action_id or guard_action_id not in self._items:
                raise RuntimeError("无法创建 Telegram 资源操作组")
            return {
                "page": layout["page"],
                "total_pages": layout["total_pages"],
                "items": tuple(result_items),
                "previous_action_id": navigation_ids["previous"],
                "next_action_id": navigation_ids["next"],
                "guard_action_id": guard_action_id,
            }

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
        if not owner_key or tool != _MISSING_EPISODE_READ_TOOL:
            raise ValueError("无法创建 Telegram 只读操作")

        from app.agent.episode_resource_actions import (
            missing_episode_resource_arguments,
        )

        try:
            normalized = missing_episode_resource_arguments(arguments)
        except AgentToolError as exc:
            raise ValueError("无法创建 Telegram 只读操作") from exc
        arguments_json = json.dumps(
            normalized,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(arguments_json) > 2048:
            raise ValueError("Telegram 只读操作参数过长")
        return self._create_single_action(
            owner=owner_key,
            action="invoke_read_tool",
            group_prefix="read",
            tool_name=tool,
            arguments_json=arguments_json,
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
        return self._create_single_action(
            owner=owner_key,
            action="invoke_workspace_action",
            group_prefix="workspace",
            action_key=normalized["action_key"],
        )

    def claim_workspace_action(
        self, action_id: str, *, owner: str
    ) -> dict[str, Any]:
        """原子领取工作区行动，确保并发 callback 只会占用一次目标额度。"""
        key = str(action_id or "").strip()
        owner_key = str(owner or "").strip()
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            item = self._items.get(key)
            if (
                item is None
                or item.action != "invoke_workspace_action"
                or not secrets.compare_digest(item.owner, owner_key)
            ):
                raise ValueError("操作已过期或无效")
            try:
                expiry = _active_action_expiry(item.expires_at, now=now)
                metadata = _action_metadata(
                    action=item.action, action_key=item.action_key
                )
            except ValueError:
                self._remove_group_locked(item)
                raise
            self._items.pop(key, None)
            return {
                "action": metadata["action"],
                "action_key": metadata["action_key"],
                "expires_at": expiry,
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
        if not math.isfinite(expiry):
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

    def inspect(self, action_id: str, *, owner: str) -> dict[str, Any]:
        """返回执行准备所需的最小元数据，但不消费一次性票据。"""
        key = str(action_id or "").strip()
        owner_key = str(owner or "").strip()
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            item = self._items.get(key)
            if item is None or not secrets.compare_digest(item.owner, owner_key):
                raise ValueError("操作已过期或无效")
            try:
                _active_action_expiry(item.expires_at, now=now)
                metadata = _action_metadata(
                    action=item.action,
                    plan_id=item.plan_id,
                    result_id=item.result_id,
                    target=item.target,
                    tool_name=item.tool_name,
                    action_key=item.action_key,
                )
                if metadata["action"] == "prepare_resource":
                    return _resolved_action_payload(
                        metadata=metadata, arguments_json=item.arguments_json
                    )
                return metadata
            except ValueError:
                self._remove_group_locked(item)
                raise

    def resolve(self, action_id: str, *, owner: str) -> dict[str, Any]:
        key = str(action_id or "").strip()
        owner_key = str(owner or "").strip()
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            item = self._items.get(key)
            if item is None or not secrets.compare_digest(item.owner, owner_key):
                raise ValueError("操作已过期或无效")
            # 任一按钮被合法所有者使用后，同一交互组的其他按钮同步失效。
            self._remove_group_locked(item)
            _active_action_expiry(item.expires_at, now=now)
            metadata = _action_metadata(
                action=item.action,
                plan_id=item.plan_id,
                result_id=item.result_id,
                target=item.target,
                tool_name=item.tool_name,
                action_key=item.action_key,
            )
            return _resolved_action_payload(
                metadata=metadata, arguments_json=item.arguments_json
            )

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


class SQLiteTelegramAgentActionStore(TelegramAgentActionStore):
    """跨 worker 共享的 Telegram Agent opaque callback 仓库。"""

    _ACTIONS = _TELEGRAM_ACTION_KINDS

    def __init__(
        self,
        *,
        ttl_seconds: float = _ACTION_TTL_SECONDS,
        max_entries: int = _ACTION_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18),
    ) -> None:
        super().__init__(
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
            clock=clock,
            token_factory=token_factory,
        )

    @staticmethod
    def _ensure_schema(conn: Any) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS telegram_agent_actions("
            "action_id TEXT PRIMARY KEY,owner_digest TEXT NOT NULL,"
            "action_kind TEXT NOT NULL,group_id TEXT NOT NULL,"
            "confirmation_id TEXT NOT NULL DEFAULT '',"
            "target TEXT NOT NULL DEFAULT '',"
            "tool_name TEXT NOT NULL DEFAULT '',"
            "arguments_json TEXT NOT NULL DEFAULT '{}',"
            "action_key TEXT NOT NULL DEFAULT '',expires_at REAL NOT NULL,"
            "created_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegram_agent_actions_owner_group "
            "ON telegram_agent_actions(owner_digest,group_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegram_agent_actions_owner_expiry "
            "ON telegram_agent_actions(owner_digest,expires_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegram_agent_actions_expiry "
            "ON telegram_agent_actions(expires_at)"
        )

    @staticmethod
    def _owner_digest(owner: str) -> str:
        return hmac.new(
            get_web_secret().encode("utf-8"),
            b"mediaflux-telegram-agent-action:v1\0" + owner.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _prune(conn: Any, now: float) -> int:
        cursor = conn.execute(
            "DELETE FROM telegram_agent_actions WHERE expires_at<=?", (now,)
        )
        return max(0, int(cursor.rowcount or 0))

    def _new_action_ids(
        self, conn: Any, *, count: int
    ) -> tuple[str, ...]:
        reserved: list[str] = []
        for _ in range(max(1, int(count))):
            for _attempt in range(8):
                action_id = str(self._token_factory() or "").strip()
                if (
                    action_id
                    and ":" not in action_id
                    and action_id not in reserved
                    and conn.execute(
                        "SELECT 1 FROM telegram_agent_actions WHERE action_id=?",
                        (action_id,),
                    ).fetchone() is None
                ):
                    reserved.append(action_id)
                    break
            else:
                raise RuntimeError("无法生成 Telegram Agent 操作标识")
        return tuple(reserved)

    def _trim_capacity(self, conn: Any) -> None:
        while int(conn.execute(
            "SELECT COUNT(*) FROM telegram_agent_actions"
        ).fetchone()[0] or 0) > self._max_entries:
            oldest = conn.execute(
                "SELECT owner_digest,group_id FROM telegram_agent_actions "
                "ORDER BY expires_at ASC,rowid ASC LIMIT 1"
            ).fetchone()
            if oldest is None:
                break
            conn.execute(
                "DELETE FROM telegram_agent_actions "
                "WHERE owner_digest=? AND group_id=?",
                (oldest["owner_digest"], oldest["group_id"]),
            )

    def _create_single_action(
        self,
        *,
        owner: str,
        action: str,
        group_prefix: str,
        tool_name: str = "",
        arguments_json: str = "{}",
        action_key: str = "",
    ) -> str:
        if action not in {"invoke_read_tool", "invoke_workspace_action"}:
            raise RuntimeError("无法生成 Telegram Agent 操作标识")
        with self._lock, db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            now = self._clock()
            self._prune(conn, now)
            action_id = self._new_action_ids(conn, count=1)[0]
            try:
                conn.execute(
                    "INSERT INTO telegram_agent_actions("
                    "action_id,owner_digest,action_kind,group_id,confirmation_id,"
                    "target,tool_name,arguments_json,action_key,expires_at,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        action_id,
                        self._owner_digest(owner),
                        action,
                        f"{group_prefix}:{action_id}",
                        "",
                        "",
                        tool_name,
                        arguments_json,
                        action_key,
                        now + self._ttl_seconds,
                        db.now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("无法生成 Telegram Agent 操作标识") from exc
            self._trim_capacity(conn)
            if conn.execute(
                "SELECT 1 FROM telegram_agent_actions WHERE action_id=?",
                (action_id,),
            ).fetchone() is None:
                raise RuntimeError("无法生成 Telegram Agent 操作标识")
            return action_id

    def create_confirmation_pair(
        self, *, owner: str, plan_id: str
    ) -> tuple[str, str]:
        owner_key = str(owner or "").strip()
        ticket = normalize_action_plan_id(plan_id)
        if not owner_key or not ticket:
            raise ValueError("无法创建 Telegram Agent 操作")
        with self._lock, db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            now = self._clock()
            self._prune(conn, now)
            confirm_id, cancel_id = self._new_action_ids(conn, count=2)
            owner_digest = self._owner_digest(owner_key)
            group_id = f"confirmation:{ticket}"
            expires_at = now + self._ttl_seconds
            created_at = db.now()
            conn.executemany(
                "INSERT INTO telegram_agent_actions("
                "action_id,owner_digest,action_kind,group_id,confirmation_id,"
                "target,tool_name,arguments_json,action_key,expires_at,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    (
                        confirm_id, owner_digest, "confirm", group_id, ticket,
                        "", "", "{}", "", expires_at, created_at,
                    ),
                    (
                        cancel_id, owner_digest, "cancel", group_id, ticket,
                        "", "", "{}", "", expires_at, created_at,
                    ),
                ),
            )
            self._trim_capacity(conn)
            return confirm_id, cancel_id

    def create_resource_interaction(
        self,
        *,
        owner: str,
        candidates: list[dict[str, Any]],
        page: int = 0,
    ) -> dict[str, Any]:
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise ValueError("无法创建 Telegram 资源操作")
        layout = _resource_interaction_layout(candidates, page)
        action_count = int(layout["action_count"])
        if action_count <= 0 or action_count > self._max_entries:
            raise RuntimeError("Telegram 资源操作组超出容量")

        with self._lock, db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            now = self._clock()
            self._prune(conn, now)
            action_ids = iter(self._new_action_ids(conn, count=action_count))
            owner_digest = self._owner_digest(owner_key)
            group_id = f"resource:{secrets.token_urlsafe(12)}"
            expires_at = now + self._ttl_seconds
            created_at = db.now()
            rows: list[tuple[Any, ...]] = []
            result_items: list[dict[str, Any]] = []
            for offset, candidate in enumerate(layout["page_candidates"]):
                position = int(layout["page_start"]) + offset + 1
                qb_id = next(action_ids)
                guangya_id = next(action_ids)
                arguments_json = json.dumps(
                    {
                        "position": position,
                        "result_id": candidate["result_id"],
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                for action_id, target in ((qb_id, "qb"), (guangya_id, "guangya")):
                    rows.append((
                        action_id,
                        owner_digest,
                        "prepare_resource",
                        group_id,
                        "",
                        target,
                        "",
                        arguments_json,
                        "",
                        expires_at,
                        created_at,
                    ))
                result_items.append({
                    "position": position,
                    "qb_action_id": qb_id,
                    "guangya_action_id": guangya_id,
                })

            navigation_ids = {"previous": "", "next": ""}
            for direction, arguments_json in layout["navigation_payloads"].items():
                action_id = next(action_ids)
                navigation_ids[direction] = action_id
                rows.append((
                    action_id,
                    owner_digest,
                    "paginate_resources",
                    group_id,
                    "",
                    "",
                    "",
                    arguments_json,
                    "",
                    expires_at,
                    created_at,
                ))

            try:
                conn.executemany(
                    "INSERT INTO telegram_agent_actions("
                    "action_id,owner_digest,action_kind,group_id,confirmation_id,"
                    "target,tool_name,arguments_json,action_key,expires_at,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("无法创建 Telegram 资源操作组") from exc
            self._trim_capacity(conn)
            guard_action_id = str(rows[0][0]) if rows else ""
            if not guard_action_id or conn.execute(
                "SELECT 1 FROM telegram_agent_actions WHERE action_id=?",
                (guard_action_id,),
            ).fetchone() is None:
                raise RuntimeError("无法创建 Telegram 资源操作组")
            return {
                "page": layout["page"],
                "total_pages": layout["total_pages"],
                "items": tuple(result_items),
                "previous_action_id": navigation_ids["previous"],
                "next_action_id": navigation_ids["next"],
                "guard_action_id": guard_action_id,
            }

    def _row_for_owner(
        self, conn: Any, *, action_id: str, owner: str
    ) -> Any | None:
        return conn.execute(
            "SELECT action_id,action_kind,group_id,confirmation_id AS plan_id,target,"
            "tool_name,arguments_json,action_key,expires_at FROM telegram_agent_actions "
            "WHERE action_id=? AND owner_digest=?",
            (action_id, self._owner_digest(owner)),
        ).fetchone()

    @staticmethod
    def _inspect_row(row: Any) -> dict[str, Any]:
        result_id = ""
        if str(row["action_kind"] or "") == "prepare_resource":
            arguments = _load_action_json_object(row["arguments_json"])
            result_id = str(arguments.get("result_id") or "")
        return _action_metadata(
            action=row["action_kind"],
            plan_id=row["plan_id"],
            result_id=result_id,
            target=row["target"],
            tool_name=row["tool_name"],
            action_key=row["action_key"],
        )

    @staticmethod
    def _delete_row_group(conn: Any, *, row: Any, owner_digest: str) -> int:
        group_id = str(row["group_id"] or "")
        if group_id:
            cursor = conn.execute(
                "DELETE FROM telegram_agent_actions "
                "WHERE owner_digest=? AND group_id=?",
                (owner_digest, group_id),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM telegram_agent_actions "
                "WHERE owner_digest=? AND action_id=?",
                (owner_digest, str(row["action_id"] or "")),
            )
        return max(0, int(cursor.rowcount or 0))

    def inspect(self, action_id: str, *, owner: str) -> dict[str, Any]:
        key = str(action_id or "").strip()
        owner_key = str(owner or "").strip()
        if not key or not owner_key:
            raise ValueError("操作已过期或无效")
        owner_digest = self._owner_digest(owner_key)
        invalid = False
        metadata: dict[str, Any] | None = None
        with self._lock, db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            now = self._clock()
            self._prune(conn, now)
            row = self._row_for_owner(conn, action_id=key, owner=owner_key)
            if row is None:
                raise ValueError("操作已过期或无效")
            try:
                _active_action_expiry(row["expires_at"], now=now)
                metadata = self._inspect_row(row)
                if metadata["action"] == "prepare_resource":
                    metadata = _resolved_action_payload(
                        metadata=metadata,
                        arguments_json=row["arguments_json"],
                    )
            except (TypeError, ValueError, OverflowError):
                self._delete_row_group(
                    conn, row=row, owner_digest=owner_digest
                )
                invalid = True
        if invalid or metadata is None:
            raise ValueError("操作已过期或无效")
        return metadata

    def claim_workspace_action(
        self, action_id: str, *, owner: str
    ) -> dict[str, Any]:
        key = str(action_id or "").strip()
        owner_key = str(owner or "").strip()
        if not key or not owner_key:
            raise ValueError("操作已过期或无效")
        owner_digest = self._owner_digest(owner_key)
        invalid = False
        claimed: dict[str, Any] | None = None
        with self._lock, db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            now = self._clock()
            self._prune(conn, now)
            row = self._row_for_owner(conn, action_id=key, owner=owner_key)
            if row is None:
                raise ValueError("操作已过期或无效")
            try:
                expiry = _active_action_expiry(row["expires_at"], now=now)
                metadata = self._inspect_row(row)
            except (TypeError, ValueError, OverflowError):
                self._delete_row_group(
                    conn, row=row, owner_digest=owner_digest
                )
                invalid = True
            else:
                if metadata["action"] != "invoke_workspace_action":
                    raise ValueError("操作已过期或无效")
                deleted = conn.execute(
                    "DELETE FROM telegram_agent_actions "
                    "WHERE action_id=? AND owner_digest=?",
                    (key, owner_digest),
                )
                if deleted.rowcount != 1:
                    raise ValueError("操作已过期或无效")
                claimed = {
                    "action": metadata["action"],
                    "action_key": metadata["action_key"],
                    "expires_at": expiry,
                }
        if invalid or claimed is None:
            raise ValueError("操作已过期或无效")
        return claimed

    def restore_workspace_action(
        self,
        action_id: str,
        *,
        owner: str,
        action_key: str,
        expires_at: float,
    ) -> bool:
        key = str(action_id or "").strip()
        owner_key = str(owner or "").strip()
        if not key or ":" in key or not owner_key:
            return False
        try:
            normalized = workspace_action_handoff_arguments({"action_key": action_key})
            expiry = float(expires_at)
        except (AgentToolError, TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(expiry):
            return False
        with self._lock, db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            now = self._clock()
            self._prune(conn, now)
            if expiry <= now or conn.execute(
                "SELECT 1 FROM telegram_agent_actions WHERE action_id=?", (key,)
            ).fetchone() is not None:
                return False
            conn.execute(
                "INSERT INTO telegram_agent_actions("
                "action_id,owner_digest,action_kind,group_id,action_key,expires_at,created_at"
                ") VALUES(?,?, 'invoke_workspace_action',?,?,?,?)",
                (
                    key,
                    self._owner_digest(owner_key),
                    f"workspace:{key}",
                    normalized["action_key"],
                    expiry,
                    db.now(),
                ),
            )
            return True

    def resolve(self, action_id: str, *, owner: str) -> dict[str, Any]:
        key = str(action_id or "").strip()
        owner_key = str(owner or "").strip()
        if not key or not owner_key:
            raise ValueError("操作已过期或无效")
        owner_digest = self._owner_digest(owner_key)
        invalid = False
        resolved: dict[str, Any] | None = None
        with self._lock, db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            now = self._clock()
            self._prune(conn, now)
            row = self._row_for_owner(conn, action_id=key, owner=owner_key)
            if row is None:
                raise ValueError("操作已过期或无效")
            try:
                _active_action_expiry(row["expires_at"], now=now)
                metadata = self._inspect_row(row)
                resolved = _resolved_action_payload(
                    metadata=metadata, arguments_json=row["arguments_json"]
                )
            except (TypeError, ValueError, OverflowError):
                invalid = True
            deleted = self._delete_row_group(
                conn, row=row, owner_digest=owner_digest
            )
            if deleted < 1:
                raise ValueError("操作已过期或无效")
        if invalid or resolved is None:
            raise ValueError("操作已过期或无效")
        return resolved

    def revoke_owner(self, *, owner: str) -> int:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return 0
        with self._lock, db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            self._prune(conn, self._clock())
            cursor = conn.execute(
                "DELETE FROM telegram_agent_actions WHERE owner_digest=?",
                (self._owner_digest(owner_key),),
            )
            return max(0, int(cursor.rowcount or 0))


_action_store: TelegramAgentActionStore = SQLiteTelegramAgentActionStore()


def get_telegram_agent_action_store() -> TelegramAgentActionStore:
    return _action_store


def _enabled(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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


def telegram_agent_control_access(chat_id: object, user_id: object) -> str:
    """校验 Agent 控制面板身份，不受 Agent 开关状态影响。"""
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


def telegram_agent_access(chat_id: object, user_id: object) -> str:
    """返回 ``disabled``、``unauthorized`` 或 ``allowed``，默认拒绝。"""
    if not is_agent_enabled():
        return "disabled"
    if not _enabled(get("TG_AGENT_ENABLED", "0")):
        return "disabled"
    return telegram_agent_control_access(chat_id, user_id)


def telegram_agent_owner(chat_id: object, user_id: object) -> str:
    chat = str(chat_id or "").strip()
    user = str(user_id or "").strip()
    if not _ALLOWED_ID_RE.fullmatch(chat) or not _ALLOWED_ID_RE.fullmatch(user):
        raise ValueError("Telegram Agent 身份无效")
    return f"tg:v1:{chat}\x1f{user}"


def _telegram_runtime_admission(*, expected_generation: int | None = None):
    """复用 Telegram 入口的统一运行态准入，避免各 callback 自行解释开关。"""
    return agent_runtime_admission(
        require_telegram=True,
        expected_generation=expected_generation,
        agent_enabled_check=is_agent_enabled,
        telegram_enabled_check=lambda: _enabled(get("TG_AGENT_ENABLED", "0")),
    )


def _remove_callback_keyboard(bot: Any, message: Any) -> bool:
    """尽快撤下已消费或过期的 inline keyboard；失败不影响主流程。"""
    edit_markup = getattr(bot, "edit_message_reply_markup", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    if not callable(edit_markup) or chat_id is None or message_id is None:
        return False
    try:
        edit_markup(chat_id, message_id, reply_markup=None)
        return True
    except Exception as exc:
        logger.info(
            "Telegram Agent keyboard 清理失败 type=%s", type(exc).__name__
        )
        return False


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
        (
            source_message_id
            if isinstance(source_message_id, int) and not isinstance(source_message_id, bool)
            else None
        ),
        _normalized_message_thread_id(message_thread_id),
    )


def _rich_message(telebot: Any, html_text: str) -> Any | None:
    input_rich_message = getattr(getattr(telebot, "types", None), "InputRichMessage", None)
    if not callable(input_rich_message):
        return None
    try:
        return input_rich_message(html=_telegram_rich_html(html_text))
    except Exception:
        return None


_RICH_STANDALONE_HEADING_RE = re.compile(r"^<b>(.+)</b>$", re.DOTALL)
_RICH_BULLET_RE = re.compile(r"^\s*•\s+(.+)$", re.DOTALL)
_RICH_BLOCK_RE = re.compile(
    r"^\s*</?(?:p|h[1-6]|ul|ol|li|blockquote|pre|tg-thinking)\b",
    re.IGNORECASE,
)


def _telegram_rich_html(value: object) -> str:
    """把 sendMessage 风格换行转换为 Rich Message 的块级 HTML。

    ``InputRichMessage.html`` 按 HTML 规则处理空白，普通 ``\n`` 不会自动成为
    Telegram 段落。若直接把旧的 sendMessage HTML 传进去，标题、列表和资源候选
    会在客户端挤成一行。这里仅处理本模块已经转义和白名单化的受控 HTML。
    """
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if _RICH_BLOCK_RE.match(text):
        return text

    blocks: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        lines = [line.strip() for line in paragraph if line.strip()]
        paragraph.clear()
        if not lines:
            return
        if len(lines) == 1:
            heading = _RICH_STANDALONE_HEADING_RE.fullmatch(lines[0])
            if heading:
                blocks.append(f"<h3>{heading.group(1)}</h3>")
                return
        blocks.append(f"<p>{'<br>'.join(lines)}</p>")

    def flush_bullets() -> None:
        if not bullets:
            return
        blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in bullets) + "</ul>")
        bullets.clear()

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_bullets()
            continue
        bullet = _RICH_BULLET_RE.fullmatch(line)
        if bullet:
            flush_paragraph()
            bullets.append(bullet.group(1).strip())
            continue
        flush_bullets()
        paragraph.append(line)

    flush_paragraph()
    flush_bullets()
    return "".join(blocks)


def _telegram_progress_request(message: Any) -> tuple[str, str]:
    """返回可放入临时进度卡的用户名和请求摘要。"""

    user = getattr(message, "from_user", None)
    name_parts = [
        str(getattr(user, field, "") or "").strip()
        for field in ("first_name", "last_name")
    ]
    display_name = " ".join(part for part in name_parts if part)
    if not display_name:
        username = str(getattr(user, "username", "") or "").strip()
        display_name = f"@{username}" if username else "你的请求"
    query = str(getattr(message, "text", "") or "").strip()
    return (
        _public_text(display_name, limit=48) or "你的请求",
        _public_text(query, limit=180) or "正在处理本次请求",
    )


def _render_agent_progress_card(
    message: Any,
    *,
    target_mode: str,
    active_label: str,
    completed_steps: tuple[str, ...] = (),
    elapsed_seconds: float = 0.0,
) -> str:
    """渲染不暴露参数、路径和内部工具名的 Telegram 工作进度卡。"""

    display_name, query = _telegram_progress_request(message)
    active = _public_text(active_label, limit=100) or "正在处理…"
    completed = tuple(
        text
        for raw in completed_steps[-_TELEGRAM_PROGRESS_MAX_COMPLETED_STEPS :]
        if (text := _public_text(raw, limit=100))
    )
    elapsed = max(0.0, float(elapsed_seconds or 0.0))
    elapsed_text = (
        f"已用时 {elapsed:.1f} 秒" if elapsed < 10 else f"已用时 {int(elapsed)} 秒"
    )
    # Telegram 草稿接口没有 reply_parameters；富/文本草稿内模拟引用卡。
    # 可编辑占位本身已经 reply_to 原消息，因此不重复展示请求。
    include_request = target_mode in {"rich_draft", "draft"}

    if target_mode == "rich_draft":
        blocks: list[str] = []
        if include_request:
            blocks.append(
                f"<blockquote><b>{display_name}</b><br>{query}</blockquote>"
            )
        if completed:
            blocks.append(f"<p>{'<br>'.join(completed)}</p>")
        blocks.append(f"<tg-thinking>{active}</tg-thinking>")
        blocks.append(f"<p><i>· {elapsed_text}</i></p>")
        return "".join(blocks)

    lines: list[str] = []
    if include_request:
        lines.extend(
            [f"<blockquote><b>{display_name}</b><br>{query}</blockquote>", ""]
        )
    else:
        lines.extend(["<b>Media Agent</b>", ""])
    lines.extend(completed)
    if completed:
        lines.append("")
    lines.append(f"<i>{active}</i>")
    lines.append(f"<i>· {elapsed_text}</i>")
    return "\n".join(lines)


class _TelegramAgentProgress:
    """把真实 Agent 阶段投影为同一条 Telegram 草稿中的公开进度。"""

    def __init__(
        self,
        bot: Any,
        telebot: Any,
        target: _StreamMessage | None,
        message: Any,
        *,
        publish: Callable[[Callable[[], Any]], tuple[bool, Any | None]],
        clock: Callable[[], float] = time.monotonic,
        update_interval_seconds: float = _TELEGRAM_PROGRESS_UPDATE_INTERVAL_SECONDS,
    ) -> None:
        self.bot = bot
        self.telebot = telebot
        self.target = target if isinstance(target, _StreamMessage) else None
        self.message = message
        self.publish = publish
        self.clock = clock
        self.update_interval_seconds = max(0.0, float(update_interval_seconds))
        self._active = self.target is not None
        self._active_label = "◌ 正在识别请求范围…"
        self._completed_steps: list[str] = []
        self._active_tools: dict[str, dict[str, Any]] = {}
        self._started_at = self.clock()
        self._last_rendered = (
            _render_agent_progress_card(
                message,
                target_mode=self.target.mode,
                active_label=self._active_label,
                elapsed_seconds=0.0,
            )
            if self.target is not None
            else ""
        )
        self._last_update_at = self._started_at
        self._lock = threading.RLock()

    def handle(self, event: AgentProgressEvent) -> None:
        """接收真实进度事件；只显示公开工具名称，不显示参数和原始摘要。"""

        phase = str(getattr(event, "phase", "") or "").strip()
        tool_name = str(getattr(event, "tool_name", "") or "").strip()
        force = False
        with self._lock:
            if not self._active or self.target is None:
                return
            if phase == "routing":
                self._active_label = "◌ 正在识别请求范围…"
            elif phase == "planning":
                self._remember_completed("✓ 已识别请求范围")
                self._active_label = "◌ 正在选择需要核对的数据源…"
                force = True
            elif phase == "model_wait":
                self._remember_completed("✓ 已识别请求范围")
                self._active_label = "◌ 正在规划检查步骤…"
                force = True
            elif phase == "synthesizing":
                self._active_tools.clear()
                self._active_label = "◌ 正在整理检查结论…"
                force = True
            elif phase in {"tool_start", "preview_start"} and tool_name:
                label = public_tool_label(tool_name)
                active = self._active_tools.setdefault(
                    tool_name, {"label": label, "count": 0, "failed": False}
                )
                active["count"] = int(active.get("count") or 0) + 1
                if phase == "preview_start":
                    self._active_label = f"◌ 正在生成安全预检：{label}…"
                else:
                    self._set_active_tool_label()
                force = True
            elif phase in {"tool_finish", "preview_finish"} and tool_name:
                label = public_tool_label(tool_name)
                ok = getattr(event, "ok", None) is not False
                active = self._active_tools.get(tool_name)
                finished = True
                failed = not ok
                if active is not None:
                    active["failed"] = bool(active.get("failed")) or not ok
                    remaining_count = max(
                        0, int(active.get("count") or 0) - 1
                    )
                    active["count"] = remaining_count
                    failed = bool(active.get("failed"))
                    finished = remaining_count == 0
                    if finished:
                        self._active_tools.pop(tool_name, None)
                if finished:
                    self._remember_completed(
                        f"! {label}未正常返回" if failed else f"✓ {label}"
                    )
                if self._active_tools:
                    self._set_active_tool_label()
                else:
                    self._active_label = (
                        "◌ 正在核对预检结果…"
                        if phase == "preview_finish"
                        else "◌ 正在分析检查结果…"
                    )
            else:
                return
        self._publish(force=force)

    def _set_active_tool_label(self) -> None:
        active_count = sum(
            max(0, int(item.get("count") or 0))
            for item in self._active_tools.values()
        )
        if active_count <= 0:
            self._active_label = "◌ 正在分析检查结果…"
            return
        if active_count == 1:
            remaining = next(
                str(item.get("label") or "MediaFlux 检查")
                for item in self._active_tools.values()
                if int(item.get("count") or 0) > 0
            )
            self._active_label = f"◌ 正在检查：{remaining}…"
            return
        self._active_label = f"◌ 正在并行核对 {active_count} 项信息…"

    def mark_answering(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._active_tools.clear()
            self._active_label = "◌ 正在整理回答…"
        self._publish(force=True)

    def stop(self) -> None:
        with self._lock:
            self._active = False

    def _remember_completed(self, value: str) -> None:
        label = str(value or "").strip()
        if not label:
            return
        normalized = label.removeprefix("✓ ").removeprefix("! ")
        self._completed_steps = [
            item
            for item in self._completed_steps
            if item.removeprefix("✓ ").removeprefix("! ") != normalized
        ]
        self._completed_steps.append(label)
        if len(self._completed_steps) > _TELEGRAM_PROGRESS_MAX_COMPLETED_STEPS:
            self._completed_steps = self._completed_steps[
                -_TELEGRAM_PROGRESS_MAX_COMPLETED_STEPS :
            ]

    def _publish(self, *, force: bool) -> None:
        with self._lock:
            target = self.target
            if not self._active or target is None:
                return
            now = self.clock()
            if (
                not force
                and now - self._last_update_at < self.update_interval_seconds
            ):
                return
            rendered = _render_agent_progress_card(
                self.message,
                target_mode=target.mode,
                active_label=self._active_label,
                completed_steps=tuple(self._completed_steps),
                elapsed_seconds=max(0.0, now - self._started_at),
            )
            if rendered == self._last_rendered:
                return
            self._last_rendered = rendered
            self._last_update_at = now

        try:
            allowed, _result = self.publish(
                lambda: _update_agent_stream(
                    self.bot,
                    self.telebot,
                    target,
                    rendered,
                    reply_markup=None,
                )
            )
        except Exception:
            allowed = False
        if not allowed:
            self.stop()


def _begin_agent_stream(
    bot: Any, telebot: Any, message: Any
) -> _StreamMessage | None:
    """优先启动 Telegram 原生草稿流；不支持时降级为可编辑消息。"""
    if not _telegram_streaming_enabled():
        return None
    chat_id, source_message_id, message_thread_id = _message_context(message)
    if chat_id is None:
        return None

    draft_id = secrets.randbelow(2_147_483_647) + 1
    send_rich_draft = getattr(bot, "send_rich_message_draft", None)
    send_rich = getattr(bot, "send_rich_message", None)
    thinking = _rich_message(
        telebot,
        _render_agent_progress_card(
            message,
            target_mode="rich_draft",
            active_label="◌ 正在理解请求…",
        ),
    )
    if callable(send_rich_draft) and callable(send_rich) and thinking is not None:
        try:
            draft_kwargs: dict[str, Any] = {}
            if message_thread_id is not None:
                draft_kwargs["message_thread_id"] = message_thread_id
            if send_rich_draft(chat_id, draft_id, thinking, **draft_kwargs):
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
            draft_kwargs = {}
            if message_thread_id is not None:
                draft_kwargs["message_thread_id"] = message_thread_id
            if send_draft(
                chat_id,
                draft_id,
                _render_agent_progress_card(
                    message,
                    target_mode="draft",
                    active_label="◌ 正在理解请求…",
                ),
                parse_mode="HTML",
                **draft_kwargs,
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
            _render_agent_progress_card(
                message,
                target_mode="edit",
                active_label="◌ 正在理解请求…",
            ),
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
            draft_kwargs: dict[str, Any] = {}
            if target.message_thread_id is not None:
                draft_kwargs["message_thread_id"] = target.message_thread_id
            return bool(
                bot.send_rich_message_draft(
                    target.chat_id, target.draft_id, rich, **draft_kwargs
                )
            )
        if target.mode == "draft":
            if target.draft_id is None:
                return False
            draft_kwargs = {"parse_mode": parse_mode}
            if target.message_thread_id is not None:
                draft_kwargs["message_thread_id"] = target.message_thread_id
            return bool(
                bot.send_message_draft(
                    target.chat_id, target.draft_id, text, **draft_kwargs
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
    if edit_fallback and target.message_id is not None:
        kwargs: dict[str, Any] = {
            "reply_markup": reply_markup,
            "disable_web_page_preview": True,
            "parse_mode": "HTML",
        }
        edit_result, _value = call_telegram_delivery(
            lambda: bot.edit_message_text(
                rendered,
                target.chat_id,
                target.message_id,
                **kwargs,
            ),
            message_id=int(target.message_id),
            edit=True,
        )
        if edit_result.ok:
            return _TelegramPublishResult(
                transport=edit_result, delivery=target,
            )
        if not telegram_edit_fallback_allowed(edit_result):
            logger.info(
                "Telegram Agent 终态编辑未安全降级 status=%s error=%s",
                edit_result.status_code or "-", edit_result.error,
            )
            return _TelegramPublishResult(transport=edit_result)
        logger.info(
            "Telegram Agent 终态被明确拒绝编辑，改发新消息 error=%s",
            edit_result.error,
        )

    if target.mode == "rich_draft":
        send_rich = getattr(bot, "send_rich_message", None)
        rich = _rich_message(telebot, rendered)
        if callable(send_rich) and rich is not None:
            kwargs = {"reply_markup": reply_markup}
            if target.message_thread_id is not None:
                kwargs["message_thread_id"] = target.message_thread_id
            reply_parameters = _rich_reply_parameters(
                telebot, target.source_message_id
            )
            if reply_parameters is not None:
                kwargs["reply_parameters"] = reply_parameters
            rich_result, message = call_telegram_delivery(
                lambda: send_rich(target.chat_id, rich, **kwargs)
            )
            if rich_result.ok:
                return _TelegramPublishResult(
                    transport=rich_result,
                    delivery=_delivery_from_message(
                        message, fallback_chat_id=target.chat_id
                    ),
                )
            if rich_result.outcome_unknown:
                logger.info(
                    "Telegram Agent 富消息固化结果未知，停止连续发送 error=%s",
                    rich_result.error,
                )
                return _TelegramPublishResult(transport=rich_result)
            logger.info(
                "Telegram Agent 富消息固化明确失败，降级普通消息 status=%s",
                rich_result.status_code or "-",
            )

    send_message = getattr(bot, "send_message", None)
    if not callable(send_message):
        return _TelegramPublishResult(
            transport=TelegramSendResult(
                ok=False, error="TelegramSenderUnavailable", status_code=503,
            ),
            fallback_allowed=True,
        )
    kwargs = {
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup,
    }
    if target.source_message_id is not None:
        kwargs["reply_to_message_id"] = target.source_message_id
    if target.message_thread_id is not None:
        kwargs["message_thread_id"] = target.message_thread_id
    send_result, message = call_telegram_delivery(
        lambda: send_message(target.chat_id, rendered, **kwargs)
    )
    if send_result.ok:
        result = _TelegramPublishResult(
            transport=send_result,
            delivery=_delivery_from_message(
                message, fallback_chat_id=target.chat_id
            ),
        )
        if edit_fallback:
            # 最终编辑明确失败后才会降级发送新消息；成功后删除旧占位，避免
            # 聊天中同时残留“正在处理”和最终答案。
            _delete_stale_telegram_delivery(bot, target)
        return result
    if send_result.outcome_unknown:
        logger.info(
            "Telegram Agent 普通消息固化结果未知，停止 reply fallback error=%s",
            send_result.error,
        )
        return _TelegramPublishResult(transport=send_result)
    logger.info(
        "Telegram Agent 普通消息固化明确失败 status=%s",
        send_result.status_code or "-",
    )
    return _TelegramPublishResult(
        transport=send_result, fallback_allowed=True,
    )


def _reply_agent_message(
    bot: Any, message: Any, rendered: str, **kwargs: Any
) -> _TelegramPublishResult:
    result, sent = call_telegram_delivery(
        lambda: bot.reply_to(message, rendered, **kwargs)
    )
    chat_id, _source_message_id, _message_thread_id = _message_context(message)
    return _TelegramPublishResult(
        transport=result,
        delivery=(
            _delivery_from_message(sent, fallback_chat_id=chat_id)
            if result.ok and chat_id is not None
            else None
        ),
    )


def _fallback_agent_reply_if_safe(
    bot: Any,
    message: Any,
    rendered: str,
    result: _TelegramPublishResult,
    **kwargs: Any,
) -> _TelegramPublishResult:
    """仅在前一投递明确未送达时，才降级到 caller reply。"""
    if result.sent or result.outcome_unknown or not result.fallback_allowed:
        return result
    return _reply_agent_message(bot, message, rendered, **kwargs)


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
        return _TelegramPublishResult(
            transport=TelegramSendResult(
                ok=False, error="TelegramStreamUnavailable", status_code=503,
            ),
            fallback_allowed=True,
        )
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
    *,
    is_allowed: Callable[[], bool] | None = None,
) -> tuple[bool, Any | None]:
    """在不持有 owner 生命周期锁的前提下执行一次 Telegram I/O。

    外部网络调用无法可靠中断，因此发送前后都检查租约。新消息和 reset 可以在
    请求阻塞期间立即撤销旧租约；旧请求返回后不得继续写历史或发送后续内容。
    """
    allowed = is_allowed or (lambda: True)
    if not coordinator.is_current(operation) or not allowed():
        return False, None
    result = callback()
    return coordinator.is_current(operation) and allowed(), result


def _finalize_telegram_operation(
    coordinator: Any,
    operation: Any,
    callback: Callable[[], Any],
    *,
    runtime_generation: int | None = None,
    is_allowed: Callable[[], bool] | None = None,
) -> tuple[bool, Any | None]:
    """在运行态与 owner 终态窗口内原子提交一次 Telegram 结果。"""
    allowed = is_allowed or (lambda: True)

    def finalize_if_allowed() -> tuple[bool, Any | None]:
        if not allowed():
            return False, None
        return coordinator.finalize_if_current(operation, callback)

    if runtime_generation is None:
        return finalize_if_allowed()
    try:
        with _telegram_runtime_admission(
            expected_generation=runtime_generation
        ):
            return finalize_if_allowed()
    except AgentRuntimeDisabled:
        return False, None


def _trace_operation_id(operation: Any) -> str:
    value = str(getattr(operation, "operation_id", "") or "").strip()
    return value or f"tg_trace_{secrets.token_urlsafe(12)}"


def _start_telegram_operation_typing(
    bot: Any,
    message: Any,
    *,
    is_current: Callable[[], bool],
) -> _TelegramTypingHeartbeat:
    chat_id, _source_message_id, message_thread_id = _message_context(message)
    return _TelegramTypingHeartbeat(
        bot,
        chat_id,
        is_current=is_current,
        message_thread_id=message_thread_id,
        interval_seconds=_TELEGRAM_TYPING_INTERVAL_SECONDS,
        timeout_seconds=_TELEGRAM_TYPING_TIMEOUT_SECONDS,
    ).start()


def _stop_telegram_typing_heartbeat(
    heartbeat: _TelegramTypingHeartbeat | None,
) -> None:
    if heartbeat is not None:
        heartbeat.stop()


def _telegram_callback_operation_id(owner: str, call: Any, *, action: str) -> str:
    """为 callback 派生稳定且不泄露身份/票据的短操作标识。"""
    source = "\x00".join((owner, str(getattr(call, "id", "") or ""), action))
    digest = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"tg_callback_{digest}"


def _cancel_telegram_runtime_operation(
    *, coordinator: Any, service: Any, operation: Any, owner: str
) -> bool:
    """只撤销仍持有当前 lease 的 Telegram 发布状态与确认世代。"""
    if operation is None:
        return False

    def invalidate() -> int:
        get_telegram_agent_action_store().revoke_owner(owner=owner)
        return invalidate_query_confirmation_epoch(service, owner=owner)

    return coordinator.cancel(
        owner=owner,
        operation_id=operation.operation_id,
        reason="runtime_changed",
        remember=False,
        invalidate=invalidate,
    )


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
    state_buffer: AgentStateCommitBuffer | None = None,
    runtime_generation: int | None = None,
    is_allowed: Callable[[], bool] | None = None,
) -> bool:
    """只让当前且仍获准的 callback 构造动作、发布消息并写入历史。"""
    allowed_check = is_allowed or (lambda: True)

    def prepare_if_allowed() -> tuple[str, Any | None] | None:
        return prepare_output() if allowed_check() else None

    if runtime_generation is None:
        allowed, prepared = coordinator.publish_if_current(
            operation, prepare_if_allowed
        )
    else:
        with _telegram_runtime_admission(
            expected_generation=runtime_generation
        ):
            allowed, prepared = coordinator.publish_if_current(
                operation, prepare_if_allowed
            )
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
        is_allowed=allowed_check,
    )
    if not allowed:
        if isinstance(publish_result, _TelegramPublishResult):
            _delete_stale_telegram_delivery(bot, publish_result.delivery)
        return False

    def finalize_callback() -> None:
        if state_buffer is not None:
            state_buffer.commit()
        _record_telegram_callback_conversation(
            owner,
            message=history_message,
            response=response,
            generation=history_generation,
            fallback_summary=fallback_summary,
        )

    finalized, _ = _finalize_telegram_operation(
        coordinator,
        operation,
        finalize_callback,
        runtime_generation=runtime_generation,
        is_allowed=allowed_check,
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
) -> tuple[str, bool, str | None]:
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
            return "", False, None
        published = answer
        emitted = True
        update_preview(force=True)
        return answer, True, None
    except AgentOperationCancelled:
        raise
    except Exception as exc:
        logger.warning(
            "Telegram Agent Provider 流中断 emitted=%s type=%s",
            emitted,
            type(exc).__name__,
        )
        if not emitted:
            return "", False, None
        interruption_kind = (
            "invalid"
            if isinstance(exc, PublicNarrativeValidationError)
            else "interrupted"
        )
        partial = projector.published_answer()
        if not partial:
            return "", True, interruption_kind
        published = partial
        update_preview(force=True, interrupted=True)
        return partial, True, interruption_kind


def _telegram_notices(response: dict[str, Any]) -> list[str]:
    candidates: list[Any] = []
    presentation = response.get("presentation")
    if isinstance(presentation, dict) and isinstance(presentation.get("notices"), list):
        candidates.extend(presentation["notices"])
    display = response.get("display")
    if isinstance(display, dict) and isinstance(display.get("notices"), list):
        candidates.extend(display["notices"])
    result = response.get("result")
    if not candidates and isinstance(result, dict):
        candidates.extend(project_public_notices(result.get("suggestions")))

    notices: list[str] = []
    for value in candidates:
        text = sanitize_public_text(value, limit=220)
        if text and text not in notices:
            notices.append(text)
        if len(notices) >= 3:
            break
    return notices


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


def _telegram_agent_trace_details(
    response: dict[str, Any], *, attention_only: bool = False
) -> str:
    """展示必要的公开核对范围；自然叙述后只补失败或未完成项。"""
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
        item_ok = item.get("ok") is True
        if attention_only and item_ok:
            continue
        projected.append((
            label,
            item_ok,
            _public_text(item.get("summary"), limit=150),
        ))
    if not projected or (len(projected) == 1 and not partial and not attention_only):
        return ""

    title = "已完成部分核对" if partial else ("需要留意" if attention_only else "本次核对")
    lines = [f"<b>{title}</b>"]
    for label, ok, summary in projected:
        lines.append(f"• <b>{label}</b> · {'完成' if ok else '需关注'}")
        if summary:
            lines.append(f"  {summary}")
    remaining = max(0, len(raw_trace) - len(projected))
    if remaining and not attention_only:
        lines.append(f"另有 {remaining} 项已核对。")
    return "\n".join(lines)


def _telegram_compact_visible_text(value: str) -> str:
    plain = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff]+", "", plain.casefold())


_TELEGRAM_GUIDANCE_ADVISORY_MARKERS = (
    "你可以", "可以", "可稍后", "建议", "不妨", "直接", "回复",
)
_TELEGRAM_GUIDANCE_NEGATING_MARKERS = (
    "不要", "不建议", "不可以", "不能", "不必", "无需", "无须", "别",
    "并非", "并不", "不是", "可以不", "可以先不", "可先不", "暂时不", "先不",
)


def _telegram_guidance_context_text(value: str) -> str:
    plain = unicodedata.normalize(
        "NFKC", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    ).casefold()
    clauses = re.sub(r"[。！？!?；;\n\r]+", "|", plain)
    return re.sub(r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff|]+", "", clauses)


def _telegram_guidance_already_advised(candidate: str, visible: str) -> bool:
    """只把同一分句中的肯定建议视为重复；否定和执行记录必须保留 guidance。"""
    cursor = 0
    while True:
        position = visible.find(candidate, cursor)
        if position < 0:
            return False
        clause_start = visible.rfind("|", 0, position) + 1
        prefix = visible[max(clause_start, position - 32):position]
        has_advisory = (
            prefix.endswith("请")
            or any(marker in prefix for marker in _TELEGRAM_GUIDANCE_ADVISORY_MARKERS)
        )
        if has_advisory and not any(
            marker in prefix for marker in _TELEGRAM_GUIDANCE_NEGATING_MARKERS
        ):
            return True
        cursor = position + 1


def _telegram_nonredundant_guidance(
    guidance: list[dict[str, str]], *, visible_html: str
) -> list[dict[str, str]]:
    """正文已经明确建议过的下一步不再重复追加成模板栏目。"""
    visible = _telegram_guidance_context_text(visible_html)
    if not visible:
        return guidance
    projected: list[dict[str, str]] = []
    for item in guidance:
        candidates = (
            _telegram_compact_visible_text(item.get("label", "")),
            _telegram_compact_visible_text(item.get("prompt", "")),
        )
        if any(
            len(candidate) >= 4
            and _telegram_guidance_already_advised(candidate, visible)
            for candidate in candidates
        ):
            continue
        projected.append(item)
    return projected


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
        promote_first=False,
    )

    presentation = payload.get("presentation")
    narrative = ""
    if (
        isinstance(presentation, dict)
        and presentation.get("kind") == "narrative"
        and presentation.get("source") in {"llm", "system", "native"}
    ):
        narrative = _public_multiline_html(
            presentation.get("narrative"),
            limit=_MAX_NARRATIVE_LENGTH,
            promote_first=False,
        )

    structured_details = _telegram_library_audit_details(payload)
    trace_details = _telegram_agent_trace_details(
        payload, attention_only=bool(narrative)
    )
    body_parts = [
        item for item in (narrative or summary, structured_details, trace_details) if item
    ]
    body = "\n\n".join(body_parts) or "当前没有可安全展示的结果摘要。"
    error = _public_multiline_html(
        display.get("error") or result.get("error"), limit=500
    )
    if confirmation:
        lines: list[str] = ["<b>需要你确认</b>", "", body]
    elif status in {"clarification_required", "selection_required"}:
        # 补充信息并不是失败，不用错误标题制造不必要的挫败感。
        lines = [body]
    elif explicit_public_status and (
        public_key == "unavailable" or public_tone == "error"
    ):
        lines = [f"<b>{public_label or '暂时无法完成'}</b>", "", body]
        if error and error != body and error not in body:
            lines.extend(["", error])
    elif explicit_public_status and (
        public_key == "attention" or public_tone == "warning"
    ):
        lines = [f"<b>{public_label or '需要留意'}</b>", "", body]
        if error and error != body and error not in body:
            lines.extend(["", error])
    elif explicit_public_status and public_key == "in_progress" and public_label:
        lines = [f"<b>{public_label}</b>", "", body]
    elif not ok:
        lines = ["<b>没能完成这次请求</b>", "", body]
        if error and error != body and error not in body:
            lines.extend(["", error])
    else:
        lines = [body]

    if payload.get("mode") == "read_plan" and not trace_details:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        safe_steps = []
        for index, step in enumerate(steps[:4], start=1):
            if not isinstance(step, dict):
                continue
            step_result = step.get("result") if isinstance(step.get("result"), dict) else {}
            if narrative and step_result.get("ok") is True:
                continue
            label = _READ_PLAN_LABELS.get(str(step.get("tool_name") or ""), "诊断步骤")
            step_summary = _public_text(step_result.get("summary"), limit=180)
            step_status = "完成" if step_result.get("ok") is True else "需关注"
            safe_steps.append(
                f"{index}. <b>{html.escape(label, quote=False)}</b> · {step_status}"
                + (f"\n   {step_summary}" if step_summary else "")
            )
        if safe_steps:
            lines.extend([
                "",
                "<b>需要留意</b>" if narrative else "<b>检查步骤</b>",
                *safe_steps,
            ])

    visible_html = "\n".join(lines)
    visible_compact = _telegram_compact_visible_text(visible_html)
    notices = [
        notice
        for notice in _telegram_notices(payload)
        if _telegram_compact_visible_text(notice) not in visible_compact
    ]
    if notices:
        lines.extend(["", "<i>提示：" + "；".join(notices) + "</i>"])

    guidance = _telegram_nonredundant_guidance(
        _telegram_guidance(payload), visible_html="\n".join(lines)
    )
    show_guidance = (
        status in {
            "clarification_required", "selection_required", "partial", "incomplete", "degraded",
        }
        or public_key == "attention"
        or public_tone == "warning"
        or not ok
    )
    if guidance and show_guidance:
        lines.extend(["", "<b>接下来可以</b>"])
        for item in guidance:
            lines.append(f"• {item['label']}")

    if confirmation:
        plan = sanitize_action_plan(payload.get("action_plan"))
        action = _public_text(plan.get("title"), limit=100)
        target = _public_text(plan.get("target"), limit=160)
        impact = _public_text(plan.get("impact"), limit=220)
        reversibility = _public_text(plan.get("reversibility"), limit=220)
        preview = _public_text(plan.get("preflight_summary"), limit=180)
        if action:
            lines[0] = f"<b>行动计划：{action}</b>"
        lines.extend([
            "",
            f"• <b>范围：</b>{target or '当前预检选中的对象'}",
            f"• <b>影响：</b>{impact or '执行后会应用预检通过的受控变更。'}",
            f"• <b>撤销：</b>{reversibility or '执行后可能需要手动撤销。'}",
        ])
        if preview:
            lines.append(f"• <b>预检：</b>{preview}")
        lines.extend(["", "尚未执行。请选择执行或取消，按钮 60 秒内有效。"])

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
    "ingest.submit",
})


def _safe_callback_media_history(
    payload: dict[str, Any], result: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
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
    verification = (
        data.get("verification")
        if isinstance(data.get("verification"), dict)
        else {}
    )
    title = sanitize_public_text(
        data.get("title")
        or verification.get("title")
        or data.get("query")
        or arguments.get("title")
        or arguments.get("query"),
        limit=160,
    )
    if not title:
        return tool_name, {}
    media: dict[str, Any] = {"title": title}
    sources = (data, verification, arguments)
    original_title = sanitize_public_text(
        data.get("original_title")
        or verification.get("original_title")
        or arguments.get("original_title"),
        limit=160,
    )
    if original_title:
        media["original_title"] = original_title
    year = str(
        data.get("year") or verification.get("year") or arguments.get("year") or ""
    ).strip()
    if re.fullmatch(r"(?:19|20)\d{2}", year):
        media["year"] = year
    media_type = (
        "tv"
        if tool_name == "ingest.submit"
        else str(
            data.get("media_type") or arguments.get("media_type") or ""
        ).strip().lower()
    )
    if media_type in {"movie", "tv"}:
        media["media_type"] = media_type
    for field, maximum_digits in (
        ("tmdb_id", 10),
        ("bangumi_id", 10),
        ("douban_id", 20),
    ):
        identifier = next((
            candidate
            for source in sources
            if (candidate := str(source.get(field) or "").strip()).isascii()
            and candidate.isdigit()
            and 1 <= len(candidate) <= maximum_digits
        ), "")
        if identifier:
            media[field] = identifier
    for field, maximum in (("season", 100), ("episode", 1000)):
        coordinate = next((
            candidate
            for source in sources
            if isinstance((candidate := source.get(field)), int)
            and not isinstance(candidate, bool)
            and 1 <= candidate <= maximum
        ), None)
        if coordinate is not None:
            media[field] = coordinate
    case_stage = media_case_stage_for_tool(tool_name)
    if case_stage:
        media["case_stage"] = case_stage
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


def _agent_control_state() -> tuple[bool, bool]:
    return is_agent_enabled(), _enabled(get("TG_AGENT_ENABLED", "0"))


def _agent_control_panel_text(*, notice: str = "") -> str:
    global_enabled, telegram_enabled = _agent_control_state()
    if not global_enabled:
        telegram_label = "随全局关闭" if telegram_enabled else "已关闭"
        detail = (
            "Media Agent 当前全局关闭；Web Agent、Telegram Agent 与后台任务均不运行。"
        )
    elif not telegram_enabled:
        telegram_label = "已关闭"
        detail = "Web Agent 与后台任务继续运行，仅 Telegram Agent 接入已关闭。"
    else:
        telegram_label = "已开启"
        detail = (
            "直接发送问题即可查询媒体库、下载、RSS 或整理状态，也可搜索资源并选择 "
            "qBittorrent / 光鸭；写操作会先要求确认。"
        )

    lines = [
        "<b>Media Agent</b>",
        "",
        f"全局服务：<b>{'已开启' if global_enabled else '已关闭'}</b>",
        f"Telegram 接入：<b>{telegram_label}</b>",
        f"后台任务：<b>{'已启用' if global_enabled else '已关闭'}</b>",
        "",
        detail,
        "传统整理、同步、搜索、RSS 与状态命令不受 Agent 开关影响。",
    ]
    if global_enabled and telegram_enabled:
        lines.extend(["", "使用 /agent_reset 可清除当前 Agent 会话和待确认操作。"])
    if notice:
        lines.extend(["", f"<b>{html.escape(notice)}</b>"])
    return "\n".join(lines)


def _agent_control_panel_markup(
    telebot_module: Any,
    *,
    chat_id: str,
    user_id: str,
) -> Any:
    from app.modules.telegram_write_confirmations import (
        get_telegram_write_confirmation_store,
    )

    global_enabled, telegram_enabled = _agent_control_state()
    if not global_enabled:
        buttons = [("开启 Media Agent", "preview", {"action": "enable_all"})]
    elif not telegram_enabled:
        buttons = [
            ("开启 Telegram Agent", "apply", {"action": "enable_telegram"}),
            ("关闭全部 Media Agent", "preview", {"action": "disable_all"}),
        ]
    else:
        buttons = [
            ("关闭 Telegram Agent", "apply", {"action": "disable_telegram"}),
            ("关闭全部 Media Agent", "preview", {"action": "disable_all"}),
        ]
    action_ids = get_telegram_write_confirmation_store().create_group(
        chat_id=chat_id,
        user_id=user_id,
        operation="agent_control",
        actions=[(decision, value) for _label, decision, value in buttons],
    )
    markup = telebot_module.types.InlineKeyboardMarkup(row_width=1)
    for (label, _decision, _value), action_id in zip(buttons, action_ids):
        markup.add(
            telebot_module.types.InlineKeyboardButton(
                label,
                callback_data=f"tgc:{action_id}",
            )
        )
    return markup



def _finish_agent_control_action(
    bot: Any,
    call: Any,
    *,
    notice: str,
    successful: bool,
) -> bool:
    """结束一次性控制快照；旧消息不再变成新的控制台。"""
    headline = html.escape(str(notice or "操作已完成"), quote=False)
    detail = (
        "开关状态已经更新。"
        if successful
        else "本次没有修改开关状态。"
    )
    text = (
        f"<b>{headline}</b>\n\n{detail}"
        "\n再次发送 /agent 可查看当前状态或继续调整。"
    )
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=None,
        )
        return True
    except Exception as exc:
        logger.info("结束 Telegram Agent 控制快照失败 type=%s", type(exc).__name__)
        _remove_callback_keyboard(bot, call.message)
        return False


def _agent_control_updates(action_name: str) -> dict[str, str]:
    global_enabled, telegram_enabled = _agent_control_state()
    if action_name == "enable_all":
        desired = {"AGENT_ENABLED": "1", "TG_AGENT_ENABLED": "1"}
    elif action_name == "enable_telegram":
        if not global_enabled:
            raise ValueError("Media Agent 全局服务尚未开启")
        desired = {"TG_AGENT_ENABLED": "1"}
    elif action_name == "disable_telegram":
        desired = {"TG_AGENT_ENABLED": "0"}
    elif action_name == "disable_all":
        desired = {"AGENT_ENABLED": "0"}
    else:
        raise ValueError("Agent 控制操作无效")

    if action_name in {"enable_all", "enable_telegram"}:
        if not str(get("TG_BOT_TOKEN", "") or "").strip():
            raise ValueError("Telegram Bot Token 尚未配置")
        if not str(get("TG_CHAT_ID", "") or "").strip():
            raise ValueError("Telegram Chat ID 尚未配置")
        if not _allowed_user_ids():
            raise ValueError("Telegram Agent 用户白名单尚未配置")

    current = {
        "AGENT_ENABLED": "1" if global_enabled else "0",
        "TG_AGENT_ENABLED": "1" if telegram_enabled else "0",
    }
    return {key: value for key, value in desired.items() if current[key] != value}


def _apply_agent_control_action(action_name: str, *, owner: str) -> str:
    from app import config

    with agent_runtime_transition():
        updates = _agent_control_updates(action_name)
        if updates:
            config.set_and_save(updates)
            # Telegram 子开关与全局开关共用同一运行代次；无论改变哪一层，
            # 切换前已开始的请求都不能在重新启用后迟到发布。
            invalidate_agent_runtime_generation()
    if updates:
        if "AGENT_ENABLED" in updates:
            try:
                from app.modules.agent_runtime import request_agent_runtime_reconcile

                request_agent_runtime_reconcile()
            except Exception as exc:
                logger.warning(
                    "Telegram Agent 运行态刷新请求失败 type=%s",
                    type(exc).__name__,
                )
        try:
            from app.bot.handlers import request_command_menu_refresh

            request_command_menu_refresh()
        except Exception as exc:
            logger.warning(
                "Telegram Agent 菜单刷新请求失败 type=%s",
                type(exc).__name__,
            )
    if action_name in {"disable_telegram", "disable_all"}:
        try:
            get_telegram_agent_action_store().revoke_owner(owner=owner)
        except Exception as exc:
            logger.warning(
                "Telegram Agent 旧按钮撤销失败 type=%s",
                type(exc).__name__,
            )
    return {
        "enable_all": "Media Agent 已开启",
        "enable_telegram": "Telegram Agent 已开启",
        "disable_telegram": "Telegram Agent 已关闭",
        "disable_all": "Media Agent 已全部关闭",
    }[action_name]


def handle_agent_control_action(
    bot: Any,
    call: Any,
    telebot_module: Any,
    action: dict[str, Any],
) -> None:
    """处理 owner 绑定的一次性 Agent 开关操作。"""
    from app.config import (
        ConcurrentConfigUpdateError,
        CorruptConfigFileError,
        ExternalConfigOverrideError,
    )
    from app.modules.telegram_write_confirmations import (
        TelegramWriteConfirmationError,
        get_telegram_write_confirmation_store,
    )

    chat_id, user_id = _identity(call)
    if telegram_agent_control_access(chat_id, user_id) != "allowed":
        _remove_callback_keyboard(bot, call.message)
        bot.answer_callback_query(call.id, "当前身份无权管理 Media Agent", show_alert=True)
        return

    decision = str(action.get("decision") or "")
    value = action.get("value") if isinstance(action.get("value"), dict) else {}
    action_name = str(value.get("action") or "")
    if decision == "cancel":
        _finish_agent_control_action(
            bot, call, notice="操作已取消", successful=False
        )
        bot.answer_callback_query(call.id, "操作已取消")
        return
    if decision == "preview" and action_name in {"enable_all", "disable_all"}:
        enabling = action_name == "enable_all"
        confirm_id, cancel_id = get_telegram_write_confirmation_store().create_pair(
            chat_id=chat_id,
            user_id=user_id,
            operation="agent_control",
            value={"action": action_name, "confirmed": True},
        )
        markup = telebot_module.types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            telebot_module.types.InlineKeyboardButton(
                "确认开启全部" if enabling else "确认关闭全部",
                callback_data=f"tgc:{confirm_id}",
            ),
            telebot_module.types.InlineKeyboardButton(
                "取消",
                callback_data=f"tgc:{cancel_id}",
            ),
        )
        if enabling:
            title = "确认开启 Media Agent"
            detail = "这会启用 Web Agent、Telegram Agent 与 Agent 后台任务。"
        else:
            title = "确认关闭全部 Media Agent"
            detail = (
                "这会停止 Web Agent、Telegram Agent 与 Agent 后台任务；"
                "传统整理、同步、搜索、RSS 与状态命令仍可使用。"
            )
        bot.edit_message_text(
            f"<b>{title}</b>\n\n{detail}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=markup,
        )
        bot.answer_callback_query(
            call.id,
            "请确认是否开启 Media Agent" if enabling else "请确认是否关闭全部 Media Agent",
        )
        return
    if decision not in {"apply", "confirm"}:
        raise TelegramWriteConfirmationError("Agent 控制操作无效")
    if action_name in {"enable_all", "disable_all"} and not bool(
        value.get("confirmed")
    ):
        raise TelegramWriteConfirmationError("全局 Media Agent 开关需要再次确认")

    try:
        owner = telegram_agent_owner(chat_id, user_id)
        notice = _apply_agent_control_action(action_name, owner=owner)
    except ExternalConfigOverrideError:
        notice = "操作未完成：该开关由 Docker 或部署环境管理"
        _finish_agent_control_action(bot, call, notice=notice, successful=False)
        bot.answer_callback_query(call.id, notice, show_alert=True)
        return
    except ConcurrentConfigUpdateError:
        notice = "操作未完成：配置刚被其他操作修改，请重试"
        _finish_agent_control_action(bot, call, notice=notice, successful=False)
        bot.answer_callback_query(call.id, notice, show_alert=True)
        return
    except CorruptConfigFileError:
        notice = "操作未完成：user.env 无法安全读取"
        _finish_agent_control_action(bot, call, notice=notice, successful=False)
        bot.answer_callback_query(call.id, notice, show_alert=True)
        return
    except ValueError as exc:
        notice = f"操作未完成：{str(exc)}"
        _finish_agent_control_action(bot, call, notice=notice, successful=False)
        bot.answer_callback_query(call.id, notice, show_alert=True)
        return
    except Exception as exc:
        logger.warning("Telegram Agent 开关更新失败 type=%s", type(exc).__name__)
        notice = "操作未完成：配置保存失败，请稍后重试"
        _finish_agent_control_action(bot, call, notice=notice, successful=False)
        bot.answer_callback_query(call.id, notice, show_alert=True)
        return

    _finish_agent_control_action(bot, call, notice=notice, successful=True)
    bot.answer_callback_query(call.id, notice)


def handle_agent_guide(
    bot: Any,
    message: Any,
    telebot_module: Any | None = None,
) -> None:
    """展示永久可用的 Telegram Agent 状态与控制入口。"""
    chat_id, user_id = _identity(message)
    if telegram_agent_control_access(chat_id, user_id) != "allowed":
        bot.reply_to(message, "当前身份未获准管理 Media Agent。")
        return
    if telebot_module is None:
        import telebot as telebot_module
    try:
        markup = _agent_control_panel_markup(
            telebot_module,
            chat_id=chat_id,
            user_id=user_id,
        )
    except Exception as exc:
        logger.warning("创建 Telegram Agent 控制面板失败 type=%s", type(exc).__name__)
        markup = None
    bot.reply_to(
        message,
        _agent_control_panel_text(),
        parse_mode="HTML",
        reply_markup=markup,
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
    plan_id: str,
):
    store = get_telegram_agent_action_store()
    confirm_id, cancel_id = store.create_confirmation_pair(
        owner=owner, plan_id=plan_id
    )
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton(
            "执行",
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


def _resource_candidates(response: Any) -> list[dict[str, Any]]:
    """只提取可下载资源的最小安全展示字段。"""
    candidates: list[dict[str, Any]] = []
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
            quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
            reasons = quality.get("reasons") if isinstance(quality.get("reasons"), list) else []
            warnings = quality.get("warnings") if isinstance(quality.get("warnings"), list) else []
            explanation_parts = [
                _redact_text(value, limit=90)
                for value in [*reasons[:2], *warnings[:1]]
                if _redact_text(value, limit=90)
            ]
            candidates.append(
                {
                    "result_id": result_id,
                    "title": title,
                    "episode": episode_label,
                    "site": _redact_text(
                        item.get("site_name") or item.get("site_id"), limit=60
                    ),
                    "size": _redact_text(item.get("size_text"), limit=32),
                    "explanation": "；".join(explanation_parts)[:180],
                }
            )
            if len(candidates) >= _RESOURCE_RESULT_LIMIT:
                return candidates
    return candidates


def _resource_candidates_are_primary(response: Any) -> bool:
    """渠道只消费服务端已附加的显式语义契约。"""
    contract = response_contract(response)
    return contract.get("resource_candidates") == "primary"


def _confirmation_is_primary(response: Any) -> bool:
    return response_contract(response).get("presentation") == "confirmation"


def _resource_markup_with_guard(
    telebot: Any,
    *,
    owner: str,
    candidates: list[dict[str, Any]],
    page: int = 0,
) -> tuple[Any | None, str]:
    if not candidates:
        return None, ""
    interaction = get_telegram_agent_action_store().create_resource_interaction(
        owner=owner,
        candidates=candidates,
        page=page,
    )
    page_number = int(interaction["page"])
    total_pages = int(interaction["total_pages"])
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    for item in interaction["items"]:
        markup.add(
            telebot.types.InlineKeyboardButton(
                f"{item['position']} · qB",
                callback_data=f"aga:{item['qb_action_id']}",
            ),
            telebot.types.InlineKeyboardButton(
                f"{item['position']} · 光鸭",
                callback_data=f"aga:{item['guangya_action_id']}",
            ),
        )
    navigation = []
    previous_id = str(interaction["previous_action_id"] or "")
    if previous_id:
        navigation.append(
            telebot.types.InlineKeyboardButton(
                f"◀ 上一页 {page_number}/{total_pages}",
                callback_data=f"aga:{previous_id}",
            )
        )
    next_id = str(interaction["next_action_id"] or "")
    if next_id:
        navigation.append(
            telebot.types.InlineKeyboardButton(
                f"查看更多 {page_number + 2}/{total_pages} ▶",
                callback_data=f"aga:{next_id}",
            )
        )
    if navigation:
        markup.add(*navigation)
    return markup, str(interaction["guard_action_id"] or "")


def _resource_markup(
    telebot: Any,
    *,
    owner: str,
    candidates: list[dict[str, Any]],
    page: int = 0,
):
    markup, _guard_action_id = _resource_markup_with_guard(
        telebot,
        owner=owner,
        candidates=candidates,
        page=page,
    )
    return markup


def _render_resource_candidates(
    response: Any, candidates: list[dict[str, Any]], *, page: int = 0
) -> str:
    contract = response_contract(response)
    if not candidates or contract.get("resource_candidates") != "primary":
        return render_agent_response(response)

    payload_page = _normalize_resource_page_payload(
        candidates, page, strict_result_ids=False
    )
    all_candidates = payload_page["candidates"]
    page_number = payload_page["page"]
    total_pages = (
        len(all_candidates) + _RESOURCE_PAGE_SIZE - 1
    ) // _RESOURCE_PAGE_SIZE
    page_start = page_number * _RESOURCE_PAGE_SIZE
    page_candidates = all_candidates[page_start : page_start + _RESOURCE_PAGE_SIZE]

    payload = response if isinstance(response, dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    display = payload.get("display") if isinstance(payload.get("display"), dict) else {}
    # 候选标题由下方确定性结构化列表统一展示；这里不采用模型叙述，
    # 避免模型先复述一遍资源、按钮区域再重复一遍。
    summary = _public_multiline_html(
        display.get("summary") or result.get("summary"),
        limit=900,
        promote_first=False,
    )
    heading = "<b>候选资源</b>"
    if total_pages > 1:
        heading = f"<b>候选资源 · 第 {page_number + 1}/{total_pages} 页</b>"
    lines = [summary or "已找到可下载资源。", "", heading]
    for position, candidate in enumerate(page_candidates, start=page_start + 1):
        if position > page_start + 1:
            lines.append("")
        metadata_parts = []
        if candidate["site"]:
            metadata_parts.append(f"来源：{candidate['site']}")
        if candidate["size"]:
            metadata_parts.append(f"体积：{candidate['size']}")
        metadata = " · ".join(metadata_parts)
        episode = candidate.get("episode")
        prefix = f"{episode} · " if episode else ""
        lines.append(f"<b>{position}.</b> {prefix}{candidate['title']}")
        if metadata:
            lines.append(f"   {metadata}")
        explanation = candidate.get("explanation")
        if explanation:
            lines.append(f"   推荐依据：{explanation}")
    lines.extend([
        "",
        "请用下方按钮选择下载目标。提交前还会显示预检并再次确认。",
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

    if confirmation_reply_intent(user_message) is not None:
        # 文本确认没有绑定具体计划，既不能执行，也不能把仍有效的卡片当作
        # “新查询”撤销。Web 与 Telegram 统一只接受卡片上的一次性按钮。
        bot.reply_to(message, "请使用行动计划卡片上的执行或取消按钮。")
        return True

    service = get_agent_service()
    coordinator = get_agent_operation_coordinator()
    runtime_generation = current_agent_runtime_generation()

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

    def operation_is_current() -> bool:
        return bool(
            coordinator.is_current(operation)
            and agent_runtime_generation_is_current(runtime_generation)
            and telegram_agent_access(chat_id, user_id) == "allowed"
        )

    def publish_if_current(
        callback: Callable[[], Any],
    ) -> tuple[bool, Any | None]:
        return _publish_telegram_io_if_current(
            coordinator,
            operation,
            callback,
            is_allowed=operation_is_current,
        )

    def finalize_if_current(
        callback: Callable[[], Any],
    ) -> tuple[bool, Any | None]:
        return _finalize_telegram_operation(
            coordinator,
            operation,
            callback,
            runtime_generation=runtime_generation,
            is_allowed=operation_is_current,
        )

    state_buffer = AgentStateCommitBuffer(owner=owner)
    llm_budget_token = begin_llm_request_budget(owner)
    stream_target = None
    progress: _TelegramAgentProgress | None = None
    message_chat_id, _source_message_id, message_thread_id = _message_context(message)
    typing_heartbeat = _TelegramTypingHeartbeat(
        bot,
        message_chat_id,
        is_current=operation_is_current,
        message_thread_id=message_thread_id,
        interval_seconds=_TELEGRAM_TYPING_INTERVAL_SECONDS,
        timeout_seconds=_TELEGRAM_TYPING_TIMEOUT_SECONDS,
    )
    try:
        typing_heartbeat.start()
        # Telegram 调用属于不可控外部 I/O，不能占用 owner 生命周期锁；否则
        # 草稿接口卡顿会同时卡住新消息抢占与 /agent_reset。
        allowed, stream_target = publish_if_current(
            lambda: _begin_agent_stream(bot, telebot, message)
        )
        if not allowed:
            _delete_stale_telegram_delivery(bot, stream_target)
            raise AgentOperationCancelled("Telegram Agent 操作已失效")
        progress = _TelegramAgentProgress(
            bot,
            telebot,
            stream_target,
            message,
            publish=publish_if_current,
        )

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
            query_kwargs["trusted_conversation_context"] = True
        reply_context = _telegram_reply_context(message)
        if reply_context:
            query_kwargs["reply_context"] = reply_context
        if confirmation_epoch is not None:
            query_kwargs["confirmation_owner_generation"] = confirmation_epoch
        if stream_target is not None:
            # 工具选择与参数必须先完整校验；只把最终公开自然语言改成 Provider 流。
            query_kwargs["present"] = False
        with bind_agent_progress_listener(
            progress.handle if progress is not None else None
        ), defer_agent_state_commits(state_buffer):
            response = service.query(user_message, **query_kwargs)
        if progress is not None:
            progress.mark_answering()
            progress.stop()
        if not operation_is_current():
            raise AgentOperationCancelled("Telegram Agent 操作已失效")

        if stream_target is not None and isinstance(response, dict):
            source = select_agent_answer_stream(
                user_message,
                response,
                owner=owner,
                tool_stream_factory=stream_tool_answer,
                conversation_stream_factory=stream_existing_answer,
            )
            if source is not None:
                answer, emitted, interruption_kind = run_awaitable_sync(
                    _consume_agent_stream(
                        bot,
                        telebot,
                        stream_target,
                        source,
                        is_current=operation_is_current,
                        publish=publish_if_current,
                    )
                )
                if interruption_kind is not None:
                    interrupted_response: dict[str, Any] | None = None
                    if answer and interruption_kind == "interrupted":
                        interrupted_response = apply_streamed_answer(
                            response,
                            answer,
                            result_projector=project_agent_result_for_user,
                        )
                        result = interrupted_response.get("result")
                        if isinstance(result, dict):
                            interrupted_result = dict(result)
                            interrupted_result["status"] = "interrupted"
                            interrupted_result["summary"] = answer
                            interrupted_response["result"] = interrupted_result
                        presentation = interrupted_response.get("presentation")
                        if isinstance(presentation, dict):
                            interrupted_response["presentation"] = {
                                **presentation,
                                "status": "interrupted",
                            }
                    rendered = (
                        _stream_preview_html(answer, interrupted=True)
                        if answer
                        else "Agent 回答生成中断，请重新发送本次问题。"
                    )

                    def publish_interruption() -> _TelegramPublishResult:
                        result = _persist_agent_stream(
                            bot, telebot, stream_target, rendered
                        )
                        return _fallback_agent_reply_if_safe(
                            bot, message, rendered, result, parse_mode="HTML"
                        )

                    allowed, publish_result = publish_if_current(publish_interruption)
                    if not allowed and isinstance(
                        publish_result, _TelegramPublishResult
                    ):
                        _delete_stale_telegram_delivery(
                            bot, publish_result.delivery
                        )
                    if not allowed:
                        raise AgentOperationCancelled(
                            "Telegram Agent 操作已失效"
                        )

                    def finalize_interruption() -> None:
                        if interrupted_response is None:
                            return
                        state_buffer.commit()
                        _record_telegram_conversation(
                            owner,
                            message=user_message,
                            response=interrupted_response,
                            generation=history_generation,
                        )

                    finalized, _ = finalize_if_current(finalize_interruption)
                    if not finalized:
                        if isinstance(publish_result, _TelegramPublishResult):
                            _delete_stale_telegram_delivery(
                                bot, publish_result.delivery
                            )
                        raise AgentOperationCancelled(
                            "Telegram Agent 操作已失效"
                        )
                    return True
                if emitted and answer and interruption_kind is None:
                    response = apply_streamed_answer(
                        response,
                        answer,
                        result_projector=project_agent_result_for_user,
                    )

        if isinstance(response, dict):
            response = attach_public_fallback_presentation(response)

        def prepare_final_output() -> tuple[str, Any]:
            action_plan = sanitize_action_plan(
                response.get("action_plan") if isinstance(response, dict) else None
            )
            markup = None
            rendered = render_agent_response(response)
            if _confirmation_is_primary(response) and action_plan:
                markup = _confirmation_markup(
                    telebot,
                    owner=owner,
                    plan_id=action_plan["plan_id"],
                )
                rendered = render_agent_response(response, confirmation=True)
            else:
                candidates = (
                    _resource_candidates(response)
                    if _resource_candidates_are_primary(response)
                    else []
                )
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
        try:
            with _telegram_runtime_admission(
                expected_generation=runtime_generation
            ):
                if not operation_is_current():
                    raise AgentOperationCancelled(
                        "Telegram Agent 操作已失效"
                    )
                allowed, prepared_output = coordinator.publish_if_current(
                    operation, prepare_final_output
                )
        except AgentRuntimeDisabled as exc:
            raise AgentOperationCancelled(
                "Telegram Agent 运行态已变化"
            ) from exc
        if not allowed or prepared_output is None:
            raise AgentOperationCancelled("Telegram Agent 操作已失效")
        rendered, markup = prepared_output

        def publish_final_message() -> _TelegramPublishResult:
            result = _finish_agent_stream(
                bot,
                telebot,
                stream_target,
                rendered,
                reply_markup=markup,
                show_progress=False,
            )
            return _fallback_agent_reply_if_safe(
                bot,
                message,
                rendered,
                result,
                reply_markup=markup,
                parse_mode="HTML",
            )

        allowed, publish_result = publish_if_current(publish_final_message)
        if not allowed and isinstance(publish_result, _TelegramPublishResult):
            _delete_stale_telegram_delivery(bot, publish_result.delivery)
        if not allowed:
            raise AgentOperationCancelled("Telegram Agent 操作已失效")

        def finalize_conversation() -> None:
            state_buffer.commit()
            _record_telegram_conversation(
                owner,
                message=user_message,
                response=response,
                generation=history_generation,
            )

        finalized, _ = finalize_if_current(finalize_conversation)
        if not finalized:
            if isinstance(publish_result, _TelegramPublishResult):
                _delete_stale_telegram_delivery(bot, publish_result.delivery)
            raise AgentOperationCancelled("Telegram Agent 操作已失效")
    except AgentOperationCancelled:
        # 运行态/TG 开关变化时，查询可能已经在不可中断的同步调用中签发了
        # 尚未发布的确认票据或生成了按钮。只撤销仍持有当前 lease 的状态；
        # 若已被新消息抢占，cancel 会失败且不会误伤新请求。
        _cancel_telegram_runtime_operation(
            coordinator=coordinator,
            service=service,
            operation=operation,
            owner=owner,
        )
        _delete_stale_telegram_delivery(bot, stream_target)
        logger.info("Telegram Agent 旧请求已停止发布 owner=%s", owner)
    except AgentToolError as exc:
        logger.info("Telegram Agent 请求被拒绝 code=%s", exc.code)
        rendered = "Agent 无法处理该请求，请调整问题后重试。"

        def publish_tool_error() -> _TelegramPublishResult:
            result = _finish_agent_stream(
                bot, telebot, stream_target, rendered
            )
            return _fallback_agent_reply_if_safe(
                bot, message, rendered, result
            )

        allowed, publish_result = publish_if_current(publish_tool_error)
        if not allowed and isinstance(publish_result, _TelegramPublishResult):
            _delete_stale_telegram_delivery(bot, publish_result.delivery)
        if not allowed:
            _cancel_telegram_runtime_operation(
                coordinator=coordinator,
                service=service,
                operation=operation,
                owner=owner,
            )
        else:
            finalized, _ = finalize_if_current(lambda: None)
            if not finalized and isinstance(
                publish_result, _TelegramPublishResult
            ):
                _delete_stale_telegram_delivery(bot, publish_result.delivery)
            if not finalized:
                _cancel_telegram_runtime_operation(
                    coordinator=coordinator,
                    service=service,
                    operation=operation,
                    owner=owner,
                )
    except Exception as exc:
        logger.warning("Telegram Agent 请求失败 type=%s", type(exc).__name__)
        rendered = "Agent 暂时不可用，请稍后重试。"

        def publish_error() -> _TelegramPublishResult:
            result = _finish_agent_stream(
                bot, telebot, stream_target, rendered
            )
            return _fallback_agent_reply_if_safe(
                bot, message, rendered, result
            )

        allowed, publish_result = publish_if_current(publish_error)
        if not allowed and isinstance(publish_result, _TelegramPublishResult):
            _delete_stale_telegram_delivery(bot, publish_result.delivery)
        if not allowed:
            _cancel_telegram_runtime_operation(
                coordinator=coordinator,
                service=service,
                operation=operation,
                owner=owner,
            )
        else:
            finalized, _ = finalize_if_current(lambda: None)
            if not finalized and isinstance(
                publish_result, _TelegramPublishResult
            ):
                _delete_stale_telegram_delivery(bot, publish_result.delivery)
            if not finalized:
                _cancel_telegram_runtime_operation(
                    coordinator=coordinator,
                    service=service,
                    operation=operation,
                    owner=owner,
                )
    finally:
        if progress is not None:
            progress.stop()
        state_buffer.discard()
        reset_llm_request_budget(llm_budget_token)
        typing_heartbeat.stop()
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
    state_buffer: AgentStateCommitBuffer | None = None
    llm_budget_token = None
    typing_heartbeat: _TelegramTypingHeartbeat | None = None
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

        service = get_agent_service()
        runtime_generation = current_agent_runtime_generation()
        operation, _ = coordinator.begin_with_context(
            owner=owner,
            operation_id=_telegram_callback_operation_id(
                owner, call, action=action
            ),
            initialize=lambda: invalidate_query_confirmation_epoch(
                service, owner=owner
            ),
        )

        def operation_is_current() -> bool:
            return bool(
                coordinator.is_current(operation)
                and agent_runtime_generation_is_current(runtime_generation)
                and telegram_agent_access(chat_id, user_id) == "allowed"
            )

        state_buffer = AgentStateCommitBuffer(owner=owner)
        llm_budget_token = begin_llm_request_budget(owner)
        bot.answer_callback_query(call.id, "正在查询，请稍候")
        callback_answered = True
        typing_heartbeat = _start_telegram_operation_typing(
            bot,
            call.message,
            is_current=operation_is_current,
        )
        history_generation = _telegram_history_generation(owner)
        _principal, trace_session_id = _telegram_history_identity(owner)
        with defer_agent_state_commits(state_buffer):
            response = _query_patrol_action(
                service,
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

        _stop_telegram_typing_heartbeat(typing_heartbeat)
        typing_heartbeat = None
        published = _publish_telegram_callback_response(
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
            state_buffer=state_buffer,
            runtime_generation=runtime_generation,
            is_allowed=operation_is_current,
        )
        if published is False:
            _cancel_telegram_runtime_operation(
                coordinator=coordinator,
                service=service,
                operation=operation,
                owner=owner,
            )
    except (AgentRuntimeDisabled, AgentToolError, ValueError) as exc:
        runtime_changed = isinstance(exc, AgentRuntimeDisabled)
        if runtime_changed:
            _cancel_telegram_runtime_operation(
                coordinator=coordinator,
                service=service,
                operation=operation,
                owner=owner,
            )
        if (
            not runtime_changed
            and operation is not None
            and not coordinator.is_current(operation)
        ):
            if not callback_answered:
                bot.answer_callback_query(
                    call.id, "操作已过期或无效", show_alert=True
                )
            return
        _stop_telegram_typing_heartbeat(typing_heartbeat)
        typing_heartbeat = None
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
        _stop_telegram_typing_heartbeat(typing_heartbeat)
        typing_heartbeat = None
        if callback_answered:
            bot.reply_to(
                call.message,
                "<b>查询没有完成</b>\n服务暂时不可用，请稍后重试。",
                parse_mode="HTML",
            )
        else:
            bot.answer_callback_query(call.id, "Agent 暂时不可用", show_alert=True)
    finally:
        if state_buffer is not None:
            state_buffer.discard()
        if llm_budget_token is not None:
            reset_llm_request_budget(llm_budget_token)
        _stop_telegram_typing_heartbeat(typing_heartbeat)
        if operation is not None:
            coordinator.finish(operation)


def handle_agent_callback(bot: Any, call: Any, telebot_module: Any = None) -> None:
    chat_id, user_id = _identity(call)
    callback_answered = False
    confirmed_action_completed = False
    coordinator = get_agent_operation_coordinator()
    operation = None
    state_buffer: AgentStateCommitBuffer | None = None
    typing_heartbeat: _TelegramTypingHeartbeat | None = None
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

        action_kind = action_metadata["action"]

        if action_kind == "paginate_resources":
            # 分页本身也是一次可见发布：旧按钮的消费、新页面按钮的生成和
            # latest-wins 租约必须处于同一 owner 短窗口。Telegram 网络调用
            # 始终在锁外执行，避免慢客户端阻塞同一用户的新请求。
            with _telegram_runtime_admission() as pagination_runtime_generation:
                with coordinator.owner_window(owner):
                    action = store.resolve(action_id, owner=owner)
                    operation = coordinator.begin(
                        owner=owner,
                        operation_id=_telegram_callback_operation_id(
                            owner, call, action="paginate_resources"
                        ),
                    )
                    module = telebot_module
                    if module is None:
                        import telebot as module
                    text = _render_resource_candidates(
                        {
                            "response_contract": build_response_contract(
                                task_kind="resource_search",
                                presentation="resource_candidates",
                                resource_candidates="primary",
                            )
                        },
                        action["candidates"],
                        page=action["page"],
                    )
                    markup, pagination_guard_id = _resource_markup_with_guard(
                        module,
                        owner=owner,
                        candidates=action["candidates"],
                        page=action["page"],
                    )

            def pagination_is_current() -> bool:
                if (
                    operation is None
                    or not coordinator.is_current(operation)
                    or not agent_runtime_generation_is_current(
                        pagination_runtime_generation
                    )
                    or telegram_agent_access(chat_id, user_id) != "allowed"
                ):
                    return False
                try:
                    store.validate(pagination_guard_id, owner=owner)
                except ValueError:
                    return False
                return True

            def discard_unpublished_page() -> None:
                # 只在本分页租约仍为当前操作时消费它刚创建的交互组；如果已被
                # 新请求取代，新请求会负责撤销旧组，不能在这里宽泛清理 owner。
                if operation is None or not coordinator.is_current(operation):
                    return
                try:
                    store.resolve(pagination_guard_id, owner=owner)
                except ValueError:
                    pass

            if not pagination_is_current():
                discard_unpublished_page()
                raise ValueError("操作已过期或无效")

            total_pages = (
                len(action["candidates"]) + _RESOURCE_PAGE_SIZE - 1
            ) // _RESOURCE_PAGE_SIZE
            bot.answer_callback_query(
                call.id, f"第 {action['page'] + 1}/{total_pages} 页"
            )
            callback_answered = True
            if not pagination_is_current():
                discard_unpublished_page()
                _remove_callback_keyboard(bot, call.message)
                return
            try:
                bot.edit_message_text(
                    text,
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception:
                discard_unpublished_page()
                raise
            if not pagination_is_current():
                # 新消息可在 Telegram edit 的网络等待期间取得 owner 窗口；
                # edit 返回后立即撤下旧卡片上已失效的按钮，避免误导用户。
                discard_unpublished_page()
                _remove_callback_keyboard(bot, call.message)
            return

        history_generation = _telegram_history_generation(owner)
        service = get_agent_service()
        callback_runtime_generation = current_agent_runtime_generation()

        def callback_operation_is_current() -> bool:
            return bool(
                operation is not None
                and coordinator.is_current(operation)
                and agent_runtime_generation_is_current(
                    callback_runtime_generation
                )
                and telegram_agent_access(chat_id, user_id) == "allowed"
            )

        if action_kind == "prepare_resource":
            bot.answer_callback_query(call.id, "正在准备，请稍候")
            callback_answered = True
            typing_heartbeat = _start_telegram_operation_typing(
                bot, call.message, is_current=lambda: True
            )
            action_plan: dict[str, Any] = {}
            prepare_error: AgentToolError | None = None
            try:
                # 仅在短窗口内确认运行态并记录代次；慢速资源预检必须在锁外执行，
                # 否则会阻塞同一 owner 的新消息、重置与开关切换。
                with _telegram_runtime_admission() as runtime_generation:
                    pass
                with coordinator.owner_window(owner):
                    action = store.inspect(action_id, owner=owner)
                    if action.get("action") != "prepare_resource":
                        raise ValueError("操作已过期或无效")
                    coordinator.invalidate_owner(
                        owner=owner, reason="controlled_action"
                    )
                    confirmation_epoch = begin_query_confirmation_epoch(
                        service, owner=owner
                    )

                prepare_kwargs: dict[str, Any] = {
                    "owner": owner,
                    "request_id": _trace_operation_id(operation),
                    "session_id": _telegram_history_identity(owner)[1],
                }
                if confirmation_epoch is not None:
                    prepare_kwargs["expected_owner_generation"] = confirmation_epoch
                response = service.prepare(
                    "ingest.submit",
                    {
                        "source_type": "resource_candidates",
                        "positions": [action["position"]],
                        "target": action["target"],
                    },
                    **prepare_kwargs,
                )
                action_plan = sanitize_action_plan(
                    response.get("action_plan")
                    if isinstance(response, dict) else None
                )
                if not _confirmation_is_primary(response) or not action_plan:
                    raise ValueError("资源预检未返回确认票据")
                if telebot_module is None:
                    import telebot as telebot_module

                try:
                    # 预检返回后再次验证同一运行代次，并在短 owner 窗口内一次性
                    # 消费原资源按钮、生成确认按钮和写入安全历史。开关变化或新消息
                    # 抢占时，原资源按钮仍未消费，可在重新启用后重试。
                    with _telegram_runtime_admission(
                        expected_generation=runtime_generation
                    ):
                        with coordinator.owner_window(owner):
                            claimed = store.resolve(action_id, owner=owner)
                            if (
                                claimed.get("action") != "prepare_resource"
                                or claimed.get("result_id") != action["result_id"]
                                or claimed.get("position") != action["position"]
                                or claimed.get("target") != action["target"]
                            ):
                                raise ValueError("操作已过期或无效")
                            markup = _confirmation_markup(
                                telebot_module,
                                owner=owner,
                                plan_id=action_plan["plan_id"],
                            )
                            _record_telegram_callback_conversation(
                                owner,
                                message="准备提交所选资源",
                                response=response,
                                generation=history_generation,
                                fallback_summary="资源提交已完成预检，等待确认。",
                            )
                except Exception:
                    # 已生成但尚未公开的计划必须主动失效，避免运行态切换或按钮
                    # 抢占后留下无入口的隐藏确认票据。
                    try:
                        service.discard_confirmation(
                            action_plan["plan_id"],
                            owner=owner,
                            advance_owner_epoch=False,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Telegram 未发布资源计划回收失败 type=%s",
                            type(exc).__name__,
                        )
                    raise

                text = render_agent_response(response, confirmation=True)
            except AgentToolError as exc:
                prepare_error = exc
            finally:
                _stop_telegram_typing_heartbeat(typing_heartbeat)
                typing_heartbeat = None

            if prepare_error is not None:
                logger.info("Telegram 资源预检被拒绝 code=%s", prepare_error.code)
                _remove_callback_keyboard(bot, call.message)
                bot.edit_message_text(
                    "<b>无法准备资源提交</b>\n"
                    "资源可能已过期，或所选下载目标尚未就绪。请重新搜索后再试。",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="HTML",
                    reply_markup=None,
                )
                return

            _remove_callback_keyboard(bot, call.message)
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
            return

        if action_kind in {"cancel", "invoke_read_tool"}:
            _remove_callback_keyboard(bot, call.message)

        if action_kind in {"cancel", "confirm"}:
            # Telegram 网络调用永远放在 owner 临界区外；只有一次性 action 的
            # 消费、确认票据操作、受控写入与历史落库参与线性化。
            if action_kind == "confirm":
                bot.answer_callback_query(call.id, "正在执行，请稍候")
                callback_answered = True
                typing_heartbeat = _start_telegram_operation_typing(
                    bot, call.message, is_current=lambda: True
                )

            try:
                if action_kind == "confirm":
                    runtime_admission = _telegram_runtime_admission()
                else:
                    runtime_admission = nullcontext()
                # 确认 callback 必须先取得运行态准入，再一次性消费包装票据。
                # 否则开关切换恰好发生在 inspect 与 confirm 之间时，服务会拒绝
                # 执行，但 Telegram 的执行/取消按钮已经永久失效。
                with runtime_admission:
                    with coordinator.owner_window(owner):
                        action = store.resolve(action_id, owner=owner)
                        coordinator.invalidate_owner(
                            owner=owner, reason="controlled_action"
                        )
                        if action["action"] == "cancel":
                            service.discard_confirmation(
                                action["plan_id"],
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
                        else:
                            response = service.confirm(
                                action["plan_id"],
                                owner=owner,
                                request_id=_trace_operation_id(operation),
                                session_id=_telegram_history_identity(owner)[1],
                            )
                            confirmed_action_completed = True
                            text = render_agent_response(response)
                            markup = None
                            history_message = "确认执行待处理操作"
                            fallback_summary = "待处理操作已执行。"

                        _record_telegram_callback_conversation(
                            owner,
                            message=history_message,
                            response=response,
                            generation=history_generation,
                            fallback_summary=fallback_summary,
                        )
            finally:
                # 受控写入只在 owner_window 内显示 typing。无论服务成功或抛错，
                # 都在任何终态 edit/send/retry 之前停止旧 Topic 的输入状态。
                _stop_telegram_typing_heartbeat(typing_heartbeat)
                typing_heartbeat = None

            if action_kind == "confirm":
                _remove_callback_keyboard(bot, call.message)

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
                    operation, _ = coordinator.begin_with_context(
                        owner=owner,
                        operation_id=_telegram_callback_operation_id(
                            owner, call, action=action_kind
                        ),
                        initialize=lambda: invalidate_query_confirmation_epoch(
                            service, owner=owner
                        ),
                    )
            if not allowed:
                bot.answer_callback_query(
                    call.id, "请求过于频繁，请稍后重试", show_alert=True
                )
                return
            _remove_callback_keyboard(bot, call.message)
            bot.answer_callback_query(call.id, "正在执行，请稍候")
            callback_answered = True
        else:
            # resolve 与 begin 同样必须线性化，避免按钮在 inspect 后被新消息
            # 撤销，而旧 callback 随后仍创建 lease 并抢占新消息。
            with coordinator.owner_window(owner):
                action = store.resolve(action_id, owner=owner)
                operation, _ = coordinator.begin_with_context(
                    owner=owner,
                    operation_id=_telegram_callback_operation_id(
                        owner, call, action=action_kind
                    ),
                    initialize=lambda: invalidate_query_confirmation_epoch(
                        service, owner=owner
                    ),
                )

        if typing_heartbeat is None and operation is not None:
            typing_heartbeat = _start_telegram_operation_typing(
                bot,
                call.message,
                is_current=callback_operation_is_current,
            )

        if action["action"] == "invoke_read_tool":
            bot.answer_callback_query(call.id, "正在查询，请稍候")
            callback_answered = True
            state_buffer = AgentStateCommitBuffer(owner=owner)
            with defer_agent_state_commits(state_buffer):
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

            _stop_telegram_typing_heartbeat(typing_heartbeat)
            typing_heartbeat = None
            published = _publish_telegram_callback_response(
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
                state_buffer=state_buffer,
                runtime_generation=callback_runtime_generation,
                is_allowed=callback_operation_is_current,
            )
            if published is False:
                _cancel_telegram_runtime_operation(
                    coordinator=coordinator,
                    service=service,
                    operation=operation,
                    owner=owner,
                )
            return
        if action["action"] == "invoke_workspace_action":
            state_buffer = AgentStateCommitBuffer(owner=owner)
            with defer_agent_state_commits(state_buffer):
                response = service.invoke_workspace_action(
                    action["action_key"],
                    owner=owner,
                    rate_identity="",
                    request_id=_trace_operation_id(operation),
                    session_id=_telegram_history_identity(owner)[1],
                )
            label = sanitize_public_text(resolution.get("label"), limit=120) or "建议检查"
            _stop_telegram_typing_heartbeat(typing_heartbeat)
            typing_heartbeat = None
            published = _publish_telegram_callback_response(
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
                state_buffer=state_buffer,
                runtime_generation=callback_runtime_generation,
                is_allowed=callback_operation_is_current,
            )
            if published is False:
                _cancel_telegram_runtime_operation(
                    coordinator=coordinator,
                    service=service,
                    operation=operation,
                    owner=owner,
                )
            return
        raise ValueError("操作已过期或无效")
    except AgentRuntimeDisabled:
        # 确认/资源预检在消费前失败时保留原按钮；只读 callback 若已取得
        # operation，则只撤销仍属于它的发布、历史、按钮和确认世代。
        _cancel_telegram_runtime_operation(
            coordinator=coordinator,
            service=service,
            operation=operation,
            owner=owner,
        )
        _stop_telegram_typing_heartbeat(typing_heartbeat)
        typing_heartbeat = None
        if callback_answered:
            bot.reply_to(
                call.message,
                "Media Agent 状态已变化，本次未执行；重新启用后可再次点击原按钮。",
            )
        else:
            bot.answer_callback_query(
                call.id, "Media Agent 当前未启用，本次未执行", show_alert=True
            )
    except (AgentToolError, ValueError):
        if operation is not None and not coordinator.is_current(operation):
            _remove_callback_keyboard(bot, call.message)
            if not callback_answered:
                bot.answer_callback_query(
                    call.id, "操作已过期或无效", show_alert=True
                )
            return
        _stop_telegram_typing_heartbeat(typing_heartbeat)
        typing_heartbeat = None
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
            _remove_callback_keyboard(bot, call.message)
            bot.answer_callback_query(call.id, "操作已过期或无效", show_alert=True)
    except Exception as exc:
        logger.warning("Telegram Agent 确认失败 type=%s", type(exc).__name__)
        if operation is not None and not coordinator.is_current(operation):
            _remove_callback_keyboard(bot, call.message)
            if not callback_answered:
                bot.answer_callback_query(
                    call.id, "Agent 暂时不可用", show_alert=True
                )
            return
        _stop_telegram_typing_heartbeat(typing_heartbeat)
        typing_heartbeat = None
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
            _remove_callback_keyboard(bot, call.message)
            bot.answer_callback_query(call.id, "Agent 暂时不可用", show_alert=True)
    finally:
        if state_buffer is not None:
            state_buffer.discard()
        _stop_telegram_typing_heartbeat(typing_heartbeat)
        if operation is not None:
            coordinator.finish(operation)
