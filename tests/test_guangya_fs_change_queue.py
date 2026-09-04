"""通用光鸭文件变更持久队列迁移与结果投影测试。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import database
from app.modules import guangya_fs_change
from app.repositories.organize_operation_jobs import (
    claim_organize_operation_job,
    count_pending_organize_operation_jobs,
    enqueue_organize_operation_job,
    fail_pending_organize_operation_job,
    finish_organize_operation_job,
    organize_operation_owner_digest,
    sanitize_organize_operation_result,
    verify_organize_operation_payload,
)
from tests.support import IsolatedDatabaseTestCase


class GuangYaFSChangeQueueTests(unittest.TestCase):
    def test_v17_migration_preserves_existing_jobs_and_accepts_generic_change_kind(
        self,
    ):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE organize_operation_jobs (
                job_id TEXT PRIMARY KEY,
                job_kind TEXT NOT NULL CHECK(job_kind IN (
                    'agent_directory_scrape','agent_guangya_cleanup',
                    'agent_guangya_rename','directory_scrape'
                )),
                owner_digest TEXT NOT NULL,
                operation TEXT NOT NULL,
                reference TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                payload_auth TEXT NOT NULL DEFAULT '',
                dedupe_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
                    'pending','running','completed','partial','failed',
                    'cancelled','manual_review'
                )),
                lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
                result_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
                expires_at REAL NOT NULL DEFAULT 0,
                purged_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );
            INSERT INTO organize_operation_jobs(
                job_id,job_kind,owner_digest,operation,dedupe_digest,created_at,updated_at
            ) VALUES (
                'old','agent_guangya_rename','owner','旧改名','dedupe','2026-08-31','2026-08-31'
            );
            """
        )

        database._migrate_agent_guangya_fs_change_jobs_v17(connection)

        preserved = connection.execute(
            "SELECT job_kind,operation FROM organize_operation_jobs WHERE job_id='old'"
        ).fetchone()
        self.assertEqual(
            dict(preserved),
            {
                "job_kind": "agent_guangya_rename",
                "operation": "旧改名",
            },
        )
        connection.execute(
            """
            INSERT INTO organize_operation_jobs(
                job_id,job_kind,owner_digest,operation,dedupe_digest,created_at,updated_at
            ) VALUES (
                'new','agent_guangya_fs_change','owner','通用变更','dedupe-new',
                '2026-08-31','2026-08-31'
            )
            """
        )
        self.assertEqual(
            connection.execute(
                "SELECT job_kind FROM organize_operation_jobs WHERE job_id='new'"
            ).fetchone()[0],
            "agent_guangya_fs_change",
        )
        connection.close()

    def test_public_result_projection_keeps_new_aggregate_counts_only(self):
        result = sanitize_organize_operation_result(
            {
                "stats": {
                    "total": 5,
                    "moved": 1,
                    "trashed": 1,
                    "created": 1,
                    "renamed": 1,
                    "copied": 1,
                    "file_id": 999,
                }
            }
        )
        self.assertEqual(
            result["stats"],
            {
                "total": 5,
                "moved": 1,
                "trashed": 1,
                "created": 1,
                "renamed": 1,
                "copied": 1,
            },
        )


class GuangYaFSChangeJobBindingTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with database.get_conn() as conn:
            conn.execute("DELETE FROM organize_operation_jobs")
        self.temp = tempfile.TemporaryDirectory()
        self.plan_dir = Path(self.temp.name) / "changes"
        self.patches = [
            mock.patch.object(
                guangya_fs_change, "_directory", return_value=self.plan_dir
            ),
            mock.patch.object(
                guangya_fs_change, "get_web_secret", return_value="test-secret"
            ),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def _confirmed_plan(self, plan_id: str = "1" * 32) -> dict:
        current = time.time()
        plan = {
            "version": 1,
            "plan_id": plan_id,
            "owner_digest": organize_operation_owner_digest("queue-owner"),
            "credential_generation": 9,
            "observation_ref": "OBS-TEST",
            "created_at": "2026-09-01T00:00:00+08:00",
            "created_at_epoch": current,
            "expires_at_epoch": current + 600,
            "confirmed_at": "2026-09-01T00:00:01+08:00",
            "confirmed_at_epoch": current,
            "execute_until_epoch": current + 900,
            "trigger_strm": False,
            "status": "confirmed",
            "operations": [{"op": "create_directory", "name": "demo"}],
            "stats": {"total": 1, "create_directory": 1},
            "samples": ["新建目录：demo"],
            "execution": {},
            "fingerprint": "f" * 64,
        }
        guangya_fs_change._atomic_write(plan)
        return plan

    def _enqueue(self, plan: dict):
        return enqueue_organize_operation_job(
            job_kind="agent_guangya_fs_change",
            owner="queue-owner",
            operation="光鸭文件变更",
            reference="冻结计划",
            payload={
                "version": 1,
                "plan_id": plan["plan_id"],
                "plan_fingerprint": plan["fingerprint"],
                "owner_digest": plan["owner_digest"],
                "credential_generation": 9,
            },
            dedupe_key=f"fs-change:{plan['plan_id']}",
        )

    def test_enqueue_injects_signed_job_id_and_binds_plan_before_return(self):
        plan = self._confirmed_plan()

        row, replayed = self._enqueue(plan)

        self.assertFalse(replayed)
        self.assertTrue(verify_organize_operation_payload(row))
        stored_payload = json.loads(str(row["payload_json"]))
        self.assertEqual(stored_payload["job_id"], row["job_id"])
        queued = guangya_fs_change.load_fs_change_plan(
            plan["plan_id"],
            expected_fingerprint=plan["fingerprint"],
            require_confirmed=True,
        )
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["job_id"], row["job_id"])
        self.assertEqual(queued["queue_until_epoch"], row["expires_at"])

        # 入队后即使原 15 分钟确认票据到期，执行准入仍由持久队列负责。
        with mock.patch.object(
            guangya_fs_change.time,
            "time",
            return_value=float(plan["execute_until_epoch"]) + 1,
        ):
            loaded = guangya_fs_change.load_fs_change_plan(
                plan["plan_id"],
                expected_fingerprint=plan["fingerprint"],
                require_confirmed=True,
            )
        self.assertEqual(loaded["status"], "queued")

    def test_replayed_enqueue_keeps_the_original_job_binding(self):
        plan = self._confirmed_plan()
        first, first_replayed = self._enqueue(plan)

        second, second_replayed = self._enqueue(plan)

        self.assertFalse(first_replayed)
        self.assertTrue(second_replayed)
        self.assertEqual(first["job_id"], second["job_id"])
        queued = guangya_fs_change.load_fs_change_plan(plan["plan_id"])
        self.assertEqual(queued["job_id"], first["job_id"])

    def test_gc_and_preview_cleanup_never_delete_live_queued_or_running_plan(self):
        plan = self._confirmed_plan()
        row, _ = self._enqueue(plan)
        queued = guangya_fs_change._read(plan["plan_id"])
        queued["expires_at_epoch"] = 1
        queued["execute_until_epoch"] = 1
        queued["queue_until_epoch"] = 30_000
        guangya_fs_change._atomic_write(queued)

        with mock.patch.object(guangya_fs_change.time, "time", return_value=10_000):
            maintained = guangya_fs_change.maintain_fs_change_plans()
        self.assertEqual(maintained["removed"], 0)
        self.assertFalse(
            guangya_fs_change.discard_fs_change_plan(
                plan["plan_id"], preview_only=True
            )
        )
        guangya_fs_change.update_fs_change_plan_execution(
            plan["plan_id"],
            status="running",
            execution={"started_at": "now"},
            expected_statuses={"queued"},
            expected_job_id=str(row["job_id"]),
        )
        with mock.patch.object(guangya_fs_change.time, "time", return_value=20_000):
            guangya_fs_change.maintain_fs_change_plans()
        self.assertEqual(
            guangya_fs_change._read(plan["plan_id"])["status"], "running"
        )

    def test_gc_removes_queued_plan_after_its_queue_lease_expires(self):
        plan = self._confirmed_plan()
        self._enqueue(plan)
        queued = guangya_fs_change._read(plan["plan_id"])
        queued["queue_until_epoch"] = 9_999
        guangya_fs_change._atomic_write(queued)

        with mock.patch.object(guangya_fs_change.time, "time", return_value=10_000):
            maintained = guangya_fs_change.maintain_fs_change_plans()

        self.assertEqual(maintained["removed"], 1)
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeError):
            guangya_fs_change.load_fs_change_plan(plan["plan_id"])

    def test_queue_terminal_updates_release_plan_from_active_state(self):
        pending_plan = self._confirmed_plan("2" * 32)
        pending_row, _ = self._enqueue(pending_plan)
        self.assertTrue(
            fail_pending_organize_operation_job(
                str(pending_row["job_id"]),
                error_code="DispatcherFailed",
                error="dispatcher failed",
            )
        )
        self.assertEqual(
            guangya_fs_change._read(pending_plan["plan_id"])["status"], "failed"
        )

        running_plan = self._confirmed_plan("3" * 32)
        running_row, _ = self._enqueue(running_plan)
        claimed = claim_organize_operation_job(str(running_row["job_id"]))
        self.assertIsNotNone(claimed)
        guangya_fs_change.update_fs_change_plan_execution(
            running_plan["plan_id"],
            status="running",
            execution={"started_at": "now"},
            expected_statuses={"queued"},
            expected_job_id=str(running_row["job_id"]),
        )
        self.assertTrue(
            finish_organize_operation_job(
                str(running_row["job_id"]),
                expected_lease_generation=int(claimed["lease_generation"]),
                status="failed",
                error_code="WorkerFailed",
                error="worker failed",
            )
        )
        self.assertEqual(
            guangya_fs_change._read(running_plan["plan_id"])["status"],
            "manual_review",
        )

    def test_expired_pending_job_marks_bound_plan_cancelled(self):
        plan = self._confirmed_plan("4" * 32)
        row, _ = self._enqueue(plan)

        with mock.patch(
            "app.repositories.organize_operation_jobs.time.time",
            return_value=float(row["expires_at"]) + 1,
        ):
            self.assertEqual(count_pending_organize_operation_jobs(), 0)

        self.assertEqual(
            guangya_fs_change._read(plan["plan_id"])["status"], "cancelled"
        )


if __name__ == "__main__":
    unittest.main()
