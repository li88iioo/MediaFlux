"""光鸭只读工作区快照的持久化与公开投影测试。"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.agent import guangya_workspace_actions as workspace_actions
from app.agent.models import ToolContext
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
                    "spam",
                    "[最新地址]ABC-123.mp4",
                    False,
                    parent_id="a",
                    size=100,
                    etag="spam-etag",
                    extension="mp4",
                ),
                GuangYaFile(
                    "clean",
                    "DEF-456.mp4",
                    False,
                    parent_id="a",
                    size=90,
                    etag="clean-etag",
                    extension="mp4",
                ),
                GuangYaFile(
                    "nested",
                    "spam.example.com",
                    True,
                    parent_id="a",
                    etag="nested-etag",
                    updated_at=3,
                ),
            ],
            "nested": [
                GuangYaFile(
                    "subtitle",
                    "广告词-GHI-789.CHT.srt",
                    False,
                    parent_id="nested",
                    size=10,
                    etag="sub-etag",
                    extension="srt",
                )
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
            mock.patch.object(
                guangya_workspace, "_directory", return_value=self.obs_dir
            ),
            mock.patch.object(
                guangya_workspace, "get_web_secret", return_value="test-secret"
            ),
            mock.patch.object(
                guangya_workspace,
                "organize_operation_owner_digest",
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
        arguments = workspace_actions.guangya_fs_query_arguments(
            {
                "operation": "tree",
                "path": "/a",
                "page": 1,
                "page_size": 10,
                "max_items": 100,
                **overrides,
            }
        )
        with mock.patch.object(workspace_actions, "GuangYaClient", return_value=client):
            return workspace_actions.query_guangya_filesystem(
                arguments, ToolContext(owner="owner", session_id="session")
            )

    def test_directory_observation_returns_names_and_opaque_refs_without_ids(self):
        result = self._query(FakeWorkspaceClient())
        self.assertTrue(result.ok)
        self.assertRegex(result.data["observation_ref"], "^OBS[0-9A-F]{32}$")
        self.assertEqual(result.data["total"], 4)
        names = {item["object_name"] for item in result.data["entries"]}
        self.assertIn("[最新地址]ABC-123.mp4", names)
        self.assertIn("广告词-GHI-789.CHT.srt", names)
        for item in result.data["entries"]:
            self.assertRegex(item["object_ref"], "^OBJ[0-9A-F]{24}$")
            self.assertNotIn("file_id", item)
            self.assertNotIn("parent_id", item)
            self.assertNotIn("path", item)

    def test_default_page_size_supports_large_model_observations(self):
        client = FakeWorkspaceClient()
        client.directories["a"] = [
            GuangYaFile(
                f"item-{index}",
                f"Episode-{index:03d}.mkv",
                False,
                parent_id="a",
                size=index,
                extension="mkv",
            )
            for index in range(60)
        ]
        arguments = workspace_actions.guangya_fs_query_arguments(
            {"operation": "list", "path": "/a", "max_items": 100}
        )
        with mock.patch.object(workspace_actions, "GuangYaClient", return_value=client):
            result = workspace_actions.query_guangya_filesystem(
                arguments, ToolContext(owner="owner", session_id="session")
            )
        self.assertEqual(result.data["page_size"], 50)
        self.assertEqual(len(result.data["entries"]), 50)
        self.assertTrue(result.data["has_more"])

    def test_query_normalizes_tree_with_query_and_filters_to_video(self):
        arguments = workspace_actions.guangya_fs_query_arguments(
            {
                "operation": "tree",
                "path": "/a",
                "query": "ABC",
                "kinds": ["video"],
            }
        )
        self.assertEqual(arguments["operation"], "search")
        self.assertEqual(arguments["kinds"], ("video",))
        with mock.patch.object(
            workspace_actions, "GuangYaClient", return_value=FakeWorkspaceClient()
        ):
            result = workspace_actions.query_guangya_filesystem(
                arguments, ToolContext(owner="owner", session_id="session")
            )
        self.assertEqual(result.data["operation"], "search")
        self.assertEqual(result.data["kinds"], ["video"])
        self.assertEqual(
            [item["object_name"] for item in result.data["entries"]],
            ["[最新地址]ABC-123.mp4"],
        )

    def test_query_can_merge_multiple_exact_directories_into_one_snapshot(self):
        client = FakeWorkspaceClient()
        client.directories["0"].append(
            GuangYaFile("b", "b", True, parent_id="0", etag="root-b")
        )
        client.directories["b"] = [
            GuangYaFile(
                "episode-b",
                "Episode-B.mkv",
                False,
                parent_id="b",
                size=80,
                extension="mkv",
            )
        ]
        arguments = workspace_actions.guangya_fs_query_arguments(
            {
                "operation": "list",
                "paths": ["/a", "/b"],
                "kinds": ["video"],
            }
        )
        with mock.patch.object(workspace_actions, "GuangYaClient", return_value=client):
            result = workspace_actions.query_guangya_filesystem(
                arguments, ToolContext(owner="owner", session_id="session")
            )
        self.assertEqual(result.data["scope"], "2 个目录")
        self.assertEqual(result.data["scopes"], ["a", "b"])
        self.assertEqual(result.data["max_depth"], 0)
        self.assertEqual(
            {item["object_name"] for item in result.data["entries"]},
            {"[最新地址]ABC-123.mp4", "DEF-456.mp4", "Episode-B.mkv"},
        )
        self.assertEqual(
            {item["location"] for item in result.data["entries"]}, {"a", "b"}
        )

    def test_query_rejects_unknown_kind_filter(self):
        with self.assertRaisesRegex(
            workspace_actions.AgentToolError, "不支持的对象类型"
        ):
            workspace_actions.guangya_fs_query_arguments(
                {"operation": "tree", "path": "/a", "kinds": ["archive"]}
            )

    def test_missing_exact_path_returns_actionable_safe_error(self):
        arguments = workspace_actions.guangya_fs_query_arguments(
            {"operation": "list", "path": "/a/not-there"}
        )
        with (
            mock.patch.object(
                workspace_actions, "GuangYaClient", return_value=FakeWorkspaceClient()
            ),
            self.assertRaisesRegex(workspace_actions.AgentToolError, "请先列出父目录"),
        ):
            workspace_actions.query_guangya_filesystem(
                arguments, ToolContext(owner="owner", session_id="session")
            )

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
        arguments = workspace_actions.guangya_fs_query_arguments(
            {
                "observation_ref": first.data["observation_ref"],
                "page": 2,
                "page_size": 2,
            }
        )
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

    def test_expired_observation_is_removed_by_maintenance(self):
        client = FakeWorkspaceClient()
        with mock.patch.object(guangya_workspace.time, "time", return_value=1000.0):
            observed = self._query(client)
        plan_id = guangya_workspace.observation_plan_id(
            observed.data["observation_ref"]
        )
        self.assertTrue(guangya_workspace._plan_path(plan_id).is_file())
        with mock.patch.object(
            guangya_workspace.time,
            "time",
            return_value=1000.0 + guangya_workspace._OBSERVATION_TTL_SECONDS + 1,
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
            guangya_workspace.load_directory_observation(owner_b_ref, owner="owner-b")[
                "plan_id"
            ],
            owner_b["plan_id"],
        )
        remaining_a = 0
        for ref in owner_a_refs:
            try:
                guangya_workspace.load_directory_observation(ref, owner="owner-a")
            except guangya_workspace.GuangYaWorkspaceError:
                continue
            remaining_a += 1
        self.assertLessEqual(remaining_a, guangya_workspace._MAX_OBSERVATIONS_PER_OWNER)

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
                        generation=2,
                        revision=7,
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
                workspace_actions.guangya_fs_query_arguments(
                    {
                        "operation": "list",
                        "path": "/a",
                        "page": 1,
                        "page_size": 10,
                        "max_items": 10,
                    }
                ),
                ToolContext(owner="owner", session_id="session"),
            )
        self.assertTrue(guangya_workspace._plan_path(previous["plan_id"]).is_file())
        self.assertEqual(
            [path.stem for path in self.obs_dir.glob("*.json")], [previous["plan_id"]]
        )

    def test_new_observation_keeps_previous_snapshot_for_multi_step_planning(self):
        client = FakeWorkspaceClient()
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(workspace_actions, "GuangYaClient", return_value=client):
            first = workspace_actions.query_guangya_filesystem(
                workspace_actions.guangya_fs_query_arguments(
                    {"operation": "list", "path": "/a"}
                ),
                context,
            )
            workspace_actions.query_guangya_filesystem(
                workspace_actions.guangya_fs_query_arguments(
                    {"operation": "stat", "path": "/a/DEF-456.mp4"}
                ),
                context,
            )

        preserved = guangya_workspace.load_directory_observation(
            first.data["observation_ref"], owner="owner"
        )
        self.assertEqual(
            preserved["plan_id"], first.data["observation_ref"][3:].lower()
        )
