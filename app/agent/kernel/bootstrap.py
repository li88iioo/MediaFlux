"""Agent Kernel 的唯一生产依赖组合入口。"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.discovery_mapping_actions import configure_discovery_mapping_context
from app.agent.domain_catalog import build_tool_specs
from app.agent.guangya_cleanup_actions import configure_guangya_cleanup_context
from app.agent.guangya_directory_scrape_actions import (
    configure_directory_scrape_context,
)
from app.agent.guangya_fs_change_actions import configure_guangya_fs_change_context
from app.agent.guangya_rename_actions import configure_guangya_rename_context
from app.agent.guangya_workspace_actions import configure_guangya_workspace_context
from app.agent.ingest_actions import AgentIngestSessionStore
from app.agent.local_media_task_actions import configure_local_media_agent_context
from app.agent.recent_resource_candidates import RecentResourceCandidateStore
from app.agent.session_context import SQLiteAgentSessionContextRepository

from .capabilities import CapabilityRetriever
from .effects import ConfirmationEffectPlanStore
from .metrics import KernelMetrics
from .model import ModelAdapter
from .persistence import SQLiteKernelStore
from .pipeline import ToolPipeline
from .ports import catalog_from_tool_specs
from .ports.mediaflux_effects import MediaFluxEffectLifecycle
from .ports.mediaflux_policy import (
    MediaFluxAuthorizationPolicy,
    MediaFluxToolRateLimiter,
    MediaFluxTurnAdmission,
)
from .provider_model import OpenAICompatibleModelAdapter, ProviderSettings
from .session import AgentSession
from .transports import TelegramKernelTransport, WebKernelTransport


@dataclass(frozen=True, slots=True)
class AgentKernelRuntime:
    session: AgentSession
    store: SQLiteKernelStore
    metrics: KernelMetrics
    web: WebKernelTransport
    telegram: TelegramKernelTransport


_lock = threading.Lock()
_runtime: AgentKernelRuntime | None = None


def _configure_domain_contexts() -> tuple[
    SQLiteAgentSessionContextRepository,
    RecentResourceCandidateStore,
    AgentIngestSessionStore,
]:
    context_repository = SQLiteAgentSessionContextRepository()
    configure_local_media_agent_context(context_repository)
    configure_discovery_mapping_context(context_repository)
    configure_directory_scrape_context(context_repository)
    configure_guangya_rename_context(context_repository)
    configure_guangya_cleanup_context(context_repository)
    configure_guangya_workspace_context(context_repository)
    configure_guangya_fs_change_context(context_repository)
    resource_store = RecentResourceCandidateStore(repository=context_repository)
    ingest_store = AgentIngestSessionStore()
    return context_repository, resource_store, ingest_store


def build_agent_kernel_runtime(
    *,
    model: ModelAdapter | None = None,
    store: SQLiteKernelStore | None = None,
    confirmation_store: SQLiteConfirmationStore | None = None,
) -> AgentKernelRuntime:
    """组合唯一 Kernel、领域能力、持久化与两个薄入口。"""
    _context_repository, resource_store, ingest_store = _configure_domain_contexts()
    declarations = build_tool_specs(resource_store, ingest_store)
    catalog = catalog_from_tool_specs(declarations)
    kernel_store = store or SQLiteKernelStore()
    effect_store = ConfirmationEffectPlanStore(
        confirmation_store or SQLiteConfirmationStore(),
        record_actions=True,
    )
    pipeline = ToolPipeline(
        catalog=catalog,
        state_store=kernel_store,
        reference_store=kernel_store,
        effect_store=effect_store,
        authorization=MediaFluxAuthorizationPolicy(),
        rate_limiter=MediaFluxToolRateLimiter(),
        effect_lifecycle=MediaFluxEffectLifecycle(),
    )
    active_model = model or OpenAICompatibleModelAdapter(ProviderSettings.from_config())
    session = AgentSession(
        model=active_model,
        catalog=catalog,
        retriever=CapabilityRetriever(),
        pipeline=pipeline,
        state_store=kernel_store,
        journal=kernel_store,
        turn_admission=MediaFluxTurnAdmission(),
    )
    metrics = KernelMetrics()
    return AgentKernelRuntime(
        session=session,
        store=kernel_store,
        metrics=metrics,
        web=WebKernelTransport(session, metrics=metrics),
        telegram=TelegramKernelTransport(session, metrics=metrics),
    )


def build_agent_kernel(
    *,
    model: ModelAdapter | None = None,
    store: SQLiteKernelStore | None = None,
    confirmation_store: SQLiteConfirmationStore | None = None,
) -> AgentSession:
    """测试与嵌入式调用的便捷入口。"""
    return build_agent_kernel_runtime(
        model=model,
        store=store,
        confirmation_store=confirmation_store,
    ).session


def get_agent_kernel_runtime() -> AgentKernelRuntime:
    global _runtime
    if _runtime is None:
        with _lock:
            if _runtime is None:
                _runtime = build_agent_kernel_runtime()
    return _runtime


def get_agent_kernel() -> AgentSession:
    return get_agent_kernel_runtime().session


def reset_agent_kernel_for_tests() -> None:
    global _runtime
    with _lock:
        _runtime = None
