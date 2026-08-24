"""媒体订阅周期巡检调度器。"""
from __future__ import annotations

import asyncio
import threading
import time

from app import database as db
from app.logger import get_logger
from app.modules.media_subscriptions import MediaSubscriptionError, get_media_subscription_service

logger = get_logger(__name__)
_MAX_CONCURRENT_CHECKS = 2
_RECOVERY_INTERVAL_SECONDS = 300.0


class MediaSubscriptionScheduler:
    """以有界并发执行到期订阅；数据库原子 claim 负责最终去重。"""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._workers: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()
        self._accepting = True
        self._last_recovery_at = 0.0

    def start(self) -> None:
        with self._lock:
            thread = self._thread
            if thread and thread.is_alive():
                return
            self._accepting = True
            self._stop_event.clear()
            self._wake_event.clear()
            self._last_recovery_at = 0.0
            self._thread = threading.Thread(
                target=self._loop,
                name="media-subscription-scheduler",
                daemon=True,
            )
            self._thread.start()
        logger.info("媒体订阅调度器已启动")

    def stop(self, timeout: float = 30.0) -> bool:
        """停止生产新检查，并等待已启动的检查在有界时间内收敛。"""
        with self._lock:
            self._accepting = False
        self._stop_event.set()
        self._wake_event.set()
        deadline = time.monotonic() + max(0.0, float(timeout))

        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        scheduler_alive = bool(thread and thread.is_alive())
        if not scheduler_alive:
            self._thread = None

        while True:
            with self._lock:
                workers = [worker for worker in self._workers.values() if worker.is_alive()]
            if not workers:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for worker in workers:
                if worker is threading.current_thread():
                    continue
                worker.join(timeout=min(remaining, 0.5))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

        with self._lock:
            alive = [subscription_id for subscription_id, worker in self._workers.items() if worker.is_alive()]
            self._workers = {
                subscription_id: worker
                for subscription_id, worker in self._workers.items()
                if worker.is_alive()
            }
        scheduler_alive = bool(self._thread and self._thread.is_alive())
        if scheduler_alive or alive:
            logger.warning(
                "媒体订阅调度器停止超时 scheduler_alive=%s subscriptions=%s",
                scheduler_alive,
                alive,
            )
            return False
        logger.info("媒体订阅调度器已停止")
        return True

    def reload(self) -> None:
        if not self._stop_event.is_set():
            self._wake_event.set()

    def run_due(self) -> int:
        if self._stop_event.is_set():
            return 0
        current = time.monotonic()
        if current - self._last_recovery_at >= _RECOVERY_INTERVAL_SECONDS:
            recovered = db.recover_stale_media_subscription_checks()
            if recovered:
                logger.warning("已恢复 %s 个中断的媒体订阅检查", recovered)
            self._last_recovery_at = current

        count = 0
        for row in db.list_due_media_subscriptions(limit=20):
            if self._stop_event.is_set():
                break
            subscription_id = int(row["id"])
            with self._lock:
                if not self._accepting or self._stop_event.is_set():
                    break
                self._workers = {
                    sid: worker for sid, worker in self._workers.items() if worker.is_alive()
                }
                if len(self._workers) >= _MAX_CONCURRENT_CHECKS:
                    break
                if subscription_id in self._workers:
                    continue
                worker = threading.Thread(
                    target=self._execute,
                    args=(subscription_id,),
                    name=f"media-subscription-{subscription_id}",
                    daemon=True,
                )
                self._workers[subscription_id] = worker
                try:
                    worker.start()
                except Exception:
                    self._workers.pop(subscription_id, None)
                    raise
            count += 1
        return count

    def _execute(self, subscription_id: int) -> None:
        try:
            asyncio.run(
                get_media_subscription_service().check_subscription(
                    subscription_id,
                    trigger="scheduler",
                    cancel_event=self._stop_event,
                )
            )
        except MediaSubscriptionError as exc:
            if exc.code not in {"busy", "cancelled"}:
                logger.warning(
                    "媒体订阅周期检查失败 subscription=%s code=%s message=%s",
                    subscription_id,
                    exc.code,
                    str(exc),
                )
        except Exception as exc:
            logger.exception(
                "媒体订阅周期检查异常 subscription=%s type=%s",
                subscription_id,
                type(exc).__name__,
            )
        finally:
            with self._lock:
                self._workers.pop(subscription_id, None)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_due()
            except Exception as exc:
                logger.warning("媒体订阅调度检查失败 type=%s", type(exc).__name__)
            self._wake_event.wait(timeout=30.0)
            self._wake_event.clear()


_scheduler = MediaSubscriptionScheduler()


def get_media_subscription_scheduler() -> MediaSubscriptionScheduler:
    return _scheduler
