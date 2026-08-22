"""Agent 查询续接状态的两阶段提交。"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import threading

logger = logging.getLogger(__name__)


class AgentStateCommitBuffer:
    """只在当前请求成功取得终态发布权后提交跨轮续接状态。"""

    def __init__(self, *, owner: str = "") -> None:
        self._lock = threading.RLock()
        self._actions: list[Callable[[], None]] = []
        self._closed = False
        self._owner = str(owner or "").strip()
        self._resource_result_ids: set[str] = set()

    def add(self, action: Callable[[], None]) -> bool:
        if not callable(action):
            raise TypeError("state commit action must be callable")
        with self._lock:
            if self._closed:
                return False
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
        committed = 0
        for action in actions:
            try:
                action()
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
