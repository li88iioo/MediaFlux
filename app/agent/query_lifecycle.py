"""跨入口共享的 Agent 查询确认生命周期契约。"""
from __future__ import annotations

from typing import Any, Protocol

from app.agent.registry import AgentToolError


class QueryConfirmationLifecycle(Protocol):
    """所有 Agent 查询入口必须实现的确认世代能力。"""

    def begin_query_confirmation_epoch(self, *, owner: str) -> int: ...

    def invalidate_query_confirmation_epoch(self, *, owner: str) -> int: ...


def _lifecycle_method(service: Any, name: str):
    method = getattr(service, name, None)
    if not callable(method):
        raise AgentToolError(
            "确认生命周期暂不可用，请稍后重试",
            code="confirmation_unavailable",
        )
    return method


def _lifecycle_integer(value: Any, *, minimum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        raise AgentToolError(
            "确认生命周期返回了无效状态，请稍后重试",
            code="confirmation_unavailable",
        )
    return value


def begin_query_confirmation_epoch(
    service: QueryConfirmationLifecycle, *, owner: str
) -> int:
    """开始新查询并返回确认世代；缺失能力时显式拒绝而非绕过。"""
    begin = _lifecycle_method(service, "begin_query_confirmation_epoch")
    return _lifecycle_integer(begin(owner=owner), minimum=1)


def invalidate_query_confirmation_epoch(
    service: QueryConfirmationLifecycle, *, owner: str
) -> int:
    """使 owner 现有确认失效；缺失能力时显式拒绝而非静默跳过。"""
    invalidate = _lifecycle_method(service, "invalidate_query_confirmation_epoch")
    return _lifecycle_integer(invalidate(owner=owner), minimum=0)
