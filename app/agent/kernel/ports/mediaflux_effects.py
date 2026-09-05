"""MediaFlux 副作用的领域后置动作、审计与运行代次收束。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from app.agent.action_history import (
    record_confirmation_error,
    record_confirmation_interrupted,
    record_confirmed_result,
)
from app.agent.action_undo import attach_undo_receipt, compensation_candidate
from app.agent.activity_actions import attach_activity_reference
from app.agent.feature_gate import invalidate_agent_runtime_generation
from app.agent.missing_media_workflow_runtime import (
    MISSING_MEDIA_CANDIDATES_KEY,
    MISSING_MEDIA_FOLLOWUPS_KEY,
    MissingMediaWorkflowRuntime,
)
from app.agent.models import RiskLevel, ToolContext, ToolResult
from app.logger import get_logger

from ..capabilities import KernelToolSpec, ToolEffect
from ..effects import EffectPlan, PreparedEffect
from ..pipeline import ToolCallContext

logger = get_logger(__name__)


class MediaFluxEffectLifecycle:
    """复用领域后置动作和既有脱敏审计；辅助故障不改变真实写入结果。"""

    def __init__(
        self, *, missing_media_runtime: MissingMediaWorkflowRuntime | None = None
    ) -> None:
        self.missing_media_runtime = missing_media_runtime

    @staticmethod
    def _risk(plan: EffectPlan) -> RiskLevel:
        raw = str(plan.metadata.get("risk") or "").strip().casefold()
        try:
            return RiskLevel(raw)
        except ValueError:
            return (
                RiskLevel.DANGER
                if plan.effect is ToolEffect.DANGER
                else RiskLevel.WRITE
            )

    @staticmethod
    def _contract(plan: EffectPlan) -> dict[str, Any]:
        contract = plan.confirmation_contract.get("audit_contract")
        return dict(contract) if isinstance(contract, dict) else {}

    @staticmethod
    def _followups(metadata: Any) -> Any:
        return (
            metadata.get(MISSING_MEDIA_FOLLOWUPS_KEY)
            if isinstance(metadata, dict)
            else None
        )

    def prepared(
        self,
        *,
        tool: KernelToolSpec,
        arguments: Mapping[str, Any],
        prepared: PreparedEffect,
        context: ToolCallContext,
    ) -> PreparedEffect:
        runtime = self.missing_media_runtime
        metadata = dict(prepared.metadata)
        inverse = compensation_candidate(tool.name, dict(arguments), dict(prepared.preview))
        if inverse:
            metadata["compensation"] = inverse
        candidates = metadata.pop(MISSING_MEDIA_CANDIDATES_KEY, None)
        if runtime is None or candidates is None:
            return replace(prepared, metadata=metadata)
        followups = runtime.stage_candidates(
            owner=context.owner,
            candidates=candidates,
        )
        if followups:
            metadata[MISSING_MEDIA_FOLLOWUPS_KEY] = list(followups)
        return replace(prepared, metadata=metadata)

    def prepare_failed(
        self, *, prepared: PreparedEffect, context: ToolCallContext
    ) -> None:
        runtime = self.missing_media_runtime
        if runtime is None:
            return
        runtime.release_followups(
            owner=context.owner,
            followups=self._followups(dict(prepared.metadata)),
        )

    def completed(self, *, plan: EffectPlan, value: Any, elapsed_ms: int) -> None:
        runtime = self.missing_media_runtime
        if runtime is not None:
            runtime.complete_submission(
                owner=plan.owner,
                tool_name=plan.tool_name,
                result=value,
                followups=self._followups(dict(plan.metadata)),
            )
        if not isinstance(value, ToolResult):
            return
        attach_activity_reference(value, plan.tool_name)
        try:
            attach_undo_receipt(
                value, plan.metadata.get("compensation"), key=plan.plan_id,
                context=ToolContext(owner=plan.owner, session_id=plan.session_id),
            )
        except Exception as exc:  # noqa: BLE001 -- 可选后置凭证失败不得覆写真实写入结果
            # 原操作的事实不能被可选回退凭证失败覆写。
            logger.warning("回退凭证创建失败 type=%s", type(exc).__name__)
            value.suggestions.append("本次操作未生成回退凭证；需要恢复时请先检查当前状态。")
        record_confirmed_result(
            owner=plan.owner,
            tool_name=plan.tool_name,
            risk=self._risk(plan),
            result=value,
            elapsed_ms=elapsed_ms,
            confirmation_contract=self._contract(plan),
            confirmation_id=plan.plan_id,
            owner_generation=plan.owner_generation,
        )
        if value.ok:
            invalidate_agent_runtime_generation()

    def failed(self, *, plan: EffectPlan, code: str, elapsed_ms: int) -> None:
        runtime = self.missing_media_runtime
        if runtime is not None:
            runtime.release_followups(
                owner=plan.owner,
                followups=self._followups(dict(plan.metadata)),
            )
        record_confirmation_error(
            owner=plan.owner,
            tool_name=plan.tool_name,
            risk=self._risk(plan),
            code=code,
            elapsed_ms=elapsed_ms,
            confirmation_contract=self._contract(plan),
            confirmation_id=plan.plan_id,
            owner_generation=plan.owner_generation,
        )

    def interrupted(self, *, plan: EffectPlan) -> None:
        runtime = self.missing_media_runtime
        if runtime is not None:
            runtime.release_followups(
                owner=plan.owner,
                followups=self._followups(dict(plan.metadata)),
            )
        record_confirmation_interrupted(
            owner=plan.owner,
            confirmation_id=plan.plan_id,
            owner_generation=plan.owner_generation,
            tool_name=plan.tool_name,
            risk=self._risk(plan),
            confirmation_contract=self._contract(plan),
        )

    def cancelled(self, *, plan: EffectPlan) -> None:
        runtime = self.missing_media_runtime
        if runtime is None:
            return
        runtime.release_followups(
            owner=plan.owner,
            followups=self._followups(dict(plan.metadata)),
        )
