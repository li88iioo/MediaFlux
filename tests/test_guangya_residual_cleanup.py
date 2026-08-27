"""光鸭整理残留安全清理测试。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app.agent import guangya_cleanup_actions as actions
from app.agent.models import RiskLevel, ToolContext
from app.agent.tools import build_tool_registry
from app.clients.guangya import GuangYaFile
from app.modules import guangya_residual_cleanup as cleanup


class FakeCleanupClient:
    def __init__(self):
        self.logged_in = True
        self.credential_generation = 13
        self.closed = False
        self.next_id = 100
        self.directories = {
            "0": [GuangYaFile("source", "整理源", True, parent_id="0", etag="src")],
            "source": [
                GuangYaFile("empty", "空目录", True, parent_id="source", etag="e1", updated_at=1),
                GuangYaFile("residual", "a", True, parent_id="source", etag="e2", updated_at=2),
                GuangYaFile("keep", "保留元数据", True, parent_id="source", etag="e3", updated_at=3),
                GuangYaFile("video_dir", "媒体目录", True, parent_id="source", etag="e4", updated_at=4),
            ],
            "empty": [],
            "residual": [
                GuangYaFile("junk", "xxx.png", False, parent_id="residual", size=10, etag="j", extension="png")
            ],
            "keep": [
                GuangYaFile("poster", "poster.jpg", False, parent_id="keep", size=10, etag="p", extension="jpg")
            ],
            "video_dir": [
                GuangYaFile("video", "ABC-123.mp4", False, parent_id="video_dir", size=100, etag="v", extension="mp4")
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

    def create_dir(self, name, parent_id="0"):
        self.next_id += 1
        file_id = f"new-{self.next_id}"
        self.directories.setdefault(str(parent_id), []).append(
            GuangYaFile(file_id, str(name), True, parent_id=str(parent_id), etag=f"etag-{file_id}", updated_at=self.next_id)
        )
        self.directories[file_id] = []
        return file_id

    def move(self, file_ids, parent_id):
        for file_id in file_ids:
            found = None
            for items in self.directories.values():
                for index, item in enumerate(items):
                    if item.file_id == str(file_id):
                        found = items.pop(index)
                        break
                if found is not None:
                    break
            if found is None:
                raise RuntimeError("missing")
            found.parent_id = str(parent_id)
            self.directories.setdefault(str(parent_id), []).append(found)
        return True

    def delete_empty_directory(self, file_id, *, expected_etag="", expected_updated_at=0):
        if self.directories.get(str(file_id)):
            raise RuntimeError("not empty")
        for items in self.directories.values():
            for index, item in enumerate(items):
                if item.file_id == str(file_id):
                    if expected_etag and item.etag != expected_etag:
                        raise RuntimeError("stale")
                    items.pop(index)
                    self.directories.pop(str(file_id), None)
                    return True
        raise RuntimeError("missing")

    def close(self):
        self.closed = True


class GuangYaResidualCleanupTests(unittest.TestCase):
    def setUp(self):
        actions.reset_guangya_cleanup_context_for_tests()
        self.temp = tempfile.TemporaryDirectory()
        self.plan_dir = Path(self.temp.name) / "plans"
        self.patches = [
            mock.patch.object(cleanup, "_plan_directory", return_value=self.plan_dir),
            mock.patch.object(cleanup, "_owner_digest", return_value="owner-digest"),
            mock.patch.object(cleanup, "get_web_secret", return_value="test-secret"),
            mock.patch.object(actions, "organize_operation_owner_digest", return_value="owner-digest"),
            mock.patch.object(actions, "_configured_sources", return_value=[{"id": "source", "name": "整理源"}]),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        actions.reset_guangya_cleanup_context_for_tests()
        self.temp.cleanup()

    def test_plan_keeps_media_metadata_but_selects_image_only_residual(self):
        client = FakeCleanupClient()
        plan = cleanup.build_cleanup_plan(
            client,
            owner="owner",
            sources=[{"id": "source", "name": "整理源"}],
            max_candidates=20,
        )
        self.assertEqual(plan["stats"]["empty_dir_count"], 1)
        self.assertEqual(plan["stats"]["residual_dir_count"], 1)
        self.assertEqual(plan["stats"]["quarantine_file_count"], 1)
        self.assertEqual(plan["residuals"][0]["root"]["name"], "a")
        self.assertEqual(plan["empties"][0]["root"]["name"], "空目录")

    def test_confirmed_execution_quarantines_residual_and_recycles_empty(self):
        client = FakeCleanupClient()
        plan = cleanup.build_cleanup_plan(
            client,
            owner="owner",
            sources=[{"id": "source", "name": "整理源"}],
            max_candidates=20,
        )
        cleanup.confirm_cleanup_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        result = cleanup.execute_cleanup_plan(
            {
                "version": 1,
                "plan_id": plan["plan_id"],
                "plan_fingerprint": plan["fingerprint"],
                "owner_digest": "owner-digest",
                "credential_generation": 13,
            },
            client_factory=lambda: client,
        )
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["quarantined"], 1)
        self.assertEqual(result["stats"]["empty_deleted"], 1)
        self.assertIsNone(client.file_info("empty"))
        self.assertIsNotNone(client.file_info("residual"))
        self.assertNotEqual(client.file_info("residual").parent_id, "source")


    def test_residual_is_rechecked_immediately_before_move(self):
        class MutatingClient(FakeCleanupClient):
            def __init__(self):
                super().__init__()
                self.mutated = False

            def create_dir(self, name, parent_id="0"):
                created = super().create_dir(name, parent_id)
                if not self.mutated:
                    self.mutated = True
                    self.directories["residual"].append(
                        GuangYaFile(
                            "late-video", "late.mp4", False,
                            parent_id="residual", size=20, etag="late",
                            extension="mp4",
                        )
                    )
                return created

        client = MutatingClient()
        plan = cleanup.build_cleanup_plan(
            client, owner="owner",
            sources=[{"id": "source", "name": "整理源"}], max_candidates=20,
        )
        cleanup.confirm_cleanup_plan(
            plan["plan_id"], owner="owner",
            expected_fingerprint=plan["fingerprint"],
        )
        result = cleanup.execute_cleanup_plan(
            {
                "version": 1, "plan_id": plan["plan_id"],
                "plan_fingerprint": plan["fingerprint"],
                "owner_digest": "owner-digest", "credential_generation": 13,
            },
            client_factory=lambda: client,
        )
        self.assertTrue(result["partial"])
        self.assertEqual(result["stats"]["quarantined"], 0)
        self.assertEqual(result["stats"]["precondition_failed"], 1)
        self.assertEqual(client.file_info("residual").parent_id, "source")

    def test_empty_directory_is_rechecked_immediately_before_delete(self):
        class MutatingClient(FakeCleanupClient):
            def __init__(self):
                super().__init__()
                self.mutated = False

            def create_dir(self, name, parent_id="0"):
                created = super().create_dir(name, parent_id)
                if not self.mutated:
                    self.mutated = True
                    self.directories["empty"].append(
                        GuangYaFile(
                            "late-junk", "late.txt", False, parent_id="empty",
                            size=1, etag="late-junk", extension="txt",
                        )
                    )
                return created

        client = MutatingClient()
        plan = cleanup.build_cleanup_plan(
            client, owner="owner",
            sources=[{"id": "source", "name": "整理源"}], max_candidates=20,
        )
        cleanup.confirm_cleanup_plan(
            plan["plan_id"], owner="owner",
            expected_fingerprint=plan["fingerprint"],
        )
        result = cleanup.execute_cleanup_plan(
            {
                "version": 1, "plan_id": plan["plan_id"],
                "plan_fingerprint": plan["fingerprint"],
                "owner_digest": "owner-digest", "credential_generation": 13,
            },
            client_factory=lambda: client,
        )
        self.assertTrue(result["partial"])
        self.assertEqual(result["stats"]["empty_deleted"], 0)
        self.assertEqual(result["stats"]["precondition_failed"], 1)
        self.assertIsNotNone(client.file_info("empty"))

    def test_agent_preview_and_durable_submission(self):
        client = FakeCleanupClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            preview = actions.preview_guangya_cleanup({"max_candidates": 20}, context)
            confirmation, fingerprint = actions.prepare_guangya_cleanup_confirmation({}, context)
            manager = mock.Mock()
            manager.start_durable_operation.return_value = {
                "ok": True, "task_id": "a" * 32, "queued": True, "queue_position": 1,
            }
            with mock.patch(
                "app.modules.organize_tasks.get_organize_manager", return_value=manager
            ):
                accepted = actions.execute_guangya_cleanup_confirmed(
                    {}, fingerprint, context
                )
        self.assertEqual(preview.status, "ready")
        self.assertEqual(confirmation.status, "confirmation_required")
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual(
            manager.start_durable_operation.call_args.kwargs["job_kind"],
            "agent_guangya_cleanup",
        )



    def test_failed_repreview_preserves_previous_cleanup_plan(self):
        client = FakeCleanupClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(actions, "GuangYaClient", return_value=client):
            actions.preview_guangya_cleanup({"max_candidates": 20}, context)
        previous = actions._flow("owner")
        self.assertIsNotNone(previous)
        previous_path = cleanup._plan_path(previous.plan_id)
        self.assertTrue(previous_path.is_file())

        with (
            mock.patch.object(actions, "GuangYaClient", return_value=client),
            mock.patch.object(
                actions, "build_cleanup_plan",
                side_effect=cleanup.GuangYaCleanupPlanError("临时读取失败"),
            ),
            self.assertRaisesRegex(Exception, "临时读取失败"),
        ):
            actions.preview_guangya_cleanup({"max_candidates": 20}, context)
        self.assertEqual(actions._flow("owner").plan_id, previous.plan_id)
        self.assertTrue(previous_path.is_file())

    def test_terminal_cleanup_plan_is_removed_after_retention(self):
        client = FakeCleanupClient()
        plan = cleanup.build_cleanup_plan(
            client, owner="owner",
            sources=[{"id": "source", "name": "整理源"}], max_candidates=20,
        )
        cleanup._update_execution(
            plan["plan_id"], "completed", {"quarantined": 1, "empty_deleted": 1}
        )
        with mock.patch.object(
            cleanup.time, "time",
            return_value=plan["created_at_epoch"] + 8 * 24 * 60 * 60,
        ):
            result = cleanup.maintain_cleanup_plans()
        self.assertEqual(result["removed"], 1)
        self.assertFalse((self.plan_dir / f"{plan['plan_id']}.json").exists())

    def test_registry_exposes_preview_and_confirmed_cleanup(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(
            capabilities["guangya.organize.cleanup.preview"]["risk"],
            RiskLevel.READ.value,
        )
        self.assertEqual(
            capabilities["guangya.organize.cleanup.execute"]["risk"],
            RiskLevel.DANGER.value,
        )
        self.assertTrue(
            capabilities["guangya.organize.cleanup.execute"]["requires_confirmation"]
        )


if __name__ == "__main__":
    unittest.main()
