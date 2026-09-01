"""统一下载任务状态跟踪与光鸭自动入库联动。"""
from __future__ import annotations

import hashlib
import logging
import json
import threading
import time
from datetime import datetime, timedelta

from app import database as db
from app.clients.guangya import GuangYaClient, close_guangya_client
from app.clients.qbittorrent import (
    QBittorrentClient,
    TorrentTask,
    close_qbittorrent_client,
    is_qb_torrent_complete,
)
from app.config import get
from app.defaults import (
    DEFAULT_DOWNLOAD_TORRENT_RETENTION_DAYS,
    MAX_DOWNLOAD_TORRENT_RETENTION_DAYS,
)
from app.logger import get_logger, log_throttled
from app.modules.naming import sanitize_name
from app.modules.organize import OrganizeRules
from app.modules.organize_tasks import get_organize_manager

logger = get_logger(__name__)

_COMPLETE_STATES = {"completed", "complete", "success", "succeeded", "finished", "done", 1, 2, 3}
_FAILED_STATES = {"failed", "error", "cancelled", "canceled", "invalid", -1}
_QB_FAILED_STATES = {"error", "missingfiles"}
_TRACKER_CURSOR_KEY = "download_tracker.active_cursor_id"
_DEFAULT_MISSING_GRACE_SECONDS = 900
_MAX_LOCAL_IMPORT_PROBE_ATTEMPTS = 8
_NOTIFICATION_LEASE_SECONDS = 300
_TORRENT_DATA_CLEANUP_INTERVAL_SECONDS = 3600
_TORRENT_DATA_CLEANUP_BATCH_SIZE = 500
_STALE_SUBMISSION_MINUTES = 15


