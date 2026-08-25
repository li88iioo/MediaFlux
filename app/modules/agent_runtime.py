"""Media Agent 后台调度器的非阻塞运行时协调。"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.agent.feature_gate import is_agent_enabled
from app.logger import get_logger

logger = get_logger(__name__)

_RECONCILE_JOIN_SECONDS = 1.0
_RECONCILE_SLOW_LOG_SECONDS = 5.0
_worker_lock = threading.Lock()
_reconcile_lock = threading.Lock()
_reconcile_requested = threading.Event()
_shutdown_requested = threading.Event()
_worker: threading.Thread | None = None


def _schedulers() -> tuple[Any, ...]:
    from app.modules.agent_download_verification_scheduler import (
        get_download_library_verification_scheduler,
    )
    from app.modules.agent_jobs_scheduler import get_agent_jobs_scheduler
    from app.modules.agent_library_patrol_scheduler import (
        get_agent_library_patrol_scheduler,
    )

    return (
        get_download_library_verification_scheduler(),
        get_agent_library_patrol_scheduler(),
        get_agent_jobs_scheduler(),
    )


def _stop_scheduler(scheduler: Any, *, expected_enabled: bool) -> bool:
    """等待调度线程退出；配置状态变化时让外层重新协调。"""
    started = time.monotonic()
    slow_logged = False
    while not scheduler.stop(timeout=_RECONCILE_JOIN_SECONDS):
        if _shutdown_requested.is_set():
            return False
        if is_agent_enabled() != expected_enabled:
            return False
        if not slow_logged and time.monotonic() - started >= _RECONCILE_SLOW_LOG_SECONDS:
            logger.info(
                "Agent 调度器仍在等待当前任务结束 scheduler=%s",
                type(scheduler).__name__,
            )
            slow_logged = True
    return True


def reconcile_agent_runtime() -> None:
    """按最新总开关串行协调三个 Agent 调度器。"""
    with _reconcile_lock:
        while not _shutdown_requested.is_set():
            _reconcile_requested.clear()
            enabled = is_agent_enabled()
            schedulers = _schedulers()

            if enabled:
                # 快速关闭后立即重新开启时，先确保旧的 stopping 线程退出，
                # 再创建新线程，避免 start() 命中仍存活的旧线程而静默失效。
                for scheduler in schedulers:
                    if not _stop_scheduler(scheduler, expected_enabled=True):
                        break
                    if _shutdown_requested.is_set() or not is_agent_enabled():
                        break
                    scheduler.start()
            else:
                for scheduler in schedulers:
                    if not _stop_scheduler(scheduler, expected_enabled=False):
                        break

            if _shutdown_requested.is_set():
                return
            if (
                not _reconcile_requested.is_set()
                and is_agent_enabled() == enabled
            ):
                return


def _run_reconcile_worker() -> None:
    global _worker
    try:
        reconcile_agent_runtime()
    except Exception as exc:
        logger.warning("Agent 总开关后台热更新失败 type=%s", type(exc).__name__)
    finally:
        with _worker_lock:
            _worker = None
            rerun = _reconcile_requested.is_set()
        if rerun and not _shutdown_requested.is_set():
            request_agent_runtime_reconcile()


def resume_agent_runtime() -> None:
    """允许当前应用生命周期接受运行时协调请求。"""
    _shutdown_requested.clear()


def shutdown_agent_runtime(timeout: float = 2.0) -> bool:
    """阻止协调线程在应用关停阶段重新启动 scheduler。"""
    _shutdown_requested.set()
    _reconcile_requested.set()
    with _worker_lock:
        worker = _worker
    if worker is not None and worker.is_alive() and worker is not threading.current_thread():
        worker.join(timeout=max(0.0, float(timeout)))
    return worker is None or not worker.is_alive()


def request_agent_runtime_reconcile() -> bool:
    """请求后台协调 Agent 调度器；调用方无需等待线程停止。"""
    global _worker
    if _shutdown_requested.is_set():
        return False
    _reconcile_requested.set()
    with _worker_lock:
        if _shutdown_requested.is_set():
            return False
        if _worker is not None and _worker.is_alive():
            return False
        worker = threading.Thread(
            target=_run_reconcile_worker,
            name="agent-runtime-reconcile",
            daemon=True,
        )
        _worker = worker
        worker.start()
        return True
