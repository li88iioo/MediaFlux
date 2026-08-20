"""同步 Agent 工具调用异步 Provider 的显式边界。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

_T = TypeVar("_T")


class AsyncBridgeUnavailable(RuntimeError):
    """同步工具被错误地放在活动事件循环线程中执行。"""


def ensure_sync_bridge_available() -> None:
    """同步工具只能在没有活动事件循环的线程中等待异步结果。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AsyncBridgeUnavailable(
        "同步 Agent Provider 不能在活动事件循环线程中执行；请改用异步调用链"
    )


def run_awaitable_sync(awaitable: Awaitable[_T]) -> _T:
    """在纯同步调用链中运行 awaitable；活动 loop 下快速失败而不阻塞。"""
    try:
        ensure_sync_bridge_available()
    except AsyncBridgeUnavailable:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise
    return asyncio.run(awaitable)
