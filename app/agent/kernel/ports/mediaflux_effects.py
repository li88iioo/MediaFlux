"""MediaFlux 已确认副作用的审计与运行代次收束。"""

from __future__ import annotations

from typing import Any

from app.agent.action_history import (
    record_confirmation_error,
    record_confirmation_interrupted,
    record_confirmed_result,
)
from app.agent.feature_gate import invalidate_agent_runtime_generation
from app.agent.models import RiskLevel, ToolResult

from ..capabilities import ToolEffect
from ..effects import EffectPlan


class MediaFluxEffectLifecycle:
    """复用既有脱敏审计；任何审计故障都不得改变领域执行结果。"""

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

    def completed(self, *, plan: EffectPlan, value: Any, elapsed_ms: int) -> None:
        if not isinstance(value, ToolResult):
            return
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
        record_confirmation_interrupted(
            owner=plan.owner,
            confirmation_id=plan.plan_id,
            owner_generation=plan.owner_generation,
            tool_name=plan.tool_name,
            risk=self._risk(plan),
            confirmation_contract=self._contract(plan),
        )
