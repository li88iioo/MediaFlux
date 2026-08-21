"""Agent 链路追踪与安全异常摘要。"""
from __future__ import annotations

from contextvars import ContextVar, Token
import re
import secrets
from typing import Any

from app.agent.models import ToolContext
from app.sensitive_data import redact_sensitive_text

_TRACE_CONTEXT: ContextVar[ToolContext | None] = ContextVar(
    "mediaflux_agent_trace_context", default=None
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")


def current_tool_context(
    *,
    owner: str = "",
    session_id: str = "",
    request_id: str = "",
) -> ToolContext:
    """合并显式身份与当前请求上下文；请求 ID 缺失时只生成一次。"""
    current = _TRACE_CONTEXT.get() or ToolContext()
    return ToolContext(
        owner=str(owner or current.owner or "").strip(),
        session_id=str(session_id or current.session_id or "").strip(),
        request_id=str(request_id or current.request_id or secrets.token_urlsafe(12)).strip(),
    )


def begin_trace_context(
    *, owner: str = "", session_id: str = "", request_id: str = ""
) -> tuple[Token[ToolContext | None], ToolContext]:
    context = current_tool_context(
        owner=owner, session_id=session_id, request_id=request_id
    )
    return _TRACE_CONTEXT.set(context), context


def end_trace_context(token: Token[ToolContext | None]) -> None:
    _TRACE_CONTEXT.reset(token)


def current_request_id() -> str:
    current = _TRACE_CONTEXT.get()
    return current.request_id if current is not None else secrets.token_urlsafe(12)


def safe_exception_summary(exc: BaseException, *, limit: int = 240) -> str:
    """生成可用于日志的脱敏、单行、长度受控异常摘要。"""
    type_name = type(exc).__name__
    text = redact_sensitive_text(str(exc or ""))
    text = _SPACE_RE.sub(" ", _CONTROL_RE.sub(" ", text)).strip()
    if not text:
        return type_name
    safe_limit = max(32, min(int(limit), 1000))
    summary = f"{type_name}: {text}"
    if len(summary) > safe_limit:
        summary = summary[: safe_limit - 1].rstrip() + "…"
    return summary


def safe_trace_value(value: Any, *, limit: int = 128) -> str:
    text = _SPACE_RE.sub(" ", _CONTROL_RE.sub(" ", str(value or ""))).strip()
    return text[: max(1, int(limit))]
