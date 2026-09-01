"""Media Agent 光鸭受控重命名能力测试。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from app import database
from app.agent import guangya_rename_actions as actions
from app.agent.models import RiskLevel, ToolContext
from app.agent.result_projection import project_agent_result_for_user
from app.agent.tools import build_tool_registry
from app.clients.guangya import GuangYaFile, GuangYaWriteRejected
from app.modules import guangya_rename


class FakeGuangYaClient:
    def __init__(self, *, reject_ids: set[str] | None = None):
        self.logged_in = True
        self.credential_generation = 7
        self.reject_ids = set(reject_ids or ())
        self.closed = False
        self.directories = {
            "0": [GuangYaFile("d1", "整理", True, parent_id="0")],
            "d1": [GuangYaFile("d2", "动漫", True, parent_id="d1")],
            "d2": [
                GuangYaFile(
                    "f1", "师兄太稳健.S01E08.2160p.15.1Mbps.60fps.mp4", False,
                    size=100, etag="etag-1", parent_id="d2", extension="mp4",
                ),
                GuangYaFile(
                    "f2", "钢之炼金术师 FA - S01E04. 1080p 2.4 Mbps. h265.mkv", False,
                    size=200, etag="etag-2", parent_id="d2", extension="mkv",
                ),
                GuangYaFile(
                    "f3", "已经正常.S01E01.1080p.mp4", False,
                    size=300, etag="etag-3", parent_id="d2", extension="mp4",
                ),
            ],
        }

    def list_dir(self, parent_id="0"):
        return [deepcopy(item) for item in self.directories.get(str(parent_id), [])]

    def file_info(self, file_id):
        for items in self.directories.values():
            for item in items:
                if item.file_id == str(file_id):
                    return deepcopy(item)
        return None

    def rename(self, file_id, new_name):
        if str(file_id) in self.reject_ids:
            raise GuangYaWriteRejected("rename", code="166", message="provider rejected")
        for items in self.directories.values():
            for item in items:
                if item.file_id == str(file_id):
                    item.name = str(new_name)
                    return True
        raise RuntimeError("missing")

    def close(self):
        self.closed = True


class GuangYaRenameTests(unittest.TestCase):
    def setUp(self):
        actions.reset_guangya_rename_context_for_tests()
        self.temp = tempfile.TemporaryDirectory()
        self.plan_dir = Path(self.temp.name) / "plans"
        self.patches = [
            mock.patch.object(guangya_rename, "_plan_directory", return_value=self.plan_dir),
            mock.patch.object(guangya_rename, "_owner_digest", return_value="owner-digest"),
            mock.patch.object(guangya_rename, "get_web_secret", return_value="test-secret"),
            mock.patch.object(actions, "organize_operation_owner_digest", return_value="owner-digest"),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        actions.reset_guangya_rename_context_for_tests()
        self.temp.cleanup()

    def test_exact_mode_is_rejected_in_favor_of_generic_fs_change(self):
        with self.assertRaisesRegex(Exception, "仅支持 remove_bitrate"):
            actions.guangya_rename_preview_arguments({
                "paths": ["/整理/动漫/旧名.mp4"],
                "mode": "exact",
            })
        with self.assertRaisesRegex(Exception, "不支持的工具参数：new_name"):
            actions.guangya_rename_preview_arguments({
                "paths": ["/整理/动漫/旧名.mp4"],
                "mode": "remove_bitrate",
                "new_name": "新名.mp4",
            })
        with self.assertRaisesRegex(Exception, "重命名方式无效"):
            guangya_rename.build_rename_plan(
                FakeGuangYaClient(),
                owner="owner",
                targets=["/整理/动漫/师兄太稳健.S01E08.2160p.15.1Mbps.60fps.mp4"],
                mode="exact",
            )

    def test_remove_bitrate_handles_compact_and_spaced_without_damaging_codec(self):
        cases = {
            "Title.H.265.15.1Mbps.60fps.mp4": "Title.H.265.60fps.mp4",
            "Title.1080p 2.4 Mbps. h265.mkv": "Title.1080p. h265.mkv",
            "Title - S01E04 - 0.0 Mbps.mkv": "Title - S01E04.mkv",
            "Title.H.264.20Mbps.30fps.mp4": "Title.H.264.30fps.mp4",
            "Movie 1.5 GB.mp4": "Movie 1.5 GB.mp4",
        }
        for before, after in cases.items():
            with self.subTest(before=before):
                self.assertEqual(guangya_rename.remove_legacy_bitrate(before), after)

    def test_plan_freezes_safe_entries_and_executes_with_verification(self):
        client = FakeGuangYaClient()
        plan = guangya_rename.build_rename_plan(
            client,
            owner="owner",
            targets=["/整理/动漫"],
            mode="remove_bitrate",
            recursive=True,
            limit=100,
        )
        self.assertEqual(plan["stats"]["rename_count"], 2)
        self.assertEqual(plan["stats"]["conflict_count"], 0)
        self.assertEqual(len(plan["samples"]), 2)
        guangya_rename.confirm_rename_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"],
        )
        result = guangya_rename.execute_rename_plan(
            {
                "version": 1,
                "plan_id": plan["plan_id"],
                "plan_fingerprint": plan["fingerprint"],
                "owner_digest": "owner-digest",
                "credential_generation": 7,
            },
            client_factory=lambda: client,
        )
        self.assertEqual(result["stats"]["renamed"], 2)
        self.assertEqual(result["stats"]["rename_failed"], 0)
        self.assertEqual(
            client.file_info("f1").name,
            "师兄太稳健.S01E08.2160p.60fps.mp4",
        )
        stored = guangya_rename.load_rename_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"],
            require_confirmed=True,
        )
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["execution"]["renamed"], 2)
        self.assertTrue((self.plan_dir / f"{plan['plan_id']}.jsonl").is_file())

    def test_legacy_persisted_mode_is_rejected_and_removed(self):
        client = FakeGuangYaClient()
        plan = guangya_rename.build_rename_plan(
            client,
            owner="owner",
            targets=[
                "/整理/动漫/师兄太稳健.S01E08.2160p.15.1Mbps.60fps.mp4"
            ],
            mode="remove_bitrate",
        )
        stored = guangya_rename.load_rename_plan(plan["plan_id"], owner="owner")
        stored["mode"] = "declarative"
        stored["transform"] = {"trigger_strm": "1"}
        guangya_rename._atomic_write_plan(
            self.plan_dir / f"{plan['plan_id']}.json", stored
        )

        with self.assertRaisesRegex(
            guangya_rename.GuangYaRenamePlanStale, "已停用的旧链路"
        ):
            guangya_rename.load_rename_plan(plan["plan_id"], owner="owner")

        result = guangya_rename.maintain_rename_plans()
        self.assertEqual(result["removed"], 1)
        self.assertFalse((self.plan_dir / f"{plan['plan_id']}.json").exists())

    def test_provider_rejection_is_partial_not_false_success(self):
        client = FakeGuangYaClient(reject_ids={"f2"})
        plan = guangya_rename.build_rename_plan(
            client, owner="owner", targets=["/整理/动漫"],
            mode="remove_bitrate", recursive=True, limit=100,
        )
        guangya_rename.confirm_rename_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"],
        )
        result = guangya_rename.execute_rename_plan(
            {
                "version": 1, "plan_id": plan["plan_id"],
                "plan_fingerprint": plan["fingerprint"],
                "owner_digest": "owner-digest", "credential_generation": 7,
            },
            client_factory=lambda: client,
        )
        self.assertTrue(result["partial"])
        self.assertEqual(result["stats"]["renamed"], 1)
        self.assertEqual(result["stats"]["rename_failed"], 1)
        self.assertIn("2.4 Mbps", client.file_info("f2").name)

    def test_execution_rechecks_each_snapshot_before_late_write(self):
        class MutatingClient(FakeGuangYaClient):
            def rename(self, file_id, new_name):
                result = super().rename(file_id, new_name)
                if str(file_id) == "f1":
                    for item in self.directories["d2"]:
                        if item.file_id == "f2":
                            item.etag = "changed-after-preflight"
                return result

        client = MutatingClient()
        plan = guangya_rename.build_rename_plan(
            client, owner="owner", targets=["/整理/动漫"],
            mode="remove_bitrate", recursive=True, limit=100,
        )
        guangya_rename.confirm_rename_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"],
        )
        with mock.patch.object(guangya_rename.time, "sleep", return_value=None):
            result = guangya_rename.execute_rename_plan(
                {
                    "version": 1, "plan_id": plan["plan_id"],
                    "plan_fingerprint": plan["fingerprint"],
                    "owner_digest": "owner-digest", "credential_generation": 7,
                },
                client_factory=lambda: client,
            )
        self.assertTrue(result["partial"])
        self.assertEqual(result["stats"]["renamed"], 1)
        self.assertEqual(result["stats"]["rename_failed"], 1)
        self.assertIn("2.4 Mbps", client.file_info("f2").name)
        stored = guangya_rename.load_rename_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"],
            require_confirmed=True,
        )
        self.assertEqual(stored["execution"]["precondition_failed"], 1)

    def test_preview_exposes_filename_examples_without_cloud_paths(self):
        client = FakeGuangYaClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            preview = actions.preview_guangya_rename(
                {
                    "paths": ["/整理/动漫"], "mode": "remove_bitrate",
                    "recursive": True, "limit": 100,
                },
                context,
            )
        self.assertEqual(len(preview.data["sample_changes"]), 2)
        self.assertNotIn("/整理/动漫", preview.data["sample_changes"][0])
        display = project_agent_result_for_user({
            "ok": preview.ok, "status": preview.status, "summary": preview.summary,
            "data": preview.data, "suggestions": preview.suggestions,
        })
        self.assertIn("名称变更示例", display["details"])
        self.assertTrue(display["details"]["名称变更示例"])

    def test_private_plan_size_limit_fails_before_writing(self):
        client = FakeGuangYaClient()
        with mock.patch.object(guangya_rename, "_MAX_PLAN_FILE_BYTES", 128):
            with self.assertRaisesRegex(
                guangya_rename.GuangYaRenamePlanError, "计划过大",
            ):
                guangya_rename.build_rename_plan(
                    client, owner="owner", targets=["/整理/动漫"],
                    mode="remove_bitrate", recursive=True, limit=100,
                )
        self.assertEqual(list(self.plan_dir.glob("*.json")), [])

    def test_agent_preview_confirmation_and_durable_submission(self):
        client = FakeGuangYaClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            preview = actions.preview_guangya_rename(
                {
                    "paths": ["/整理/动漫"], "mode": "remove_bitrate",
                    "recursive": True, "limit": 100,
                },
                context,
            )
            self.assertEqual(preview.status, "ready")
            self.assertEqual(preview.data["rename_count"], 2)
            confirmation, fingerprint = actions.prepare_guangya_rename_confirmation({}, context)
            self.assertEqual(confirmation.status, "confirmation_required")

            manager = mock.Mock()
            manager.start_durable_operation.return_value = {
                "ok": True, "task_id": "a" * 32, "queued": True,
                "queue_position": 1,
            }
            with mock.patch(
                "app.modules.organize_tasks.get_organize_manager", return_value=manager,
            ):
                accepted = actions.execute_guangya_rename_confirmed(
                    {}, fingerprint, context,
                )
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual(accepted.data["rename_count"], 2)
        kwargs = manager.start_durable_operation.call_args.kwargs
        self.assertEqual(kwargs["job_kind"], "agent_guangya_rename")
        self.assertNotIn("paths", kwargs["payload"])
        self.assertNotIn("entries", kwargs["payload"])


    def test_failed_repreview_preserves_previous_confirmation_plan(self):
        client = FakeGuangYaClient()
        context = ToolContext(owner="owner", session_id="session")
        arguments = {
            "paths": ["/整理/动漫"], "mode": "remove_bitrate",
            "recursive": True, "limit": 100,
        }
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            actions.preview_guangya_rename(arguments, context)
        previous = actions._flow("owner")
        self.assertIsNotNone(previous)
        previous_path = guangya_rename._plan_path(previous.plan_id)
        self.assertTrue(previous_path.is_file())

        with (
            mock.patch.object(actions, "GuangYaClient", return_value=client),
            mock.patch.object(
                actions, "build_rename_plan",
                side_effect=guangya_rename.GuangYaRenamePlanError("临时读取失败"),
            ),
            self.assertRaisesRegex(Exception, "临时读取失败"),
        ):
            actions.preview_guangya_rename(arguments, context)
        self.assertEqual(actions._flow("owner").plan_id, previous.plan_id)
        self.assertTrue(previous_path.is_file())

    def test_successful_repreview_discards_replaced_private_plan(self):
        client = FakeGuangYaClient()
        context = ToolContext(owner="owner", session_id="session")
        arguments = {
            "paths": ["/整理/动漫"], "mode": "remove_bitrate",
            "recursive": True, "limit": 100,
        }
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            actions.preview_guangya_rename(arguments, context)
            first = actions._flow("owner")
            actions.preview_guangya_rename(arguments, context)
            second = actions._flow("owner")
        self.assertNotEqual(first.plan_id, second.plan_id)
        self.assertFalse(guangya_rename._plan_path(first.plan_id).exists())
        self.assertTrue(guangya_rename._plan_path(second.plan_id).is_file())

    def test_v9_queue_migration_preserves_rows_and_allows_both_guangya_kinds(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE organize_operation_jobs ("
            "job_id TEXT PRIMARY KEY,"
            "job_kind TEXT NOT NULL CHECK(job_kind IN "
            "('agent_directory_scrape','directory_scrape')),"
            "owner_digest TEXT NOT NULL,operation TEXT NOT NULL,"
            "reference TEXT NOT NULL DEFAULT '',payload_json TEXT NOT NULL DEFAULT '{}',"
            "payload_auth TEXT NOT NULL DEFAULT '',dedupe_digest TEXT NOT NULL,"
            "status TEXT NOT NULL DEFAULT 'pending',lease_generation INTEGER NOT NULL DEFAULT 0,"
            "result_json TEXT NOT NULL DEFAULT '{}',error_code TEXT NOT NULL DEFAULT '',"
            "error TEXT NOT NULL DEFAULT '',cancel_requested INTEGER NOT NULL DEFAULT 0,"
            "expires_at REAL NOT NULL DEFAULT 0,purged_at TEXT,created_at TEXT NOT NULL,"
            "updated_at TEXT NOT NULL,started_at TEXT,finished_at TEXT)"
        )
        conn.execute(
            "INSERT INTO organize_operation_jobs("
            "job_id,job_kind,owner_digest,operation,dedupe_digest,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?)",
            ("a" * 32, "directory_scrape", "owner", "刮削", "dedupe", "c", "u"),
        )
        database._migrate_agent_guangya_operation_jobs_v10(conn)
        row = conn.execute(
            "SELECT job_kind,operation FROM organize_operation_jobs WHERE job_id=?",
            ("a" * 32,),
        ).fetchone()
        self.assertEqual((row["job_kind"], row["operation"]), ("directory_scrape", "刮削"))
        for job_id, job_kind, operation, dedupe in (
            ("b" * 32, "agent_guangya_rename", "改名", "dedupe2"),
            ("c" * 32, "agent_guangya_cleanup", "清理", "dedupe3"),
        ):
            conn.execute(
                "INSERT INTO organize_operation_jobs("
                "job_id,job_kind,owner_digest,operation,dedupe_digest,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (job_id, job_kind, "owner", operation, dedupe, "c", "u"),
            )
        self.assertEqual(
            {
                row["job_kind"]
                for row in conn.execute(
                    "SELECT job_kind FROM organize_operation_jobs "
                    "WHERE job_id IN (?,?)", ("b" * 32, "c" * 32)
                )
            },
            {"agent_guangya_rename", "agent_guangya_cleanup"},
        )
        conn.close()

    def test_registry_exposes_read_preview_and_confirmed_write(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(capabilities["guangya.rename.preview"]["risk"], RiskLevel.READ.value)
        self.assertEqual(capabilities["guangya.rename.execute"]["risk"], RiskLevel.DANGER.value)
        self.assertTrue(capabilities["guangya.rename.execute"]["requires_confirmation"])
        llm = {item["name"] for item in registry.llm_orchestration_capabilities(include_confirmations=True)}
        self.assertIn("guangya.rename.preview", llm)
        self.assertIn("guangya.rename.execute", llm)


if __name__ == "__main__":
    unittest.main()
