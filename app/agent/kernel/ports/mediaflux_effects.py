"""MediaFlux 副作用的领域后置动作、审计与运行代次收束。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from app import config
from app.agent import effect_completion
from app.agent.action_history import (
    record_confirmation_claimed,
    record_confirmation_error,
    record_confirmation_interrupted,
    record_confirmed_result,
)
from app.modules.activity_undo import preference_compensation, preference_undo_receipt
from app.agent.domains.library.missing_workflow_runtime import (
    MISSING_MEDIA_CANDIDATES_KEY,
    MISSING_MEDIA_FOLLOWUPS_KEY,
    MissingMediaWorkflowRuntime,
)
from app.agent.feature_gate import invalidate_agent_runtime_generation
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.models import RiskLevel, ToolResult
from app.logger import get_logger
from app.modules.guangya_fs_change import discard_fs_change_plan

from ..capabilities import KernelToolSpec, ToolEffect
from ..completion_receipts import EffectCompletionScope
from ..tool_calls import EffectPlan, PreparedEffect

logger = get_logger(__name__)


class MediaFluxEffectLifecycle:
    """复用领域后置动作和既有脱敏审计；辅助故障不改变真实写入结果。"""

    def __init__(
        self, *, missing_media_runtime: MissingMediaWorkflowRuntime | None = None
    ) -> None:
        self.missing_media_runtime = missing_media_runtime

    @staticmethod
    def _configuration_fingerprint() -> str:
        # 只保存摘要，不把配置值写进计划；文件操作不能打断其他会话的只读查询。
        payload = json.dumps(config.all_items(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

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

    def _release_followups(
        self, metadata_source: EffectPlan | PreparedEffect,
        owner_source: EffectPlan | ToolCallContext,
    ) -> None:
        runtime = self.missing_media_runtime
        if runtime is not None:
            runtime.release_followups(
                owner=owner_source.owner,
                followups=self._followups(dict(metadata_source.metadata)),
            )

    def _audit_fields(self, plan: EffectPlan) -> dict[str, Any]:
        return {
            "owner": plan.owner,
            "tool_name": plan.tool_name,
            "risk": self._risk(plan),
            "confirmation_contract": self._contract(plan),
            "confirmation_id": plan.plan_id,
            "owner_generation": plan.owner_generation,
        }

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
        metadata["configuration_fingerprint"] = self._configuration_fingerprint()
        inverse = preference_compensation(tool.name, dict(arguments), dict(prepared.preview))
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
        self._release_followups(prepared, context)
        self._discard_native_cloud_preview(prepared)

    @staticmethod
    def _discard_native_cloud_preview(source: EffectPlan | PreparedEffect) -> None:
        if source.metadata.get("source_kind") != "native_cloud_change":
            return
        if isinstance(source, EffectPlan) and source.tool_name != "cloud.change":
            return
        plan_id = source.metadata.get("fs_change_plan_id")
        if plan_id:
            discard_fs_change_plan(plan_id, preview_only=True)

    def substep_started(self, *, plan: EffectPlan) -> None:
        record_confirmation_claimed(**self._audit_fields(plan), action_arguments=dict(plan.arguments))

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
        try:
            if value.ok and value.status in {"completed", "success"}:
                receipt = preference_undo_receipt(
                    plan.metadata.get("compensation"), value.effect_metadata.get("compensation_after"),
                    key=plan.plan_id, owner=plan.owner,
                )
                if receipt:
                    value.data["receipt_id"] = receipt
                    value.data["undo"] = {"method": "activity.undo", "arguments": {"receipt_id": receipt}}
                    value.suggestions.append("需要恢复操作前设置时，可使用本次回退凭证生成确认计划；不会撤回已经启动的任务。")
        except Exception as exc:  # noqa: BLE001 -- 可选后置凭证失败不得覆写真实写入结果
            # 原操作的事实不能被可选回退凭证失败覆写。
            logger.warning("回退凭证创建失败 type=%s", type(exc).__name__)
            value.suggestions.append("本次操作未生成回退凭证；需要恢复时请先检查当前状态。")
        record_confirmed_result(
            **self._audit_fields(plan),
            result=value,
            elapsed_ms=elapsed_ms,
        )
        # 配置变化才使全局运行快照失效；普通写入由各领域的对象/版本快照防重放。
        # 历史确认卡没有配置摘要时仍保守失效，不能跳过既有保护。
        if (value.ok or (isinstance(value.data, dict) and value.data.get("background_job"))) and (
            plan.metadata.get("configuration_fingerprint") != self._configuration_fingerprint()
        ):
            invalidate_agent_runtime_generation()

    def failed(self, *, plan: EffectPlan, code: str, elapsed_ms: int) -> None:
        self._release_followups(plan, plan)
        record_confirmation_error(
            **self._audit_fields(plan),
            code=code,
            elapsed_ms=elapsed_ms,
        )

    def interrupted(self, *, plan: EffectPlan) -> None:
        self._release_followups(plan, plan)
        record_confirmation_interrupted(
            **self._audit_fields(plan),
        )

    def cancelled(self, *, plan: EffectPlan) -> None:
        self._release_followups(plan, plan)
        self._discard_native_cloud_preview(plan)

    async def register_terminal_failure(
        self, *, scope: EffectCompletionScope, tool: str, result: Mapping[str, Any],
    ) -> bool:
        return await effect_completion.register_terminal_failure(scope, tool=tool, result=result)
