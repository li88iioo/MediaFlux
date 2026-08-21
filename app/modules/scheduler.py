"""后台任务调度器：STRM 定时扫描。

设计约束：
- 使用标准 5 段 cron 表达式（分 时 日 月 周）
- 进程级单例 + 非阻塞锁，防止 cron/Web/TG 三路重复执行
- 任务运行记录写入 SQLite，服务重启后可查看历史结果
- 配置未完整或未显式启用时不自动执行
"""
from __future__ import annotations

import html
import json
import threading
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Optional

from croniter import croniter

from app import database as db
from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.config import get, get_bool, get_int
from app.logger import get_logger
from app.modules.strm import (
    DEFAULT_METADATA_EXTS, DEFAULT_VIDEO_EXTS, STRM_OPERATION_LOCK, STRM_SUBDIR,
    clean_empty_strm_dirs, clean_retired_strm_sources, configured_strm_source_plans,
    finalize_changed_paths, safe_path_component, sync_strm, sync_strm_incremental,
)
from app.modules.media_refresh import plan_refresh_targets
from app.modules.strm_notifications import append_change, build_strm_detail_messages, relative_change
from app.notifier import NotificationEvent, send as send_text, send_event

# 保留模块级 ``send`` 名称，兼容既有测试/补丁；实际发送仍走结构化 send_event。
send = send_event

logger = get_logger(__name__)

TASK_NAME = "strm_sync"


def _source_local_dir(strm_root: str, source: dict[str, str]) -> str:
    """返回与 STRM 目标生成规则一致的来源本地根目录。"""
    target = Path(strm_root).expanduser() / STRM_SUBDIR
    rel_prefix = str(source.get("rel_prefix") or "").strip()
    if rel_prefix:
        target /= safe_path_component(rel_prefix)
    return str(target)


def _request_ids(options: dict[str, object]) -> list[int]:
    return [
        int(item) for item in options.get("download_request_ids", [])
        if str(item).isdigit()
    ]


def _update_strm_requests(options: dict[str, object], **fields) -> None:
    for request_id in _request_ids(options):
        db.update_download_request(request_id, **fields)


def _merge_organize_changes(*groups) -> list[dict[str, object]]:
    """排队合并时同 source/kind/file 以后到的最终快照为准。"""
    merged: dict[tuple[str, str, str], dict[str, object]] = {}
    for group in groups:
        for raw in group or []:
            if not isinstance(raw, dict):
                continue
            source_id = str(raw.get("source_id") or "").strip()
            kind = str(raw.get("kind") or "video").strip().lower()
            file_id = str(raw.get("file_id") or "").strip()
            if not source_id or not file_id:
                continue
            merged[(source_id, kind, file_id)] = dict(raw)
    return list(merged.values())


