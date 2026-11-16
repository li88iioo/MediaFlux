"""会话仓储、publication 生命周期与进程内回合协调。"""
from __future__ import annotations

import asyncio
import inspect
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from app.concurrency import CrossLoopAsyncLock

from .conversation_codec import conversation_rows, json_text, pending_content, state_storage_payload
from .conversation_state import commit_conversation
from .session_state import (
    CandidateSelectionGuard,
    PendingEffectGuard,
    SessionState,
    publication_commit_matches,
    publication_matches,
    validate_execution_state,
)
from .state_types import (
    CancellationToken,
    PublicationLease,
    SessionBusyError,
    StalePublicationError,
    StateUpdate,
    _CancelledReadInline,
    _exact_value,
    _ExecutionCapacity,
    _PendingEffectTransition,
)
from .task_state import TaskPatch, reduce_task


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
        commit_conversation(state, state.conversation, receipt_metadata={"effect_waits": delivered})

    from .completion_receipts import effect_active
    remaining = {
        plan_id: deepcopy(record)
        for plan_id, record in waits.items()
        if plan_id not in delivered or effect_active(record)
    }
    if remaining:
        state.metadata["effect_waits"] = remaining
        next_polls = [
            record["next_poll_at"]
            for record in remaining.values()
            if isinstance(record, Mapping)
            and not record.get("delivered")
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
    from .continuation import _DeliveryAcknowledged
    reserved_fields = ("pending_effect_binding", "pending_effect_goal", "confirmed_publication", "active_publication", "scoped_trust", "read_wait")
    for reserved in reserved_fields + (() if type(change) is _DeliveryAcknowledged else ("continuation_deliveries",)):
        if (reserved in updated.metadata) != (reserved in state.metadata) or updated.metadata.get(reserved) != state.metadata.get(reserved):
            raise ValueError("effect callback cannot change publication binding")
    if ("task" in updated.metadata) != ("task" in state.metadata) or updated.metadata.get("task") != state.metadata.get("task"):
        raise ValueError("effect callback cannot change task")
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise TypeError("effect state change must be synchronous")
    updated.owner = state.owner
    updated.session_id = state.session_id
    updated.generation = state.generation
    updated.pending_effect_plan_id = state.pending_effect_plan_id
    updated.execution_capacity = state.execution_capacity
    updated.lifecycle_id = state.lifecycle_id
    return updated, result, updated != state


def _record_stop_request(state: SessionState, generation: int) -> bool:
    """停止意图不抢占确认持有者；同代确认提交后读取此栅栏。"""
    if type(generation) is not int or generation <= 0 or state.generation != generation:
        return False
    state.metadata["stop_requested_generation"] = generation
    return True


def _begin_publication(state: SessionState, request_id: str, turn_id: str) -> None:
    """Internal durable request identity, never copied from model metadata."""
    from .continuation import revoke
    revoke(state, "superseded")
    state.metadata["active_publication"] = {
        "generation": state.generation, "turn_id": turn_id, "request_id": request_id, "status": "running",
    }


def _cancel_publication(state: SessionState, request_id: str) -> bool:
    active = state.metadata.get("active_publication")
    if (not isinstance(active, dict) or active.get("generation") != state.generation
            or active.get("status") != "running"
            or (request_id and request_id != active.get("request_id"))
            or active.get("protected") is True):
        return False
    state.metadata["active_publication"] = {**active, "status": "cancelled"}
    from .continuation import revoke
    revoke(state, "cancelled")
    state.generation += 1
    return True


def cancelled_publication_matches(state: SessionState, lease: PublicationLease) -> bool:
    marker = state.metadata.get("active_publication") or {}
    return ((state.owner, state.session_id) == (lease.owner, lease.session_id)
            and state.generation == lease.generation + 1
            and marker.get("protected") is not True
            and _exact_value({key: marker.get(key) for key in ("generation", "turn_id", "request_id", "status")})
            == _exact_value({"generation": lease.generation, "turn_id": lease.turn_id,
                             "request_id": lease.request_id, "status": "cancelled"}))


def _settle_cancelled_reads(state, receipts, maximum=256 * 1024):
    """以同一个取消检查点原子保存已返回的只读事实，不接纳新调用或新发布。"""
    if not isinstance(receipts, tuple) or not receipts or any(
        type(receipt) is not _CancelledReadInline or not receipt._content for receipt in receipts
    ):
        return None
    lease = receipts[0].lease
    if not cancelled_publication_matches(state, lease):
        return None
    expected = _exact_value(state.conversation)
    updated = state.clone()
    positions = set()
    for receipt in receipts:
        if receipt.lease != lease or receipt.expected != expected or receipt.tool_index in positions:
            return None
        try:
            assistant = state.conversation[receipt.assistant_index]
            call = assistant["tool_calls"][receipt.call_index]
            row = state.conversation[receipt.tool_index]
            if (not 0 <= receipt.assistant_index < receipt.tool_index < len(state.conversation)
                    or receipt.tool_index != receipt.assistant_index + 1 + receipt.call_index
                    or not 0 <= receipt.call_index < len(assistant["tool_calls"])
                    or assistant.get("role") != "assistant" or _exact_value(call) != receipt.call
                    or any(item.get("role") != "tool" for item in state.conversation[receipt.assistant_index + 1:receipt.tool_index])
                    or row.get("role") != "tool"
                    or row.get("tool_call_id") != call.get("call_id")
                    or row.get("tool_name") != call.get("name")
                    or _exact_value(row.get("content")) != _exact_value(pending_content(True))):
                return None
        except (KeyError, IndexError, TypeError):
            return None
        positions.add(receipt.tool_index)
        updated.conversation[receipt.tool_index]["content"] = receipt._content
    payload = state_storage_payload(updated.to_payload(), owner=updated.owner)
    if (_exact_value(conversation_rows(payload["conversation"])) != _exact_value(updated.conversation)
            or len(json_text(payload).encode()) > maximum):
        raise ValueError("cancelled read settlement exceeds lossless state capacity")
    return updated


class InMemorySessionStateStore:
    """测试与单进程运行使用的权威状态实现。"""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._lock = CrossLoopAsyncLock()
        self._states: dict[tuple[str, str], SessionState] = {}
        self._clock = clock

    async def begin_turn(
        self, *, owner: str, session_id: str, request_id: str,
        selection_guard: CandidateSelectionGuard | None = None,
        pending_guard: PendingEffectGuard | None = None,
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
            if pending_guard is not None:
                pending_guard.check(state)
            else:
                # Only a checked pending guard may carry a frozen plan across
                # discussion generations; it never changes the approved scope.
                state.metadata.pop("pending_effect_binding", None)
            _consume_delivered_effect_receipts(state)
            state.generation = generation
            turn_id = secrets.token_urlsafe(12)
            _begin_publication(state, request_id, turn_id)
            if state.execution_capacity or pending_guard is not None:
                validate_execution_state(state)
            self._states[key] = state
            lease = PublicationLease(
                owner=owner,
                session_id=session_id,
                generation=generation,
                turn_id=turn_id,
                request_id=request_id,
            )
            return lease, state.clone()

    async def cancel_publication(self, *, owner: str, session_id: str, request_id: str) -> bool:
        async with self._lock:
            state = self._states.get((owner, session_id))
            if state is None:
                return False
            updated = state.clone()
            if not _cancel_publication(updated, request_id):
                return False
            self._states[(owner, session_id)] = updated
            return True

    async def settle_cancelled_reads(self, receipts: tuple[_CancelledReadInline, ...]) -> bool:
        if not isinstance(receipts, tuple) or not receipts or type(receipts[0]) is not _CancelledReadInline:
            return False
        key = (receipts[0].lease.owner, receipts[0].lease.session_id)
        async with self._lock:
            state = self._states.get(key)
            updated = _settle_cancelled_reads(state, receipts) if state is not None else None
            if updated is None:
                return False
            validate_execution_state(updated)
            self._states[key] = updated
            return True

    async def is_current(self, lease: PublicationLease) -> bool:
        async with self._lock:
            state = self._states.get((lease.owner, lease.session_id))
            if state and lease.continuation_id:
                from .continuation import check_claim
                try:
                    check_claim(state, lease, self._clock(), publication=True)
                except StalePublicationError:
                    return False
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
        task_patch: TaskPatch | None = None,
    ) -> SessionState:
        key = (lease.owner, lease.session_id)
        async with self._lock:
            state = self._states.get(key)
            if state is None or not publication_commit_matches(lease, state, conversation, updates):
                raise StalePublicationError("turn no longer owns publication authority")
            state = state.clone()
            from .continuation import apply_actions, check_claim, task_changed
            check_claim(state, lease, self._clock())
            if task_patch is not None:
                state.metadata["task"] = reduce_task(state.metadata.get("task"), task_patch, owner=state.owner, session_id=state.session_id)
                task_changed(state)
            if conversation is not None:
                commit_conversation(state, conversation, updates)
            apply_actions(state, lease, updates, self._clock())
            state.apply(updates)
            if task_patch is not None or state.execution_capacity or any(type(update) in (_ExecutionCapacity, _PendingEffectTransition) for update in updates):
                validate_execution_state(state)
            validated = state.clone()
            self._states[key] = validated
            return validated.clone()

    def update_effect_state_in_transaction(
        self, conn: Any, *, owner: str, session_id: str,
        change: Callable[[SessionState], Any],
    ) -> Any:
        raise RuntimeError("持久队列回执需要与队列共用 SQLite 会话存储")

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
                if updated.execution_capacity:
                    validate_execution_state(updated)
                self._states[key] = updated.clone()
            return result

    async def due_continuations(self, *, limit: int = 16) -> list[dict[str, Any]]:
        from .continuation import due_records
        async with self._lock:
            records = [record for state in self._states.values() for record in due_records(state.metadata, self._clock())]
            return sorted(records, key=lambda r: r["due_at"])[:max(0, min(16, limit))]

    async def request_stop_generation(self, *, owner: str, session_id: str, generation: int) -> bool:
        async with self._lock:
            state = self._states.get((owner, session_id))
            return _record_stop_request(state, generation) if state is not None else False

    async def claim_continuation(self, candidate, binding):
        from .continuation import claim
        async with self._lock:
            key = candidate['owner'], candidate['session_id']
            current = self._states.get(key)
            if current is None:
                return None
            state = current.clone()
            lease = claim(state, candidate, self._clock(), binding)
            self._states[key] = state
            return (lease, state.clone()) if lease else None

    async def due_effect_waits(self, *, limit: int = 16) -> list[dict[str, Any]]:
        maximum = max(0, limit)
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
                        or record.get("delivered") is True
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
                else SessionState(owner=owner, session_id=session_id, lifecycle_id="")
            )


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
            self._active[key] = (lease, token, protected, task)
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

    async def protect(self, lease: PublicationLease, token: CancellationToken) -> None:
        async with self._lock:
            current = self._owned_turn(lease, token)
            if current is None:
                raise StalePublicationError("turn lost execution authority")
            self._active[(lease.owner, lease.session_id)] = (lease, token, True, current[3])

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
