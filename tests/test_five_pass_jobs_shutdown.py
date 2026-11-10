"""长任务关闭只收尾已接纳批次，不再领取后续持久任务。"""
from __future__ import annotations

from tests.support import assert_present

import json
from unittest.mock import Mock, patch

from app.repositories import agent_jobs as agent_jobs_repo
from app.modules.library_patrol import empty_patrol_projection
from app.modules.episode_audit import EpisodeAuditResult
from app.modules.agent_jobs_scheduler import AgentJobsScheduler
from tests.support import IsolatedDatabaseTestCase, isolated_test_database

_AS_OF = "2026-09-07"


class DurableJobsShutdownTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.jobs = []
        for index in range(3):
            row, created = agent_jobs_repo.create_agent_job(
                owner="five-pass", job_type="library_episode_audit", dedupe_key=f"batch-{index}",
                input_json=json.dumps({"as_of": _AS_OF, "max_series": 2}),
                checkpoint_json=json.dumps({"as_of": _AS_OF, "cursor": "", "stall_attempts": 0}),
                projection_json=json.dumps(empty_patrol_projection(as_of=_AS_OF)),
            )
            self.assertTrue(created)
            self.jobs.append(str(row["job_id"]))

    @staticmethod
    def _result():
        return EpisodeAuditResult(True, "up_to_date", "已检查", data={
            **empty_patrol_projection(as_of=_AS_OF),
            "checked_series_count": 1, "mapped_series_count": 1, "up_to_date_count": 1,
            "continuation_pending": False, "last_processed_tmdb_id": "", "stalled_tmdb_id": "",
        })

    def _statuses(self):
        return [assert_present(agent_jobs_repo.get_agent_job(owner="five-pass", job_id=job_id))["status"] for job_id in self.jobs]

    def test_stop_during_batch_leaves_next_jobs_pending_and_new_scheduler_resumes(self):
        executor = Mock()
        scheduler = AgentJobsScheduler(audit_executor=executor)

        def finish_current(_arguments):
            scheduler.stop(timeout=0)
            return self._result()

        executor.side_effect = finish_current
        scheduler._loop()
        self.assertEqual(executor.call_count, 1)
        self.assertEqual(sorted(self._statuses()), ["pending", "pending", "succeeded"])
        restarted = AgentJobsScheduler(audit_executor=lambda _arguments: self._result())
        self.assertEqual([restarted.run_once() for _ in range(3)], [1, 1, 0])
        self.assertEqual(self._statuses(), ["succeeded"] * 3)

    def test_stopped_scheduler_does_not_claim_jobs(self):
        executor = Mock()
        scheduler = AgentJobsScheduler(audit_executor=executor)
        scheduler.stop(timeout=0)
        with patch.object(agent_jobs_repo, "claim_due_agent_job", return_value=None) as claim:
            self.assertEqual(scheduler.run_once(), 0)
        claim.assert_not_called()
        executor.assert_not_called()
        self.assertEqual(self._statuses(), ["pending"] * 3)

    def test_stop_racing_claim_releases_lease_without_spending_attempt(self):
        executor = Mock(return_value=self._result())
        scheduler = AgentJobsScheduler(audit_executor=executor)
        original = agent_jobs_repo.claim_due_agent_job

        def claim_then_stop(**kwargs):
            row = original(**kwargs)
            scheduler.stop(timeout=0)
            return row

        with patch.object(agent_jobs_repo, "claim_due_agent_job", side_effect=claim_then_stop):
            self.assertEqual(scheduler.run_once(), 0)
        executor.assert_not_called()
        self.assertEqual(self._statuses(), ["pending"] * 3)
        self.assertTrue(all(assert_present(agent_jobs_repo.get_agent_job(owner="five-pass", job_id=job_id))["attempts"] == 0 for job_id in self.jobs))
