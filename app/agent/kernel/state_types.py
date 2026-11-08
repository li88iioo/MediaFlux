"""会话输入、发布租约、原子更新与取消票据的低层契约。"""
from __future__ import annotations

import asyncio
import json
import secrets
import threading
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .conversation_codec import PendingText, json_text, pending_content


class KernelStateError(RuntimeError):
    pass


class StalePublicationError(KernelStateError):
    """当前回合已失去发布权，禁止提交迟到状态。"""


class SessionBusyError(KernelStateError):
    """已确认的副作用正在执行；新回合不能抢占。"""


class SelectionInvalidError(KernelStateError):
    """候选输入在取得新回合前失效，不得改变原会话。"""

    def __init__(self, message: str = "候选已失效或不属于当前会话，请重新搜索后选择。") -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AgentInput:
    message: str
    owner: str
    session_id: str
    request_id: str = ""
    channel: str = "api"
    reply_context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    delivery_route: Mapping[str, Any] = field(default_factory=dict)
    initial_read_tool: str = ""  # 服务端固定接力；整个回合仅允许 READ，不是 quota 恢复。
    initial_read_arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        message = (self.message or "").strip()
        owner = (self.owner or "").strip()
        session_id = (self.session_id or "").strip()
        request_id = (self.request_id or "").strip() or secrets.token_urlsafe(12)
        channel = (self.channel or "api").strip().lower() or "api"
        if not message:
            raise ValueError("message cannot be empty")
        if not owner:
            raise ValueError("owner cannot be empty")
        if not session_id:
            raise ValueError("session_id cannot be empty")
        if len(message) > 12_000:
            raise ValueError("message is too long")
        if not isinstance(self.initial_read_tool, str):
            raise ValueError("initial_read_tool must be a string")
        if not isinstance(self.initial_read_arguments, Mapping):
            raise ValueError("initial_read_arguments must be a mapping")
        if self.initial_read_arguments and not self.initial_read_tool.strip():
            raise ValueError("read arguments require a handoff tool")
        if self.initial_read_tool.strip() and "selection" in self.metadata:
            raise ValueError("read handoff cannot include selection")
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "reply_context", deepcopy(dict(self.reply_context)))
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata)))
        object.__setattr__(self, "delivery_route", deepcopy(dict(self.delivery_route)))
        object.__setattr__(self, "initial_read_tool", self.initial_read_tool.strip())
        object.__setattr__(self, "initial_read_arguments", deepcopy(dict(self.initial_read_arguments)))


@dataclass(frozen=True, slots=True)
class PublicationLease:
    owner: str
    session_id: str
    generation: int
    turn_id: str
    request_id: str
    continuation_id: str = ""
    continuation_claim_id: str = ""
    continuation_kind: str = "read_quota"


@dataclass(frozen=True, slots=True)
class StateUpdate:
    key: str
    value: Any
    mode: str = "set"


@dataclass(frozen=True, slots=True)
class _PublicationFinished(StateUpdate):
    """Executor terminal transition, not a business state update."""


@dataclass(frozen=True, slots=True)
class _ConfirmationClaim(StateUpdate):
    """原子消费当前确认并接管执行身份；不能从业务 JSON 构造。"""

    request_id: str = ""


@dataclass(frozen=True, slots=True)
class _PublicationUnprotected(StateUpdate):
    """真实写入和回执落盘后解除当前执行者的保护。"""


@dataclass(frozen=True, slots=True)
class _PendingEffectTransition(StateUpdate):
    """Internal atomic ID/binding/goal transition; never built from business JSON."""

    plan_generation: int = 0
    cancelled: bool = False
    goal: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _ScopedTrustTransition(StateUpdate):
    """仅显式用户入口可更改的会话预授权，不能从工具 JSON 重建。"""


