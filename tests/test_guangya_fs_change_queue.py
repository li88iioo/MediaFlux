"""通用光鸭文件变更持久队列迁移与结果投影测试。"""

from __future__ import annotations

from app.modules.process_lock import CrossProcessLock
from threading import Thread
from copy import deepcopy
from collections import Counter
from functools import partial
from dataclasses import asdict

from typing import cast
from collections.abc import Callable
from app.clients.guangya import GuangYaClient, GuangYaFile

import asyncio
import json
import os
import subprocess
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app import database, database_migrations
from app.modules.organize_tasks import get_organize_operation_status
from app.agent.effect_completion import wait_for_effect_completion
from app.agent.models import ToolContext, ToolResult
from app.modules import guangya_fs_change
from app.modules.organize_tasks import OrganizeOperationCancelled, OrganizeTaskManager
from app.repositories.organize_operation_jobs import (
    claim_organize_operation_job,
    count_pending_organize_operation_jobs,
    enqueue_organize_operation_job,
    fail_pending_organize_operation_job,
    finish_organize_operation_job,
    get_organize_operation_job,
    organize_operation_owner_digest,
    organize_operation_public_ref,
    request_cancel_organize_operation_job,
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
        self.enterContext(mock.patch("socket.socket.connect", side_effect=AssertionError("禁止外联")))
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

    def _file_id_plan(self, client=None, operations=None):
        from tests.test_agent_guangya_fs_gateway import FakeGatewayClient

        client = client or FakeGatewayClient()
        plan = guangya_fs_change.build_fs_change_plan(
            cast(GuangYaClient, client),
            owner="queue-owner",
            trigger_strm=False,
            operations=operations
            or [{"op": "rename", "file_id": "rename", "new_name": "New.mp4"}],
        )
        return client, plan

    def _submit_shared(self, plan, **kwargs):
        manager = OrganizeTaskManager()
        manager._worker = cast(Thread, object())
        with (
            mock.patch(
                "app.modules.organize_tasks.get_organize_manager", return_value=manager
            ),
            mock.patch.object(manager, "_ensure_operation_dispatcher"),
        ):
            return guangya_fs_change.submit_fs_change_plan(
                plan["plan_id"],
                owner="queue-owner",
                expected_fingerprint=plan["fingerprint"],
                **kwargs,
            )

    def test_file_id_preflight_is_readonly_and_returns_every_frozen_target(self):
        from tests.test_agent_guangya_fs_gateway import FakeGatewayClient

        client = FakeGatewayClient()
        operations = [
            {"op": "rename", "file_id": "rename", "new_name": "New.mp4"},
            {"op": "move", "file_id": "move", "target_path": "/target"},
            {"op": "trash", "file_id": "trash"},
        ]
        for index in range(8):
            file_id = f"Copy.Mixed-{index}"
            client.directories["source"].append(
                GuangYaFile(
                    file_id,
                    f"Copy-{index}.mp4",
                    False,
                    parent_id="source",
                    size=12,
                    etag=file_id,
                )
            )
            operations.append(
                {"op": "copy", "file_id": file_id, "target_path": "/target"}
            )
        original_operations, original_files = (
            deepcopy(operations),
            deepcopy(client.directories),
        )
        self.assertFalse(hasattr(guangya_fs_change, "capture_observation_source"))
        _, plan = self._file_id_plan(client, operations)
        self.assertEqual(client.directories, original_files)
        self.assertEqual(operations, original_operations)
        self.assertEqual(
            (plan["stats"]["total"], len(plan["operations"]), len(plan["samples"])),
            (11, 11, 6),
        )
        self.assertEqual(
            {item["source"]["file_id"] for item in plan["operations"]},
            {item["file_id"] for item in operations},
        )
        for item in plan["operations"]:
            self.assertNotIn("object_ref", item)
            self.assertTrue(item["source_path"].startswith("/source/"))
            if item["op"] in {"move", "copy"}:
                self.assertEqual(
                    (item["target_id"], item["target_path"]), ("target", "/target")
                )
        self.assertNotIn("observation_ref", plan)
        self.assertNotIn("observation_source", plan)
        json.dumps(plan, allow_nan=False)

    def test_readonly_preflight_shares_source_and_target_details(self):
        client, plan = self._file_id_plan(operations=[
            {"op": "relocate", "file_id": "rename", "target_path": "/target", "new_name": "New.mp4"},
            {"op": "move", "file_id": "move", "target_path": "/target"},
        ])
        before = deepcopy(client.directories)
        outcomes = [{"status": "not_started"} for _ in plan["operations"]]
        with mock.patch.object(client, "file_info", wraps=client.file_info) as info:
            guangya_fs_change._preflight_fs_change_plan(client, plan["operations"], outcomes, None)
        self.assertEqual(Counter(call.args[0] for call in info.call_args_list),
                         {"rename": 1, "move": 1, "source": 1, "target": 1})
        self.assertEqual(client.directories, before)

    def test_deep_batch_preflight_bounds_total_provider_reads(self):
        from tests.test_agent_guangya_fs_gateway import FakeGatewayClient

        client = FakeGatewayClient()
        source, target = "source", "target"
        target_path = "/target"
        for depth in range(6):
            source = client.create_dir(f"source-{depth}", source)
            target = client.create_dir(f"target-{depth}", target)
            target_path += f"/target-{depth}"
        client.directories[source] = [
            GuangYaFile(f"file-{i}", f"Old-{i}.mp4", False, parent_id=source, size=10, etag=str(i))
            for i in range(20)
        ]
        _, plan = self._file_id_plan(client, [
            {"op": "relocate", "file_id": f"file-{i}", "target_path": target_path, "new_name": f"New-{i}.mp4"}
            for i in range(20)
        ])
        before = deepcopy(client.directories)
        with mock.patch.object(client, "file_info", wraps=client.file_info) as info, \
             mock.patch.object(client, "list_dir", wraps=client.list_dir) as listing:
            guangya_fs_change._preflight_fs_change_plan(
                client, plan["operations"], [{"status": "not_started"} for _ in plan["operations"]], None)
        # 20 个源对象及两侧各 7 层目录；原实现共 114 次详情/列表请求。
        self.assertLessEqual(info.call_count, 34)
        self.assertEqual(Counter(call.args[0] for call in listing.call_args_list), {source: 1, target: 1})
        self.assertEqual(client.directories, before)

    def test_preflight_fallback_reuses_one_parent_listing_for_conflicts(self):
        client, plan = self._file_id_plan()
        original_info = client.file_info

        def info(file_id):
            if file_id == "rename":
                raise TimeoutError("detail unavailable")
            return original_info(file_id)

        with mock.patch.object(client, "file_info", side_effect=info) as details, \
             mock.patch.object(client, "list_dir", wraps=client.list_dir) as listing:
            guangya_fs_change._preflight_operation(cast(GuangYaClient, client), plan["operations"][0])
        self.assertEqual(Counter(call.args[0] for call in details.call_args_list), {"rename": 1, "source": 1})
        listing.assert_called_once_with("source")

    def test_next_write_rechecks_shared_ancestor_after_first_write(self):
        client, plan = self._file_id_plan(operations=[
            {"op": "move", "file_id": file_id, "target_path": "/target"}
            for file_id in ("rename", "move")
        ])
        task = self._submit_shared(plan)
        claimed = claim_organize_operation_job(task["task_id"])
        assert claimed is not None
        original_move = client.move

        def move(file_ids, parent_id):
            result = original_move(file_ids, parent_id)
            client.directories["0"][0].name = "external-rename"
            return result

        with mock.patch.object(client, "move", side_effect=move) as moved:
            result = guangya_fs_change.execute_fs_change_plan(
                json.loads(claimed["payload_json"]), client_factory=lambda: cast(GuangYaClient, client),
                lease_generation=claimed["lease_generation"])
        moved.assert_called_once_with(["rename"], "target")
        self.assertTrue(result["partial"])
        self.assertEqual(result["stats"]["moved"], 1)
        self.assertEqual(result["operation_items"][1]["status"], "blocked")
        self.assertEqual(result["operation_items"][1]["reason"], "precondition_failed")

    def test_copy_half_preflight_reuses_target_details_and_replica_listing(self):
        client, plan = self._file_id_plan(operations=[
            {"op": "copy", "file_id": "move", "target_path": "/target", "new_name": "Backup.mp4"},
        ])
        client.copy(["move"], "target")
        replica = asdict(client.list_dir("target")[0])
        before = deepcopy(client.directories)
        with mock.patch.object(client, "file_info", wraps=client.file_info) as info, \
             mock.patch.object(client, "list_dir", wraps=client.list_dir) as listing:
            guangya_fs_change._preflight_operation(cast(GuangYaClient, client), plan["operations"][0], source_snapshot=replica)
        self.assertEqual(Counter(call.args[0] for call in info.call_args_list),
                         {replica["file_id"]: 1, "move": 1, "target": 1})
        listing.assert_called_once_with("target")
        self.assertEqual(client.directories, before)
        # 目录缓存不能吞掉 Provider 的重复行，从而误认副本身份唯一。
        with mock.patch.object(client, "list_dir", return_value=client.list_dir("target") * 2):
            with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                guangya_fs_change._preflight_operation(cast(GuangYaClient, client),
                                                      plan["operations"][0], source_snapshot=replica)

    def test_target_path_error_explains_directory_contract_without_writes(self):
        from tests.test_agent_guangya_fs_gateway import FakeGatewayClient

        for target in ("/source/Move.mp4", "/target/new/Backup.mp4"):
            with self.subTest(target=target):
                client = FakeGatewayClient()
                before = deepcopy(client.directories)
                operations = [
                    {"op": "create_directory", "parent_path": "/target", "name": "new"},
                    {"op": "copy", "file_id": "move", "target_path": target, "new_name": "Backup.mp4"},
                ]
                original_operations = deepcopy(operations)
                with self.assertRaises((guangya_fs_change.GuangYaFSChangeError,
                                        guangya_fs_change.GuangYaWorkspaceError)) as error:
                    self._file_id_plan(client, operations)
                for text in ("target_path", "目录", "new_name", "预检未执行云端写入"):
                    self.assertIn(text, str(error.exception))
                self.assertEqual(client.directories, before)
                self.assertEqual(operations, original_operations)
                self.assertFalse(self.plan_dir.exists())
        _, valid = self._file_id_plan(operations=[
            {"op": "create_directory", "parent_path": "/target", "name": "new"},
            {"op": "copy", "file_id": "move", "target_path": "/target/new", "new_name": "Backup.mp4"},
        ])
        self.assertEqual([item["op"] for item in valid["operations"]], ["create_directory", "copy"])

    def test_file_id_preflight_rejects_missing_root_and_ambiguous_parameters(self):
        for operation in (
            {"op": "trash", "file_id": "missing"},
            {"op": "trash", "file_id": "0"},
            {"op": "trash", "file_id": True},
            {"op": "trash", "file_id": "trash", "permanent": True},
            {"op": "rename", "file_id": "rename", "new_name": "New.txt"},
        ):
            with (
                self.subTest(operation=operation),
                self.assertRaises(guangya_fs_change.GuangYaFSChangeError),
            ):
                self._file_id_plan(operations=[operation])
        self.assertFalse(self.plan_dir.exists())

    def test_shared_submission_reuses_job_and_preserves_admission_transaction(self):
        _, plan = self._file_id_plan()
        admitted = mock.Mock()
        first = self._submit_shared(plan, on_admitted=admitted)
        second = self._submit_shared(plan, on_admitted=admitted)
        self.assertEqual(
            (first["status"], first["queued"], first["replayed"]),
            ("pending", True, False),
        )
        self.assertEqual(
            (second["task_id"], second["replayed"]), (first["task_id"], True)
        )
        self.assertEqual(second["expires_at"], first["expires_at"])
        self.assertEqual(
            [call.args[2] for call in admitted.call_args_list], [False, True]
        )
        row = get_organize_operation_job(first["task_id"])
        assert row is not None
        self.assertEqual(row["job_kind"], "agent_guangya_fs_change")
        self.assertTrue(verify_organize_operation_payload(row))
        self.assertEqual(
            guangya_fs_change._read(plan["plan_id"])["job_id"], row["job_id"]
        )
        self.assertEqual(count_pending_organize_operation_jobs(), 1)
        for owner, fingerprint in (
            ("another-owner", plan["fingerprint"]),
            ("queue-owner", "0" * 64),
            ("queue-owner", ""),
        ):
            with (
                self.subTest(owner=owner, fingerprint=fingerprint),
                self.assertRaises(guangya_fs_change.GuangYaFSChangeError),
            ):
                guangya_fs_change.submit_fs_change_plan(
                    plan["plan_id"], owner=owner, expected_fingerprint=fingerprint
                )

    def test_shared_submission_denied_admission_rolls_back_before_binding(self):
        _, plan = self._file_id_plan()
        from app.repositories.organize_operation_jobs import (
            OrganizeOperationAdmissionError,
        )

        with self.assertRaises(OrganizeOperationAdmissionError):
            self._submit_shared(
                plan, on_admitted=mock.Mock(side_effect=PermissionError("denied"))
            )
        self.assertEqual(count_pending_organize_operation_jobs(), 0)
        self.assertNotIn("job_id", guangya_fs_change._read(plan["plan_id"]))
        self.assertTrue(self._submit_shared(plan)["ok"])

    def test_shared_submission_recovers_commit_before_local_claim_failure(self):
        _, plan = self._file_id_plan()
        manager = OrganizeTaskManager()
        manager._lock = cast(CrossProcessLock, threading.Lock())
        with (
            mock.patch(
                "app.modules.organize_tasks.get_organize_manager", return_value=manager
            ),
            mock.patch(
                "app.modules.organize_tasks.claim_organize_operation_job",
                side_effect=RuntimeError("claim failed"),
            ),
        ):
            task = guangya_fs_change.submit_fs_change_plan(
                plan["plan_id"],
                owner="queue-owner",
                expected_fingerprint=plan["fingerprint"],
            )
        self.assertEqual(
            (task["status"], task["queued"], task["replayed"]), ("pending", True, True)
        )
        self.assertEqual(self._submit_shared(plan)["task_id"], task["task_id"])
        self.assertEqual(count_pending_organize_operation_jobs(), 1)

    def test_shared_submission_dispatch_failure_is_not_reported_as_accepted(self):
        _, plan = self._file_id_plan()
        manager = OrganizeTaskManager()
        manager._worker = cast(Thread, object())
        with (
            mock.patch(
                "app.modules.organize_tasks.get_organize_manager", return_value=manager
            ),
            mock.patch.object(
                manager,
                "_ensure_operation_dispatcher",
                side_effect=RuntimeError("dispatcher failed"),
            ),
            self.assertRaises(guangya_fs_change.GuangYaFSChangeError),
        ):
            guangya_fs_change.submit_fs_change_plan(
                plan["plan_id"],
                owner="queue-owner",
                expected_fingerprint=plan["fingerprint"],
            )
        persisted = guangya_fs_change._read(plan["plan_id"])
        self.assertEqual(persisted["status"], "failed")
        job = get_organize_operation_job(persisted["job_id"])
        assert job is not None
        self.assertEqual(job["status"], "failed")

    def test_cancelled_shared_task_is_not_resubmitted(self):
        _, plan = self._file_id_plan()
        task = self._submit_shared(plan)
        row, outcome = request_cancel_organize_operation_job(
            task["task_id"], owner="queue-owner", expected_lease_generation=0
        )
        assert row is not None
        self.assertEqual(outcome, "cancelled")
        self.assertEqual(row["status"], "cancelled")
        with mock.patch(
            "app.modules.organize_tasks.get_organize_manager",
            side_effect=AssertionError("终态不再入队"),
        ):
            repeated = guangya_fs_change.submit_fs_change_plan(
                plan["plan_id"],
                owner="queue-owner",
                expected_fingerprint=plan["fingerprint"],
            )
        self.assertEqual(
            (repeated["task_id"], repeated["status"], repeated["replayed"]),
            (task["task_id"], "cancelled", True),
        )

    def test_expired_shared_task_does_not_create_or_extend_another_job(self):
        _, plan = self._file_id_plan()
        task = self._submit_shared(plan)
        with (
            mock.patch.object(
                guangya_fs_change.time, "time", return_value=task["expires_at"] + 1
            ),
            mock.patch(
                "app.modules.organize_tasks.get_organize_manager",
                side_effect=AssertionError("过期不重新入队"),
            ),
        ):
            expired = guangya_fs_change.submit_fs_change_plan(
                plan["plan_id"],
                owner="queue-owner",
                expected_fingerprint=plan["fingerprint"],
            )
        row = get_organize_operation_job(task["task_id"])
        assert row is not None
        self.assertEqual(
            (expired["task_id"], expired["status"]), (task["task_id"], "cancelled")
        )
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["expires_at"], task["expires_at"])
        self.assertEqual(count_pending_organize_operation_jobs(), 0)
        with database.get_conn() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[
                    0
                ],
                1,
            )

    def test_real_id_plan_runs_through_common_worker_and_returns_same_terminal_job(
        self,
    ):
        from tests.test_agent_guangya_fs_gateway import FakeGatewayClient

        client = FakeGatewayClient()
        client.directories["source"].append(
            GuangYaFile(
                "Copy.ID", "Copy.mp4", False, parent_id="source", size=12, etag="copy"
            )
        )
        _, plan = self._file_id_plan(
            client,
            [
                {"op": "rename", "file_id": "rename", "new_name": "New.mp4"},
                {"op": "move", "file_id": "move", "target_path": "/target"},
                {"op": "copy", "file_id": "Copy.ID", "target_path": "/target"},
                {"op": "trash", "file_id": "trash"},
                {"op": "create_directory", "parent_path": "/target", "name": "New"},
            ],
        )
        task = self._submit_shared(plan)
        claimed = claim_organize_operation_job(task["task_id"])
        assert claimed is not None
        execute = partial(
            guangya_fs_change.execute_fs_change_plan,
            client_factory=lambda: cast(GuangYaClient, client),
        )
        with (
            mock.patch.object(guangya_fs_change, "execute_fs_change_plan", execute),
            mock.patch.object(client, "delete", wraps=client.delete) as recycle,
        ):
            result = OrganizeTaskManager._execute_durable_operation(dict(claimed))
        assert isinstance(result, dict)
        self.assertEqual(
            {
                key: result["stats"][key]
                for key in ("renamed", "moved", "copied", "trashed", "created")
            },
            {"renamed": 1, "moved": 1, "copied": 1, "trashed": 1, "created": 1},
        )
        self.assertFalse(result["partial"])
        recycle.assert_called_once_with(["trash"])
        self.assertTrue(
            finish_organize_operation_job(
                task["task_id"],
                expected_lease_generation=claimed["lease_generation"],
                status="completed",
                result=result,
            )
        )
        with mock.patch(
            "app.modules.organize_tasks.get_organize_manager",
            side_effect=AssertionError("终态不再入队"),
        ):
            repeated = guangya_fs_change.submit_fs_change_plan(
                plan["plan_id"],
                owner="queue-owner",
                expected_fingerprint=plan["fingerprint"],
            )
        self.assertEqual(
            (repeated["task_id"], repeated["status"], repeated["replayed"]),
            (task["task_id"], "completed", True),
        )

    def test_cold_process_restores_complete_signed_plan_and_original_queue_payload(
        self,
    ):
        from tests.test_agent_guangya_fs_gateway import FakeGatewayClient

        client = FakeGatewayClient()
        operations = [
            {"op": "move", "file_id": "move", "target_path": "/target"},
            {"op": "copy", "file_id": "rename", "target_path": "/target"},
            {"op": "trash", "file_id": "trash"},
        ]
        for index in range(7):
            file_id = f"cold-{index}"
            client.directories["source"].append(
                GuangYaFile(
                    file_id,
                    f"Old-{index}.mp4",
                    False,
                    parent_id="source",
                    size=12,
                    etag=file_id,
                )
            )
            operations.append(
                {"op": "rename", "file_id": file_id, "new_name": f"New-{index}.mp4"}
            )
        _, plan = self._file_id_plan(client, operations)
        task = self._submit_shared(plan)
        state_path = Path(self.temp.name) / "fake-cloud.json"
        from app.repositories import agent_jobs, organize_operation_jobs as jobs

        state_path.write_text(
            json.dumps(
                {
                    "directories": {
                        key: [asdict(item) for item in items]
                        for key, items in client.directories.items()
                    },
                    "plan_secret": guangya_fs_change.get_web_secret(),
                    "queue_secret": jobs.get_web_secret(),
                    "owner_secret": agent_jobs.get_web_secret(),
                }
            )
        )
        script = r"""
import tests
import json, os, socket, sys
from functools import partial
from pathlib import Path
from unittest.mock import patch
def deny(*args, **kwargs):
    raise AssertionError("cold restart forbids network")
for method in ("connect", "connect_ex", "sendto"):
    setattr(socket.socket, method, deny)
from app import database
database.configure_database(sys.argv[1], test_mode=True)
database.init_db()
from app.clients.guangya import GuangYaFile
from app.modules import guangya_fs_change as fs
from app.modules.organize_tasks import OrganizeTaskManager
from app.repositories import agent_jobs, organize_operation_jobs as jobs
from tests.test_agent_guangya_fs_gateway import FakeGatewayClient
state = json.loads(Path(sys.argv[6]).read_text())
fs._directory = lambda: Path(sys.argv[2])
fs.get_web_secret = lambda: state["plan_secret"]
jobs.get_web_secret = lambda: state["queue_secret"]
agent_jobs.get_web_secret = lambda: state["owner_secret"]
plan = fs.load_fs_change_plan(sys.argv[3], owner="queue-owner", expected_fingerprint=sys.argv[4])
assert "observation_ref" not in plan and "observation_source" not in plan
row = jobs.get_organize_operation_job(sys.argv[5])
assert row["job_id"] == plan["job_id"] and jobs.verify_organize_operation_payload(row)
client = FakeGatewayClient()
client.directories = {key: [GuangYaFile(**item) for item in items] for key, items in state["directories"].items()}
claimed = jobs.claim_organize_operation_job(row["job_id"])
execute = partial(fs.execute_fs_change_plan, client_factory=lambda: client)
with patch.object(fs, "execute_fs_change_plan", execute):
    result = OrganizeTaskManager._execute_durable_operation(dict(claimed))
assert jobs.finish_organize_operation_job(row["job_id"], expected_lease_generation=claimed["lease_generation"], status="completed", result=result)
print(json.dumps({"pid": os.getpid(), "job_id": row["job_id"], "stats": result["stats"],
                  "ids": [item["source"]["file_id"] for item in plan["operations"]],
                  "names": [item.name for item in client.directories["source"]],
                  "target_ids": [item["target_id"] for item in plan["operations"] if "target_id" in item]}))
"""
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(self.test_db_path),
                str(self.plan_dir),
                plan["plan_id"],
                plan["fingerprint"],
                task["task_id"],
                str(state_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        restored = json.loads(child.stdout)
        self.assertNotEqual(restored["pid"], os.getpid())
        self.assertEqual(restored["job_id"], task["task_id"])
        self.assertEqual(set(restored["ids"]), {item["file_id"] for item in operations})
        self.assertEqual((restored["stats"]["total"], len(plan["samples"])), (10, 6))
        self.assertEqual(
            (
                restored["stats"]["renamed"],
                restored["stats"]["copied"],
                restored["stats"]["moved"],
                restored["stats"]["trashed"],
            ),
            (7, 1, 1, 1),
        )
        self.assertEqual(restored["target_ids"], ["target", "target"])
        self.assertTrue(
            {f"New-{index}.mp4" for index in range(7)}.issubset(restored["names"])
        )
        repeated = self._submit_shared(plan)
        self.assertEqual(
            (repeated["task_id"], repeated["status"]), (task["task_id"], "completed")
        )

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
                "credential_generation": plan["credential_generation"],
            },
            dedupe_key=f"fs-change:{plan['plan_id']}",
        )

    def test_waiting_copy_releases_worker_and_resumes_same_plan_without_recopy(self):
        from app.repositories.organize_operation_jobs import CLOUD_COPY_PENDING_CODE
        from tests.test_agent_guangya_fs_gateway import FakeGatewayClient

        for source_id in ("trash", "move"):
            with self.subTest(object_type="directory" if source_id == "trash" else "file"):
                client = FakeGatewayClient()
                plan = guangya_fs_change.build_fs_change_plan(cast(GuangYaClient, client), owner="queue-owner",
                    operations=[{"op": "copy", "file_id": source_id, "target_path": "/target"},
                                {"op": "move", "file_id": "source", "target_path": "/target"}], trigger_strm=False)
                guangya_fs_change.confirm_fs_change_plan(plan["plan_id"], owner="queue-owner", expected_fingerprint=plan["fingerprint"])
                queued, _ = self._enqueue(plan)
                job_id = queued["job_id"]
                public_ref = organize_operation_public_ref(job_id)
                payload = json.loads(queued["payload_json"])

                def run(row):
                    manager = OrganizeTaskManager()
                    manager._lock = cast(CrossProcessLock, threading.Lock())
                    manager._lock.acquire()
                    manager._worker = cast(Thread, object())
                    manager._task = {"id": job_id, "status": "running", "durable": True, "owner_digest": row["owner_digest"]}
                    with mock.patch.object(manager, "_wake_download_tracker"), \
                         mock.patch.object(manager, "_execute_durable_operation", side_effect=lambda _row:
                             guangya_fs_change.execute_fs_change_plan(payload, client_factory=cast(Callable[[], GuangYaClient], lambda: client), lease_generation=row["lease_generation"])):
                        manager._run_durable_operation(dict(row))
                    self.assertIsNone(manager._worker)
                    self.assertTrue(manager._lock.acquire(blocking=False), "waiting must release the organizer write lock")
                    manager._lock.release()
                    return manager

                with mock.patch.object(client, "copy", wraps=client.copy) as copied, \
                     mock.patch.object(client, "move", wraps=client.move) as moved, \
                     mock.patch.object(client, "task_status", return_value={"data": {"status": 1}}) as task_status, \
                     mock.patch.object(guangya_fs_change, "_COPY_FOREGROUND_ATTEMPTS", 1):
                    first_manager = run(claim_organize_operation_job(job_id))
                    waiting = get_organize_operation_job(job_id)
                    assert waiting is not None
                    self.assertEqual(waiting["status"], "pending")
                    self.assertEqual(waiting["error_code"], CLOUD_COPY_PENDING_CODE)
                    self.assertIsNone(waiting["finished_at"])
                    self.assertEqual(waiting["expires_at"], queued["expires_at"])
                    self.assertEqual(waiting["payload_json"], queued["payload_json"])
                    self.assertEqual(json.loads(waiting["result_json"])["stats"]["copied"], 0)
                    file_info_result = client.file_info("source")
                    assert file_info_result is not None
                    self.assertEqual(file_info_result.parent_id, "0")
                    moved.assert_not_called()
                    checkpoint = guangya_fs_change._read_fs_checkpoints(guangya_fs_change._read(plan["plan_id"]))
                    task_id = checkpoint["copy_tasks"]["1"]
                    self.assertTrue(task_id)
                    task_status.assert_called_with(task_id)
                    self.assertIsNone(claim_organize_operation_job())
                    task_result_result = first_manager.task_result(public_ref, owner="queue-owner")
                    assert task_result_result is not None
                    self.assertEqual(task_result_result["status"], "queued")
                    # 复制在云端运行时，另一个普通任务可以领取，不被等待队首占锁。
                    other, _ = enqueue_organize_operation_job(job_kind="directory_scrape", owner="queue-owner",
                        operation="independent", reference="", payload={}, dedupe_key=f"independent:{source_id}")
                    claimed_other = claim_organize_operation_job()
                    assert claimed_other is not None
                    self.assertEqual(claimed_other["job_id"], other["job_id"])
                    finish_organize_operation_job(other["job_id"], expected_lease_generation=claimed_other["lease_generation"], status="completed")
                    for generation in (2, 3):
                        with database.get_conn() as conn:
                            conn.execute("UPDATE organize_operation_jobs SET updated_at='2000-01-01 00:00:00' WHERE job_id=?", (job_id,))
                        if generation == 3: task_status.return_value = {"data": {"status": 2}}
                        claimed = claim_organize_operation_job(job_id)
                        assert claimed is not None
                        self.assertEqual(claimed["lease_generation"], generation)
                        run(claimed)
                        if generation == 2:
                            still_waiting = get_organize_operation_job(job_id)
                            assert still_waiting is not None
                            self.assertEqual(still_waiting["status"], "pending")
                            self.assertEqual(still_waiting["error_code"], CLOUD_COPY_PENDING_CODE)
                            self.assertIsNone(still_waiting["finished_at"])
                            self.assertEqual(json.loads(still_waiting["result_json"])["stats"]["copied"], 0)
                            moved.assert_not_called()
                    copied.assert_called_once_with([source_id], "target")
                    moved.assert_called_once_with(["source"], "target")
                    self.assertGreaterEqual(task_status.call_count, 3)
                    self.assertTrue(all(call.args == (task_id,) for call in task_status.call_args_list))
                finished = get_organize_operation_job(job_id)
                assert finished is not None
                self.assertEqual(finished["status"], "completed")
                file_info_result_2 = client.file_info("source")
                assert file_info_result_2 is not None
                self.assertEqual(file_info_result_2.parent_id, "target")
                self.assertEqual(json.loads(finished["result_json"])["stats"]["copied"], 1)
                # 原manager的queued缓存不能遮住另一worker已落盘的终态。
                task_result_result_2 = first_manager.task_result(public_ref, owner="queue-owner")
                assert task_result_result_2 is not None
                self.assertEqual(task_result_result_2["status"], "completed")
                self.assertNotIn("copy-task-", finished["result_json"])

    def test_owner_purge_cannot_be_undone_by_running_or_finished_worker_cache(self):
        queued, _ = self._enqueue(self._confirmed_plan())
        job_id = queued["job_id"]
        claimed = claim_organize_operation_job(job_id)
        public_ref = organize_operation_public_ref(job_id)
        manager = OrganizeTaskManager()
        manager._lock = cast(CrossProcessLock, threading.Lock())
        manager._lock.acquire()
        private_result = {"stats": {"total": 1}, "operation_items": [{
            "position": 1, "operation": "copy", "status": "unknown",
            "label": "复制：已清理的文件名", "completed_actions": [],
        }]}
        assert claimed is not None
        manager._task = {"id": job_id, "status": "running", "durable": True,
                         "owner_digest": claimed["owner_digest"], "result": private_result}
        database.purge_agent_subject_data(owner="queue-owner")
        self.assertIsNone(manager.task_result(public_ref, owner="queue-owner"))
        cancelled = OrganizeOperationCancelled("cancelled")
        cancelled.operation_result = private_result
        with mock.patch.object(manager, "_execute_durable_operation", side_effect=cancelled), mock.patch.object(manager, "_wake_download_tracker"):
            manager._run_durable_operation(dict(claimed))
        self.assertIsNone(get_organize_operation_job(job_id))
        self.assertIsNone(manager.task_result(public_ref, owner="queue-owner"))
        manager._task = {}
        self.assertIsNone(manager.task_result(public_ref, owner="queue-owner"), "history cache must not resurrect the erased receipt")

    def test_wait_checkpoint_failure_is_manual_review_not_retryable_failure(self):
        queued, _ = self._enqueue(self._confirmed_plan())
        claimed = claim_organize_operation_job(queued["job_id"])
        manager = OrganizeTaskManager()
        manager._lock = cast(CrossProcessLock, threading.Lock())
        manager._lock.acquire()
        manager._task = {"id": queued["job_id"], "status": "running", "durable": True}
        error = guangya_fs_change.GuangYaFSCheckpointError("checkpoint unavailable")
        error.operation_result = {"stats": {"audit_failures": 1}, "requires_manual": True}
        with mock.patch.object(manager, "_execute_durable_operation", side_effect=error), mock.patch.object(manager, "_wake_download_tracker"):
            assert claimed is not None
            manager._run_durable_operation(dict(claimed))
        result = get_organize_operation_job(queued["job_id"])
        assert result is not None
        self.assertEqual(result["status"], "manual_review")
        self.assertIn("勿重复提交", result["error"])

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

    def test_operation_items_survive_worker_history_and_restart(self):
        self._assert_durable_stats_reach_waiter(
            {"total": 3, "created": 0, "renamed": 1, "failed": 2}, terminal="partial",
            operation_items=[
                {"position": 1, "operation": "rename", "label": "改名：A → A-done", "status": "completed", "completed_actions": ["rename"]},
                {"position": 2, "operation": "relocate", "label": "移动并改名：dirty → target / clean", "status": "partial", "completed_actions": ["move"], "reason": "write_rejected"},
                {"position": 3, "operation": "move", "label": "移动：B → archive", "status": "blocked", "completed_actions": [], "reason": "dependency_failed"},
            ],
        )

    def test_cancelled_worker_preserves_verified_and_unstarted_items(self):
        self._assert_durable_stats_reach_waiter(
            {"total": 2, "created": 0, "renamed": 1, "failed": 0},
            terminal="cancelled", error=OrganizeOperationCancelled("cancelled"),
            operation_items=[
                {"position": 1, "operation": "rename", "label": "改名：A → A-done", "status": "completed", "completed_actions": ["rename"]},
                {"position": 2, "operation": "rename", "label": "改名：B → B-done", "status": "not_started", "completed_actions": [], "reason": "cancelled"},
            ],
        )

    def _assert_durable_stats_reach_waiter(self, stats, *, terminal, operation_items=None, error=None):
        queued, _ = self._enqueue(self._confirmed_plan())
        job_id = str(queued["job_id"])
        claimed = claim_organize_operation_job(job_id)
        public_ref = organize_operation_public_ref(job_id)
        context = ToolContext(owner="queue-owner")
        manager = OrganizeTaskManager()
        manager._lock = cast(CrossProcessLock, threading.Lock())
        manager._lock.acquire()
        assert claimed is not None
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
                public = ToolResult(**get_organize_operation_status(public_ref, owner=context.owner))
                self.assertEqual(public.data["task"]["status"], "running")
                self.assertEqual(public.data["task"]["stats"], {})

        execution_result = {
            "partial": terminal == "partial", "requires_manual": False,
            "stats": {**stats, "internal_file_id": "private-item"},
            "copy_tasks": {"1": "private-copy-task-id"},
            "stage_sources": {
                "1": {
                    "file_id": "private-stage-file-id",
                    "parent_id": "private-stage-parent-id",
                    "etag": "private-stage-etag",
                    "updated_at": "private-stage-updated-at",
                },
            },
            **({"operation_items": operation_items} if operation_items is not None else {}),
        }
        if error is not None:
            error.operation_result = execution_result
        with (
            mock.patch.object(manager, "_execute_durable_operation", return_value=execution_result, side_effect=error),
            mock.patch.object(manager, "_wake_download_tracker"),
        ):
            manager._run_durable_operation(dict(claimed))
        persisted = get_organize_operation_job(job_id)
        assert persisted is not None
        self.assertEqual(persisted["status"], terminal)
        expected_result = sanitize_organize_operation_result({"stats": stats,
            **({"operation_items": operation_items} if operation_items is not None else {})})
        self.assertEqual(json.loads(persisted["result_json"]), expected_result)
        self.assertNotIn("stage_sources", persisted["result_json"])
        self.assertNotIn("copy_tasks", persisted["result_json"])
        private_stage_values = (
            "private-stage-file-id", "private-stage-parent-id",
            "private-stage-etag", "private-stage-updated-at", "private-copy-task-id",
        )
        for private in private_stage_values:
            assert persisted is not None
            self.assertNotIn(private, persisted["result_json"])

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
                public = ToolResult(**get_organize_operation_status(public_ref, owner=context.owner))
                receipt = asyncio.run(wait_for_effect_completion(
                    accepted, tool="cloud.change", context=context,
                ))
                self.assertEqual(receipt.data["stats"], stats)
                self.assertEqual(receipt.data["background_job"]["stats"], stats)
                self.assertEqual(receipt.status, terminal)
                self.assertEqual(receipt.ok, terminal == "completed")
                self.assertEqual(receipt.data["relocate_count"], stats["total"] - stats["created"])
                self.assertEqual(receipt.data["operation_ref"], public_ref)
                self.assertEqual(public.data["task"]["stats"], stats)
                assert raw is not None
                self.assertEqual(raw["result"], expected_result)
                self.assertNotIn("copy_tasks", json.dumps(public.data))
                self.assertNotIn("copy_tasks", json.dumps(receipt.data))
                self.assertNotIn("stage_sources", json.dumps(public.data))
                self.assertNotIn("stage_sources", json.dumps(receipt.data))
                if operation_items is not None:
                    self.assertEqual(receipt.data["operation_items"], expected_result["operation_items"])
                    self.assertEqual(public.data["task"]["operation_items"], expected_result["operation_items"])
                self.assertIsNone(current.task_result(public_ref, owner="other-owner"))
                for private in (
                    "owner_digest", "internal_file_id", "private-item", job_id,
                    *private_stage_values,
                ):
                    for public_payload in (public.data, receipt.data):
                        self.assertNotIn(private, json.dumps(public_payload))

    def test_preflight_stale_reason_survives_live_history_and_restart(self):
        queued, _ = self._enqueue(self._confirmed_plan())
        job_id = str(queued["job_id"])
        claimed = claim_organize_operation_job(job_id)
        public_ref = organize_operation_public_ref(job_id)
        context = ToolContext(owner="queue-owner")
        manager = OrganizeTaskManager()
        manager._lock = cast(CrossProcessLock, threading.Lock())
        manager._lock.acquire()
        assert claimed is not None
        manager._task = {
            "id": job_id, "status": "running", "durable": True,
            "owner_digest": claimed["owner_digest"],
        }
        with (
            mock.patch.object(manager, "_execute_durable_operation", side_effect=
                              guangya_fs_change.GuangYaFSChangeStale("private token=secret /private/path")),
            mock.patch.object(manager, "_wake_download_tracker"),
        ):
            manager._run_durable_operation(dict(claimed))
        get_organize_operation_job_result = get_organize_operation_job(job_id)
        assert get_organize_operation_job_result is not None
        self.assertEqual(get_organize_operation_job_result["error_code"], "GuangYaFSChangeStale")
        accepted = ToolResult(True, "accepted", "已提交", data={"operation_ref": public_ref})
        for source in ("live", "history", "restart"):
            if source == "history":
                manager._task = {}
            current = OrganizeTaskManager() if source == "restart" else manager
            with (
                self.subTest(source=source),
                mock.patch("app.modules.organize_tasks.get_organize_manager", return_value=current),
                mock.patch.object(current, "status", return_value={}),
            ):
                receipt = asyncio.run(wait_for_effect_completion(
                    accepted, tool="cloud.change", context=context,
                ))
                self.assertFalse(receipt.ok)
                self.assertEqual(receipt.status, "failed")
                assert receipt.error is not None
                self.assertIn("对象状态已变化", receipt.error)
                self.assertIn("本次变更未执行", receipt.error)
                for private in ("secret", "/private", "GuangYaFSChangeStale"):
                    self.assertNotIn(private, str(receipt.to_dict()))

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
        assert (claimed) is not None
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


def test_change_labels_show_the_parent_of_same_named_targets():
    """真实验收：分季方案把 Season 01 建在了根目录，而卡片只写“→ Season 01”，用户与模型都无法分辨。"""
    from app.modules.guangya_fs_change import _operation_label

    relocate = {"op": "relocate", "source": {"name": "[GroupA] 测试剧 - 01.mkv"},
                "target_path": "/测试根/Season 01", "new_name": "测试剧 (2024) - S01E01.mkv"}
    nested = {**relocate, "target_path": "/测试根/测试剧 (2024)/Season 01"}
    created = {"op": "create_directory", "name": "Season 01", "parent_path": "/测试根"}
    assert _operation_label(relocate) == "移动并改名：[GroupA] 测试剧 - 01.mkv → 测试根 › Season 01 / 测试剧 (2024) - S01E01.mkv"
    assert "测试剧 (2024) › Season 01" in _operation_label(nested)
    assert _operation_label(created) == "新建目录：测试根 › Season 01"
    assert _operation_label({"op": "move", "source": {"name": "a"}, "target_path": "/"}) == "移动：a → 账号根目录"
