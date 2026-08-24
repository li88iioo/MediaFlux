"""Agent 查询续接状态的两阶段提交。"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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
        self._resource_result_ids: set[str] = set()
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

    def stage_resource_result_ids(self, *, owner: str, result_ids: set[str]) -> bool:
        owner_key = str(owner or "").strip()
        if not owner_key or owner_key != self._owner:
            return False
        with self._lock:
            if self._closed:
                return False
            self._resource_result_ids.update(result_ids)
            return True

    def owns_resource_result(self, *, owner: str, result_id: str) -> bool:
        owner_key = str(owner or "").strip()
        token = str(result_id or "").strip()
        with self._lock:
            return bool(
                not self._closed
                and owner_key
                and owner_key == self._owner
                and token in self._resource_result_ids
            )

    def commit(self) -> int:
        """至多提交一次；单项续接写入失败不篡改已完成的工具结果。"""
        with self._lock:
            if self._closed:
                return 0
            self._closed = True
            actions = tuple(self._actions)
            self._actions.clear()
            self._resource_result_ids.clear()
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
            self._resource_result_ids.clear()
            self._request_states.clear()
            self._action_keys.clear()
            return discarded


_ACTIVE_STATE_COMMIT_BUFFER: ContextVar[AgentStateCommitBuffer | None] = ContextVar(
    "agent_state_commit_buffer", default=None
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


def commit_or_defer_agent_state(action: Callable[[], None]) -> bool:
    """无协调器时保持即时写入；有缓冲区时延迟到终态发布窗口。"""
    buffer = _ACTIVE_STATE_COMMIT_BUFFER.get()
    if buffer is None:
        action()
        return True
    return buffer.add(action)


def stage_agent_resource_result_ids(*, owner: str, result_ids: set[str]) -> bool:
    buffer = _ACTIVE_STATE_COMMIT_BUFFER.get()
    if buffer is None:
        return False
    return buffer.stage_resource_result_ids(owner=owner, result_ids=result_ids)


def active_agent_state_owns_resource(*, owner: str, result_id: str) -> bool:
    buffer = _ACTIVE_STATE_COMMIT_BUFFER.get()
    return bool(
        buffer is not None
        and buffer.owns_resource_result(owner=owner, result_id=result_id)
    )
