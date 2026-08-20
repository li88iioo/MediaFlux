"""跨入口共享的 Agent 查询生命周期小工具。"""
from __future__ import annotations

from typing import Any


def begin_query_confirmation_epoch(service: Any, *, owner: str) -> int | None:
    """开始新查询并返回确认世代；旧实现或异常返回值安全降级为 ``None``。"""
    begin = getattr(service, "begin_query_confirmation_epoch", None)
    if not callable(begin):
        return None
    generation = begin(owner=owner)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        return None
    return generation


def invalidate_query_confirmation_epoch(service: Any, *, owner: str) -> None:
    """使 owner 现有确认失效；兼容尚未实现该能力的服务。"""
    invalidate = getattr(service, "invalidate_query_confirmation_epoch", None)
    if callable(invalidate):
        invalidate(owner=owner)
