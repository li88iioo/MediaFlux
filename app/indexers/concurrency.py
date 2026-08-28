"""Indexer 跨事件循环可复用的轻量异步互斥。"""
from __future__ import annotations

import asyncio
import threading


class CrossLoopAsyncLock:
    """不绑定具体事件循环的异步互斥锁。

    Indexer runtime 通常固定在单一事件循环，但测试、脚本和独立调用可能让同一
    adapter 被不同线程的事件循环复用。标准 ``asyncio.Lock`` 会绑定其中一个
    loop；这里用线程锁做原子占用，等待阶段仅短暂让出当前 loop，取消时不会遗留
    一个稍后才获得、却无人释放的后台锁。
    """

    def __init__(self, *, poll_interval: float = 0.005) -> None:
        self._lock = threading.Lock()
        self._poll_interval = max(0.001, float(poll_interval))

    async def acquire(self) -> bool:
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(self._poll_interval)
        return True

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> "CrossLoopAsyncLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.release()
