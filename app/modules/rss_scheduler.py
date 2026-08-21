"""RSS 订阅周期调度器。"""
from __future__ import annotations

import logging
import threading
import time

from app import database as db
from app.logger import get_logger, log_throttled
from app.modules.rss import RSSEngine, rss_subscription_refresh_revision

logger = get_logger(__name__)
_MAX_CONCURRENT_REFRESHES = 4


class RSSScheduler:
    def __init__(self):
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running_ids: set[int] = set()
        self._lock = threading.Lock()
        self._cleanup_interval_seconds = 3600
        self._last_cleanup_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="rss-scheduler", daemon=True)
        self._thread.start()
        logger.info("RSS 调度器已启动")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if not thread or not thread.is_alive():
            self._thread = None

    def reload(self) -> None:
        self._wake_event.set()

    def run_due(self) -> int:
        self._run_cleanup_if_due()
        recovered = db.recover_stale_submitting_rss_entries(stale_minutes=15)
        if recovered:
            logger.warning(
                "已将 %s 条提交结果未知的 RSS 条目转为人工核对状态", recovered
            )
        count = 0
        for row in db.list_due_rss_subscriptions():
            sub_id = int(row["id"])
            with self._lock:
                if len(self._running_ids) >= _MAX_CONCURRENT_REFRESHES:
                    break
                if sub_id in self._running_ids:
                    continue
                self._running_ids.add(sub_id)
            try:
                threading.Thread(
                    target=self._execute,
                    args=(
                        sub_id,
                        str(row["action"] or "subscribe"),
                        rss_subscription_refresh_revision(row),
                    ),
                    name=f"rss-refresh-{sub_id}",
                    daemon=True,
                ).start()
            except Exception:
                with self._lock:
                    self._running_ids.discard(sub_id)
                raise
            count += 1
        return count

    def _run_cleanup_if_due(self) -> int:
        current = time.monotonic()
        if self._last_cleanup_at and current - self._last_cleanup_at < self._cleanup_interval_seconds:
            return 0
        deleted = db.purge_processed_rss_entries(retention_days=7)
        self._last_cleanup_at = current
        if deleted:
            logger.info(f"已清理 {deleted} 条超过 7 天的已处理 RSS 条目")
        return deleted

    def _execute(self, sub_id: int, action: str, expected_revision: str = "") -> None:
        try:
            engine = RSSEngine()
            result = (
                engine.auto_download(sub_id, expected_revision=expected_revision)
                if action == "download"
                else engine.refresh(sub_id, expected_revision=expected_revision)
            )
            if result.get("busy"):
                logger.debug("RSS 周期刷新跳过 sub#%s", sub_id)
            elif result.get("conflict"):
                logger.debug("RSS 周期刷新取消 sub#%s", sub_id)
            elif result.get("error"):
                logger.warning("RSS 周期任务失败 sub#%s", sub_id)
            elif int(result.get("outcome_unknown_count") or 0) > 0:
                logger.warning(
                    "RSS 周期下载结果待核对 sub#%s outcome_unknown=%s failed=%s",
                    sub_id,
                    int(result.get("outcome_unknown_count") or 0),
                    int(result.get("failed") or 0),
                )
            elif int(result.get("failed") or 0) > 0:
                logger.warning(
                    "RSS 周期下载部分失败 sub#%s failed=%s",
                    sub_id,
                    int(result.get("failed") or 0),
                )
            elif bool(result.get("partial")) or bool(
                (result.get("refresh") or {}).get("partial")
            ):
                logger.warning("RSS 周期刷新部分完成 sub#%s", sub_id)
        except Exception as exc:
            logger.warning("RSS 周期任务异常 sub#%s type=%s", sub_id, type(exc).__name__)
        finally:
            with self._lock:
                self._running_ids.discard(sub_id)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_due()
            except Exception as exc:
                log_throttled(
                    logger, logging.WARNING, f"rss-scheduler:{type(exc).__name__}",
                    "RSS 调度检查失败 type=%s", type(exc).__name__,
                )
            self._wake_event.wait(timeout=30)
            self._wake_event.clear()


_scheduler = RSSScheduler()


def get_rss_scheduler() -> RSSScheduler:
    return _scheduler
