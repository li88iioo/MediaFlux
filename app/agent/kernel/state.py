"""统一会话状态、publication generation 与取消协调。"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.concurrency import CrossLoopAsyncLock


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
class CandidateSelectionGuard:
    generation: int
    ref: str
    expires_at: float
    batch_generation: int | None = None

    def check(self, state: SessionState) -> None:
        view = state.metadata.get("ux_candidate_view")
        if (
            state.generation != self.generation
            or self.expires_at <= time.time()
            or not isinstance(view, dict)
            or view.get("ref") != self.ref
            or view.get("generation") != (self.generation if self.batch_generation is None else self.batch_generation)
        ):
            raise SelectionInvalidError()


@dataclass(frozen=True, slots=True)
class AgentInput:
    message: str
    owner: str
    session_id: str
    request_id: str = ""
    channel: str = "api"
    reply_context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        message = str(self.message or "").strip()
        owner = str(self.owner or "").strip()
        session_id = str(self.session_id or "").strip()
        request_id = str(self.request_id or "").strip() or secrets.token_urlsafe(12)
        channel = str(self.channel or "api").strip().lower() or "api"
        if not message:
            raise ValueError("message cannot be empty")
        if not owner:
            raise ValueError("owner cannot be empty")
        if not session_id:
            raise ValueError("session_id cannot be empty")
        if len(message) > 12_000:
            raise ValueError("message is too long")
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "reply_context", deepcopy(dict(self.reply_context)))
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class PublicationLease:
    owner: str
    session_id: str
    generation: int
    turn_id: str
    request_id: str


def publication_matches(
    lease: PublicationLease, *, generation: int, confirmed: Any = None,
) -> bool:
    """普通回合按 generation；已认领确认还必须匹配持久化的执行 turn。

    不增加 generation，避免破坏已冻结票据。旧历史没有标记时继续兼容；
    新普通回合增加 generation 后，上一代确认标记自然失效。
    """
    if generation != lease.generation:
        return False
    if not isinstance(confirmed, Mapping) or confirmed.get("generation") != generation:
        return True
    return confirmed.get("turn_id") == lease.turn_id


def publication_commit_matches(
    lease: PublicationLease,
    state: SessionState,
    conversation: Sequence[Mapping[str, Any]] | None,
    updates: Sequence[StateUpdate],
) -> bool:
    """已原子领到当前待确认票据的下一步可接管发布权，旧读回合仍被隔离。"""
    claim = (
        conversation is None
        and len(updates) == 2
        and updates[0].mode == "set"
        and updates[0].key == "metadata.confirmed_publication"
        and updates[1].mode == "clear_if_equals"
        and updates[1].key == "pending_effect_plan_id"
    )
    value = updates[0].value if claim else None
    handoff = (
        isinstance(value, Mapping)
        and bool(state.pending_effect_plan_id)
        and str(updates[1].value or "") == state.pending_effect_plan_id
        and value
        == {
            "generation": lease.generation,
            "turn_id": lease.turn_id,
            "plan_id": state.pending_effect_plan_id,
        }
    )
    # 撤销/过期收束只做计划 ID 的原子条件清理，不接管已确认回执的发布权。
    pending_clear = (
        conversation is None and len(updates) == 1
        and updates[0].key == "pending_effect_plan_id"
        and updates[0].mode == "clear_if_equals"
    )
    confirmed = None if handoff or pending_clear or candidate_metadata_only(
        conversation, updates
    ) else state.metadata.get("confirmed_publication")
    return publication_matches(
        lease,
        generation=state.generation,
        confirmed=confirmed,
    )


@dataclass(frozen=True, slots=True)
class StateUpdate:
    key: str
    value: Any
    mode: str = "set"


def candidate_metadata_only(
    conversation: Sequence[Mapping[str, Any]] | None, updates: Sequence[StateUpdate],
) -> bool:
    # TG 候选 UI 使用专门的事务内 CAS，不是模型回合的 conversation 发布。
    # 只给这一窄契约例外，不能借 metadata 更新携带回执/计划覆盖。
    return (
        conversation is None and len(updates) == 1
        and updates[0].key == "metadata.ux_candidate_draft"
        and updates[0].mode == "compare_candidate"
    )


@dataclass(slots=True)
class SessionState:
    owner: str
    session_id: str
    generation: int = 0
    conversation: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    recent_refs: list[str] = field(default_factory=list)
    ref_kinds: set[str] = field(default_factory=set)
    pending_effect_plan_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def clone(self) -> SessionState:
        return SessionState(
            owner=self.owner,
            session_id=self.session_id,
            generation=self.generation,
            conversation=deepcopy(self.conversation),
            summary=self.summary,
            recent_refs=list(self.recent_refs),
            ref_kinds=set(self.ref_kinds),
            pending_effect_plan_id=self.pending_effect_plan_id,
            metadata=deepcopy(self.metadata),
        )

    def apply(self, updates: Sequence[StateUpdate]) -> None:
        for update in updates:
            key = str(update.key or "").strip()
            if not key:
                continue
            if key == "summary":
                self.summary = str(update.value or "")[:8_000]
            elif key == "pending_effect_plan_id":
                if update.mode == "clear_if_equals":
                    # 必须在 store.commit 的锁/事务内比较最新值；旧票据的
                    # 迟到取消或确认结果不能清掉同代次刚发布的新计划。
                    if self.pending_effect_plan_id == str(update.value or ""):
                        self.pending_effect_plan_id = ""
                else:
                    self.pending_effect_plan_id = str(update.value or "")[:200]
            elif key == "recent_refs":
                values = [str(item) for item in (update.value or []) if str(item)]
                if update.mode == "append":
                    self.recent_refs = (self.recent_refs + values)[-100:]
                else:
                    self.recent_refs = values[-100:]
            elif key == "ref_kinds":
                values = {str(item) for item in (update.value or []) if str(item)}
                self.ref_kinds = (
                    self.ref_kinds | values if update.mode == "append" else values
                )
            elif key == "metadata.ux_candidate_draft" and update.mode == "compare_candidate":
                value = update.value
                current = self.metadata.get("ux_candidate_draft") or {}
                view = self.metadata.get("ux_candidate_view") or {}
                if (
                    view.get("ref") != value["next"]["candidate_ref"]
                    or view.get("expires_at", 0) <= time.time()
                    or (value["expected"] is not None and current.get("handle") != value["expected"])
                ):
                    raise SelectionInvalidError("选择状态已更新，请使用当前消息中的按钮。")
                self.metadata["ux_candidate_draft"] = deepcopy(value["next"])
            elif key.startswith("metadata."):
                field_name = key.partition(".")[2]
                if field_name:
                    if update.mode == "delete":
                        self.metadata.pop(field_name, None)
                    else:
                        self.metadata[field_name] = deepcopy(update.value)


def retain_conversation(conversation: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """最多80条；优先当前请求与执行事实，原生tool问答不得从中间切开。"""
    if len(conversation) <= 80:
        return list(conversation)
    latest_user = next((i for i in range(len(conversation) - 1, -1, -1)
                        if conversation[i].get("role") == "user"), -1)
    groups: list[list[int]] = []
    pending_calls: set[str] = set()
    for i, row in enumerate(conversation):
        if row.get("role") == "tool" and row.get("tool_call_id") in pending_calls:
            groups[-1].append(i)
            pending_calls.discard(row["tool_call_id"])
        else:
            groups.append([i])
            pending_calls = {call.get("call_id") for call in (row.get("tool_calls") or ())}
    priority = sorted(groups, key=lambda group: (
        latest_user in group,
        any(i > latest_user and (conversation[i].get("effect_plan_id")
                                 or conversation[i].get("completion_receipt_id")) for i in group),
        group[-1],
    ), reverse=True)
    selected: list[int] = []
    for group in priority:
        if len(selected) + len(group) <= 80:
            selected.extend(group)
    return [conversation[i] for i in sorted(selected)]


def merge_effect_receipts(
    conversation: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """按 plan ID 将已完成回执并入会话，不做文本近似匹配。"""
    merged = [deepcopy(dict(message)) for message in conversation]
    waits = metadata.get("effect_waits")
    if not isinstance(waits, Mapping):
        return merged

    for plan_id, record in waits.items():
        if not isinstance(plan_id, str) or not plan_id or not isinstance(record, Mapping):
            continue
        receipt = record.get("receipt_message")
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("completion_receipt_id") != plan_id
        ):
            continue

        matches = [
            index for index, message in enumerate(merged)
            if message.get("completion_receipt_id") == plan_id
        ]
        replacement = deepcopy(dict(receipt))
        if matches:
            # 确认后的模型答复可能已概述“仍在执行”。晚到终态必须位于它
            # 之后，否则公开投影会把更新后的系统回执当中间结果折叠掉。
            has_later_answer = any(
                message.get("role") == "user" or (
                    message.get("role") == "assistant"
                    and message.get("effect_plan_id") == plan_id
                    and not message.get("tool_name")
                )
                for message in merged[matches[0] + 1:]
            )
            if has_later_answer:
                for index in reversed(matches):
                    del merged[index]
                merged.append(replacement)
            else:
                merged[matches[0]] = replacement
                for index in reversed(matches[1:]):
                    del merged[index]
        else:
            merged.append(replacement)
    return merged


def _consume_delivered_effect_receipts(state: SessionState) -> None:
    waits = state.metadata.get("effect_waits")
    if not isinstance(waits, Mapping):
        state.metadata.pop("effect_waits", None)
        state.metadata.pop("effect_next_poll_at", None)
        return

    delivered = {
        plan_id: record
        for plan_id, record in waits.items()
        if isinstance(plan_id, str)
        and isinstance(record, Mapping)
        and record.get("delivered") is True
    }
    if delivered:
        state.conversation = retain_conversation(merge_effect_receipts(
            state.conversation, {"effect_waits": delivered},
        ))

    remaining = {
        plan_id: deepcopy(record)
        for plan_id, record in waits.items()
        if plan_id not in delivered
    }
    if remaining:
        state.metadata["effect_waits"] = remaining
        next_polls = [
            record.get("next_poll_at")
            for record in remaining.values()
            if isinstance(record, Mapping)
            and isinstance(record.get("next_poll_at"), (int, float))
            and not isinstance(record.get("next_poll_at"), bool)
        ]
        if next_polls:
            state.metadata["effect_next_poll_at"] = min(next_polls)
        else:
            state.metadata.pop("effect_next_poll_at", None)
    else:
        state.metadata.pop("effect_waits", None)
        state.metadata.pop("effect_next_poll_at", None)


def _effect_time_is_due(value: Any, now: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value <= now
    )


def _apply_effect_state_change(
    state: SessionState, change: Callable[[SessionState], Any],
) -> tuple[SessionState, Any, bool]:
    """在副本上执行纯同步回调，并固定 publication 身份字段。"""
    updated = state.clone()
    result = change(updated)
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise TypeError("effect state change must be synchronous")
    updated.owner = state.owner
    updated.session_id = state.session_id
    updated.generation = state.generation
    updated.pending_effect_plan_id = state.pending_effect_plan_id
    return updated, result, updated != state


class SessionStateStore(Protocol):
    async def begin_turn(
        self, *, owner: str, session_id: str, request_id: str,
        selection_guard: CandidateSelectionGuard | None = None,
    ) -> tuple[PublicationLease, SessionState]: ...

    async def is_current(self, lease: PublicationLease) -> bool: ...

    async def commit(
        self,
        lease: PublicationLease,
        *,
        conversation: Sequence[Mapping[str, Any]] | None = None,
        updates: Sequence[StateUpdate] = (),
    ) -> SessionState: ...

    async def update_effect_state(
        self, *, owner: str, session_id: str,
        change: Callable[[SessionState], Any],
    ) -> Any: ...

    async def due_effect_waits(self, *, limit: int = 16) -> list[dict[str, Any]]: ...

    async def load(self, *, owner: str, session_id: str) -> SessionState: ...


class InMemorySessionStateStore:
    """测试与单进程运行使用的权威状态实现。"""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._lock = CrossLoopAsyncLock()
        self._states: dict[tuple[str, str], SessionState] = {}
        self._clock = clock

    async def begin_turn(
        self, *, owner: str, session_id: str, request_id: str,
        selection_guard: CandidateSelectionGuard | None = None,
    ) -> tuple[PublicationLease, SessionState]:
        key = (owner, session_id)
        async with self._lock:
            current = self._states.get(key)
            generation = (current.generation if current else 0) + 1
            state = (
                current.clone()
                if current
                else SessionState(owner=owner, session_id=session_id)
            )
            if selection_guard is not None:
                selection_guard.check(state)
            _consume_delivered_effect_receipts(state)
            state.generation = generation
            self._states[key] = state
            lease = PublicationLease(
                owner=owner,
                session_id=session_id,
                generation=generation,
                turn_id=secrets.token_urlsafe(12),
                request_id=request_id,
            )
            return lease, state.clone()

    async def is_current(self, lease: PublicationLease) -> bool:
        async with self._lock:
            state = self._states.get((lease.owner, lease.session_id))
            return bool(state and publication_matches(
                lease, generation=state.generation,
                confirmed=state.metadata.get("confirmed_publication"),
            ))

    async def commit(
        self,
        lease: PublicationLease,
        *,
        conversation: Sequence[Mapping[str, Any]] | None = None,
        updates: Sequence[StateUpdate] = (),
    ) -> SessionState:
        key = (lease.owner, lease.session_id)
        async with self._lock:
            state = self._states.get(key)
            if state is None or not publication_commit_matches(lease, state, conversation, updates):
                raise StalePublicationError("turn no longer owns publication authority")
            if conversation is not None:
                state.conversation = retain_conversation(merge_effect_receipts(
                    conversation, state.metadata,
                ))
            state.apply(updates)
            return state.clone()

    async def update_effect_state(
        self, *, owner: str, session_id: str,
        change: Callable[[SessionState], Any],
    ) -> Any:
        key = (owner, session_id)
        async with self._lock:
            current = self._states.get(key)
            if current is None:
                return None
            updated, result, changed = _apply_effect_state_change(current, change)
            if changed:
                self._states[key] = updated
            return result

    async def due_effect_waits(self, *, limit: int = 16) -> list[dict[str, Any]]:
        maximum = max(0, int(limit))
        if not maximum:
            return []
        now = self._clock()
        async with self._lock:
            due: list[tuple[float, str, dict[str, Any]]] = []
            for state in self._states.values():
                if not _effect_time_is_due(state.metadata.get("effect_next_poll_at"), now):
                    continue
                waits = state.metadata.get("effect_waits")
                if not isinstance(waits, Mapping):
                    continue
                for plan_id, record in waits.items():
                    if (
                        not isinstance(record, Mapping)
                        or not _effect_time_is_due(record.get("next_poll_at"), now)
                    ):
                        continue
                    item = deepcopy(dict(record))
                    item["plan_id"] = plan_id
                    due.append((float(record["next_poll_at"]), str(plan_id), item))
            due.sort(key=lambda item: (item[0], item[1]))
            return [item[2] for item in due[:maximum]]

    async def load(self, *, owner: str, session_id: str) -> SessionState:
        async with self._lock:
            state = self._states.get((owner, session_id))
            return (
                state.clone()
                if state
                else SessionState(owner=owner, session_id=session_id)
            )


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
        self._reason = str(reason or "cancelled")[:200]
        self._event.set()
        if self.interruptible and task is not None and not task.done() and not self._task_cancel_requested:
            self._task_cancel_requested = True  # 重复取消不能打断状态/回执收尾。
            task.get_loop().call_soon_threadsafe(task.cancel)

    async def wait(self) -> None:
        while not self._event.is_set():
            await asyncio.sleep(0.01)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError(self.reason)


class TurnCoordinator:
    """同一 owner/session 的最新回合拥有唯一发布权。"""

    def __init__(self) -> None:
        self._lock = CrossLoopAsyncLock()
        self._active: dict[tuple[str, str], tuple[PublicationLease, CancellationToken, bool, asyncio.Task[Any] | None]] = {}

    async def begin(
        self,
        lease: PublicationLease,
        *,
        protected: bool = False,
        task: asyncio.Task[Any] | None = None,
    ) -> CancellationToken:
        key = (lease.owner, lease.session_id)
        token = CancellationToken()
        async with self._lock:
            previous = self._active.get(key)
            if previous is not None:
                if previous[2]:
                    raise SessionBusyError("confirmed effect is executing")
                previous[1].cancel("superseded", previous[3])
            self._active[key] = (lease, token, bool(protected), task)
        return token

    async def cancel(
        self, *, owner: str, session_id: str, reason: str = "cancelled", request_id: str = ""
    ) -> bool:
        async with self._lock:
            current = self._active.get((owner, session_id))
            if current is None or current[2] or (
                request_id and current[0].request_id != request_id
            ):
                return False
            current[1].cancel(reason, current[3])
            return True

    def _owned_turn(self, lease: PublicationLease, token: CancellationToken):
        current = self._active.get((lease.owner, lease.session_id))
        return current if current and current[0].turn_id == lease.turn_id and current[1] is token else None

    async def unprotect(self, lease: PublicationLease, token: CancellationToken) -> None:
        """真实写入与回执持久化后，后续规划恢复为可停止的普通回合。"""
        async with self._lock:
            if current := self._owned_turn(lease, token):
                self._active[(lease.owner, lease.session_id)] = (lease, token, False, current[3])

    async def describe(self, *, owner: str, session_id: str) -> dict[str, Any] | None:
        """只读观察现有轮次；不取得发布权或启动执行。"""
        async with self._lock:
            current = self._active.get((owner, session_id))
            if current is None:
                return None
            return {
                "request_id": current[0].request_id,
                "turn_id": current[0].turn_id,
                "generation": current[0].generation,
                "protected": current[2],
                "status": "cancelling" if current[1].cancelled else "running",
            }

    async def has_protected_turn(self, *, owner: str, session_id: str) -> bool:
        async with self._lock:
            current = self._active.get((owner, session_id))
            return bool(current and current[2])

    async def is_current(self, lease: PublicationLease, token: CancellationToken) -> bool:
        async with self._lock:
            return bool(self._owned_turn(lease, token) and not token.cancelled)

    async def finish(self, lease: PublicationLease, token: CancellationToken) -> None:
        async with self._lock:
            if self._owned_turn(lease, token):
                self._active.pop((lease.owner, lease.session_id), None)
