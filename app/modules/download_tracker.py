"""统一下载任务状态跟踪与光鸭自动入库联动。"""
from __future__ import annotations

import hashlib
import logging
import json
import threading
from datetime import datetime, timedelta

from app import database as db
from app.clients.guangya import GuangYaClient
from app.clients.qbittorrent import QBittorrentClient, is_qb_torrent_complete
from app.config import get
from app.logger import get_logger, log_throttled
from app.modules.organize import OrganizeRules
from app.modules.organize_tasks import get_organize_manager
from app.notifier import NotificationEvent, send_event

# 保留模块级 ``send`` 名称，兼容既有测试/补丁；实际发送仍走结构化 send_event。
send = send_event

logger = get_logger(__name__)

_COMPLETE_STATES = {"completed", "complete", "success", "succeeded", "finished", "done", 1, 2, 3}
_FAILED_STATES = {"failed", "error", "cancelled", "canceled", "invalid", -1}
_QB_FAILED_STATES = {"error", "missingfiles"}
_TRACKER_CURSOR_KEY = "download_tracker.active_cursor_id"
_DEFAULT_MISSING_GRACE_SECONDS = 900
_MAX_LOCAL_IMPORT_PROBE_ATTEMPTS = 8


class DownloadTracker:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
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
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="download-tracker", daemon=True)
        self._thread.start()
        logger.info("下载任务跟踪器已启动")

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

    def run_once(self) -> int:
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
        if qb_status in {"submitted", "downloading", "outcome_unknown"}:
            task = self._match_qb(row, qb_tasks) if qb_available else None
            matched_qb_task = task
            if task:
                progress = max(0.0, min(float(task.progress or 0), 1.0))
                state = str(task.state or "").strip().lower()
                updates["qb_task_id"] = task.hash
                updates["qb_task_missing_since"] = None
                if state in _QB_FAILED_STATES:
                    updates["qb_status"] = "failed"
                else:
                    updates["qb_status"] = (
                        "completed"
                        if is_qb_torrent_complete(state, progress)
                        else "downloading"
                    )
                self._update_backend_log(request_id, "qb", updates["qb_status"], progress, task.hash)
            elif qb_available and str(self._row_value(row, "qb_task_id", "") or ""):
                missing_since = self._row_value(row, "qb_task_missing_since", "")
                if not missing_since:
                    updates["qb_task_missing_since"] = db.now()
                elif self._missing_expired(missing_since):
                    updates["qb_status"] = "manual_review"
                    updates["error"] = "qB 后端任务长时间未找到，请核对下载器后人工处理"
            elif qb_status == "outcome_unknown" and qb_available:
                updates["qb_status"] = "manual_review"
                updates["error"] = "qB 提交结果未知且无法提取任务标识，请人工核对下载器"
            elif qb_available and self._qb_submission_has_no_stable_identity(row):
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
        if effective_qb == "completed" and matched_qb_task is None and qb_available:
            matched_qb_task = self._match_qb(row, qb_tasks)
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

    def _start_local_import(self, row, task) -> None:
        # 新来源配置优先走持久化调度器；此处只上报完成事件，不执行文件写入。
        from app.modules.local_media_scheduler import (
            LocalMediaProbeRetryable,
            LocalMediaSourceMigrationRequired,
            get_local_media_scheduler,
        )

        scheduler = get_local_media_scheduler()
        try:
            local_task_id = scheduler.enqueue_completed_torrent(task, wake=False)
        except LocalMediaSourceMigrationRequired as exc:
            self._record_local_import_configuration_failure(row, task, exc)
            return
        except LocalMediaProbeRetryable as exc:
            self._record_local_import_probe_retry(row, task, exc)
            return
        if local_task_id is not None:
            linked = db.link_download_request_to_local_media_task(
                int(row["id"]), local_task_id, str(getattr(task, "content_path", "") or ""),
            )
            if linked:
                scheduler.reload()
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
            send(
                NotificationEvent(
                    "⏳ 下载完成，等待云端文件落稳",
                    fields=(("任务", self._row_value(row, "title", "") or "未命名任务"), ("状态", error)),
                    footer="文件落稳后会自动进入串行整理，无需重复提交。",
                    layout="relaxed",
                ),
                chat_id=str(DownloadTracker._row_value(row, "chat_id", "") or "") or None,
            )

    @staticmethod
    def _fail_settle(row, message: str) -> None:
        request_id = int(row["id"])
        db.update_download_request(
            request_id, organize_started=-1, organize_status="failed",
            organize_error=message, organize_finished_at=db.now(), organize_next_retry_at=None,
            strm_status="skipped", strm_error=message, strm_finished_at=db.now(), error=message,
        )
        logger.warning("光鸭下载落稳检查失败 request=%s detail=%s", request_id, message)
        send(
            NotificationEvent(
                "⚠️ 自动入库需要人工核验",
                fields=(("任务", DownloadTracker._row_value(row, "title", "") or "未命名任务"), ("原因", message)),
                footer="已停止自动整理与后续 STRM 同步，请在下载任务的待处理列表中核验。",
                layout="relaxed",
            ),
            chat_id=str(DownloadTracker._row_value(row, "chat_id", "") or "") or None,
        )

    def _start_organize(self, row) -> None:
        source_id = str(row["gy_target_dir"] or get("OFFLINE_TARGET_DIR", "0") or "0")
        target_id = get("GY_ORGANIZE_TARGET_DIR", "").strip()
        if source_id in {"", "0"} or not target_id:
            message = "下载完成，但未配置有效的光鸭整理源/目标目录"
            db.update_download_request(
                int(row["id"]), organize_started=-1,
                organize_status="skipped", organize_error=message,
                organize_finished_at=db.now(), strm_status="skipped",
                strm_error=message, strm_finished_at=db.now(), error=message,
            )
            send(
                NotificationEvent(
                    "⚠️ 自动入库未启动",
                    fields=(("任务", DownloadTracker._row_value(row, "title", "") or "未命名任务"), ("原因", message)),
                ),
                chat_id=str(row["chat_id"] or "") or None,
            )
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
            send(
                NotificationEvent(
                    "🚀 下载完成，自动整理已启动",
                    fields=(("任务", row["title"] or "未命名任务"),),
                    footer="整理完成后将自动生成 STRM 并刷新媒体库。",
                    layout="relaxed",
                ),
                chat_id=str(row["chat_id"] or "") or None,
            )
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
                    send(
                        NotificationEvent(
                            "⏳ 下载完成，整理已排队",
                            fields=(("任务", row["title"] or "未命名任务"),),
                            footer="当前整理完成后将自动处理本任务，不会并行扫描云盘。",
                            layout="relaxed",
                        ),
                        chat_id=str(row["chat_id"] or "") or None,
                    )
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
    def _notify_completion(row, qb_status: str, gy_status: str, updates: dict) -> None:
        if updates.get("status") not in {"completed", "failed"}:
            return
        old_status = str(row["status"] or "")
        if old_status in {"completed", "failed"}:
            return
        fields = [("任务", row["title"] or "未命名任务")]
        if qb_status:
            fields.append(("qBittorrent", DownloadTracker._label(qb_status)))
        if gy_status:
            fields.append(("光鸭云盘", DownloadTracker._label(gy_status)))
        send(
            NotificationEvent("📥 下载任务状态更新", fields=tuple(fields)),
            chat_id=str(row["chat_id"] or "") or None,
        )

    @staticmethod
    def _label(status: str) -> str:
        return {
            "submitted": "已提交", "downloading": "下载中",
            "completed": "已完成", "failed": "失败",
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
        try:
            if not get("QB_URL", "").strip():
                return False, []
            return True, QBittorrentClient(
                url=get("QB_URL"), username=get("QB_USERNAME"),
                password=get("QB_PASSWORD"), api_key=get("QB_API_KEY"),
            ).list_torrents()
        except Exception as exc:
            log_throttled(
                logger, logging.WARNING, f"download-tracker-qb:{type(exc).__name__}",
                "下载跟踪读取 qB 失败 type=%s", type(exc).__name__,
            )
            return False, []

    @staticmethod
    def _gy_tasks() -> tuple[bool, list[dict]]:
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
