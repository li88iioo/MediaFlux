"""Media Agent 的结构化模型与安全响应协议。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
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


ContextualToolHandler = Callable[[dict[str, Any], ToolContext], ToolResult]
ConfirmedToolHandler = Callable[[dict[str, Any], str], ToolResult]
ContextualConfirmedToolHandler = Callable[[dict[str, Any], str, ToolContext], ToolResult]
ConfirmationContextProvider = Callable[[dict[str, Any]], str]
ConfirmationStateCleaner = Callable[[], None]
ConfirmationPreparer = Callable[[dict[str, Any]], tuple[ToolResult, str]]
ContextualConfirmationPreparer = Callable[
    [dict[str, Any], ToolContext], tuple[ToolResult, str]
]


@dataclass(frozen=True, kw_only=True)
class ToolSpec:
    name: str
    description: str
    risk: RiskLevel
    parameters: dict[str, Any]
    handler: ToolHandler
    validator: ArgumentsValidator
    requires_confirmation: bool = False
    preview_handler: ToolHandler | None = None
    confirmation_context: ConfirmationContextProvider | None = None
    confirmation_state_cleaner: ConfirmationStateCleaner | None = None
    confirmed_handler: ConfirmedToolHandler | None = None
    confirmation_preparer: ConfirmationPreparer | None = None
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
    # 只用于模型候选召回与能力说明，不参与权限、风险、确认或限流判定。
    llm_examples: tuple[str, ...] = ()

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
        return capability
