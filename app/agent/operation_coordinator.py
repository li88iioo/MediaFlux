"""Agent 长操作的进程内生命周期协调。

当前 Web 与 Telegram 都在同一应用进程内执行。协调器为每个身份维护一个
“最新操作”，提供显式取消、会话失效、发布门和最终提交门：旧请求可以继续在
后台完成不可中断的同步调用，但不能再发布内容、创建操作票据或写入会话历史。
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

_T = TypeVar("_T")


class AgentOperationCancelled(RuntimeError):
    """操作已被取消或被同一 owner 的更新请求取代。"""


@dataclass(slots=True)
class _OperationState:
    owner: str
    operation_id: str
    generation: int
    status: str = "active"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class AgentOperationLease:
    """一次 Agent 操作的不可伪造进程内租约。"""

    owner: str
    operation_id: str
    generation: int
    _state: _OperationState


class AgentOperationCoordinator:
    """按 owner 串行化发布，并让最新操作拥有唯一发布权。"""

    _OWNER_LOCK_STRIPES = 64

    def __init__(
        self,
        *,
        cancellation_ttl_seconds: float = 60.0,
        max_cancellation_entries: int = 2048,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._cancellation_ttl_seconds = max(1.0, float(cancellation_ttl_seconds))
        self._max_cancellation_entries = max(32, int(max_cancellation_entries))
        self._lock = threading.RLock()
        # 固定分片锁避免客户端可控 owner 造成每 owner 锁永久增长；同一 owner
        # 始终落入同一把锁，不同 owner 仅在哈希碰撞时短暂串行。
        self._owner_locks = tuple(
            threading.RLock() for _ in range(self._OWNER_LOCK_STRIPES)
        )
        self._active: dict[str, _OperationState] = {}
        self._generations: dict[str, int] = {}
        self._pre_cancelled: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()

    @staticmethod
    def _owner(value: str) -> str:
        owner = str(value or "").strip()
        if not owner:
            raise ValueError("Agent operation owner 不能为空")
        return owner

    @staticmethod
    def _operation_id(value: str | None) -> str:
        operation_id = str(value or "").strip()
        if not operation_id:
            operation_id = secrets.token_urlsafe(18)
        if len(operation_id) > 128:
            raise ValueError("Agent operation id 过长")
        return operation_id

    def _owner_lock(self, owner: str) -> threading.RLock:
        return self._owner_locks[hash(owner) % len(self._owner_locks)]

    def _reclaim_generation_locked(self, owner: str) -> None:
        if owner not in self._active:
            self._generations.pop(owner, None)

    def _prune_pre_cancelled_locked(self, now: float) -> None:
        expired = [
            key for key, (expires_at, _reason) in self._pre_cancelled.items()
            if expires_at <= now
        ]
        for key in expired:
            self._pre_cancelled.pop(key, None)
        while len(self._pre_cancelled) > self._max_cancellation_entries:
            self._pre_cancelled.popitem(last=False)

    def _begin(
        self,
        *,
        owner: str,
        operation_id: str | None,
        initialize: Callable[[], _T] | None,
    ) -> tuple[AgentOperationLease, _T | None]:
        owner_key = self._owner(owner)
        request_key = self._operation_id(operation_id)
        owner_lock = self._owner_lock(owner_key)
        with owner_lock:
            with self._lock:
                now = self._clock()
                self._prune_pre_cancelled_locked(now)
                previous = self._active.pop(owner_key, None)
                if previous is not None and previous.status in {"active", "finalizing"}:
                    previous.status = "cancelled"
                    previous.reason = "superseded"
                generation = self._generations.get(owner_key, 0) + 1
                self._generations[owner_key] = generation
                state = _OperationState(owner_key, request_key, generation)
                remembered = self._pre_cancelled.pop((owner_key, request_key), None)
                if remembered is not None:
                    state.status = "cancelled"
                    state.reason = remembered[1] or "cancelled"
                    self._reclaim_generation_locked(owner_key)
                    return AgentOperationLease(owner_key, request_key, generation, state), None
                self._active[owner_key] = state

            lease = AgentOperationLease(owner_key, request_key, generation, state)
            if initialize is None:
                return lease, None
            try:
                initialized = initialize()
            except Exception:
                with self._lock:
                    if self._active.get(owner_key) is state:
                        self._active.pop(owner_key, None)
                    state.status = "cancelled"
                    state.reason = "initialization_failed"
                    self._reclaim_generation_locked(owner_key)
                raise
            return lease, initialized

    def begin(self, *, owner: str, operation_id: str | None = None) -> AgentOperationLease:
        lease, _ = self._begin(
            owner=owner,
            operation_id=operation_id,
            initialize=None,
        )
        return lease

    def begin_with_context(
        self,
        *,
        owner: str,
        operation_id: str | None = None,
        initialize: Callable[[], _T],
    ) -> tuple[AgentOperationLease, _T | None]:
        """在线性化的 owner 窗口内启动操作并初始化其短期上下文。"""
        return self._begin(
            owner=owner,
            operation_id=operation_id,
            initialize=initialize,
        )

    def is_current(self, lease: AgentOperationLease) -> bool:
        with self._lock:
            return (
                lease._state.status == "active"
                and self._active.get(lease.owner) is lease._state
            )

    def reason(self, lease: AgentOperationLease) -> str:
        with self._lock:
            return lease._state.reason or (
                "cancelled" if lease._state.status == "cancelled" else ""
            )

    def cancel(
        self,
        *,
        owner: str,
        operation_id: str,
        reason: str = "cancelled",
        remember: bool = True,
        invalidate: Callable[[], object] | None = None,
    ) -> bool:
        """取消匹配操作；可记住先于 query 抵达的取消请求。"""
        owner_key = self._owner(owner)
        request_key = self._operation_id(operation_id)
        owner_lock = self._owner_lock(owner_key)
        with owner_lock:
            accepted = False
            with self._lock:
                now = self._clock()
                self._prune_pre_cancelled_locked(now)
                current = self._active.get(owner_key)
                if current is not None and secrets.compare_digest(
                    current.operation_id, request_key
                ):
                    current.status = "cancelled"
                    current.reason = str(reason or "cancelled")
                    self._active.pop(owner_key, None)
                    accepted = True
                elif remember:
                    self._pre_cancelled[(owner_key, request_key)] = (
                        now + self._cancellation_ttl_seconds,
                        str(reason or "cancelled"),
                    )
                    self._pre_cancelled.move_to_end((owner_key, request_key))
                    self._prune_pre_cancelled_locked(now)
                    accepted = True
                self._reclaim_generation_locked(owner_key)
            if accepted and invalidate is not None:
                invalidate()
            return accepted

    def invalidate_owner(
        self,
        *,
        owner: str,
        reason: str = "reset",
        invalidate: Callable[[], object] | None = None,
    ) -> bool:
        owner_key = self._owner(owner)
        owner_lock = self._owner_lock(owner_key)
        with owner_lock:
            with self._lock:
                current = self._active.pop(owner_key, None)
                if current is not None:
                    current.status = "cancelled"
                    current.reason = str(reason or "reset")
                self._reclaim_generation_locked(owner_key)
            if invalidate is not None:
                invalidate()
            return current is not None

    @contextmanager
    def owner_window(self, owner: str) -> Iterator[None]:
        """串行化同一 owner 的短期受控操作，但不改变 latest-wins 状态。

        用于已经取得一次性确认票据的写操作：确认执行与 reset/new query 必须
        有明确先后顺序，同时不能把该回调伪装成新的查询并抢占 active lease。
        已确认工具的受控执行（包括其不可撤销的外部副作用）必须留在窗口内，确保
        reset/delete 返回时不存在迟到写入；但 ASGI/Telegram 等面向客户端的发送
        必须在窗口外完成，避免客户端背压占用 owner 锁。
        """
        owner_key = self._owner(owner)
        with self._owner_lock(owner_key):
            yield

    @contextmanager
    def publication_window_if_current(
        self,
        lease: AgentOperationLease,
    ) -> Iterator[bool]:
        """在单次可见发布期间阻止同 owner 的 begin/cancel 插入。"""
        owner_lock = self._owner_lock(lease.owner)
        with owner_lock:
            with self._lock:
                allowed = (
                    lease._state.status == "active"
                    and self._active.get(lease.owner) is lease._state
                )
            yield allowed

    @contextmanager
    def finalization_window_if_current(
        self,
        lease: AgentOperationLease,
    ) -> Iterator[bool]:
        """原子领取终态发布权，直到历史与最终结果完成发布后才释放。"""
        owner_lock = self._owner_lock(lease.owner)
        with owner_lock:
            with self._lock:
                allowed = (
                    lease._state.status == "active"
                    and self._active.get(lease.owner) is lease._state
                )
                if allowed:
                    lease._state.status = "finalizing"
            if not allowed:
                yield False
                return
            try:
                yield True
            finally:
                with self._lock:
                    if self._active.get(lease.owner) is lease._state:
                        self._active.pop(lease.owner, None)
                    lease._state.status = "completed"
                    self._reclaim_generation_locked(lease.owner)

    def publish_if_current(
        self,
        lease: AgentOperationLease,
        callback: Callable[[], _T],
    ) -> tuple[bool, _T | None]:
        """仅当前租约可发布；同 owner 的取消会等待正在执行的发布结束。"""
        with self.publication_window_if_current(lease) as allowed:
            if not allowed:
                return False, None
            return True, callback()

    def finalize_if_current(
        self,
        lease: AgentOperationLease,
        callback: Callable[[], _T],
    ) -> tuple[bool, _T | None]:
        """原子领取唯一终态发布权，并在回调后关闭租约。"""
        with self.finalization_window_if_current(lease) as allowed:
            if not allowed:
                return False, None
            return True, callback()

    def finish(self, lease: AgentOperationLease) -> None:
        owner_lock = self._owner_lock(lease.owner)
        with owner_lock, self._lock:
            if self._active.get(lease.owner) is lease._state:
                self._active.pop(lease.owner, None)
            if lease._state.status == "active":
                lease._state.status = "completed"
            self._reclaim_generation_locked(lease.owner)

    def reset(self) -> None:
        # 测试/进程重置也必须遵守同一分片锁顺序，避免发布窗口尚未退出时
        # 同 owner 在另一把锁上重新开始。
        with ExitStack() as stack:
            for owner_lock in self._owner_locks:
                stack.enter_context(owner_lock)
            with self._lock:
                for state in self._active.values():
                    state.status = "cancelled"
                    state.reason = "reset"
                self._active.clear()
                self._generations.clear()
                self._pre_cancelled.clear()


class RecentEventDeduplicator(Generic[_T]):
    """有界 TTL 去重器；首次 claim 返回 True，重复事件返回 False。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = 120.0,
        max_entries: int = 2048,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_entries = max(32, int(max_entries))
        self._clock = clock
        self._lock = threading.RLock()
        self._items: OrderedDict[_T, float] = OrderedDict()

    def claim(self, key: _T) -> bool:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            expires_at = self._items.get(key)
            if expires_at is not None and expires_at > now:
                self._items.move_to_end(key)
                return False
            self._items[key] = now + self._ttl_seconds
            self._items.move_to_end(key)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)
            return True

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, expires_at in self._items.items() if expires_at <= now]
        for key in expired:
            self._items.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._items.clear()


_operation_coordinator = AgentOperationCoordinator()
_telegram_message_deduplicator: RecentEventDeduplicator[str] = RecentEventDeduplicator()


def get_agent_operation_coordinator() -> AgentOperationCoordinator:
    return _operation_coordinator


def get_telegram_message_deduplicator() -> RecentEventDeduplicator[str]:
    return _telegram_message_deduplicator


def reset_agent_operation_state_for_tests() -> None:
    _operation_coordinator.reset()
    _telegram_message_deduplicator.reset()
