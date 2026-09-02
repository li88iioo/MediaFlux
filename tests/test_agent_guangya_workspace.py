"""光鸭只读工作区快照的持久化与公开投影测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from app.agent import guangya_workspace_actions as workspace_actions
from app.agent.models import ToolContext
from app.agent.result_projection import project_agent_response_for_llm
from app.clients.guangya import GuangYaFile
from app.modules import guangya_workspace


class FakeWorkspaceClient:
    def __init__(self):
        self.logged_in = True
        self.credential_generation = 21
        self.closed = False
        self.list_calls: list[str] = []
        self.directories = {
            "0": [GuangYaFile("a", "a", True, parent_id="0", etag="root")],
            "a": [
                GuangYaFile(
                    "spam", "[最新地址]ABC-123.mp4", False,
                    parent_id="a", size=100, etag="spam-etag", extension="mp4",
                ),
                GuangYaFile(
                    "clean", "DEF-456.mp4", False,
                    parent_id="a", size=90, etag="clean-etag", extension="mp4",
                ),
                GuangYaFile(
                    "nested", "spam.example.com", True,
                    parent_id="a", etag="nested-etag", updated_at=3,
                ),
            ],
            "nested": [
                GuangYaFile(
                    "subtitle", "广告词-GHI-789.CHT.srt", False,
                    parent_id="nested", size=10, etag="sub-etag", extension="srt",
                ),
            ],
        }

    def list_dir(self, parent_id="0"):
        self.list_calls.append(str(parent_id))
        return [deepcopy(item) for item in self.directories.get(str(parent_id), [])]

    def file_info(self, file_id):
        for items in self.directories.values():
            for item in items:
                if item.file_id == str(file_id):
                    return deepcopy(item)
        return None

    def close(self):
        self.closed = True


class GuangYaWorkspaceAgentTests(unittest.TestCase):
    def setUp(self):
        workspace_actions.reset_guangya_workspace_context_for_tests()
        self.temp = tempfile.TemporaryDirectory()
        self.obs_dir = Path(self.temp.name) / "observations"
        self.patches = [
            mock.patch.object(guangya_workspace, "_directory", return_value=self.obs_dir),
            mock.patch.object(guangya_workspace, "get_web_secret", return_value="test-secret"),
            mock.patch.object(
                guangya_workspace, "organize_operation_owner_digest",
                side_effect=lambda owner: f"digest:{owner}",
            ),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        workspace_actions.reset_guangya_workspace_context_for_tests()
        self.temp.cleanup()

    def _query(self, client, **overrides):
        arguments = workspace_actions.guangya_fs_query_arguments({
            "operation": "tree", "path": "/a", "page": 1,
            "page_size": 10, "max_items": 100,
            **overrides,
        })
        with mock.patch.object(workspace_actions, "GuangYaClient", return_value=client):
            return workspace_actions.query_guangya_filesystem(
                arguments, ToolContext(owner="owner", session_id="session")
            )

    def test_directory_observation_returns_names_and_opaque_refs_without_ids(self):
        result = self._query(FakeWorkspaceClient())
        self.assertTrue(result.ok)
        self.assertRegex(result.data["observation_ref"], r"^OBS[0-9A-F]{32}$")
        self.assertEqual(result.data["total"], 4)
        names = {item["object_name"] for item in result.data["entries"]}
        self.assertIn("[最新地址]ABC-123.mp4", names)
        self.assertIn("广告词-GHI-789.CHT.srt", names)
        for item in result.data["entries"]:
            self.assertRegex(item["object_ref"], r"^OBJ[0-9A-F]{24}$")
            self.assertNotIn("file_id", item)
            self.assertNotIn("parent_id", item)
            self.assertNotIn("path", item)

    def test_observation_can_continue_by_ref_and_is_owner_bound(self):
        first = self._query(FakeWorkspaceClient(), page_size=2)
        self.assertEqual(
            workspace_actions.latest_guangya_observation_cursor("owner"),
            {
                "observation_ref": first.data["observation_ref"],
                "page": 1,
                "page_size": 2,
                "has_more": True,
            },
        )
        arguments = workspace_actions.guangya_fs_query_arguments({
            "observation_ref": first.data["observation_ref"],
            "page": 2, "page_size": 2,
        })
        second = workspace_actions.query_guangya_filesystem(
            arguments, ToolContext(owner="owner", session_id="session")
        )
        self.assertEqual(second.data["page"], 2)
        self.assertEqual(len(second.data["entries"]), 2)
        self.assertEqual(
            workspace_actions.latest_guangya_observation_cursor("owner")["has_more"],
            False,
        )
        with self.assertRaisesRegex(Exception, "不属于当前会话"):
            guangya_workspace.load_directory_observation(
                first.data["observation_ref"], owner="other-owner"
            )

    def test_llm_projection_preserves_public_names_and_object_refs(self):
        observed = self._query(FakeWorkspaceClient(), page_size=4)
        projected = project_agent_response_for_llm({
            "mode": "read_only",
            "tool_call": {"name": "guangya.fs.query"},
            "result": observed.to_dict(),
        })
        serialized = str(projected)
        self.assertIn("[最新地址]ABC-123.mp4", serialized)
        self.assertIn("OBS", serialized)
        self.assertIn("OBJ", serialized)
        self.assertIn("spam.example.com", serialized)
        self.assertNotIn("内部检查", serialized)
        self.assertNotIn("spam-etag", serialized)

    def test_expired_observation_is_removed_by_maintenance(self):
        client = FakeWorkspaceClient()
        with mock.patch.object(guangya_workspace.time, "time", return_value=1_000.0):
            observed = self._query(client)
        plan_id = guangya_workspace.observation_plan_id(
            observed.data["observation_ref"]
        )
        self.assertTrue(guangya_workspace._plan_path(plan_id).is_file())

        with mock.patch.object(
            guangya_workspace.time, "time",
            return_value=1_000.0 + guangya_workspace._OBSERVATION_TTL_SECONDS + 1,
        ):
            result = guangya_workspace.maintain_workspace_observations()

        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertFalse(guangya_workspace._plan_path(plan_id).exists())

    def test_observation_capacity_is_fair_per_owner(self):
        client = FakeWorkspaceClient()
        owner_a_refs = []
        for _ in range(guangya_workspace._MAX_OBSERVATIONS_PER_OWNER + 2):
            payload = guangya_workspace.create_directory_observation(
                client, owner="owner-a", path="/a", recursive=False, max_items=10
            )
            owner_a_refs.append(guangya_workspace.observation_ref(payload["plan_id"]))
        owner_b = guangya_workspace.create_directory_observation(
            client, owner="owner-b", path="/a", recursive=False, max_items=10
        )
        owner_b_ref = guangya_workspace.observation_ref(owner_b["plan_id"])

        self.assertEqual(
            guangya_workspace.load_directory_observation(
                owner_b_ref, owner="owner-b"
            )["plan_id"],
            owner_b["plan_id"],
        )
        remaining_a = 0
        for ref in owner_a_refs:
            try:
                guangya_workspace.load_directory_observation(ref, owner="owner-a")
            except Exception:
                continue
            remaining_a += 1
        self.assertLessEqual(
            remaining_a, guangya_workspace._MAX_OBSERVATIONS_PER_OWNER
        )

    def test_stale_observation_save_discards_only_new_snapshot(self):
        client = FakeWorkspaceClient()
        previous = guangya_workspace.create_directory_observation(
            client, owner="owner", path="/a", recursive=False, max_items=10
        )
        previous_ref = guangya_workspace.observation_ref(previous["plan_id"])

        class StaleRepository:
            def __init__(self):
                self.begin_called = False

            def begin_context_update(self, *, owner, context_type):
                self.begin_called = True
                return (
                    SimpleNamespace(
                        payload={"observation_ref": previous_ref},
                        generation=2, revision=7,
                    ),
                    workspace_actions.AgentContextWriteGuard(2, 7),
                )

            def replace_latest_guarded(self, **_kwargs):
                return None

        repository = StaleRepository()
        workspace_actions.configure_guangya_workspace_context(repository)
        original_list_dir = client.list_dir

        def guarded_list_dir(parent_id="0"):
            self.assertTrue(repository.begin_called)
            return original_list_dir(parent_id)

        client.list_dir = guarded_list_dir
        with (
            mock.patch.object(workspace_actions, "GuangYaClient", return_value=client),
            self.assertRaisesRegex(Exception, "更新请求取代"),
        ):
            workspace_actions.query_guangya_filesystem(
                workspace_actions.guangya_fs_query_arguments({
                    "operation": "list", "path": "/a", "page": 1,
                    "page_size": 10, "max_items": 10,
                }),
                ToolContext(owner="owner", session_id="session"),
            )
        self.assertTrue(guangya_workspace._plan_path(previous["plan_id"]).is_file())
        self.assertEqual(
            [path.stem for path in self.obs_dir.glob("*.json")],
            [previous["plan_id"]],
        )


if __name__ == "__main__":
    unittest.main()
