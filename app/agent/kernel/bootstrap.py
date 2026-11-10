"""Agent Kernel 的唯一生产依赖组合入口。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from app import config
from app import database as db
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.domains.downloads.recent_submissions import RecentDownloadSubmissionStore
from app.modules.media_preferences import owner_memory
from app.agent.domains.library.missing_workflow_runtime import (
    MissingMediaWorkflowRuntime,
)
from app.repositories.agent_download_verification import (
    SQLiteMissingMediaWorkflowRepository,
)
from app.agent.feature_gate import (
    RuntimeFence,
    RuntimeFenceError,
    read_runtime_fence,
    require_runtime_fence,
)
from app.repositories.agent_provider_plans import invalidate_provider_plans_for_session
from app.agent.session_context import SQLiteAgentSessionContextRepository
from app.agent.transports import TelegramKernelTransport, WebKernelTransport

from .sandbox import run_code_tool, workspace_unavailable_reason
from .effects import ConfirmationEffectPlanStore
from .lifecycle import AgentSessionLifecycle
from .metrics import KernelMetrics
from .model import ModelAdapter
from .persistence import SQLiteKernelStore
from .pipeline import ToolPipeline
from .capabilities import ToolCatalog
from .ports.mediaflux_effects import MediaFluxEffectLifecycle
from .ports.mediaflux_policy import (
    MediaFluxAuthorizationPolicy,
    MediaFluxToolRateLimiter,
    MediaFluxTurnAdmission,
    select_channel_model,
)
from .provider_model import OpenAICompatibleModelAdapter, ProviderSettings
from .session import AgentSession, SessionLimits


@dataclass(frozen=True, slots=True)
class RuntimeCreationBinding:
    fence: RuntimeFence
    effective_digest: str
    snapshot: config.RuntimeConfigSnapshot = field(repr=False, compare=False)


def _capture_creation_binding() -> RuntimeCreationBinding:
    snapshot = config.capture_runtime_config_snapshot()
    if not db.resolve_db_path().is_file():
        raise RuntimeFenceError("runtime_database_unavailable")
    with db.get_conn() as conn:
        fence = read_runtime_fence(conn)
        require_runtime_fence(conn, fence)
    if fence.config_digest != snapshot.config_digest:
        raise RuntimeFenceError("runtime_snapshot_mismatch")
    return RuntimeCreationBinding(fence, snapshot.effective_digest, snapshot)


def _recheck_creation_binding(binding: RuntimeCreationBinding) -> None:
    try:
        current = _capture_creation_binding()
    except RuntimeFenceError as exc:
        raise RuntimeFenceError("runtime_changed_during_creation") from exc
    if current != binding:
        raise RuntimeFenceError("runtime_changed_during_creation")


@dataclass(frozen=True, slots=True)
class AgentKernelRuntime:
    session: AgentSession
    store: SQLiteKernelStore
    lifecycle: AgentSessionLifecycle
    metrics: KernelMetrics
    web: WebKernelTransport
    telegram: TelegramKernelTransport
    creation_binding: RuntimeCreationBinding | None = None


_lock = threading.Lock()
_runtime: AgentKernelRuntime | None = None


def _build_domain_dependencies() -> tuple[
    MissingMediaWorkflowRuntime,
]:
    context_repository = SQLiteAgentSessionContextRepository()
    missing_media_runtime = MissingMediaWorkflowRuntime(
        repository=SQLiteMissingMediaWorkflowRepository(),
        recent_download_store=RecentDownloadSubmissionStore(
            repository=context_repository
        ),
    )
    return (missing_media_runtime,)


def _session_limits_from_config() -> SessionLimits:
    context_window = config.get_int("AGENT_LLM_CONTEXT_WINDOW_TOKENS", 128_000)
    return SessionLimits(
        context_window_tokens=max(16_384, min(2_000_000, context_window))
    )


def build_tool_catalog() -> ToolCatalog:
    """SDK EffectExecutor 的内部完整目录；模型入口在 runtime 中单独声明。"""
    from .projection import DefaultProjector
    from .ports.resource_presentation import ResourceSelection
    catalog = ToolCatalog(projector=DefaultProjector(), selection=ResourceSelection())
    from .ports.sdk_effects import automation_tools, subscription_tools
    for tool in (*automation_tools(), *subscription_tools()):
        catalog.register(tool)
    from .ports.sdk_effects import media_refresh_tools, activity_reorganize_tool, activity_undo_tool, activity_follow_tools, share_transfer_tool, directory_scrape_tool, qb_control_tools, cloud_change_tool, cloud_recycle_tools, cloud_share_tools, download_retry_tool, download_submit_tool, preferences_tools, resource_download_tool, watchlist_tools
    from .ports.sdk_effects import rss_configuration_tools, rss_delivery_tools
    for tool in (*rss_configuration_tools(), *rss_delivery_tools()):
        catalog.register(tool)
    for tool in media_refresh_tools():
        catalog.register(tool)
    from .ports.sdk_effects import library_patrol_tool
    catalog.register(library_patrol_tool())
    from .ports.sdk_effects import cloud_organize_tools
    for tool in cloud_organize_tools():
        catalog.register(tool)
    from .ports.sdk_effects import library_audit_job_tools
    for tool in library_audit_job_tools():
        catalog.register(tool)
    from .ports.sdk_effects import playback_tools
    for tool in playback_tools():
        catalog.register(tool)
    from .ports.sdk_effects import media_user_tools
    for tool in media_user_tools():
        catalog.register(tool)
    catalog.register(activity_undo_tool())
    catalog.register(activity_reorganize_tool())
    for tool in activity_follow_tools():
        catalog.register(tool)
    catalog.register(resource_download_tool())
    catalog.register(share_transfer_tool())
    catalog.register(download_submit_tool())
    catalog.register(download_retry_tool())
    from .ports.sdk_effects import download_diagnose_tool
    catalog.register(download_diagnose_tool())
    for tool in qb_control_tools():
        catalog.register(tool)
    catalog.register(cloud_change_tool())
    catalog.register(directory_scrape_tool())
    from .ports.sdk_effects import schedule_policy_tools, strm_metadata_tools, strm_run_tool, strm_failure_tools
    for tool in schedule_policy_tools():
        catalog.register(tool)
    catalog.register(strm_run_tool())
    for tool in strm_failure_tools():
        catalog.register(tool)
    for tool in strm_metadata_tools():
        catalog.register(tool)
    for tool in cloud_recycle_tools():
        catalog.register(tool)
    for tool in cloud_share_tools():
        catalog.register(tool)
    from .ports.sdk_effects import media_mapping_tool
    catalog.register(media_mapping_tool())
    for tool in watchlist_tools():
        catalog.register(tool)
    from .ports.sdk_effects import release_format_save_tool, recognition_knowledge_tools, media_path_mapping_tools, recognition_rule_tool, notification_test_tool, policy_config_tool, feature_config_tool
    catalog.register(release_format_save_tool())
    catalog.register(recognition_rule_tool())
    catalog.register(feature_config_tool())
    catalog.register(policy_config_tool())
    catalog.register(notification_test_tool())
    for tool in media_path_mapping_tools():
        catalog.register(tool)
    for tool in recognition_knowledge_tools():
        catalog.register(tool)
    for tool in preferences_tools():
        catalog.register(tool)
    from .ports.sdk_effects import indexer_configuration_tool
    catalog.register(indexer_configuration_tool())
    from .ports.sdk_effects import local_source_tools, local_task_tools
    for tool in (*local_source_tools(), *local_task_tools()):
        catalog.register(tool)
    return catalog


def build_agent_kernel_runtime(
    *,
    model: ModelAdapter | None = None,
    store: SQLiteKernelStore | None = None,
    confirmation_store: SQLiteConfirmationStore | None = None,
) -> AgentKernelRuntime:
    if model is not None:
        # Explicit injection never becomes a default paid adapter, even if falsey.
        return _build_agent_kernel_runtime(model=model, store=store, confirmation_store=confirmation_store)
    with config.runtime_config_read_guard():
        binding = _capture_creation_binding()
    with config.use_runtime_config_snapshot(binding.snapshot):
        runtime = _build_agent_kernel_runtime(store=store, confirmation_store=confirmation_store, creation_binding=binding)
    with config.runtime_config_read_guard():
        _recheck_creation_binding(binding)
        return runtime


def _build_agent_kernel_runtime(
    *,
    model: ModelAdapter | None = None,
    store: SQLiteKernelStore | None = None,
    confirmation_store: SQLiteConfirmationStore | None = None,
    creation_binding: RuntimeCreationBinding | None = None,
) -> AgentKernelRuntime:
    """组合唯一 Kernel、领域能力、持久化与两个薄入口。"""
    (
        missing_media_runtime,
    ) = _build_domain_dependencies()
    catalog = build_tool_catalog()
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
        effect_lifecycle=MediaFluxEffectLifecycle(
            missing_media_runtime=missing_media_runtime
        ),
    )
    workspace_error = workspace_unavailable_reason()
    if not workspace_error:
        # 普通代码仅在镜像已配置的隔离工作区执行，不退回语言白名单或主进程执行。
        catalog.register(run_code_tool(pipeline))
    if model is not None:
        bool(model)  # Preserve one observable evaluation, never use it to route.
        active_model = model
        limits = _session_limits_from_config()
    else:
        settings = ProviderSettings.from_config()
        active_model = OpenAICompatibleModelAdapter(settings)
        limits = SessionLimits(
            context_window_tokens=settings.context_window_tokens,
            # 推理 token 计入输出额度；放宽到既有上限，避免思考挤掉工具调用。
            max_output_tokens=16_000 if settings.reasoning_effort else 6_000,
        )
    session = AgentSession(
        model=active_model,
        catalog=catalog,
        model_tools=tuple(tool.name for tool in catalog.visible() if tool.name == "agent.run_code"),
        pipeline=pipeline,
        state_store=kernel_store,
        journal=kernel_store,
        limits=limits,
        turn_admission=MediaFluxTurnAdmission(workspace_error=workspace_error),
        model_selector=select_channel_model,
        stream_display=lambda: config.get_bool("AGENT_STREAM_DISPLAY_ENABLED"),
        user_memory=owner_memory,
    )
    session.continuation_binding = creation_binding
    lifecycle = AgentSessionLifecycle(
        session=session,
        store=kernel_store,
        effect_store=effect_store,
        clear_legacy_plans=invalidate_provider_plans_for_session,
    )
    return AgentKernelRuntime(
        session=session,
        store=kernel_store,
        lifecycle=lifecycle,
        metrics=session.metrics,
        web=WebKernelTransport(session),
        telegram=TelegramKernelTransport(session),
        creation_binding=creation_binding,
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
    """读取持久授权并获取运行态；包含阻塞锁/I/O，异步入口须在线程执行。"""
    global _runtime
    # Never acquire the config lock while holding the cache lock.
    with config.runtime_config_read_guard():
        binding = _capture_creation_binding()
        with _lock:
            if _runtime is not None and _runtime.creation_binding == binding:
                return _runtime
    with config.use_runtime_config_snapshot(binding.snapshot):
        runtime = _build_agent_kernel_runtime(creation_binding=binding)
    with config.runtime_config_read_guard():
        _recheck_creation_binding(binding)
        with _lock:
            if _runtime is not None and _runtime.creation_binding == binding:
                return _runtime
            _runtime = runtime
        return runtime


def get_agent_kernel() -> AgentSession:
    return get_agent_kernel_runtime().session


def invalidate_agent_kernel_runtime() -> None:
    """使下一次入口调用按最新模型与上下文配置重建 Kernel。"""
    global _runtime
    with _lock:
        _runtime = None


def reset_agent_kernel_for_tests() -> None:
    invalidate_agent_kernel_runtime()
