"""Indexer 运行时单例与配置装配。"""
from __future__ import annotations

import atexit
import asyncio
import inspect
import threading
from collections.abc import Awaitable
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any, TypeVar

from app import config
from app.logger import get_logger

from .config import DEFAULT_INDEXER_SITE_IDS
from .registry import build_default_registry
from .result_store import IndexerResultStore
from .service import IndexerService

_T = TypeVar("_T")
logger = get_logger(__name__)

_service: IndexerService | None = None
_lock = threading.Lock()
_runtime_loop: asyncio.AbstractEventLoop | None = None
_runtime_loop_lock = threading.Lock()
_runtime_stopping = False
_runtime_futures: set[Future[Any]] = set()
_standalone_lock = threading.Lock()
_standalone_loop: asyncio.AbstractEventLoop | None = None
_standalone_thread: threading.Thread | None = None


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


def _standalone_loop_main(
    ready: threading.Event,
    holder: dict[str, asyncio.AbstractEventLoop],
) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    holder["loop"] = loop
    ready.set()
    try:
        loop.run_forever()
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


def _ensure_standalone_runtime() -> asyncio.AbstractEventLoop:
    """为无 ASGI 生命周期的同步/脚本调用提供稳定的长期事件循环。"""
    global _runtime_loop, _runtime_stopping, _standalone_loop, _standalone_thread
    with _standalone_lock:
        loop = _standalone_loop
        thread = _standalone_thread
        if loop is not None and thread is not None and thread.is_alive() and loop.is_running():
            return loop
        ready = threading.Event()
        holder: dict[str, asyncio.AbstractEventLoop] = {}
        thread = threading.Thread(
            target=_standalone_loop_main,
            args=(ready, holder),
            name="indexer-standalone-loop",
            daemon=True,
        )
        thread.start()
        if not ready.wait(timeout=5.0):
            raise RuntimeError("索引器独立事件循环启动超时")
        loop = holder.get("loop")
        if loop is None or not loop.is_running():
            raise RuntimeError("索引器独立事件循环启动失败")
        with _runtime_loop_lock:
            if _runtime_loop is not None and _runtime_loop is not loop:
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=5.0)
                raise RuntimeError("索引器运行时已绑定其他事件循环")
            _standalone_loop = loop
            _standalone_thread = thread
            _runtime_loop = loop
            _runtime_stopping = False
        return loop


def _stop_standalone_runtime(timeout_seconds: float = 10.0) -> bool:
    """在其所属循环关闭独立 Indexer 服务，再结束循环线程。"""
    global _runtime_loop, _runtime_stopping, _standalone_loop, _standalone_thread
    with _standalone_lock:
        loop = _standalone_loop
        thread = _standalone_thread
        if loop is None or thread is None:
            return True
        with _runtime_loop_lock:
            if _runtime_loop is loop:
                _runtime_stopping = True
                pending = tuple(_runtime_futures)
            else:
                pending = ()
        for future in pending:
            future.cancel()

        closed = _service is None
        if loop.is_running() and not loop.is_closed():
            shutdown_awaitable = shutdown_indexer_service()
            try:
                future = asyncio.run_coroutine_threadsafe(shutdown_awaitable, loop)
            except Exception as exc:
                _close_awaitable(shutdown_awaitable)
                logger.warning(
                    "提交 Indexer 独立运行时关闭失败 type=%s", type(exc).__name__
                )
                return False
            try:
                future.result(timeout=max(0.1, float(timeout_seconds)))
                closed = True
            except Exception as exc:
                future.cancel()
                # service/client 必须在其所属 loop 上重试关闭。首次失败时保留
                # loop、thread 和 stopping 栅栏，不能让后续调用创建第二套运行时。
                logger.warning(
                    "关闭 Indexer 独立运行时失败 type=%s", type(exc).__name__
                )
                return False
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass

        if thread is not threading.current_thread():
            thread.join(timeout=max(0.1, float(timeout_seconds)))
        stopped = not thread.is_alive()
        if not (closed and stopped):
            return False

        with _runtime_loop_lock:
            if _runtime_loop is loop:
                _runtime_loop = None
                _runtime_stopping = False
                _runtime_futures.clear()
            if _standalone_loop is loop:
                _standalone_loop = None
            if _standalone_thread is thread:
                _standalone_thread = None
        return True


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
        standalone_bound = (
            _runtime_loop is not None
            and _runtime_loop is _standalone_loop
            and _runtime_loop is not loop
        )
    if standalone_bound and not _stop_standalone_runtime():
        raise RuntimeError("索引器独立运行时未能安全停止")
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
    with _runtime_loop_lock:
        standalone_bound = _runtime_loop is not None and _runtime_loop is _standalone_loop
    if standalone_bound and (loop is None or loop is _standalone_loop):
        _stop_standalone_runtime()
        return
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

    if owner_loop is None and current_loop is None:
        owner_loop = _ensure_standalone_runtime()

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

    _close_awaitable(awaitable)
    raise RuntimeError("同步索引器桥接不能在活动事件循环线程中运行")


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

    if owner_loop is None:
        owner_loop = _ensure_standalone_runtime()
    if current_loop is owner_loop:
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
    site_timeout_seconds = _bounded_int("INDEXER_SITE_TIMEOUT_SECONDS", 10, 2, 60)
    # Nyaa 有主站和镜像两个端点；单端点预算必须显著短于总站点预算，
    # 否则主站卡满后服务层会取消整个适配器，镜像永远没有执行机会。
    nyaa_endpoint_timeout_seconds = max(0.5, min(4.0, site_timeout_seconds * 0.4))
    # 1LOU 的两个可信入口当前偶有应用层长时间无响应；限制单入口预算，
    # 确保镜像和可选 Google 回退仍能在总站点预算内执行。
    onelou_endpoint_timeout_seconds = max(0.5, min(3.0, site_timeout_seconds * 0.3))
    registry = build_default_registry(
        user_agent=_user_agent(),
        nyaa_endpoint_timeout_seconds=nyaa_endpoint_timeout_seconds,
        btbtla_min_interval_seconds=_bounded_int(
            "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS", 5, 0, 60
        ),
        onelou_min_interval_seconds=_bounded_int(
            "INDEXER_1LOU_MIN_INTERVAL_SECONDS", 5, 0, 10
        ),
        onelou_endpoint_timeout_seconds=onelou_endpoint_timeout_seconds,
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
        site_timeout_seconds=site_timeout_seconds,
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
        service = _service
    if service is None:
        return

    # 只有资源真实关闭后才移除全局句柄。失败时保留同一个、已被关闭
    # 栅栏隔离的实例，允许后续停机或配置热更新再次调用 aclose。
    await service.aclose()
    with _lock:
        if _service is service:
            _service = None


atexit.register(_stop_standalone_runtime)
