"""已提交的后台规格改名必须独立于探测可用性完成审计/任务收尾。"""
from __future__ import annotations

from app.clients.guangya import GuangYaClient
from typing import cast

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.repositories import organize_history as organize_history_repo
from app.repositories import organize_probe as organize_probe_repo
from app.modules.organize import OrganizeRules
from app.modules.organize_probe_worker import OrganizeProbeWorker
from tests import test_release_chain_organize as fixtures
from tests.support import isolated_test_database


class ProbeCommittedFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.fixture = fixtures.ReleaseChainCompensationTests()
        self.log_id = self.fixture._log()
        self.cloud = fixtures.MemoryCloud()
        self.worker = self.new_worker()
        self.job_id = organize_probe_repo.enqueue_organize_probe_completion(
            self.log_id, source_id="target", rel_dir="Archive", rules={}
        )
        self.probe = self.enterContext(patch(
            "app.modules.media_probe.probe_media_profile", return_value=object()
        ))
        self.enterContext(patch.object(
            self.worker, "_desired_plan", return_value=(
                object(), OrganizeRules(link_strm=False),
                SimpleNamespace(new_name="Archive-1080p.mkv"),
            )
        ))

    def new_worker(self):
        worker = OrganizeProbeWorker()
        worker._client = cast(GuangYaClient, self.cloud)
        return worker

    def row(self):
        return self.fixture._job(self.job_id)

    def run_due(self, worker=None):
        self.fixture._due(self.job_id)
        return (worker or self.worker)._process_one()

    def assert_committed(self):
        get_organize_log_result = db.get_organize_log(self.log_id)
        assert get_organize_log_result is not None
        self.assertEqual(get_organize_log_result["current_name"], "Archive-1080p.mkv")
        self.assertEqual(self.cloud.files["video"].name, "Archive-1080p.mkv")
        self.assertEqual(self.row()["attempts"], 0)
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(self.cloud.calls, [("rename", "video", "Archive-1080p.mkv")])

    def finish_without_external_read(self, worker=None):
        worker = worker or self.new_worker()
        with patch.object(worker, "_runtime_client", side_effect=AssertionError("不得重复读云端/探测")):
            self.assertTrue(self.run_due(worker))
        self.assertEqual(self.row()["status"], "completed")
        self.assert_committed()

    def test_audit_completion_failure_recovers_without_reprobing(self):
        with patch.object(organize_history_repo, "finish_organize_operation_step", side_effect=sqlite3.OperationalError("busy")):
            self.assertTrue(self.run_due())
        self.assertEqual(self.row()["status"], "retry_wait")
        self.assert_committed()
        self.finish_without_external_read()

    def test_repeated_recovery_audit_errors_do_not_consume_probe_attempts(self):
        with patch.object(organize_history_repo, "finish_organize_operation_step", side_effect=sqlite3.OperationalError("busy")):
            self.run_due()
        for _ in range(3):
            with patch.object(organize_history_repo, "finish_organize_operation_step", side_effect=sqlite3.OperationalError("busy")):
                self.assertTrue(self.run_due(self.new_worker()))
            self.assertEqual(self.row()["status"], "retry_wait")
            self.assert_committed()
        self.finish_without_external_read()

    def test_task_ack_errors_keep_successful_steps_as_durable_completion_fact(self):
        with patch.object(organize_probe_repo, "complete_organize_probe_job", side_effect=sqlite3.OperationalError("busy")):
            self.run_due()
        self.assertEqual(self.row()["status"], "retry_wait")
        self.assert_committed()
        self.assertEqual(organize_history_repo.list_organize_operation_steps(self.log_id)[0]["status"], "success")
        # 新进程/新worker没有内存标记；即使所有步骤早已成功，也只能继续ack。
        for _ in range(3):
            worker = self.new_worker()
            with patch.object(worker, "_runtime_client", side_effect=AssertionError("不得重探")), \
                 patch.object(organize_probe_repo, "complete_organize_probe_job", side_effect=sqlite3.OperationalError("busy")):
                self.assertTrue(self.run_due(worker))
            self.assertEqual(self.row()["status"], "retry_wait")
            self.assert_committed()
        self.finish_without_external_read()

    def test_lost_ack_lease_does_not_hide_completed_steps_behind_display_limit(self):
        with patch.object(organize_probe_repo, "complete_organize_probe_job", return_value=False):
            self.run_due()
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO organize_operation_steps(log_id,operation_token,step_index,action,status) VALUES(?,?,?,?,?)",
                [(self.log_id, "later-audit", i, "other", "success") for i in range(1010)],
            )
        self.finish_without_external_read()

    def test_repeated_enqueue_is_same_job_not_a_new_probe_cycle(self):
        with patch.object(organize_probe_repo, "complete_organize_probe_job", return_value=False):
            self.run_due()
        same = organize_probe_repo.enqueue_organize_probe_completion(
            self.log_id, source_id="target", rel_dir="Archive", rules={}
        )
        self.assertEqual(same, self.job_id)
        self.finish_without_external_read()
        self.assertEqual(organize_probe_repo.enqueue_organize_probe_completion(
            self.log_id, source_id="target", rel_dir="Archive", rules={}
        ), self.job_id)
        self.assertFalse(self.run_due(self.new_worker()))
        self.assert_committed()

    def test_other_job_success_cannot_short_circuit_a_fresh_probe(self):
        organize_history_repo.add_organize_operation_step(
            self.log_id, f"probe:{self.job_id + 1}:old-job", 1, "probe_rename",
            file_id="video", to_name="Archive.mkv", to_parent_id="target", status="success",
        )
        self.assertTrue(self.run_due())
        self.assertEqual(self.row()["status"], "completed")
        self.assert_committed()

    def test_recovery_state_database_outage_does_not_exhaust_committed_job(self):
        with patch.object(organize_probe_repo, "complete_organize_probe_job", return_value=False):
            self.run_due()
        reads = (
            (organize_history_repo, "list_pending_organize_probe_steps"),
            (db, "get_organize_log"),
            (db, "list_organize_log_items"),
        )
        for owner, name in reads:
            with self.subTest(read=name):
                for _ in range(3):
                    with patch.object(owner, name, side_effect=sqlite3.OperationalError("database is locked")):
                        self.assertTrue(self.run_due(self.new_worker()))
                    self.assertEqual(self.row()["status"], "retry_wait")
                    self.assert_committed()
        self.finish_without_external_read()

    def test_precommit_database_error_still_rolls_back_remote_and_does_not_ack(self):
        with patch.object(organize_probe_repo, "commit_organize_probe_rename", side_effect=sqlite3.OperationalError("busy")):
            self.run_due()
        self.assertEqual(self.cloud.files["video"].name, "Archive.mkv")
        get_organize_log_result_2 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_2 is not None
        self.assertEqual(get_organize_log_result_2["current_name"], "Archive.mkv")
        self.assertEqual(self.row()["status"], "retry_wait")
        self.assertEqual(self.row()["attempts"], 1)
        self.assertTrue(all(row["status"] != "success" for row in organize_history_repo.list_organize_operation_steps(self.log_id)))


