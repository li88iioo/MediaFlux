"""仅供领域回归测试使用的 Kernel EffectPlan 驱动器。

它不提供自然语言路由，也不进入生产代码；用途是让既有领域动作测试直接经过
ToolPipeline -> EffectPlan -> execute_confirmed 的真实生命周期。
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any

from app.agent.confirmation import ConfirmationStore
from app.agent.domain_catalog import build_tool_specs
from app.agent.errors import AgentToolError
from app.agent.kernel.bootstrap import _configure_domain_contexts
from app.agent.kernel.capabilities import ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.pipeline import (
    InMemoryRateLimiter,
    PipelineResult,
    ToolCallContext,
    ToolPipeline,
    ToolPipelineError,
)
from app.agent.kernel.ports.existing_actions import catalog_from_tool_specs
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.state import CancellationToken, InMemorySessionStateStore
from app.agent.models import ToolContext


async def _ignore_progress(_payload: dict[str, Any]) -> None:
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
            _repository,
            resource_store,
            ingest_store,
            missing_media_runtime,
        ) = _configure_domain_contexts()
        self.recent_resource_store = resource_store
        self.catalog = catalog_from_tool_specs(
            build_tool_specs(
                resource_store,
                ingest_store,
                missing_media_runtime=missing_media_runtime,
            )
        )
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
            from app.agent.kernel.state import PublicationLease

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
                "argument_keys": sorted(str(key) for key in result.arguments),
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
        except ToolPipelineError as exc:
            if exc.code in {
                "confirmation_invalid",
                "confirmation_stale",
                "tool_not_found",
                "authorization_denied",
                "agent_disabled",
            }:
                raise AgentToolError(str(exc), code=exc.code) from exc
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
