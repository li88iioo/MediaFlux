"""Media Agent 服务单例。"""
from __future__ import annotations

import threading

from app.agent.orchestrator import AgentOrchestrator
from app.agent.missing_media_workflows import SQLiteMissingMediaWorkflowRepository
from app.agent.recent_download_submissions import (
    RecentDownloadSubmissionStore,
    enqueue_recent_download_library_verification,
)
from app.agent.recent_patrol import RecentPatrolStore
from app.agent.recent_read_operations import RecentReadOperationStore
from app.agent.recent_resource_candidates import RecentResourceCandidateStore
from app.agent.recent_discovery_candidates import RecentDiscoveryCandidateStore
from app.agent.session_context import SQLiteAgentSessionContextRepository
from app.agent.tools import build_tool_registry

_lock = threading.Lock()
_service: AgentOrchestrator | None = None


def get_agent_service() -> AgentOrchestrator:
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                context_repository = SQLiteAgentSessionContextRepository()
                missing_workflow_repository = SQLiteMissingMediaWorkflowRepository()
                _service = AgentOrchestrator(
                    build_tool_registry(),
                    recent_patrol_store=RecentPatrolStore(repository=context_repository),
                    # 资源 result_id 只在进程内 IndexerResultStore 中有效；
                    # 不把不可恢复的句柄伪装成跨重启可提交上下文。
                    recent_resource_store=RecentResourceCandidateStore(),
                    recent_discovery_store=RecentDiscoveryCandidateStore(),
                    recent_download_store=RecentDownloadSubmissionStore(
                        repository=context_repository
                    ),
                    recent_read_store=RecentReadOperationStore(),
                    missing_workflow_repository=missing_workflow_repository,
                    session_context_repository=context_repository,
                    automatic_verification_enqueuer=(
                        enqueue_recent_download_library_verification
                    ),
                    record_actions=True,
                )
    return _service


def reset_agent_service_for_tests() -> None:
    global _service
    with _lock:
        _service = None
