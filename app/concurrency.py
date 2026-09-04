"""跨事件循环与跨线程复用的轻量并发原语。"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field


class CrossLoopAsyncLock:
    """不绑定具体事件循环的异步互斥锁。

    Web、Telegram、脚本和测试可能让同一运行时被不同线程的事件循环复用。
    标准 ``asyncio.Lock`` 在发生等待后会绑定其中一个 loop；这里用线程锁做原子
    占用，等待阶段仅短暂让出当前 loop，取消时不会遗留稍后才获得却无人释放的锁。
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


@dataclass(frozen=True, slots=True)
class SingleFlightLease:
    """一次同键请求的 owner/waiter 身份与代际快照。"""

    key: Hashable
    owner: bool
    generation: int
    tracked: bool
    _event: threading.Event = field(repr=False, compare=False)


class KeyedSingleFlight:
    """合并同一进程内的并发同键同步请求。

    首个调用者成为 owner，其余调用者等待 owner 完成后重新读取权威缓存。
    登记表有明确上限；达到上限时新键以未跟踪 owner 方式继续执行，避免为了
    去重而形成无界内存增长。``clear`` 会释放全部 waiter，并通过代际隔离阻止
    清理前的旧 owner 删除或唤醒清理后创建的新请求。
    """

    def __init__(self, *, max_entries: int = 256) -> None:
        if isinstance(max_entries, bool) or int(max_entries) < 1:
            raise ValueError("max_entries 必须是正整数")
        self._max_entries = int(max_entries)
        self._lock = threading.RLock()
        self._inflight: dict[Hashable, tuple[int, threading.Event]] = {}
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    def reserve(self, key: Hashable) -> SingleFlightLease:
        """登记请求；返回 owner 或等待同键 owner 的 waiter 租约。"""
        with self._lock:
            generation = self._generation
            current = self._inflight.get(key)
            if current is not None:
                return SingleFlightLease(
                    key=key,
                    owner=False,
                    generation=current[0],
                    tracked=True,
                    _event=current[1],
                )

            event = threading.Event()
            if len(self._inflight) >= self._max_entries:
                return SingleFlightLease(
                    key=key,
                    owner=True,
                    generation=generation,
                    tracked=False,
                    _event=event,
                )

            self._inflight[key] = (generation, event)
            return SingleFlightLease(
                key=key,
                owner=True,
                generation=generation,
                tracked=True,
                _event=event,
            )

    @staticmethod
    def wait(lease: SingleFlightLease, *, timeout: float | None) -> bool:
        """等待 owner 完成；owner 自身调用时立即返回。"""
        if lease.owner:
            return True
        if timeout is not None:
            timeout = max(0.0, float(timeout))
        return lease._event.wait(timeout=timeout)

    def finish(self, lease: SingleFlightLease) -> None:
        """由当前代际的已跟踪 owner 完成请求并释放 waiter。"""
        if not lease.owner or not lease.tracked:
            return
        should_release = False
        with self._lock:
            current = self._inflight.get(lease.key)
            if (
                current is not None
                and current[0] == lease.generation
                and current[1] is lease._event
            ):
                self._inflight.pop(lease.key, None)
                should_release = True
        if should_release:
            lease._event.set()

    def run_cached[T](
        self,
        key: Hashable,
        *,
        read_cache: Callable[[], T | None],
        compute: Callable[[], T],
        write_cache: Callable[[T], None],
        unavailable: Callable[[], T],
        timeout: float | None,
    ) -> T:
        """统一 owner 复查、waiter 等待、缓存发布与异常清理。

        调用方可先走缓存快路；登记租约后必须再读一次，防止快路错过的
        owner 已完成并退出。网络计算不持锁，只有缓存发布与 clear 互斥，
        清理前的慢请求不能重新污染缓存。缓存回调不得执行网络请求。
        """
        lease = self.reserve(key)
        try:
            if not lease.owner and not self.wait(lease, timeout=timeout):
                return unavailable()
            cached = read_cache()
            if cached is not None:
                return cached
            if not lease.owner:
                return unavailable()
            result = compute()
            with self._lock:
                if lease.generation == self._generation:
                    write_cache(result)
            return result
        finally:
            # 额度退款或缓存写入失败也必须释放 waiter，不能依赖调用方清理。
            self.finish(lease)

    def clear(self, *, clear_cache: Callable[[], None] | None = None) -> None:
        """原子失效缓存与租约代际；清理失败时也释放当前 waiter。"""
        with self._lock:
            self._generation += 1
            pending = tuple(event for _, event in self._inflight.values())
            self._inflight.clear()
            try:
                if clear_cache is not None:
                    clear_cache()
            finally:
                for event in pending:
                    event.set()
