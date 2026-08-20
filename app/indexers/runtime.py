"""Indexer 运行时单例与配置装配。"""
from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any, TypeVar

from app import config

from .config import DEFAULT_INDEXER_SITE_IDS
from .registry import build_default_registry
from .result_store import IndexerResultStore
from .service import IndexerService

_T = TypeVar("_T")

_service: IndexerService | None = None
_lock = threading.Lock()
_runtime_loop: asyncio.AbstractEventLoop | None = None
_runtime_loop_lock = threading.Lock()
_runtime_stopping = False
_runtime_futures: set[Future[Any]] = set()


def _close_awaitable(awaitable: Awaitable[Any]) -> None:
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()


async def _await_value(awaitable: Awaitable[_T]) -> _T:
    return await awaitable


def _as_coroutine(awaitable: Awaitable[_T]):
    return awaitable if inspect.iscoroutine(awaitable) else _await_value(awaitable)


def _forget_runtime_future(future: Future[Any]) -> None:
    with _runtime_loop_lock:
        _runtime_futures.discard(future)


def _submit_to_runtime_loop(
    awaitable: Awaitable[_T], owner_loop: asyncio.AbstractEventLoop
) -> Future[_T]:
    coroutine = _as_coroutine(awaitable)
    try:
        with _runtime_loop_lock:
            if (
                _runtime_stopping
                or _runtime_loop is not owner_loop
                or owner_loop.is_closed()
                or not owner_loop.is_running()
            ):
                raise RuntimeError("索引器运行时正在关闭")
            future = asyncio.run_coroutine_threadsafe(coroutine, owner_loop)
            _runtime_futures.add(future)
    except Exception:
        _close_awaitable(coroutine)
        if coroutine is not awaitable:
            _close_awaitable(awaitable)
        raise
    future.add_done_callback(_forget_runtime_future)
    return future


def bind_indexer_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """绑定 ASGI 生命周期事件循环，供同步 Agent 安全调用异步索引器。"""
    if loop.is_closed():
        raise RuntimeError("不能绑定已关闭的索引器事件循环")
    global _runtime_loop, _runtime_stopping
    with _runtime_loop_lock:
        if _runtime_stopping and _runtime_loop is not None and _runtime_loop is not loop:
            raise RuntimeError("上一次索引器运行时尚未安全收敛，请重启进程")
        _runtime_loop = loop
        _runtime_stopping = False


def begin_indexer_shutdown(loop: asyncio.AbstractEventLoop) -> int:
    """停止接收跨线程调用并取消尚未完成的桥接任务。"""
    global _runtime_stopping
    with _runtime_loop_lock:
        if _runtime_loop is not loop:
            return 0
        _runtime_stopping = True
        pending = tuple(_runtime_futures)
    for future in pending:
        future.cancel()
    return len(pending)


def unbind_indexer_event_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """仅解除当前绑定，避免旧应用实例清除后来者的事件循环。"""
    global _runtime_loop, _runtime_stopping
    pending: tuple[Future[Any], ...] = ()
    with _runtime_loop_lock:
        if loop is None or _runtime_loop is loop:
            _runtime_loop = None
            _runtime_stopping = False
            pending = tuple(_runtime_futures)
            _runtime_futures.clear()
    for future in pending:
        future.cancel()


def run_indexer_awaitable_sync(
    awaitable: Awaitable[_T],
    *,
    timeout_seconds: float | None = None,
) -> _T:
    """从同步请求线程在索引器所属事件循环执行 awaitable。

    FastAPI 的同步 Agent 路由运行在线程池；若直接使用 ``asyncio.run``，
    全局 ``httpx.AsyncClient`` 会绑定到短命事件循环，随后在应用主循环关闭时
    触发跨循环错误。应用运行期间改为提交到生命周期主循环；无 ASGI 生命周期
    的单元测试和脚本仍保留独立事件循环回退。
    """
    if timeout_seconds is not None and timeout_seconds <= 0:
        _close_awaitable(awaitable)
        raise TimeoutError("索引器调用超时")

    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    with _runtime_loop_lock:
        owner_loop = _runtime_loop
        runtime_stopping = _runtime_stopping
    if runtime_stopping:
        _close_awaitable(awaitable)
        raise RuntimeError("索引器运行时正在关闭")
    if owner_loop is not None and (owner_loop.is_closed() or not owner_loop.is_running()):
        owner_loop = None

    if owner_loop is not None:
        if current_loop is owner_loop:
            _close_awaitable(awaitable)
            raise RuntimeError("同步索引器桥接不能阻塞其所属事件循环")
        future = _submit_to_runtime_loop(awaitable, owner_loop)
        try:
            return future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
            future.cancel()
            raise TimeoutError("索引器调用超时") from None

    if current_loop is not None:
        _close_awaitable(awaitable)
        raise RuntimeError("同步索引器桥接不能在活动事件循环线程中运行")

    coroutine = _as_coroutine(awaitable)
    if timeout_seconds is None:
        return asyncio.run(coroutine)

    async def _with_timeout() -> _T:
        return await asyncio.wait_for(coroutine, timeout=timeout_seconds)

    return asyncio.run(_with_timeout())


