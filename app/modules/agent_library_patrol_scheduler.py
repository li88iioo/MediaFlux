"""Agent 全库缺集巡检的持久化只读调度器。"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import threading
import time
from typing import Callable

from app import config, database as db
from app.agent.models import ToolResult
from app.agent.library_patrol_status import serialize_patrol_projection
from app.agent.library_patrol_progress import (
    finalize_patrol_progress,
    load_patrol_progress,
    merge_patrol_progress,
)
from app.modules.agent_library_patrol_notifications import (
    build_patrol_result_fingerprint,
    load_patrol_notification_payload,
    send_library_patrol_notification,
    serialize_patrol_notification_payload,
)
from app.logger import get_logger

logger = get_logger(__name__)

# 审计执行器自身有 30 秒总 deadline；5 分钟租约为卡死/进程异常兜底。
_RUNNING_LEASE_SECONDS = 5 * 60
_RETRY_DELAYS = (15 * 60, 60 * 60)
_NOTIFICATION_LEASE_SECONDS = 2 * 60
_NOTIFICATION_RETRY_DELAYS = (60, 5 * 60, 30 * 60, 6 * 60 * 60, 24 * 60 * 60)
_ALLOWED_OUTCOMES = {
    "updates_available",
    "up_to_date",
    "inconclusive",
    "not_configured",
    "unavailable",
}


class AgentLibraryPatrolScheduler:
    """周期执行 read-only 全库巡检并持久化安全摘要。"""

    def __init__(
        self,
        *,
        audit_executor: Callable[[dict], tuple[ToolResult, int]] | None = None,
        notification_sender: Callable[[dict], bool] | None = None,
        interval: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._audit_executor = audit_executor or self._default_audit_executor
        self._notification_sender = (
            notification_sender or send_library_patrol_notification
        )
        self.interval = max(0.1, float(interval))
        self._clock = clock or datetime.now
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        # 配置重载、outbox 入队和实际外发共用同一栅栏。关闭通知时，
        # reload() 会等待已开始的发送结束，并保证返回后不会再启动旧发送。
        self._notification_gate = threading.RLock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="agent-library-patrol",
            daemon=True,
        )
        self._thread.start()
        logger.info("Agent 全库缺集巡检调度器已启动")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if not thread or not thread.is_alive():
            self._thread = None

    def reload(self, *, immediate: bool = True) -> None:
        current = self._now()
        if self._enabled():
            next_run_at = current if immediate else self._after(self._interval_seconds())
            db.reschedule_agent_library_patrol(next_run_at=next_run_at)
        else:
            db.cancel_agent_library_patrol_lease(next_run_at=current)
        with self._notification_gate:
            if not self._notifications_enabled():
                db.discard_agent_library_patrol_notifications()
        self._wake_event.set()

    def trigger_now(self) -> dict[str, object]:
        """按当前策略将巡检排到现在；不修改配置，也不取消运行租约。"""
        if not self._enabled():
            return {"ok": False, "status": "disabled"}
        current = self._now()
        queued = db.reschedule_agent_library_patrol(next_run_at=current)
        row = db.get_agent_library_patrol()
        task_status = str(row["status"] or "") if row is not None else ""
        self._wake_event.set()
        if queued:
            return {"ok": True, "status": "queued", "task_status": task_status}
        if task_status == "running":
            return {"ok": True, "status": "already_running", "task_status": task_status}
        return {"ok": False, "status": "unavailable", "task_status": task_status}

    def status(self) -> dict[str, object]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "enabled": self._enabled(),
            "interval_seconds": self.interval,
        }

    def run_once(self) -> int:
        if not self._enabled():
            return 0
        current = self._now()
        db.ensure_agent_library_patrol(next_run_at=current)
        stale_before = self._format(
            self._clock() - timedelta(seconds=_RUNNING_LEASE_SECONDS)
        )
        job = db.claim_due_agent_library_patrol(
            current_time=current,
            stale_before=stale_before,
        )
        if job is None:
            return 0
        # 配置可能在领取租约后被关闭；执行前再次确认并使本租约失效。
        if not self._enabled():
            db.cancel_agent_library_patrol_lease(
                next_run_at=current,
                expected_lease_generation=int(job["lease_generation"]),
            )
            return 0
        try:
            self._process(job, current=current)
        except Exception as exc:
            logger.warning("Agent 全库缺集巡检失败 type=%s", type(exc).__name__)
            self._complete_failure(job, current=current, error_type=type(exc).__name__)
        return 1

    def _process(self, job, *, current: str) -> None:
        cycle_as_of = str(job["cycle_as_of"] or "")
        as_of = cycle_as_of or self._clock().date().isoformat()
        arguments = {
            "as_of": as_of,
            "max_series": self._max_series(),
        }
        cursor = str(job["cycle_cursor_tmdb_id"] or "")
        if cursor:
            arguments["after_tmdb_id"] = cursor
        result, _elapsed_ms = self._audit_executor(arguments)
        _batch_json, batch_projection = serialize_patrol_projection(result)
        previous = load_patrol_progress(job["cycle_accumulator_json"], as_of=as_of)
        merged = merge_patrol_progress(previous, batch_projection)
        continuation = bool(
            isinstance(result.data, dict)
            and result.data.get("continuation_pending")
        )
        if continuation:
            next_cursor = str(result.data.get("last_processed_tmdb_id") or "").strip()
            stalled_tmdb_id = str(result.data.get("stalled_tmdb_id") or "").strip()
            current_cursor_value = int(cursor or 0)
            next_cursor_value = int(next_cursor or 0)
            stored_stalls = max(0, int(job["cycle_stall_attempts"] or 0))
            if next_cursor_value > current_cursor_value:
                stall_attempts = 0
            elif (
                next_cursor_value == current_cursor_value
                and stalled_tmdb_id.isascii()
                and stalled_tmdb_id.isdigit()
                and int(stalled_tmdb_id) > current_cursor_value
            ):
                stall_attempts = stored_stalls + 1
                if stall_attempts >= 3:
                    # 同一条目连续三批都被 deadline/请求预算卡住时，不再让整轮
                    # 巡检永久自旋；将该条目标记为不可判定并从下一条继续。
                    merged["checked_series_count"] += 1
                    merged["inconclusive_count"] += 1
                    next_cursor = str(int(stalled_tmdb_id))
                    stall_attempts = 0
                else:
                    next_cursor = cursor
            else:
                raise RuntimeError("PatrolCursorDidNotAdvance")
            accumulator_json = json.dumps(
                merged, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            updated = db.continue_agent_library_patrol(
                expected_lease_generation=int(job["lease_generation"]),
                next_run_at=current,
                cycle_as_of=as_of,
                cycle_cursor_tmdb_id=next_cursor,
                cycle_accumulator_json=accumulator_json,
                cycle_stall_attempts=stall_attempts,
                cycle_started_at=str(job["cycle_started_at"] or current),
            )
            if not updated:
                logger.info("Agent 全库缺集巡检批次进度已被更新租约丢弃")
            return

        outcome = str(result.status or "").strip()
        if outcome not in _ALLOWED_OUTCOMES:
            outcome = "failed"
        projection = finalize_patrol_progress(
            merged, terminal_status=outcome, resumed=bool(cursor)
        )
        persisted_outcome = (
            outcome if outcome in {"failed", "not_configured", "unavailable"}
            else projection["patrol_status"]
        )
        projection_json = json.dumps(
            projection, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        attempts = max(0, int(job["attempts"] or 0))
        complete = persisted_outcome in {"updates_available", "up_to_date"}
        if complete:
            status = "pending"
            attempts = 0
            next_run_at = self._after(self._interval_seconds())
        else:
            attempts += 1
            if attempts <= len(_RETRY_DELAYS):
                status = "retry_wait"
                next_run_at = self._after(_RETRY_DELAYS[attempts - 1])
            else:
                status = "pending"
                attempts = 0
                next_run_at = self._after(self._interval_seconds())
        result_fingerprint = None
        notification_payload_json = ""
        if complete:
            result_fingerprint = build_patrol_result_fingerprint(projection)
            notification_payload_json = serialize_patrol_notification_payload(projection)
        with self._notification_gate:
            updated = db.update_agent_library_patrol(
                status=status,
                outcome=persisted_outcome,
                attempts=attempts,
                next_run_at=next_run_at,
                expected_lease_generation=int(job["lease_generation"]),
                as_of=projection["as_of"],
                checked_series_count=projection["checked_series_count"],
                updates_available_count=projection["updates_available_count"],
                missing_episode_count=projection["missing_episode_count"],
                inconclusive_count=projection["inconclusive_count"],
                unmapped_series_count=projection["unmapped_series_count"],
                projection_json=projection_json,
                findings_truncated=projection["findings_truncated"],
                last_finished_at=current,
                result_fingerprint=result_fingerprint,
                notification_payload_json=notification_payload_json,
                enqueue_notification=(complete and self._notifications_enabled()),
            )
        if not updated:
            logger.info("Agent 全库缺集巡检结果已被更新租约丢弃")

    def _complete_failure(self, job, *, current: str, error_type: str) -> None:
        attempts = max(0, int(job["attempts"] or 0)) + 1
        if attempts <= len(_RETRY_DELAYS):
            status = "retry_wait"
            next_run_at = self._after(_RETRY_DELAYS[attempts - 1])
        else:
            status = "pending"
            attempts = 0
            next_run_at = self._after(self._interval_seconds())
        if (
            str(job["cycle_as_of"] or "").strip()
            and db.retry_agent_library_patrol_cycle(
                expected_lease_generation=int(job["lease_generation"]),
                status=status,
                attempts=attempts,
                next_run_at=next_run_at,
                error_type=error_type,
            )
        ):
            return
        failed = ToolResult(False, "failed", "自动缺集巡检未完成")
        projection_json, projection = serialize_patrol_projection(failed)
        updated = db.update_agent_library_patrol(
            status=status,
            outcome="failed",
            attempts=attempts,
            next_run_at=next_run_at,
            expected_lease_generation=int(job["lease_generation"]),
            projection_json=projection_json,
            findings_truncated=projection["findings_truncated"],
            error_type=error_type,
            last_finished_at=current,
        )
        if not updated:
            logger.info("Agent 全库缺集巡检失败结果已被更新租约丢弃")

    def dispatch_notification_once(self) -> int:
        """发送一条到期 outbox；失败只更新重试状态，不影响巡检结果。"""
        with self._notification_gate:
            if not self._notifications_enabled():
                db.discard_agent_library_patrol_notifications()
                return 0
            current = self._now()
            stale_before = self._format(
                self._clock() - timedelta(seconds=_NOTIFICATION_LEASE_SECONDS)
            )
            item = db.claim_due_agent_library_patrol_notification(
                current_time=current,
                stale_before=stale_before,
            )
            if item is None:
                return 0
            notification_id = int(item["id"])
            generation = int(item["lease_generation"])
            # claim 与 sender 之间再次读取配置，覆盖外部配置更新或测试替身
            # 在领取瞬间关闭通知的情况。
            if not self._notifications_enabled():
                db.discard_agent_library_patrol_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    error_type="NotificationsDisabled",
                )
                return 0
            try:
                projection = load_patrol_notification_payload(item["payload_json"])
            except ValueError:
                db.discard_agent_library_patrol_notification(
                    notification_id,
                    expected_lease_generation=generation,
                    error_type="InvalidPayload",
                )
                return 1
            try:
                sent = bool(self._notification_sender(projection))
                if sent:
                    db.complete_agent_library_patrol_notification(
                        notification_id,
                        expected_lease_generation=generation,
                        sent_at=current,
                    )
                else:
                    self._retry_notification(item, error_type="DeliveryFailed")
            except Exception as exc:
                logger.warning(
                    "Agent 全库缺集巡检通知发送失败 type=%s",
                    type(exc).__name__,
                )
                self._retry_notification(item, error_type=type(exc).__name__)
            return 1

    def _retry_notification(self, item, *, error_type: str) -> None:
        attempts = max(0, int(item["attempts"] or 0))
        delay = _NOTIFICATION_RETRY_DELAYS[
            min(attempts, len(_NOTIFICATION_RETRY_DELAYS) - 1)
        ]
        db.retry_agent_library_patrol_notification(
            int(item["id"]),
            expected_lease_generation=int(item["lease_generation"]),
            next_attempt_at=self._after(delay),
            error_type=error_type,
        )

    @staticmethod
    def _enabled() -> bool:
        return config.get_bool("AGENT_LIBRARY_PATROL_ENABLED", False)

    @staticmethod
    def _notifications_enabled() -> bool:
        return config.get_bool("AGENT_LIBRARY_PATROL_NOTIFY_ENABLED", False)

    @staticmethod
    def _max_series() -> int:
        return max(1, min(config.get_int("AGENT_LIBRARY_PATROL_MAX_SERIES", 50), 100))

    @staticmethod
    def _interval_seconds() -> int:
        hours = max(1, min(config.get_int("AGENT_LIBRARY_PATROL_INTERVAL_HOURS", 24), 168))
        return hours * 60 * 60

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
                for _index in range(3):
                    if self.dispatch_notification_once() == 0:
                        break
            except Exception as exc:
                logger.warning("Agent 全库缺集巡检轮询失败 type=%s", type(exc).__name__)
            self._wake_event.wait(timeout=self.interval)
            self._wake_event.clear()


_scheduler = AgentLibraryPatrolScheduler()


def get_agent_library_patrol_scheduler() -> AgentLibraryPatrolScheduler:
    return _scheduler
