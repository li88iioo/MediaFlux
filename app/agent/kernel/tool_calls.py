"""工具调用上下文与结果契约；不依赖执行器实现。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .capabilities import ToolEffect

if TYPE_CHECKING:
    from app.agent.kernel.state_types import CancellationToken, PublicationLease

    from .capabilities import KernelToolSpec
    from .projection import ToolOutcome
    from .task_state import TaskPatch


@dataclass(frozen=True, slots=True)
class PreparedEffect:
    preview: Mapping[str, Any]
    snapshot_fingerprint: str
    arguments: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EffectPlan:
    plan_id: str
    owner: str
    session_id: str
    generation: int
    tool_name: str
    effect: ToolEffect
    arguments: Mapping[str, Any]
    snapshot_fingerprint: str
    preview: Mapping[str, Any]
    expires_at: float
    owner_generation: int = 0
    confirmation_contract: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    public_result: Mapping[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        expires_at = (
            datetime.fromtimestamp(self.expires_at, tz=timezone.utc).isoformat(
                timespec="seconds"
            )
            if self.expires_at >= 946_684_800
            else ""
        )
        result: dict[str, Any] = {
            "plan_id": self.plan_id,
            "tool_name": self.tool_name,
            "effect": self.effect.value,
            "preview": deepcopy(dict(self.preview)),
            "expires_at": expires_at,
        }
        confirmation = self.confirmation_contract.get("audit_contract")
        if isinstance(confirmation, Mapping) and confirmation:
            result["confirmation"] = deepcopy(dict(confirmation))
        editing = self.metadata.get("editing") or {}
        if editing.get("schema", {}).get("properties"):
            result["editing"] = deepcopy(editing)
        if self.metadata.get("trust_eligible"):
            result["trust_eligible"] = True
            result["trust_scope"] = deepcopy(self.metadata["trust_scope"])
        return result

    def public_approval_dict(self) -> dict[str, Any]:
        """返回恢复待确认卡片时与事件公开载荷一致的字段。"""
        result = self.public_dict()
        result.setdefault("confirmation", {})
        result["result"] = deepcopy(dict(self.public_result))
        return result


ProgressSink = Callable[[Mapping[str, Any]], Awaitable[None]]

@dataclass(frozen=True, slots=True)
class ToolCallContext:
    owner: str
    session_id: str
    request_id: str
    turn_id: str
    lease: PublicationLease
    cancellation: CancellationToken
    report_progress: ProgressSink
    wait_for_completion: bool = False
    selection_arguments: Mapping[str, Any] | None = None
    resource_candidate_ref: str = ""
    channel: str = "api"
    completion_scope: Any = None
    continuation_binding: Any = None
    continuation_policy: MutableMapping[str, Any] | None = None
    diagnostic_only: bool = False
    read_only: bool = False
    delivery_route: Mapping[str, Any] = field(default_factory=dict)
    confirmed_effect: PipelineResult | None = None
    current_task: dict | None = None
    task_control: Callable[[TaskPatch | None], Awaitable[dict | None]] | None = None
    task_budget: Callable[[], Awaitable[None]] | None = None
    active_effect_plan: EffectPlan | None = None
    batch_progress: dict | None = None
    user_source: str = ""
    user_history: tuple[str, ...] = ()
    user_reply_context: Mapping[str, Any] = field(default_factory=dict)
    call_id: str = ""
    reobservation_source: Mapping[str, Any] | None = field(default=None, repr=False)
    scoped_trust: bool = False

    def policy_context(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "generation": self.lease.generation,
        }

@dataclass(frozen=True, slots=True)
class PipelineResult:
    tool: KernelToolSpec
    arguments: Mapping[str, Any]
    outcome: ToolOutcome
    effect_plan: EffectPlan | None = None
    elapsed_ms: int = 0
    task_patch: TaskPatch | None = None
    prepared_effect: PreparedEffect | None = None


def independent_read(tool, task=None) -> bool:
    from .task_state import tracks_read

    return (tool.effect is ToolEffect.READ and tool.observe is None and not tracks_read(task, tool.name)
            and tool.metadata.get("parallel_read") is True
            and not tool.metadata.get("kernel_utility")
            and not tool.metadata.get("publishes_selection"))
