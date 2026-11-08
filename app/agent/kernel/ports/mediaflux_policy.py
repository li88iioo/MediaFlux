"""MediaFlux 生产身份、功能开关与共享限流策略。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

from app import config, database as db
from app.agent import errors
from app.agent.errors import ToolPipelineError
from app.agent.feature_gate import (
    AgentRuntimeDisabled, RuntimeFenceError, agent_runtime_admission,
    agent_runtime_transition, read_runtime_fence, require_runtime_fence,
    agent_runtime_generation_is_current,
    current_agent_runtime_generation,
    is_agent_enabled,
)
from app.agent.kernel.state_types import AgentInput
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.owner_routes import telegram_owner_route_is_currently_authorized
from app.agent.rate_limit import consume_agent_tool

from ..capabilities import KernelToolSpec
from ..model import ModelAdapter
from ..provider_model import OpenAICompatibleModelAdapter

_WEB_OWNER_RE = re.compile(r"^webk:v1:[0-9a-f]{64}$")


def select_channel_model(agent_input: AgentInput, model: ModelAdapter) -> str:
    if agent_input.channel != "telegram":
        return ""
    from app.modules.telegram_model_preferences import get_telegram_model_preference

    preference_options = {}
    if isinstance(model, OpenAICompatibleModelAdapter):
        preference_options["settings"] = model.settings
    return get_telegram_model_preference(agent_input.owner, agent_input.session_id, **preference_options)


class MediaFluxTurnAdmission:
    """把功能开关代次绑定到整个只读模型/工具回合。"""

    def __init__(self, *, workspace_error: str = "") -> None:
        self.workspace_error = workspace_error

    async def begin(self, agent_input: AgentInput) -> int:
        if not is_agent_enabled():
            raise ToolPipelineError("Media Agent 当前未启用", code="agent_disabled")
        if agent_input.owner.startswith("tg:v1:") and not (
            telegram_owner_route_is_currently_authorized(agent_input.owner)
        ):
            raise ToolPipelineError(
                "当前 Telegram 身份无权使用 Media Agent",
                code="authorization_denied",
            )
        if not (
            _WEB_OWNER_RE.fullmatch(agent_input.owner)
            or agent_input.owner.startswith("tg:v1:")
        ):
            raise ToolPipelineError("Agent 身份无效", code="authorization_denied")
        if self.workspace_error:
            raise ToolPipelineError(self.workspace_error, code="workspace_unavailable")
        return current_agent_runtime_generation()

    async def is_current(self, token: Any, agent_input: AgentInput) -> bool:
        if not isinstance(token, int) or isinstance(token, bool):
            return False
        if not is_agent_enabled() or not agent_runtime_generation_is_current(token):
            return False
        if agent_input.owner.startswith("tg:v1:"):
            return telegram_owner_route_is_currently_authorized(agent_input.owner)
        return bool(_WEB_OWNER_RE.fullmatch(agent_input.owner))


class MediaFluxAuthorizationPolicy:
    """只允许经 Web 登录或 Telegram 白名单派生的生产 principal。"""

    async def authorize(
        self,
        tool: KernelToolSpec,
        arguments: Mapping[str, Any],
        context: ToolCallContext,
    ) -> None:
        del tool, arguments
        if not is_agent_enabled():
            raise ToolPipelineError("Media Agent 当前未启用", code="agent_disabled")
        owner = (context.owner or "")
        if _WEB_OWNER_RE.fullmatch(owner):
            return
        if owner.startswith("tg:v1:"):
            if telegram_owner_route_is_currently_authorized(owner):
                return
            raise ToolPipelineError(
                "当前 Telegram 身份无权使用 Media Agent", code="authorization_denied"
            )
        raise ToolPipelineError("Agent 身份无效", code="authorization_denied")


class MediaFluxToolRateLimiter:
    """复用现有 SQLite 共享预算，不因多 Worker 重置工具限流。"""

    async def acquire(
        self, *, owner: str, tool_name: str, cost: float, arguments: Mapping[str, Any]
    ) -> None:
        del cost
        canonical = (tool_name or "").removeprefix("confirm:")
        # 预览不写入，不能耗尽用户点击确认时的执行额度；两阶段仍按同一身份跨 Worker 限流。
        scope_suffix = "confirmed" if tool_name.startswith("confirm:") else ""
        decision = await asyncio.to_thread(
            consume_agent_tool, owner, canonical, scope_suffix=scope_suffix
        )
        if not decision.allowed:
            raise errors.ToolRateLimitError(decision)


def _production_owner_kind(owner: object) -> str:
    value = str(owner or "")
    if value.startswith("webk:v1:"):
        return "web"
    if value.startswith("tg:v1:"):
        return "telegram"
    return ""


@contextmanager
def confirmation_runtime(context: ToolCallContext):
    """预检与执行使用同一持久身份；配置发布不能穿过确认执行边界。"""
    owner_kind = _production_owner_kind(context.owner)
    if not owner_kind:
        yield ""
        return
    with agent_runtime_transition(), config.runtime_config_read_guard():
        snapshot = config.capture_runtime_config_snapshot()
        with db.get_conn() as conn:
            fence = read_runtime_fence(conn)
            require_runtime_fence(conn, fence)
        if fence.config_digest != snapshot.config_digest:
            raise RuntimeFenceError("runtime_snapshot_mismatch")
        with config.use_runtime_config_snapshot(snapshot), agent_runtime_admission(require_telegram=owner_kind == "telegram"):
            if owner_kind == "telegram" and not telegram_owner_route_is_currently_authorized(context.owner):
                raise AgentRuntimeDisabled("Telegram Agent 授权已变化")
            yield f"mfkr2:{fence.incarnation}:{fence.epoch}:{snapshot.effective_digest}:"
