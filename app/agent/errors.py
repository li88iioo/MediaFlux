"""Agent 领域能力可安全公开的统一异常。"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent.kernel.task_state import TaskPatch
    from app.agent.rate_limit import RateLimitDecision


class AgentToolError(ValueError):
    """可由 ToolPipeline 映射为稳定公开错误的领域异常。"""

    def __init__(self, message: str, *, code: str = "invalid_tool_call",
                 argument_schemas: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.safe_message = message
        self.code = (code or "invalid_tool_call")
        self.argument_schemas = argument_schemas


class ToolPipelineError(RuntimeError):
    def __init__(self, message: str, *, code: str = "tool_failed", task_patch: TaskPatch | None = None,
                 argument_schemas: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.task_patch = task_patch
        self.argument_schemas = argument_schemas
        self.execution_started: bool | None = None
        self.plan_invalidated = False

    def mark_execution_started(self, started: bool) -> None:
        if self.execution_started is not None:
            return
        self.execution_started = started
        if not started:
            self.args = (f"本次未执行业务操作：{self}。原确认已使用，请重新预检后确认。",)

class ToolRateLimitError(ToolPipelineError):
    """Trusted local denial with a fixed, identity-free recovery projection."""

    def __init__(self, decision: RateLimitDecision) -> None:
        super().__init__(
            "MediaFlux 对该能力的本地调用频率已达上限，本次未访问后端；"
            "请稍后重试，不能据此判断后端故障或没有数据。",
            code="rate_limited",
        )
        self._rate_limit: Mapping[str, int | float | str | None] = MappingProxyType({
            "limit": decision.limit,
            "cost": decision.cost,
            "remaining": decision.remaining,
            "retry_after_seconds": decision.retry_after_seconds,
            "retry_at_epoch": decision.retry_at_epoch,
            "reason": decision.reason,
        })

    @property
    def rate_limit(self) -> Mapping[str, int | float | str | None]:
        return self._rate_limit

class _ReadPermitRejected(ToolRateLimitError):
    """Proven pre-handler denial, issued only at the acquire boundary."""

    def __init__(self, error: ToolRateLimitError, tool_name: str) -> None:
        ToolPipelineError.__init__(self, str(error), code=error.code)
        self._rate_limit = error.rate_limit
        self.tool_name = tool_name

class ConfirmationClaimError(ToolPipelineError):
    """未认领票据的请求错误，不代表一次已确认操作的业务终态。"""

    def __init__(self) -> None:
        super().__init__("确认计划无效、已过期或已被使用", code="confirmation_invalid")
