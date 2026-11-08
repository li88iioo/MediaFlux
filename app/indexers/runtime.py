"""Indexer 运行时单例与配置装配。"""
from __future__ import annotations

import asyncio
import atexit
import inspect
import re
import threading
import time
import unicodedata
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, TypeVar

from app import config
from app.logger import get_logger

from .config import (
    INDEXER_SITE_LABELS,
    SENSITIVE_INDEXER_SITE_IDS,
    current_indexer_site_ids,
)
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
_STANDALONE_STARTUP_TIMEOUT_SECONDS = 5.0


def source_readiness(*, scope: str = "manual") -> dict[str, Any]:
    """只读本地注册与当前入口的站点权限；不进行站点连通性探测。"""
    scope = IndexerService._normalize_scope(scope)
    data: dict[str, Any] = {
        "probe_mode": "local", "scope": scope,
        "network_accessed": False, "filesystem_accessed": False,
        "indexer_enabled": None,
        "counts": {"registered": 0, "enabled": 0, "searchable": 0, "downloadable": 0, "attention": 0},
        "transport": {"mode": "direct", "outbound_proxy_applied": False},
        "sites": [], "configuration": None,
    }
    try:
        from .config import site_configuration
        data["configuration"] = site_configuration()
        data["indexer_enabled"] = config.get_bool("INDEXER_SEARCH_ENABLED")
        if not data["indexer_enabled"]:
            return {"ok": True, "status": "disabled", **data}
        service = get_indexer_service()
        registered = tuple(service.registry.ids())
        enabled = set(service.site_ids_for_scope(scope))
    except Exception as exc:
        logger.warning("索引器本地就绪状态读取失败 type=%s", type(exc).__name__)
        return {"ok": False, "status": "unavailable", **data, "error": "索引器就绪诊断当前不可用。"}

    counts = data["counts"]
    for raw_id in registered:
        site_id = (raw_id or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", site_id):
            counts["attention"] += 1
            continue
        site_enabled = site_id in enabled
        try:
            adapter = service.registry.get(site_id)
            kinds = sorted({kind.strip().lower() for kind in adapter.capabilities.download_kinds
                            if kind.strip().lower() in {"magnet", "torrent"}})
            pagination = adapter.capabilities.pagination_supported
            label = unicodedata.normalize("NFKC", (INDEXER_SITE_LABELS.get(site_id, adapter.site_name) or "")).strip()
            if not label or len(label) > 80 or any(unicodedata.category(char).startswith("C") for char in label):
                label = site_id
            searchable = site_enabled
            downloadable = site_enabled and bool(kinds)
            reason = "site_disabled" if not site_enabled else "ready" if kinds else "search_only"
        except Exception:
            kinds, pagination, label = [], False, site_id
            searchable = downloadable = False
            reason = "registry_unavailable"
            counts["attention"] += 1
        counts["registered"] += 1
        counts["enabled"] += int(site_enabled)
        counts["searchable"] += int(searchable)
        counts["downloadable"] += int(downloadable)
        data["sites"].append({
            "site_id": site_id, "site_name": label, "enabled": site_enabled,
            "search_available": searchable, "download_available": downloadable,
            "download_kinds": kinds, "pagination_supported": pagination,
            "sensitive": site_id in SENSITIVE_INDEXER_SITE_IDS, "reason": reason,
        })
    status = "no_enabled_sites" if not counts["enabled"] else "attention" if counts["attention"] else "ready"
    return {"ok": True, "status": status, **data}


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
    holder: dict[str, Any],
    cancelled: threading.Event,
) -> None:
    loop: asyncio.AbstractEventLoop | None = None
    try:
        try:
            loop = asyncio.new_event_loop()
            holder["loop"] = loop
            asyncio.set_event_loop(loop)
            if cancelled.is_set():
                return
            # 创建了 loop 不等于已经运行；由首轮循环回调发布真正的就绪状态。
            loop.call_soon(ready.set)
            loop.run_forever()
        finally:
            if loop is not None:
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                finally:
                    loop.close()
            asyncio.set_event_loop(None)
    except BaseException as exc:
        holder["error"] = exc
    finally:
        # 创建/初始化失败也唤醒调用者，不能伪装成等满启动预算的超时。
        ready.set()


