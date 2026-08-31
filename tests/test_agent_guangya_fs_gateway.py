"""光鸭通用能力网关与确认写入链路测试。"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from app.agent import guangya_fs_change_actions as change_actions
from app.agent import guangya_workspace_actions as workspace_actions
from app.agent.models import RiskLevel, ToolContext
from app.agent.registry import AgentToolError
from app.agent.tools import build_tool_registry
from app.clients.guangya import GuangYaFile
from app.modules import guangya_fs_change, guangya_workspace


class FakeGatewayClient:
    def __init__(self):
        self.logged_in = True
        self.credential_generation = 31
        self.closed = False
        self.counter = 0
        self.directories: dict[str, list[GuangYaFile]] = {
            "0": [
                GuangYaFile("source", "source", True, parent_id="0", etag="s"),
                GuangYaFile("target", "target", True, parent_id="0", etag="t"),
            ],
            "source": [
                GuangYaFile(
                    "rename",
                    "广告-ABC.mp4",
                    False,
                    parent_id="source",
                    size=100,
                    etag="r",
                    extension="mp4",
                ),
                GuangYaFile(
                    "move",
                    "Move.mp4",
                    False,
                    parent_id="source",
                    size=90,
                    etag="m",
                    extension="mp4",
                ),
                GuangYaFile(
                    "trash",
                    "垃圾残余",
                    True,
                    parent_id="source",
                    etag="x",
                ),
            ],
            "target": [],
            "trash": [],
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
        item = self._pop(str(file_id))
        item.name = str(new_name)
        self.directories[item.parent_id].append(item)
        return True

    def move(self, file_ids, parent_id):
        for file_id in file_ids:
            item = self._pop(str(file_id))
            item.parent_id = str(parent_id)
            self.directories.setdefault(str(parent_id), []).append(item)
        return True

    def delete(self, file_ids):
        for file_id in file_ids:
            item = self._pop(str(file_id))
            if item.is_dir:
                self.directories.pop(str(item.file_id), None)
        return True

    def create_dir(self, name, parent_id="0"):
        self.counter += 1
        file_id = f"created-{self.counter}"
        self.directories.setdefault(str(parent_id), []).append(
            GuangYaFile(file_id, str(name), True, parent_id=str(parent_id), etag="new")
        )
        self.directories[file_id] = []
        return file_id

    def close(self):
        self.closed = True
        return True

    def _pop(self, file_id: str) -> GuangYaFile:
        for items in self.directories.values():
            for index, item in enumerate(items):
                if item.file_id == file_id:
                    return items.pop(index)
        raise RuntimeError("missing")


class GuangYaFSGatewayTests(unittest.TestCase):
    def setUp(self):
        workspace_actions.reset_guangya_workspace_context_for_tests()
        change_actions.reset_guangya_fs_change_context_for_tests()
        self.temp = tempfile.TemporaryDirectory()
        self.obs_dir = Path(self.temp.name) / "observations"
        self.plan_dir = Path(self.temp.name) / "changes"
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
            mock.patch.object(
                guangya_fs_change, "_directory", return_value=self.plan_dir
            ),
            mock.patch.object(
                guangya_fs_change, "get_web_secret", return_value="test-secret"
            ),
            mock.patch.object(
                guangya_fs_change,
                "_owner_digest",
                side_effect=lambda owner: f"digest:{owner}",
            ),
            mock.patch.object(
                change_actions,
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
        change_actions.reset_guangya_fs_change_context_for_tests()
        self.temp.cleanup()

    def _query(self, client: FakeGatewayClient, **values):
        raw = {
            "operation": "list",
            "path": "/source",
            "page": 1,
            "page_size": 10,
            "max_items": 100,
            **values,
        }
        arguments = workspace_actions.guangya_fs_query_arguments(
            {key: value for key, value in raw.items() if value is not None}
        )
        with mock.patch.object(workspace_actions, "GuangYaClient", return_value=client):
            return workspace_actions.query_guangya_filesystem(
                arguments, ToolContext(owner="owner", session_id="session")
            )

    def test_query_supports_search_stat_and_opaque_refs(self):
        client = FakeGatewayClient()
        searched = self._query(client, operation="search", query="垃圾", max_items=100)
        self.assertEqual(searched.data["operation"], "search")
        self.assertEqual(searched.data["total"], 1)
        entry = searched.data["entries"][0]
        self.assertEqual(entry["object_name"], "垃圾残余")
        self.assertRegex(entry["object_ref"], r"^OBJ[0-9A-F]{24}$")
        self.assertNotIn("file_id", entry)

        stated = self._query(
            client,
            operation="stat",
            path="/source/广告-ABC.mp4",
            max_items=None,
        )
        self.assertEqual(stated.data["operation"], "stat")
        self.assertEqual(stated.data["total"], 1)
        self.assertEqual(stated.data["entries"][0]["object_name"], "广告-ABC.mp4")

    def test_query_can_start_from_root_but_root_stat_is_not_an_object(self):
        client = FakeGatewayClient()
        listed = self._query(client, path="/")

        self.assertEqual(listed.data["scope"], "根目录")
        self.assertEqual(
            {item["object_name"] for item in listed.data["entries"]},
            {"source", "target"},
        )
        with self.assertRaises(AgentToolError):
            workspace_actions.guangya_fs_query_arguments(
                {"operation": "stat", "path": "/"}
            )

    def test_mixed_plan_executes_only_frozen_operations_and_verifies_results(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {
            item["object_name"]: item["object_ref"] for item in observed.data["entries"]
        }
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        plan = guangya_fs_change.build_fs_change_plan(
            client,
            owner="owner",
            observation=observation,
            trigger_strm=False,
            operations=[
                {
                    "op": "rename",
                    "object_ref": refs["广告-ABC.mp4"],
                    "new_name": "ABC.mp4",
                },
                {
                    "op": "move",
                    "object_ref": refs["Move.mp4"],
                    "target_path": "/target",
                },
                {"op": "trash", "object_ref": refs["垃圾残余"]},
                {"op": "create_directory", "parent_path": "/target", "name": "新目录"},
            ],
        )
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        result = guangya_fs_change.execute_fs_change_plan(
            {
                "version": 1,
                "plan_id": plan["plan_id"],
                "plan_fingerprint": plan["fingerprint"],
                "owner_digest": "digest:owner",
                "credential_generation": 31,
            },
            client_factory=lambda: client,
        )
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["renamed"], 1)
        self.assertEqual(result["stats"]["moved"], 1)
        self.assertEqual(result["stats"]["trashed"], 1)
        self.assertEqual(result["stats"]["created"], 1)
        self.assertEqual(
            [item.name for item in client.directories["source"]], ["ABC.mp4"]
        )
        self.assertEqual(
            {item.name for item in client.directories["target"]}, {"Move.mp4", "新目录"}
        )

    def test_execution_rejects_stale_snapshot_before_any_write(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        target = next(
            item
            for item in observed.data["entries"]
            if item["object_name"] == "垃圾残余"
        )
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        plan = guangya_fs_change.build_fs_change_plan(
            client,
            owner="owner",
            observation=observation,
            trigger_strm=False,
            operations=[{"op": "trash", "object_ref": target["object_ref"]}],
        )
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        client.directories["source"][2].etag = "changed"
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(
                {
                    "version": 1,
                    "plan_id": plan["plan_id"],
                    "plan_fingerprint": plan["fingerprint"],
                    "owner_digest": "digest:owner",
                    "credential_generation": 31,
                },
                client_factory=lambda: client,
            )
        self.assertIsNotNone(client.file_info("trash"))

    def test_external_parent_change_invalidates_frozen_source_location(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        target = next(
            item
            for item in observed.data["entries"]
            if item["object_name"] == "垃圾残余"
        )
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        plan = guangya_fs_change.build_fs_change_plan(
            client,
            owner="owner",
            observation=observation,
            trigger_strm=False,
            operations=[{"op": "trash", "object_ref": target["object_ref"]}],
        )
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        client.move(["trash"], "target")

        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(
                {
                    "version": 1,
                    "plan_id": plan["plan_id"],
                    "plan_fingerprint": plan["fingerprint"],
                    "owner_digest": "digest:owner",
                    "credential_generation": 31,
                },
                client_factory=lambda: client,
            )
        self.assertIsNotNone(client.file_info("trash"))
        self.assertEqual(client.file_info("trash").parent_id, "target")

    def test_completed_plan_cannot_be_replayed(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        target = next(
            item
            for item in observed.data["entries"]
            if item["object_name"] == "广告-ABC.mp4"
        )
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        plan = guangya_fs_change.build_fs_change_plan(
            client,
            owner="owner",
            observation=observation,
            trigger_strm=False,
            operations=[
                {
                    "op": "rename",
                    "object_ref": target["object_ref"],
                    "new_name": "ABC.mp4",
                }
            ],
        )
        payload = {
            "version": 1,
            "plan_id": plan["plan_id"],
            "plan_fingerprint": plan["fingerprint"],
            "owner_digest": "digest:owner",
            "credential_generation": 31,
        }
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        first = guangya_fs_change.execute_fs_change_plan(
            payload, client_factory=lambda: client
        )
        self.assertFalse(first["partial"])

        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(
                payload, client_factory=lambda: client
            )

    def test_multiple_moves_to_same_target_ignore_own_directory_version_changes(self):
        class UpdatingDirectoryClient(FakeGatewayClient):
            def move(self, file_ids, parent_id):
                result = super().move(file_ids, parent_id)
                target = next(
                    item for item in self.directories["0"] if item.file_id == parent_id
                )
                target.etag += "-changed"
                target.updated_at += 1
                return result

        client = UpdatingDirectoryClient()
        observed = self._query(client)
        refs = {
            item["object_name"]: item["object_ref"] for item in observed.data["entries"]
        }
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        plan = guangya_fs_change.build_fs_change_plan(
            client,
            owner="owner",
            observation=observation,
            trigger_strm=False,
            operations=[
                {
                    "op": "move",
                    "object_ref": refs["广告-ABC.mp4"],
                    "target_path": "/target",
                },
                {
                    "op": "move",
                    "object_ref": refs["Move.mp4"],
                    "target_path": "/target",
                },
            ],
        )
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        result = guangya_fs_change.execute_fs_change_plan(
            {
                "version": 1,
                "plan_id": plan["plan_id"],
                "plan_fingerprint": plan["fingerprint"],
                "owner_digest": "digest:owner",
                "credential_generation": 31,
            },
            client_factory=lambda: client,
        )
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["moved"], 2)

    def test_preview_confirmation_queues_durable_job_without_accepting_new_arguments(
        self,
    ):
        client = FakeGatewayClient()
        observed = self._query(client)
        target = next(
            item
            for item in observed.data["entries"]
            if item["object_name"] == "垃圾残余"
        )
        arguments = change_actions.guangya_fs_change_preview_arguments(
            {
                "operations": [{"op": "trash", "object_ref": target["object_ref"]}],
                "trigger_strm": False,
            }
        )
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(change_actions, "GuangYaClient", return_value=client):
            preview = change_actions.preview_guangya_fs_change(arguments, context)
            confirmation, fingerprint = (
                change_actions.prepare_guangya_fs_change_confirmation({}, context)
            )
        self.assertEqual(preview.status, "ready")
        self.assertEqual(preview.data["trash_count"], 1)
        self.assertEqual(confirmation.status, "confirmation_required")
        with self.assertRaises(AgentToolError):
            change_actions.execute_guangya_fs_change({})

        manager = mock.Mock()
        manager.start_durable_operation.return_value = {
            "ok": True,
            "task_id": "a" * 32,
            "queued": True,
            "queue_position": 1,
        }
        with mock.patch(
            "app.modules.organize_tasks.get_organize_manager", return_value=manager
        ):
            accepted = change_actions.execute_guangya_fs_change_confirmed(
                {}, fingerprint, context
            )
        self.assertEqual(accepted.status, "accepted")
        self.assertRegex(accepted.data["operation_ref"], r"^GY-")
        self.assertEqual(
            manager.start_durable_operation.call_args.kwargs["job_kind"],
            "agent_guangya_fs_change",
        )

    def test_registry_exposes_read_and_confirmation_bound_write_tools(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(
            capabilities["guangya.capabilities"]["risk"], RiskLevel.READ.value
        )
        self.assertEqual(capabilities["guangya.fs.query"]["risk"], RiskLevel.READ.value)
        self.assertEqual(
            capabilities["guangya.fs.change.preview"]["risk"], RiskLevel.READ.value
        )
        self.assertEqual(
            capabilities["guangya.fs.change.execute"]["risk"], RiskLevel.DANGER.value
        )
        self.assertTrue(
            capabilities["guangya.fs.change.execute"]["requires_confirmation"]
        )
        self.assertNotIn("guangya.directory.inspect", capabilities)
        self.assertNotIn("guangya.change_plan.preview", capabilities)
        self.assertNotIn("guangya.change_plan.execute", capabilities)
        rename_schema = capabilities["guangya.rename.preview"]["parameters"]
        self.assertEqual(
            rename_schema["properties"]["mode"]["enum"],
            ["remove_bitrate", "replace_text"],
        )
        self.assertNotIn("new_name", rename_schema["properties"])


if __name__ == "__main__":
    unittest.main()