@dataclass(frozen=True, slots=True)
class _ExecutionCapacity(StateUpdate):
    """Executor-only stage update; never reconstructed from business JSON."""

    current_start: int | None = None
    receipt_plan_id: str = ""  # 只读诊断可裁剪；该回执及此前执行事实仍须完整保留。


class HistoryCapacityError(KernelStateError):
    """Retention would discard the executor's current call/result group."""


class CancellationToken:
    __slots__ = ("_event", "_reason", "_task_cancel_requested", "interruptible")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""
        self._task_cancel_requested = False
        self.interruptible = False

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason or "cancelled"

    def cancel(self, reason: str = "cancelled", task: asyncio.Task[Any] | None = None) -> None:
        self._reason = (reason or "cancelled")[:200]
        self._event.set()
        if self.interruptible and task is not None and not task.done() and not self._task_cancel_requested:
            self._task_cancel_requested = True  # 重复取消不能打断状态/回执收尾。
            task.get_loop().call_soon_threadsafe(self._interrupt, task)

    def _interrupt(self, task: asyncio.Task[Any]) -> None:
        # 在交付时再检查阶段：模型可能已观察取消并进入终态持久化。
        if self.interruptible and not task.done():
            task.cancel()

    async def wait(self) -> None:
        while not self._event.is_set():
            await asyncio.sleep(0.01)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError(self.reason)


def _exact_value(value: Any) -> Any:
    """Immutable typed comparison; bool/int and native payloads stay distinct."""
    if isinstance(value, dict):
        return (type(value), tuple(sorted((key, _exact_value(item)) for key, item in value.items())))
    if isinstance(value, (list, tuple)):
        return (type(value), tuple(_exact_value(item) for item in value))
    if type(value) not in (type(None), str, int, float, bool, PendingText):
        raise TypeError("checkpoint must contain immutable JSON values")
    return (type(value), value)


@dataclass(frozen=True, slots=True)
class _CancelledReadInline:
    """Invocation-private checkpoint and put-once bytes, never a handler DTO."""

    lease: PublicationLease
    expected: Any
    assistant_index: int
    call_index: int
    tool_index: int
    call: Any
    tool: Any = field(repr=False)
    _content: str | None = field(default=None, init=False)

    @classmethod
    def capture(cls, lease, conversation, call_index, *, tool):
        from .capabilities import ToolEffect
        if tool.effect is not ToolEffect.READ:
            raise ValueError("only catalog READ invocations may retain returned facts")
        # Receipts can be merged after the current native batch. Locate its
        # assistant row, not the conversation tail or a reusable historical call ID.
        assistant_index = next(index for index in range(len(conversation) - 1, -1, -1)
                               if conversation[index].get("role") == "assistant"
                               and conversation[index].get("tool_calls"))
        call = conversation[assistant_index]["tool_calls"][call_index]
        return cls(lease, _exact_value(conversation), assistant_index, call_index,
                   assistant_index + 1 + call_index, _exact_value(call), tool)

    def at_checkpoint(self, conversation):
        """只绑定同一原生调用在执行器最新检查点中的待结算槽。"""
        try:
            current = self.capture(self.lease, conversation, self.call_index, tool=self.tool)
            row = conversation[current.tool_index]
            if (current.call != self.call or row.get("tool_call_id") != conversation[current.assistant_index]["tool_calls"][self.call_index].get("call_id")
                    or _exact_value(row.get("content")) != _exact_value(pending_content(True))):
                return None
        except (ValueError, StopIteration, KeyError, IndexError, TypeError):
            return None
        if self._content:
            current.put_once(self._content)
        return current

    def reject_incomplete(self) -> None:
        if self._content is not None:
            raise ValueError("returned read already captured")
        object.__setattr__(self, "_content", "")

    def put_once(self, content: str) -> None:
        if self._content is not None:
            raise ValueError("returned read already captured")
        # Copy the full, already-safe JSON, never retain mutable domain aliases.
        object.__setattr__(self, "_content", json_text(json.loads(content)))