class ProbeSupersededFinalizationTests(unittest.TestCase):
    def test_later_manual_correction_is_not_erased_by_old_probe_finalization(self):
        for failed_stage in ("audit", "ack"):
            with self.subTest(stage=failed_stage), isolated_test_database():
                fixture = fixtures.ReleaseChainBusinessSnapshotTests()
                clock = fixtures.ReleaseChainCompensationTests()
                try:
                    log_id, cloud, service = fixture._fixture()
                    worker = OrganizeProbeWorker()
                    worker._client = cast(GuangYaClient, cloud)
                    job_id = organize_probe_repo.enqueue_organize_probe_completion(log_id, source_id="target", rel_dir="Archive", rules={})
                    real_complete = organize_probe_repo.complete_organize_probe_job

                    def expired_ack(*args, **kwargs):
                        until = clock._job(job_id)["lease_until"]
                        with patch("app.repositories.organize_probe.time.time", return_value=until + 1):
                            return real_complete(*args, **kwargs)

                    failure = (patch.object(organize_history_repo, "finish_organize_operation_step", side_effect=sqlite3.OperationalError("busy"))
                               if failed_stage == "audit" else
                               patch.object(organize_probe_repo, "complete_organize_probe_job", side_effect=expired_ack))
                    with patch("app.modules.media_probe.probe_media_profile", return_value=object()), \
                         patch.object(worker, "_desired_plan", return_value=(object(), OrganizeRules(link_strm=False),
                            SimpleNamespace(new_name="First.2025.S01E07-1080p.mkv"))), failure:
                        clock._due(job_id)
                        worker._process_one()
                    self.assertEqual(clock._job(job_id)["status"], "retry_wait")
                    correction = service.reorganize(log_id, "later-manual", service.detail(log_id)["version"],
                                                    "2", "tv", season=2, episode=3)
                    self.assertTrue(correction["success"])
                    before = dict(db.list_organize_log_items(log_id)[0])
                    second = OrganizeProbeWorker()
                    with patch.object(second, "_runtime_client", side_effect=AssertionError("旧任务不再探测")):
                        clock._due(job_id)
                        second._process_one()
                    self.assertEqual(dict(db.list_organize_log_items(log_id)[0]), before)
                    get_organize_log_result_3 = db.get_organize_log(log_id)
                    assert get_organize_log_result_3 is not None
                    self.assertEqual(get_organize_log_result_3["status"], "success")
                    self.assertEqual(clock._job(job_id)["status"], "cancelled")
                    self.assertEqual((cloud.files["video"].parent_id, cloud.files["video"].name),
                                     (before["current_parent_id"], before["current_name"]))
                finally:
                    fixture.doCleanups()
