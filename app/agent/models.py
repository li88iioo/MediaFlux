"""Media Agent 的结构化模型与安全响应协议。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Callable


class RiskLevel(str, Enum):
    """工具风险等级；首期只允许 READ 自动执行。"""

    READ = "read"
    LOW_WRITE = "low_write"
    WRITE = "write"
    DANGER = "danger"


class LLMToolDisposition(str, Enum):
    """LLM 选中工具后，服务端允许采取的唯一动作。"""

    EXECUTE_READ = "execute_read"
    PREPARE_CONFIRMATION = "prepare_confirmation"


@dataclass(frozen=True)
class Evidence:
    source: str
    description: str
    collected_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class ToolResult:
    ok: bool
    status: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    error: str = ""

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
ConfirmationFollowupResolver = Callable[[ToolContext], dict[str, Any]]
ContextFreeConfirmedToolHandler = Callable[[dict[str, Any], str], ToolResult]
ContextualConfirmedToolHandler = Callable[[dict[str, Any], str, ToolContext], ToolResult]
ContextFreeConfirmationPreparer = Callable[[dict[str, Any]], tuple[ToolResult, str]]
ContextualConfirmationPreparer = Callable[
    [dict[str, Any], ToolContext], tuple[ToolResult, str]
]


@dataclass(frozen=True, kw_only=True)
class ToolSpec:
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
    # LLM 能力声明必须跟随工具本身，避免路由器维护另一份易漂移白名单。
    # 这些字段只控制“模型可见性”，不会改变风险等级或绕过确认门。
    llm_read: bool = False
    llm_read_plan: bool = False
    llm_confirmation: bool = False
    native_alias: str = ""
    # 只读预览可声明唯一的确认型续接工具。该字段只生成“准备行动计划”入口，
    # 不会执行写操作；最终写入仍必须消费一次性确认票据。
    confirmation_followup: str = ""
    # 少数预览（如 Provider 写计划）需要从 owner/session 私有状态恢复
    # 不透明参数；解析器只生成目标工具参数，仍会经过目标 validator 与确认门。
    confirmation_followup_resolver: ConfirmationFollowupResolver | None = None
    # 结果的主展示语义跟随工具定义，避免编排器和消息渠道维护工具名白名单。
    result_presentation: str = "narrative"
    stages_resource_candidates: bool = False
    # 领域能力语义只参与模型候选召回与执行展示，不授予任何工具权限。
    llm_domains: tuple[str, ...] = ()
    llm_source_kind: str = "system_state"
    llm_evidence_role: str = "primary"
    llm_freshness: str = "snapshot"
    llm_parallel_safe: bool = True
    # 只用于模型候选召回与能力说明，不参与权限、风险、确认或限流判定。
    llm_examples: tuple[str, ...] = ()

    @staticmethod
    def context_free_confirmation_preparer(
        handler: ContextFreeConfirmationPreparer,
    ) -> ContextualConfirmationPreparer:
        """把无需调用身份的预检函数绑定到唯一的上下文确认协议。"""

        @wraps(handler)
        def adapted(
            arguments: dict[str, Any], _context: ToolContext,
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

    def llm_capability_dict(self) -> dict[str, Any]:
        capability = self.public_dict()
        examples = [
            str(item).strip()[:160]
            for item in self.llm_examples[:6]
            if str(item).strip()
        ]
        if examples:
            capability["examples"] = examples
        capability["semantics"] = {
            "domains": list(self.llm_domains),
            "source_kind": self.llm_source_kind,
            "evidence_role": self.llm_evidence_role,
            "freshness": self.llm_freshness,
            "parallel_safe": self.llm_parallel_safe,
        }
        return capability
