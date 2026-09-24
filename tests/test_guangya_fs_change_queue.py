"""通用光鸭文件变更持久队列迁移与结果投影测试。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app import database, database_migrations
from app.agent.domain_catalog.cloud_runtime import guangya_organize_status
from app.agent.effect_completion import wait_for_effect_completion
from app.agent.models import ToolContext, ToolResult
from app.modules import guangya_fs_change
from app.modules.organize_tasks import OrganizeTaskManager
from app.repositories.organize_operation_jobs import (
    claim_organize_operation_job,
    count_pending_organize_operation_jobs,
    enqueue_organize_operation_job,
    fail_pending_organize_operation_job,
    finish_organize_operation_job,
    get_organize_operation_job,
    organize_operation_owner_digest,
    organize_operation_public_ref,
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

        database_migrations._migrate_agent_guangya_fs_change_jobs_v17(connection)

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

    def test_durable_fs_partial_stats_reach_waiter_from_live_history_and_restart(self):
        # 实盘结果的数值/结构；所有执行均替换为内存结果，不访问云盘。
        stats = {
            "total": 168, "renamed": 0, "moved": 0, "relocated": 154,
            "copied": 0, "trashed": 0, "created": 1, "failed": 13,
            "verification_failed": 0, "precondition_failed": 0, "audit_failures": 0,
        }
        self._assert_durable_stats_reach_waiter(stats, terminal="partial")

    def test_durable_fs_completed_stats_reach_waiter_from_live_history_and_restart(self):
        self._assert_durable_stats_reach_waiter(
            {"total": 13, "relocated": 13, "created": 0, "failed": 0},
            terminal="completed",
        )

    def _assert_durable_stats_reach_waiter(self, stats, *, terminal):
        queued, _ = self._enqueue(self._confirmed_plan())
        job_id = str(queued["job_id"])
        claimed = claim_organize_operation_job(job_id)
        public_ref = organize_operation_public_ref(job_id)
        context = ToolContext(owner="queue-owner")
        manager = OrganizeTaskManager()
        manager._lock = threading.Lock()
        manager._lock.acquire()
        manager._task = {
            "id": job_id, "status": "running", "stats": {}, "durable": True,
            "owner_digest": claimed["owner_digest"],
        }

        # 作业运行中且尚无持久终态时，不得拿 preview 数量伪造执行统计。
        for source, current in (("live", manager), ("restart", OrganizeTaskManager())):
            with (
                self.subTest(phase="running", source=source),
                mock.patch("app.modules.organize_tasks.get_organize_manager", return_value=current),
                mock.patch.object(current, "status", return_value={}),
            ):
                public = guangya_organize_status({"operation_ref": public_ref}, context)
                self.assertEqual(public.data["task"]["status"], "running")
                self.assertEqual(public.data["task"]["stats"], {})

        with (
            mock.patch.object(manager, "_execute_durable_operation", return_value={
                "partial": terminal == "partial", "requires_manual": False,
                "stats": {**stats, "internal_file_id": "private-item"},
            }),
            mock.patch.object(manager, "_wake_download_tracker"),
        ):
            manager._run_durable_operation(dict(claimed))
        persisted = get_organize_operation_job(job_id)
        self.assertEqual(persisted["status"], terminal)
        self.assertEqual(json.loads(persisted["result_json"]), {"stats": stats})

        accepted = ToolResult(True, "accepted", "已提交", data={
            "operation_ref": public_ref, "total": stats["total"],
            "relocate_count": stats["total"] - stats["created"],
        })
        for source in ("live", "history", "restart"):
            if source == "history":
                # 终态已由 worker 写入历史；当前任务切走后必须仍保留同一结果。
                manager._task = {}
            current = OrganizeTaskManager() if source == "restart" else manager
            with (
                self.subTest(phase=terminal, source=source),
                mock.patch("app.modules.organize_tasks.get_organize_manager", return_value=current),
                mock.patch.object(current, "status", return_value={}),
            ):
                raw = current.task_result(public_ref, owner=context.owner)
                public = guangya_organize_status({"operation_ref": public_ref}, context)
                receipt = asyncio.run(wait_for_effect_completion(
                    accepted, tool="guangya.fs.change.execute", context=context,
                ))
                self.assertEqual(receipt.data["stats"], stats)
                self.assertEqual(receipt.data["background_job"]["stats"], stats)
                self.assertEqual(receipt.status, terminal)
                self.assertEqual(receipt.ok, terminal == "completed")
                self.assertEqual(receipt.data["relocate_count"], stats["total"] - stats["created"])
                self.assertEqual(receipt.data["operation_ref"], public_ref)
                self.assertEqual(public.data["task"]["stats"], stats)
                self.assertEqual(raw["result"], {"stats": stats})
                self.assertIsNone(current.task_result(public_ref, owner="other-owner"))
                for private in ("owner_digest", "internal_file_id", "private-item", job_id):
                    self.assertNotIn(private, json.dumps(receipt.data))

    def test_legacy_empty_directory_plan_needs_no_media_sync(self):
        plan = self._confirmed_plan()
        plan["trigger_strm"] = True
        plan["operations"] = [{"op": "create_directory", "name": "legacy"}]
        plan["fingerprint"] = guangya_fs_change._fingerprint(plan)
        guangya_fs_change._atomic_write(plan)

        with (
            mock.patch(
                "app.modules.strm.configured_strm_source_plans",
                return_value=([{"id": "source", "name": "source"}], ""),
            ),
            mock.patch(
                "app.config.get",
                side_effect=lambda key, default="": {
                    "GY_ORGANIZE_SOURCE_DIRS": "[{\"id\":\"source\"}]",
                    "GY_ORGANIZE_TARGET_DIR": "target",
                }.get(key, default),
            ),
        ):
            from app.modules.strm import cloud_change_sources, trigger_cloud_changes
            stats = trigger_cloud_changes(plan["operations"], sources=cloud_change_sources(None))

        self.assertEqual(stats, {"strm_trigger_skipped": 1})
        queued, _replayed = self._enqueue(plan)
        self.assertEqual(queued["job_kind"], "agent_guangya_fs_change")
        self.assertNotIn("strm_scope", guangya_fs_change.load_fs_change_plan(plan["plan_id"]))

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
