from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from app import database as db
from app.modules.organize_tasks import OrganizeTaskManager
from app.repositories.organize_operation_jobs import (
    claim_organize_operation_job,
    enqueue_organize_operation_job,
    finish_organize_operation_job,
    get_organize_operation_job,
    is_organize_operation_cancel_requested,
    organize_operation_owner_digest,
    verify_organize_operation_payload,
    organize_operation_job_id_from_public_ref,
    organize_operation_public_ref,
)
from tests.support import IsolatedDatabaseTestCase


class OrganizeOperationJobRepositoryTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_operation_jobs")

    def _enqueue(self, *, dedupe: str = "owner:preview"):
        return enqueue_organize_operation_job(
            job_kind="agent_directory_scrape",
            owner="owner-durable-test",
            operation="目录刮削",
            reference="安全引用",
            payload={"version": 1},
            dedupe_key=dedupe,
        )

    def test_enqueue_is_idempotent_and_public_reference_round_trips(self) -> None:
        first, first_replayed = self._enqueue()
        second, second_replayed = self._enqueue()

        self.assertFalse(first_replayed)
        self.assertTrue(second_replayed)
        self.assertEqual(first["job_id"], second["job_id"])
        public_ref = organize_operation_public_ref(str(first["job_id"]))
        self.assertRegex(public_ref, r"^GY-(?:[0-9A-F]{4}-){7}[0-9A-F]{4}$")
        self.assertEqual(
            organize_operation_job_id_from_public_ref(public_ref),
            str(first["job_id"]),
        )

    def test_targeted_claim_cannot_bypass_older_pending_job(self) -> None:
        first, _ = self._enqueue(dedupe="owner:first")
        second, _ = self._enqueue(dedupe="owner:second")

        self.assertIsNone(claim_organize_operation_job(str(second["job_id"])))
        claimed_first = claim_organize_operation_job(str(first["job_id"]))
        self.assertEqual(claimed_first["job_id"], first["job_id"])

    def test_stopped_durable_result_is_persisted_as_partial(self) -> None:
        created, _ = self._enqueue(dedupe="owner:stopped")
        claimed = claim_organize_operation_job(str(created["job_id"]))
        manager = OrganizeTaskManager()
        manager._lock = threading.Lock()
        self.assertTrue(manager._lock.acquire(blocking=False))
        manager._task = {"id": str(created["job_id"]), "status": "running"}

        with patch.object(
            manager,
            "_execute_durable_operation",
            return_value={"stats": {"moved": 1, "stopped": 1}},
        ):
            manager._run_durable_operation(dict(claimed))

        terminal = get_organize_operation_job(str(created["job_id"]))
        self.assertEqual(terminal["status"], "partial")
        self.assertFalse(manager._lock.locked())

    def test_claim_and_finish_are_generation_fenced(self) -> None:
        created, _ = self._enqueue()
        claimed = claim_organize_operation_job(str(created["job_id"]))
        self.assertIsNotNone(claimed)
        generation = int(claimed["lease_generation"])

        self.assertFalse(finish_organize_operation_job(
            str(created["job_id"]),
            expected_lease_generation=generation + 1,
            status="completed",
            result={"stats": {"moved": 1}},
        ))
        self.assertTrue(finish_organize_operation_job(
            str(created["job_id"]),
            expected_lease_generation=generation,
            status="completed",
            result={"stats": {"moved": 1}},
        ))
        terminal = get_organize_operation_job(str(created["job_id"]))
        self.assertEqual(terminal["status"], "completed")

    def test_init_db_does_not_reclassify_a_live_running_operation(self) -> None:
        created, _ = self._enqueue(dedupe="owner:live")
        claimed = claim_organize_operation_job(str(created["job_id"]))
        self.assertEqual(claimed["status"], "running")

        db.init_db()

        current = get_organize_operation_job(str(created["job_id"]))
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["lease_generation"], claimed["lease_generation"])

    def test_dedupe_and_reads_are_owner_isolated(self) -> None:
        first, _ = self._enqueue(dedupe="shared-preview")
        second, replayed = enqueue_organize_operation_job(
            job_kind="agent_directory_scrape",
            owner="different-owner",
            operation="目录刮削",
            reference="安全引用",
            payload={"version": 1},
            dedupe_key="shared-preview",
        )
        self.assertFalse(replayed)
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(first["owner_digest"], second["owner_digest"])
        manager = OrganizeTaskManager()
        public_ref = organize_operation_public_ref(str(first["job_id"]))
        self.assertIsNotNone(manager.task_result(
            public_ref, owner="owner-durable-test"
        ))
        self.assertIsNone(manager.task_result(
            public_ref, owner="different-owner"
        ))
        self.assertIsNone(manager.task_result(public_ref))

    def test_payload_integrity_and_terminal_minimization(self) -> None:
        created, _ = self._enqueue(dedupe="owner:payload-auth")
        self.assertTrue(verify_organize_operation_payload(created))
        tampered = dict(created)
        tampered["payload_json"] = '{"version":2}'
        self.assertFalse(verify_organize_operation_payload(tampered))
        claimed = claim_organize_operation_job(str(created["job_id"]))
        self.assertTrue(finish_organize_operation_job(
            str(created["job_id"]),
            expected_lease_generation=int(claimed["lease_generation"]),
            status="completed",
        ))
        terminal = get_organize_operation_job(str(created["job_id"]))
        self.assertEqual(terminal["payload_json"], "{}")
        self.assertEqual(terminal["payload_auth"], "")
        self.assertEqual(terminal["reference"], "")
        self.assertEqual(terminal["result_json"], '{}')

    def test_per_owner_capacity_and_expired_confirmation(self) -> None:
        for index in range(4):
            self._enqueue(dedupe=f"owner:capacity:{index}")
        with self.assertRaises(RuntimeError):
            self._enqueue(dedupe="owner:capacity:overflow")
        with patch(
            "app.repositories.organize_operation_jobs.time.time",
            return_value=time.time() + 4_000,
        ):
            self.assertIsNone(claim_organize_operation_job())
        with db.get_conn() as conn:
            statuses = {
                str(row["status"])
                for row in conn.execute("SELECT status FROM organize_operation_jobs")
            }
        self.assertEqual(statuses, {"cancelled"})



class OrganizeDurableOperationManagerTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_operation_jobs")

    @staticmethod
    def _payload() -> dict:
        return {"version": 1, "safe": True}

    def test_pending_operation_survives_shutdown_and_runs_after_resume(self) -> None:
        first = OrganizeTaskManager()
        first._lock = threading.Lock()
        first._lock.acquire()
        queued = first.start_durable_operation(
            "目录刮削",
            "安全引用",
            job_kind="agent_directory_scrape",
            owner="owner-durable-restart",
            payload=self._payload(),
            dedupe_key="durable:restart",
        )
        self.assertTrue(queued["ok"])
        self.assertTrue(queued["queued"])
        first.begin_shutdown()
        first._lock.release()
        persisted = get_organize_operation_job(queued["task_id"])
        self.assertEqual(persisted["status"], "pending")

        second = OrganizeTaskManager()
        second._lock = threading.Lock()
        with patch.object(
            OrganizeTaskManager,
            "_execute_durable_operation",
            return_value={"stats": {"moved": 1}},
        ):
            second.resume()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = second.task_result(queued["task_id"])
                if result and result["status"] == "completed":
                    break
                time.sleep(0.01)
            else:
                self.fail("持久化操作未在恢复后执行")
        second.begin_shutdown()

    def test_durable_queue_is_aggregate_only_in_global_status(self) -> None:
        manager = OrganizeTaskManager()
        manager._lock = threading.Lock()
        manager._lock.acquire()
        try:
            queued = manager.start_durable_operation(
                "目录刮削", "PRIVATE DIRECTORY",
                job_kind="agent_directory_scrape",
                owner="owner-global-status",
                payload=self._payload(),
                dedupe_key="durable:global-status",
            )
            status = manager.task_status()["operation_queue"]
        finally:
            manager.begin_shutdown()
            manager._lock.release()
        self.assertTrue(queued["ok"])
        self.assertEqual(status["durable_pending_count"], 1)
        self.assertEqual(status["items"], [])
        self.assertNotIn("PRIVATE DIRECTORY", str(status))

    def test_owner_history_keeps_digest_and_public_ref_remains_queryable(self) -> None:
        owner = "owner-history-query"
        created, _ = enqueue_organize_operation_job(
            job_kind="agent_directory_scrape", owner=owner, operation="目录刮削",
            reference="安全引用", payload=self._payload(), dedupe_key="history-query",
        )
        claimed = claim_organize_operation_job(str(created["job_id"]))
        finish_organize_operation_job(
            str(created["job_id"]),
            expected_lease_generation=int(claimed["lease_generation"]),
            status="completed", result={"stats": {"moved": 1}, "directory": "PRIVATE"},
        )
        manager = OrganizeTaskManager()
        with manager._state_lock:
            manager._remember_task_locked({
                "id": str(created["job_id"]), "status": "completed",
                "operation": "目录刮削", "durable": True,
                "owner_digest": organize_operation_owner_digest(owner),
                "result": {"stats": {"moved": 1}},
            })
            manager._task = {"id": "different-task", "status": "running"}
        public_ref = organize_operation_public_ref(str(created["job_id"]))
        self.assertEqual(manager.task_result(public_ref, owner=owner)["status"], "completed")
        self.assertIsNone(manager.task_result(public_ref, owner="other-owner"))

    def test_live_worker_recovers_orphaned_running_job_when_process_lock_is_free(self) -> None:
        created, _ = enqueue_organize_operation_job(
            job_kind="agent_directory_scrape",
            owner="owner-orphaned",
            operation="目录刮削",
            reference="安全引用",
            payload=self._payload(),
            dedupe_key="durable:orphaned",
        )
        claimed = claim_organize_operation_job(str(created["job_id"]))
        self.assertEqual(claimed["status"], "running")

        manager = OrganizeTaskManager()
        manager._lock = threading.Lock()
        manager.resume()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            recovered = get_organize_operation_job(str(created["job_id"]))
            if recovered and recovered["status"] == "manual_review":
                break
            time.sleep(0.01)
        else:
            self.fail("存活 Worker 未收束失去执行者的 running 操作")
        self.assertEqual(recovered["error_code"], "WorkerExitedUnknownOutcome")
        manager.begin_shutdown()

    def test_live_cross_process_lock_prevents_false_orphan_recovery(self) -> None:
        created, _ = enqueue_organize_operation_job(
            job_kind="agent_directory_scrape",
            owner="owner-live-lock",
            operation="目录刮削",
            reference="安全引用",
            payload=self._payload(),
            dedupe_key="durable:live-lock",
        )
        claimed = claim_organize_operation_job(str(created["job_id"]))
        holder = OrganizeTaskManager()
        self.assertTrue(holder._lock.acquire(blocking=False))
        manager = OrganizeTaskManager()
        try:
            db.init_db()
            manager.resume()
            time.sleep(0.15)
            current = get_organize_operation_job(str(created["job_id"]))
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["lease_generation"], claimed["lease_generation"])
        finally:
            holder._lock.release()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = get_organize_operation_job(str(created["job_id"]))
            if current and current["status"] == "manual_review":
                break
            time.sleep(0.01)
        else:
            self.fail("跨进程锁释放后未收束孤儿任务")
        manager.begin_shutdown()

    def test_owner_purge_requests_running_cancel_and_removes_terminal_row(self) -> None:
        owner = "owner-privacy-running"
        created, _ = enqueue_organize_operation_job(
            job_kind="agent_directory_scrape",
            owner=owner,
            operation="目录刮削",
            reference="敏感引用",
            payload=self._payload(),
            dedupe_key="durable:privacy-running",
        )
        claimed = claim_organize_operation_job(str(created["job_id"]))
        deleted = db.purge_agent_subject_data(owner=owner)
        self.assertEqual(deleted["organize_operation_jobs"], 1)
        self.assertTrue(is_organize_operation_cancel_requested(
            str(created["job_id"]),
            expected_lease_generation=int(claimed["lease_generation"]),
        ))
        with db.get_conn() as conn:
            scrubbed = conn.execute(
                "SELECT payload_json,reference,cancel_requested FROM organize_operation_jobs "
                "WHERE job_id=?", (str(created["job_id"]),)
            ).fetchone()
        self.assertEqual(scrubbed["payload_json"], "{}")
        self.assertEqual(scrubbed["reference"], "")
        self.assertEqual(scrubbed["cancel_requested"], 1)
        self.assertTrue(finish_organize_operation_job(
            str(created["job_id"]),
            expected_lease_generation=int(claimed["lease_generation"]),
            status="cancelled",
        ))
        self.assertIsNone(get_organize_operation_job(str(created["job_id"])))

    def test_worker_start_and_terminal_write_failures_still_release_lock(self) -> None:
        manager = OrganizeTaskManager()
        manager._lock = threading.Lock()
        with patch(
            "app.modules.organize_tasks.threading.Thread.start",
            side_effect=RuntimeError("thread unavailable"),
        ), patch(
            "app.modules.organize_tasks.finish_organize_operation_job",
            side_effect=RuntimeError("database unavailable"),
        ):
            result = manager.start_durable_operation(
                "目录刮削", "安全引用",
                job_kind="agent_directory_scrape",
                owner="owner-worker-start-failure",
                payload=self._payload(),
                dedupe_key="durable:worker-start-failure",
            )
        self.assertFalse(result["ok"])
        self.assertTrue(manager._lock.acquire(blocking=False))
        manager._lock.release()

    def test_durable_dispatcher_start_failure_is_persisted_and_retryable(self) -> None:
        manager = OrganizeTaskManager()
        manager._lock = threading.Lock()
        manager._lock.acquire()
        try:
            with patch.object(
                manager,
                "_ensure_operation_dispatcher",
                side_effect=RuntimeError("thread unavailable"),
            ):
                result = manager.start_durable_operation(
                    "目录刮削",
                    "安全引用",
                    job_kind="agent_directory_scrape",
                    owner="owner-durable-dispatcher",
                    payload=self._payload(),
                    dedupe_key="durable:dispatcher-failure",
                )
        finally:
            manager._lock.release()

        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["error_code"], "queue_dispatcher_start_failed")
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT status,error_code FROM organize_operation_jobs "
                "ORDER BY created_at DESC,job_id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "queue_dispatcher_start_failed")


if __name__ == "__main__":
    unittest.main()
