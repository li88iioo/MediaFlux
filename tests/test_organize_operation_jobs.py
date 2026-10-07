from __future__ import annotations

import json
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app import database as db
from app.modules.organize_tasks import OrganizeTaskManager
from app.repositories.organize_operation_jobs import (
    _payload_auth,
    claim_organize_operation_job,
    enqueue_organize_operation_job,
    finish_organize_operation_job,
    get_organize_operation_job,
    get_organize_operation_job_for_owner,
    is_organize_operation_cancel_requested,
    organize_operation_job_id_from_public_ref,
    organize_operation_owner_digest,
    organize_operation_public_ref,
    recover_orphaned_organize_operation_jobs,
    sanitize_organize_operation_result,
    verify_organize_operation_payload,
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

    def test_non_durable_task_results_still_use_organize_counters(self) -> None:
        manager = OrganizeTaskManager()
        manager._task = {
            "id": "normal-organize", "status": "completed",
            "result": {"stats": {"total": 3, "moved": 3}},
        }
        for source in ("live", "history"):
            with self.subTest(source=source):
                result = manager.task_result("normal-organize")["result"]
                self.assertEqual(result["schema_version"], 1)
                self.assertEqual(result["counters"]["total"], 3)
                self.assertEqual(result["counters"]["moved"], 3)
                manager._remember_task_locked(manager._task)
                manager._task = {}

    def test_batch_operation_terminal_status_and_receipt_survive_history(self) -> None:
        for count, expected in ((2, "completed"), (1, "partial"), (0, "failed")):
            with self.subTest(completed=count):
                receipt = {
                    "success": count == 2, "requested": 2,
                    "completed": [{"log_id": index} for index in range(count)],
                    "failed": [{"log_id": index, "error": "目标冲突"} for index in range(count, 2)],
                    "warnings": ["联动通知延迟"],
                }
                manager = OrganizeTaskManager()
                manager._lock = threading.Lock()
                manager._lock.acquire()
                manager._task = {"id": "batch", "operation": "批量纠正", "status": "running"}
                with patch.object(manager, "_wake_download_tracker"):
                    manager._run_operation("batch", "批量纠正", "2条日志", lambda: receipt)
                self.assertEqual(manager.task_status()["status"], expected)
                for source in ("current", "history"):
                    with self.subTest(source=source):
                        result = manager.task_result("batch")
                        self.assertEqual(result["status"], expected)
                        self.assertEqual(result["result"], receipt)
                        manager._task = {"id": "next", "status": "running"}

    def test_operation_status_endpoint_selects_original_task_and_requires_login(self) -> None:
        from app.routes.guangya_api import router

        manager = OrganizeTaskManager()
        original = {"id": "batch", "operation": "批量纠正", "status": "partial",
                    "result": {"completed": [{"log_id": 1}], "failed": [{"log_id": 2, "error": "目标冲突"}]}}
        manager._remember_task_locked(original)
        manager._task = {"id": "next", "status": "running"}
        api = FastAPI()
        api.add_middleware(SessionMiddleware, secret_key="isolated-status-test")
        api.include_router(router)

        @api.post("/__test/login")
        def login(request: Request):
            request.session["logged_in"] = True
            return {"ok": True}

        with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager), TestClient(api) as client:
            self.assertIn(client.get("/api/guangya/organize/status?task_id=batch").status_code, (401, 403))
            client.post("/__test/login")
            response = client.get("/api/guangya/organize/status?task_id=batch")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["id"], "batch")
            self.assertEqual(response.json()["result"], original["result"])
            self.assertNotIn("operation_queue", response.json())
            self.assertEqual(client.get("/api/guangya/organize/status?task_id=missing").status_code, 404)
            self.assertEqual(client.get("/api/guangya/organize/status?task_id=next").status_code, 404)
            manager._task = {"id": "private", "operation": "Agent任务", "durable": True, "owner_digest": "another-owner"}
            self.assertEqual(client.get("/api/guangya/organize/status?task_id=private").status_code, 404)

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

    def test_result_sanitizer_keeps_scope_and_relocated_counters(self) -> None:
        result = {
            "stats": {
                "strm_scope_unknown": 1,
                "strm_trigger_skipped": 1,
                "relocated": 2,
                "leaked_text": "must be discarded",
            }
        }
        expected = {
            "stats": {
                "strm_scope_unknown": 1,
                "strm_trigger_skipped": 1,
                "relocated": 2,
            }
        }
        self.assertEqual(sanitize_organize_operation_result(result), expected)

        created, _ = self._enqueue(dedupe="owner:scope-stats")
        claimed = claim_organize_operation_job(str(created["job_id"]))
        self.assertTrue(finish_organize_operation_job(
            str(created["job_id"]),
            expected_lease_generation=int(claimed["lease_generation"]),
            status="partial",
            result=result,
        ))
        terminal = get_organize_operation_job(str(created["job_id"]))
        self.assertEqual(json.loads(terminal["result_json"]), expected)
        public_ref = organize_operation_public_ref(str(created["job_id"]))
        queried = OrganizeTaskManager().task_result(
            public_ref, owner="owner-durable-test"
        )
        self.assertEqual(queried["result"], expected)
        self.assertEqual(set(queried["result"]), {"stats"})

    def test_operation_items_are_sanitized_persisted_and_owner_scoped(self) -> None:
        raw_result = {
            "stats": {"total": 2, "relocated": 1, "private_counter": 99},
            "operation_items": [
                {
                    "position": 1,
                    "operation": "relocate",
                    "status": "completed",
                    "label": "第 1 集 password=ultra-private-secret",
                    "completed_actions": ["move", "rename"],
                    "provider_id": "provider-secret-id",
                    "file_id": "file-secret-id",
                    "raw_path": "/private/library/episode.mkv",
                    "raw_exception": "RuntimeError token=exception-secret",
                },
                {
                    "position": 2,
                    "operation": "copy",
                    "status": "failed",
                    "label": "复制：第 1 集 → target / clean",
                    "completed_actions": [],
                    "reason": "execution_error",
                    "path": "/private/library/episode.mkv",
                    "error": "private provider exception",
                },
                {"position": True, "operation": "move", "status": "completed"},
                {"position": 3, "operation": "raw_move", "status": "completed"},
                {"position": 4, "operation": "move", "status": "invented"},
                {
                    "position": 5,
                    "operation": "move",
                    "status": "completed",
                    "completed_actions": ["move", "rename", "trash"],
                },
                {
                    "position": 6,
                    "operation": "move",
                    "status": "completed",
                    "label": "/private/library/episode.mkv",
                    "completed_actions": [],
                    "provider_id": "only",
                },
                {"position": 7, "op": "move", "status": "completed"},
                {"position": 401, "operation": "move", "status": "completed"},
                {
                    "position": 8,
                    "operation": "rename",
                    "status": "not_started",
                    "label": "发布组-第1集-ABC123.mkv",
                },
            ],
            "provider_id": "top-level-provider-secret",
            "raw_path": "/private/library",
        }
        expected = sanitize_organize_operation_result(raw_result)
        self.assertEqual(expected["stats"], {"total": 2, "relocated": 1})
        self.assertEqual(len(expected["operation_items"]), 4)
        self.assertEqual(
            expected["operation_items"][0],
            {
                "position": 1,
                "operation": "relocate",
                "status": "completed",
                "completed_actions": ["move", "rename"],
            },
        )
        self.assertEqual(
            expected["operation_items"][1],
            {
                "position": 2,
                "operation": "copy",
                "status": "failed",
                "label": "复制:第 1 集 → target / clean",
                "completed_actions": [],
                "reason": "execution_error",
            },
        )
        self.assertEqual(
            expected["operation_items"][2],
            {
                "position": 6,
                "operation": "move",
                "status": "completed",
                "completed_actions": [],
            },
        )
        self.assertIn(
            "发布组-第1集",
            expected["operation_items"][3]["label"],
        )
        self.assertNotIn("provider-secret-id", str(expected))
        self.assertNotIn("private/library", str(expected))
        self.assertNotIn("exception-secret", str(expected))

        created, _ = self._enqueue(dedupe="owner:operation-items")
        claimed = claim_organize_operation_job(str(created["job_id"]))
        self.assertTrue(
            finish_organize_operation_job(
                str(created["job_id"]),
                expected_lease_generation=int(claimed["lease_generation"]),
                status="partial",
                result=raw_result,
            )
        )
        terminal = get_organize_operation_job(str(created["job_id"]))
        self.assertEqual(json.loads(terminal["result_json"]), expected)
        owner_row = get_organize_operation_job_for_owner(
            str(created["job_id"]), "owner-durable-test"
        )
        other_owner_row = get_organize_operation_job_for_owner(
            str(created["job_id"]), "someone-else"
        )
        self.assertIsNotNone(owner_row)
        self.assertIsNone(other_owner_row)

        public_ref = organize_operation_public_ref(str(created["job_id"]))
        manager = OrganizeTaskManager()
        queried = manager.task_result(public_ref, owner="owner-durable-test")
        self.assertEqual(queried["result"], expected)
        self.assertIsNone(manager.task_result(public_ref, owner="someone-else"))

    def test_operation_labels_preserve_media_filenames_not_tool_identifiers(self) -> None:
        label = "改名：Blue.Streak.1999.2160p.HDR.mkv → 笨贼妙探.1999.2160p.mkv"
        item = {"position": 1, "operation": "rename", "status": "not_started", "label": label}
        safe = sanitize_organize_operation_result({"operation_items": [item]})
        self.assertIn("Blue.Streak.1999.2160p.HDR.mkv", safe["operation_items"][0]["label"])
        self.assertIn("笨贼妙探.1999.2160p.mkv", safe["operation_items"][0]["label"])
        self.assertNotIn("内部检查", safe["operation_items"][0]["label"])

    def test_operation_item_labels_are_utf8_bounded_and_400_items_fit_result_budget(self) -> None:
        long_label = "开头" + "集" * 100 + "结尾"
        single = sanitize_organize_operation_result(
            {
                "stats": {},
                "operation_items": [
                    {
                        "position": 1,
                        "operation": "rename",
                        "status": "completed",
                        "label": long_label,
                    }
                ],
            }
        )["operation_items"][0]["label"]
        self.assertLessEqual(len(single.encode("utf-8")), 128)
        self.assertTrue(single.startswith("开头"))
        self.assertTrue(single.endswith("结尾"))

        for kind, maximum_label in (("unicode", "😀" * 32), ("json_escape", '"' * 128)):
            with self.subTest(kind=kind):
                worst_case = sanitize_organize_operation_result(
                    {
                        "stats": {"total": 400},
                        "operation_items": [
                            {
                                "position": index + 1,
                                "operation": "create_directory",
                                "status": "not_started",
                                "label": maximum_label,
                                "completed_actions": ["create_directory", "create_directory"],
                                "reason": "dependency_failed",
                            }
                            for index in range(400)
                        ],
                    }
                )
                self.assertEqual(len(worst_case["operation_items"]), 400)
                self.assertTrue(
                    all(
                        len(item["label"].encode("utf-8")) <= 128
                        for item in worst_case["operation_items"]
                    )
                )
                serialized = json.dumps(
                    worst_case, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ).encode("utf-8")
                self.assertLess(len(serialized), 131_072)
                created, _ = self._enqueue(dedupe=f"label-budget:{kind}")
                claimed = claim_organize_operation_job(str(created["job_id"]))
                self.assertTrue(finish_organize_operation_job(
                    str(created["job_id"]), expected_lease_generation=int(claimed["lease_generation"]),
                    status="partial", result=worst_case,
                ))
                persisted = get_organize_operation_job(str(created["job_id"]))
                self.assertEqual(json.loads(persisted["result_json"]), worst_case)

    def test_old_manual_review_history_is_pruned_on_next_enqueue(self) -> None:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO organize_operation_jobs("
                "job_id,job_kind,owner_digest,operation,dedupe_digest,status,"
                "expires_at,created_at,updated_at) VALUES(?,?,?,?,?,'manual_review',?,?,?)",
                (
                    "f" * 32, "agent_guangya_cleanup", "legacy-owner",
                    "旧清理任务", "legacy-dedupe", 0,
                    "2000-01-01 00:00:00.000000",
                    "2000-01-01 00:00:00.000000",
                ),
            )
        self._enqueue(dedupe="owner:trigger-retention")
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS total FROM organize_operation_jobs "
                "WHERE status='manual_review'"
            ).fetchone()["total"]
        self.assertEqual(count, 0)

    def test_manual_review_history_has_a_global_capacity_bound(self) -> None:
        timestamp = db.now()
        with db.get_conn() as conn:
            for index in range(3):
                conn.execute(
                    "INSERT INTO organize_operation_jobs("
                    "job_id,job_kind,owner_digest,operation,dedupe_digest,status,"
                    "expires_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'manual_review',?,?,?)",
                    (
                        f"{index + 1:032x}", "agent_guangya_rename",
                        f"owner-{index}", "待人工核对", f"dedupe-{index}",
                        0, timestamp, timestamp,
                    ),
                )
        with patch(
            "app.repositories.organize_operation_jobs._MAX_MANUAL_REVIEW_HISTORY_GLOBAL",
            2,
        ):
            self._enqueue(dedupe="owner:trigger-global-cap")
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS total FROM organize_operation_jobs "
                "WHERE status='manual_review'"
            ).fetchone()["total"]
        self.assertEqual(count, 2)

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

    def _enqueue_fs_change(self, *, dedupe: str, case: str = "eligible"):
        owner = f"owner-fs-resume-{dedupe}"
        owner_digest = organize_operation_owner_digest(owner)
        payload = {
            "version": 1,
            "owner_digest": owner_digest,
            "plan_id": "a" * 32,
            "plan_fingerprint": "b" * 64,
            "case": case,
        }
        with patch("app.modules.guangya_fs_change.bind_fs_change_plan_job"):
            created, _ = enqueue_organize_operation_job(
                job_kind="agent_guangya_fs_change",
                owner=owner,
                operation="文件变更",
                reference=f"安全引用-{dedupe}",
                payload=payload,
                dedupe_key=f"fs-resume:{dedupe}",
            )
        return created, payload

    def test_orphan_fs_change_requeues_only_when_plan_is_resumable(self) -> None:
        created, _payload = self._enqueue_fs_change(dedupe="eligible")
        claimed = claim_organize_operation_job(str(created["job_id"]))
        self.assertEqual(claimed["status"], "running")
        old_generation = int(claimed["lease_generation"])
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_operation_jobs SET result_json=? WHERE job_id=?",
                ('{"stats":{"moved":2}}', str(created["job_id"])),
            )
        before = get_organize_operation_job(str(created["job_id"]))

        with (
            patch(
                "app.modules.guangya_fs_change.can_resume_fs_change_plan",
                create=True,
                return_value=True,
            ) as can_resume,
            patch(
                "app.modules.guangya_fs_change.finalize_fs_change_plan_job"
            ) as finalize,
        ):
            self.assertEqual(recover_orphaned_organize_operation_jobs(), 1)

        can_resume.assert_called_once_with(
            json.loads(before["payload_json"]),
            job_id=str(created["job_id"]),
            owner_digest=str(before["owner_digest"]),
            lease_generation=old_generation,
        )
        finalize.assert_not_called()
        resumed = get_organize_operation_job(str(created["job_id"]))
        self.assertEqual(resumed["status"], "pending")
        self.assertEqual(resumed["lease_generation"], old_generation + 1)
        self.assertEqual(resumed["error_code"], "WorkerInterruptedResume")
        self.assertEqual(resumed["error"], "")
        for field in (
            "payload_json", "payload_auth", "reference", "dedupe_digest",
            "expires_at", "result_json",
        ):
            self.assertEqual(resumed[field], before[field], field)

        next_claim = claim_organize_operation_job(str(created["job_id"]))
        self.assertEqual(next_claim["status"], "running")
        self.assertEqual(next_claim["lease_generation"], old_generation + 2)
        self.assertFalse(
            finish_organize_operation_job(
                str(created["job_id"]),
                expected_lease_generation=old_generation,
                status="completed",
            )
        )

    def test_orphan_fs_change_exclusions_and_legacy_payload_stay_manual_review(
        self,
    ) -> None:
        cases = {
            case: self._enqueue_fs_change(dedupe=case, case=case)[0]
            for case in (
                "false",
                "raises",
                "tampered",
                "legacy",
                "cancelled",
                "purged",
                "expired",
            )
        }
        other_kind, _ = enqueue_organize_operation_job(
            job_kind="agent_directory_scrape",
            owner="owner-fs-resume-other-kind",
            operation="目录刮削",
            reference="安全引用",
            payload=self._payload(),
            dedupe_key="fs-resume:other-kind",
        )
        # Make every row running before applying the recovery exclusions.
        for _ in range(len(cases) + 1):
            self.assertIsNotNone(claim_organize_operation_job())
        with db.get_conn() as conn:
            tampered_id = str(cases["tampered"]["job_id"])
            conn.execute(
                "UPDATE organize_operation_jobs SET payload_json=? WHERE job_id=?",
                ('{"version":1,"tampered":true}', tampered_id),
            )
            legacy_id = str(cases["legacy"]["job_id"])
            legacy_payload = json.dumps(
                {"version": 1, "case": "legacy"},
                separators=(",", ":"),
                sort_keys=True,
            )
            legacy = conn.execute(
                "SELECT owner_digest FROM organize_operation_jobs WHERE job_id=?",
                (legacy_id,),
            ).fetchone()
            conn.execute(
                "UPDATE organize_operation_jobs SET payload_json=?,payload_auth=? "
                "WHERE job_id=?",
                (
                    legacy_payload,
                    _payload_auth(
                        job_id=legacy_id,
                        owner_digest=str(legacy["owner_digest"]),
                        job_kind="agent_guangya_fs_change",
                        payload_json=legacy_payload,
                    ),
                    legacy_id,
                ),
            )
            conn.execute(
                "UPDATE organize_operation_jobs SET cancel_requested=1 WHERE job_id=?",
                (str(cases["cancelled"]["job_id"]),),
            )
            conn.execute(
                "UPDATE organize_operation_jobs SET purged_at=? WHERE job_id=?",
                (db.now(), str(cases["purged"]["job_id"])),
            )
            conn.execute(
                "UPDATE organize_operation_jobs SET expires_at=? WHERE job_id=?",
                (time.time() - 1, str(cases["expired"]["job_id"])),
            )

        def cannot_resume(payload, *_args):
            if payload.get("case") == "raises":
                raise RuntimeError("plan unavailable")
            return False

        with (
            patch(
                "app.modules.guangya_fs_change.can_resume_fs_change_plan",
                create=True,
                side_effect=cannot_resume,
            ) as can_resume,
            patch("app.modules.guangya_fs_change.finalize_fs_change_plan_job"),
        ):
            self.assertEqual(recover_orphaned_organize_operation_jobs(), len(cases) + 1)

        self.assertEqual(can_resume.call_count, 3)
        self.assertEqual(
            {str(call.args[0].get("case")) for call in can_resume.call_args_list},
            {"false", "raises", "legacy"},
        )
        self.assertEqual(
            get_organize_operation_job(str(cases["cancelled"]["job_id"]))["status"],
            "cancelled",
        )
        self.assertIsNone(get_organize_operation_job(str(cases["purged"]["job_id"])))
        for case in ("false", "raises", "tampered", "legacy", "expired"):
            row = get_organize_operation_job(str(cases[case]["job_id"]))
            self.assertEqual(row["status"], "manual_review", case)
            self.assertEqual(row["error_code"], "WorkerExitedUnknownOutcome", case)
        other = get_organize_operation_job(str(other_kind["job_id"]))
        self.assertEqual(other["status"], "manual_review")
        self.assertEqual(other["error_code"], "WorkerExitedUnknownOutcome")

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