def _ensure_standalone_runtime() -> asyncio.AbstractEventLoop:
    """为无 ASGI 生命周期的同步/脚本调用提供稳定的长期事件循环。"""
    global _runtime_loop, _runtime_stopping, _standalone_loop, _standalone_thread
    with _standalone_lock:
        loop = _standalone_loop
        thread = _standalone_thread
        if thread is not None and thread.is_alive():
            if loop is not None and loop.is_running():
                return loop
            raise RuntimeError("上一次索引器独立事件循环启动尚未退出，请稍后重试")
        ready = threading.Event()
        cancelled = threading.Event()
        holder: dict[str, Any] = {}
        thread = threading.Thread(
            target=_standalone_loop_main,
            args=(ready, holder, cancelled),
            name="indexer-standalone-loop",
            daemon=True,
        )
        # 即使 loop 创建暂时阻塞，失败后仍须有句柄阻止重试生成第二个线程。
        _standalone_thread = thread
        try:
            thread.start()
            if not ready.wait(timeout=_STANDALONE_STARTUP_TIMEOUT_SECONDS):
                raise RuntimeError("索引器独立事件循环启动超时")
            loop = holder.get("loop")
            if "error" in holder:
                raise RuntimeError("索引器独立事件循环启动失败") from holder["error"]
            if loop is None or not loop.is_running():
                raise RuntimeError("索引器独立事件循环启动失败")
            with _runtime_loop_lock:
                if _runtime_loop is not None and _runtime_loop is not loop:
                    raise RuntimeError("索引器运行时已绑定其他事件循环")
                _standalone_loop = loop
                _runtime_loop = loop
                _runtime_stopping = False
            return loop
        except BaseException:
            # 先撤销启动，再处理已经创建的 loop；稍后才创建的 loop 由线程自行关闭。
            cancelled.set()
            loop = holder.get("loop")
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    pass
            if thread.ident is not None:
                thread.join(timeout=_STANDALONE_STARTUP_TIMEOUT_SECONDS)
            if not thread.is_alive():
                _standalone_thread = None
            raise


def _stop_standalone_runtime(timeout_seconds: float = 10.0) -> bool:
    """在其所属循环关闭独立 Indexer 服务，再结束循环线程。"""
    global _runtime_loop, _runtime_stopping, _standalone_loop, _standalone_thread
    with _standalone_lock:
        loop = _standalone_loop
        thread = _standalone_thread
        if thread is None:
            return True
        if loop is None:
            # 启动失败但创建线程尚未退出：取消已由启动方发布，只能有界等待，
            # 不可宣称关闭成功或丢弃其句柄后允许另一次启动。
            if thread is not threading.current_thread():
                thread.join(timeout=max(0.1, float(timeout_seconds)))
            if thread.is_alive():
                return False
            _standalone_thread = None
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
    cancelled: Callable[[], bool] | None = None,
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
    if cancelled is not None and cancelled():
        _close_awaitable(awaitable)
        raise InterruptedError("索引器调用已取消")

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
        try:
            owner_loop = _ensure_standalone_runtime()
        except BaseException:
            _close_awaitable(awaitable)
            raise

    if owner_loop is not None:
        if current_loop is owner_loop:
            _close_awaitable(awaitable)
            raise RuntimeError("同步索引器桥接不能阻塞其所属事件循环")
        future = _submit_to_runtime_loop(awaitable, owner_loop)
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        try:
            while True:
                # 已完成的结果仍可由批量调用保留；取消仅中止当前等待者。
                if not future.done() and cancelled is not None and cancelled():
                    raise InterruptedError("索引器调用已取消")
                remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
                wait = min(0.1, remaining) if remaining is not None else 0.1
                try:
                    return future.result(timeout=wait if cancelled is not None else remaining)
                except FutureTimeoutError:
                    if future.done():
                        return future.result()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("索引器调用超时") from None
        except BaseException:
            future.cancel()
            raise

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
        try:
            owner_loop = _ensure_standalone_runtime()
        except BaseException:
            _close_awaitable(awaitable)
            raise
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


