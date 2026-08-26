"""请求级 Agent 进度事件。

事件只描述受控执行阶段和内部工具标识；具体入口必须把工具名映射为公开名称，
不得直接向用户展示这里的原始字段。监听器失败不能影响 Agent 主流程。
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentProgressEvent:
    """一次短生命周期执行进度，不携带工具参数或原始结果。"""

    phase: str
    tool_name: str = ""
    ok: bool | None = None


AgentProgressListener = Callable[[AgentProgressEvent], None]
_AGENT_PROGRESS_LISTENER: ContextVar[AgentProgressListener | None] = ContextVar(
    "mediaflux_agent_progress_listener",
    default=None,
)


@contextmanager
def bind_agent_progress_listener(
    listener: AgentProgressListener | None,
) -> Iterator[None]:
    """把进度监听器限制在当前请求上下文内。"""

    token = _AGENT_PROGRESS_LISTENER.set(listener if callable(listener) else None)
    try:
        yield
    finally:
        _AGENT_PROGRESS_LISTENER.reset(token)


def emit_agent_progress(
    phase: str,
    *,
    tool_name: str = "",
    ok: bool | None = None,
) -> None:
    """尽力发布进度；监听器异常严格与 Agent 执行隔离。"""

    listener = _AGENT_PROGRESS_LISTENER.get()
    if listener is None:
        return
    event = AgentProgressEvent(
        phase=str(phase or "").strip()[:40],
        tool_name=str(tool_name or "").strip()[:120],
        ok=ok if isinstance(ok, bool) else None,
    )
    if not event.phase:
        return
    try:
        listener(event)
    except Exception:
        return
