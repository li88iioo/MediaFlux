"""Media Agent 的结构化模型与安全响应协议。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from functools import wraps
from typing import Any


class RiskLevel(str, Enum):
    """工具风险等级；首期只允许 READ 自动执行。"""

    READ = "read"
    LOW_WRITE = "low_write"
    WRITE = "write"
    DANGER = "danger"


@dataclass(frozen=True)
class Evidence:
    source: str
    description: str
    collected_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ToolReference:
    """领域结果交给 Kernel 持久化的私有引用值。

    引用内容不会进入公开 DTO 或模型上下文；ToolPipeline 会把它转换为
    owner/session 绑定的 opaque ref。领域层只声明引用类型和值，不接触
    Kernel 的引用存储实现。
    """

    kind: str
    value: Any
    ttl_seconds: int = 900


@dataclass
class ToolResult:
    ok: bool
    status: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    error: str = ""
    model_data: dict[str, Any] | None = None
    references: list[ToolReference] = field(default_factory=list, repr=False)
    effect_metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "summary": self.summary,
            "data": self.data,
            "evidence": [item.to_dict() for item in self.evidence],
            "suggestions": list(self.suggestions),
            "error": self.error,
        }

    def to_model_dict(self) -> dict[str, Any]:
        """返回供模型继续规划的紧凑 DTO；公开结果仍使用 ``to_dict``。"""
        value = self.to_dict()
        if self.model_data is not None:
            value["data"] = self.model_data
        return value


ArgumentsValidator = Callable[[dict[str, Any]], dict[str, Any]]
ToolHandler = Callable[[dict[str, Any]], ToolResult]
PostWriteVerifier = Callable[[dict[str, Any], ToolResult], ToolResult]


@dataclass(frozen=True)
class ToolContext:
    """仅由服务端注入的调用身份；绝不来自模型或工具参数。"""

    owner: str = ""
    session_id: str = ""
    request_id: str = ""
    confirmation_bootstrap: bool = False


ContextualToolHandler = Callable[[dict[str, Any], ToolContext], ToolResult]
ContextFreeConfirmedToolHandler = Callable[[dict[str, Any], str], ToolResult]
ContextualConfirmedToolHandler = Callable[
    [dict[str, Any], str, ToolContext], ToolResult
]
ContextFreeConfirmationPreparer = Callable[[dict[str, Any]], tuple[ToolResult, str]]
ContextualConfirmationPreparer = Callable[
    [dict[str, Any], ToolContext], tuple[ToolResult, str]
]


@dataclass(frozen=True, kw_only=True)
class ToolSpec:
    """领域 action 的原子能力声明；由 Kernel 适配为统一工具契约。"""

    name: str
    description: str
    risk: RiskLevel
    parameters: dict[str, Any]
    validator: ArgumentsValidator
    handler: ToolHandler | None = None
    requires_confirmation: bool = False
    context_handler: ContextualToolHandler | None = None
    context_confirmation_preparer: ContextualConfirmationPreparer | None = None
    context_confirmed_handler: ContextualConfirmedToolHandler | None = None
    post_write_verifier: PostWriteVerifier | None = None
    model_name: str = ""
    related_tools: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    source_kind: str = "system_state"
    freshness: str = "snapshot"
    workflow: str = ""
    workflow_stage: int = 0
    examples: tuple[str, ...] = ()

    @staticmethod
    def context_free_confirmation_preparer(
        handler: ContextFreeConfirmationPreparer,
    ) -> ContextualConfirmationPreparer:
        """把无需调用身份的预检函数绑定到唯一的上下文确认协议。"""

        @wraps(handler)
        def adapted(
            arguments: dict[str, Any],
            _context: ToolContext,
        ) -> tuple[ToolResult, str]:
            return handler(arguments)

        return adapted

    @staticmethod
    def context_free_confirmed_handler(
        handler: ContextFreeConfirmedToolHandler,
    ) -> ContextualConfirmedToolHandler:
        """把无需调用身份的执行函数绑定到唯一的上下文确认协议。"""

        @wraps(handler)
        def adapted(
            arguments: dict[str, Any],
            expected_context: str,
            _context: ToolContext,
        ) -> ToolResult:
            return handler(arguments, expected_context)

        return adapted

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk.value,
            "parameters": self.parameters,
            "requires_confirmation": self.requires_confirmation,
        }