def _bounded_int(key: str, default: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(config.get_int(key, default), maximum))


def _user_agent() -> str:
    raw = (config.get("INDEXER_USER_AGENT", "MediaFlux/1.0") or "")
    value = raw.strip()
    if not value or len(value) > 256 or "\r" in raw or "\n" in raw:
        return "MediaFlux/1.0"
    return value


def build_indexer_service() -> IndexerService:
    site_timeout_seconds = _bounded_int("INDEXER_SITE_TIMEOUT_SECONDS", 10, 2, 60)
    cache_ttl_seconds = _bounded_int("INDEXER_CACHE_TTL_SECONDS", 120, 30, 600)
    max_results_per_site = _bounded_int("INDEXER_MAX_RESULTS_PER_SITE", 40, 1, 100)
    # Nyaa 有主站和镜像两个端点；单端点预算必须显著短于总站点预算，
    # 否则主站卡满后服务层会取消整个适配器，镜像永远没有执行机会。
    nyaa_endpoint_timeout_seconds = max(0.5, min(4.0, site_timeout_seconds * 0.4))
    registry = build_default_registry(
        user_agent=_user_agent(),
        nyaa_endpoint_timeout_seconds=nyaa_endpoint_timeout_seconds,
        mikan_endpoint_timeout_seconds=site_timeout_seconds,
        btbtla_min_interval_seconds=_bounded_int(
            "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS", 5, 0, 60
        ),
        btbtla_page_size=max_results_per_site,
        btbtla_cache_ttl_seconds=cache_ttl_seconds,
    )
    configured = current_indexer_site_ids()
    sukebei_authorized = "sukebei" in configured
    enabled = [site_id for site_id in configured if site_id in registry.ids()]
    if sukebei_authorized and "sukebei" in registry.ids():
        enabled.append("sukebei")
    return IndexerService(
        registry=registry,
        result_store=IndexerResultStore(
            ttl_seconds=_bounded_int("INDEXER_RESULT_TTL_SECONDS", 600, 60, 3600),
            max_entries=10_000,
        ),
        site_timeout_seconds=site_timeout_seconds,
        total_timeout_seconds=_bounded_int("INDEXER_TOTAL_TIMEOUT_SECONDS", 15, 3, 120),
        max_results_per_site=max_results_per_site,
        max_concurrency=_bounded_int("INDEXER_MAX_CONCURRENCY", 5, 1, 10),
        cache_ttl_seconds=cache_ttl_seconds,
        enabled_site_ids=enabled,
        sukebei_authorized=sukebei_authorized,
    )



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


def refresh_indexer_configuration(*, timeout: float = 5.0) -> dict[str, bool]:
    """配置保存后释放Web/TG旧服务；失败保留旧实例，调用方明确提示重启。"""
    status = {"web": True, "telegram": True}
    try:
        run_indexer_awaitable_sync(shutdown_indexer_service(), timeout_seconds=timeout)
    except Exception as exc:
        status["web"] = False
        logger.warning("Web索引器配置热更新失败 type=%s", type(exc).__name__)
    try:
        from app.modules.telegram_resource_search import shutdown_telegram_indexer_worker
        status["telegram"] = shutdown_telegram_indexer_worker(timeout=timeout)
    except Exception as exc:
        status["telegram"] = False
        logger.warning("Telegram索引器配置热更新失败 type=%s", type(exc).__name__)
    return status


atexit.register(_stop_standalone_runtime)
