"""Agent owner 隔离、可恢复长任务的状态机与公开语义测试。"""
from __future__ import annotations

from datetime import datetime
import json
from unittest.mock import Mock, patch

from app import database as db
from app.agent.durable_job_actions import (
    get_agent_job_status,
    prepare_start_episode_audit,
    start_episode_audit_arguments,
    start_episode_audit_confirmed,
)
from app.agent.library_patrol_progress import empty_patrol_projection
from app.agent.models import ToolContext, ToolResult
from app.agent.registry import AgentToolError
from app.modules.agent_jobs_scheduler import AgentJobsScheduler
from app.repositories.agent_jobs import (
    get_agent_job,
    list_agent_jobs,
)
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


def _result(*, ok: bool, status: str, checked: int = 0, continuation: bool = False) -> ToolResult:
    return ToolResult(
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
        return db.create_agent_job(
            owner=owner,
            job_type=_JOB_TYPE,
            dedupe_key=dedupe_key,
            input_json=json.dumps({"as_of": _AS_OF, "max_series": 2}),
            checkpoint_json=json.dumps({"as_of": _AS_OF, "cursor": "", "stall_attempts": 0}),
            projection_json=_projection_json(),
            progress_total=0,
            max_attempts=max_attempts,
            job_id=job_id,
        )

    def test_disabled_feature_gate_does_not_claim_or_execute_jobs(self):
        executor = Mock()
        scheduler = AgentJobsScheduler(audit_executor=executor)
        with patch("app.agent.feature_gate.is_agent_enabled", return_value=False), patch(
            "app.modules.agent_jobs_scheduler.db.claim_due_agent_job"
        ) as claim:
            self.assertEqual(scheduler.run_once(), 0)
        claim.assert_not_called()
        executor.assert_not_called()

    def test_owner_isolation_and_no_raw_owner_persistence(self):
        row, created = self._create(owner="private-owner-a")
        self.assertTrue(created)
        self.assertIsNotNone(db.get_agent_job(owner="private-owner-a", job_id=row["job_id"]))
        self.assertIsNone(db.get_agent_job(owner="private-owner-b", job_id=row["job_id"]))
        self.assertEqual(db.list_agent_jobs(owner="private-owner-b"), [])
        self.assertIsNone(db.find_active_agent_job(
            owner="private-owner-b", job_type=_JOB_TYPE, dedupe_key=f"{_AS_OF}:2"
        ))
        self.assertNotIn("private-owner-a", repr(dict(row)))
    def test_active_creation_is_idempotent_but_terminal_job_allows_new_run(self):
        first, created = self._create(owner="owner-idempotent")
        self.assertTrue(created)
        duplicate, created = self._create(owner="owner-idempotent")
        self.assertFalse(created)
        self.assertEqual(first["job_id"], duplicate["job_id"])

        cancelled, outcome = db.cancel_agent_job(
            owner="owner-idempotent", job_id=str(first["job_id"])
        )
        self.assertEqual(outcome, "cancelled")
        self.assertEqual(cancelled["status"], "cancelled")
        second, created = self._create(owner="owner-idempotent")
        self.assertTrue(created)
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_lease_renewal_and_generation_cas_reject_stale_worker(self):
        row, _ = self._create(owner="owner-lease")
        claimed = db.claim_due_agent_job(
            job_type=_JOB_TYPE,
            current_time="2099-01-01 00:00:00",
            stale_before="2098-12-31 23:55:00",
        )
        self.assertEqual(claimed["job_id"], row["job_id"])
        generation = int(claimed["lease_generation"])
        self.assertTrue(db.renew_agent_job_lease(
            str(row["job_id"]),
            expected_lease_generation=generation,
            renewed_at="2099-01-01 00:01:00",
        ))
        self.assertFalse(db.complete_agent_job(
            str(row["job_id"]),
            expected_lease_generation=generation - 1,
            projection_json=_projection_json(),
            progress_current=0,
            progress_total=0,
            summary="stale",
        ))
        self.assertTrue(db.complete_agent_job(
            str(row["job_id"]),
            expected_lease_generation=generation,
            projection_json=_projection_json(),
            progress_current=0,
            progress_total=0,
            summary="done",
        ))

    def test_cancel_is_owner_scoped_and_running_job_stops_at_batch_boundary(self):
        row, _ = self._create(owner="owner-cancel")
        missing, outcome = db.cancel_agent_job(
            owner="other-owner", job_id=str(row["job_id"])
        )
        self.assertIsNone(missing)
        self.assertEqual(outcome, "not_found")

        claimed = db.claim_due_agent_job(
            job_type=_JOB_TYPE,
            current_time="2099-01-01 00:00:00",
            stale_before="2098-12-31 23:55:00",
        )
        requested, outcome = db.cancel_agent_job(
            owner="owner-cancel", job_id=str(row["job_id"])
        )
        self.assertEqual(outcome, "requested")
        self.assertEqual(requested["status"], "running")
        generation = int(claimed["lease_generation"])
        self.assertTrue(db.is_agent_job_cancel_requested(
            str(row["job_id"]), expected_lease_generation=generation
        ))
        self.assertTrue(db.finalize_cancelled_agent_job(
            str(row["job_id"]), expected_lease_generation=generation
        ))
        terminal = db.get_agent_job(owner="owner-cancel", job_id=str(row["job_id"]))
        self.assertEqual(terminal["status"], "cancelled")

    def test_cancel_racing_with_terminal_completion_is_finalized_immediately(self):
        row, _ = self._create(owner="owner-terminal-race")
        job_id = str(row["job_id"])
        real_complete = db.complete_agent_job

        def cancel_before_complete(*args, **kwargs):
            _row, outcome = db.cancel_agent_job(
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
            "app.modules.agent_jobs_scheduler.db.complete_agent_job",
            side_effect=cancel_before_complete,
        ):
            self.assertEqual(scheduler.run_once(), 1)

        stored = db.get_agent_job(owner="owner-terminal-race", job_id=job_id)
        self.assertEqual(stored["status"], "cancelled")
        self.assertTrue(stored["finished_at"])

    def test_cancel_racing_with_continuation_is_finalized_immediately(self):
        row, _ = self._create(
            owner="owner-continuation-race", dedupe_key=f"{_AS_OF}:race"
        )
        job_id = str(row["job_id"])
        real_continue = db.continue_agent_job

        def cancel_before_continue(*args, **kwargs):
            _row, outcome = db.cancel_agent_job(
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
            "app.modules.agent_jobs_scheduler.db.continue_agent_job",
            side_effect=cancel_before_continue,
        ):
            self.assertEqual(scheduler.run_once(), 1)

        stored = db.get_agent_job(owner="owner-continuation-race", job_id=job_id)
        self.assertEqual(stored["status"], "cancelled")
        self.assertTrue(stored["finished_at"])

    def test_init_db_recovers_interrupted_running_job_and_invalidates_old_lease(self):
        row, _ = self._create(owner="owner-restart")
        claimed = db.claim_due_agent_job(
            job_type=_JOB_TYPE,
            current_time="2099-01-01 00:00:00",
            stale_before="2098-12-31 23:55:00",
        )
        old_generation = int(claimed["lease_generation"])
        db.init_db()
        recovered = db.get_agent_job(owner="owner-restart", job_id=str(row["job_id"]))
        self.assertEqual(recovered["status"], "retry_wait")
        self.assertEqual(recovered["error_code"], "ProcessInterrupted")
        self.assertEqual(int(recovered["lease_generation"]), old_generation + 1)
        self.assertFalse(db.complete_agent_job(
            str(row["job_id"]),
            expected_lease_generation=old_generation,
            projection_json=_projection_json(),
            progress_current=0,
            progress_total=0,
            summary="stale",
        ))

    def test_not_configured_is_terminal_failure_and_never_reported_up_to_date(self):
        row, _ = self._create(owner="owner-not-configured")
        scheduler = AgentJobsScheduler(
            audit_executor=lambda _arguments: _result(ok=False, status="not_configured"),
            clock=MutableClock(datetime(2099, 1, 1, 0, 0, 0)),
        )
        self.assertEqual(scheduler.run_once(), 1)
        stored = db.get_agent_job(owner="owner-not-configured", job_id=str(row["job_id"]))
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error_code"], "NotConfigured")
        public = get_agent_job_status(
            {"job_id": str(row["job_id"]), "limit": 5},
            ToolContext(owner="owner-not-configured"),
        )
        self.assertFalse(public.ok)
        self.assertEqual(public.status, "failed")
        self.assertNotIn("暂未发现", public.summary)

    def test_unavailable_is_retried_and_inconclusive_is_a_valid_terminal_result(self):
        unavailable, _ = self._create(owner="owner-unavailable")
        scheduler = AgentJobsScheduler(
            audit_executor=lambda _arguments: _result(ok=False, status="unavailable"),
            clock=MutableClock(datetime(2099, 1, 1, 0, 0, 0)),
        )
        self.assertEqual(scheduler.run_once(), 1)
        stored = db.get_agent_job(owner="owner-unavailable", job_id=str(unavailable["job_id"]))
        self.assertEqual(stored["status"], "retry_wait")
        self.assertEqual(stored["error_code"], "UpstreamUnavailable")

        inconclusive, _ = self._create(owner="owner-inconclusive", dedupe_key=f"{_AS_OF}:3")
        scheduler = AgentJobsScheduler(
            audit_executor=lambda _arguments: _result(ok=False, status="inconclusive", checked=1),
            clock=MutableClock(datetime(2099, 1, 1, 0, 0, 0)),
        )
        self.assertEqual(scheduler.run_once(), 1)
        stored = db.get_agent_job(owner="owner-inconclusive", job_id=str(inconclusive["job_id"]))
        self.assertEqual(stored["status"], "succeeded")
        public = get_agent_job_status(
            {"job_id": str(inconclusive["job_id"]), "limit": 5},
            ToolContext(owner="owner-inconclusive"),
        )
        self.assertTrue(public.ok)
        self.assertEqual(public.status, "inconclusive")
        self.assertNotIn("暂未发现", public.summary)

    def test_confirmation_describes_full_scan_with_batch_limit_and_rejects_stale_replay(self):
        arguments = start_episode_audit_arguments({"as_of": _AS_OF, "max_series": 2})
        context = ToolContext(owner="owner-confirm")
        preview, fingerprint = prepare_start_episode_audit(arguments, context)
        self.assertIn("整个媒体库", preview.summary)
        self.assertIn("每批最多 2 部", preview.summary)
        self.assertIn("创建后台", preview.summary)
        self.assertTrue(any("不会自动下载" in item for item in preview.data["effects"]))
        self.assertTrue(any("Jellyfin / Emby" in item for item in preview.data["effects"]))
        wake = Mock()
        with patch(
            "app.agent.durable_job_actions.get_agent_jobs_scheduler",
            return_value=Mock(wake=wake),
        ):
            accepted = start_episode_audit_confirmed(arguments, fingerprint, context)
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual(accepted.data["progress_total"], 0)
        self.assertIn("整个媒体库", accepted.summary)
        wake.assert_called_once_with()
        with self.assertRaises(AgentToolError) as stale:
            start_episode_audit_confirmed(arguments, fingerprint, context)
        self.assertEqual(stale.exception.code, "confirmation_stale")
