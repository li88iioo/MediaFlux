"""仅供领域回归测试使用的 Kernel EffectPlan 驱动器。

它不提供自然语言路由，也不进入生产代码；用途是让既有领域动作测试直接经过
ToolPipeline -> EffectPlan -> execute_confirmed 的真实生命周期。
"""

from __future__ import annotations

from app.agent.kernel.ports.sdk_effects import cloud_organize_tools

import asyncio
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.agent.confirmation import ConfirmationStore
from app.agent.errors import AgentToolError, ToolPipelineError
from app.agent.kernel.bootstrap import _build_domain_dependencies
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, InMemoryRateLimiter
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.sdk_effects import subscription_tools, policy_config_tool, local_task_tools, automation_tools, indexer_configuration_tool, library_audit_job_tools, local_source_tools, activity_undo_tool, activity_follow_tools, preferences_tools, schedule_policy_tools, watchlist_tools
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import CancellationToken, SessionBusyError
from app.agent.kernel.tool_calls import PipelineResult, ToolCallContext
from app.agent.models import ToolContext


async def _ignore_progress(_payload: Mapping[str, Any]) -> None:
    return None


@dataclass(slots=True)
class _PreparedTurn:
    owner: str
    session_id: str
    result: PipelineResult


class KernelTestRegistry:
    """测试可读视图；只包装 Kernel catalog，不复制生产注册逻辑。"""

    def __init__(self, harness: KernelDomainTestHarness) -> None:
        self._harness = harness

    def capabilities(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk": str(
                    tool.metadata.get("risk")
                    or (
                        "read"
                        if tool.effect is ToolEffect.READ
                        else "danger"
                        if tool.effect is ToolEffect.DANGER
                        else "write"
                    )
                ),
                "parameters": dict(tool.input_schema),
                "requires_confirmation": tool.effect is not ToolEffect.READ,
            }
            for tool in self._harness.pipeline.catalog.visible({})
        ]

    def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> tuple[Any, int]:
        tool = self._harness.pipeline.catalog.get(name)
        if tool.effect is not ToolEffect.READ:
            raise AgentToolError("该操作需要确认", code="confirmation_required")
        response = self._harness.invoke(
            name,
            arguments or {},
            owner=(context.owner if context else "test-owner"),
        )
        return response["result"], 1


