"""Agent owner 隔离、可恢复长任务的状态机与公开语义测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from app import database as db
from app.repositories import agent_jobs as agent_jobs_repo
from app.modules.library_patrol import empty_patrol_projection
from app.modules.library_audit_jobs import (
    audit_job,
    prepare_start_audit,
    start_audit,
)
from app.repositories.agent_jobs import AgentJobError
from app.agent.models import ToolResult
from app.modules.agent_jobs_scheduler import AgentJobsScheduler
from app.modules.episode_audit import EpisodeAuditResult
from tests.support import IsolatedDatabaseTestCase

_AS_OF = "2026-08-09"
_JOB_TYPE = "library_episode_audit"


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _projection_json() -> str:
    return json.dumps(empty_patrol_projection(as_of=_AS_OF), ensure_ascii=False)


def _result(
    *, ok: bool, status: str, checked: int = 0, continuation: bool = False
) -> EpisodeAuditResult:
    return EpisodeAuditResult(
        ok=ok,
        status=status,
        summary=status,
        data={
            "as_of": _AS_OF,
            "max_series": 2,
            "checked_series_count": checked,
            "mapped_series_count": checked,
            "unmapped_series_count": 0,
            "updates_available_count": 0,
            "up_to_date_count": checked if status == "up_to_date" else 0,
            "inconclusive_count": checked if status == "inconclusive" else 0,
            "missing_episode_count": 0,
            "unknown_air_date_count": 0,
            "findings": [],
            "findings_truncated": False,
            "continuation_pending": continuation,
            "last_processed_tmdb_id": "100" if continuation else "",
            "stalled_tmdb_id": "",
        },
        error="temporary" if not ok else "",
    )


class AgentDurableJobTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_jobs")

    def _create(
        self,
        *,
        owner: str = "web-owner-a",
        dedupe_key: str = f"{_AS_OF}:2",
        max_attempts: int = 3,
        job_id: str | None = None,
    ):
        return agent_jobs_repo.create_agent_job(
            owner=owner,
            job_type=_JOB_TYPE,
            dedupe_key=dedupe_key,
            input_json=json.dumps({"as_of": _AS_OF, "max_series": 2}),
            checkpoint_json=json.dumps(
                {"as_of": _AS_OF, "cursor": "", "stall_attempts": 0}
            ),
            projection_json=_projection_json(),
            progress_total=0,
            max_attempts=max_attempts,
            job_id=job_id,
        )

    def test_disabled_feature_gate_does_not_claim_or_execute_jobs(self):
        executor = Mock()
        scheduler = AgentJobsScheduler(audit_executor=executor)
        with (
            patch("app.agent.feature_gate.is_agent_enabled", return_value=False),
            patch("app.repositories.agent_jobs.claim_due_agent_job") as claim,
        ):
            self.assertEqual(scheduler.run_once(), 0)
        claim.assert_not_called()
        executor.assert_not_called()

    def test_owner_isolation_and_no_raw_owner_persistence(self):
        row, created = self._create(owner="private-owner-a")
        self.assertTrue(created)
        assert (agent_jobs_repo.get_agent_job(owner="private-owner-a", job_id=row["job_id"])) is not None
        self.assertIsNone(
            agent_jobs_repo.get_agent_job(owner="private-owner-b", job_id=row["job_id"])
        )
        self.assertEqual(agent_jobs_repo.list_agent_jobs(owner="private-owner-b"), [])
        self.assertIsNone(
            agent_jobs_repo.find_active_agent_job(
                owner="private-owner-b", job_type=_JOB_TYPE, dedupe_key=f"{_AS_OF}:2"
            )
        )
        self.assertNotIn("private-owner-a", repr(dict(row)))

    def test_active_creation_is_idempotent_but_terminal_job_allows_new_run(self):
        first, created = self._create(owner="owner-idempotent")
        self.assertTrue(created)
        duplicate, created = self._create(owner="owner-idempotent")
        self.assertFalse(created)
        self.assertEqual(first["job_id"], duplicate["job_id"])

        cancelled, outcome = agent_jobs_repo.cancel_agent_job(
            owner="owner-idempotent", job_id=str(first["job_id"])
        )
        self.assertEqual(outcome, "cancelled")
        assert cancelled is not None
        self.assertEqual(cancelled["status"], "cancelled")
        second, created = self._create(owner="owner-idempotent")
        self.assertTrue(created)
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_recent_history_survives_volume_until_cold_receipt_delivery(self):
        from app.agent import effect_completion as effects
        from app.agent.completion_observers import _CompletionTracker
        from app.agent.kernel.completion_receipts import EffectCompletionScope
        from app.agent.kernel.persistence import SQLiteKernelStore

        async def exercise():
            scope = {"owner": "history-owner", "session_id": "waiting-session"}
            store = SQLiteKernelStore()
            lease, _ = await store.begin_turn(**scope, request_id="start-audit")
            executor = Mock(return_value=_result(ok=True, status="up_to_date", checked=1))
            scheduler = AgentJobsScheduler(
                audit_executor=executor, clock=lambda: datetime(2099, 1, 1),
            )
            first = ""
            for index in range(52):
                row, _ = self._create(owner=scope["owner"], job_id=f"job_{index:020d}")
                if not first:
                    first = str(row["job_id"])
                    await effects._register_effect_wait(
                        ToolResult(True, "accepted", "后台任务已提交"),
                        _CompletionTracker("agent_job", {"job_id": first}),
                        "test.audit", EffectCompletionScope(store, lease, "first-audit", "web"),
                    )
                    await store.commit(lease)
                self.assertEqual(scheduler.run_once(), 1)

            def due(state):
                state.metadata["effect_waits"]["first-audit"]["next_poll_at"] = 0
                state.metadata["effect_next_poll_at"] = 0

            await store.update_effect_state(**scope, change=due)
            cold = SQLiteKernelStore()
            self.assertEqual(await effects.poll_effect_receipts(cold), 1)
            state = await cold.load(**scope)
            receipt = state.metadata["effect_waits"]["first-audit"]
            self.assertEqual(receipt["state"], "terminal")
            self.assertEqual(receipt["final_result"]["status"], "up_to_date")
            self.assertTrue(receipt["delivered"])
            self.assertIsNotNone(agent_jobs_repo.get_agent_job(owner=scope["owner"], job_id=first))
            self.assertEqual(await effects.poll_effect_receipts(SQLiteKernelStore()), 0)
            self.assertEqual(executor.call_count, 52)

        with patch.object(db, "now", return_value="2099-01-01 00:00:00"), patch(
            "app.agent.feature_gate.is_agent_enabled", return_value=True,
        ):
            asyncio.run(exercise())

    def test_history_cleanup_preserves_recovery_window_latest_rows_and_other_owners(self):
        from app.agent.models import EFFECT_RECEIPT_RETENTION_SECONDS

        start = datetime(2099, 1, 1)
        with patch.object(db, "now", return_value=start.strftime("%Y-%m-%d %H:%M:%S")):
            other, _ = self._create(owner="other-history-owner")
            agent_jobs_repo.cancel_agent_job(owner="other-history-owner", job_id=other["job_id"])
            running, _ = self._create(dedupe_key="running")
            claimed = agent_jobs_repo.claim_due_agent_job(
                job_type=_JOB_TYPE, current_time=db.now(), stale_before="2098-12-31 23:00:00",
            )
            assert claimed is not None
            self.assertEqual(claimed["job_id"], running["job_id"])
            oldest = ""
            for index in range(52):
                row, _ = self._create(job_id=f"job_{index:020d}")
                oldest = oldest or str(row["job_id"])
                agent_jobs_repo.cancel_agent_job(owner="web-owner-a", job_id=row["job_id"])

        # 回执窗口边界内不因超过50条而删除任何终态；窗口外仍保留最近50条。
        boundary = start + timedelta(seconds=EFFECT_RECEIPT_RETENTION_SECONDS)
        with patch.object(db, "now", return_value=boundary.strftime("%Y-%m-%d %H:%M:%S")):
            self._create(dedupe_key="at-boundary")
        self.assertIsNotNone(agent_jobs_repo.get_agent_job(owner="web-owner-a", job_id=oldest))
        with patch.object(db, "now", return_value=(boundary + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")):
            self._create(dedupe_key="after-boundary")
        self.assertIsNone(agent_jobs_repo.get_agent_job(owner="web-owner-a", job_id=oldest))
        self.assertIsNotNone(agent_jobs_repo.get_agent_job(owner="other-history-owner", job_id=other["job_id"]))
        active = agent_jobs_repo.get_agent_job(owner="web-owner-a", job_id=running["job_id"])
        assert active is not None
        self.assertEqual(active["status"], "running")
        with db.get_conn() as conn:
            retained = conn.execute(
                "SELECT count(*) FROM agent_jobs WHERE owner_digest=? AND status='cancelled'",
                (running["owner_digest"],),
            ).fetchone()[0]
        self.assertEqual(retained, 50)

    def test_lease_renewal_and_generation_cas_reject_stale_worker(self):
        row, _ = self._create(owner="owner-lease")
        claimed = agent_jobs_repo.claim_due_agent_job(
            job_type=_JOB_TYPE,
            current_time="2099-01-01 00:00:00",
            stale_before="2098-12-31 23:55:00",
        )
        assert claimed is not None
        self.assertEqual(claimed["job_id"], row["job_id"])
        generation = int(claimed["lease_generation"])
        self.assertTrue(
            agent_jobs_repo.renew_agent_job_lease(
                str(row["job_id"]),
                expected_lease_generation=generation,
                renewed_at="2099-01-01 00:01:00",
            )
        )
        self.assertFalse(
            agent_jobs_repo.complete_agent_job(
                str(row["job_id"]),
                expected_lease_generation=generation - 1,
                projection_json=_projection_json(),
                progress_current=0,
                progress_total=0,
                summary="stale",
            )
        )
        self.assertTrue(
            agent_jobs_repo.complete_agent_job(
                str(row["job_id"]),
                expected_lease_generation=generation,
                projection_json=_projection_json(),
                progress_current=0,
                progress_total=0,
                summary="done",
            )
        )

    def test_cancel_is_owner_scoped_and_running_job_stops_at_batch_boundary(self):
        row, _ = self._create(owner="owner-cancel")
        missing, outcome = agent_jobs_repo.cancel_agent_job(
            owner="other-owner", job_id=str(row["job_id"])
        )
        self.assertIsNone(missing)
        self.assertEqual(outcome, "not_found")

        claimed = agent_jobs_repo.claim_due_agent_job(
            job_type=_JOB_TYPE,
            current_time="2099-01-01 00:00:00",
            stale_before="2098-12-31 23:55:00",
        )
        requested, outcome = agent_jobs_repo.cancel_agent_job(
            owner="owner-cancel", job_id=str(row["job_id"])
        )
        self.assertEqual(outcome, "requested")
        assert requested is not None
        self.assertEqual(requested["status"], "running")
        assert claimed is not None
        generation = int(claimed["lease_generation"])
        self.assertTrue(
            agent_jobs_repo.is_agent_job_cancel_requested(
                str(row["job_id"]), expected_lease_generation=generation
            )
        )
        self.assertTrue(
            agent_jobs_repo.finalize_cancelled_agent_job(
                str(row["job_id"]), expected_lease_generation=generation
            )
        )
        terminal = agent_jobs_repo.get_agent_job(owner="owner-cancel", job_id=str(row["job_id"]))
        assert terminal is not None
        self.assertEqual(terminal["status"], "cancelled")

    def test_cancel_racing_with_terminal_completion_is_finalized_immediately(self):
        row, _ = self._create(owner="owner-terminal-race")
        job_id = str(row["job_id"])
        real_complete = agent_jobs_repo.complete_agent_job

        def cancel_before_complete(*args, **kwargs):
            _row, outcome = agent_jobs_repo.cancel_agent_job(
                owner="owner-terminal-race", job_id=job_id
            )
            self.assertEqual(outcome, "requested")
            return real_complete(*args, **kwargs)

        scheduler = AgentJobsScheduler(
            audit_executor=lambda _arguments: _result(
                ok=True, status="up_to_date", checked=1
            ),
            clock=MutableClock(datetime(2099, 1, 1, 0, 0, 0)),
        )
        with patch(
            "app.repositories.agent_jobs.complete_agent_job",
            side_effect=cancel_before_complete,
        ):
            self.assertEqual(scheduler.run_once(), 1)

        stored = agent_jobs_repo.get_agent_job(owner="owner-terminal-race", job_id=job_id)
        assert stored is not None
        self.assertEqual(stored["status"], "cancelled")
        self.assertTrue(stored["finished_at"])

    def test_cancel_racing_with_continuation_is_finalized_immediately(self):
        row, _ = self._create(
            owner="owner-continuation-race", dedupe_key=f"{_AS_OF}:race"
        )
        job_id = str(row["job_id"])
        real_continue = agent_jobs_repo.continue_agent_job

        def cancel_before_continue(*args, **kwargs):
            _row, outcome = agent_jobs_repo.cancel_agent_job(
                owner="owner-continuation-race", job_id=job_id
            )
            self.assertEqual(outcome, "requested")
            return real_continue(*args, **kwargs)

        scheduler = AgentJobsScheduler(
            audit_executor=lambda _arguments: _result(
                ok=True, status="partial", checked=1, continuation=True
            ),
            clock=MutableClock(datetime(2099, 1, 1, 0, 0, 0)),
        )
        with patch(
            "app.repositories.agent_jobs.continue_agent_job",
            side_effect=cancel_before_continue,
        ):
            self.assertEqual(scheduler.run_once(), 1)

        stored = agent_jobs_repo.get_agent_job(owner="owner-continuation-race", job_id=job_id)
        assert stored is not None
        self.assertEqual(stored["status"], "cancelled")
        self.assertTrue(stored["finished_at"])

    def test_init_db_recovers_interrupted_running_job_and_invalidates_old_lease(self):
        row, _ = self._create(owner="owner-restart")
        claimed = agent_jobs_repo.claim_due_agent_job(
            job_type=_JOB_TYPE,
            current_time="2099-01-01 00:00:00",
            stale_before="2098-12-31 23:55:00",
        )
        assert claimed is not None
        old_generation = int(claimed["lease_generation"])
        db.init_db()
        recovered = agent_jobs_repo.get_agent_job(owner="owner-restart", job_id=str(row["job_id"]))
        assert recovered is not None
        self.assertEqual(recovered["status"], "retry_wait")
        self.assertEqual(recovered["error_code"], "ProcessInterrupted")
        self.assertEqual(int(recovered["lease_generation"]), old_generation + 1)
        self.assertFalse(
            agent_jobs_repo.complete_agent_job(
                str(row["job_id"]),
                expected_lease_generation=old_generation,
                projection_json=_projection_json(),
                progress_current=0,
                progress_total=0,
                summary="stale",
            )
        )

    def test_not_configured_is_terminal_failure_and_never_reported_up_to_date(self):
        row, _ = self._create(owner="owner-not-configured")
        scheduler = AgentJobsScheduler(
            audit_executor=lambda _arguments: _result(
                ok=False, status="not_configured"
            ),
            clock=MutableClock(datetime(2099, 1, 1, 0, 0, 0)),
        )
        self.assertEqual(scheduler.run_once(), 1)
        stored = agent_jobs_repo.get_agent_job(
            owner="owner-not-configured", job_id=str(row["job_id"])
        )
        assert stored is not None
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error_code"], "NotConfigured")
        public = audit_job(owner="owner-not-configured", **{"job_id": str(row["job_id"]), "limit": 5})
        self.assertFalse(public["ok"])
        self.assertEqual(public["status"], "failed")
        self.assertNotIn("暂未发现", public["summary"])

    def test_unavailable_is_retried_and_inconclusive_is_a_valid_terminal_result(self):
        unavailable, _ = self._create(owner="owner-unavailable")
        scheduler = AgentJobsScheduler(
            audit_executor=lambda _arguments: _result(ok=False, status="unavailable"),
            clock=MutableClock(datetime(2099, 1, 1, 0, 0, 0)),
        )
        self.assertEqual(scheduler.run_once(), 1)
        stored = agent_jobs_repo.get_agent_job(
            owner="owner-unavailable", job_id=str(unavailable["job_id"])
        )
        assert stored is not None
        self.assertEqual(stored["status"], "retry_wait")
        self.assertEqual(stored["error_code"], "UpstreamUnavailable")

        inconclusive, _ = self._create(
            owner="owner-inconclusive", dedupe_key=f"{_AS_OF}:3"
        )
        scheduler = AgentJobsScheduler(
            audit_executor=lambda _arguments: _result(
                ok=False, status="inconclusive", checked=1
            ),
            clock=MutableClock(datetime(2099, 1, 1, 0, 0, 0)),
        )
        self.assertEqual(scheduler.run_once(), 1)
        stored = agent_jobs_repo.get_agent_job(
            owner="owner-inconclusive", job_id=str(inconclusive["job_id"])
        )
        assert stored is not None
        self.assertEqual(stored["status"], "succeeded")
        public = audit_job(owner="owner-inconclusive", **{"job_id": str(inconclusive["job_id"]), "limit": 5})
        self.assertTrue(public["ok"])
        self.assertEqual(public["status"], "inconclusive")
        self.assertNotIn("暂未发现", public["summary"])

    def test_confirmation_describes_full_scan_with_batch_limit_and_rejects_stale_replay(
        self,
    ):
        arguments = {"as_of": _AS_OF, "max_series": 2}
        context = "owner-confirm"
        preview, fingerprint = prepare_start_audit(owner=context, **arguments)
        self.assertEqual(preview["data"]["max_series"], 2)
        self.assertEqual(preview["data"]["as_of"], _AS_OF)
        self.assertFalse(preview["data"]["reused"])
        wake = Mock()
        with patch(
            "app.modules.agent_jobs_scheduler.get_agent_jobs_scheduler",
            return_value=Mock(wake=wake),
        ):
            accepted = start_audit(owner=context, expected_snapshot=fingerprint, **arguments)
        self.assertEqual(accepted["status"], "accepted")
        self.assertIsNone(accepted["data"]["progress_total"])
        row = agent_jobs_repo.get_agent_job(owner=context, job_id=accepted["data"]["job_id"])
        assert row is not None
        self.assertEqual(row["status"], "pending")
        self.assertEqual(json.loads(row["input_json"]), arguments)
        wake.assert_called_once_with()
        with self.assertRaises(AgentJobError) as stale:
            start_audit(owner=context, expected_snapshot=fingerprint, **arguments)
        self.assertEqual(stale.exception.code, "confirmation_stale")