class DownloadTracker:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._stopping = False
        self._lifecycle_generation = 0
        self._last_torrent_data_cleanup_at = 0.0

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._stopping or (self._thread and self._thread.is_alive()):
                return
            generation = self._lifecycle_generation
        try:
            projected, released = db.reconcile_startup_media_download_admissions(
                stale_seconds=self._missing_grace_seconds()
            )
            if projected or released:
                logger.info(
                    "启动恢复下载准入：投影 %s 条，释放 %s 条可重试记录",
                    projected,
                    released,
                )
        except Exception:
            # 恢复失败不阻断主服务；后续订阅巡检仍会继续按请求状态对账。
            logger.exception("启动恢复下载准入失败")
        with self._lifecycle_lock:
            if (
                self._stopping
                or generation != self._lifecycle_generation
                or (self._thread and self._thread.is_alive())
            ):
                return
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._loop, name="download-tracker", daemon=True
            )
            self._thread = thread
            thread.start()
        logger.info("下载任务跟踪器已启动")

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lifecycle_lock:
            self._stopping = True
            self._lifecycle_generation += 1
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        stopped = not thread or not thread.is_alive()
        with self._lifecycle_lock:
            if self._thread is thread and stopped:
                self._thread = None
            self._stopping = False
        if not stopped:
            logger.warning("下载任务跟踪器未能在关闭超时内结束")
        return stopped

    def reload(self, *, reset_torrent_cleanup: bool = False) -> None:
        if reset_torrent_cleanup:
            with self._lifecycle_lock:
                self._last_torrent_data_cleanup_at = 0.0
        self._wake_event.set()

    def run_once(self) -> int:
        with self._run_lock:
            return self._run_once_locked()

    def _run_once_locked(self) -> int:
        self._run_torrent_data_cleanup_if_due()
        try:
            recovered = db.recover_stale_submitting_download_requests(
                stale_minutes=_STALE_SUBMISSION_MINUTES,
            )
        except Exception as exc:
            log_throttled(
                logger,
                logging.WARNING,
                f"download-tracker-stale-submitting:{type(exc).__name__}",
                "恢复超时下载提交状态失败 type=%s",
                type(exc).__name__,
                interval_seconds=300.0,
            )
        else:
            if recovered:
                logger.warning("已将 %s 条超时下载提交转为人工核验", recovered)
        local_media_enabled = bool(db.list_local_media_sources(owner="admin", enabled_only=True))
        try:
            cursor = max(0, int(db.kv_get(_TRACKER_CURSOR_KEY, "0") or 0))
        except (TypeError, ValueError):
            cursor = 0
        rows = db.list_active_download_requests(
            include_local_import=local_media_enabled, after_id=cursor, wrap=True,
        )
        if not rows:
            return 0
        qb_needed = any(
            str(row["qb_status"] or "") in {"submitted", "downloading", "completed", "outcome_unknown"}
            for row in rows
        )
        gy_needed = any(
            str(row["gy_status"] or "") in {"submitted", "downloading", "outcome_unknown"}
            for row in rows
        )
        qb_available, qb_tasks = self._qb_tasks() if qb_needed else (False, [])
        gy_available, gy_tasks = self._gy_tasks() if gy_needed else (False, [])
        for row in rows:
            self._update_request(
                row, qb_tasks, gy_tasks,
                qb_available=qb_available, gy_available=gy_available,
            )
        db.kv_set(_TRACKER_CURSOR_KEY, str(int(rows[-1]["id"])))
        return len(rows)

    @staticmethod
    def _torrent_data_retention_days() -> int:
        raw = get(
            "DOWNLOAD_TORRENT_RETENTION_DAYS",
            str(DEFAULT_DOWNLOAD_TORRENT_RETENTION_DAYS),
        )
        try:
            days = int(str(raw or "").strip() or DEFAULT_DOWNLOAD_TORRENT_RETENTION_DAYS)
        except (TypeError, ValueError):
            return DEFAULT_DOWNLOAD_TORRENT_RETENTION_DAYS
        if days < 0 or days > MAX_DOWNLOAD_TORRENT_RETENTION_DAYS:
            return DEFAULT_DOWNLOAD_TORRENT_RETENTION_DAYS
        return days

    def _run_torrent_data_cleanup_if_due(self) -> int:
        current = time.monotonic()
        if (
            self._last_torrent_data_cleanup_at
            and current - self._last_torrent_data_cleanup_at
            < _TORRENT_DATA_CLEANUP_INTERVAL_SECONDS
        ):
            return 0
        retention_days = self._torrent_data_retention_days()
        if retention_days <= 0:
            self._last_torrent_data_cleanup_at = current
            return 0
        try:
            cleared = db.purge_expired_download_request_torrent_data(
                retention_days,
                limit=_TORRENT_DATA_CLEANUP_BATCH_SIZE,
            )
        except Exception as exc:
            self._last_torrent_data_cleanup_at = current
            log_throttled(
                logger,
                logging.WARNING,
                f"torrent-data-retention:{type(exc).__name__}",
                "原始种子保留期清理失败 type=%s",
                type(exc).__name__,
            )
            return 0
        # 满批时让下一轮继续处理积压；不足一批则按小时节流。
        self._last_torrent_data_cleanup_at = (
            0.0 if cleared >= _TORRENT_DATA_CLEANUP_BATCH_SIZE else current
        )
        if cleared:
            logger.info(
                "已清理 %s 条超过 %s 天的原始种子数据，下载请求与日志仍保留",
                cleared,
                retention_days,
            )
        return cleared

    @staticmethod
    def _missing_grace_seconds() -> int:
        try:
            return max(60, int(get("DOWNLOAD_TRACKER_MISSING_GRACE_SECONDS", str(_DEFAULT_MISSING_GRACE_SECONDS))))
        except (TypeError, ValueError):
            return _DEFAULT_MISSING_GRACE_SECONDS

    @classmethod
    def _missing_expired(cls, value: object) -> bool:
        raw = str(value or "").strip()
        if not raw:
            return False
        try:
            started = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return True
        return datetime.now() - started >= timedelta(seconds=cls._missing_grace_seconds())

    def _update_request(
        self, row, qb_tasks: list, gy_tasks: list[dict], *,
        qb_available: bool = True, gy_available: bool = True,
    ) -> None:
        request_id = int(row["id"])
        updates = {}
        qb_status = str(row["qb_status"] or "")
        gy_status = str(row["gy_status"] or "")

        matched_qb_task = None
        local_import_pending = str(
            self._row_value(row, "local_import_status", "") or ""
        ) in {"", "pending"}
        tracking_completed_qb = qb_status == "completed" and local_import_pending
        if qb_status in {"submitted", "downloading", "outcome_unknown"} or tracking_completed_qb:
            task = self._match_qb(row, qb_tasks) if qb_available else None
            matched_qb_task = task
            if task:
                progress = max(0.0, min(float(task.progress or 0), 1.0))
                state = str(task.state or "").strip().lower()
                updates["qb_task_id"] = task.hash
                updates["qb_task_missing_since"] = None
                if not tracking_completed_qb:
                    if state in _QB_FAILED_STATES:
                        updates["qb_status"] = "failed"
                    else:
                        updates["qb_status"] = (
                            "completed"
                            if is_qb_torrent_complete(state, progress)
                            else "downloading"
                        )
                    self._update_backend_log(
                        request_id, "qb", updates["qb_status"], progress, task.hash
                    )
            elif (
                not tracking_completed_qb
                and qb_available
                and str(self._row_value(row, "qb_task_id", "") or "")
            ):
                missing_since = self._row_value(row, "qb_task_missing_since", "")
                if not missing_since:
                    updates["qb_task_missing_since"] = db.now()
                elif self._missing_expired(missing_since):
                    updates["qb_status"] = "manual_review"
                    updates["error"] = "qB 后端任务长时间未找到，请核对下载器后人工处理"
            elif not tracking_completed_qb and qb_status == "outcome_unknown" and qb_available:
                updates["qb_status"] = "manual_review"
                updates["error"] = "qB 提交结果未知且无法提取任务标识，请人工核对下载器"
            elif (
                not tracking_completed_qb
                and qb_available
                and self._qb_submission_has_no_stable_identity(row)
            ):
                updates["qb_status"] = "manual_review"
                updates["error"] = (
                    "qB 已接收直链任务，但当前下载器接口未返回可跟踪任务标识；"
                    "请在下载器中核对，勿直接重复提交"
                )

        if gy_status in {"submitted", "downloading", "outcome_unknown"}:
            task_ids = self._parse_gy_task_ids(row)
            if task_ids and gy_available:
                task_by_id = {str(task.get("id") or ""): task for task in gy_tasks}
                matched = [task_by_id[task_id] for task_id in task_ids if task_id in task_by_id]
                expected_batches = max(len(task_ids), int(self._row_value(row, "gy_batch_count", 0) or 0))
                states = [self._gy_task_state(task) for task in matched]
                progress_values = [
                    max(0.0, min(float(task.get("progress") or 0), 1.0)) for task in matched
                ]
                progress = sum(progress_values) / expected_batches if expected_batches else 0.0
                if expected_batches > len(task_ids):
                    updates["gy_status"] = "manual_review"
                    updates["error"] = (
                        "光鸭仅返回部分任务 ID，部分提交可能已经生效；"
                        "请核对云端任务，勿直接重复提交"
                    )
                elif any(state == "failed" for state in states):
                    updates["gy_status"] = "failed"
                    updates["error"] = "光鸭分批下载存在失败任务，已停止自动整理，请人工核验"
                elif len(matched) == expected_batches and states and all(state == "completed" for state in states):
                    updates["gy_status"] = "completed"
                    updates["gy_task_missing_since"] = None
                elif len(matched) < expected_batches:
                    missing_since = self._row_value(row, "gy_task_missing_since", "")
                    if not missing_since:
                        updates["gy_task_missing_since"] = db.now()
                    elif self._missing_expired(missing_since):
                        updates["gy_status"] = "manual_review"
                        updates["error"] = "光鸭后端任务长时间未找到，请核对云端任务后人工处理"
                    else:
                        updates["gy_status"] = "downloading"
                else:
                    updates["gy_status"] = "downloading"
                    updates["gy_task_missing_since"] = None
                self._update_backend_log(
                    request_id, "guangya", updates.get("gy_status", gy_status), progress, task_ids[0],
                )
            elif not task_ids and gy_available:
                task = self._match_gy(row, gy_tasks)
                if task:
                    progress = max(0.0, min(float(task.get("progress") or 0), 1.0))
                    updates["gy_status"] = self._gy_task_state(task)
                    updates["gy_task_missing_since"] = None
                    if task.get("id"):
                        matched_task_id = str(task["id"])
                        updates["gy_task_id"] = matched_task_id
                        if int(self._row_value(row, "gy_isolated", 0) or 0):
                            updates["gy_task_ids"] = json.dumps([matched_task_id], ensure_ascii=False)
                            updates["gy_batch_count"] = 1
                    self._update_backend_log(
                        request_id, "guangya", updates["gy_status"], progress,
                        str(task.get("id") or self._row_value(row, "gy_task_id", "") or ""),
                    )
                elif gy_status == "outcome_unknown":
                    updates["gy_status"] = "manual_review"
                    updates["error"] = (
                        "光鸭提交结果未知且无法匹配云端任务；"
                        "请人工核对后再决定是否重试"
                    )
                else:
                    # 提交时没有拿到任务 ID，且云端任务列表中也匹配不到
                    # （可能已快速完成并从列表消失）。与批量路径对称：先记
                    # 缺失时间，宽限期后升级人工核验，禁止永远停在 submitted。
                    missing_since = self._row_value(row, "gy_task_missing_since", "")
                    if not missing_since:
                        updates["gy_task_missing_since"] = db.now()
                    elif self._missing_expired(missing_since):
                        updates["gy_status"] = "manual_review"
                        updates["error"] = (
                            "光鸭云端任务长时间无法匹配，可能已完成或提交失败；"
                            "请核对云端文件后人工处理"
                        )

        effective_qb = updates.get("qb_status", qb_status)
        effective_gy = updates.get("gy_status", gy_status)
        if effective_qb == "completed" and matched_qb_task is None:
            if qb_available:
                matched_qb_task = self._match_qb(row, qb_tasks)
            if matched_qb_task is None:
                matched_qb_task = self._persisted_qb_import_task(row)
        statuses = [status for status in (effective_qb, effective_gy) if status]
        # successor 只接管用户明确重提的后端；旧请求仍需跟踪另一个活动后端，
        # 但不得再把审计根状态从 resubmitted 改回 downloading/completed。
        try:
            root_status = str(row["status"] or "")
        except (KeyError, IndexError):
            root_status = ""
        if root_status != "resubmitted":
            if any(status == "manual_review" for status in statuses):
                updates["status"] = "manual_review"
                updates["completed_at"] = db.now()
            elif statuses and all(status in {"completed", "failed"} for status in statuses):
                updates["status"] = (
                    "completed" if any(status == "completed" for status in statuses) else "failed"
                )
                updates["completed_at"] = db.now()
            elif any(status in {"downloading", "completed", "outcome_unknown"} for status in statuses):
                updates["status"] = "downloading"
        next_root_status = str(updates.get("status") or "")
        if (
            next_root_status in {"completed", "failed", "manual_review"}
            and root_status not in {"completed", "failed", "manual_review"}
        ):
            notification_payload = {
                "title": str(self._row_value(row, "title", "") or "未命名任务"),
                "event_status": next_root_status,
                "qb_status": str(effective_qb or ""),
                "gy_status": str(effective_gy or ""),
                "chat_id": str(self._row_value(row, "chat_id", "") or ""),
            }
            updates.update({
                "notification_event_status": next_root_status,
                "notification_delivery_status": "pending",
                "notification_attempts": 0,
                "notification_next_retry_at": db.now(),
                "notification_sent_at": None,
                "notification_payload_json": json.dumps(
                    notification_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            })
        if updates:
            db.update_download_request_and_sync_media_admission(request_id, **updates)

        if effective_qb == "completed" and matched_qb_task is not None:
            self._start_local_import(row, matched_qb_task)
        if (
            effective_gy == "completed"
            and not int(row["organize_started"] or 0)
            and self._organize_retry_due(row)
        ):
            if self._staging_ready_for_organize(row):
                self._start_organize(row)
        self._notify_completion(row, effective_qb, effective_gy, updates)


    @classmethod
    def _persisted_qb_import_task(cls, row) -> TorrentTask | None:
        """qB 自动删种后，继续用已持久化的完成路径重试本地入库。"""
        content_path = str(cls._row_value(row, "qb_content_path", "") or "").strip()
        if not content_path:
            return None
        return TorrentTask(
            hash=str(cls._row_value(row, "qb_task_id", "") or ""),
            name=str(cls._row_value(row, "title", "") or ""),
            progress=1.0,
            state="completed",
            save_path="",
            content_path=content_path,
            size=0,
            downloaded=0,
            dlspeed=0,
            upspeed=0,
            eta=0,
            ratio=0.0,
            category="",
            added_on=0,
        )

    @staticmethod
    def _linked_local_media_task_id(row) -> int | None:
        target = str(DownloadTracker._row_value(row, "local_import_target", "") or "")
        prefix = "local-media-task:"
        if not target.startswith(prefix):
            return None
        try:
            task_id = int(target.removeprefix(prefix))
        except (TypeError, ValueError):
            return None
        return task_id if task_id > 0 else None

    @staticmethod
    def _reconcile_linked_local_media_task(local_task) -> None:
        """已关联请求只跟随任务状态，不再次探测已被移动的下载路径。"""
        status = str(getattr(local_task, "status", "") or "")
        if status in {"completed", "failed", "requires_manual"}:
            db.update_download_request_for_local_media_task(
                int(local_task.id),
                status,
                error=str(getattr(local_task, "error", "") or ""),
            )

    def _start_local_import(self, row, task) -> None:
        # 新来源配置优先走持久化调度器；此处只上报完成事件，不执行文件写入。
        from app.modules.local_media_scheduler import (
            LocalMediaProbeRetryable,
            LocalMediaSourceMigrationRequired,
            get_local_media_scheduler,
        )

        scheduler = get_local_media_scheduler()
        linked_task_id = self._linked_local_media_task_id(row)
        if linked_task_id is not None:
            linked_task = db.get_local_media_task(linked_task_id, owner="admin")
            if linked_task is None:
                self._record_local_import_configuration_failure(
                    row, task, RuntimeError("已关联的本地整理任务不存在")
                )
                return
            self._reconcile_linked_local_media_task(linked_task)
            if str(linked_task.status) not in {"completed", "failed", "requires_manual"}:
                scheduler.reload()
            return

        try:
            local_task_id = scheduler.enqueue_completed_torrent(
                task, wake=False, request_id=int(row["id"]),
            )
        except LocalMediaSourceMigrationRequired as exc:
            self._record_local_import_configuration_failure(row, task, exc)
            return
        except LocalMediaProbeRetryable as exc:
            self._record_local_import_probe_retry(row, task, exc)
            return
        except (LookupError, ValueError) as exc:
            # 原子绑定可能因请求已进入终态、来源被删除或历史路径冲突而拒绝。
            # 条件更新会保护并发终态；仍处于 pending 时则给出可见失败。
            self._record_local_import_configuration_failure(row, task, exc)
            return
        if local_task_id is not None:
            scheduler.reload()
            linked_task = db.get_local_media_task(local_task_id, owner="admin")
            if linked_task is None:
                self._record_local_import_configuration_failure(
                    row, task, RuntimeError("新建的本地整理任务不存在")
                )
                return
            self._reconcile_linked_local_media_task(linked_task)
            return
        content_path = str(getattr(task, "content_path", "") or "")
        db.mark_download_request_local_media_skipped(
            int(row["id"]), content_path, "未命中可整理的本地媒体内容"
        )
        logger.info(
            "qB 完成任务未命中可整理的本地媒体内容 request=%s path=%s",
            int(row["id"]), content_path,
        )

    def _record_local_import_configuration_failure(self, row, task, error: Exception) -> None:
        request_id = int(row["id"])
        message = str(error or "本地媒体来源配置无效")[:1000]
        updated = db.mark_download_request_local_media_failed(
            request_id,
            str(getattr(task, "content_path", "") or ""),
            message,
        )
        if updated:
            logger.warning(
                "qB 本地媒体来源需要迁移 request=%s error=%s", request_id, message,
            )

    def _record_local_import_probe_retry(self, row, task, error: Exception) -> None:
        request_id = int(row["id"])
        attempts = int(self._row_value(row, "local_import_attempts", 0) or 0) + 1
        exhausted = attempts >= _MAX_LOCAL_IMPORT_PROBE_ATTEMPTS
        timestamp = db.now()
        message = str(error or "本地媒体路径暂时不可读")[:1000]
        db.update_download_request(
            request_id,
            qb_content_path=str(getattr(task, "content_path", "") or ""),
            local_import_status="failed" if exhausted else "pending",
            local_import_attempts=attempts,
            local_import_error=message,
            local_import_started_at=self._row_value(row, "local_import_started_at", "") or timestamp,
            local_import_completed_at=timestamp if exhausted else None,
        )
        if exhausted:
            logger.error(
                "qB 本地媒体路径连续 %s 次不可读，停止自动重试 request=%s error=%s",
                attempts, request_id, message,
            )
        else:
            logger.warning(
                "qB 本地媒体路径暂时不可读，保留 pending 等待重试 request=%s attempt=%s/%s error=%s",
                request_id, attempts, _MAX_LOCAL_IMPORT_PROBE_ATTEMPTS, message,
            )

    @staticmethod
    def _settle_delay(attempt: int) -> int:
        delays = (30, 30, 30, 60, 120, 300, 600, 900)
        return delays[min(max(1, int(attempt)) - 1, len(delays) - 1)]

    def _staging_ready_for_organize(self, row) -> bool:
        """等待隔离目录中的异步云端写入落稳，避免只整理首批可见文件。"""
        if not int(self._row_value(row, "gy_isolated", 0) or 0):
            return True
        request_id = int(row["id"])
        staging_id = str(self._row_value(row, "gy_target_dir", "") or "")
        if not staging_id:
            self._fail_settle(row, "下载隔离目录缺失，无法确认文件是否落稳")
            return False
        client = None
        try:
            client = GuangYaClient()
            if not client.logged_in:
                raise RuntimeError("光鸭未登录")
            info = client.file_info(staging_id)
            expected_parent = str(self._row_value(row, "gy_staging_parent_dir", "") or "")
            expected_name = str(self._row_value(row, "gy_staging_name", "") or "")
            if (
                info is None or not bool(info.is_dir)
                or (expected_parent and str(info.parent_id or "") != expected_parent)
                or (expected_name and str(info.name or "") != expected_name)
            ):
                self._fail_settle(row, "下载隔离目录身份已变化，已停止自动整理")
                return False
            count, snapshot = self._scan_staging_snapshot(client, staging_id)
        except Exception as exc:
            self._schedule_settle(row, observed=0, snapshot="", error=f"读取隔离目录失败: {type(exc).__name__}")
            return False
        finally:
            close_guangya_client(client)

        expected = max(0, int(self._row_value(row, "gy_expected_file_count", 0) or 0))
        previous = str(self._row_value(row, "gy_settle_snapshot", "") or "")
        stable_count = int(self._row_value(row, "gy_settle_stable_count", 0) or 0)
        stable_count = stable_count + 1 if snapshot and snapshot == previous else (1 if snapshot else 0)
        # 仅“数量达到预期”仍不足以证明云端写入完成：光鸭可能先暴露目录项，
        # 随后继续更新大小/etag。统一要求两次快照一致，避免整理到半写入文件。
        ready = bool(count and stable_count >= 2 and (expected <= 0 or count >= expected))
        if ready:
            db.update_download_request(
                request_id, gy_settle_observed_file_count=count, gy_settle_snapshot=snapshot,
                gy_settle_stable_count=stable_count, organize_status="queued",
                organize_next_retry_at=None, organize_error="", error="",
            )
            logger.info(
                "光鸭下载目录已落稳 request=%s observed=%s expected=%s stable=%s",
                request_id, count, expected or "unknown", stable_count,
            )
            return True

        detail = (
            f"等待云端文件落稳：已发现 {count}/{expected} 个文件"
            if expected > 0 else f"等待云端目录稳定：当前 {count} 个文件，连续稳定 {stable_count}/2 次"
        )
        self._schedule_settle(row, observed=count, snapshot=snapshot, error=detail, stable_count=stable_count)
        return False

    @staticmethod
    def _scan_staging_snapshot(client: GuangYaClient, root_id: str) -> tuple[int, str]:
        max_entries = max(100, int(get("DOWNLOAD_GY_SETTLE_MAX_ENTRIES", "20000") or 20000))
        stack = [str(root_id)]
        leaves: list[str] = []
        visited: set[str] = set()
        while stack:
            directory_id = stack.pop()
            if directory_id in visited:
                continue
            visited.add(directory_id)
            for item in client.list_dir(directory_id):
                if len(visited) + len(leaves) >= max_entries:
                    raise RuntimeError("隔离目录项目超过落稳检查上限")
                if item.is_dir:
                    if item.file_id:
                        stack.append(str(item.file_id))
                    continue
                leaves.append(
                    f"{item.file_id}|{item.size}|{item.etag}|{item.updated_at}|{item.name}"
                )
        digest = hashlib.sha256("\n".join(sorted(leaves)).encode("utf-8")).hexdigest() if leaves else ""
        return len(leaves), digest

    def _schedule_settle(
        self, row, *, observed: int, snapshot: str, error: str, stable_count: int = 0,
    ) -> None:
        request_id = int(row["id"])
        attempts = int(self._row_value(row, "gy_settle_attempts", 0) or 0) + 1
        if attempts > 8:
            self._fail_settle(row, f"云端文件长时间未落稳；{error}")
            return
        delay = self._settle_delay(attempts)
        next_retry = (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
        first_notice = str(self._row_value(row, "organize_status", "") or "") != "settling"
        db.update_download_request(
            request_id, organize_started=0, organize_status="settling", organize_error=error,
            organize_next_retry_at=next_retry, gy_settle_attempts=attempts,
            gy_settle_observed_file_count=max(0, int(observed)),
            gy_settle_snapshot=str(snapshot or ""), gy_settle_stable_count=max(0, int(stable_count)),
            # STRM 尚未入队，不能标为 pending；否则服务重启恢复会把它误判为
            # “上次 STRM 排队期间中断”，产生虚假的待处理记录。
            strm_status="", strm_error="", strm_finished_at=None, error="",
        )
        logger.info(
            "光鸭下载完成但文件尚未落稳 request=%s attempt=%s delay=%ss observed=%s detail=%s",
            request_id, attempts, delay, observed, error,
        )
        if first_notice:
            self._publish_lifecycle(request_id)

    @staticmethod
    def _fail_settle(row, message: str) -> None:
        request_id = int(row["id"])
        db.update_download_request(
            request_id, organize_started=-1, organize_status="failed",
            organize_error=message, organize_finished_at=db.now(), organize_next_retry_at=None,
            strm_status="skipped", strm_error=message, strm_finished_at=db.now(), error=message,
        )
        logger.warning("光鸭下载落稳检查失败 request=%s detail=%s", request_id, message)
        DownloadTracker._publish_lifecycle(request_id)

    def _retain_staging_without_organize(self, row, reason: str) -> None:
        """保留无法安全收口的隔离目录，并让待处理页给出明确原因。"""
        message = str(reason or "下载目录需要人工核验")[:500]
        db.update_download_request(
            int(row["id"]),
            organize_started=-1, organize_status="skipped", organize_error="",
            organize_finished_at=db.now(), organize_next_retry_at=None,
            strm_status="skipped", strm_error="",
            strm_finished_at=db.now(), error="",
            gy_staging_cleanup_status="retained", gy_staging_cleanup_error=message,
        )
        logger.warning("光鸭下载暂存目录已保留 request=%s reason=%s", int(row["id"]), message)

    def _finalize_staging_without_organize(self, row) -> bool:
        """通过统一光鸭 Writer 收口未配置自动入库的隔离目录。"""
        if not int(self._row_value(row, "gy_isolated", 0) or 0):
            return False
        request_id = int(row["id"])
        staging_id = str(self._row_value(row, "gy_target_dir", "") or "")
        parent_id = str(self._row_value(row, "gy_staging_parent_dir", "") or "0")
        expected_name = str(self._row_value(row, "gy_staging_name", "") or "")
        if not staging_id or not expected_name:
            self._retain_staging_without_organize(
                row,
                "暂存目录身份信息不完整，未自动移动",
            )
            return True

        if not db.claim_download_request_staging_finalize(
            request_id,
            staging_id=staging_id,
            parent_id=parent_id,
            staging_name=expected_name,
        ):
            logger.info(
                "跳过已失效、未到重试时间或已被认领的暂存目录收口 request=%s",
                request_id,
            )
            return True

        def finalize() -> dict:
            current = db.get_download_request(request_id)
            if current is None:
                return {"ok": True, "stats": {"skipped": 1}}
            if (
                not int(self._row_value(current, "gy_isolated", 0) or 0)
                or str(self._row_value(current, "gy_target_dir", "") or "") != staging_id
                or str(self._row_value(current, "gy_staging_parent_dir", "") or "0") != parent_id
                or str(self._row_value(current, "gy_staging_name", "") or "") != expected_name
            ):
                logger.info(
                    "光鸭下载暂存目录已由其他任务收口 request=%s",
                    request_id,
                )
                return {"ok": True, "stats": {"skipped": 1}}
            client = GuangYaClient()
            try:
                handled = self._finalize_staging_without_organize_with_client(
                    current,
                    client,
                    request_id=request_id,
                    staging_id=staging_id,
                    parent_id=parent_id,
                    expected_name=expected_name,
                )
                return {
                    "ok": bool(handled),
                    "request_id": request_id,
                    "stats": {"finalized": 1 if handled else 0},
                }
            finally:
                close_guangya_client(client)

        result = get_organize_manager().start_operation(
            "收口光鸭下载目录",
            expected_name,
            finalize,
            queue_if_busy=False,
            dedupe_key=f"download-staging-finalize:{request_id}:{staging_id}",
        )
        if not result.get("ok"):
            retry_at = (datetime.now() + timedelta(seconds=5)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            db.update_download_request(
                request_id,
                organize_started=0,
                organize_status="queued",
                organize_error=str(result.get("error") or "等待网盘 Writer")[:500],
                organize_next_retry_at=retry_at,
            )
        return True

    def _finalize_staging_without_organize_with_client(
        self,
        row,
        client: GuangYaClient,
        *,
        request_id: int,
        staging_id: str,
        parent_id: str,
        expected_name: str,
    ) -> bool:
        """使用调用期客户端完成暂存目录收口。"""
        try:
            if not client.logged_in:
                raise RuntimeError("光鸭未登录")
            info = client.file_info(staging_id)
            if (
                info is None or not info.is_dir
                or str(info.parent_id or "0") != parent_id
                or str(info.name or "") != expected_name
            ):
                self._retain_staging_without_organize(row, "暂存目录身份已变化，未自动移动")
                return True
            children = client.list_dir(staging_id)
            if not children:
                self._retain_staging_without_organize(row, "暂存目录为空，未找到可收口的下载内容")
                return True
            parent_entries = client.list_dir(parent_id)
        except Exception as exc:
            self._retain_staging_without_organize(
                row, f"读取暂存目录失败: {type(exc).__name__}"
            )
            return True

        def has_conflict(name: str, *, ignore_id: str = "") -> bool:
            key = str(name or "").strip().casefold()
            return any(
                str(item.file_id or "") != ignore_id
                and str(item.name or "").strip().casefold() == key
                for item in parent_entries
            )

        if len(children) == 1:
            child = children[0]
            if not child.file_id or has_conflict(child.name, ignore_id=staging_id):
                self._retain_staging_without_organize(
                    row, f"目标目录已存在同名内容：{child.name or '未命名对象'}"
                )
                return True
            try:
                client.move([child.file_id], parent_id)
                moved = client.file_info(child.file_id)
                if moved is None or str(moved.parent_id or "0") != parent_id:
                    raise RuntimeError("移动后未能确认目标位置")
                latest = client.file_info(staging_id)
                if (
                    latest is None or not latest.is_dir
                    or str(latest.parent_id or "0") != parent_id
                    or str(latest.name or "") != expected_name
                ):
                    raise RuntimeError("移动后暂存目录身份已变化")
                if not client.supports_guarded_empty_directory_delete:
                    self._retain_staging_without_organize(
                        row, "下载内容已移入目标目录，但 Provider 不支持安全删除空暂存目录"
                    )
                    return True
                from app.modules.organize_delete_audit import (
                    DeleteCandidate,
                    execute_recycle_bin_delete,
                )

                execute_recycle_bin_delete(
                    client,
                    trigger="download_staging_finalize",
                    reason="下载内容已安全提升，清理已复核为空的隔离暂存目录",
                    candidate=DeleteCandidate(
                        file_id=staging_id,
                        name=str(latest.name or expected_name),
                        parent_id=str(latest.parent_id or parent_id),
                        size=max(0, int(latest.size or 0)),
                        gcid=str(latest.etag or ""),
                    ),
                    safe_failure_message="隔离暂存目录清理失败，目录已保留",
                    delete_operation=lambda: client.delete_empty_directory(
                        staging_id,
                        expected_etag=str(latest.etag or ""),
                        expected_updated_at=int(latest.updated_at or 0),
                    ),
                )
            except Exception as exc:
                self._retain_staging_without_organize(
                    row, f"暂存内容收口失败: {type(exc).__name__}"
                )
                return True
            db.update_download_request(
                request_id,
                gy_target_dir=parent_id, gy_target_name=str(
                    self._row_value(row, "gy_staging_parent_name", "")
                    or get("OFFLINE_TARGET_DIR_NAME", "离线目录")
                ),
                gy_isolated=0, gy_staging_cleanup_status="completed",
                gy_staging_cleanup_error="",
                organize_started=-1, organize_status="skipped", organize_error="",
                organize_finished_at=db.now(), organize_next_retry_at=None,
                strm_status="skipped", strm_error="",
                strm_finished_at=db.now(), error="",
            )
            logger.info(
                "光鸭单项下载已移入目标目录 request=%s item=%s",
                request_id, child.file_id,
            )
            return True

        try:
            final_name = sanitize_name(str(self._row_value(row, "title", "") or "离线下载"))[:180]
        except ValueError:
            final_name = f"离线下载-{request_id}"
        if has_conflict(final_name, ignore_id=staging_id):
            self._retain_staging_without_organize(
                row, f"目标目录已存在同名资源目录：{final_name}"
            )
            return True
        try:
            if final_name != expected_name:
                client.rename(staging_id, final_name)
            renamed = client.file_info(staging_id)
            if (
                renamed is None or not renamed.is_dir
                or str(renamed.parent_id or "0") != parent_id
                or str(renamed.name or "") != final_name
            ):
                raise RuntimeError("重命名后未能确认目录身份")
        except Exception as exc:
            self._retain_staging_without_organize(
                row, f"资源目录收口失败: {type(exc).__name__}"
            )
            return True
        db.update_download_request(
            request_id,
            gy_target_dir=staging_id, gy_target_name=final_name,
            gy_isolated=0, gy_staging_name=final_name,
            gy_staging_cleanup_status="completed", gy_staging_cleanup_error="",
            organize_started=-1, organize_status="skipped", organize_error="",
            organize_finished_at=db.now(), organize_next_retry_at=None,
            strm_status="skipped", strm_error="",
            strm_finished_at=db.now(), error="",
        )
        logger.info(
            "光鸭多项下载目录已完成可读化收口 request=%s name=%s",
            request_id, final_name,
        )
        return True

    def _start_organize(self, row) -> None:
        source_id = str(row["gy_target_dir"] or get("OFFLINE_TARGET_DIR", "0") or "0")
        target_id = get("GY_ORGANIZE_TARGET_DIR", "").strip()
        if not target_id and self._finalize_staging_without_organize(row):
            return
        if source_id in {"", "0"} or not target_id:
            message = "下载完成，但未配置有效的光鸭整理源/目标目录"
            db.update_download_request(
                int(row["id"]), organize_started=-1,
                organize_status="skipped", organize_error=message,
                organize_finished_at=db.now(), strm_status="skipped",
                strm_error=message, strm_finished_at=db.now(), error=message,
            )
            self._publish_lifecycle(int(row["id"]))
            return
        if not db.claim_download_request_organize(int(row["id"])):
            logger.info(
                "跳过已失效或已被认领的下载整理请求 request=%s",
                int(row["id"]),
            )
            return
        result = get_organize_manager().start(
            [{"id": source_id, "name": str(row["gy_target_name"] or "Telegram 下载目录")}],
            OrganizeRules.from_config(target_id),
            trigger_type="download",
            download_request_ids=[int(row["id"])],
        )
        if result.get("ok"):
            self._publish_lifecycle(int(row["id"]))
        else:
            error = str(result.get("error") or "整理任务启动失败")
            if "正在运行" in error:
                # 保持 organize_started=0，让下载跟踪器按 request id 顺序继续消费；
                # queued 状态用于抑制重复通知，并明确这是串行等待而非失败。
                already_queued = str(row["organize_status"] or "") == "queued"
                db.update_download_request(
                    int(row["id"]), organize_started=0,
                    organize_status="queued", organize_error="",
                    strm_status="pending", strm_error="",
                )
                if not already_queued:
                    self._publish_lifecycle(int(row["id"]))
            else:
                attempts = int(self._row_value(row, "organize_attempts", 0) or 0) + 1
                permanent = any(marker in error for marker in (
                    "目标目录不能", "无法校验光鸭整理目录", "至少选择一个源目录",
                    "未配置", "无效的光鸭整理",
                ))
                if not permanent and attempts <= 4:
                    delay = (30, 60, 120, 300)[attempts - 1]
                    next_retry = (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
                    db.update_download_request(
                        int(row["id"]), organize_started=0, organize_status="queued",
                        organize_attempts=attempts, organize_next_retry_at=next_retry,
                        organize_error=error, strm_status="pending", strm_error="", error=error,
                    )
                    logger.warning(
                        "下载自动整理启动失败，将重试 request=%s attempt=%s delay=%ss error=%s",
                        int(row["id"]), attempts, delay, error,
                    )
                else:
                    db.update_download_request(
                        int(row["id"]), organize_started=-1, organize_status="failed",
                        organize_attempts=attempts, organize_next_retry_at=None,
                        organize_error=error, organize_finished_at=db.now(),
                        strm_status="skipped", strm_error=error,
                        strm_finished_at=db.now(), error=error,
                    )
                self._publish_lifecycle(int(row["id"]))

    @staticmethod
    def _organize_retry_due(row) -> bool:
        retry_at = str(
            DownloadTracker._row_value(row, "organize_next_retry_at", "") or ""
        ).strip()
        if not retry_at:
            return True
        try:
            return datetime.strptime(retry_at, "%Y-%m-%d %H:%M:%S") <= datetime.now()
        except ValueError:
            # 旧记录或人工修改产生的非法时间不应永久卡死任务；启动成功后
            # OrganizeTaskManager 会清空该字段。
            return True

    @staticmethod
    def _publish_lifecycle(request_id: int) -> bool:
        try:
            from app.modules.telegram_download_lifecycle import (
                publish_download_lifecycle,
            )

            return bool(publish_download_lifecycle(int(request_id)))
        except Exception as exc:
            logger.warning(
                "下载入库事务通知更新异常 request#%s type=%s",
                request_id,
                type(exc).__name__,
            )
            return False

    @staticmethod
    def _notify_completion(row, qb_status: str, gy_status: str, updates: dict) -> None:
        """领取下载终态投递租约，并更新唯一的下载事务消息。"""
        delivery_status = str(
            updates.get("notification_delivery_status")
            or DownloadTracker._row_value(row, "notification_delivery_status", "")
            or ""
        )
        event_status = str(
            updates.get("notification_event_status")
            or DownloadTracker._row_value(row, "notification_event_status", "")
            or updates.get("status")
            or ""
        )
        request_id = int(DownloadTracker._row_value(row, "id", 0) or 0)
        if (
            delivery_status not in {"pending", "retry_wait", "sending"}
            or event_status not in {"completed", "failed", "manual_review"}
            or request_id <= 0
        ):
            return
        claim = db.claim_download_request_notification(
            request_id, lease_seconds=_NOTIFICATION_LEASE_SECONDS,
        )
        if claim is None:
            return
        token = str(claim.get("token") or "")
        accepted = DownloadTracker._publish_lifecycle(request_id)
        attempts = int(claim.get("attempts") or 0) + 1
        delay = min(3600, 30 * (2 ** min(attempts - 1, 6)))
        retry_at = (datetime.now() + timedelta(seconds=delay)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            db.finalize_download_request_notification(
                request_id,
                token,
                delivered=accepted,
                retry_at=None if accepted else retry_at,
            )
        except Exception as exc:
            logger.warning(
                "下载任务旧通知状态提交异常 request#%s type=%s",
                request_id,
                type(exc).__name__,
            )

    @staticmethod
    def _label(status: str) -> str:
        return {
            "submitted": "已提交", "downloading": "下载中",
            "completed": "已完成", "failed": "失败",
            "manual_review": "待人工核对", "outcome_unknown": "结果未知",
        }.get(status, status)

    @staticmethod
    def _update_backend_log(request_id: int, source: str, status: str,
                            progress: float, task_id: str) -> None:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM download_log WHERE request_id=? AND source=? ORDER BY id DESC LIMIT 1",
                (request_id, source),
            ).fetchone()
        if row:
            db.update_download_log(
                int(row["id"]), status="success" if status == "completed" else status,
                progress=progress, backend_task_id=task_id,
                completed_at=db.now() if status in {"completed", "failed"} else None,
            )

    @staticmethod
    def _qb_tasks() -> tuple[bool, list]:
        client = None
        try:
            if not get("QB_URL", "").strip():
                return False, []
            client = QBittorrentClient(
                url=get("QB_URL"), username=get("QB_USERNAME"),
                password=get("QB_PASSWORD"), api_key=get("QB_API_KEY"),
            )
            return True, client.list_torrents()
        except Exception as exc:
            log_throttled(
                logger, logging.WARNING, f"download-tracker-qb:{type(exc).__name__}",
                "下载跟踪读取 qB 失败 type=%s", type(exc).__name__,
            )
            return False, []
        finally:
            close_qbittorrent_client(client)

    @staticmethod
    def _gy_tasks() -> tuple[bool, list[dict]]:
        client = None
        try:
            client = GuangYaClient()
            if not client.logged_in:
                return False, []
            return True, client.list_offline_tasks()
        except Exception as exc:
            log_throttled(
                logger, logging.WARNING, f"download-tracker-guangya:{type(exc).__name__}",
                "下载跟踪读取光鸭失败 type=%s", type(exc).__name__,
            )
            return False, []
        finally:
            close_guangya_client(client)

    @staticmethod
    def _row_value(row, key: str, default=None):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return default

    @classmethod
    def _parse_gy_task_ids(cls, row) -> list[str]:
        raw = cls._row_value(row, "gy_task_ids", "[]")
        try:
            values = json.loads(str(raw or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            values = []
        if not isinstance(values, list):
            return []
        return list(dict.fromkeys(str(item) for item in values if str(item)))

    @staticmethod
    def _gy_task_state(task: dict) -> str:
        progress = max(0.0, min(float(task.get("progress") or 0), 1.0))
        state = task.get("status")
        normalized = str(state).strip().lower()
        if progress >= 1 or state in _COMPLETE_STATES or normalized in _COMPLETE_STATES:
            return "completed"
        if state in _FAILED_STATES or normalized in _FAILED_STATES:
            return "failed"
        return "downloading"

    @classmethod
    def _qb_submission_has_no_stable_identity(cls, row) -> bool:
        kind = str(cls._row_value(row, "kind", "") or "").strip().lower()
        identity = str(cls._row_value(row, "qb_task_id", "") or "").strip()
        return kind in {"http", "ed2k"} and not identity

    @classmethod
    def _match_qb(cls, row, tasks):
        identity = str(cls._row_value(row, "qb_task_id", "") or "").lower()
        title = str(cls._row_value(row, "title", "") or "").strip().lower()
        if identity:
            return next(
                (task for task in tasks if task.hash.lower() == identity),
                None,
            )
        # qB 4.x 对 HTTP/ED2K 成功响应可能没有 hash。标题并非稳定身份，
        # 同名任务会串单，因此这两类请求必须转人工核对。
        if cls._qb_submission_has_no_stable_identity(row):
            return None
        return next(
            (task for task in tasks if title and task.name.strip().lower() == title),
            None,
        )

    @staticmethod
    def _match_gy(row, tasks: list[dict]):
        task_id = str(DownloadTracker._row_value(row, "gy_task_id", "") or "")
        title = str(DownloadTracker._row_value(row, "title", "") or "").strip().lower()
        source_value = str(DownloadTracker._row_value(row, "source_value", "") or "")
        target_dir = str(DownloadTracker._row_value(row, "gy_target_dir", "") or "")
        isolated = bool(int(DownloadTracker._row_value(row, "gy_isolated", 0) or 0))
        if task_id:
            return next(
                (task for task in tasks if str(task.get("id") or "") == task_id),
                None,
            )
        # 自动下载每个请求使用唯一隔离目录；无 task ID 时只允许用该稳定身份认领，
        # 不再回退 URL/标题，避免同磁力或同名任务串单。
        if isolated:
            if not target_dir:
                return None
            target_matches = [
                task for task in tasks
                if str(task.get("target_dir") or "") == target_dir
            ]
            return target_matches[0] if len(target_matches) == 1 else None
        for task in tasks:
            raw = task.get("raw") if isinstance(task.get("raw"), dict) else {}
            raw_url = str(raw.get("url") or raw.get("sourceUrl") or "")
            if source_value and raw_url == source_value:
                return task
        if target_dir:
            target_matches = [
                task for task in tasks
                if str(task.get("target_dir") or "") == target_dir
            ]
            if len(target_matches) == 1:
                return target_matches[0]
        return next(
            (task for task in tasks if title and str(task.get("name") or "").strip().lower() == title),
            None,
        )

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                log_throttled(
                    logger, logging.ERROR, f"download-tracker-loop:{type(exc).__name__}",
                    "下载跟踪检查失败 type=%s", type(exc).__name__,
                )
            self._wake_event.wait(timeout=30)
            self._wake_event.clear()


_tracker = DownloadTracker()


def get_download_tracker() -> DownloadTracker:
    return _tracker
