"""Agent 会话滚动摘要的非阻塞、失败安全调度。"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from typing import Any

from app.agent.conversation_history import (
    SQLiteAgentConversationHistoryRepository,
    get_agent_conversation_history_repository,
)
from app.logger import get_logger

logger = get_logger(__name__)

SummaryFunction = Callable[..., dict[str, Any] | None]
JobRunner = Callable[[Callable[[], None]], None]


def _daemon_runner(job: Callable[[], None]) -> None:
    thread = threading.Thread(
        target=job,
        name="mediaflux-agent-context-compaction",
        daemon=True,
    )
    thread.start()


class ConversationCompactionCoordinator:
    """限制并发并按主体/会话去重，不阻塞交互请求。"""

    def __init__(
        self,
        *,
        max_concurrency: int = 2,
        runner: JobRunner = _daemon_runner,
    ) -> None:
        self._capacity = threading.BoundedSemaphore(
            max(1, min(int(max_concurrency), 4))
        )
        self._runner = runner
        self._lock = threading.Lock()
        self._active: set[str] = set()

    @staticmethod
    def _key(principal: str, session_id: str) -> str:
        material = (
            "mediaflux-agent-context-compaction:v1\0"
            + str(principal or "")
            + "\0"
            + str(session_id or "")
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def schedule(
        self,
        *,
        principal: str,
        session_id: str,
        llm_owner: str,
        repository: SQLiteAgentConversationHistoryRepository,
        summarizer: SummaryFunction,
    ) -> bool:
        try:
            snapshot = repository.prepare_compaction(
                principal=principal,
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning(
                "Agent 会话摘要快照准备失败 type=%s", type(exc).__name__
            )
            return False
        if snapshot is None:
            return False

        key = self._key(principal, session_id)
        with self._lock:
            if key in self._active or not self._capacity.acquire(blocking=False):
                return False
            self._active.add(key)

        def run() -> None:
            stored = False
            try:
                summary = summarizer(
                    snapshot.previous_summary,
                    list(snapshot.messages),
                    owner=llm_owner,
                )
                if summary is None:
                    return
                stored = repository.store_compaction_summary(
                    principal=principal,
                    session_id=session_id,
                    snapshot=snapshot,
                    summary=summary,
                )
            except Exception as exc:
                # 摘要是辅助能力；失败不能改变当前回答，也不能泄露会话身份或内容。
                logger.warning(
                    "Agent 会话摘要后台任务失败 type=%s", type(exc).__name__
                )
            finally:
                with self._lock:
                    self._active.discard(key)
                    self._capacity.release()
                # 摘要生成期间可能又写入了新消息。只有当前快照成功落库后
                # 才重新检查一次，避免活跃任务期间被去重的刷新永久丢失。
                # prepare_compaction() 没有新工作时会立即停止，因此不会空转。
                if stored:
                    self.schedule(
                        principal=principal,
                        session_id=session_id,
                        llm_owner=llm_owner,
                        repository=repository,
                        summarizer=summarizer,
                    )

        try:
            self._runner(run)
        except Exception as exc:
            with self._lock:
                self._active.discard(key)
                self._capacity.release()
            logger.warning("Agent 会话摘要调度失败 type=%s", type(exc).__name__)
            return False
        return True


_coordinator = ConversationCompactionCoordinator()


def schedule_conversation_compaction(
    *,
    principal: str,
    session_id: str,
    llm_owner: str,
    repository: SQLiteAgentConversationHistoryRepository | None = None,
    summarizer: SummaryFunction | None = None,
) -> bool:
    """在已成功写入一轮历史后，尽力调度低优先级上下文压缩。"""
    if summarizer is None:
        from app.agent.llm_router import (
            conversation_summary_enabled,
            summarize_conversation_context,
        )

        if not conversation_summary_enabled():
            return False
        summarizer = summarize_conversation_context
    return _coordinator.schedule(
        principal=principal,
        session_id=session_id,
        llm_owner=llm_owner,
        repository=repository or get_agent_conversation_history_repository(),
        summarizer=summarizer,
    )