class KernelDomainTestHarness:
    """同步测试外壳，内部只调用真实异步 Kernel pipeline。"""

    def __init__(self) -> None:
        self.state = InMemorySessionStateStore()
        (
            missing_media_runtime,
        ) = _build_domain_dependencies()
        self.catalog = ToolCatalog()
        from app.agent.kernel.ports.sdk_effects import (
            playback_tools,
            resource_download_tool,
            rss_configuration_tools,
            rss_delivery_tools,
            share_transfer_tool,
        )
        for tool in (*subscription_tools(), policy_config_tool(), *cloud_organize_tools(), *local_task_tools(), *automation_tools(), indexer_configuration_tool(), *library_audit_job_tools(), *local_source_tools(), *rss_configuration_tools(), *rss_delivery_tools(), activity_undo_tool(), resource_download_tool(), share_transfer_tool(), *playback_tools(), *watchlist_tools(), *preferences_tools(), *schedule_policy_tools(), *activity_follow_tools()):
            self.catalog.register(tool)
        self.pipeline = ToolPipeline(
            catalog=self.catalog,
            state_store=self.state,
            effect_store=ConfirmationEffectPlanStore(
                ConfirmationStore(),
                record_actions=True,
            ),
            rate_limiter=InMemoryRateLimiter(limit=10_000),
            effect_lifecycle=MediaFluxEffectLifecycle(
                missing_media_runtime=missing_media_runtime
            ),
        )
        self.registry = KernelTestRegistry(self)
        self._prepared: dict[str, _PreparedTurn] = {}

    def capabilities(self) -> dict[str, Any]:
        return {"tools": self.registry.capabilities()}

    @staticmethod
    def _session(owner: str) -> str:
        return f"kernel-domain-{abs(hash(owner)) & 0xFFFFFFFF:08x}"

    async def _context(self, owner: str, *, begin: bool) -> ToolCallContext:
        session_id = self._session(owner)
        if begin:
            lease, _state = await self.state.begin_turn(
                owner=owner,
                session_id=session_id,
                request_id=secrets.token_urlsafe(12),
            )
        else:
            current = await self.state.load(owner=owner, session_id=session_id)
            if current.generation <= 0:
                raise AgentToolError("没有待确认计划", code="confirmation_invalid")
            from app.agent.kernel.state_types import PublicationLease

            lease = PublicationLease(
                owner=owner,
                session_id=session_id,
                generation=current.generation,
                turn_id=secrets.token_urlsafe(12),
                request_id=secrets.token_urlsafe(12),
            )
        return ToolCallContext(
            owner=owner,
            session_id=session_id,
            request_id=lease.request_id,
            turn_id=lease.turn_id,
            lease=lease,
            cancellation=CancellationToken(),
            report_progress=_ignore_progress,
        )

    @staticmethod
    def _response(result: PipelineResult, *, mode: str) -> dict[str, Any]:
        response: dict[str, Any] = {
            "mode": mode,
            "tool_call": {
                "name": result.tool.name,
                "argument_keys": sorted(result.arguments),
            },
            "result": dict(result.outcome.public_content),
        }
        if result.effect_plan is not None:
            plan = result.effect_plan.public_dict()
            plan["status"] = (
                "awaiting_approval" if mode == "confirmation_required" else "completed"
            )
            plan.setdefault(
                "decisions",
                [
                    {"id": "confirm", "label": "执行"},
                    {"id": "cancel", "label": "取消"},
                ],
            )
            response["action_plan"] = plan
        return response

    async def _prepare_async(
        self, name: str, arguments: dict[str, Any], owner: str
    ) -> dict[str, Any]:
        context = await self._context(owner, begin=True)
        result = await self.pipeline.execute(name, arguments, context=context)
        if result.effect_plan is None:
            raise AgentToolError("工具不是写操作", code="confirmation_not_supported")
        self._prepared[result.effect_plan.plan_id] = _PreparedTurn(
            owner=owner,
            session_id=context.session_id,
            result=result,
        )
        return self._response(result, mode="confirmation_required")

    def prepare(
        self, name: str, arguments: dict[str, Any], *, owner: str = "test-owner"
    ) -> dict[str, Any]:
        try:
            return asyncio.run(self._prepare_async(name, arguments, owner))
        except ToolPipelineError as exc:
            raise AgentToolError(str(exc), code=exc.code) from exc

    async def _confirm_async(self, plan_id: str, owner: str) -> dict[str, Any]:
        prepared = self._prepared.get(plan_id)
        if prepared is None or prepared.owner != owner:
            raise AgentToolError("确认计划无效", code="confirmation_invalid")
        context = await self._context(owner, begin=False)
        try:
            result = await self.pipeline.execute_confirmed(plan_id, context=context)
        except SessionBusyError as exc:
            # 与生产 Session 的 effect_in_progress 对齐；未领取的票据可稍后重试。
            raise AgentToolError("另一项会话操作正在执行，请稍后重试。", code="effect_in_progress") from exc
        except ToolPipelineError as exc:
            if exc.code in {
                "confirmation_invalid",
                "confirmation_stale",
                "tool_not_found",
                "authorization_denied",
                "agent_disabled",
            }:
                raise AgentToolError(str(exc), code=exc.code) from exc
            assert prepared.result.effect_plan is not None
            return {
                "mode": "result",
                "result": {
                    "ok": False,
                    "status": exc.code,
                    "summary": str(exc),
                    "data": {},
                    "evidence": [],
                    "suggestions": [],
                    "error": str(exc),
                },
                "action_plan": {
                    **prepared.result.effect_plan.public_dict(),
                    "status": "failed",
                },
            }
        return self._response(result, mode="result")

    def confirm(self, plan_id: str, *, owner: str = "test-owner") -> dict[str, Any]:
        return asyncio.run(self._confirm_async(plan_id, owner))

    async def _invoke_async(
        self, name: str, arguments: dict[str, Any], owner: str
    ) -> dict[str, Any]:
        context = await self._context(owner, begin=True)
        result = await self.pipeline.execute(name, arguments, context=context)
        if result.effect_plan is not None:
            raise AgentToolError("写操作只能预检", code="confirmation_required")
        return self._response(result, mode="result")

    def invoke(
        self, name: str, arguments: dict[str, Any], *, owner: str = "test-owner"
    ) -> dict[str, Any]:
        try:
            return asyncio.run(self._invoke_async(name, arguments, owner))
        except ToolPipelineError as exc:
            raise AgentToolError(str(exc), code=exc.code) from exc


_ACTIVE: KernelDomainTestHarness | None = None


def get_kernel_test_service() -> KernelDomainTestHarness:
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = KernelDomainTestHarness()
    return _ACTIVE


def reset_kernel_test_service() -> None:
    global _ACTIVE
    _ACTIVE = None


def build_kernel_test_registry() -> KernelTestRegistry:
    return get_kernel_test_service().registry
