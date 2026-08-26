"""网盘整理后台任务管理。

Web 与 Telegram 共用进程级单任务锁，支持多源目录、状态查询和协作式停止。
停止只阻止后续扫描/移动，已经完成的云盘操作不会回滚。
"""
from __future__ import annotations

import json
import threading
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Callable

from app import config, database as db
from app.logger import get_logger
from app.modules.organize import OrganizeRules, Organizer
from app.modules.organize_results import build_organize_result, read_organize_result
from app.modules.organize_sources import normalize_organize_sources
from app.modules.organize_delete_audit import DeleteCandidate, execute_recycle_bin_delete
from app.modules.process_lock import CrossProcessLock
from app.repositories.organize_operation_jobs import (
    claim_organize_operation_job,
    count_pending_organize_operation_jobs,
    count_running_organize_operation_jobs,
    enqueue_organize_operation_job,
    fail_pending_organize_operation_job,
    finish_organize_operation_job,
    get_organize_operation_job,
    get_organize_operation_job_for_owner,
    is_organize_operation_cancel_requested,
    list_pending_organize_operation_jobs,
    organize_operation_queue_position,
    organize_operation_job_id_from_public_ref,
    organize_operation_owner_digest,
    recover_orphaned_organize_operation_jobs,
    sanitize_organize_operation_result,
    verify_organize_operation_payload,
    OrganizeOperationCancelled,
    OrganizeOperationQueueFullError,
)
from app.modules.directory_scrape_errors import (
    DirectoryScrapePublicError,
    public_error_message,
)

logger = get_logger(__name__)


def _operation_result_is_partial(result: object) -> bool:
    """识别单次操作结果中的非致命失败，避免把部分完成误报为成功。"""
    if not isinstance(result, dict):
        return False
    if result.get("partial") is True:
        return True
    stats = result.get("stats")
    if not isinstance(stats, dict):
        return False
    for key in (
        "failed",
        "replacement_cleanup_failed",
        "empty_dir_cleanup_failed",
        "source_dir_cleanup_failed",
        "audit_failures",
        "stopped",
    ):
        value = stats.get(key)
        if isinstance(value, (list, tuple, set, dict)):
            if value:
                return True
            continue
        try:
            if int(value or 0) > 0:
                return True
        except (TypeError, ValueError):
            if value:
                return True
    scan_errors = stats.get("scan_errors")
    return bool(scan_errors)


def _credential_snapshot_is_current(client: Any, expected_generation: int | None) -> bool:
    """确认后台线程仍使用提交时的同一凭据世代。"""
    if client is None or expected_generation is None:
        return True
    try:
        generation = int(getattr(client, "credential_generation", -1))
    except (TypeError, ValueError):
        return False
    try:
        logged_in = bool(getattr(client, "logged_in", False))
    except Exception:
        return False
    return logged_in and generation == int(expected_generation)


_SOURCE_ROOT_CLEANUP_UNSAFE_STATS = (
    "failed",
    "scan_errors",
    "stopped",
    "replacement_cleanup_failed",
    "audit_failures",
)


def _protected_organize_root_ids(rules: OrganizeRules) -> tuple[set[str], str]:
    """读取永久整理根保护集；配置异常时返回错误并要求调用方 fail-closed。"""
    sources, error = normalize_organize_sources(
        config.get("GY_ORGANIZE_SOURCE_DIRS", "")
    )
    protected = {"0", str(rules.target_dir_id or "").strip()}
    protected.update(str(item.get("id") or "").strip() for item in sources)
    protected.discard("")
    return protected, error


def _cleanup_manual_source_root(
    organizer: Organizer,
    source_id: str,
    source_name: str,
    rules: OrganizeRules,
    stats: dict[str, Any],
    *,
    trigger_type: str,
) -> None:
    """安全清理手动选择的临时空源根，永久配置根和归档根永不删除。"""
    stats.setdefault("source_dir_cleaned", 0)
    if trigger_type != "manual" or not rules.clean_empty:
        return
    if any(stats.get(key) for key in _SOURCE_ROOT_CLEANUP_UNSAFE_STATS):
        stats["source_dir_cleanup_skipped"] = int(
            stats.get("source_dir_cleanup_skipped", 0) or 0
        ) + 1
        Organizer._append_reason(
            stats,
            "empty_dir_cleanup_reasons",
            "本次整理存在待确认、失败或扫描异常，已保留手动来源目录",
            limit=6,
        )
        return

    source_id = str(source_id or "").strip()
    protected, error = _protected_organize_root_ids(rules)
    if error:
        stats["source_dir_cleanup_skipped"] = int(
            stats.get("source_dir_cleanup_skipped", 0) or 0
        ) + 1
        Organizer._append_reason(
            stats,
            "empty_dir_cleanup_reasons",
            "整理来源配置无法安全解析，已保留手动来源目录",
            limit=6,
        )
        logger.warning("永久整理源配置无效，已保留手动整理源目录: %s", error)
        return
    if not source_id:
        stats["source_dir_cleanup_skipped"] = int(
            stats.get("source_dir_cleanup_skipped", 0) or 0
        ) + 1
        Organizer._append_reason(
            stats,
            "empty_dir_cleanup_reasons",
            "来源目录标识缺失，已跳过空目录清理",
            limit=6,
        )
        return
    if source_id in protected:
        stats["source_dir_cleanup_protected"] = int(
            stats.get("source_dir_cleanup_protected", 0) or 0
        ) + 1
        Organizer._append_reason(
            stats,
            "empty_dir_cleanup_reasons",
            "配置的来源根目录按安全策略保留，仅清理其中的空子目录",
            limit=6,
        )
        return

    cleanup = getattr(organizer, "_clean_empty_dirs_report", None)
    if not callable(cleanup):
        return
    report = cleanup(
        [(source_id, 0, "", 0)],
        protected_source_ids=protected,
    )
    if not isinstance(report, dict):
        return
    cleaned = int(report.get("cleaned", 0) or 0)
    failures = int(report.get("delete_failures", 0) or 0)
    unsupported = int(report.get("unsupported", 0) or 0)
    for report_key, stats_key in (
        ("protected", "source_dir_cleanup_protected"),
        ("not_empty", "source_dir_cleanup_not_empty"),
        ("unavailable", "source_dir_cleanup_unavailable"),
    ):
        value = int(report.get(report_key, 0) or 0)
        if value:
            stats[stats_key] = int(stats.get(stats_key, 0) or 0) + value
    for reason in report.get("reasons", []) or []:
        Organizer._append_reason(
            stats, "empty_dir_cleanup_reasons", reason, limit=6
        )
    if cleaned:
        stats["source_dir_cleaned"] = int(stats.get("source_dir_cleaned", 0) or 0) + cleaned
        stats["empty_dirs_cleaned"] = int(stats.get("empty_dirs_cleaned", 0) or 0) + cleaned
        logger.info("手动整理已清理空源目录: %s", source_name or source_id)
    if failures:
        stats["source_dir_cleanup_failed"] = int(
            stats.get("source_dir_cleanup_failed", 0) or 0
        ) + failures
    if unsupported:
        stats["source_dir_cleanup_unsupported"] = int(
            stats.get("source_dir_cleanup_unsupported", 0) or 0
        ) + unsupported


