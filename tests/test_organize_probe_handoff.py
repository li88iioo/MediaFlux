"""规格补全提交后的持久交接：只重试投递，不重复改名。"""
from __future__ import annotations

from app.clients.guangya import GuangYaClient
from typing import cast

import json
import sqlite3
from dataclasses import asdict
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.repositories import organize_probe as organize_probe_repo
from app.clients.guangya import GuangYaFile
from app.modules.media_probe import MediaProfile
from app.modules.organize import OrganizeRules
from app.modules.organize_probe_worker import OrganizeProbeWorker
from app.modules.organize_probe_notifications import tag_probe_changes
from tests.support import IsolatedDatabaseTestCase
from tests import test_organize_probe_worker as probe_fixtures


class OrganizeProbeHandoffTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER IF EXISTS reject_probe_pending")
            conn.execute("DROP TRIGGER IF EXISTS expire_probe_during_audit")
            conn.execute("DROP TRIGGER IF EXISTS reject_probe_ack")
            conn.execute("DELETE FROM organize_probe_queue")
            conn.execute("DELETE FROM organize_log_items")
            conn.execute("DELETE FROM organize_log")
        fixture = probe_fixtures.OrganizeProbeWorkerTests()
        self.log_id = fixture._create_log()
        get_organize_log_result = db.get_organize_log(self.log_id)
        assert get_organize_log_result is not None
        self.old_name = str(get_organize_log_result["current_name"])
        self.client = probe_fixtures._ProbeCompletionClient([
            GuangYaFile("video-15", self.old_name, False, 1000, "video-etag", "target-parent"),
            GuangYaFile("subtitle-15", self.old_name.rsplit(".", 1)[0] + ".chs.srt", False, 100, "subtitle-etag", "target-parent"),
        ])
        self.worker = OrganizeProbeWorker()
        self.worker._client = cast(GuangYaClient, self.client)
        self.scheduler = Mock()
        self.scheduler.trigger.return_value = {"ok": True, "queued": True}
        self.probe = self.enterContext(patch(
            "app.modules.media_probe.probe_media_profile",
            return_value=MediaProfile(resolution="1080p", video_codec="H.264"),
        ))
        self.enterContext(patch("app.modules.scheduler.get_scheduler", return_value=self.scheduler))
        self.config = self.enterContext(patch(
            "app.modules.organize.get",
            side_effect=lambda key, default="": {
                "GY_STRM_BASE_URL": "http://example.invalid", "STRM_ROOT": "/fake-strm",
            }.get(key, default),
        ))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("禁止外联")))

    def enqueue(self, *, link_strm=True):
        self.job_id = organize_probe_repo.enqueue_organize_probe_completion(
            self.log_id, source_id="target", rel_dir="动漫/示例/Season 1",
            rules=asdict(OrganizeRules(target_dir_id="target", link_strm=link_strm)),
        )
        self.make_due()

    def make_due(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET next_attempt_at='2000-01-01 00:00:00'")

    def row(self):
        with db.get_conn() as conn:
            return dict(conn.execute("SELECT * FROM organize_probe_queue WHERE id=?", (self.job_id,)).fetchone())

    def assert_pending(self):
        row = self.row()
        self.assertEqual(row["status"], "retry_wait")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(len(json.loads(row["pending_strm_changes_json"])), 2)

    def test_trigger_failure_restart_replays_pending_without_probe_or_rename(self):
        self.enqueue()
        self.scheduler.trigger.return_value = {"ok": False, "error": "持久化暂不可用"}
        self.assertTrue(self.worker._process_one())
        self.assert_pending()
        pending = json.loads(self.row()["pending_strm_changes_json"])
        self.assertEqual(len(self.client.renames), 2)
        self.assertNotEqual(self.client.files["video-15"].name, self.old_name)
        get_organize_log_result_2 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_2 is not None
        self.assertEqual(get_organize_log_result_2["current_name"], self.client.files["video-15"].name)

        restarted = OrganizeProbeWorker()
        self.probe.side_effect = AssertionError("交接重试不得再次探测")
        self.make_due()
        self.scheduler.trigger.return_value = {"ok": True, "queued": True}
        with patch.object(restarted, "_runtime_client", side_effect=AssertionError("交接不访问云盘")):
            self.assertTrue(restarted._process_one())
        self.assertEqual(self.scheduler.trigger.call_count, 2)
        self.assertEqual(self.scheduler.trigger.call_args.kwargs["organize_changes"], tag_probe_changes(pending, {}, notify_enabled=True))
        self.assertEqual(len(self.client.renames), 2)
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.row()["pending_strm_changes_json"], "[]")

    def test_success_handoff_observes_committed_pending_then_atomically_completes(self):
        self.enqueue()
        observed = []
        def accepted(*args, **kwargs):
            get_organize_log_result_7 = db.get_organize_log(self.log_id)
            assert get_organize_log_result_7 is not None
            observed.append((self.row(), dict(get_organize_log_result_7), kwargs))
            return {"ok": True, "queued": True}
        self.scheduler.trigger.side_effect = accepted
        self.worker._process_one()
        self.assertEqual(len(observed), 1)
        row, log, options = observed[0]
        self.assertEqual(row["status"], "running")
        self.assertEqual(tag_probe_changes(json.loads(row["pending_strm_changes_json"]), {}, notify_enabled=True), options["organize_changes"])
        self.assertNotEqual(log["current_name"], self.old_name)
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.row()["pending_strm_changes_json"], "[]")
        self.assertEqual(len(self.client.renames), 2)

    def test_disabled_link_keeps_normal_rename_without_handoff(self):
        self.enqueue(link_strm=False)
        self.worker._process_one()
        self.scheduler.trigger.assert_not_called()
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.row()["pending_strm_changes_json"], "[]")
        self.assertEqual(len(self.client.renames), 2)

    def test_missing_configuration_preserves_pending_beyond_probe_attempt_limit(self):
        self.enqueue()
        self.config.side_effect = lambda key, default="": "" if key in {"GY_STRM_BASE_URL", "STRM_ROOT"} else default
        for _ in range(4):
            self.make_due()
            self.worker._process_one()
            self.assert_pending()
        self.scheduler.trigger.assert_not_called()
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(len(self.client.renames), 2)

    def test_trigger_exception_preserves_pending_beyond_probe_attempt_limit(self):
        self.enqueue()
        self.scheduler.trigger.side_effect = RuntimeError("暂时不能启动")
        for _ in range(4):
            self.make_due()
            self.worker._process_one()
            self.assert_pending()
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(len(self.client.renames), 2)

    def test_pending_write_failure_rolls_back_audit_and_remote_names(self):
        self.enqueue()
        old_items = [dict(item) for item in db.list_organize_log_items(self.log_id)]
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_probe_pending BEFORE UPDATE OF pending_strm_changes_json "
                         "ON organize_probe_queue WHEN NEW.pending_strm_changes_json != '[]' "
                         "BEGIN SELECT RAISE(ABORT, 'injected pending write failure'); END")
        self.worker._process_one()
        self.scheduler.trigger.assert_not_called()
        self.assertEqual(self.client.files["video-15"].name, self.old_name)
        get_organize_log_result_3 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_3 is not None
        self.assertEqual(get_organize_log_result_3["current_name"], self.old_name)
        self.assertEqual([dict(item) for item in db.list_organize_log_items(self.log_id)], old_items)
        self.assertEqual(self.row()["pending_strm_changes_json"], "[]")
        self.assertEqual(self.row()["status"], "retry_wait")
        self.assertEqual(len(self.client.renames), 4)

    def test_expired_lease_at_commit_rolls_back_remote_without_audit_update(self):
        self.enqueue()
        original_commit = organize_probe_repo.commit_organize_probe_rename
        def expire_then_commit(*args, **kwargs):
            with db.get_conn() as conn:
                conn.execute("UPDATE organize_probe_queue SET lease_until=0 WHERE id=?", (self.job_id,))
            return original_commit(*args, **kwargs)
        with patch.object(organize_probe_repo, "commit_organize_probe_rename", side_effect=expire_then_commit):
            self.worker._process_one()
        self.scheduler.trigger.assert_not_called()
        self.assertEqual(self.client.files["video-15"].name, self.old_name)
        get_organize_log_result_4 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_4 is not None
        self.assertEqual(get_organize_log_result_4["current_name"], self.old_name)
        self.assertEqual(self.row()["pending_strm_changes_json"], "[]")
        self.assertNotEqual(self.row()["status"], "completed")

    def test_completion_failure_keeps_pending_for_replay_without_consuming_attempts(self):
        self.enqueue()
        for _ in range(3):
            self.make_due()
            with patch.object(organize_probe_repo, "complete_organize_probe_job", side_effect=sqlite3.OperationalError("busy")):
                self.worker._process_one()
            self.assert_pending()
        self.make_due()
        self.worker._process_one()
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.row()["pending_strm_changes_json"], "[]")
        self.assertEqual(len(self.client.renames), 2)

    def test_expired_lease_at_ack_cannot_clear_pending(self):
        self.enqueue()
        def expire_before_ack(*args, **kwargs):
            with db.get_conn() as conn:
                conn.execute("UPDATE organize_probe_queue SET lease_until=0 WHERE id=?", (self.job_id,))
            return {"ok": True, "queued": True}
        self.scheduler.trigger.side_effect = expire_before_ack
        self.worker._process_one()
        self.assertNotEqual(self.row()["status"], "completed")
        self.assertEqual(len(json.loads(self.row()["pending_strm_changes_json"])), 2)
        self.scheduler.trigger.side_effect = None
        organize_probe_repo.recover_stale_organize_probe_jobs()
        self.make_due()
        OrganizeProbeWorker()._process_one()
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(len(self.client.renames), 2)

    def test_repository_rejects_stale_wrong_owner_and_wrong_log_commits(self):
        self.enqueue()
        job = organize_probe_repo.claim_due_organize_probe_jobs(owner="owner")[0]
        items = [dict(item) for item in db.list_organize_log_items(self.log_id)]
        updates = [{"id": item["id"], "expected_name": item["current_name"], "current_name": "new-" + item["current_name"]} for item in items]
        for owner, log_id, lease_until in [("other", self.log_id, job["lease_until"]), ("owner", self.log_id + 999, job["lease_until"]), ("owner", self.log_id, 0)]:
            with self.subTest(owner=owner, log_id=log_id, lease_until=lease_until):
                with db.get_conn() as conn:
                    conn.execute("UPDATE organize_probe_queue SET lease_until=? WHERE id=?", (lease_until, self.job_id))
                self.assertFalse(organize_probe_repo.commit_organize_probe_rename(
                    log_id, current_name="new-name", new_path="new-path", item_updates=updates,
                    job_id=self.job_id, owner=owner, changes=[{"file_id": "video-15"}],
                ))
                get_organize_log_result_5 = db.get_organize_log(self.log_id)
                assert get_organize_log_result_5 is not None
                self.assertEqual(get_organize_log_result_5["current_name"], self.old_name)
                self.assertEqual(self.row()["pending_strm_changes_json"], "[]")

    def test_completion_without_handoff_ack_cannot_discard_pending(self):
        self.enqueue()
        organize_probe_repo.claim_due_organize_probe_jobs(owner="owner")
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET pending_strm_changes_json=? WHERE id=?", ('[{"file_id":"video-15"}]', self.job_id))
        self.assertFalse(organize_probe_repo.complete_organize_probe_job(self.job_id, owner="owner"))
        self.assertEqual(self.row()["status"], "running")
        self.assertNotEqual(self.row()["pending_strm_changes_json"], "[]")

    def test_restart_after_rename_commit_before_handoff_uses_durable_payload(self):
        self.enqueue()
        with patch.object(self.worker, "_handoff_pending", side_effect=[False, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                self.worker._process_one()
        self.assertEqual(self.row()["status"], "running")
        self.assertEqual(len(json.loads(self.row()["pending_strm_changes_json"])), 2)
        self.scheduler.trigger.assert_not_called()
        self.assertEqual(organize_probe_repo.recover_stale_organize_probe_jobs(force=True), 1)
        self.probe.side_effect = AssertionError("已提交后不再探测")
        OrganizeProbeWorker()._process_one()
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.row()["pending_strm_changes_json"], "[]")
        self.assertEqual(len(self.client.renames), 2)
        self.scheduler.trigger.assert_called_once()

    def test_lease_expiry_during_transaction_rolls_back_audit_and_pending(self):
        self.enqueue()
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER expire_probe_during_audit AFTER UPDATE ON organize_log "
                "BEGIN UPDATE organize_probe_queue SET lease_until=0 "
                "WHERE organize_log_id=NEW.id; END"
            )
        self.worker._process_one()
        get_organize_log_result_6 = db.get_organize_log(self.log_id)
        assert get_organize_log_result_6 is not None
        self.assertEqual(get_organize_log_result_6["current_name"], self.old_name)
        self.assertEqual(self.client.files["video-15"].name, self.old_name)
        self.assertEqual(self.row()["pending_strm_changes_json"], "[]")
        self.assertEqual(self.row()["status"], "retry_wait")
        self.scheduler.trigger.assert_not_called()

    def test_corrupt_pending_is_retained_without_probe_or_attempt_exhaustion(self):
        self.enqueue()
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_probe_queue SET pending_strm_changes_json=? WHERE id=?",
                ("not-json", self.job_id),
            )
        for _ in range(3):
            self.make_due()
            self.worker._process_one()
            self.assertEqual(self.row()["status"], "retry_wait")
            self.assertEqual(self.row()["attempts"], 0)
            self.assertEqual(self.row()["pending_strm_changes_json"], "not-json")
        self.probe.assert_not_called()
        self.scheduler.trigger.assert_not_called()
        self.assertEqual(self.client.renames, [])

    def test_changed_owner_cannot_acknowledge_pending(self):
        self.enqueue()
        def change_owner(*args, **kwargs):
            with db.get_conn() as conn:
                conn.execute("UPDATE organize_probe_queue SET lease_owner='new-owner' WHERE id=?", (self.job_id,))
            return {"ok": True, "queued": True}
        self.scheduler.trigger.side_effect = change_owner
        self.worker._process_one()
        self.assertEqual(self.row()["status"], "running")
        self.assertEqual(self.row()["lease_owner"], "new-owner")
        self.assertEqual(len(json.loads(self.row()["pending_strm_changes_json"])), 2)
        self.assertEqual(len(self.client.renames), 2)

    def test_unchanged_name_without_pending_completes_without_handoff(self):
        self.enqueue()
        original = self.worker._desired_plan
        def unchanged_plan(*args):
            organizer, rules, plan = original(*args)
            plan.new_name = self.old_name
            return organizer, rules, plan
        with patch.object(self.worker, "_desired_plan", side_effect=unchanged_plan):
            self.worker._process_one()
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.row()["pending_strm_changes_json"], "[]")
        self.assertEqual(self.client.renames, [])
        self.scheduler.trigger.assert_not_called()

    def test_ack_sql_failure_preserves_pending_and_completion_atomically(self):
        self.enqueue()
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER reject_probe_ack BEFORE UPDATE ON organize_probe_queue "
                "WHEN NEW.status='completed' "
                "BEGIN SELECT RAISE(ABORT, 'injected ack failure'); END"
            )
        self.worker._process_one()
        self.assert_pending()
        self.assertFalse(self.row()["completed_at"])
        self.assertEqual(len(self.client.renames), 2)
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER reject_probe_ack")
        self.make_due()
        OrganizeProbeWorker()._process_one()
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.row()["pending_strm_changes_json"], "[]")
        self.assertTrue(self.row()["completed_at"])
        self.assertEqual(len(self.client.renames), 2)

    def test_pending_retries_even_when_probe_budget_already_exhausted(self):
        self.enqueue()
        self.scheduler.trigger.return_value = {"ok": False}
        self.worker._process_one()
        pending = self.row()["pending_strm_changes_json"]
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET attempts=max_attempts WHERE id=?", (self.job_id,))
        for _ in range(4):
            self.make_due()
            OrganizeProbeWorker()._process_one()
            row = self.row()
            self.assertEqual(row["status"], "retry_wait")
            self.assertEqual(row["attempts"], row["max_attempts"])
            self.assertEqual(row["pending_strm_changes_json"], pending)
        self.scheduler.trigger.return_value = {"ok": True, "queued": True}
        self.make_due()
        OrganizeProbeWorker()._process_one()
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(len(self.client.renames), 2)

    def test_ack_checks_lease_after_waiting_for_database_writer(self):
        from app.repositories import organize_probe

        self.enqueue()
        organize_probe_repo.claim_due_organize_probe_jobs(owner="owner")
        pending = '[{"file_id":"video-15"}]'
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_probe_queue SET lease_until=1001,pending_strm_changes_json=? WHERE id=?",
                (pending, self.job_id),
            )
        clock = [1000.0]
        original_get_conn = db.get_conn

        @contextmanager
        def delayed_writer_connection():
            with original_get_conn() as conn:
                def execute(sql, *args):
                    # 确定性模拟获取 SQLite writer 时等待到 lease 过期。
                    # 不能拿等待前绑定的时间去确认等待后执行的 UPDATE。
                    if sql.startswith(("BEGIN IMMEDIATE", "UPDATE")):
                        clock[0] = 1002.0
                    return conn.execute(sql, *args)
                yield SimpleNamespace(execute=execute)

        with patch.object(db, "get_conn", side_effect=delayed_writer_connection), patch.object(
            organize_probe, "time", SimpleNamespace(time=lambda: clock[0]),
        ):
            self.assertFalse(organize_probe_repo.complete_organize_probe_job(
                self.job_id, owner="owner", handoff_completed=True,
            ))
        self.assertEqual(self.row()["status"], "running")
        self.assertEqual(self.row()["pending_strm_changes_json"], pending)
