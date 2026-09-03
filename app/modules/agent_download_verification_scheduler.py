"""Agent 下载完成后的持久化媒体库复核调度器。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta

from app import database as db
from app.agent.episode_audit import invalidate_episode_audit_cache
from app.agent.feature_gate import (
    AgentRuntimeDisabled,
    agent_runtime_effect_admission,
    agent_runtime_generation_is_current,
    current_agent_runtime_generation,
)
from app.agent.models import ToolResult
from app.agent.owner_routes import (
    parse_telegram_owner_route,
    telegram_owner_route_is_currently_authorized,
)
from app.agent.recent_download_submissions import (
    RecentDownloadSubmission,
    RecentDownloadVerification,
    build_recent_download_library_verification,
    build_recent_download_status,
)
from app.logger import get_logger
from app.modules.agent_download_verification_notifications import (
    dump_download_verification_payload,
    load_download_verification_payload,
    notify_download_verification_terminal_result,
)
from app.notifier import TelegramSendResult

logger = get_logger(__name__)

_ACTIVE_PHASES = {
    "pending",
    "submitting",
    "submitted",
    "downloading",
    "post_processing",
    "partial_in_progress",
    "accepted",
}
_RETRY_DELAYS = (60, 180, 300, 600, 900)
_RUNNING_LEASE_SECONDS = 30 * 60
_VERIFICATION_LEASE_HEARTBEAT_SECONDS = 60.0
_NOTIFICATION_LEASE_SECONDS = 5 * 60
_NOTIFICATION_RETRY_DELAYS = (60, 180, 600, 1800, 3600)
_NOTIFICATION_MAX_ATTEMPTS = len(_NOTIFICATION_RETRY_DELAYS)
_HISTORY_RETENTION_DAYS = 7
_HISTORY_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
_HISTORY_CLEANUP_RETRY_SECONDS = 5 * 60
_HISTORY_CLEANUP_BATCH_SIZE = 500


class DownloadLibraryVerificationScheduler:
    """原子领取并执行已确认缺集下载的自动媒体库复核。"""

    def __init__(
        self,
        *,
        audit_executor: Callable[[dict], tuple[ToolResult, int]] | None = None,
        interval: float = 30.0,
        max_attempts: int = 5,
        clock: Callable[[], datetime] | None = None,
        terminal_notifier: Callable[..., object] | None = None,
    ) -> None:
        self._audit_executor = audit_executor or self._default_audit_executor
        self._terminal_notifier = (
            terminal_notifier or notify_download_verification_terminal_result
        )
        self._terminal_notifier_uses_request_id = terminal_notifier is None
        self._notification_enabled_override = terminal_notifier is not None
        self.interval = max(0.1, float(interval))
        self.max_attempts = max(1, min(int(max_attempts), len(_RETRY_DELAYS)))
        self._clock = clock or datetime.now
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_cleanup_at: datetime | None = None
        self._notification_gate = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="agent-download-library-verification",
            daemon=True,
        )
        self._thread.start()
        logger.info("Agent 下载后媒体库复核调度器已启动")

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        stopped = not thread or not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def reload(self) -> None:
        with self._notification_gate:
            if not self._notifications_enabled():
                db.discard_agent_download_verification_notifications()
        self._wake_event.set()

    def status(self) -> dict[str, object]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "interval_seconds": self.interval,
            "max_attempts": self.max_attempts,
            "notifications_enabled": self._notifications_enabled(),
        }

    def _runtime_allows(self, generation: int) -> bool:
        return bool(
            not self._stop_event.is_set()
            and agent_runtime_generation_is_current(generation)
        )

    @staticmethod
    def _release_verification_job(
        job,
        *,
        current: str,
        attempts: int | None = None,
        last_checked_at=None,
    ) -> bool:
        """无损释放尚未发布终态的核验租约。"""
        return db.update_agent_download_verification(
            int(job["request_id"]),
            status="pending",
            result=str(job["result"] or ""),
            attempts=int(job["attempts"] or 0)
            if attempts is None
            else max(0, int(attempts)),
            next_check_at=current,
            last_checked_at=(
                job["last_checked_at"] if last_checked_at is None else last_checked_at
            ),
            expected_lease_generation=int(job["lease_generation"]),
        )

    def run_once(self, *, limit: int = 10) -> int:
        runtime_generation = current_agent_runtime_generation()
        if not self._runtime_allows(runtime_generation):
            return 0
        self._maybe_purge_expired_history()
        processed = 0
        for _ in range(max(1, min(int(limit), 100))):
            if not self._runtime_allows(runtime_generation):
                break
            current = self._now()
            stale_before = self._format(
                self._clock() - timedelta(seconds=_RUNNING_LEASE_SECONDS)
            )
            job = db.claim_due_agent_download_verification(
                current_time=current,
                stale_before=stale_before,
            )
            if job is None:
                break
            if not self._runtime_allows(runtime_generation):
                self._release_verification_job(job, current=current)
                break
            try:
                self._process(
                    job,
                    current=current,
                    runtime_generation=runtime_generation,
                )
            except Exception as exc:
                logger.warning(
                    "Agent 下载后媒体库复核任务恢复 type=%s",
                    type(exc).__name__,
                )
                if self._runtime_allows(runtime_generation):
                    self._recover_claimed_job(job, current=current)
                else:
                    self._release_verification_job(job, current=current)
            processed += 1
        for _ in range(max(1, min(int(limit), 10))):
            if (
                not self._runtime_allows(runtime_generation)
                or self.dispatch_notification_once(
                    runtime_generation=runtime_generation
                )
                == 0
            ):
                break
        return processed

    def _maybe_purge_expired_history(self) -> None:
        current = self._clock()
        if self._next_cleanup_at is not None and current < self._next_cleanup_at:
            return
        self._next_cleanup_at = current + timedelta(
            seconds=_HISTORY_CLEANUP_INTERVAL_SECONDS
        )
        cutoff = self._format(current - timedelta(days=_HISTORY_RETENTION_DAYS))
        next_cleanup = self._format(self._next_cleanup_at)
        private_maintainers = (
            (
                "Agent 光鸭私有计划",
                "app.modules.guangya_rename",
                "maintain_rename_plans",
            ),
            (
                "Agent 光鸭残留清理私有计划",
                "app.modules.guangya_residual_cleanup",
                "maintain_cleanup_plans",
            ),
            (
                "Agent 光鸭观察快照",
                "app.modules.guangya_workspace",
                "maintain_workspace_observations",
            ),
            (
                "Agent 光鸭通用变更私有计划",
                "app.modules.guangya_fs_change",
                "maintain_fs_change_plans",
            ),
        )
        for label, module_name, function_name in private_maintainers:
            try:
                module = __import__(module_name, fromlist=[function_name])
                cleanup = getattr(module, function_name)()
                if int(cleanup.get("removed") or 0) > 0:
                    logger.info(
                        "%s已清理 plans=%s remaining=%s bytes=%s",
                        label,
                        int(cleanup.get("removed") or 0),
                        int(cleanup.get("remaining") or 0),
                        int(cleanup.get("bytes") or 0),
                    )
            except Exception as exc:
                logger.warning("%s清理失败 type=%s", label, type(exc).__name__)
        try:
            deleted = db.purge_expired_agent_task_history(
                current_time=self._format(current),
                next_cleanup_at=next_cleanup,
                terminal_before=cutoff,
                limit_per_table=_HISTORY_CLEANUP_BATCH_SIZE,
            )
        except Exception as exc:
            self._next_cleanup_at = current + timedelta(
                seconds=_HISTORY_CLEANUP_RETRY_SECONDS
            )
            logger.warning(
                "Agent 持久任务历史清理失败 type=%s",
                type(exc).__name__,
            )
            return
        if not bool(deleted.get("performed")):
            try:
                self._next_cleanup_at = datetime.strptime(
                    str(deleted.get("next_cleanup_at") or ""),
                    "%Y-%m-%d %H:%M:%S",
                )
            except ValueError:
                self._next_cleanup_at = current + timedelta(
                    seconds=_HISTORY_CLEANUP_RETRY_SECONDS
                )
            return
        total = sum(
            max(0, int(deleted.get(key) or 0))
            for key in (
                "download_verifications",
                "download_verification_notification_outbox",
                "patrol_notification_outbox",
            )
        )
        if total:
            logger.info("Agent 持久任务历史已清理 rows=%s", total)

    def _process(self, job, *, current: str, runtime_generation: int) -> None:
        request_id = int(job["request_id"])
        attempts = max(0, int(job["attempts"] or 0))
        result = str(job["result"] or "")
        if not self._runtime_allows(runtime_generation):
            self._release_verification_job(job, current=current)
            return
        request = db.get_download_request(request_id)
        if not self._runtime_allows(runtime_generation):
            self._release_verification_job(job, current=current)
            return
        if request is None:
            self._finish(
                job,
                status="attention",
                result=result,
                attempts=attempts,
                current=current,
            )
            return

        target = str(request["targets"] or "").strip()
        if target not in {"qb", "guangya", "both"}:
            self._finish(
                job,
                status="attention",
                result=result,
                attempts=attempts,
                current=current,
            )
            return

        record = self._record(job, target=target)
        status_result = build_recent_download_status(record, position=1)
        if not self._runtime_allows(runtime_generation):
            self._release_verification_job(job, current=current)
            return
        status_data = status_result.data if isinstance(status_result.data, dict) else {}
        phase = str(status_data.get("phase") or "unknown")
        if phase in _ACTIVE_PHASES:
            db.update_agent_download_verification(
                request_id,
                status="pending",
                result=result,
                attempts=attempts,
                next_check_at=self._after(self.interval),
                last_checked_at=job["last_checked_at"],
                expected_lease_generation=int(job["lease_generation"]),
            )
            return
        if phase != "completed":
            self._finish(
                job,
                status="attention",
                result=result,
                attempts=attempts,
                current=current,
            )
            return

        if attempts == 0 and not job["last_checked_at"]:
            # 下载 tracker 会先提交完成态，再关联本地导入/云盘整理。等待一个
            # 完整轮询周期，避免在两次事务之间把“下载完成”误判为“入库完成”。
            db.update_agent_download_verification(
                request_id,
                status="pending",
                result=result,
                attempts=attempts,
                next_check_at=self._after(self.interval),
                last_checked_at=current,
                expected_lease_generation=int(job["lease_generation"]),
            )
            return

        verification = record.verification
        assert verification is not None
        arguments = {
            "query": verification.title,
            "tmdb_id": verification.tmdb_id,
            "season": verification.season,
            "target_episode": verification.episode,
            "as_of": verification.as_of,
        }
        if verification.library_name:
            arguments["library_name"] = verification.library_name
        if not self._runtime_allows(runtime_generation):
            self._release_verification_job(job, current=current)
            return
        attempts += 1
        # 在调用媒体服务前先持久化尝试次数；即使进程中止，重启后的
        # running 恢复也不会绕过最大尝试预算。
        claimed = db.update_agent_download_verification(
            request_id,
            status="running",
            result=result,
            attempts=attempts,
            next_check_at=current,
            last_checked_at=current,
            expected_lease_generation=int(job["lease_generation"]),
        )
        if not claimed:
            return
        try:
            with self._lease_heartbeat(
                request_id,
                int(job["lease_generation"]),
            ):
                invalidate_episode_audit_cache(arguments)
                audit, _elapsed_ms = self._audit_executor(arguments)
            projected = build_recent_download_library_verification(
                record,
                audit,
                position=1,
            )
            projected_data = projected.data if isinstance(projected.data, dict) else {}
            verification_result = str(
                projected_data.get("verification") or "inconclusive"
            )
            if verification_result not in {"visible", "missing", "inconclusive"}:
                verification_result = "inconclusive"
        except Exception as exc:
            logger.warning(
                "Agent 下载后媒体库复核失败 request_id=%s type=%s",
                request_id,
                type(exc).__name__,
            )
            verification_result = "inconclusive"

        if not self._runtime_allows(runtime_generation):
            self._release_verification_job(
                job,
                current=current,
                attempts=max(0, attempts - 1),
                last_checked_at=job["last_checked_at"],
            )
            return

        if verification_result == "visible":
            self._finish(
                job,
                status="visible",
                result="visible",
                attempts=attempts,
                current=current,
            )
            return
        if attempts >= self.max_attempts:
            self._finish(
                job,
                status="attention",
                result=verification_result,
                attempts=attempts,
                current=current,
            )
            return
        db.update_agent_download_verification(
            request_id,
            status="retry_wait",
            result=verification_result,
            attempts=attempts,
            next_check_at=self._after(_RETRY_DELAYS[attempts - 1]),
            last_checked_at=current,
            expected_lease_generation=int(job["lease_generation"]),
        )

    @contextmanager
    def _lease_heartbeat(self, request_id: int, generation: int):
        """长审计期间续期，避免第二个 worker 把任务当作过期任务重领。"""
        stopped = threading.Event()

        def renew_loop() -> None:
            while not stopped.wait(_VERIFICATION_LEASE_HEARTBEAT_SECONDS):
                try:
                    if not db.renew_agent_download_verification_lease(
                        request_id,
                        expected_lease_generation=generation,
                    ):
                        break
                except Exception as exc:
                    logger.warning(
                        "Agent 下载后媒体库复核租约续期失败 type=%s",
                        type(exc).__name__,
                    )
                    break

        thread = threading.Thread(
            target=renew_loop,
            name=f"agent-download-verification-heartbeat-{request_id}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1.0)

    def _recover_claimed_job(self, job, *, current: str) -> None:
        request_id = int(job["request_id"])
        try:
            lease_generation = int(job["lease_generation"] or 0)
            latest = db.get_agent_download_verification(request_id)
            if (
                latest is not None
                and int(latest["lease_generation"] or 0) != lease_generation
            ):
                return
            current_job = latest or job
            attempts = max(0, int(current_job["attempts"] or 0))
            result = str(current_job["result"] or "")
            terminal = attempts >= self.max_attempts
            normalized_result = (
                result if result in {"", "visible", "missing", "inconclusive"} else ""
            )
            if terminal:
                self._finish(
                    current_job,
                    status="attention",
                    result=normalized_result,
                    attempts=attempts,
                    current=current,
                )
            else:
                db.update_agent_download_verification(
                    request_id,
                    status="retry_wait",
                    result=normalized_result,
                    attempts=attempts,
                    next_check_at=self._after(self.interval),
                    last_checked_at=current,
                    expected_lease_generation=lease_generation,
                )
        except Exception as exc:
            # 行仍保持 running；30 分钟 lease 到期后可由同进程或其他进程重领。
            logger.warning(
                "Agent 下载后媒体库复核任务延迟恢复 type=%s",
                type(exc).__name__,
            )

    def _finish(
        self,
        job,
        *,
        status: str,
        result: str,
        attempts: int,
        current: str,
    ) -> None:
        request_id = int(job["request_id"])
        payload_json = dump_download_verification_payload(
            title=str(job["title"]),
            season=int(job["season"]),
            episode=int(job["episode"]),
            status=status,
            result=result,
            attempts=attempts,
        )
        finished = db.finish_agent_download_verification(
            request_id,
            status=status,
            result=result,
            attempts=attempts,
            next_check_at=current,
            last_checked_at=current if attempts else None,
            expected_lease_generation=int(job["lease_generation"]),
            payload_json=payload_json,
        )
        if not finished:
            return
        try:
            from app.agent.missing_media_workflows import (
                SQLiteMissingMediaWorkflowRepository,
            )

            SQLiteMissingMediaWorkflowRepository().finish_verification(
                request_id=request_id,
                status=status,
                result=result,
            )
        except Exception as exc:
            # 下载核验终态已原子落库；补库进度镜像失败不得破坏调度器语义。
            logger.warning(
                "Agent 补库工作流同步核验终态失败 type=%s",
                type(exc).__name__,
            )

    def dispatch_notification_once(
        self, *, runtime_generation: int | None = None
    ) -> int:
        """投递一条到期通知；失败进入持久退避，不影响核验终态。"""
        generation_guard = (
            current_agent_runtime_generation()
            if runtime_generation is None
            else int(runtime_generation)
        )
        with self._notification_gate:
            if not self._runtime_allows(generation_guard):
                return 0
            if not self._notifications_enabled():
                db.discard_agent_download_verification_notifications()
                return 0
            current = self._now()
            stale_before = self._format(
                self._clock() - timedelta(seconds=_NOTIFICATION_LEASE_SECONDS)
            )
            item = db.claim_due_agent_download_verification_notification(
                current_time=current,
                stale_before=stale_before,
            )
            if item is None:
                return 0
            notification_id = int(item["id"])
            generation = int(item["lease_generation"])
            if not self._runtime_allows(generation_guard):
                db.release_agent_download_verification_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    next_attempt_at=current,
                )
                return 0
            if not self._notifications_enabled():
                db.discard_agent_download_verification_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    error_type="NotificationsDisabled",
                )
                return 0
            try:
                payload = load_download_verification_payload(item["payload_json"])
            except (TypeError, ValueError):
                db.discard_agent_download_verification_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    error_type="InvalidPayload",
                )
                return 1
            owner = str(item["owner"] or "")
            chat_id = str(item["chat_id"] or "").strip()
            route = parse_telegram_owner_route(owner)
            if route is None or not chat_id or route.chat_id != chat_id:
                db.discard_agent_download_verification_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    error_type="MissingRoute"
                    if not owner and not chat_id
                    else "InvalidRoute",
                )
                return 1
            if not telegram_owner_route_is_currently_authorized(
                route,
                chat_id=chat_id,
            ):
                db.discard_agent_download_verification_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    error_type="AuthorizationRevoked",
                )
                return 1
            if not self._runtime_allows(generation_guard):
                db.release_agent_download_verification_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    next_attempt_at=current,
                )
                return 0
            try:
                with agent_runtime_effect_admission(generation_guard):
                    if not self._runtime_allows(generation_guard):
                        db.release_agent_download_verification_notification(
                            notification_id,
                            expected_lease_generation=generation,
                            next_attempt_at=current,
                        )
                        return 0
                    if self._terminal_notifier_uses_request_id:
                        raw_result = self._terminal_notifier(
                            owner=owner,
                            chat_id=chat_id,
                            request_id=int(item["request_id"]),
                            **payload,
                        )
                    else:
                        # 外部注入器沿用既有公开签名，避免插件被内部关联 ID 破坏。
                        raw_result = self._terminal_notifier(
                            owner=owner,
                            chat_id=chat_id,
                            **payload,
                        )
                result = (
                    raw_result
                    if isinstance(raw_result, TelegramSendResult)
                    else TelegramSendResult(
                        ok=bool(raw_result),
                        error="" if raw_result else "DeliveryFailed",
                        status_code=500 if raw_result is False else 0,
                    )
                )
            except AgentRuntimeDisabled:
                db.release_agent_download_verification_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    next_attempt_at=current,
                )
                return 0
            except Exception as exc:
                logger.warning(
                    "Agent 下载后媒体库复核通知失败 type=%s",
                    type(exc).__name__,
                )
                if self._runtime_allows(generation_guard):
                    self._retry_notification(item, error_type=type(exc).__name__)
                else:
                    db.release_agent_download_verification_notification(
                        notification_id,
                        expected_lease_generation=generation,
                        next_attempt_at=current,
                    )
                return 1
            if result.ok:
                db.complete_agent_download_verification_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    sent_at=current,
                )
            elif result.outcome_unknown:
                db.discard_agent_download_verification_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    error_type="DeliveryOutcomeUnknown",
                )
            elif result.error in {
                "NotificationsDisabled",
                "AuthorizationRevoked",
                "InvalidRoute",
            }:
                db.discard_agent_download_verification_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    error_type=result.error,
                )
            else:
                if self._runtime_allows(generation_guard):
                    self._retry_notification(
                        item,
                        error_type="DeliveryFailed",
                        delay_override=result.retry_after_seconds,
                    )
                else:
                    db.release_agent_download_verification_notification(
                        notification_id,
                        expected_lease_generation=generation,
                        next_attempt_at=current,
                    )
            return 1

    def _retry_notification(
        self,
        item,
        *,
        error_type: str,
        delay_override: int = 0,
    ) -> None:
        attempts = max(0, int(item["attempts"] or 0))
        if attempts + 1 >= _NOTIFICATION_MAX_ATTEMPTS:
            db.discard_agent_download_verification_notification(
                int(item["id"]),
                expected_lease_generation=int(item["lease_generation"]),
                error_type=error_type,
            )
            return
        delay = (
            max(0, int(delay_override or 0))
            or _NOTIFICATION_RETRY_DELAYS[
                min(attempts, len(_NOTIFICATION_RETRY_DELAYS) - 1)
            ]
        )
        db.retry_agent_download_verification_notification(
            int(item["id"]),
            expected_lease_generation=int(item["lease_generation"]),
            next_attempt_at=self._after(delay),
            error_type=error_type,
        )

    def _notifications_enabled(self) -> bool:
        if self._notification_enabled_override:
            return True
        from app import config
        from app.modules.telegram_notification_policy import notifications_enabled

        return (
            config.get_bool("AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED", True)
            and notifications_enabled()
        )

    def _record(self, job, *, target: str) -> RecentDownloadSubmission:
        verification = RecentDownloadVerification(
            title=str(job["title"]),
            tmdb_id=str(job["tmdb_id"]),
            season=int(job["season"]),
            episode=int(job["episode"]),
            as_of=str(job["as_of"]),
            library_name=str(job["library_name"] or ""),
        )
        succeeded = ("qb", "guangya") if target == "both" else (target,)
        return RecentDownloadSubmission(
            request_id=int(job["request_id"]),
            target=target,
            dispatch_status="submitted",
            succeeded=succeeded,
            failed=(),
            created=True,
            duplicate=False,
            result_status="accepted",
            captured_at=str(job["created_at"] or ""),
            verification=verification,
        )

    @staticmethod
    def _format(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S")

    def _now(self) -> str:
        return self._format(self._clock())

    def _after(self, seconds: float) -> str:
        return self._format(self._clock() + timedelta(seconds=max(0.0, seconds)))

    @staticmethod
    def _default_audit_executor(arguments: dict) -> tuple[ToolResult, int]:
        from app.agent.library_episode_audit import audit_library_episodes_batch

        started = time.monotonic()
        result = audit_library_episodes_batch(arguments)
        return result, max(0, int((time.monotonic() - started) * 1000))

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                logger.warning(
                    "Agent 下载后媒体库复核轮询失败 type=%s",
                    type(exc).__name__,
                )
            self._wake_event.wait(timeout=self.interval)
            self._wake_event.clear()


_scheduler = DownloadLibraryVerificationScheduler()


def get_download_library_verification_scheduler() -> (
    DownloadLibraryVerificationScheduler
):
    return _scheduler
