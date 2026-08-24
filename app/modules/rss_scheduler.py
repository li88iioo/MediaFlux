"""RSS 订阅周期调度器。"""
from __future__ import annotations

import logging
import threading
import time

from app import database as db
from app.logger import get_logger, log_throttled
from app.modules.rss import RSSEngine, rss_subscription_refresh_revision
from app.notifier import NotificationEvent, send_event

logger = get_logger(__name__)
_MAX_CONCURRENT_REFRESHES = 4


class RSSScheduler:
    def __init__(self):
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running_ids: set[int] = set()
        self._workers: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()
        self._cleanup_interval_seconds = 3600
        self._last_cleanup_at = 0.0
        self._alert_signatures: dict[int, tuple[object, ...]] = {}

    def _notify_issue(self, sub_id: int, code: str, fields: list[tuple[str, object]]) -> None:
        signature = (code, *(str(value) for _label, value in fields))
        with self._lock:
            if self._alert_signatures.get(sub_id) == signature:
                return
        subscription = db.get_rss_subscription(sub_id)
        name = str(subscription["name"] or f"订阅 #{sub_id}") if subscription else f"订阅 #{sub_id}"
        try:
            delivered = send_event(
                NotificationEvent(
                    "RSS 周期任务需要处理",
                    fields=(("订阅", name), *tuple(fields)),
                )
            )
        except Exception as exc:
            logger.warning(
                "RSS 周期告警发送异常 sub#%s type=%s",
                sub_id,
                type(exc).__name__,
            )
            return
        if delivered:
            with self._lock:
                self._alert_signatures[sub_id] = signature

    def _clear_issue(self, sub_id: int) -> None:
        with self._lock:
            self._alert_signatures.pop(sub_id, None)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="rss-scheduler", daemon=True)
        self._thread.start()
        logger.info("RSS 调度器已启动")

    def stop(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if not thread or not thread.is_alive():
            self._thread = None
        with self._lock:
            workers = tuple(self._workers.values())
        for worker in workers:
            if worker is threading.current_thread() or not worker.is_alive():
                continue
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            alive = any(worker.is_alive() for worker in self._workers.values())
        return not alive and (self._thread is None or not self._thread.is_alive())

    def reload(self) -> None:
        self._wake_event.set()

    def run_due(self) -> int:
        if self._stop_event.is_set():
            return 0
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
                worker = threading.Thread(
                    target=self._execute,
                    args=(
                        sub_id,
                        str(row["action"] or "subscribe"),
                        rss_subscription_refresh_revision(row),
                    ),
                    name=f"rss-refresh-{sub_id}",
                    daemon=True,
                )
                with self._lock:
                    self._workers[sub_id] = worker
                worker.start()
            except Exception:
                with self._lock:
                    self._running_ids.discard(sub_id)
                    self._workers.pop(sub_id, None)
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
                self._notify_issue(
                    sub_id,
                    str(result.get("error_code") or "error"),
                    [("状态", "刷新失败，请检查订阅源")],
                )
            elif int(result.get("outcome_unknown_count") or 0) > 0:
                logger.warning(
                    "RSS 周期下载结果待核对 sub#%s outcome_unknown=%s failed=%s",
                    sub_id,
                    int(result.get("outcome_unknown_count") or 0),
                    int(result.get("failed") or 0),
                )
                self._notify_issue(
                    sub_id,
                    "outcome_unknown",
                    [
                        ("状态", "提交结果待人工核对，请勿重复提交"),
                        ("待核对", int(result.get("outcome_unknown_count") or 0)),
                        ("失败", int(result.get("failed") or 0)),
                    ],
                )
            elif int(result.get("failed") or 0) > 0:
                logger.warning(
                    "RSS 周期下载部分失败 sub#%s failed=%s",
                    sub_id,
                    int(result.get("failed") or 0),
                )
                self._notify_issue(
                    sub_id,
                    "partial_failure",
                    [("状态", "周期下载部分失败"), ("失败", int(result.get("failed") or 0))],
                )
            elif bool(result.get("partial")) or bool(
                (result.get("refresh") or {}).get("partial")
            ):
                logger.warning("RSS 周期刷新部分完成 sub#%s", sub_id)
                self._notify_issue(
                    sub_id,
                    "partial_refresh",
                    [("状态", "部分订阅源刷新失败")],
                )
            else:
                self._clear_issue(sub_id)
        except Exception as exc:
            logger.warning("RSS 周期任务异常 sub#%s type=%s", sub_id, type(exc).__name__)
            self._notify_issue(
                sub_id,
                f"exception:{type(exc).__name__}",
                [("状态", "周期任务异常，请检查运行日志")],
            )
        finally:
            with self._lock:
                self._running_ids.discard(sub_id)
                current = self._workers.get(sub_id)
                if current is threading.current_thread():
                    self._workers.pop(sub_id, None)

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
