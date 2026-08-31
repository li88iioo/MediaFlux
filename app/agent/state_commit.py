"""Agent 查询续接状态的两阶段提交。"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any
import logging
import threading

logger = logging.getLogger(__name__)


class AgentStateCommitBuffer:
    """只在当前请求成功取得终态发布权后提交跨轮续接状态。"""

    def __init__(self, *, owner: str = "") -> None:
        self._lock = threading.RLock()
        self._actions: list[Callable[[], Any]] = []
        self._closed = False
        self._owner = str(owner or "").strip()
        self._resource_candidate_snapshot: dict[str, Any] | None = None
        self._request_states: dict[str, Any] = {}
        self._action_keys: set[str] = set()

    def add(self, action: Callable[[], Any]) -> bool:
        if not callable(action):
            raise TypeError("state commit action must be callable")
        with self._lock:
            if self._closed:
                return False
            self._actions.append(action)
            return True

    def get_or_create_request_state(
        self,
        *,
        owner: str,
        key: str,
        factory: Callable[[], Any],
    ) -> Any | None:
        """返回当前请求私有状态；它不会在提交前泄漏到跨轮上下文。"""
        owner_key = str(owner or "").strip()
        state_key = str(key or "").strip()
        if not callable(factory):
            raise TypeError("request state factory must be callable")
        if not owner_key or owner_key != self._owner or not state_key:
            return None
        with self._lock:
            if self._closed:
                return None
            if state_key not in self._request_states:
                self._request_states[state_key] = factory()
            return self._request_states[state_key]

    def add_once(self, *, key: str, action: Callable[[], Any]) -> bool:
        """同一请求内按键至多登记一次提交动作。"""
        action_key = str(key or "").strip()
        if not action_key:
            return False
        if not callable(action):
            raise TypeError("state commit action must be callable")
        with self._lock:
            if self._closed:
                return False
            if action_key in self._action_keys:
                return True
            self._action_keys.add(action_key)
            self._actions.append(action)
            return True

    def stage_resource_candidates(
        self, *, owner: str, snapshot: dict[str, Any]
    ) -> bool:
        owner_key = str(owner or "").strip()
        if not owner_key or owner_key != self._owner or not isinstance(snapshot, dict):
            return False
        with self._lock:
            if self._closed:
                return False
            self._resource_candidate_snapshot = deepcopy(snapshot)
            return True

    def resource_candidates(self, *, owner: str) -> dict[str, Any] | None:
        owner_key = str(owner or "").strip()
        with self._lock:
            if (
                self._closed
                or not owner_key
                or owner_key != self._owner
                or self._resource_candidate_snapshot is None
            ):
                return None
            return deepcopy(self._resource_candidate_snapshot)

    def commit(self) -> int:
        """至多提交一次；单项续接写入失败不篡改已完成的工具结果。"""
        with self._lock:
            if self._closed:
                return 0
            self._closed = True
            actions = tuple(self._actions)
            self._actions.clear()
            self._resource_candidate_snapshot = None
            self._request_states.clear()
            self._action_keys.clear()
        committed = 0
        for action in actions:
            try:
                if action() is not False:
                    committed += 1
            except Exception as exc:
                logger.warning(
                    "Agent 续接状态提交失败 type=%s", type(exc).__name__
                )
        return committed

    def discard(self) -> int:
        """撤销所有尚未提交的续接写入，并拒绝迟到工具继续追加。"""
        with self._lock:
            if self._closed:
                return 0
            self._closed = True
            discarded = len(self._actions)
            self._actions.clear()
            self._resource_candidate_snapshot = None
            self._request_states.clear()
            self._action_keys.clear()
            return discarded


_ACTIVE_STATE_COMMIT_BUFFER: ContextVar[AgentStateCommitBuffer | None] = ContextVar(
    "agent_state_commit_buffer", default=None
)
_ACTIVE_RESOURCE_CANDIDATES: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar(
    "agent_active_resource_candidates", default=None
)


@contextmanager
def defer_agent_state_commits(buffer: AgentStateCommitBuffer) -> Iterator[None]:
    """让当前查询及其 ``asyncio.to_thread`` 子调用共享同一提交缓冲区。"""
    if not isinstance(buffer, AgentStateCommitBuffer):
        raise TypeError("buffer must be AgentStateCommitBuffer")
    token = _ACTIVE_STATE_COMMIT_BUFFER.set(buffer)
    try:
        yield
    finally:
        _ACTIVE_STATE_COMMIT_BUFFER.reset(token)


def active_agent_state_commit_buffer() -> AgentStateCommitBuffer | None:
    """返回当前请求缓冲区，仅供需要请求内隔离视图的状态仓储使用。"""
    return _ACTIVE_STATE_COMMIT_BUFFER.get()


@contextmanager
def isolate_agent_resource_results() -> Iterator[None]:
    """让一次原生工具循环内的新资源句柄立即可见，但不提前持久化候选上下文。"""
    current = _ACTIVE_RESOURCE_CANDIDATES.get()
    if current is not None:
        yield
        return
    token = _ACTIVE_RESOURCE_CANDIDATES.set({})
    try:
        yield
    finally:
        _ACTIVE_RESOURCE_CANDIDATES.reset(token)


def commit_or_defer_agent_state(action: Callable[[], None]) -> bool:
    """无协调器时保持即时写入；有缓冲区时延迟到终态发布窗口。"""
    buffer = _ACTIVE_STATE_COMMIT_BUFFER.get()
    if buffer is None:
        action()
        return True
    return buffer.add(action)



def stage_agent_resource_candidates(*, owner: str, snapshot: dict[str, Any]) -> bool:
    staged = False
    buffer = _ACTIVE_STATE_COMMIT_BUFFER.get()
    if buffer is not None:
        staged = buffer.stage_resource_candidates(owner=owner, snapshot=snapshot) or staged
    active = _ACTIVE_RESOURCE_CANDIDATES.get()
    owner_key = str(owner or "").strip()
    if active is not None and owner_key:
        active[owner_key] = deepcopy(snapshot)
        staged = True
    return staged


def active_agent_resource_candidates(*, owner: str) -> dict[str, Any] | None:
    owner_key = str(owner or "").strip()
    buffer = _ACTIVE_STATE_COMMIT_BUFFER.get()
    if buffer is not None:
        snapshot = buffer.resource_candidates(owner=owner_key)
        if snapshot is not None:
            return snapshot
    active = _ACTIVE_RESOURCE_CANDIDATES.get()
    snapshot = active.get(owner_key) if active is not None else None
    return deepcopy(snapshot) if isinstance(snapshot, dict) else None