async def run_indexer_awaitable(
    awaitable: Awaitable[_T],
    *,
    timeout_seconds: float | None = None,
) -> _T:
    """在 Indexer 所属事件循环执行异步调用。

    Web 请求本来就在 ASGI 生命周期循环中，可直接等待；媒体订阅调度器等
    后台线程会创建自己的事件循环，此时必须把协程提交回生命周期循环，避免
    全局 ``httpx.AsyncClient`` 在多个事件循环之间交叉使用。
    """
    if timeout_seconds is not None and timeout_seconds <= 0:
        _close_awaitable(awaitable)
        raise TimeoutError("索引器调用超时")

    current_loop = asyncio.get_running_loop()
    with _runtime_loop_lock:
        owner_loop = _runtime_loop
        runtime_stopping = _runtime_stopping
    if runtime_stopping:
        _close_awaitable(awaitable)
        raise RuntimeError("索引器运行时正在关闭")
    if owner_loop is not None and (owner_loop.is_closed() or not owner_loop.is_running()):
        owner_loop = None

    if owner_loop is None or current_loop is owner_loop:
        if timeout_seconds is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)

    try:
        future = _submit_to_runtime_loop(awaitable, owner_loop)
    except RuntimeError:
        raise
    wrapped = asyncio.wrap_future(future, loop=current_loop)
    try:
        if timeout_seconds is None:
            return await wrapped
        return await asyncio.wait_for(wrapped, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        future.cancel()
        raise TimeoutError("索引器调用超时") from None


def _csv(value: str) -> list[str]:
    return list(dict.fromkeys(part.strip().lower() for part in str(value or "").split(",") if part.strip()))


def _bounded_int(key: str, default: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(config.get_int(key, default), maximum))


def _user_agent() -> str:
    raw = str(config.get("INDEXER_USER_AGENT", "MediaFlux/1.0") or "")
    value = raw.strip()
    if not value or len(value) > 256 or "\r" in raw or "\n" in raw:
        return "MediaFlux/1.0"
    return value


def build_indexer_service() -> IndexerService:
    registry = build_default_registry(
        user_agent=_user_agent(),
        btbtla_min_interval_seconds=_bounded_int(
            "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS", 5, 0, 60
        ),
        onelou_min_interval_seconds=_bounded_int(
            "INDEXER_1LOU_MIN_INTERVAL_SECONDS", 5, 0, 10
        ),
        onelou_google_enabled=config.get_bool("INDEXER_1LOU_GOOGLE_ENABLED", True),
    )
    configured = _csv(
        config.get("INDEXER_ENABLED_SITES", ",".join(DEFAULT_INDEXER_SITE_IDS))
    )
    if config.get_bool("INDEXER_SUKEBEI_ENABLED", False) and "sukebei" not in configured:
        configured.append("sukebei")
    enabled = [site_id for site_id in configured if site_id in registry.ids()]
    return IndexerService(
        registry=registry,
        result_store=IndexerResultStore(
            ttl_seconds=_bounded_int("INDEXER_RESULT_TTL_SECONDS", 600, 60, 3600),
            max_entries=10_000,
        ),
        site_timeout_seconds=_bounded_int("INDEXER_SITE_TIMEOUT_SECONDS", 10, 2, 60),
        total_timeout_seconds=_bounded_int("INDEXER_TOTAL_TIMEOUT_SECONDS", 15, 3, 120),
        max_results_per_site=_bounded_int("INDEXER_MAX_RESULTS_PER_SITE", 40, 1, 100),
        max_concurrency=_bounded_int("INDEXER_MAX_CONCURRENCY", 5, 1, 10),
        cache_ttl_seconds=_bounded_int("INDEXER_CACHE_TTL_SECONDS", 120, 30, 600),
        enabled_site_ids=enabled,
    )


# 兼容既有测试与内部调用；新代码使用公开名称。
_build_service = build_indexer_service


def get_indexer_service() -> IndexerService:
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = build_indexer_service()
    return _service


async def shutdown_indexer_service() -> None:
    global _service
    with _lock:
        service, _service = _service, None
    if service is not None:
        await service.aclose()
