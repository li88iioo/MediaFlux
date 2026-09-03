"""Agent 领域能力可安全公开的统一异常。"""

from __future__ import annotations


class AgentToolError(ValueError):
    """可由 ToolPipeline 映射为稳定公开错误的领域异常。"""

    def __init__(self, message: str, *, code: str = "invalid_tool_call") -> None:
        super().__init__(message)
        self.safe_message = str(message)
        self.code = str(code or "invalid_tool_call")