class _ChangeTargetLeaseHeartbeat:
    """后台续租本轮领取的 STRM 变化目标，避免长任务租约过期后被重复消费。"""

    def __init__(self, claimed: list[dict], lease_seconds: int):
        self._claimed = [dict(item) for item in claimed]
        self._lease_seconds = max(30, int(lease_seconds or 30))
        self._owner = str(self._claimed[0].get("lease_owner") or "") if self._claimed else ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._claimed or not self._owner:
            return
        self._thread = threading.Thread(
            target=self._run, name="strm-change-lease-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        interval = max(5.0, float(self._lease_seconds) / 3.0)
        while not self._stop.wait(interval):
            try:
                renewed = db.renew_strm_change_target_leases(
                    self._claimed, owner=self._owner, lease_seconds=self._lease_seconds
                )
                if renewed != len(self._claimed):
                    logger.warning(
                        "STRM 变化目标租约部分失效 renewed=%s claimed=%s",
                        renewed, len(self._claimed),
                    )
                    return
            except Exception:
                logger.exception("续租 STRM 变化目标失败")


class STRMScheduler:
    """STRM 定时扫描调度器。"""

    def __init__(self):
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._run_released_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._run_lock = STRM_OPERATION_LOCK
        self._state_lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self._running = False
        self._current_trigger = ""
        self._next_run: Optional[datetime] = None
        self._loaded_cron = ""
        self._run_options: dict[str, object] = {}
        self._pending_organize_options: dict[str, object] | None = None
        self._pending_thread: Optional[threading.Thread] = None
        self._progress = {
            "stage": "idle", "completed": 0, "total": 1, "percent": 0, "detail": "",
        }
        self._source_runtime: list[dict] = []

    def start(self) -> None:
        """启动调度检查线程。重复调用安全。"""
        with self._admission_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name="strm-scheduler", daemon=True
            )
            self._thread.start()
        self._recover_change_queue()
        self._recover_notification_outbox()
        from app.modules.strm_metadata_worker import get_strm_metadata_worker

        get_strm_metadata_worker().start()
        logger.info("STRM 调度器已启动")

    @staticmethod
    def _recover_notification_outbox() -> None:
        """恢复上次进程中断遗留的整理通知，避免结果通知永久丢失。"""
        try:
            from app.modules.organize_notification_outbox import (
                recover_organize_notifications,
            )

            recover_organize_notifications()
        except Exception:
            logger.exception("恢复整理通知投递队列失败")

    def _recover_change_queue(self) -> None:
        """启动时恢复上次进程中断遗留的变化目标，避免变化永久卡在队列里。"""
        try:
            recovered = db.recover_stale_strm_change_targets()
            pending = db.count_pending_strm_change_targets()
        except Exception:
            logger.exception("恢复 STRM 变化目标队列失败")
            return
        if recovered:
            logger.warning("已恢复中断的 STRM 变化目标 count=%s", recovered)
        if not pending:
            return
        logger.info("检测到待同步 STRM 变化目标 count=%s，已排队恢复执行", pending)
        try:
            self.trigger("organize")
        except Exception:
            logger.exception("恢复 STRM 变化目标触发失败")

    def stop(self, timeout: float = 30.0) -> None:
        """请求调度线程退出并等待收尾；重复调用安全。"""
        with self._admission_lock:
            self._stop_event.set()
            self._wake_event.set()
            self._run_released_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if not thread or not thread.is_alive():
            self._thread = None
        pending_thread = self._pending_thread
        if (
            pending_thread
            and pending_thread.is_alive()
            and pending_thread is not threading.current_thread()
        ):
            pending_thread.join(timeout=timeout)
        if not pending_thread or not pending_thread.is_alive():
            self._pending_thread = None
        worker = self._worker
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=timeout)
        if not worker or not worker.is_alive():
            self._worker = None
        else:
            logger.warning("STRM 工作线程未能在关闭超时内结束 trigger=%s", self._current_trigger)
        from app.modules.strm_metadata_worker import get_strm_metadata_worker

        get_strm_metadata_worker().stop(timeout=timeout)

    def reload(self) -> None:
        """配置保存后立即重算下次运行时间。"""
        with self._state_lock:
            self._next_run = None
            self._loaded_cron = ""
        self._wake_event.set()

    def _start_locked_worker(
        self, trigger_type: str, options: dict[str, object]
    ) -> dict:
        with self._state_lock:
            self._running = True
            self._current_trigger = trigger_type
            self._run_options = dict(options)
        try:
            _update_strm_requests(
                options, strm_status="running", strm_error="", strm_finished_at=None
            )
            worker = threading.Thread(
                target=self._execute_locked,
                args=(trigger_type,),
                name=f"strm-sync-{trigger_type}",
                daemon=False,
            )
            with self._state_lock:
                self._worker = worker
            worker.start()
        except Exception:
            with self._state_lock:
                self._worker = None
                self._running = False
                self._current_trigger = ""
                self._run_options = {}
            self._run_lock.release()
            self._run_released_event.set()
            _update_strm_requests(
                options, strm_status="failed", strm_error="STRM 同步任务启动失败",
                strm_finished_at=db.now(),
            )
            logger.exception("启动 STRM 同步线程失败 trigger=%s", trigger_type)
            return {"ok": False, "error": "STRM 同步任务启动失败"}
        return {"ok": True, "message": "STRM 同步任务已启动"}

    def _wait_and_run_pending_organize(self) -> None:
        while not self._stop_event.is_set():
            if not self._run_lock.acquire(blocking=False):
                # 通常由本调度器释放锁时直接唤醒；保留低频兜底，兼容测试、
                # 运维工具或其他进程内调用方直接操作共享锁的场景。
                self._run_released_event.wait(timeout=0.5)
                self._run_released_event.clear()
                continue
            with self._admission_lock:
                if self._stop_event.is_set():
                    self._run_lock.release()
                    break
                with self._state_lock:
                    options = self._pending_organize_options
                    self._pending_organize_options = None
                    self._pending_thread = None
                if options is None:
                    self._run_lock.release()
                    return
                result = self._start_locked_worker("organize", options)
            logger.info("执行排队的整理联动 STRM: %s", result)
            return
        with self._state_lock:
            cancelled_options = dict(self._pending_organize_options or {})
            cancelled = bool(cancelled_options)
            self._pending_organize_options = None
            self._pending_thread = None
        if cancelled:
            _update_strm_requests(
                cancelled_options, strm_status="stopped",
                strm_error="服务停止，排队中的 STRM 联动已取消",
                strm_finished_at=db.now(),
            )
            logger.warning("应用停止，已取消尚未执行的整理联动 STRM")

    def _queue_organize_trigger(self, options: dict[str, object]) -> dict:
        with self._state_lock:
            pending = dict(self._pending_organize_options or {})
            for key, value in options.items():
                if value is None:
                    continue
                if key in {"download_request_ids", "chat_ids"}:
                    pending[key] = list(dict.fromkeys([
                        *list(pending.get(key) or []), *list(value or []),
                    ]))
                elif key == "organize_changes":
                    pending[key] = _merge_organize_changes(
                        pending.get(key), value
                    )
                elif key == "force_full":
                    pending[key] = bool(pending.get(key)) or bool(value)
                elif key == "sync_mode":
                    current_mode = str(pending.get(key) or "auto")
                    incoming_mode = str(value or "auto")
                    pending[key] = (
                        "full" if "full" in {current_mode, incoming_mode}
                        else incoming_mode
                    )
                else:
                    pending[key] = bool(value)
            self._pending_organize_options = pending
            _update_strm_requests(
                pending, strm_status="queued", strm_error="", strm_finished_at=None
            )
            waiter = self._pending_thread
            if waiter is None or not waiter.is_alive():
                waiter = threading.Thread(
                    target=self._wait_and_run_pending_organize,
                    name="strm-sync-organize-waiter",
                    daemon=True,
                )
                self._pending_thread = waiter
                try:
                    waiter.start()
                except Exception:
                    self._pending_thread = None
                    self._pending_organize_options = None
                    _update_strm_requests(
                        pending, strm_status="failed", strm_error="STRM 整理联动排队失败",
                        strm_finished_at=db.now(),
                    )
                    logger.exception("启动整理联动 STRM 等待线程失败")
                    return {"ok": False, "error": "STRM 整理联动排队失败"}
        return {
            "ok": True,
            "queued": True,
            "message": "STRM 整理联动已排队，将在当前操作结束后执行",
        }

    def trigger(self, trigger_type: str = "manual", *,
                notify_override: bool | None = None,
                detail_notify_override: bool | None = None,
                emby_refresh_override: bool | None = None,
                on_progress=None,
                download_request_ids: list[int] | None = None,
                organize_changes: list[dict[str, object]] | None = None,
                force_full: bool = False,
                sync_mode: str = "auto",
                chat_id: str = "") -> dict:
        """异步触发一次任务；整理联动在忙时合并排队，其余触发保持拒绝。"""
        with self._admission_lock:
            if self._stop_event.is_set():
                return {"ok": False, "error": "STRM 调度器正在停止"}
            normalized_mode = str(sync_mode or "auto").strip().lower()
            if normalized_mode not in {"auto", "fast", "full"}:
                return {"ok": False, "error": "STRM 同步模式无效"}
            if force_full:
                normalized_mode = "full"
            options = {
                "notify_override": notify_override,
                "detail_notify_override": detail_notify_override,
                "emby_refresh_override": emby_refresh_override,
                "on_progress": on_progress,
                "download_request_ids": list(download_request_ids or []),
                "organize_changes": _merge_organize_changes(organize_changes),
                "force_full": bool(force_full),
                "sync_mode": normalized_mode,
                "chat_ids": [str(chat_id).strip()] if str(chat_id or "").strip() else [],
            }
            # 先持久化再触发：进程在排队或执行中崩溃时，变化目标仍可恢复。
            if not self._persist_change_targets(options.get("organize_changes")):
                return {"ok": False, "error": "STRM 变化目标持久化失败，已取消同步"}
            if not self._run_lock.acquire(blocking=False):
                if trigger_type == "organize":
                    options.pop("on_progress", None)
                    return self._queue_organize_trigger(options)
                return {"ok": False, "error": "STRM 同步任务正在运行"}
            return self._start_locked_worker(trigger_type, options)

    def run_blocking(
        self,
        trigger_type: str = "manual",
        *,
        on_progress=None,
        organize_changes: list[dict[str, object]] | None = None,
        force_full: bool = False,
        sync_mode: str = "auto",
    ) -> dict:
        """同步执行一次，主要用于测试/CLI。"""
        with self._admission_lock:
            if self._stop_event.is_set():
                return {"ok": False, "error": "STRM 调度器正在停止"}
            normalized_changes = _merge_organize_changes(organize_changes)
            if not self._persist_change_targets(normalized_changes):
                return {"ok": False, "error": "STRM 变化目标持久化失败，已取消同步"}
            if not self._run_lock.acquire(blocking=False):
                return {"ok": False, "error": "STRM 同步任务正在运行"}
            normalized_mode = str(sync_mode or "auto").strip().lower()
            if normalized_mode not in {"auto", "fast", "full"}:
                self._run_lock.release()
                return {"ok": False, "error": "STRM 同步模式无效"}
            if force_full:
                normalized_mode = "full"
            with self._state_lock:
                self._running = True
                self._current_trigger = trigger_type
                self._run_options = {
                    "on_progress": on_progress,
                    "organize_changes": normalized_changes,
                    "force_full": bool(force_full),
                    "sync_mode": normalized_mode,
                }
                # CLI/TG 同步执行也纳入 stop() 的工作线程追踪，避免应用
                # 关闭时只等待 scheduler 自建线程而漏掉调用方线程。
                self._worker = threading.current_thread()
        return self._execute_locked(trigger_type)

    def _persist_change_targets(self, organize_changes: object) -> bool:
        """把整理变化写入唯一权威队列；失败时必须阻断同步，避免事件只存在内存。"""
        if not organize_changes:
            return True
        try:
            db.enqueue_strm_change_targets(organize_changes)
            return True
        except Exception:
            logger.exception("登记 STRM 变化目标队列失败")
            return False

    def _claim_change_targets(self, trigger_type: str, sync_mode: str = "auto") -> list[dict]:
        """整理联动运行前领取到期目标；领取失败不得降级为空队列继续执行。"""
        if trigger_type != "organize" and sync_mode != "fast":
            return []
        lease_seconds = max(30, get_int("STRM_CHANGE_LEASE_SECONDS", 900))
        return db.claim_strm_change_targets(
            owner=f"strm-sync-{threading.get_ident()}",
            lease_seconds=lease_seconds,
        )

    def _settle_change_targets(
        self, claimed: list[dict], outcome: str, *, error: str = "",
    ) -> None:
        """按运行结果收敛变化目标；owner/代次栅栏阻止迟到 worker 覆盖新状态。"""
        rows = [dict(item) for item in claimed if item.get("id")]
        if not rows:
            return
        try:
            if outcome == "stopped":
                db.release_strm_change_targets(
                    rows, reason="服务停止，STRM 变化目标已退回队列"
                )
                return
            if outcome == "failed":
                for item in rows:
                    state = db.fail_strm_change_target(
                        int(item["id"]),
                        expected_owner=str(item.get("lease_owner") or ""),
                        expected_lease_generation=int(item.get("lease_generation") or 0),
                        error=error,
                    )
                    if state == "stale":
                        logger.warning("忽略迟到的 STRM 失败结算 target=%s", item["id"])
                return
            requeued = 0
            for item in rows:
                state = db.complete_strm_change_target(
                    int(item["id"]),
                    expected_owner=str(item.get("lease_owner") or ""),
                    expected_lease_generation=int(item.get("lease_generation") or 0),
                )
                if state == "queued":
                    requeued += 1
                elif state == "stale":
                    logger.warning("忽略迟到的 STRM 完成结算 target=%s", item["id"])
        except Exception:
            logger.exception("更新 STRM 变化目标状态失败")
            return
        due_count = 0
        try:
            due_count = db.count_due_strm_change_targets()
        except Exception:
            logger.exception("读取待续跑 STRM 变化目标失败")
        if due_count and not self._stop_event.is_set():
            logger.info(
                "仍有可领取的 STRM 变化目标，已安排下一轮 count=%s dirty_requeued=%s",
                due_count, requeued,
            )
            try:
                self.trigger("organize")
            except Exception:
                logger.exception("重新排队 STRM 变化目标触发失败")

    def status(self) -> dict:
        """返回调度器状态和最近运行记录。"""
        enabled = get_bool("STRM_SCHEDULE_ENABLED", False)
        cron_expr = get("STRM_SCHEDULE_CRON", "0 4 * * *").strip()
        error = self.validate_config(auto_only=False)
        next_run = self._calculate_next(cron_expr) if enabled and not error else None
        last = db.get_last_task_run(TASK_NAME)
        with self._state_lock:
            running = self._running
            current_trigger = self._current_trigger
            progress = dict(self._progress)
            source_runtime = [dict(row) for row in self._source_runtime]
            pending_options = dict(self._pending_organize_options or {})
            pending_organize = bool(pending_options)
            pending_change_count = len(
                _merge_organize_changes(pending_options.get("organize_changes"))
            )
            pending_request_count = len(
                list(pending_options.get("download_request_ids") or [])
            )
            pending_chat_count = len(list(pending_options.get("chat_ids") or []))
        try:
            from app.modules.strm_metadata_worker import get_strm_metadata_worker

            metadata_queue = get_strm_metadata_worker().status()
        except Exception:
            logger.exception("读取 STRM 元数据队列状态失败")
            metadata_queue = db.count_strm_metadata_jobs()
        return {
            "enabled": enabled,
            "cron": cron_expr,
            "sources": self._source_dirs(),
            "metadata_enabled": get_bool("STRM_METADATA_ENABLED", False),
            "cron_valid": self.validate_cron(cron_expr),
            "config_error": error,
            "running": running,
            "current_trigger": current_trigger,
            "pending_organize": pending_organize,
            "pending_organize_changes": pending_change_count,
            "pending_organize_requests": pending_request_count,
            "pending_organize_chats": pending_chat_count,
            "progress": progress,
            "source_runtime": source_runtime,
            "metadata_queue": metadata_queue,
            "next_run": next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else "",
            "last_run": self._row_to_dict(last),
        }

    def _set_progress(self, stage: str, completed: int, total: int, detail: str) -> None:
        bounded_total = max(1, int(total or 0))
        bounded_completed = max(0, min(int(completed or 0), bounded_total))
        payload = {
            "stage": str(stage), "completed": bounded_completed, "total": bounded_total,
            "percent": int(bounded_completed * 100 / bounded_total), "detail": str(detail or ""),
        }
        with self._state_lock:
            self._progress = payload
            callback = self._run_options.get("on_progress")
        if callable(callback):
            try:
                callback(stage, bounded_completed, bounded_total, detail)
            except Exception as exc:
                logger.warning(f"STRM 外部进度回调失败 stage={stage}: {exc}")

    @staticmethod
    def validate_cron(expr: str) -> bool:
        """仅接受标准 5 段 cron。"""
        if len((expr or "").split()) != 5:
            return False
        try:
            croniter(expr, datetime.now()).get_next(datetime)
            return True
        except (ValueError, KeyError):
            return False

    @staticmethod
    def validate_config(auto_only: bool = False) -> str:
        """验证运行所需配置，返回错误文字或空字符串。"""
        if auto_only and not get_bool("STRM_SCHEDULE_ENABLED", False):
            return "定时任务未启用"
        sources = STRMScheduler._source_dirs()
        root = get("STRM_ROOT", "").strip()
        base_url = get("GY_STRM_BASE_URL", "").strip()
        retirements = db.list_strm_retired_sources()
        if not sources and not retirements:
            return "未配置光鸭 STRM 源目录"
        if sources and not root:
            return "未配置 STRM 本地根目录"
        if sources and not base_url:
            return "未配置 STRM 播放服务地址"
        cron_expr = get("STRM_SCHEDULE_CRON", "0 4 * * *").strip()
        if not STRMScheduler.validate_cron(cron_expr):
            return "cron 表达式无效，需使用 5 段格式：分 时 日 月 周"
        return ""

    @staticmethod
    def _empty_stats() -> dict:
        return {
            "total": 0, "generated": 0, "created": 0, "updated": 0,
            "skipped": 0, "failed": 0,
            "metadata_total": 0, "metadata_generated": 0,
            "metadata_queued": 0, "metadata_queue_failed": 0,
            "metadata_queue_cancelled": 0,
            "metadata_skipped": 0, "metadata_failed": 0,
            "metadata_cleaned": 0,
            "cleaned": 0, "clean_skipped": False,
            "empty_dirs_cleaned": 0, "directories": 0, "scan_entries": 0,
            "scanned_files": 0,
            "directory_requests": 0, "scan_pages": 0, "read_retries": 0,
            "rate_limit_retries": 0, "read_failures": 0,
            "request_p50_ms": 0.0, "request_p95_ms": 0.0,
            "request_p99_ms": 0.0,
            "scan_workers_configured": 0, "scan_workers_peak": 0,
            "scan_queue_peak": 0, "verify_workers_configured": 0,
            "verified_candidates": 0, "verify_prefiltered": 0,
            "scan_elapsed_seconds": 0.0, "generate_elapsed_seconds": 0.0,
            "metadata_elapsed_seconds": 0.0, "cleanup_elapsed_seconds": 0.0,
            "refresh_elapsed_seconds": 0.0,
            "failure_resolve_batches": 0,
            "failure_resolve_elapsed_seconds": 0.0,
            "failure_ledger_failed": 0,
            "error_samples": [], "changes": [], "omitted_count": 0,
            "changed_strm_paths": [], "changed_dirs": [], "changed_paths_omitted": 0,
            "retired_sources": 0, "retired_blocked": 0,
        }

    @staticmethod
    def _merge_source_stats(aggregate: dict, current: dict, source_name: str) -> None:
        # 先收敛本来源的真实变化路径，供后续精准媒体库刷新使用。
        finalize_changed_paths(current)
        for key in list(aggregate):
            if key in {
                "error_samples", "changes", "omitted_count",
                "changed_strm_paths", "changed_dirs",
                "retired_sources", "retired_blocked",
            }:
                continue
            value = current.get(key, 0) or 0
            if key == "clean_skipped":
                aggregate[key] = bool(aggregate[key]) or bool(
                    value and not current.get("stopped")
                )
                continue
            if key in {
                "request_p50_ms", "request_p95_ms", "request_p99_ms",
                "scan_workers_configured", "scan_workers_peak", "scan_queue_peak",
                "verify_workers_configured",
            }:
                aggregate[key] = max(float(aggregate[key]), float(value))
                if key.startswith("scan_") or key.endswith("_configured"):
                    aggregate[key] = int(aggregate[key])
                continue
            aggregate[key] += (
                float(value) if key.endswith("_seconds") else int(value)
            )
        remaining = max(0, 5000 - len(aggregate["changes"]))
        current_changes = current.get("changes") or []
        aggregate["changes"].extend(current_changes[:remaining])
        aggregate["omitted_count"] += int(current.get("omitted_count", 0) or 0)
        aggregate["omitted_count"] += max(0, len(current_changes) - remaining)
        aggregate["changed_strm_paths"] = list(dict.fromkeys([
            *aggregate.get("changed_strm_paths", []),
            *current.get("changed_strm_paths", []),
        ]))
        aggregate["changed_dirs"] = list(dict.fromkeys([
            *aggregate.get("changed_dirs", []),
            *current.get("changed_dirs", []),
        ]))
        for sample in current.get("error_samples") or []:
            text = f"{source_name}/{sample}"
            if text not in aggregate["error_samples"] and len(aggregate["error_samples"]) < 3:
                aggregate["error_samples"].append(text)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error(f"STRM 调度检查异常: {e}")
            try:
                self._drain_notification_outbox()
            except Exception:
                logger.exception("整理通知补发检查失败")
            self._wake_event.wait(timeout=10)
            self._wake_event.clear()

    @staticmethod
    def _drain_notification_outbox() -> None:
        """周期补发到期的整理通知；无待发项时只做一次计数查询。"""
        from app.repositories.organize_notifications import (
            count_pending_organize_notifications,
        )

        if not count_pending_organize_notifications():
            return
        from app.modules.organize_notification_outbox import (
            drain_organize_notifications,
        )

        drain_organize_notifications()

    def _tick(self) -> None:
        if not get_bool("STRM_SCHEDULE_ENABLED", False):
            with self._state_lock:
                self._next_run = None
                self._loaded_cron = ""
            return
        error = self.validate_config(auto_only=True)
        if error:
            logger.warning(f"STRM 定时任务配置无效: {error}")
            return
        cron_expr = get("STRM_SCHEDULE_CRON", "0 4 * * *").strip()
        now = datetime.now()
        with self._state_lock:
            if self._next_run is None or self._loaded_cron != cron_expr:
                self._next_run = self._calculate_next(cron_expr, now)
                self._loaded_cron = cron_expr
                logger.info(f"STRM 下次定时扫描: {self._next_run:%Y-%m-%d %H:%M:%S}")
            due = self._next_run is not None and now >= self._next_run
            if due:
                self._next_run = self._calculate_next(cron_expr, now)
        if due:
            result = self.trigger("cron")
            if not result.get("ok"):
                logger.warning(result.get("error", "STRM 定时任务触发失败"))

    def _run_incremental_sources(
        self,
        sources: list[dict[str, str]],
        changes: list[dict[str, object]],
        *,
        base_url: str,
        strm_root: str,
        exts: set[str],
        metadata_exts: set[str],
        threshold: int,
    ) -> tuple[dict, list[dict], bool, str]:
        """运行可信整理清单；返回 aggregate/source_results/stopped/fallback_reason。"""
        prepared_changes: list[dict[str, object]] = []
        for item in changes:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "video").lower() == "metadata":
                if not metadata_exts:
                    continue
                if str(item.get("action") or "upsert").lower() == "upsert":
                    name = str(item.get("name") or "")
                    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    if ext not in metadata_exts:
                        continue
            prepared_changes.append(item)
        changes = prepared_changes
        source_ids = {str(source["id"]) for source in sources}
        change_source_ids = {
            str(item.get("source_id") or "") for item in changes if isinstance(item, dict)
        }
        if not change_source_ids or not change_source_ids <= source_ids:
            stats = self._empty_stats()
            stats["fatal_incremental_error"] = True
            with self._state_lock:
                for row in self._source_runtime:
                    row["status"] = "failed"
            return stats, [], False, "整理目标未唯一匹配已配置 STRM 来源"
        if db.list_strm_retired_sources():
            return self._empty_stats(), [], False, "存在待清理的已移除 STRM 来源"
        grouped: dict[str, list[dict]] = {}
        for item in changes:
            grouped.setdefault(str(item.get("source_id") or ""), []).append(dict(item))
        aggregate = self._empty_stats()
        source_results: list[dict] = []
        stopped = False
        for source_index, source in enumerate(sources):
            source_changes = grouped.get(str(source["id"]), [])
            if not source_changes:
                with self._state_lock:
                    self._source_runtime[source_index]["status"] = "skipped"
                continue
            if self._stop_event.is_set():
                stopped = True
                break
            with self._state_lock:
                self._source_runtime[source_index]["status"] = "running"

            def source_progress(stage, completed, total, detail):
                if stage != "complete":
                    self._set_progress(stage, completed, total, detail)

            current = sync_strm_incremental(
                source_dir_id=source["id"],
                changes=source_changes,
                base_url=base_url,
                strm_root=strm_root,
                video_exts=exts,
                skip_threshold_mb=threshold,
                rel_prefix=str(source.get("rel_prefix") or ""),
                metadata_exts=metadata_exts,
                defer_metadata=True,
                source_name=source["name"],
                on_progress=source_progress,
                should_stop=self._stop_event.is_set,
            )
            completed_count = sum(int(current.get(key, 0) or 0) for key in (
                "generated", "skipped", "failed", "metadata_generated",
                "metadata_queued", "metadata_skipped", "metadata_failed",
            ))
            total_count = int(current.get("total", 0) or 0) + int(
                current.get("metadata_total", 0) or 0
            )
            stopped = bool(current.get("stopped"))
            fallback_reason = str(current.get("fallback_reason") or "")
            source_partial = bool(
                current.get("fallback_required")
                or int(current.get("failed", 0) or 0)
                or int(current.get("metadata_failed", 0) or 0)
            )
            with self._state_lock:
                self._source_runtime[source_index].update({
                    "status": "stopped" if stopped else (
                        "partial" if source_partial else "completed"
                    ),
                    "completed": completed_count,
                    "total": total_count,
                })
            source_results.append({
                "id": source["id"], "name": source["name"],
                "local_dir": _source_local_dir(strm_root, source),
                "stats": current,
            })
            self._merge_source_stats(aggregate, current, source["name"])
            if stopped:
                break
            if source_partial:
                return aggregate, source_results, False, (
                    fallback_reason or "精准增量未能安全完成"
                )
        return aggregate, source_results, stopped, ""

    def _run_full_sources(
        self,
        sources: list[dict[str, str]],
        *,
        base_url: str,
        strm_root: str,
        exts: set[str],
        metadata_exts: set[str],
        threshold: int,
    ) -> tuple[dict, list[dict], bool]:
        """执行全量来源扫描，并在整轮安全后统一提交删除型清理。"""
        aggregate = self._empty_stats()
        source_results: list[dict] = []
        _configured, source_error = configured_strm_source_plans()
        round_cleanup_safe = not bool(source_error) and bool(sources)
        stopped = self._stop_event.is_set()

        for source_index, source in enumerate(sources):
            if self._stop_event.is_set():
                stopped = True
                round_cleanup_safe = False
                break
            with self._state_lock:
                self._source_runtime[source_index]["status"] = "running"

            def source_progress(stage, completed, total, detail):
                if stage != "complete":
                    self._set_progress(stage, completed, total, detail)

            source_cleanup_actions: list = []
            current = sync_strm(
                source_dir_id=source["id"],
                base_url=base_url,
                strm_root=strm_root,
                video_exts=exts,
                skip_threshold_mb=threshold,
                rel_prefix=str(source.get("rel_prefix") or ""),
                metadata_exts=metadata_exts,
                defer_metadata=True,
                clean_invalid=False,
                clean_empty_dirs=False,
                deferred_cleanup_actions=source_cleanup_actions,
                source_name=source["name"],
                on_progress=source_progress,
                should_stop=self._stop_event.is_set,
            )
            completed_count = sum(int(current.get(key, 0) or 0) for key in (
                "generated", "skipped", "failed", "metadata_generated",
                "metadata_queued", "metadata_skipped", "metadata_failed",
            ))
            total_count = int(current.get("total", 0) or 0) + int(
                current.get("metadata_total", 0) or 0
            )
            source_stopped = bool(current.get("stopped"))
            source_partial = bool(
                int(current.get("failed", 0) or 0)
                or int(current.get("metadata_failed", 0) or 0)
                or current.get("scan_errors")
                or current.get("scan_incomplete")
                or (current.get("clean_skipped") and not source_stopped)
            )
            if source_stopped or source_partial:
                round_cleanup_safe = False
            with self._state_lock:
                self._source_runtime[source_index].update({
                    "status": "stopped" if source_stopped else (
                        "partial" if source_partial else "completed"
                    ),
                    "completed": completed_count,
                    "total": total_count,
                })
            source_results.append({
                "id": source["id"], "name": source["name"],
                "local_dir": _source_local_dir(strm_root, source),
                "stats": current,
                "_cleanup_actions": source_cleanup_actions,
                "_source_index": source_index,
            })
            stopped = source_stopped
            if stopped:
                break

        if len(source_results) != len(sources):
            round_cleanup_safe = False
        if self._stop_event.is_set():
            stopped = True
            round_cleanup_safe = False

        retirement = {
            "sources": 0, "blocked": 0, "cleaned": 0,
            "empty_dirs_cleaned": 0, "removed_paths": [],
            "removed_dir_paths": [], "errors": [], "stopped": False,
            "empty_dir_roots": [],
        }
        if round_cleanup_safe:
            self._set_progress("cleanup", 0, max(1, len(source_results)), "提交整轮安全清理")
            for cleanup_index, result in enumerate(source_results, start=1):
                current = result["stats"]
                try:
                    for action in result.pop("_cleanup_actions", []):
                        action()
                except Exception as exc:
                    current["clean_skipped"] = True
                    sample = f"整轮清理提交失败（{type(exc).__name__}）"
                    samples = current.setdefault("error_samples", [])
                    if sample not in samples and len(samples) < 3:
                        samples.append(sample)
                    logger.exception(
                        "STRM 整轮清理提交失败 source=%s", result["id"]
                    )
                if current.get("clean_skipped"):
                    round_cleanup_safe = False
                    with self._state_lock:
                        self._source_runtime[result["_source_index"]]["status"] = "partial"
                self._set_progress(
                    "cleanup", cleanup_index, len(source_results),
                    f"已清理 {cleanup_index}/{len(source_results)} 个来源",
                )

        if round_cleanup_safe:
            self._set_progress("retirement", 0, 1, "清理已移除的 STRM 来源")
            try:
                aggregate["metadata_queue_cancelled"] += int(
                    db.cancel_retired_strm_metadata_jobs(
                        {source["id"] for source in sources}
                    ) or 0
                )
            except Exception as exc:
                round_cleanup_safe = False
                logger.warning(
                    "跳过退役来源元数据队列维护 type=%s", type(exc).__name__
                )
            if round_cleanup_safe:
                retirement = clean_retired_strm_sources(
                    {source["id"] for source in sources},
                    should_stop=self._stop_event.is_set,
                    active_ids_complete=True,
                    clean_empty_dirs=False,
                )
                if retirement.get("stopped"):
                    stopped = True
                    round_cleanup_safe = False
                if retirement.get("blocked"):
                    round_cleanup_safe = False
            self._set_progress("retirement", 1, 1, "清理已移除的 STRM 来源")

        if not round_cleanup_safe:
            cleanup_reason = (
                "整轮扫描未完整或存在异常，已跳过删除型清理、来源退役与空目录清理"
            )
            aggregate["clean_skipped"] = True
            if cleanup_reason not in aggregate["error_samples"]:
                aggregate["error_samples"].append(cleanup_reason)
            for result in source_results:
                result.pop("_cleanup_actions", None)
                result.pop("_source_index", None)
                current = result["stats"]
                current["clean_skipped"] = True
                samples = current.setdefault("error_samples", [])
                if cleanup_reason not in samples and len(samples) < 3:
                    samples.append(cleanup_reason)

        for result in source_results:
            self._merge_source_stats(aggregate, result["stats"], result["name"])
            result.pop("_cleanup_actions", None)
            result.pop("_source_index", None)

        aggregate["retired_sources"] = int(retirement.get("sources", 0) or 0)
        aggregate["retired_blocked"] = int(retirement.get("blocked", 0) or 0)
        aggregate["cleaned"] += int(retirement.get("cleaned", 0) or 0)
        aggregate["empty_dirs_cleaned"] += int(
            retirement.get("empty_dirs_cleaned", 0) or 0
        )
        if retirement.get("blocked"):
            aggregate["clean_skipped"] = True
            aggregate["error_samples"].extend(list(retirement.get("errors") or [])[:3])
        for path in retirement.get("removed_paths", []):
            append_change(aggregate, relative_change("removed", path, strm_root or "/"))
            aggregate["changed_strm_paths"].append(str(path))
        for path in retirement.get("removed_dir_paths", []):
            append_change(aggregate, relative_change("removed_dir", path, strm_root or "/"))

        cleanup_roots: list[Path] = []
        if round_cleanup_safe and strm_root:
            cleanup_roots.append(Path(strm_root) / STRM_SUBDIR)
        if round_cleanup_safe:
            cleanup_roots.extend(
                Path(item) for item in retirement.get("empty_dir_roots", []) if str(item)
            )
        unique_cleanup_roots: list[Path] = []
        seen_cleanup_roots: set[str] = set()
        for root in cleanup_roots:
            normalized = str(root.expanduser().resolve(strict=False))
            if normalized in seen_cleanup_roots:
                continue
            seen_cleanup_roots.add(normalized)
            unique_cleanup_roots.append(Path(normalized))
        if round_cleanup_safe and unique_cleanup_roots and not self._stop_event.is_set():
            cleanup_started = monotonic()
            for cleanup_root in unique_cleanup_roots:
                empty_cleanup = clean_empty_strm_dirs(
                    strm_root,
                    should_stop=self._stop_event.is_set,
                    owned_root=cleanup_root,
                )
                aggregate["empty_dirs_cleaned"] += int(
                    empty_cleanup.get("empty_dirs_cleaned", 0) or 0
                )
                for path in empty_cleanup.get("removed_dir_paths", []):
                    append_change(
                        aggregate, relative_change("removed_dir", path, strm_root or "/")
                    )
                if empty_cleanup.get("stopped"):
                    stopped = True
                    aggregate["clean_skipped"] = True
                    break
            aggregate["cleanup_elapsed_seconds"] += max(
                0.0, monotonic() - cleanup_started
            )
        return aggregate, source_results, stopped

    def _execute_locked(self, trigger_type: str) -> dict:
        options = dict(self._run_options)
        request_ids = _request_ids(options)
        run_id = db.add_task_run(TASK_NAME, trigger_type)
        claimed_targets: list[dict] = []
        lease_heartbeat: _ChangeTargetLeaseHeartbeat | None = None
        for request_id in request_ids:
            db.update_download_request(
                request_id, strm_run_id=run_id, strm_status="running",
                strm_error="", strm_finished_at=None,
            )
        started = datetime.now()
        try:
            error = self.validate_config(auto_only=False)
            if error:
                raise ValueError(error)
            sources = self._source_dirs()
            base_url = get("GY_STRM_BASE_URL").strip()
            strm_root = get("STRM_ROOT").strip()
            exts = self._video_exts()
            metadata_exts = self._metadata_exts()
            threshold = get_int("STRM_SKIP_THRESHOLD_MB", 0)
            logger.info(
                "STRM 任务开始 trigger=%s sources=%s root=%s retirements=%s",
                trigger_type, len(sources), strm_root, len(db.list_strm_retired_sources()),
            )
            aggregate = self._empty_stats()
            source_results = []
            with self._state_lock:
                self._source_runtime = [
                    {
                        "id": source["id"], "name": source["name"],
                        "status": "pending", "completed": 0, "total": 0,
                    }
                    for source in sources
                ]

            mode = "full"
            fallback_used = False
            fallback_reason = ""
            incremental_stats: dict | None = None
            requested_mode = str(options.get("sync_mode") or "auto").strip().lower()
            if trigger_type == "cron":
                requested_mode = "full"
            if bool(options.get("force_full")):
                requested_mode = "full"
            organize_changes = _merge_organize_changes(options.get("organize_changes"))
            # 持久队列是唯一权威来源：内存清单只是本轮的快捷路径，
            # 领取结果会补齐上一次进程中断或失败重试遗留的变化目标。
            claimed_targets = self._claim_change_targets(trigger_type, requested_mode)
            if claimed_targets:
                lease_heartbeat = _ChangeTargetLeaseHeartbeat(
                    claimed_targets, max(30, get_int("STRM_CHANGE_LEASE_SECONDS", 900))
                )
                lease_heartbeat.start()
                organize_changes = _merge_organize_changes(
                    organize_changes,
                    [
                        change
                        for target in claimed_targets
                        for change in (target.get("changes") or [])
                    ],
                )
            use_incremental = bool(
                (trigger_type == "organize" or requested_mode == "fast")
                and organize_changes
                and requested_mode != "full"
            )
            if requested_mode == "fast" and not organize_changes:
                mode = "fast_noop"
                stopped = False
                with self._state_lock:
                    for row in self._source_runtime:
                        row["status"] = "skipped"
                self._set_progress("complete", 1, 1, "没有待处理的增量变化")
            elif use_incremental:
                aggregate, source_results, stopped, fallback_reason = (
                    self._run_incremental_sources(
                        sources,
                        organize_changes,
                        base_url=base_url,
                        strm_root=strm_root,
                        exts=exts,
                        metadata_exts=metadata_exts,
                        threshold=threshold,
                    )
                )
                mode = "fast" if requested_mode == "fast" else "incremental"
                if (
                    fallback_reason
                    and not stopped
                    and aggregate.get("fatal_incremental_error")
                ):
                    raise RuntimeError(
                        f"STRM 整理联动配置错误：{fallback_reason}"
                    )
                if fallback_reason and not stopped:
                    if requested_mode == "fast":
                        # 快速同步必须保持“只处理可信变化”的边界；遇到远端
                        # 快照不一致时退回队列并提示用户执行完整校准，不能在
                        # 未二次确认的情况下自动扩大为全量扫描和失效清理。
                        mode = "fast_partial"
                        aggregate["clean_skipped"] = True
                        logger.warning(
                            "STRM 快速同步需要完整校准 trigger=%s reason=%s",
                            trigger_type,
                            fallback_reason,
                        )
                    else:
                        incremental_stats = aggregate
                        fallback_used = True
                        mode = "full_fallback"
                        logger.warning(
                            "STRM 精准增量回退全量 trigger=%s reason=%s",
                            trigger_type,
                            fallback_reason,
                        )
                        with self._state_lock:
                            for row in self._source_runtime:
                                row.update({
                                    "status": "pending", "completed": 0, "total": 0,
                                })
                        aggregate, source_results, stopped = self._run_full_sources(
                            sources,
                            base_url=base_url,
                            strm_root=strm_root,
                            exts=exts,
                            metadata_exts=metadata_exts,
                            threshold=threshold,
                        )
                        # 增量阶段已经真实落盘的变化仍计入最终通知与刷新判断；
                        # 全量阶段负责重新核验和补齐，不重复累加扫描总量。
                        for key in (
                            "generated", "created", "updated",
                            "metadata_generated", "cleaned",
                            "metadata_cleaned", "empty_dirs_cleaned",
                        ):
                            aggregate[key] += int(incremental_stats.get(key, 0) or 0)
                        prior_changes = list(incremental_stats.get("changes") or [])
                        full_changes = list(aggregate.get("changes") or [])
                        remaining = max(0, 5000 - len(prior_changes))
                        aggregate["changes"] = prior_changes + full_changes[:remaining]
                        aggregate["omitted_count"] += int(
                            incremental_stats.get("omitted_count", 0) or 0
                        ) + max(0, len(full_changes) - remaining)
                        # 增量阶段真实落盘的路径必须保留：全量复核会把它们标记
                        # 为跳过而不再记录，否则精准媒体库刷新会漏掉这些目录。
                        aggregate["changed_strm_paths"] = list(dict.fromkeys([
                            *incremental_stats.get("changed_strm_paths", []),
                            *aggregate.get("changed_strm_paths", []),
                        ]))
                        aggregate["changed_dirs"] = list(dict.fromkeys([
                            *incremental_stats.get("changed_dirs", []),
                            *aggregate.get("changed_dirs", []),
                        ]))
            else:
                aggregate, source_results, stopped = self._run_full_sources(
                    sources,
                    base_url=base_url,
                    strm_root=strm_root,
                    exts=exts,
                    metadata_exts=metadata_exts,
                    threshold=threshold,
                )

            if stopped or self._stop_event.is_set():
                with self._state_lock:
                    for row in self._source_runtime:
                        if row.get("status") == "pending":
                            row["status"] = "skipped"
                for key in (
                    "scan_elapsed_seconds", "generate_elapsed_seconds",
                    "metadata_elapsed_seconds", "cleanup_elapsed_seconds",
                    "failure_resolve_elapsed_seconds", "refresh_elapsed_seconds",
                ):
                    aggregate[key] = round(float(aggregate.get(key, 0.0) or 0.0), 3)
                elapsed = round((datetime.now() - started).total_seconds(), 1)
                result = {
                    "stats": aggregate, "sources": source_results,
                    "media_refresh": {}, "elapsed_seconds": elapsed,
                    "source_runtime": [dict(row) for row in self._source_runtime],
                    "mode": mode,
                    "fallback_used": fallback_used,
                    "fallback_reason": fallback_reason,
                }
                db.finish_task_run(
                    run_id, "skipped", result=json.dumps(result, ensure_ascii=False)
                )
                for request_id in request_ids:
                    db.update_download_request(
                        request_id, strm_status="stopped",
                        strm_error="服务停止，STRM 同步已安全中止",
                        strm_finished_at=db.now(),
                    )
                self._set_progress("stopped", 1, 1, "同步已停止")
                if lease_heartbeat:
                    lease_heartbeat.stop()
                    lease_heartbeat = None
                self._settle_change_targets(claimed_targets, "stopped")
                return {"ok": True, "stopped": True, **result}

            for key in (
                "scan_elapsed_seconds", "generate_elapsed_seconds",
                "metadata_elapsed_seconds", "cleanup_elapsed_seconds",
                "failure_resolve_elapsed_seconds", "refresh_elapsed_seconds",
            ):
                aggregate[key] = round(float(aggregate.get(key, 0.0) or 0.0), 3)
            stats = aggregate
            has_changes = any(int(stats.get(key, 0) or 0) > 0 for key in (
                "generated", "metadata_generated", "cleaned",
                "metadata_cleaned"
            ))
            self._set_progress("refresh", 0, 1, "刷新媒体库")
            refresh_started = monotonic()
            media_refresh = self._refresh_media_servers(
                emby_enabled=options.get("emby_refresh_override"),
                has_changes=has_changes,
                changed_paths=list(stats.get("changed_strm_paths") or []),
                changed_dirs=list(stats.get("changed_dirs") or []),
            )
            stats["refresh_elapsed_seconds"] = round(
                max(0.0, monotonic() - refresh_started), 3
            )
            self._set_progress("refresh", 1, 1, "刷新媒体库")
            self._set_progress("complete", 1, 1, "同步完成")
            elapsed = round((datetime.now() - started).total_seconds(), 1)
            result = {
                "stats": stats,
                "sources": source_results,
                "media_refresh": media_refresh,
                "elapsed_seconds": elapsed,
                "source_runtime": [dict(row) for row in self._source_runtime],
                "mode": mode,
                "base_url": base_url.rstrip("/"),
                "fallback_used": fallback_used,
                "fallback_reason": fallback_reason,
            }
            partial = bool(
                int(stats.get("failed", 0) or 0)
                or int(stats.get("metadata_failed", 0) or 0)
                or stats.get("clean_skipped")
                or any(value is False for value in media_refresh.values())
            )
            terminal_status = "partial" if partial else "completed"
            db.finish_task_run(
                run_id, "partial" if partial else "success",
                result=json.dumps(result, ensure_ascii=False),
            )
            for request_id in request_ids:
                db.update_download_request(
                    request_id, strm_status=terminal_status,
                    strm_error=("STRM 同步部分完成，请查看运行记录" if partial else ""),
                    strm_finished_at=db.now(),
                )
            self._notify_success(
                stats, media_refresh, elapsed, trigger_type, source_results, strm_root,
                notify_override=options.get("notify_override"),
                chat_ids=list(options.get("chat_ids") or []),
            )
            self._notify_details(
                stats, trigger_type,
                enabled_override=options.get("detail_notify_override"),
                chat_ids=list(options.get("chat_ids") or []),
            )
            logger.info(
                "STRM 任务%s trigger=%s mode=%s fallback=%s elapsed=%ss",
                "部分完成" if partial else "完成", trigger_type, mode,
                fallback_used, elapsed,
            )
            # 部分完成意味着仍有目标未落盘，必须按失败重试而不是标记完成。
            if lease_heartbeat:
                lease_heartbeat.stop()
                lease_heartbeat = None
            self._settle_change_targets(
                claimed_targets,
                "failed" if partial else "completed",
                error="STRM 同步部分完成，等待重试" if partial else "",
            )
            return {"ok": True, "partial": partial, **result}
        except Exception as exc:
            with self._state_lock:
                for row in self._source_runtime:
                    if row.get("status") == "running":
                        row["status"] = "failed"
            error_text = str(exc)
            self._set_progress("failed", 1, 1, "同步失败")
            db.finish_task_run(run_id, "failed", error=error_text)
            for request_id in request_ids:
                db.update_download_request(
                    request_id, strm_status="failed", strm_error=error_text[:500],
                    strm_finished_at=db.now(),
                )
            logger.error("STRM 任务失败 trigger=%s: %s", trigger_type, error_text)
            if lease_heartbeat:
                lease_heartbeat.stop()
                lease_heartbeat = None
            self._settle_change_targets(claimed_targets, "failed", error=error_text)
            self._notify_failure(
                error_text,
                trigger_type,
                notify_override=options.get("notify_override"),
                chat_ids=list(options.get("chat_ids") or []),
            )
            return {"ok": False, "error": error_text}
        finally:
            if lease_heartbeat:
                lease_heartbeat.stop()
            with self._state_lock:
                if self._worker is threading.current_thread():
                    self._worker = None
                self._running = False
                self._current_trigger = ""
                self._run_options = {}
            self._run_lock.release()
            self._run_released_event.set()
            try:
                from app.modules.strm_metadata_worker import get_strm_metadata_worker

                get_strm_metadata_worker().wake()
            except Exception:
                logger.exception("唤醒 STRM 元数据后台下载器失败")

    @staticmethod
    def _calculate_next(expr: str, base: Optional[datetime] = None) -> datetime:
        return croniter(expr, base or datetime.now()).get_next(datetime)

    @staticmethod
    def _source_dirs() -> list[dict[str, str]]:
        """读取并规划 STRM 来源；同步与失败重试共享同一 prefix/namespace。"""
        sources, error = configured_strm_source_plans()
        if error:
            logger.warning(error)
            return []
        return sources

    @staticmethod
    def _video_exts() -> set[str]:
        raw = get("STRM_VIDEO_EXTS", "").strip()
        if not raw:
            return set(DEFAULT_VIDEO_EXTS)
        return {
            item.strip().lower().lstrip(".")
            for item in raw.replace("，", ",").split(",")
            if item.strip()
        }

    @staticmethod
    def _metadata_exts() -> set[str]:
        if not get_bool("STRM_METADATA_ENABLED", False):
            return set()
        raw = get("STRM_METADATA_EXTS", "").strip()
        if not raw:
            return set(DEFAULT_METADATA_EXTS)
        return {
            item.strip().lower().lstrip(".")
            for item in raw.replace("，", ",").split(",")
            if item.strip()
        }

    @staticmethod
    def _refresh_media_servers(emby_enabled: bool | None = None,
                               has_changes: bool = True,
                               changed_paths: list[str] | None = None,
                               changed_dirs: list[str] | None = None) -> dict:
        """有 STRM 增量时刷新媒体库；整理联动可单独关闭 Emby。"""
        if not has_changes:
            logger.info("STRM 本轮无增量变化，跳过媒体库刷新")
            return {}
        strm_root = get("STRM_ROOT", "")
        plan = plan_refresh_targets(
            changed_paths or [],
            changed_dirs or [],
            media_roots=[strm_root, str(Path(strm_root) / STRM_SUBDIR)] if strm_root else [],
        )
        if plan.reason:
            logger.info("媒体库精准刷新降级 reason=%s", plan.reason)
        if not plan.has_targets:
            logger.info(
                "本轮变化没有安全的媒体库刷新目标，跳过媒体库刷新 reason=%s",
                plan.reason or "无变化目标",
            )
            return {}

        def refresh(client) -> bool:
            all_ok = True
            for index, batch in enumerate(plan.batches, start=1):
                outcome = client.refresh_for_paths(list(batch))
                if outcome.get("fallback"):
                    logger.info(
                        "%s 精准刷新部分降级 batch=%s/%s reason=%s items=%s libraries=%s",
                        client.display_name,
                        index,
                        len(plan.batches),
                        outcome.get("fallback"),
                        len(outcome.get("items") or []),
                        len(outcome.get("libraries") or []),
                    )
                all_ok = bool(outcome.get("ok")) and all_ok
            return all_ok

        results: dict[str, bool] = {}
        if get_bool("JELLYFIN_ENABLED") and get("JELLYFIN_URL") and get("JELLYFIN_API_KEY"):
            results["Jellyfin"] = refresh(JellyfinClient(
                get("JELLYFIN_URL"), get("JELLYFIN_API_KEY")
            ))
        allow_emby = True if emby_enabled is None else emby_enabled
        if allow_emby and get_bool("EMBY_ENABLED") and get("EMBY_URL") and get("EMBY_TOKEN"):
            results["Emby"] = refresh(EmbyClient(
                get("EMBY_URL"), get("EMBY_TOKEN")
            ))
        if any(results.values()):
            from app.services import clear_dashboard_cache

            clear_dashboard_cache()
        return results

    @staticmethod
    def _trigger_label(trigger_type: str) -> str:
        return {
            "manual": "手动执行",
            "organize": "整理联动",
            "cron": "定时任务",
            "telegram": "Telegram 命令",
        }.get(str(trigger_type or ""), str(trigger_type or "未知"))

    @staticmethod
    def _build_success_event(stats: dict, refresh: dict, elapsed: float,
                             trigger_type: str, sources: list[dict],
                             strm_root: str) -> NotificationEvent:
        partial = (
            int(stats.get("failed", 0) or 0) > 0
            or int(stats.get("metadata_failed", 0) or 0) > 0
            or bool(stats.get("clean_skipped"))
        )
        names = [str(item.get("name") or item.get("id") or "未命名") for item in sources]
        source_text = f"{len(sources)} 个"
        if names:
            source_text += " · " + "、".join(names[:3])
            if len(names) > 3:
                source_text += f" 等 {len(names)} 个"
        refresh_text = " / ".join(
            f"{name} {'✅' if ok else '❌'}" for name, ok in refresh.items()
        ) or "未启用或本轮无变化"
        fields = (
            ("触发方式", STRMScheduler._trigger_label(trigger_type)),
            ("同步来源", source_text),
            ("本地目录", strm_root),
            ("扫描范围", f"{int(stats.get('directories', 0) or 0)} 个目录"),
            ("扫描并发", (
                f"峰值 {int(stats.get('scan_workers_peak', 0) or 0)} / "
                f"配置 {int(stats.get('scan_workers_configured', 0) or 0)}"
            )),
            ("云端请求", (
                f"{int(stats.get('directory_requests', 0) or 0)} 次 · "
                f"{int(stats.get('scan_pages', 0) or 0)} 页 · "
                f"{int(stats.get('read_retries', 0) or 0)} 次重试"
            )),
            ("请求延迟", (
                f"P50 {float(stats.get('request_p50_ms', 0) or 0):.0f}ms · "
                f"P95 {float(stats.get('request_p95_ms', 0) or 0):.0f}ms · "
                f"P99 {float(stats.get('request_p99_ms', 0) or 0):.0f}ms"
            )),
            ("视频", f"{int(stats.get('total', 0) or 0)} 个"),
            ("STRM 变化", (
                f"{int(stats.get('generated', 0) or 0)} 生成/更新 · "
                f"{int(stats.get('skipped', 0) or 0)} 未变化/跳过 · "
                f"{int(stats.get('failed', 0) or 0)} 失败"
            )),
            ("元数据", (
                f"{int(stats.get('metadata_generated', 0) or 0)} 新增/更新 · "
                f"{int(stats.get('metadata_queued', 0) or 0)} 后台排队 · "
                f"{int(stats.get('metadata_skipped', 0) or 0)} 未变化 · "
                f"{int(stats.get('metadata_failed', 0) or 0)} 失败 · "
                f"{int(stats.get('metadata_cleaned', 0) or 0)} 清理"
            )),
            ("清理", (
                f"{int(stats.get('cleaned', 0) or 0)} 个无效 STRM · "
                f"{int(stats.get('empty_dirs_cleaned', 0) or 0)} 个空目录"
            )),
            ("媒体库刷新", refresh_text),
            ("阶段耗时", (
                f"扫描 {float(stats.get('scan_elapsed_seconds', 0) or 0):.1f}s · "
                f"生成 {float(stats.get('generate_elapsed_seconds', 0) or 0):.1f}s · "
                f"元数据 {float(stats.get('metadata_elapsed_seconds', 0) or 0):.1f}s · "
                f"清理 {float(stats.get('cleanup_elapsed_seconds', 0) or 0):.1f}s · "
                f"刷新 {float(stats.get('refresh_elapsed_seconds', 0) or 0):.1f}s"
            )),
            ("总耗时", f"{elapsed} 秒"),
        )
        errors = tuple(
            f"错误摘要：{str(item)[:300]}" for item in (stats.get("error_samples") or [])[:3]
        )
        return NotificationEvent(
            "⚠️ STRM 同步部分完成" if partial else "✅ STRM 同步完成",
            fields=fields, lines=errors,
        )

    @staticmethod
    def _notify_success(stats: dict, refresh: dict, elapsed: float,
                        trigger_type: str, sources: list[dict], strm_root: str,
                        notify_override: bool | None = None,
                        chat_ids: list[str] | None = None) -> None:
        enabled = get_bool("STRM_NOTIFY_ENABLED", True) if notify_override is None else notify_override
        if trigger_type == "telegram" or not enabled:
            return
        event = STRMScheduler._build_success_event(
            stats, refresh, elapsed, trigger_type, sources, strm_root
        )
        recipients = list(dict.fromkeys(str(item) for item in (chat_ids or []) if str(item)))
        if recipients:
            for recipient in recipients:
                send(event, chat_id=recipient)
        else:
            send(event)

    @staticmethod
    def _notify_details(stats: dict, trigger_type: str,
                        enabled_override: bool | None = None,
                        chat_ids: list[str] | None = None) -> None:
        """仅为整理联动发送有界文件明细，汇总通知开关与其相互独立。"""
        if trigger_type != "organize":
            return
        enabled = (
            get_bool("GY_ORGANIZE_STRM_DETAIL_NOTIFY", True)
            if enabled_override is None else enabled_override
        )
        if not enabled:
            return
        for message in build_strm_detail_messages(
            stats.get("changes") or [],
            omitted_count=int(stats.get("omitted_count", 0) or 0),
            max_messages=max(0, get_int("STRM_DETAIL_MAX_MESSAGES", 200)),
        ):
            # notifier.send 接收 HTML 文本；完整转义动态目录和文件名。
            recipients = list(dict.fromkeys(str(item) for item in (chat_ids or []) if str(item)))
            if recipients:
                for recipient in recipients:
                    send_text(html.escape(message), chat_id=recipient)
            else:
                send_text(html.escape(message))

    @staticmethod
    def _notify_failure(error: str, trigger_type: str,
                        notify_override: bool | None = None,
                        chat_ids: list[str] | None = None) -> None:
        enabled = get_bool("STRM_NOTIFY_ENABLED", True) if notify_override is None else notify_override
        if trigger_type != "telegram" and enabled:
            event = NotificationEvent(
                "❌ STRM 同步失败",
                fields=(("触发", trigger_type), ("错误", error[:500])),
            )
            recipients = list(dict.fromkeys(str(item) for item in (chat_ids or []) if str(item)))
            if recipients:
                for recipient in recipients:
                    send(event, chat_id=recipient)
            else:
                send(event)

    @staticmethod
    def _row_to_dict(row) -> dict:
        if not row:
            return {}
        result = {}
        if row["result"]:
            try:
                result = json.loads(row["result"])
            except (ValueError, TypeError):
                result = {"raw": row["result"]}
        return {
            "id": row["id"],
            "trigger_type": row["trigger_type"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "result": result,
            "error": row["error"] or "",
        }


_scheduler = STRMScheduler()


def get_scheduler() -> STRMScheduler:
    return _scheduler
