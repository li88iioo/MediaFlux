"""Owner 隔离、可恢复、可取消的 Agent 长任务调度器。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import json
import threading
import time
from typing import Any, Callable

from app import database as db
from app.agent.library_patrol_progress import (
    finalize_patrol_progress,
    load_patrol_progress,
    merge_patrol_progress,
)
from app.agent.library_patrol_status import build_persisted_patrol_projection
from app.agent.models import ToolResult
from app.logger import get_logger

logger = get_logger(__name__)

_JOB_TYPE = "library_episode_audit"
_RUNNING_LEASE_SECONDS = 5 * 60
_LEASE_HEARTBEAT_SECONDS = 60.0
_RETRY_DELAYS = (60, 5 * 60, 15 * 60)


class AgentJobsScheduler:
    """执行用户触发的有界只读长任务，并在批次边界响应取消。"""

    def __init__(
        self,
        *,
        audit_executor: Callable[[dict[str, Any]], ToolResult] | None = None,
        interval: float = 2.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._audit_executor = audit_executor or self._default_audit_executor
        self.interval = max(0.1, float(interval))
        self._clock = clock or datetime.now
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="agent-durable-jobs",
            daemon=True,
        )
        self._thread.start()
        logger.info("Agent 持久化长任务调度器已启动")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if not thread or not thread.is_alive():
            self._thread = None

    def wake(self) -> None:
        self._wake_event.set()

    def status(self) -> dict[str, object]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "interval_seconds": self.interval,
        }

    def run_once(self) -> int:
        from app.agent.feature_gate import is_agent_enabled

        if not is_agent_enabled():
            return 0
        current = self._now()
        stale_before = self._format(
            self._clock() - timedelta(seconds=_RUNNING_LEASE_SECONDS)
        )
        job = db.claim_due_agent_job(
            job_type=_JOB_TYPE,
            current_time=current,
            stale_before=stale_before,
        )
        if job is None:
            return 0
        try:
            self._process(job, current=current)
        except Exception as exc:
            logger.warning("Agent 长任务执行失败 type=%s", type(exc).__name__)
            self._retry(job, error_code=type(exc).__name__)
        return 1

    def _process(self, job, *, current: str) -> None:
        job_id = str(job["job_id"])
        generation = int(job["lease_generation"])
        if bool(job["cancel_requested"]) or db.is_agent_job_cancel_requested(
            job_id, expected_lease_generation=generation
        ):
            db.finalize_cancelled_agent_job(
                job_id, expected_lease_generation=generation
            )
            return

        arguments = self._load_input(job["input_json"])
        checkpoint = self._load_checkpoint(
            job["checkpoint_json"], as_of=arguments["as_of"]
        )
        previous = load_patrol_progress(
            job["projection_json"], as_of=arguments["as_of"]
        )
        with self._lease_heartbeat(job_id, generation):
            result = self._audit_executor({
                **arguments,
                "after_tmdb_id": checkpoint["cursor"],
            })
        if not isinstance(result, ToolResult):
            raise TypeError("InvalidAgentJobResult")
        if not result.ok and result.status == "not_configured":
            self._fail_terminal(
                job,
                error_code="NotConfigured",
                summary="后台检查无法开始，请先配置并启用媒体服务器与 TMDB",
            )
            return
        if not result.ok and result.status == "unavailable":
            self._retry(job, error_code="UpstreamUnavailable")
            return
        if not result.ok and result.status != "inconclusive":
            self._retry(job, error_code="AgentJobResultFailed")
            return
        current_projection = build_persisted_patrol_projection(result)
        merged = merge_patrol_progress(previous, current_projection)

        # 取消采用协作式语义：当前有界批次完成后，不再继续下一批。
        if db.is_agent_job_cancel_requested(
            job_id, expected_lease_generation=generation
        ):
            db.finalize_cancelled_agent_job(
                job_id, expected_lease_generation=generation
            )
            return

        continuation = bool(
            isinstance(result.data, dict)
            and result.data.get("continuation_pending")
        )
        if continuation:
            next_cursor, stall_attempts = self._next_cursor(
                result,
                current_cursor=checkpoint["cursor"],
                stored_stalls=checkpoint["stall_attempts"],
                merged=merged,
            )
            checkpoint_json = json.dumps(
                {
                    "as_of": arguments["as_of"],
                    "cursor": next_cursor,
                    "stall_attempts": stall_attempts,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            projection_json = json.dumps(
                merged,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            checked = int(merged["checked_series_count"])
            updated = db.continue_agent_job(
                job_id,
                expected_lease_generation=generation,
                checkpoint_json=checkpoint_json,
                projection_json=projection_json,
                progress_current=checked,
                progress_total=0,
                next_run_at=current,
                summary=f"已检查 {checked} 部剧集，后台将继续下一批",
            )
            if not updated:
                if db.is_agent_job_cancel_requested(
                    job_id, expected_lease_generation=generation
                ):
                    db.finalize_cancelled_agent_job(
                        job_id, expected_lease_generation=generation
                    )
                    logger.info("Agent 长任务在批次提交竞态中完成取消")
                else:
                    logger.info("Agent 长任务批次进度因租约变化被丢弃")
            return

        terminal_status = str(result.status or "inconclusive")
        finalized = finalize_patrol_progress(
            merged,
            terminal_status=terminal_status,
            resumed=bool(checkpoint["cursor"]),
        )
        projection_json = json.dumps(
            finalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        checked = int(finalized["checked_series_count"])
        summary = self._completion_summary(finalized)
        updated = db.complete_agent_job(
            job_id,
            expected_lease_generation=generation,
            projection_json=projection_json,
            progress_current=checked,
            progress_total=checked,
            summary=summary,
            finished_at=current,
        )
        if not updated:
            if db.is_agent_job_cancel_requested(
                job_id, expected_lease_generation=generation
            ):
                db.finalize_cancelled_agent_job(
                    job_id, expected_lease_generation=generation
                )
                logger.info("Agent 长任务在结果提交竞态中完成取消")
            else:
                logger.info("Agent 长任务结果因租约变化被丢弃")

    @staticmethod
    def _next_cursor(
        result: ToolResult,
        *,
        current_cursor: str,
        stored_stalls: int,
        merged: dict[str, Any],
    ) -> tuple[str, int]:
        data = result.data if isinstance(result.data, dict) else {}
        next_cursor = str(data.get("last_processed_tmdb_id") or "").strip()
        stalled_tmdb_id = str(data.get("stalled_tmdb_id") or "").strip()
        current_value = int(current_cursor or 0)
        next_value = int(next_cursor or 0)
        if next_value > current_value:
            return str(next_value), 0
        if (
            next_value == current_value
            and stalled_tmdb_id.isascii()
            and stalled_tmdb_id.isdigit()
            and int(stalled_tmdb_id) > current_value
        ):
            stalls = max(0, int(stored_stalls)) + 1
            if stalls >= 3:
                # 防止单一条目永久阻塞整个用户任务。
                merged["checked_series_count"] += 1
                merged["inconclusive_count"] += 1
                return str(int(stalled_tmdb_id)), 0
            return current_cursor, stalls
        raise RuntimeError("AgentJobCursorDidNotAdvance")

    @contextmanager
    def _lease_heartbeat(self, job_id: str, generation: int):
        """长批次运行期间续期，避免被另一个 worker 当作过期任务重复领取。"""
        stopped = threading.Event()

        def renew_loop() -> None:
            while not stopped.wait(_LEASE_HEARTBEAT_SECONDS):
                try:
                    if not db.renew_agent_job_lease(
                        job_id,
                        expected_lease_generation=generation,
                    ):
                        break
                except Exception as exc:
                    logger.warning(
                        "Agent 长任务租约续期失败 type=%s", type(exc).__name__
                    )
                    break

        thread = threading.Thread(
            target=renew_loop,
            name=f"agent-job-heartbeat-{job_id[-8:]}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1.0)

    def _fail_terminal(self, job, *, error_code: str, summary: str) -> None:
        status = db.fail_or_retry_agent_job(
            str(job["job_id"]),
            expected_lease_generation=int(job["lease_generation"]),
            attempts=max(1, int(job["max_attempts"] or 1)),
            next_run_at=self._now(),
            error_code=error_code,
            summary=summary,
        )
        if status == "stale":
            logger.info("Agent 长任务终止结果因租约变化被丢弃")

    def _retry(self, job, *, error_code: str) -> None:
        attempts = max(0, int(job["attempts"] or 0)) + 1
        delay = _RETRY_DELAYS[min(attempts - 1, len(_RETRY_DELAYS) - 1)]
        status = db.fail_or_retry_agent_job(
            str(job["job_id"]),
            expected_lease_generation=int(job["lease_generation"]),
            attempts=attempts,
            next_run_at=self._after(delay),
            error_code=error_code,
            summary=(
                "后台检查暂时失败，稍后会自动重试"
                if attempts < int(job["max_attempts"] or 1)
                else "后台检查未能完成，请检查媒体服务器与 TMDB 配置后重试"
            ),
        )
        if status == "stale":
            logger.info("Agent 长任务失败结果因租约变化被丢弃")

    @staticmethod
    def _load_input(raw: object) -> dict[str, Any]:
        try:
            value = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("InvalidAgentJobInput") from exc
        if not isinstance(value, dict) or set(value) != {"as_of", "max_series"}:
            raise ValueError("InvalidAgentJobInput")
        as_of = str(value.get("as_of") or "")
        try:
            date.fromisoformat(as_of)
        except ValueError as exc:
            raise ValueError("InvalidAgentJobInput") from exc
        max_series = value.get("max_series")
        if isinstance(max_series, bool) or not isinstance(max_series, int):
            raise ValueError("InvalidAgentJobInput")
        if not 1 <= max_series <= 100:
            raise ValueError("InvalidAgentJobInput")
        return {"as_of": as_of, "max_series": max_series}

    @staticmethod
    def _load_checkpoint(raw: object, *, as_of: str) -> dict[str, Any]:
        try:
            value = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("InvalidAgentJobCheckpoint") from exc
        if not isinstance(value, dict) or set(value) != {
            "as_of", "cursor", "stall_attempts"
        }:
            raise ValueError("InvalidAgentJobCheckpoint")
        cursor = str(value.get("cursor") or "").strip()
        if cursor and (
            not cursor.isascii() or not cursor.isdigit() or len(cursor) > 10
        ):
            raise ValueError("InvalidAgentJobCheckpoint")
        stalls = value.get("stall_attempts")
        if isinstance(stalls, bool) or not isinstance(stalls, int) or not 0 <= stalls <= 2:
            raise ValueError("InvalidAgentJobCheckpoint")
        if value.get("as_of") != as_of:
            raise ValueError("InvalidAgentJobCheckpoint")
        return {
            "as_of": as_of,
            "cursor": str(int(cursor)) if cursor else "",
            "stall_attempts": stalls,
        }

    @staticmethod
    def _completion_summary(projection: dict[str, Any]) -> str:
        checked = int(projection["checked_series_count"])
        updates = int(projection["updates_available_count"])
        missing = int(projection["missing_episode_count"])
        inconclusive = int(projection["inconclusive_count"])
        unmapped = int(projection["unmapped_series_count"])
        if updates:
            suffix = f"；另有 {inconclusive + unmapped} 部需要人工确认" if inconclusive or unmapped else ""
            return f"已检查 {checked} 部剧集，发现 {updates} 部共缺 {missing} 集{suffix}"
        if inconclusive or unmapped:
            return f"已检查 {checked} 部剧集，其中 {inconclusive + unmapped} 部暂时无法确认"
        return f"已检查 {checked} 部剧集，暂未发现已播缺集"

    @staticmethod
    def _default_audit_executor(arguments: dict[str, Any]) -> ToolResult:
        from app.agent.library_episode_audit import audit_library_episodes_batch

        return audit_library_episodes_batch(arguments)

    @staticmethod
    def _format(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S")

    def _now(self) -> str:
        return self._format(self._clock())

    def _after(self, seconds: float) -> str:
        return self._format(self._clock() + timedelta(seconds=max(0.0, seconds)))

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                for _index in range(3):
                    if self.run_once() == 0:
                        break
            except Exception as exc:
                logger.warning("Agent 长任务轮询失败 type=%s", type(exc).__name__)
            self._wake_event.wait(timeout=self.interval)
            self._wake_event.clear()


_scheduler = AgentJobsScheduler()


def get_agent_jobs_scheduler() -> AgentJobsScheduler:
    return _scheduler
