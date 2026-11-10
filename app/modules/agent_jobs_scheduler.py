"""Owner 隔离、可恢复、可取消的 Agent 长任务调度器。"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import asdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Callable

from app.repositories import agent_jobs as agent_jobs_repo
from app.modules.library_patrol import (
    finalize_patrol_progress,
    load_patrol_progress,
    merge_patrol_progress,
)
from app.modules.library_patrol import build_persisted_patrol_projection
from app.agent.feature_gate import (
    agent_runtime_generation_is_current,
    current_agent_runtime_generation,
)
from app.logger import get_logger
from app.modules.episode_audit import EpisodeAuditResult, audit_library_episodes_batch

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
        audit_executor: Callable[[dict[str, Any]], EpisodeAuditResult] | None = None,
        interval: float = 2.0,
        clock: Callable[[], datetime] | None = None,
        continuation_runtime_factory: Callable | None = None,
    ) -> None:
        self._audit_executor = audit_executor or self._default_audit_executor
        self.interval = max(0.1, float(interval))
        self._clock = clock or datetime.now
        self._continuation_runtime_factory = continuation_runtime_factory
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._continuation_thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lifecycle_lock:
            if any(worker and worker.is_alive() for worker in (self._thread, self._continuation_thread)):
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="agent-durable-jobs",
                daemon=True,
            )
            self._thread.start()
        logger.info("Agent 持久化长任务调度器已启动")

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lifecycle_lock:
            self._stop_event.set()
            self._wake_event.set()
            workers = (self._thread, self._continuation_thread)
        deadline = time.monotonic() + max(0.0, float(timeout))
        for worker in workers:
            if worker and worker.is_alive() and worker is not threading.current_thread():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lifecycle_lock:
            for name, worker in zip(("_thread", "_continuation_thread"), workers):
                if getattr(self, name) is worker and worker is not None and not worker.is_alive():
                    setattr(self, name, None)
            return not any(worker and worker.is_alive() for worker in (self._thread, self._continuation_thread))

    def wake(self) -> None:
        self._wake_event.set()

    def status(self) -> dict[str, object]:
        return {
            "running": any(bool(worker and worker.is_alive()) for worker in (self._thread, self._continuation_thread)),
            "interval_seconds": self.interval,
        }

    def run_once(self) -> int:
        from app.agent.feature_gate import is_agent_enabled

        if self._stop_event.is_set() or not is_agent_enabled():
            return 0
        runtime_generation = current_agent_runtime_generation()
        current = self._now()
        stale_before = self._format(
            self._clock() - timedelta(seconds=_RUNNING_LEASE_SECONDS)
        )
        job = agent_jobs_repo.claim_due_agent_job(
            job_type=_JOB_TYPE,
            current_time=current,
            stale_before=stale_before,
        )
        if job is None:
            return 0
        # 关闭可能与 SQLite claim 交错；未开始的批次无损还回持久队列。
        if self._stop_event.is_set() or not agent_runtime_generation_is_current(runtime_generation):
            agent_jobs_repo.release_agent_job_lease(
                str(job["job_id"]),
                expected_lease_generation=int(job["lease_generation"]),
                next_run_at=current,
            )
            return 0
        try:
            self._process(
                job,
                current=current,
                runtime_generation=runtime_generation,
            )
        except Exception as exc:
            logger.warning("Agent 长任务执行失败 type=%s", type(exc).__name__)
            if agent_runtime_generation_is_current(runtime_generation):
                self._retry(job, error_code=type(exc).__name__)
            else:
                agent_jobs_repo.release_agent_job_lease(
                    str(job["job_id"]),
                    expected_lease_generation=int(job["lease_generation"]),
                    next_run_at=current,
                )
        return 1

    def _process(self, job, *, current: str, runtime_generation: int) -> None:
        job_id = str(job["job_id"])
        generation = int(job["lease_generation"])
        if bool(job["cancel_requested"]) or agent_jobs_repo.is_agent_job_cancel_requested(
            job_id, expected_lease_generation=generation
        ):
            agent_jobs_repo.finalize_cancelled_agent_job(
                job_id, expected_lease_generation=generation
            )
            return

        arguments = self._load_input(job["input_json"])
        checkpoint = self._load_checkpoint(
            job["checkpoint_json"], as_of=arguments["as_of"]
        )
        previous = load_patrol_progress(
            job["projection_json"],
            as_of=arguments["as_of"],
            resumed=bool(checkpoint["cursor"]),
        )
        with self._lease_heartbeat(job_id, generation):
            result = self._audit_executor({
                **arguments,
                "after_tmdb_id": checkpoint["cursor"],
            })
        if not agent_runtime_generation_is_current(runtime_generation):
            agent_jobs_repo.release_agent_job_lease(
                job_id,
                expected_lease_generation=generation,
                next_run_at=current,
            )
            return
        if not isinstance(result, EpisodeAuditResult):
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
        current_projection = build_persisted_patrol_projection(asdict(result))
        merged = merge_patrol_progress(previous, current_projection)

        # 取消采用协作式语义：当前有界批次完成后，不再继续下一批。
        if agent_jobs_repo.is_agent_job_cancel_requested(
            job_id, expected_lease_generation=generation
        ):
            agent_jobs_repo.finalize_cancelled_agent_job(
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
            updated = agent_jobs_repo.continue_agent_job(
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
                if agent_jobs_repo.is_agent_job_cancel_requested(
                    job_id, expected_lease_generation=generation
                ):
                    agent_jobs_repo.finalize_cancelled_agent_job(
                        job_id, expected_lease_generation=generation
                    )
                    logger.info("Agent 长任务在批次提交竞态中完成取消")
                else:
                    logger.info("Agent 长任务批次进度因租约变化被丢弃")
            return

        terminal_status = (result.status or "inconclusive")
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
        updated = agent_jobs_repo.complete_agent_job(
            job_id,
            expected_lease_generation=generation,
            projection_json=projection_json,
            progress_current=checked,
            progress_total=checked,
            summary=summary,
            finished_at=current,
        )
        if not updated:
            if agent_jobs_repo.is_agent_job_cancel_requested(
                job_id, expected_lease_generation=generation
            ):
                agent_jobs_repo.finalize_cancelled_agent_job(
                    job_id, expected_lease_generation=generation
                )
                logger.info("Agent 长任务在结果提交竞态中完成取消")
            else:
                logger.info("Agent 长任务结果因租约变化被丢弃")

    @staticmethod
    def _next_cursor(
        result: EpisodeAuditResult,
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
            stalls = max(0, stored_stalls) + 1
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
                    if not agent_jobs_repo.renew_agent_job_lease(
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
        status = agent_jobs_repo.fail_or_retry_agent_job(
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
        status = agent_jobs_repo.fail_or_retry_agent_job(
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
        except (TypeError, ValueError) as exc:
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
        except (TypeError, ValueError) as exc:
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
    def _default_audit_executor(arguments: dict[str, Any]) -> EpisodeAuditResult:
        return audit_library_episodes_batch(arguments)

    @staticmethod
    def _format(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S")

    def _now(self) -> str:
        return self._format(self._clock())

    def _after(self, seconds: float) -> str:
        return self._format(self._clock() + timedelta(seconds=max(0.0, seconds)))

    def _run_due_continuation_once(self) -> int:
        """一个有界的原任务续行；读取等待与后台终态共用执行器。"""
        from dataclasses import asdict

        from app import config
        from app.agent.feature_gate import is_agent_enabled
        from app.agent.kernel.bootstrap import (
            _capture_creation_binding,
            _recheck_creation_binding,
            build_agent_kernel_runtime,
        )
        from app.agent.kernel.persistence import SQLiteKernelStore
        from app.agent.kernel.ports.mediaflux_policy import MediaFluxTurnAdmission
        from app.agent.kernel.state_types import AgentInput
        if self._stop_event.is_set() or not is_agent_enabled():
            return 0

        async def run():
            store = SQLiteKernelStore(clock=lambda: self._clock().timestamp())
            for candidate in await store.due_continuations(limit=16):
                if self._stop_event.is_set():
                    return 0
                if candidate.get('kind') == 'delivery':
                    from app.agent.kernel.continuation import (
                        deliver_continuation_result,
                    )
                    from app.bot.agent_adapter import (
                        prepare_agent_continuation_delivery,
                        publish_agent_continuation_delivery,
                    )
                    if await deliver_continuation_result(
                        store, candidate,
                        prepare_delivery=prepare_agent_continuation_delivery,
                        publish_delivery=publish_agent_continuation_delivery,
                    ):
                        return 1
                    continue
                with config.runtime_config_read_guard():
                    binding = _capture_creation_binding()
                if (candidate['admission_fence'] != asdict(binding.fence)
                        or candidate['effective_digest'] != binding.effective_digest):
                    # The same claim transaction retires only this exact old wait.
                    await store.claim_continuation(candidate, binding)
                    continue
                await MediaFluxTurnAdmission().begin(AgentInput(
                    message="恢复已保存的任务", owner=candidate['owner'], session_id=candidate['session_id'],
                    request_id=candidate['source_request_id'], channel=candidate['channel']))
                with config.use_runtime_config_snapshot(binding.snapshot):
                    # Explicit factory is a trusted embedding/test dependency, never model metadata.
                    if self._continuation_runtime_factory is None:
                        if not config.get_bool('AGENT_LLM_ENABLED', False):
                            return 0
                        runtime = build_agent_kernel_runtime(store=store)
                    else:
                        runtime = self._continuation_runtime_factory(candidate, binding, store)
                with config.runtime_config_read_guard():
                    _recheck_creation_binding(binding)
                if runtime.creation_binding != binding or runtime.session.continuation_binding != binding:
                    return 0
                from app.agent.kernel.state_types import SessionBusyError
                try:
                    resumed = await runtime.session.resume_wait(candidate=candidate, cancelled=self._stop_event.is_set)
                except SessionBusyError:
                    continue  # This session's foreground confirmation must not starve others.
                if resumed:
                    return 1
            return 0
        return asyncio.run(run())

    def _continuation_loop(self) -> None:
        # One bounded worker uses the same claim/_drive executor. Slow model I/O
        # must not suspend completion receipts and durable domain-job polling.
        while not self._stop_event.is_set():
            try:
                self._run_due_continuation_once()
            except Exception as exc:
                logger.warning("Agent 任务续行失败 type=%s", type(exc).__name__)
            self._stop_event.wait(timeout=self.interval)

    def _loop(self) -> None:
        with self._lifecycle_lock:
            if self._stop_event.is_set():
                return
            self._continuation_thread = threading.Thread(
                target=self._continuation_loop, name="agent-continuation", daemon=True,
            )
            self._continuation_thread.start()
        while not self._stop_event.is_set():
            try:
                from app.agent.effect_completion import poll_effect_receipts
                from app.agent.feature_gate import is_agent_enabled
                from app.agent.kernel.persistence import SQLiteKernelStore

                if is_agent_enabled():
                    generation = current_agent_runtime_generation()
                    asyncio.run(poll_effect_receipts(SQLiteKernelStore(), cancelled=lambda: (
                        self._stop_event.is_set() or not agent_runtime_generation_is_current(generation)
                    )))
            except Exception as exc:
                logger.warning("Agent 回执轮询失败 type=%s", type(exc).__name__)
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