class OrganizeTaskManager:
    def __init__(self) -> None:
        self._lock = CrossProcessLock("guangya-organize")
        self._state_lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._task: dict[str, Any] = {}
        self._worker: threading.Thread | None = None
        self._operation_queue: deque[dict[str, Any]] = deque()
        self._operation_history: deque[dict[str, Any]] = deque(maxlen=16)
        self._task_history: deque[dict[str, Any]] = deque(maxlen=32)
        self._operation_queue_wakeup = threading.Event()
        self._operation_dispatcher: threading.Thread | None = None
        self._shutting_down = False

    def start(
        self,
        sources: list[dict[str, str]],
        rules: OrganizeRules,
        *,
        trigger_type: str = "manual",
        chat_id: str = "",
        download_request_ids: list[int] | None = None,
        client: Any | None = None,
        expected_credential_generation: int | None = None,
    ) -> dict:
        if not sources:
            return {"ok": False, "error": "至少选择一个源目录"}
        self._admission_lock.acquire()
        with self._state_lock:
            if self._shutting_down:
                self._admission_lock.release()
                return {"ok": False, "error": "服务正在停止，暂不接受新的整理任务"}
            if self._operation_queue or count_pending_organize_operation_jobs() > 0:
                self._admission_lock.release()
                return {"ok": False, "error": "网盘整理任务正在运行"}
        if not self._lock.acquire(blocking=False):
            self._admission_lock.release()
            return {"ok": False, "error": "网盘整理任务正在运行"}
        try:
            validator = Organizer(client=client) if client is not None else Organizer()
            if client is None:
                client = getattr(validator, "client", None)
            if client is not None and expected_credential_generation is None:
                try:
                    expected_credential_generation = int(
                        getattr(client, "credential_generation")
                    )
                except (AttributeError, TypeError, ValueError):
                    # 测试替身或兼容客户端可以不实现世代；真实 GuangYaClient
                    # 始终提供持久化 credential_generation。
                    expected_credential_generation = None
            for source in sources:
                validator._validate_target_outside_source(
                    source["id"], rules.target_dir_id
                )
        except DirectoryScrapePublicError as exc:
            self._lock.release()
            self._admission_lock.release()
            return {"ok": False, "error": public_error_message(exc)}
        except Exception:
            self._lock.release()
            self._admission_lock.release()
            logger.error("整理目录边界校验失败", exc_info=True)
            return {"ok": False, "error": "无法校验光鸭整理目录"}
        task_id = uuid.uuid4().hex
        request_ids = list(dict.fromkeys(int(item) for item in (download_request_ids or [])))
        resolved_chat_id = str(chat_id or "").strip()
        if not resolved_chat_id and request_ids:
            try:
                request_row = db.get_download_request(request_ids[0])
            except Exception as exc:
                # 通知上下文是可选信息；数据库短暂不可读不应在已取得跨进程
                # 锁后中断任务并泄漏锁。后续状态写入仍按各自错误边界处理。
                logger.warning(
                    "读取整理任务通知上下文失败 request=%s type=%s",
                    request_ids[0],
                    type(exc).__name__,
                )
                request_row = None
            if request_row is not None:
                resolved_chat_id = str(request_row["chat_id"] or "").strip()
        chat_id = resolved_chat_id
        initial_result = build_organize_result(
            {},
            status="running",
            source_results=sources,
            task_id=task_id,
        )
        initial_result["source_count"] = len(sources)
        run_payload = json.dumps(initial_result, ensure_ascii=False, default=str)
        try:
            run_id = db.add_task_run(
                "guangya_organize", trigger_type, result=run_payload
            )
        except Exception as exc:
            run_id = 0
            logger.warning(
                "创建整理任务运行记录失败 task_id=%s type=%s",
                task_id,
                type(exc).__name__,
            )
        self._cancel_event.clear()
        with self._state_lock:
            self._task = {
                "id": task_id,
                "run_id": run_id,
                "status": "running",
                "message": "整理任务已启动",
                "stoppable": True,
                "sources": sources,
                "rules": rules,
                "chat_id": str(chat_id or "").strip(),
                "current_source": "",
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
                "stats": {},
                "error": "",
                "trigger_type": trigger_type,
                "notification_sent": False,
            }
        for request_id in request_ids:
            db.update_download_request(
                request_id, organize_started=1, organize_task_id=task_id,
                organize_run_id=run_id or None, organize_status="running",
                organize_attempts=0, organize_next_retry_at=None,
                organize_error="", organize_finished_at=None,
                strm_status="pending", strm_error="", strm_finished_at=None,
            )
        try:
            worker = threading.Thread(
                target=self._run,
                args=(
                    task_id,
                    run_id,
                    sources,
                    rules,
                    str(chat_id or "").strip(),
                    request_ids,
                    str(trigger_type or "manual").strip().lower(),
                    client,
                    expected_credential_generation,
                ),
                name=f"organize-{task_id[:8]}",
                daemon=False,
            )
            with self._state_lock:
                self._worker = worker
            worker.start()
        except Exception:
            logger.exception("启动光鸭整理线程失败 task_id=%s", task_id)
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._state_lock:
                self._worker = None
                self._task.update({
                    "status": "failed",
                    "message": "整理任务启动失败",
                    "error": "整理任务启动失败",
                    "stoppable": False,
                    "finished_at": finished_at,
                })
            try:
                for request_id in request_ids:
                    try:
                        db.update_download_request(
                            request_id, organize_started=0, organize_status="queued",
                            organize_attempts=1, organize_next_retry_at=None,
                            organize_error="整理任务线程启动失败，可稍后重试",
                            strm_status="pending", strm_error="",
                        )
                    except Exception:
                        logger.exception("记录整理线程启动失败请求状态异常 request=%s", request_id)
                if run_id:
                    try:
                        failed_result = build_organize_result(
                            {},
                            status="failed",
                            source_results=sources,
                            task_id=task_id,
                            error="整理任务启动失败",
                        )
                        db.finish_task_run(
                            run_id,
                            "failed",
                            result=json.dumps(failed_result, ensure_ascii=False),
                            error="整理任务启动失败",
                        )
                    except Exception:
                        logger.exception("记录整理任务启动失败状态异常 task_id=%s", task_id)
            finally:
                self._lock.release()
                self._admission_lock.release()
            return {"ok": False, "error": "整理任务启动失败", "retryable": True, "error_code": "thread_start_failed"}
        self._admission_lock.release()
        return {
            "ok": True, "task_id": task_id, "run_id": run_id,
            "message": "整理任务已启动",
        }

    def resume(self) -> None:
        """应用启动时重新接受任务并恢复尚未执行的持久化操作。"""
        with self._admission_lock:
            with self._state_lock:
                self._shutting_down = False
        try:
            # 每个 Worker 都保留一个轻量 dispatcher。这样其它 Worker 在
            # 持锁进程异常退出后，也能根据跨进程锁收束孤儿 running 任务。
            self._ensure_operation_dispatcher()
            self._operation_queue_wakeup.set()
        except Exception as exc:
            logger.warning(
                "恢复光鸭持久化操作队列失败 type=%s", type(exc).__name__
            )

    def begin_shutdown(self) -> None:
        """立即关闭新整理任务准入，等待动作由 shutdown() 统一完成。"""
        with self._admission_lock:
            with self._state_lock:
                self._shutting_down = True
                self._cancel_queued_operations_locked("服务关闭，排队任务未执行")
        self._operation_queue_wakeup.set()

    def shutdown(self, timeout: float = 30.0) -> bool:
        """停止接收新任务，并在安全边界等待当前任务结束。"""
        with self._admission_lock:
            with self._state_lock:
                self._shutting_down = True
                self._cancel_queued_operations_locked("服务关闭，排队任务未执行")
                worker = self._worker
                stoppable = self._task.get("stoppable") is not False
                running = self._task.get("status") in {"running", "stopping"}
                if running and stoppable:
                    self._task["status"] = "stopping"
                    self._task["message"] = "服务关闭中，正在安全停止整理任务"
                    self._cancel_event.set()
        self._operation_queue_wakeup.set()
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, float(timeout)))
        drained = not worker or not worker.is_alive()
        if not drained:
            logger.warning("整理任务未能在关闭超时内结束 task=%s", self._task.get("id", ""))
        return drained

    def stop(
        self,
        *,
        expected_task_id: str = "",
        require_running: bool = False,
    ) -> dict:
        """协作式停止任务；可选原子校验任务身份与运行态。"""
        expected_id = str(expected_task_id or "").strip()
        with self._state_lock:
            current_id = str(self._task.get("id") or "").strip()
            if expected_id and current_id != expected_id:
                return {"ok": False, "error": "整理任务状态已变化"}
            allowed_statuses = {"running"} if require_running else {"running", "stopping"}
            if self._task.get("status") not in allowed_statuses:
                return {"ok": False, "error": "当前没有运行中的整理任务"}
            if self._task.get("stoppable") is False:
                return {"ok": False, "error": "当前纠偏操作处于不可中断的原子写入阶段"}
            self._task["status"] = "stopping"
            self._task["message"] = "正在停止，将在当前文件操作完成后退出"
            self._cancel_event.set()
        return {"ok": True, "message": "已发送停止请求"}

    def task_status(self) -> dict:
        """返回纯任务状态，供调度器观察结果，避免状态接口递归。"""
        with self._state_lock:
            queue_payload = self._operation_queue_payload_locked()
            history_payload = list(self._operation_history)
            if not self._task:
                return {
                    "id": "", "status": "idle", "message": "暂无整理任务",
                    "stoppable": False, "sources": [], "current_source": "", "started_at": "",
                    "finished_at": "", "stats": {}, "error": "", "trigger_type": "",
                    "operation_queue": queue_payload,
                    "operation_history": history_payload,
                }
            result = dict(self._task)
            rules = result.pop("rules", None)
            result.pop("chat_id", None)
            result.pop("dedupe_key", None)
            result.pop("owner_digest", None)
            if result.get("durable"):
                result["id"] = ""
                result["reference"] = ""
            if rules is not None:
                result["target_dir_id"] = rules.target_dir_id
            # 轮询接口每秒被 Web 拉取；大型任务的 STRM 变化清单与组级
            # 结果可达数千条，属于内部联动数据，没有任何状态消费方读取。
            # 只保留计数，避免把数百 KB 的内部载荷反复序列化给前端。
            stats = result.get("stats")
            if isinstance(stats, dict) and (
                "strm_changes" in stats or "group_results" in stats
            ):
                slim = {
                    key: value for key, value in stats.items()
                    if key not in {"strm_changes", "group_results"}
                }
                slim["strm_changes_count"] = len(stats.get("strm_changes") or [])
                result["stats"] = slim
            result["operation_queue"] = queue_payload
            result["operation_history"] = history_payload
            return result

    def task_result(self, task_id: str, *, owner: str | None = None) -> dict | None:
        """按任务 ID 查询当前或近期终态；公开编号必须绑定调用主体。"""
        expected = str(task_id or "").strip()
        if not expected:
            return None
        public_reference = expected.upper().startswith("GY-")
        if public_reference:
            if not str(owner or "").strip():
                return None
            try:
                expected = organize_operation_job_id_from_public_ref(expected)
            except ValueError:
                return None
        owner_digest = (
            organize_operation_owner_digest(str(owner))
            if str(owner or "").strip() else ""
        )
        with self._state_lock:
            if str(self._task.get("id") or "") == expected:
                if owner_digest and str(self._task.get("owner_digest") or "") != owner_digest:
                    pass
                else:
                    result = dict(self._task)
                    if isinstance(result.get("result"), dict):
                        result["result"] = read_organize_result(result["result"])
                    return result
            for item in self._task_history:
                if str(item.get("id") or "") == expected:
                    if owner_digest and str(item.get("owner_digest") or "") != owner_digest:
                        continue
                    result = dict(item)
                    if isinstance(result.get("result"), dict):
                        result["result"] = read_organize_result(result["result"])
                    return result
        try:
            row = (
                get_organize_operation_job_for_owner(expected, str(owner))
                if owner_digest else get_organize_operation_job(expected)
            )
        except ValueError:
            row = None
        if row is None:
            return None
        try:
            persisted_result = json.loads(str(row["result_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            persisted_result = {}
        status = str(row["status"] or "")
        public_status = "queued" if status == "pending" else status
        return {
            "id": str(row["job_id"] or ""),
            "status": public_status,
            "message": (
                f"{str(row['operation'] or '操作')}已排队"
                if status == "pending" else
                f"{str(row['operation'] or '操作')}需要人工核验"
                if status == "manual_review" else
                f"{str(row['operation'] or '操作')}{'已完成' if status == 'completed' else '部分完成' if status == 'partial' else '执行失败' if status == 'failed' else '已取消' if status == 'cancelled' else '正在执行'}"
            ),
            "operation": str(row["operation"] or ""),
            "reference": str(row["reference"] or ""),
            "started_at": str(row["started_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
            "error": str(row["error"] or ""),
            "result": persisted_result if isinstance(persisted_result, dict) else {},
            "durable": True,
        }

    def status(self) -> dict:
        result = self.task_status()
        try:
            from app.modules.organize_scheduler import get_organize_scheduler

            result["schedule"] = get_organize_scheduler().status()
        except Exception as exc:
            logger.warning(
                "读取网盘整理调度状态失败 type=%s",
                type(exc).__name__,
            )
            result["schedule"] = {
                "enabled": False, "cron": "", "cron_valid": False,
                "config_error": "调度状态不可用", "next_run": "", "last_result": {},
            }
        return result

    def clean_empty(self, sources: list[dict[str, str]], *, client=None) -> dict:
        if not sources:
            return {"ok": False, "error": "至少选择一个源目录"}
        with self._admission_lock:
            with self._state_lock:
                if self._shutting_down:
                    return {"ok": False, "error": "服务正在停止，暂不接受新的整理操作"}
                if self._operation_queue:
                    return {"ok": False, "error": "网盘整理任务正在运行"}
            if not self._lock.acquire(blocking=False):
                return {"ok": False, "error": "网盘整理任务正在运行"}
            try:
                total = 0
                details = []
                organizer = Organizer(client=client) if client is not None else Organizer()
                protected_source_ids = {
                    str(source.get("id") or "").strip()
                    for source in sources
                    if str(source.get("id") or "").strip()
                }
                for source in sources:
                    try:
                        report = organizer.clean_empty_dirs(
                            source["id"],
                            with_report=True,
                            protected_source_ids=protected_source_ids,
                        )
                    except Exception as exc:
                        logger.warning(
                            "清理来源空目录失败 type=%s",
                            type(exc).__name__,
                        )
                        report = {
                            "cleaned": 0,
                            "scan_failures": 1,
                            "delete_failures": 0,
                            "unsupported": 0,
                        }
                    if not isinstance(report, dict):
                        report = {
                            "cleaned": int(report or 0),
                            "scan_failures": 0,
                            "delete_failures": 0,
                            "unsupported": 0,
                        }
                    cleaned = max(0, int(report.get("cleaned") or 0))
                    scan_failures = max(0, int(report.get("scan_failures") or 0))
                    delete_failures = max(0, int(report.get("delete_failures") or 0))
                    unsupported = max(0, int(report.get("unsupported") or 0))
                    total += cleaned
                    details.append({
                        **source,
                        "cleaned": cleaned,
                        "scan_failures": scan_failures,
                        "delete_failures": delete_failures,
                        "unsupported": unsupported,
                    })
                scan_failures = sum(item["scan_failures"] for item in details)
                delete_failures = sum(item["delete_failures"] for item in details)
                unsupported = sum(item["unsupported"] for item in details)
                return {
                    "ok": True,
                    "partial": bool(scan_failures or delete_failures),
                    "cleaned": total,
                    "scan_failures": scan_failures,
                    "delete_failures": delete_failures,
                    "unsupported": unsupported,
                    "sources": details,
                }
            finally:
                self._lock.release()

    def start_operation(
        self,
        operation: str,
        reference: str,
        callback: Callable[[], object],
        *,
        queue_if_busy: bool = False,
        dedupe_key: str = "",
    ) -> dict:
        """提交与全量整理共用互斥锁的单次云盘写操作。

        默认保持旧行为：锁忙时立即返回冲突。人工目录刮削可显式开启
        ``queue_if_busy``，在同一进程内按 FIFO 串行执行，避免快速连续
        提交时让用户反复等待和重试。
        """
        operation = str(operation or "操作").strip() or "操作"
        reference = str(reference or "").strip()
        dedupe_key = str(dedupe_key or "").strip()
        task_id = uuid.uuid4().hex
        with self._admission_lock:
            with self._state_lock:
                if self._shutting_down:
                    return {"ok": False, "error": "服务正在停止，暂不接受新的整理操作"}
                duplicate = self._find_operation_locked(dedupe_key)
                if duplicate is not None:
                    return {
                        "ok": True,
                        "task_id": duplicate["id"],
                        "message": duplicate["message"],
                        "queued": duplicate["status"] == "queued",
                        "queue_position": duplicate.get("queue_position", 0),
                        "replayed": True,
                    }
                queue_has_items = bool(self._operation_queue)
            try:
                durable_has_items = (
                    count_pending_organize_operation_jobs() > 0
                    or count_running_organize_operation_jobs() > 0
                )
            except Exception as exc:
                logger.warning(
                    "检查光鸭持久化队列准入失败 type=%s", type(exc).__name__
                )
                durable_has_items = True

            if (
                not queue_has_items
                and not durable_has_items
                and self._lock.acquire(blocking=False)
            ):
                return self._launch_operation_with_lock(
                    task_id,
                    operation,
                    reference,
                    callback,
                    dedupe_key=dedupe_key,
                )

            if not queue_if_busy:
                return {"ok": False, "error": "网盘整理任务正在运行"}

            queued_item = {
                "id": task_id,
                "status": "queued",
                "message": f"{operation}已排队",
                "operation": operation,
                "reference": reference,
                "callback": callback,
                "dedupe_key": dedupe_key,
                "queued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            }
            with self._state_lock:
                self._operation_queue.append(queued_item)
                queue_position = len(self._operation_queue)
            try:
                self._ensure_operation_dispatcher()
            except Exception:
                logger.exception(
                    "启动光鸭整理排队调度线程失败 operation=%s", operation
                )
                with self._state_lock:
                    try:
                        self._operation_queue.remove(queued_item)
                    except ValueError:
                        pass
                return {
                    "ok": False,
                    "error": f"{operation}排队失败",
                    "retryable": True,
                    "error_code": "queue_dispatcher_start_failed",
                }
            self._operation_queue_wakeup.set()
            return {
                "ok": True,
                "task_id": task_id,
                "message": f"{operation}已排队，前方 {max(0, queue_position - 1)} 项",
                "queued": True,
                "queue_position": queue_position,
            }

    def start_durable_operation(
        self,
        operation: str,
        reference: str,
        *,
        job_kind: str,
        owner: str,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> dict:
        """提交可在进程重启后恢复的单次云盘操作。

        仅 ``pending`` 状态会在重启后继续执行；已经进入 ``running`` 的
        云端写操作只有在同一把跨进程锁确认空闲后才收束为 ``manual_review``，
        避免误判仍存活的执行者，也避免对未知远端副作用盲目重放。
        """
        operation = str(operation or "操作").strip() or "操作"
        reference = str(reference or "").strip()
        with self._admission_lock:
            with self._state_lock:
                if self._shutting_down:
                    return {"ok": False, "error": "服务正在停止，暂不接受新的整理操作"}
            try:
                row, replayed = enqueue_organize_operation_job(
                    job_kind=job_kind,
                    owner=owner,
                    operation=operation,
                    reference=reference,
                    payload=payload,
                    dedupe_key=dedupe_key,
                )
            except OrganizeOperationQueueFullError:
                return {
                    "ok": False, "error": "光鸭操作队列已满，请等待现有任务完成",
                    "retryable": True, "error_code": "durable_queue_full",
                }
            except Exception as exc:
                logger.warning(
                    "创建光鸭持久化操作失败 operation=%s type=%s",
                    operation, type(exc).__name__,
                )
                return {
                    "ok": False, "error": f"{operation}排队失败",
                    "retryable": True, "error_code": "durable_queue_write_failed",
                }
            task_id = str(row["job_id"] or "")
            status = str(row["status"] or "")
            if replayed:
                position = (
                    organize_operation_queue_position(task_id)
                    if status == "pending" else 0
                )
                return {
                    "ok": True, "task_id": task_id,
                    "message": (
                        f"{operation}已排队" if status == "pending"
                        else f"{operation}正在执行"
                    ),
                    "queued": status == "pending",
                    "queue_position": position,
                    "replayed": True,
                }

            with self._state_lock:
                can_launch_now = self._worker is None and not self._operation_queue
            if can_launch_now and self._lock.acquire(blocking=False):
                try:
                    claimed = claim_organize_operation_job(task_id)
                except Exception as exc:
                    self._lock.release()
                    logger.warning(
                        "领取新建光鸭持久化操作失败 operation=%s type=%s",
                        operation, type(exc).__name__,
                    )
                    return {
                        "ok": False, "error": f"{operation}排队失败",
                        "retryable": True, "error_code": "durable_queue_claim_failed",
                    }
                if claimed is not None:
                    return self._launch_durable_operation_with_lock(claimed)
                self._lock.release()

            try:
                self._ensure_operation_dispatcher()
            except Exception:
                logger.exception(
                    "启动光鸭持久化操作调度线程失败 operation=%s", operation
                )
                fail_pending_organize_operation_job(
                    task_id,
                    error_code="queue_dispatcher_start_failed",
                    error=f"{operation}排队失败",
                )
                return {
                    "ok": False, "error": f"{operation}排队失败",
                    "retryable": True, "error_code": "queue_dispatcher_start_failed",
                }
            self._operation_queue_wakeup.set()
            current = get_organize_operation_job(task_id)
            current_status = str(current["status"] or "") if current is not None else "pending"
            position = (
                organize_operation_queue_position(task_id)
                if current_status == "pending" else 0
            )
            queued = current_status == "pending"
            return {
                "ok": True, "task_id": task_id,
                "message": (
                    f"{operation}已排队，前方 {max(0, position - 1)} 项"
                    if queued else f"{operation}已被调度执行"
                ),
                "queued": queued, "queue_position": position,
            }

    def _launch_durable_operation_with_lock(self, row: Any) -> dict:
        task_id = str(row["job_id"] or "")
        operation = str(row["operation"] or "操作")
        reference = str(row["reference"] or "")
        generation = int(row["lease_generation"] or 0)
        self._cancel_event.clear()
        with self._state_lock:
            self._task = {
                "id": task_id, "status": "running",
                "message": f"{operation}已启动", "operation": operation,
                "stoppable": False, "reference": reference, "sources": [],
                "current_source": reference,
                "started_at": str(row["started_at"] or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "finished_at": "", "stats": {}, "error": "",
                "durable": True, "owner_digest": str(row["owner_digest"] or ""),
            }
        try:
            worker = threading.Thread(
                target=self._run_durable_operation,
                args=(dict(row),),
                name=f"organize-durable-{task_id[:8]}",
                daemon=False,
            )
            with self._state_lock:
                self._worker = worker
            worker.start()
        except Exception:
            logger.exception("启动光鸭持久化操作线程失败 operation=%s", operation)
            persisted = False
            try:
                persisted = finish_organize_operation_job(
                    task_id, expected_lease_generation=generation, status="failed",
                    error_code="WorkerStartFailed", error=f"{operation}启动失败",
                )
            except Exception as persist_exc:
                logger.warning(
                    "持久化光鸭操作启动失败终态异常 type=%s",
                    type(persist_exc).__name__,
                )
            finally:
                with self._state_lock:
                    self._worker = None
                    self._task.update({
                        "status": "failed" if persisted else "manual_review",
                        "message": (
                            f"{operation}启动失败" if persisted
                            else f"{operation}需要人工核验"
                        ),
                        "error": (
                            f"{operation}启动失败" if persisted
                            else "任务启动失败且状态未能可靠持久化，请核对后重试"
                        ),
                        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    self._remember_task_locked(self._task)
                    self._remember_operation_locked(self._task)
                self._lock.release()
                self._operation_queue_wakeup.set()
            return {"ok": False, "error": f"{operation}启动失败"}
        return {"ok": True, "task_id": task_id, "message": f"{operation}已启动"}

    @staticmethod
    def _execute_durable_operation(row: dict[str, Any]) -> object:
        if not verify_organize_operation_payload(row):
            raise ValueError("持久化操作参数完整性校验失败")
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("持久化操作参数损坏") from exc
        if not isinstance(payload, dict):
            raise ValueError("持久化操作参数损坏")
        task_id = str(row.get("job_id") or "")
        generation = int(row.get("lease_generation") or 0)

        def cancel_check() -> None:
            if is_organize_operation_cancel_requested(
                task_id, expected_lease_generation=generation
            ):
                raise OrganizeOperationCancelled("光鸭操作已取消")

        cancel_check()
        kind = str(row.get("job_kind") or "")
        if kind == "agent_directory_scrape":
            from app.agent.guangya_directory_scrape_actions import (
                execute_durable_directory_scrape_job,
            )

            return execute_durable_directory_scrape_job(
                payload, cancel_check=cancel_check
            )
        raise ValueError("不支持的持久化操作类型")

    def _run_durable_operation(self, row: dict[str, Any]) -> None:
        task_id = str(row.get("job_id") or "")
        operation = str(row.get("operation") or "操作")
        generation = int(row.get("lease_generation") or 0)
        try:
            result = self._execute_durable_operation(row)
        except Exception as exc:
            logger.error(
                "持久化整理操作失败 operation=%s type=%s",
                operation, type(exc).__name__,
            )
            cancelled = isinstance(exc, OrganizeOperationCancelled)
            error = "" if cancelled else (
                public_error_message(exc)
                if isinstance(exc, DirectoryScrapePublicError)
                else f"{operation}失败，请重新检查后重试"
            )
            terminal_status = "cancelled" if cancelled else "failed"
            try:
                persisted = finish_organize_operation_job(
                    task_id, expected_lease_generation=generation, status=terminal_status,
                    error_code="" if cancelled else type(exc).__name__, error=error,
                )
            except Exception as persist_exc:
                logger.warning(
                    "持久化光鸭操作失败终态写入异常 type=%s",
                    type(persist_exc).__name__,
                )
                persisted = False
            memory_status = terminal_status if persisted else "manual_review"
            memory_error = error if persisted else (
                "操作执行结果未能可靠持久化，请核对目标目录后再决定是否重试"
            )
            with self._state_lock:
                if self._task.get("id") == task_id:
                    self._task.update({
                        "status": memory_status,
                        "message": (
                            f"{operation}已取消" if persisted and cancelled
                            else f"{operation}失败" if persisted
                            else f"{operation}需要人工核验"
                        ),
                        "error": memory_error, "current_source": "",
                        "group_progress": {},
                        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    self._remember_task_locked(self._task)
                    self._remember_operation_locked(self._task)
        else:
            partial = _operation_result_is_partial(result)
            terminal_status = "partial" if partial else "completed"
            safe_result = sanitize_organize_operation_result(result)
            try:
                persisted = finish_organize_operation_job(
                    task_id, expected_lease_generation=generation,
                    status=terminal_status,
                    result=safe_result,
                )
            except Exception as persist_exc:
                logger.warning(
                    "持久化光鸭操作成功终态写入异常 type=%s",
                    type(persist_exc).__name__,
                )
                persisted = False
            memory_status = terminal_status if persisted else "manual_review"
            with self._state_lock:
                if self._task.get("id") == task_id:
                    self._task.update({
                        "status": memory_status,
                        "message": (
                            f"{operation}部分完成" if persisted and partial
                            else f"{operation}已完成" if persisted
                            else f"{operation}需要人工核验"
                        ),
                        "error": "" if persisted else (
                            "操作结果未能可靠持久化，请核对目标目录"
                        ),
                        "current_source": "", "group_progress": {},
                        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "result": safe_result if persisted else {},
                    })
                    self._remember_task_locked(self._task)
                    self._remember_operation_locked(self._task)
        finally:
            with self._state_lock:
                if self._task.get("id") == task_id:
                    self._worker = None
            self._lock.release()
            self._operation_queue_wakeup.set()
            self._wake_download_tracker()


    def _launch_operation_with_lock(
        self,
        task_id: str,
        operation: str,
        reference: str,
        callback: Callable[[], object],
        *,
        dedupe_key: str = "",
    ) -> dict:
        """在已持有跨进程锁时启动单次操作。"""
        self._cancel_event.clear()
        with self._state_lock:
            self._task = {
                "id": task_id,
                "status": "running",
                "message": f"{operation}已启动",
                "operation": operation,
                "stoppable": False,
                "reference": reference,
                "sources": [],
                "current_source": reference,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
                "stats": {},
                "error": "",
                "dedupe_key": dedupe_key,
            }
        try:
            worker = threading.Thread(
                target=self._run_operation,
                args=(task_id, operation, reference, callback),
                name=f"organize-op-{task_id[:8]}",
                daemon=False,
            )
            with self._state_lock:
                self._worker = worker
            worker.start()
        except Exception:
            logger.exception("启动光鸭整理操作线程失败 operation=%s", operation)
            with self._state_lock:
                self._worker = None
                self._task.update({
                    "status": "failed", "message": f"{operation}启动失败",
                    "error": f"{operation}启动失败", "stoppable": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                self._remember_task_locked(self._task)
                self._remember_operation_locked(self._task)
            self._lock.release()
            self._operation_queue_wakeup.set()
            return {"ok": False, "error": f"{operation}启动失败"}
        return {"ok": True, "task_id": task_id, "message": f"{operation}已启动"}

    def _find_operation_locked(self, dedupe_key: str) -> dict[str, Any] | None:
        if not dedupe_key:
            return None
        if (
            self._task.get("dedupe_key") == dedupe_key
            and self._task.get("status") in {"running", "stopping"}
        ):
            return {
                "id": str(self._task.get("id") or ""),
                "status": str(self._task.get("status") or "running"),
                "message": str(self._task.get("message") or "任务正在运行"),
                "queue_position": 0,
            }
        for position, item in enumerate(self._operation_queue, start=1):
            if item.get("dedupe_key") == dedupe_key:
                return {
                    "id": str(item.get("id") or ""),
                    "status": "queued",
                    "message": str(item.get("message") or "任务已排队"),
                    "queue_position": position,
                }
        return None

    def _operation_queue_payload_locked(self) -> dict[str, Any]:
        # 通用 Web 状态只展开本进程 legacy 队列；owner 隔离的 Agent durable
        # 队列仅暴露聚合数量，详情必须通过 owner-bound GY 编号查询。
        items = [
            {
                "id": str(item.get("id") or ""),
                "status": "queued",
                "message": str(item.get("message") or "任务已排队"),
                "operation": str(item.get("operation") or ""),
                "reference": str(item.get("reference") or ""),
                "queued_at": str(item.get("queued_at") or ""),
                "queue_position": position,
                "ahead": position - 1,
                "durable": False,
            }
            for position, item in enumerate(self._operation_queue, start=1)
        ]
        try:
            durable_count = count_pending_organize_operation_jobs()
        except Exception as exc:
            logger.warning(
                "读取光鸭持久化操作队列失败 type=%s", type(exc).__name__
            )
            durable_count = 0
        return {
            "total": len(items) + durable_count,
            "items": items,
            "durable_pending_count": durable_count,
        }

    def _remember_task_locked(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("id") or "")
        if not task_id:
            return
        self._task_history = deque(
            (item for item in self._task_history if str(item.get("id") or "") != task_id),
            maxlen=self._task_history.maxlen,
        )
        self._task_history.appendleft({
            "id": task_id,
            "status": str(task.get("status") or ""),
            "message": str(task.get("message") or ""),
            "operation": str(task.get("operation") or ""),
            "reference": str(task.get("reference") or ""),
            "trigger_type": str(task.get("trigger_type") or ""),
            "finished_at": str(task.get("finished_at") or ""),
            "error": str(task.get("error") or ""),
            "stats": dict(task.get("stats") or {}),
            "notification_sent": bool(task.get("notification_sent")),
            "result": task.get("result") if isinstance(task.get("result"), dict) else {},
            "owner_digest": str(task.get("owner_digest") or ""),
            "durable": bool(task.get("durable")),
        })

    def _cancel_queued_operations_locked(self, message: str) -> None:
        finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        while self._operation_queue:
            item = self._operation_queue.popleft()
            terminal = {
                "id": str(item.get("id") or ""),
                "status": "stopped",
                "message": message,
                "error": message,
                "operation": str(item.get("operation") or ""),
                "reference": str(item.get("reference") or ""),
                "finished_at": finished_at,
                "result": {},
            }
            self._remember_task_locked(terminal)
            self._remember_operation_locked(terminal)

    def _remember_operation_locked(self, task: dict[str, Any]) -> None:
        if bool(task.get("durable")):
            return
        result = task.get("result")
        compact_result: dict[str, Any] = {}
        if isinstance(result, dict):
            stats = result.get("stats")
            if isinstance(stats, dict):
                compact_result["stats"] = dict(stats)
        self._operation_history.appendleft({
            "id": str(task.get("id") or ""),
            "status": str(task.get("status") or ""),
            "message": str(task.get("message") or ""),
            "operation": str(task.get("operation") or ""),
            "reference": str(task.get("reference") or ""),
            "finished_at": str(task.get("finished_at") or ""),
            "error": str(task.get("error") or ""),
            "result": compact_result,
        })

    def _ensure_operation_dispatcher(self) -> None:
        with self._state_lock:
            current = self._operation_dispatcher
            if current is not None and current.is_alive():
                return
            dispatcher = threading.Thread(
                target=self._operation_dispatch_loop,
                name="organize-operation-queue",
                daemon=True,
            )
            self._operation_dispatcher = dispatcher
        try:
            dispatcher.start()
        except Exception:
            with self._state_lock:
                if self._operation_dispatcher is dispatcher:
                    self._operation_dispatcher = None
            raise

    def _operation_dispatch_loop(self) -> None:
        while True:
            self._operation_queue_wakeup.wait(timeout=2.0)
            self._operation_queue_wakeup.clear()
            while True:
                with self._admission_lock:
                    with self._state_lock:
                        if self._shutting_down:
                            return
                        memory_pending = bool(self._operation_queue)
                        if self._worker is not None:
                            break
                    try:
                        durable_pending = count_pending_organize_operation_jobs() > 0
                        durable_running = count_running_organize_operation_jobs() > 0
                    except Exception as exc:
                        logger.warning(
                            "检查光鸭持久化队列失败 type=%s", type(exc).__name__
                        )
                        durable_pending = False
                        durable_running = False
                    if not memory_pending and not durable_pending and not durable_running:
                        break
                    if self._lock.acquire(blocking=False):
                        if durable_running:
                            try:
                                recovered = recover_orphaned_organize_operation_jobs()
                                if recovered:
                                    logger.warning(
                                        "已将 %s 个失去执行进程的光鸭操作标记为需要人工核验",
                                        recovered,
                                    )
                            except Exception as exc:
                                logger.warning(
                                    "收束光鸭孤儿操作失败 type=%s", type(exc).__name__
                                )
                                self._lock.release()
                                break
                        durable_row = None
                        if durable_pending:
                            try:
                                pending_rows = list_pending_organize_operation_jobs(limit=1)
                                durable_row = pending_rows[0] if pending_rows else None
                            except Exception as exc:
                                logger.warning(
                                    "读取光鸭持久化队首失败 type=%s",
                                    type(exc).__name__,
                                )
                        with self._state_lock:
                            memory_item = (
                                self._operation_queue[0]
                                if self._operation_queue
                                else None
                            )
                        memory_key = (
                            str(memory_item.get("queued_at") or ""),
                            str(memory_item.get("id") or ""),
                        ) if memory_item is not None else None
                        durable_key = (
                            str(durable_row["created_at"] or ""),
                            str(durable_row["job_id"] or ""),
                        ) if durable_row is not None else None
                        launch_memory = bool(
                            memory_item is not None
                            and (durable_key is None or memory_key <= durable_key)
                        )
                        if launch_memory:
                            with self._state_lock:
                                item = (
                                    self._operation_queue.popleft()
                                    if self._operation_queue
                                    else None
                                )
                            if item is not None:
                                self._launch_operation_with_lock(
                                    str(item["id"]),
                                    str(item["operation"]),
                                    str(item["reference"]),
                                    item["callback"],
                                    dedupe_key=str(item.get("dedupe_key") or ""),
                                )
                                break
                        try:
                            row = claim_organize_operation_job(
                                str(durable_row["job_id"])
                                if durable_row is not None
                                else None
                            )
                        except Exception as exc:
                            logger.warning(
                                "领取光鸭持久化操作失败 type=%s", type(exc).__name__
                            )
                            row = None
                        if row is None:
                            self._lock.release()
                            break
                        self._launch_durable_operation_with_lock(row)
                        break
                if self._operation_queue_wakeup.wait(timeout=0.25):
                    self._operation_queue_wakeup.clear()
                    continue

    def _run_operation(self, task_id: str, operation: str, reference: str,
                       callback: Callable[[], object]) -> None:
        try:
            result = callback()
            partial = _operation_result_is_partial(result)
            with self._state_lock:
                if self._task.get("id") == task_id:
                    self._task.update({
                        "status": "partial" if partial else "completed",
                        "message": f"{operation}部分完成" if partial else f"{operation}已完成",
                        "current_source": "",
                        "group_progress": {},
                        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "result": result,
                    })
                    self._remember_task_locked(self._task)
                    self._remember_operation_locked(self._task)
        except Exception as exc:
            logger.error(
                "整理操作失败 operation=%s type=%s",
                operation,
                type(exc).__name__,
            )
            error = (
                public_error_message(exc)
                if isinstance(exc, DirectoryScrapePublicError)
                else f"{operation}失败，请稍后重试"
            )
            with self._state_lock:
                if self._task.get("id") == task_id:
                    self._task.update({
                        "status": "failed", "message": f"{operation}失败", "error": error,
                        "current_source": "",
                        "group_progress": {},
                        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    self._remember_task_locked(self._task)
                    self._remember_operation_locked(self._task)
        finally:
            with self._state_lock:
                if self._task.get("id") == task_id:
                    self._worker = None
            self._lock.release()
            self._operation_queue_wakeup.set()
            self._wake_download_tracker()

    def _run(
        self,
        task_id: str,
        run_id: int,
        sources: list[dict[str, str]],
        rules: OrganizeRules,
        chat_id: str = "",
        download_request_ids: list[int] | None = None,
        trigger_type: str = "manual",
        client: Any | None = None,
        expected_credential_generation: int | None = None,
    ) -> None:
        aggregate: dict[str, object] = {}
        source_results = []
        current_source = ""
        try:
            if not _credential_snapshot_is_current(client, expected_credential_generation):
                raise RuntimeError("光鸭登录凭据已变化，已拒绝执行整理")
            organizer = Organizer(client=client) if client is not None else Organizer()
            protected_source_ids = {
                str(source.get("id") or "").strip()
                for source in sources
                if str(source.get("id") or "").strip()
            }
            for source in sources:
                if self._cancel_event.is_set():
                    break
                if not _credential_snapshot_is_current(client, expected_credential_generation):
                    raise RuntimeError("光鸭登录凭据已变化，已拒绝继续整理")
                current_source = str(source.get("name") or source.get("id") or "")
                with self._state_lock:
                    self._task["current_source"] = current_source
                    self._task["message"] = f"正在整理：{current_source}"
                    self._task["group_progress"] = {}
                    self._task["source_groups"] = []

                def _publish_group_progress(
                    payload: dict, _source: str = current_source,
                ) -> None:
                    """把组级进度投影到任务状态；前端据此局部更新，不整块重绘。"""
                    progress = dict(payload.get("progress") or {})
                    groups = [
                        dict(row) for row in (payload.get("groups") or [])
                        if isinstance(row, dict)
                    ]
                    index = int(progress.get("current_index") or 0)
                    total = int(progress.get("total") or 0)
                    label = str(progress.get("current_group") or "")
                    stage = str(progress.get("current_stage_label") or "")
                    with self._state_lock:
                        if self._task.get("id") != task_id:
                            return
                        self._task["group_progress"] = progress
                        self._task["source_groups"] = groups
                        if total and index:
                            detail = f"{index}/{total}"
                            if label:
                                detail = f"{detail} · {label}"
                            if stage:
                                detail = f"{detail} · {stage}"
                            self._task["message"] = f"正在整理：{_source} · {detail}"

                organizer._validate_target_outside_source(
                    source["id"], rules.target_dir_id
                )
                _plans, stats = organizer.organize(
                    source["id"], rules, dry_run=False,
                    cancel_event=self._cancel_event, post_actions=False,
                    source_name=current_source,
                    require_complete_scan=True,
                    protected_source_ids=protected_source_ids,
                    automatic=trigger_type in {"cron", "download", "telegram"},
                    group_progress=_publish_group_progress,
                )
                _cleanup_manual_source_root(
                    organizer,
                    source["id"],
                    current_source,
                    rules,
                    stats,
                    trigger_type=trigger_type,
                )
                source_results.append({**source, "stats": stats})
                for key, value in stats.items():
                    if (
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and key != "stopped"
                    ):
                        aggregate[key] = int(aggregate.get(key, 0) or 0) + value
                aggregate.setdefault("strm_changes", []).extend(
                    list(stats.get("strm_changes") or [])
                )
                aggregate["strm_force_full"] = bool(
                    aggregate.get("strm_force_full")
                    or stats.get("strm_force_full")
                )
                for key in ("media_items", "confirmation_groups", "group_results"):
                    values = stats.get(key) or []
                    if isinstance(values, list):
                        aggregate.setdefault(key, []).extend(
                            item for item in values if isinstance(item, dict)
                        )
                for key in (
                    "confirmations", "skip_reasons", "empty_dir_cleanup_reasons"
                ):
                    for value in stats.get(key) or []:
                        Organizer._append_reason(aggregate, key, value, limit=10)
                with self._state_lock:
                    self._task["stats"] = dict(aggregate)
            stopped = self._cancel_event.is_set()
            aggregate["stopped"] = 1 if stopped else int(aggregate.get("stopped", 0) or 0)
            partial = bool(
                int(aggregate.get("failed", 0) or 0)
                or aggregate.get("scan_errors")
                or int(aggregate.get("replacement_cleanup_failed", 0) or 0)
                or int(aggregate.get("empty_dir_cleanup_failed", 0) or 0)
                or int(aggregate.get("source_dir_cleanup_failed", 0) or 0)
                or int(aggregate.get("audit_failures", 0) or 0)
            )
            notification_sent = False
            if source_results or stopped:
                # 通知 outbox 用任务 ID 作为幂等键，重试不会重复发送汇总。
                aggregate["task_id"] = task_id
                Organizer.trigger_post_actions(
                    aggregate,
                    rules,
                    source_name=f"{len(source_results)} 个源目录",
                    chat_id=chat_id,
                    download_request_ids=download_request_ids,
                    notify_result=False,
                )
                notification_sent = bool(Organizer.notify_task_results(
                    aggregate,
                    rules,
                    source_name=f"{len(source_results)} 个源目录",
                    chat_id=chat_id,
                ))
            status = "stopped" if stopped else ("partial" if partial else "completed")
            message = (
                "整理任务已停止" if stopped
                else ("整理任务部分完成" if partial else "整理任务已完成")
            )
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if status == "completed":
                self._cleanup_download_staging(organizer, download_request_ids or [], sources)
            structured_result = build_organize_result(
                aggregate,
                status=status,
                source_results=source_results,
                notification_sent=notification_sent,
                task_id=task_id,
            )
            with self._state_lock:
                self._task.update({
                    "status": status,
                    "message": message,
                    "current_source": "",
                    "group_progress": {},
                    "finished_at": finished_at,
                    "stats": aggregate,
                    "source_results": source_results,
                    "notification_sent": notification_sent,
                    "result": structured_result,
                })
            for request_id in download_request_ids or []:
                fields = {
                    "organize_status": status, "organize_error": "",
                    "organize_finished_at": db.now(),
                }
                if stopped:
                    # 停止不代表回滚；可能已有部分文件移动，禁止跟踪器自动重跑。
                    fields.update({
                        "organize_started": -1,
                        "organize_error": (
                            "整理任务已停止，可能已有部分文件完成移动；"
                            "请先核对整理日志，勿直接重复执行"
                        ),
                    })
                elif partial:
                    # partial 可能已经移动了部分文件，既不能静默完成，也不能
                    # 由 tracker 自动重跑；转入待处理供用户核验具体失败项。
                    fields.update({
                        "organize_started": -1,
                        "organize_error": (
                            "整理任务部分完成，可能仍有文件未入库；"
                            "请核对整理日志后人工处理"
                        ),
                    })
                strm_result = aggregate.get("strm") or {}
                # started/queued 状态由 scheduler 在启动线程或入队之前写入；
                # 这里不得再覆盖可能已经完成的 STRM 终态。
                if not (isinstance(strm_result, dict) and strm_result.get("ok")):
                    if stopped:
                        strm_status = "skipped"
                        strm_error = "整理任务已停止，未执行后续 STRM 同步"
                    elif isinstance(strm_result, dict) and strm_result.get("skipped"):
                        strm_status = "skipped"
                        strm_error = str(strm_result.get("error") or "")
                    elif isinstance(strm_result, dict) and strm_result:
                        strm_status = "failed"
                        strm_error = str(strm_result.get("error") or "STRM 联动失败")
                    else:
                        strm_status = "skipped"
                        strm_error = (
                            "整理未产生需要同步的媒体变更"
                            if not int(aggregate.get("moved", 0) or 0)
                            else "未启用或未配置整理联动 STRM"
                        )
                    fields.update({
                        "strm_status": strm_status, "strm_error": strm_error[:500],
                        "strm_finished_at": db.now(),
                    })
                db.update_download_request(request_id, **fields)
            if run_id:
                try:
                    db.finish_task_run(
                        run_id,
                        "skipped" if stopped else ("partial" if partial else "success"),
                        result=json.dumps(
                            structured_result, ensure_ascii=False, default=str
                        ),
                    )
                except Exception as persist_exc:
                    logger.error(
                        "保存整理完成状态失败 task_id=%s type=%s",
                        task_id,
                        type(persist_exc).__name__,
                    )
        except Exception as exc:
            logger.exception(
                "整理后台任务失败 task_id=%s source=%s type=%s",
                task_id,
                current_source or "-",
                type(exc).__name__,
            )
            public_error = "整理任务失败，且可能已有部分文件完成移动；请先查看整理日志，勿直接重复执行"
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for request_id in download_request_ids or []:
                db.update_download_request(
                    request_id, organize_started=-1, organize_status="failed",
                    organize_error=public_error, organize_finished_at=db.now(),
                    strm_status="skipped",
                    strm_error="整理失败，未执行 STRM 同步",
                    strm_finished_at=db.now(),
                )
            notification_sent = False
            try:
                from app.notifier import NotificationEvent, send_event
                notification_sent = bool(send_event(
                    NotificationEvent(
                        "❌ 光鸭整理任务失败",
                        fields=(
                            ("任务", task_id),
                            ("当前目录", current_source or "未记录"),
                            ("状态", "可能已部分执行，请先核对整理日志"),
                        ),
                        footer="已停止后续 STRM 同步和媒体库刷新，请勿直接重复执行。",
                    ),
                    chat_id=chat_id or None,
                ))
            except Exception as notify_exc:
                logger.error(
                    "发送整理失败通知失败 task_id=%s type=%s",
                    task_id,
                    type(notify_exc).__name__,
                )
            persistence_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            structured_result = build_organize_result(
                aggregate,
                status="failed",
                source_results=source_results,
                notification_sent=notification_sent,
                task_id=task_id,
                current_source=current_source,
                error=public_error,
            )
            if run_id:
                try:
                    db.finish_task_run(
                        run_id,
                        "failed",
                        result=json.dumps(
                            structured_result, ensure_ascii=False, default=str
                        ),
                        error=persistence_error,
                    )
                except Exception as persist_exc:
                    logger.error(
                        "保存整理失败状态失败 task_id=%s type=%s",
                        task_id,
                        type(persist_exc).__name__,
                    )
            with self._state_lock:
                if str(self._task.get("id") or "") == task_id:
                    self._task.update({
                        "status": "failed",
                        "message": "整理任务失败",
                        "error": public_error,
                        "current_source": current_source,
                        "finished_at": finished_at,
                        "stats": dict(aggregate),
                        "source_results": source_results,
                        "notification_sent": notification_sent,
                        "result": structured_result,
                    })
        finally:
            with self._state_lock:
                if (
                    str(self._task.get("id") or "") == task_id
                    and self._task.get("status") in {"completed", "partial", "failed", "stopped"}
                ):
                    self._remember_task_locked(self._task)
                if self._worker is threading.current_thread():
                    self._worker = None
            self._lock.release()
            self._operation_queue_wakeup.set()
            self._wake_download_tracker()

    @staticmethod
    def _wake_download_tracker() -> None:
        try:
            from app.modules.download_tracker import get_download_tracker
            get_download_tracker().reload()
        except Exception:
            logger.debug("整理锁释放后唤醒下载跟踪器失败", exc_info=True)

    @staticmethod
    def _cleanup_download_staging(organizer: Organizer, request_ids: list[int], sources: list[dict[str, str]]) -> None:
        source_ids = {str(source.get("id") or "") for source in sources}
        for request_id in request_ids:
            row = db.get_download_request(int(request_id))
            if row is None or not int(row["gy_isolated"] or 0):
                continue
            staging_id = str(row["gy_target_dir"] or "")
            if not staging_id or staging_id not in source_ids:
                continue
            try:
                remaining = organizer.client.list_dir(staging_id)
                if remaining:
                    names = [str(getattr(item, "name", "") or "未命名") for item in remaining]
                    preview = "、".join(names[:5])
                    if len(names) > 5:
                        preview += f" 等 {len(names)} 项"
                    db.update_download_request(
                        int(request_id), gy_staging_cleanup_status="retained",
                        gy_staging_cleanup_error=(
                            f"隔离目录仍有 {len(names)} 项未整理或未识别：{preview}"
                        ),
                    )
                    continue
                info = organizer.client.file_info(staging_id)
                expected_parent = str(row["gy_staging_parent_dir"] or "")
                expected_name = str(row["gy_staging_name"] or "")
                if (
                    info is None
                    or not bool(getattr(info, "is_dir", False))
                    or not expected_parent
                    or not expected_name
                    or str(getattr(info, "parent_id", "") or "") != expected_parent
                    or str(getattr(info, "name", "") or "") != expected_name
                ):
                    db.update_download_request(
                        int(request_id), gy_staging_cleanup_status="retained",
                        gy_staging_cleanup_error=(
                            "隔离目录身份与请求记录不一致，目录已保留"
                        ),
                    )
                    continue
                expected_etag = str(getattr(info, "etag", "") or "")
                try:
                    expected_updated_at = max(
                        0, int(getattr(info, "updated_at", 0) or 0)
                    )
                except (TypeError, ValueError):
                    expected_updated_at = 0
                delete_empty = getattr(
                    organizer.client, "delete_empty_directory", None
                )
                supports_guarded = getattr(
                    organizer.client, "supports_guarded_empty_directory_delete", None
                )
                if supports_guarded is None:
                    supports_guarded = getattr(
                        organizer.client, "supports_atomic_empty_directory_delete", None
                    )
                if (
                    not callable(delete_empty)
                    or supports_guarded is False
                    or (not expected_etag and not expected_updated_at)
                ):
                    db.update_download_request(
                        int(request_id), gy_staging_cleanup_status="retained",
                        gy_staging_cleanup_error=(
                            "Provider 不支持带版本与空目录复核的回收站删除，隔离目录已保留"
                        ),
                    )
                    continue
                execute_recycle_bin_delete(
                    organizer.client,
                    trigger="download_staging_cleanup",
                    reason="下载自动整理完成后清理空隔离目录",
                    candidate=DeleteCandidate(
                        file_id=staging_id,
                        name=(getattr(info, "name", "") or str(row["gy_staging_name"] or "下载隔离目录")),
                        parent_id=(str(getattr(info, "parent_id", "") or row["gy_staging_parent_dir"] or "0")),
                    ),
                    safe_failure_message="清理下载隔离目录失败，目录已保留",
                    delete_operation=lambda current_id=staging_id,
                    current_etag=expected_etag,
                    current_updated_at=expected_updated_at: delete_empty(
                        current_id,
                        expected_etag=current_etag,
                        expected_updated_at=current_updated_at,
                    ),
                )
                db.update_download_request(
                    int(request_id), gy_staging_cleanup_status="completed",
                    gy_staging_cleanup_error="",
                )
            except Exception as exc:
                db.update_download_request(
                    int(request_id), gy_staging_cleanup_status="failed",
                    gy_staging_cleanup_error=f"{type(exc).__name__}: {str(exc)[:300]}",
                )
                logger.warning(
                    "下载隔离目录清理失败 request=%s staging=%s type=%s",
                    request_id, staging_id, type(exc).__name__,
                )


_manager = OrganizeTaskManager()


def get_organize_manager() -> OrganizeTaskManager:
    return _manager
