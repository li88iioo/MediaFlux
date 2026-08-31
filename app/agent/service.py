"""Media Agent 服务单例。"""
from __future__ import annotations

import threading

from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.discovery_mapping_actions import (
    configure_discovery_mapping_context,
    reset_discovery_mapping_context_for_tests,
)
from app.agent.guangya_cleanup_actions import (
    configure_guangya_cleanup_context,
    reset_guangya_cleanup_context_for_tests,
)
from app.agent.guangya_directory_scrape_actions import (
    configure_directory_scrape_context,
    reset_directory_scrape_context_for_tests,
)
from app.agent.guangya_fs_change_actions import (
    configure_guangya_fs_change_context,
    reset_guangya_fs_change_context_for_tests,
)
from app.agent.guangya_rename_actions import (
    configure_guangya_rename_context,
    reset_guangya_rename_context_for_tests,
)
from app.agent.guangya_workspace_actions import (
    configure_guangya_workspace_context,
    reset_guangya_workspace_context_for_tests,
)
from app.agent.local_media_task_actions import (
    configure_local_media_agent_context,
    reset_local_media_agent_context_for_tests,
)
from app.agent.missing_media_workflows import SQLiteMissingMediaWorkflowRepository
from app.agent.orchestrator import AgentOrchestrator
from app.agent.recent_discovery_candidates import RecentDiscoveryCandidateStore
from app.agent.recent_download_submissions import (
    RecentDownloadSubmissionStore,
    enqueue_recent_download_library_verification,
)
from app.agent.recent_patrol import RecentPatrolStore
from app.agent.recent_read_operations import RecentReadOperationStore
from app.agent.recent_resource_candidates import RecentResourceCandidateStore
from app.agent.session_context import SQLiteAgentSessionContextRepository
from app.agent.tools import build_tool_registry, reset_agent_tool_caches_for_tests

_lock = threading.Lock()
_service: AgentOrchestrator | None = None


def get_agent_service() -> AgentOrchestrator:
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                context_repository = SQLiteAgentSessionContextRepository()
                configure_local_media_agent_context(context_repository)
                configure_discovery_mapping_context(context_repository)
                configure_directory_scrape_context(context_repository)
                configure_guangya_rename_context(context_repository)
                configure_guangya_cleanup_context(context_repository)
                configure_guangya_workspace_context(context_repository)
                configure_guangya_fs_change_context(context_repository)
                missing_workflow_repository = SQLiteMissingMediaWorkflowRepository()
                recent_resource_store = RecentResourceCandidateStore(
                    repository=context_repository
                )
                _service = AgentOrchestrator(
                    build_tool_registry(recent_resource_store),
                    confirmation_store=SQLiteConfirmationStore(),
                    recent_patrol_store=RecentPatrolStore(repository=context_repository),
                    # 候选安全投影可跨进程续接；若底层 result_id 已失效，
                    # 确认执行阶段仍会由资源存储返回明确的过期提示。
                    recent_resource_store=recent_resource_store,
                    recent_discovery_store=RecentDiscoveryCandidateStore(
                        repository=context_repository
                    ),
                    recent_download_store=RecentDownloadSubmissionStore(
                        repository=context_repository
                    ),
                    recent_read_store=RecentReadOperationStore(
                        repository=context_repository
                    ),
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
        reset_local_media_agent_context_for_tests()
        reset_discovery_mapping_context_for_tests()
        reset_directory_scrape_context_for_tests()
        reset_guangya_rename_context_for_tests()
        reset_guangya_cleanup_context_for_tests()
        reset_guangya_workspace_context_for_tests()
        reset_guangya_fs_change_context_for_tests()
        reset_agent_tool_caches_for_tests()
