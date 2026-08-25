"""STRM 伴随元数据后台下载器。

完整/增量 STRM 只负责把远端快照写入持久队列；本模块在独立线程中下载，
并仅在最终原子提交时短暂获取 STRM_OPERATION_LOCK，避免网络抖动阻塞 STRM。
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from app import database as db
from app.clients.guangya import GuangYaClient
from app.config import get, get_bool, get_int
from app.logger import get_logger, redact_sensitive_text
from app.modules.process_lock import CrossProcessLock
from app.modules.strm import (
    STRM_OPERATION_LOCK,
    _STRMStopped,
    commit_strm_metadata_job,
    prepare_strm_metadata_job,
)

logger = get_logger(__name__)


class STRMMetadataWorker:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._worker_lock = CrossProcessLock("strm-metadata-worker")
        self._consumer_active = False
        self._client: GuangYaClient | None = None
        self._owner = f"metadata-{uuid.uuid4().hex[:12]}"
        self._consecutive_failures = 0
        self._breaker_until = 0.0
        self._current_job_id = 0
        self._last_error_type = ""
        self._completed_session = 0
        self._failed_session = 0
        self._changed_paths: list[str] = []
        self._last_refresh_at = time.monotonic()
        self._refresh_retry_pending = False

    def start(self) -> None:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name="strm-metadata-worker", daemon=True
            )
            self._thread.start()
        self._wake_event.set()
        logger.info("STRM 元数据后台下载器已启动")

    def stop(self, timeout: float = 30.0) -> bool:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, float(timeout or 0.1)))
        stopped = not thread or not thread.is_alive()
        if stopped:
            self._thread = None
            client = self._client
            self._client = None
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        else:
            logger.warning("STRM 元数据后台下载器未能在关闭超时内结束")
        return stopped

    def wake(self) -> None:
        self._wake_event.set()

    def status(self) -> dict[str, object]:
        counts = db.count_strm_metadata_jobs()
        with self._state_lock:
            breaker_remaining = max(0, int(self._breaker_until - time.monotonic()))
            return {
                **counts,
                "enabled": get_bool("STRM_METADATA_ENABLED", False),
                "worker_running": bool(self._thread and self._thread.is_alive()),
                "consumer_active": bool(self._consumer_active),
                "current_job_id": int(self._current_job_id),
                "breaker_seconds": breaker_remaining,
                "last_error_type": self._last_error_type,
                "completed_session": int(self._completed_session),
                "failed_session": int(self._failed_session),
            }

    def _runtime_client(self) -> GuangYaClient:
        if self._client is None:
            self._client = GuangYaClient()
        return self._client

    def _loop(self) -> None:
        owns_lock = False
        try:
            # 只允许一个进程消费同一持久队列。拿到独占锁后，才可以立即恢复
            # 上个进程遗留的 running 租约，不会抢占仍活跃的下载器。
            while not self._stop_event.is_set():
                if self._worker_lock.acquire(blocking=False):
                    owns_lock = True
                    break
                self._stop_event.wait(1.0)
            if not owns_lock:
                return
            with self._state_lock:
                self._consumer_active = True
            recovered = db.recover_stale_strm_metadata_jobs(force=True)
            if recovered:
                logger.warning("已恢复中断的 STRM 元数据任务 count=%s", recovered)
            while not self._stop_event.is_set():
                try:
                    worked = self._process_one()
                except Exception:
                    logger.exception("STRM 元数据后台轮询异常")
                    worked = False
                if worked:
                    interval_ms = max(
                        0, min(
                            get_int("STRM_METADATA_REQUEST_INTERVAL_MS", 0), 10000
                        )
                    )
                    if interval_ms:
                        self._stop_event.wait(interval_ms / 1000.0)
                    continue
                self._flush_media_refresh(force=True)
                self._wake_event.wait(timeout=5.0)
                self._wake_event.clear()
        finally:
            self._flush_media_refresh(force=True)
            try:
                db.recover_stale_strm_metadata_jobs(force=True, owner=self._owner)
            except Exception:
                logger.exception("恢复当前 STRM 元数据租约失败")
            with self._state_lock:
                self._consumer_active = False
            if owns_lock:
                self._worker_lock.release()

    def _process_one(self) -> bool:
        if not get_bool("STRM_METADATA_ENABLED", False):
            return False
        root_text = get("STRM_ROOT", "").strip()
        if not root_text:
            return False
        now_mono = time.monotonic()
        with self._state_lock:
            breaker_until = self._breaker_until
        if breaker_until > now_mono:
            return False
        lease_seconds = max(30, get_int("STRM_METADATA_LEASE_SECONDS", 900))
        jobs = db.claim_due_strm_metadata_jobs(
            owner=self._owner, lease_seconds=lease_seconds, limit=1
        )
        if not jobs:
            return False
        job = jobs[0]
        job_id = int(job["id"])
        lease_generation = int(job["lease_generation"] or 0)
        revision = int(job["revision"] or 0)
        with self._state_lock:
            self._current_job_id = job_id
        heartbeat_stop = threading.Event()

        def renew_lease() -> None:
            interval = max(5.0, float(lease_seconds) / 3.0)
            while not heartbeat_stop.wait(interval):
                try:
                    if not db.renew_strm_metadata_job_lease(
                        job_id,
                        expected_owner=self._owner,
                        expected_lease_generation=lease_generation,
                        lease_seconds=lease_seconds,
                    ):
                        logger.warning(
                            "STRM 元数据任务租约已失效 job=%s generation=%s",
                            job_id, lease_generation,
                        )
                        return
                except Exception:
                    logger.exception("续租 STRM 元数据任务失败 job=%s", job_id)

        heartbeat = threading.Thread(
            target=renew_lease, name=f"strm-metadata-lease-{job_id}", daemon=True
        )
        heartbeat.start()
        prepared_job = None
        try:
            extension = str(job.get("filename") or "").rsplit(".", 1)[-1].lower()
            configured = {
                item.strip().lower().lstrip(".")
                for item in get("STRM_METADATA_EXTS", "").replace("，", ",").split(",")
                if item.strip()
            }
            if configured and extension not in configured:
                db.cancel_strm_metadata_job(
                    str(job["source_id"]), str(job["file_id"]),
                    reason="元数据扩展名已从同步配置移除",
                )
                return True
            prepared_job = prepare_strm_metadata_job(
                job, root_text, client=self._runtime_client(),
                should_stop=self._stop_event.is_set,
            )
            # 下载阶段不持有 STRM 写锁；仅最终原子替换和索引提交需要串行。
            while not self._stop_event.is_set():
                if STRM_OPERATION_LOCK.acquire(blocking=False):
                    break
                self._stop_event.wait(0.25)
            else:
                raise _STRMStopped("元数据后台任务已停止")
            try:
                if not db.strm_metadata_job_is_current(
                    job_id,
                    expected_lease_generation=lease_generation,
                    expected_revision=revision,
                    expected_owner=self._owner,
                ):
                    prepared = prepared_job.get("prepared")
                    temp = getattr(prepared, "temp", None)
                    if isinstance(temp, Path):
                        temp.unlink(missing_ok=True)
                    db.complete_strm_metadata_job(
                        job_id,
                        expected_lease_generation=lease_generation,
                        expected_revision=revision,
                        expected_owner=self._owner,
                    )
                    return True
                result = commit_strm_metadata_job(
                    job, prepared_job, root_text,
                    should_stop=self._stop_event.is_set,
                )
                settled = db.complete_strm_metadata_job(
                    job_id,
                    expected_lease_generation=lease_generation,
                    expected_revision=revision,
                    expected_owner=self._owner,
                    refresh_path=str(result.get("path") or ""),
                )
            finally:
                STRM_OPERATION_LOCK.release()
            if settled == "completed":
                path = str(result.get("path") or "")
                if path:
                    self._changed_paths.append(path)
                with self._state_lock:
                    self._consecutive_failures = 0
                    self._last_error_type = ""
                    self._completed_session += 1
                self._flush_media_refresh(force=False)
            return True
        except _STRMStopped:
            prepared = prepared_job.get("prepared") if isinstance(prepared_job, dict) else None
            temp = getattr(prepared, "temp", None)
            if isinstance(temp, Path):
                temp.unlink(missing_ok=True)
            db.recover_stale_strm_metadata_jobs(force=True, owner=self._owner)
            return False
        except Exception as exc:
            prepared = prepared_job.get("prepared") if isinstance(prepared_job, dict) else None
            temp = getattr(prepared, "temp", None)
            if isinstance(temp, Path):
                temp.unlink(missing_ok=True)
            error_type = type(exc).__name__
            state = db.fail_or_retry_strm_metadata_job(
                job_id,
                expected_lease_generation=lease_generation,
                expected_revision=revision,
                expected_owner=self._owner,
                error_type=error_type,
                error=exc,
            )
            db.record_strm_failure(
                source_id=str(job.get("source_id") or ""),
                source_name=str(job.get("source_name") or ""),
                file_id=str(job.get("file_id") or ""),
                parent_id=str(job.get("parent_id") or ""),
                filename=str(job.get("filename") or ""),
                action="metadata",
                rel_dir=str(job.get("rel_dir") or ""),
                target_rel_path=str(job.get("target_rel_path") or ""),
                error=exc,
            )
            with self._state_lock:
                self._consecutive_failures += 1
                self._last_error_type = error_type
                self._failed_session += 1
                if self._consecutive_failures >= 5:
                    pause_seconds = max(
                        30, min(get_int("STRM_METADATA_BREAKER_SECONDS", 120), 1800)
                    )
                    self._breaker_until = time.monotonic() + pause_seconds
                    self._consecutive_failures = 0
                    logger.warning(
                        "STRM 元数据下载连续失败，已暂停后台消费 seconds=%s",
                        pause_seconds,
                    )
            logger.warning(
                "STRM 元数据后台下载失败 job=%s state=%s type=%s error=%s",
                job_id, state, error_type,
                redact_sensitive_text(str(exc))[:300],
            )
            return True
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1.0)
            with self._state_lock:
                self._current_job_id = 0

    def _flush_media_refresh(self, *, force: bool) -> None:
        durable_paths = db.list_strm_metadata_refresh_paths(limit=20000)
        paths = list(dict.fromkeys([*durable_paths, *self._changed_paths]))
        if not paths:
            return
        batch_size = max(
            50, min(get_int("STRM_METADATA_REFRESH_BATCH_SIZE", 500), 5000)
        )
        interval = max(
            30, min(get_int("STRM_METADATA_REFRESH_INTERVAL_SECONDS", 300), 3600)
        )
        elapsed = time.monotonic() - self._last_refresh_at
        if self._refresh_retry_pending and not force and elapsed < interval:
            return
        if not force and len(paths) < batch_size and elapsed < interval:
            return
        self._last_refresh_at = time.monotonic()
        try:
            from app.modules.scheduler import STRMScheduler

            results = STRMScheduler._refresh_media_servers(
                has_changes=True,
                changed_paths=paths,
                changed_dirs=[],
            )
        except Exception:
            self._refresh_retry_pending = True
            logger.exception("STRM 元数据落盘后的媒体库刷新失败，将保留变更稍后重试")
            return
        if results and not all(bool(value) for value in results.values()):
            self._refresh_retry_pending = True
            logger.warning("STRM 元数据已落盘，但媒体库刷新未全部成功，将稍后重试")
            return
        db.acknowledge_strm_metadata_refresh_paths(durable_paths)
        self._changed_paths = [path for path in self._changed_paths if path not in paths]
        self._refresh_retry_pending = False


_worker = STRMMetadataWorker()


def get_strm_metadata_worker() -> STRMMetadataWorker:
    return _worker
