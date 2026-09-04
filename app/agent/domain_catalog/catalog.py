"""按领域组合 MediaFlux Agent 的原子工具声明。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.ingest_actions import AgentIngestSessionStore, IngestActions
from app.agent.missing_media_workflow_runtime import MissingMediaWorkflowRuntime
from app.agent.models import ToolSpec
from app.agent.recent_resource_candidates import RecentResourceCandidateStore

from .cloud import register_specs as register_cloud_specs
from .cloud_sdk import register_specs as register_cloud_sdk_specs
from .discovery import register_specs as register_discovery_specs
from .download import register_specs as register_download_specs
from .library import register_specs as register_library_specs
from .local_media import register_specs as register_local_media_specs
from .playback import register_specs as register_playback_specs
from .resource import register_specs as register_resource_specs
from .strm import register_specs as register_strm_specs
from .subscription import register_specs as register_subscription_specs
from .system import register_specs as register_system_specs
from .workspace import register_specs as register_workspace_specs


@dataclass(slots=True)
class ToolSpecCollector:
    items: list[ToolSpec] = field(default_factory=list)

    def register(self, spec: ToolSpec) -> None:
        if any(item.name == spec.name for item in self.items):
            raise ValueError(f"duplicate tool: {spec.name}")
        self.items.append(spec)

    def capabilities(self) -> list[dict]:
        return [item.public_dict() for item in self.items]


_REGISTRARS = (
    register_system_specs,
    register_local_media_specs,
    register_download_specs,
    register_subscription_specs,
    register_playback_specs,
    register_strm_specs,
    register_cloud_specs,
    register_cloud_sdk_specs,
    register_discovery_specs,
    register_resource_specs,
    register_workspace_specs,
    register_library_specs,
)


def build_tool_specs(
    recent_resource_store: RecentResourceCandidateStore | None = None,
    ingest_store: AgentIngestSessionStore | None = None,
    missing_media_runtime: MissingMediaWorkflowRuntime | None = None,
) -> tuple[ToolSpec, ...]:
    collector = ToolSpecCollector()
    resource_store = recent_resource_store or RecentResourceCandidateStore()
    active_ingest_store = ingest_store or AgentIngestSessionStore()
    ingest_actions = IngestActions(
        store=active_ingest_store,
        recent_resource_store=resource_store,
    )
    for registrar in _REGISTRARS:
        if registrar is register_library_specs:
            registrar(
                collector,
                resource_store=resource_store,
                active_ingest_store=active_ingest_store,
                ingest_actions=ingest_actions,
                missing_media_runtime=missing_media_runtime,
            )
        else:
            registrar(
                collector,
                resource_store=resource_store,
                active_ingest_store=active_ingest_store,
                ingest_actions=ingest_actions,
            )
    return tuple(collector.items)
