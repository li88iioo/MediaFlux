"""光鸭通用能力网关与确认写入链路测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from app import database as db
from app.agent import guangya_fs_change_actions as change_actions
from app.agent import guangya_workspace_actions as workspace_actions
from app.agent.errors import AgentToolError
from app.agent.models import ToolContext
from app.clients.guangya import GuangYaFile, GuangYaWriteRejected
from app.modules import guangya_fs_change, guangya_workspace
from tests.support import isolated_test_database


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
                GuangYaFile("trash", "垃圾残余", True, parent_id="source", etag="x"),
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

    def copy(self, file_ids, parent_id):
        for file_id in file_ids:
            source = self.file_info(str(file_id))
            if source is None:
                raise RuntimeError("missing")
            self.counter += 1
            copied = deepcopy(source)
            copied.file_id = f"copied-{self.counter}"
            copied.parent_id = str(parent_id)
            self.directories.setdefault(str(parent_id), []).append(copied)
        return f"copy-task-{self.counter}"

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
        self.enterContext(isolated_test_database())
        self.enterContext(mock.patch("socket.socket.connect", side_effect=AssertionError("禁止外联")))
        workspace_actions.reset_guangya_workspace_context_for_tests()
        change_actions.reset_guangya_fs_change_context_for_tests()
        self.temp = tempfile.TemporaryDirectory()
        self._job_sequence = 0
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

    def _confirmed_plan(
        self,
        client: FakeGatewayClient,
        operation: dict,
        *,
        trigger_strm: bool = False,
        path: str = "/source",
    ) -> dict:
        observed = self._query(client, path=path)
        entries = {item["object_name"]: item for item in observed.data["entries"]}
        operation = dict(operation)
        source_name = str(operation.pop("source_name", ""))
        if source_name:
            operation["object_ref"] = entries[source_name]["object_ref"]
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        plan = guangya_fs_change.build_fs_change_plan(
            client,
            owner="owner",
            observation=observation,
            trigger_strm=trigger_strm,
            operations=[operation],
        )
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        return plan

    def _execute_with_strm_scope(
        self,
        client: FakeGatewayClient,
        plan: dict,
        *,
        strm_source_ids: tuple[str, ...] = ("source",),
        organize_source_ids: tuple[str, ...] = ("source",),
        organize_target_id: str = "target",
    ) -> tuple[dict, mock.Mock]:
        scheduler = mock.Mock()
        scheduler.trigger.return_value = {"ok": True}
        config_values = {
            "GY_ORGANIZE_SOURCE_DIRS": json.dumps(
                [{"id": source_id, "name": source_id} for source_id in organize_source_ids]
            ),
            "GY_ORGANIZE_TARGET_DIR": organize_target_id,
        }

        def get_config(key, default=""):
            return config_values.get(key, default)

        with (
            mock.patch("app.modules.scheduler.get_scheduler", return_value=scheduler),
            mock.patch(
                "app.modules.strm.configured_strm_source_plans",
                return_value=(
                    [{"id": source_id, "name": source_id} for source_id in strm_source_ids],
                    "",
                ),
            ),
            mock.patch("app.config.get", side_effect=get_config),
        ):
            result = guangya_fs_change.execute_fs_change_plan(
                self._queued_payload(plan), client_factory=lambda: client
            )
        return result, scheduler

    def _queued_payload(self, plan: dict, *, job_id: str = "") -> dict:
        if not job_id:
            self._job_sequence += 1
            job_id = f"{self._job_sequence:032x}"
        guangya_fs_change.bind_fs_change_plan_job(
            plan["plan_id"],
            owner_digest="digest:owner",
            expected_fingerprint=plan["fingerprint"],
            job_id=job_id,
            queue_until_epoch=10**12,
        )
        return {
            "version": 1,
            "plan_id": plan["plan_id"],
            "plan_fingerprint": plan["fingerprint"],
            "owner_digest": "digest:owner",
            "credential_generation": 31,
            "job_id": job_id,
        }

    def test_strm_trigger_skips_successful_unrelated_prefix_sibling_change(self):
        client = FakeGatewayClient()
        client.directories["0"].append(
            GuangYaFile("source-2", "source-2", True, parent_id="0", etag="s2")
        )
        client.directories["source-2"] = [
            GuangYaFile(
                "private",
                "Private.mp4",
                False,
                parent_id="source-2",
                size=80,
                etag="p",
                extension="mp4",
            )
        ]
        plan = self._confirmed_plan(
            client,
            {"op": "rename", "source_name": "Private.mp4", "new_name": "Private-renamed.mp4"},
            trigger_strm=True,
            path="/source-2",
        )

        result, scheduler = self._execute_with_strm_scope(client, plan)

        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["renamed"], 1)
        scheduler.trigger.assert_not_called()
        self.assertNotIn("strm_triggered", result["stats"])

    def _nested_strm_root_client(self):
        client = FakeGatewayClient()
        client.directories["0"].extend(
            [
                GuangYaFile("media", "媒体", True, parent_id="0", etag="media"),
                GuangYaFile("other", "other", True, parent_id="0", etag="other"),
            ]
        )
        client.directories["media"] = [
            GuangYaFile("guoman", "国漫", True, parent_id="media", etag="guoman"),
            GuangYaFile(
                "outside-media",
                "其他.mp4",
                False,
                parent_id="media",
                size=80,
                etag="outside",
                extension="mp4",
            ),
        ]
        client.directories["guoman"] = []
        client.directories["other"] = []
        return client

    def test_directory_ancestor_of_strm_root_is_in_scope_but_parent_path_is_not(self):
        cases = (
            ("rename", "/", "媒体", {"new_name": "媒体-改名"}, True),
            ("move", "/", "媒体", {"target_path": "/other"}, True),
            ("rename", "/媒体", "其他.mp4", {"new_name": "其他-改名.mp4"}, False),
        )
        for op, path, source_name, extra, should_trigger in cases:
            with self.subTest(op=op, source_name=source_name):
                client = self._nested_strm_root_client()
                operation = {"op": op, "source_name": source_name, **extra}
                plan = self._confirmed_plan(
                    client, operation, trigger_strm=True, path=path
                )

                result, scheduler = self._execute_with_strm_scope(
                    client,
                    plan,
                    strm_source_ids=("guoman",),
                    organize_source_ids=("0",),
                    organize_target_id="",
                )

                self.assertFalse(result["partial"])
                self.assertEqual(result["stats"]["moved" if op == "move" else "renamed"], 1)
                if should_trigger:
                    scheduler.trigger.assert_called_once_with(
                        "organize", sync_mode="full", selected_source_ids=["guoman"]
                    )
                else:
                    scheduler.trigger.assert_not_called()

    def test_organize_input_sources_are_not_strm_scope(self):
        for organize_source_id in ("new-nsfw", "0"):
            with self.subTest(organize_source_id=organize_source_id):
                client = FakeGatewayClient()
                client.directories["0"].extend(
                    [
                        GuangYaFile("整理", "整理", True, parent_id="0", etag="archive"),
                        GuangYaFile("new-nsfw", "NewNsfw", True, parent_id="0", etag="nsfw"),
                        GuangYaFile("zhuxian", "诛仙", True, parent_id="0", etag="novel"),
                    ]
                )
                client.directories["整理"] = []
                client.directories["new-nsfw"] = [
                    GuangYaFile(
                        "new-file",
                        "NewNsfw.mp4",
                        False,
                        parent_id="new-nsfw",
                        size=80,
                        etag="new",
                        extension="mp4",
                    )
                ]
                client.directories["zhuxian"] = [
                    GuangYaFile(
                        "zhuxian-file",
                        "诛仙.mp4",
                        False,
                        parent_id="zhuxian",
                        size=80,
                        etag="novel-file",
                        extension="mp4",
                    )
                ]
                for source_path, source_name in (
                    ("/NewNsfw", "NewNsfw.mp4"),
                    ("/诛仙", "诛仙.mp4"),
                ):
                    with self.subTest(source_path=source_path):
                        plan = self._confirmed_plan(
                            client,
                            {
                                "op": "rename",
                                "source_name": source_name,
                                "new_name": f"{source_name}.renamed.mp4",
                            },
                            trigger_strm=True,
                            path=source_path,
                        )
                        result, scheduler = self._execute_with_strm_scope(
                            client,
                            plan,
                            strm_source_ids=("整理",),
                            organize_source_ids=(organize_source_id,),
                            organize_target_id="",
                        )

                        self.assertFalse(result["partial"])
                        self.assertEqual(result["stats"]["renamed"], 1)
                        scheduler.trigger.assert_not_called()

    def test_move_into_and_out_of_strm_source_still_triggers(self):
        for direction in ("in", "out"):
            with self.subTest(direction=direction):
                client = FakeGatewayClient()
                client.directories["0"].extend(
                    [
                        GuangYaFile("整理", "整理", True, parent_id="0", etag="archive"),
                        GuangYaFile("other", "other", True, parent_id="0", etag="other"),
                    ]
                )
                client.directories["整理"] = []
                client.directories["other"] = []
                if direction == "in":
                    client.directories["other"].append(
                        GuangYaFile(
                            "other-file",
                            "待整理.mp4",
                            False,
                            parent_id="other",
                            size=80,
                            etag="in-file",
                            extension="mp4",
                        )
                    )
                    path, source_name, target_path = "/other", "待整理.mp4", "/整理"
                else:
                    client.directories["整理"].append(
                        GuangYaFile(
                            "archive-file",
                            "已归档.mp4",
                            False,
                            parent_id="整理",
                            size=80,
                            etag="out-file",
                            extension="mp4",
                        )
                    )
                    path, source_name, target_path = "/整理", "已归档.mp4", "/other"
                plan = self._confirmed_plan(
                    client,
                    {"op": "move", "source_name": source_name, "target_path": target_path},
                    trigger_strm=True,
                    path=path,
                )

                result, scheduler = self._execute_with_strm_scope(
                    client,
                    plan,
                    strm_source_ids=("整理",),
                    organize_source_ids=("new-nsfw",),
                    organize_target_id="",
                )

                self.assertFalse(result["partial"])
                self.assertEqual(result["stats"]["moved"], 1)
                scheduler.trigger.assert_called_once_with(
                    "organize", sync_mode="full", selected_source_ids=["整理"]
                )

    def test_copy_out_of_scope_does_not_trigger_for_unchanged_source(self):
        client = FakeGatewayClient()
        client.directories["0"].append(
            GuangYaFile("other", "other", True, parent_id="0", etag="o")
        )
        client.directories["other"] = []
        plan = self._confirmed_plan(
            client,
            {
                "op": "copy",
                "source_name": "Move.mp4",
                "target_path": "/other",
            },
            trigger_strm=True,
        )

        result, scheduler = self._execute_with_strm_scope(client, plan)

        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["copied"], 1)
        scheduler.trigger.assert_not_called()

    def test_strm_trigger_covers_move_into_and_out_of_configured_archive(self):
        for direction in ("out", "in"):
            with self.subTest(direction=direction):
                client = FakeGatewayClient()
                client.directories["0"].append(
                    GuangYaFile("other", "other", True, parent_id="0", etag="o")
                )
                client.directories["other"] = []
                if direction == "out":
                    plan = self._confirmed_plan(
                        client,
                        {
                            "op": "move",
                            "source_name": "Move.mp4",
                            "target_path": "/other",
                        },
                        trigger_strm=True,
                    )
                else:
                    client.directories["other"].append(
                        GuangYaFile(
                            "other-file",
                            "Other.mp4",
                            False,
                            parent_id="other",
                            size=70,
                            etag="o1",
                            extension="mp4",
                        )
                    )
                    plan = self._confirmed_plan(
                        client,
                        {
                            "op": "move",
                            "source_name": "Other.mp4",
                            "target_path": "/target",
                        },
                        trigger_strm=True,
                        path="/other",
                    )

                result, scheduler = self._execute_with_strm_scope(client, plan, strm_source_ids=("source", "target"))

                self.assertFalse(result["partial"])
                self.assertEqual(result["stats"]["moved"], 1)
                scheduler.trigger.assert_called_once_with(
                    "organize", sync_mode="full", selected_source_ids=["source" if direction == "out" else "target"]
                )
                self.assertEqual(result["stats"]["strm_triggered"], 1)

    def test_organize_target_not_configured_as_strm_source_does_not_trigger(self):
        client = FakeGatewayClient()
        client.directories["0"].append(
            GuangYaFile("other", "other", True, parent_id="0", etag="o")
        )
        client.directories["other"] = [
            GuangYaFile("other-file", "Other.mp4", False, parent_id="other", size=70,
                        etag="o1", extension="mp4")
        ]
        plan = self._confirmed_plan(
            client,
            {"op": "move", "source_name": "Other.mp4", "target_path": "/target"},
            trigger_strm=True,
            path="/other",
        )
        result, scheduler = self._execute_with_strm_scope(
            client, plan, strm_source_ids=("source",), organize_target_id="target"
        )
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["moved"], 1)
        self.assertEqual(result["stats"]["strm_trigger_skipped"], 1)
        scheduler.trigger.assert_not_called()

    def test_strm_trigger_covers_archive_root_object_and_root_destination(self):
        client = FakeGatewayClient()
        client.directories["0"].append(
            GuangYaFile("container", "container", True, parent_id="0", etag="c")
        )
        client.directories["container"] = [
            GuangYaFile("archive", "archive", True, parent_id="container", etag="a")
        ]
        client.directories["archive"] = []
        plan = self._confirmed_plan(
            client,
            {"op": "move", "source_name": "archive", "target_path": "/"},
            trigger_strm=True,
            path="/container",
        )

        result, scheduler = self._execute_with_strm_scope(
            client, plan, strm_source_ids=("source", "archive"), organize_source_ids=(), organize_target_id="archive"
        )

        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["moved"], 1)
        scheduler.trigger.assert_called_once_with(
            "organize", sync_mode="full", selected_source_ids=["archive"]
        )

    def test_new_empty_directory_never_triggers_strm(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(
            client,
            {"op": "create_directory", "parent_path": "/", "name": "root-created"},
            trigger_strm=True,
            path="/",
        )

        result, scheduler = self._execute_with_strm_scope(
            client, plan, strm_source_ids=("source",), organize_source_ids=(), organize_target_id="0"
        )

        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["created"], 1)
        scheduler.trigger.assert_not_called()
        self.assertEqual(result["stats"]["strm_trigger_skipped"], 1)

    def test_only_successful_relevant_operations_can_trigger_strm(self):
        client = FakeGatewayClient()
        client.directories["0"].append(
            GuangYaFile("source-2", "source-2", True, parent_id="0", etag="s2")
        )
        client.directories["source-2"] = [
            GuangYaFile(
                "private",
                "Private.mp4",
                False,
                parent_id="source-2",
                size=80,
                etag="p",
                extension="mp4",
            )
        ]
        observed = self._query(client, path="/", operation="tree", page_size=50)
        entries = {item["object_name"]: item["object_ref"] for item in observed.data["entries"]}
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        operations = [
            {"op": "rename", "object_ref": entries["广告-ABC.mp4"], "new_name": "ABC.mp4"},
            {
                "op": "rename",
                "object_ref": entries["Private.mp4"],
                "new_name": "Private-renamed.mp4",
            },
        ]
        config_values = {
            "GY_ORGANIZE_SOURCE_DIRS": json.dumps([{"id": "source", "name": "source"}]),
            "GY_ORGANIZE_TARGET_DIR": "target",
        }
        with (
            mock.patch(
                "app.modules.strm.configured_strm_source_plans",
                return_value=([{"id": "source", "name": "source"}], ""),
            ),
            mock.patch(
                "app.config.get",
                side_effect=lambda key, default="": config_values.get(key, default),
            ),
        ):
            plan = guangya_fs_change.build_fs_change_plan(
                client, owner="owner", observation=observation, operations=operations, trigger_strm=True
            )
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )

        scheduler = mock.Mock()
        scheduler.trigger.return_value = {"ok": True}
        original_rename = client.rename

        def rename(file_id, new_name):
            if str(file_id) == "rename":
                raise GuangYaWriteRejected("rename", code="rejected")
            return original_rename(file_id, new_name)

        with (
            mock.patch.object(client, "rename", side_effect=rename),
            mock.patch("app.modules.scheduler.get_scheduler", return_value=scheduler),
            mock.patch("app.modules.strm.configured_strm_source_plans", return_value=([{"id": "source", "name": "source"}], "")),
            mock.patch("app.config.get", side_effect=lambda key, default="": config_values.get(key, default)),
        ):
            result = guangya_fs_change.execute_fs_change_plan(
                self._queued_payload(plan), client_factory=lambda: client
            )

        self.assertTrue(result["partial"])
        self.assertEqual(result["stats"]["failed"], 1)
        self.assertEqual(result["stats"]["renamed"], 1)
        scheduler.trigger.assert_not_called()

    def test_scope_lookup_failure_keeps_real_write_result_without_triggering(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(
            client,
            {"op": "rename", "source_name": "Move.mp4", "new_name": "Move-renamed.mp4"},
            trigger_strm=True,
        )
        scheduler = mock.Mock()
        with (
            mock.patch("app.modules.scheduler.get_scheduler", return_value=scheduler),
            mock.patch(
                "app.modules.strm.configured_strm_source_plans",
                side_effect=RuntimeError("scope unavailable"),
            ),
        ):
            result = guangya_fs_change.execute_fs_change_plan(
                self._queued_payload(plan), client_factory=lambda: client
            )

        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["renamed"], 1)
        self.assertEqual(result["stats"]["strm_scope_unknown"], 1)
        scheduler.trigger.assert_not_called()

    def test_partly_unknown_source_paths_are_reconciled_without_scanning_known_unrelated_roots(self):
        from app.modules.strm import trigger_cloud_changes

        for paths, selected in ((["/A/child/one.mp4", "/B/child/two.mp4"], ["a", "b"]),
                                (["/B/child/two.mp4"], ["b"])):
            with self.subTest(paths=paths):
                operations = [
                    {"op": "rename", "source_path": path,
                     "source": {"file_id": str(index), "parent_id": f"child-{index}"}}
                    for index, path in enumerate(paths)
                ]
                scheduler = mock.Mock()
                scheduler.trigger.return_value = {"ok": True}
                with mock.patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
                    result = trigger_cloud_changes(
                        operations, sources={"a": "/A", "b": "", "c": "/C"}
                    )
                self.assertEqual(result, {"strm_scope_unknown": 1, "strm_triggered": 1})
                scheduler.trigger.assert_called_once_with(
                    "organize", sync_mode="full", selected_source_ids=selected
                )

    def test_trigger_strm_false_never_reads_or_triggers_scope(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(
            client,
            {"op": "rename", "source_name": "Move.mp4", "new_name": "Move-renamed.mp4"},
            trigger_strm=False,
        )
        scheduler = mock.Mock()
        with (
            mock.patch("app.modules.scheduler.get_scheduler", return_value=scheduler),
            mock.patch(
                "app.modules.strm.configured_strm_source_plans",
                side_effect=AssertionError("trigger_strm=False 不应读取 STRM 配置"),
            ),
        ):
            result = guangya_fs_change.execute_fs_change_plan(
                self._queued_payload(plan), client_factory=lambda: client
            )

        self.assertFalse(result["partial"])
        scheduler.trigger.assert_not_called()

    def test_query_supports_search_stat_and_opaque_refs(self):
        client = FakeGatewayClient()
        searched = self._query(client, operation="search", query="垃圾", max_items=100)
        self.assertEqual(searched.data["operation"], "search")
        self.assertEqual(searched.data["total"], 1)
        entry = searched.data["entries"][0]
        self.assertEqual(entry["object_name"], "垃圾残余")
        self.assertRegex(entry["object_ref"], "^OBJ[0-9A-F]{24}$")
        self.assertNotIn("file_id", entry)
        stated = self._query(
            client, operation="stat", path="/source/广告-ABC.mp4", max_items=None
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

    def test_preview_resolves_object_refs_across_recent_owner_observations(self):
        client = FakeGatewayClient()
        source = self._query(client, path="/source")
        source_ref = next(
            item["object_ref"]
            for item in source.data["entries"]
            if item["object_name"] == "广告-ABC.mp4"
        )
        self._query(client, path="/target")
        arguments = change_actions.guangya_fs_change_preview_arguments(
            {
                "operations": [
                    {
                        "op": "rename",
                        "object_ref": source_ref,
                        "new_name": "ABC.mp4",
                    }
                ],
                "trigger_strm": False,
            }
        )

        with mock.patch.object(change_actions, "GuangYaClient", return_value=client):
            result = change_actions.preview_guangya_fs_change(
                arguments, ToolContext(owner="owner", session_id="session")
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.data["total"], 1)

    def test_preview_arguments_accept_full_season_batch(self):
        operations = [
            {
                "op": "create_directory",
                "parent_path": "/target",
                "name": f"Season-{index:03d}",
            }
            for index in range(85)
        ]
        normalized = change_actions.guangya_fs_change_preview_arguments(
            {"operations": operations, "trigger_strm": False}
        )
        self.assertEqual(len(normalized["operations"]), 85)

    def test_zero_credential_generation_is_a_valid_initial_generation(self):
        client = FakeGatewayClient()
        client.credential_generation = 0
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

        self.assertEqual(plan["credential_generation"], 0)

    def test_plan_rejects_names_shared_by_multiple_operations(self):
        for kind in ("rename", "rename_case", "create_directory", "move", "copy", "relocate", "target_alias", "transient_rename"):
            for reverse in (False, True):
                with self.subTest(kind=kind, reverse=reverse):
                    client = FakeGatewayClient()
                    client.directories["source"].append(GuangYaFile("other", "Other", True, parent_id="source", etag="other"))
                    client.directories["other"] = [GuangYaFile("incoming", "Shared.mp4", False, parent_id="other", size=123, etag="incoming")]
                    observed = self._query(client, operation="tree")
                    refs = {item["object_name"]: item["object_ref"] for item in observed.data["entries"]}
                    observation = guangya_workspace.load_directory_observation(observed.data["observation_ref"], owner="owner")
                    first = {"op": "rename", "object_ref": refs["广告-ABC.mp4"], "new_name": "Shared.mp4"}
                    if kind in {"rename", "rename_case"}:
                        second = {"op": "rename", "object_ref": refs["Move.mp4"], "new_name": "SHARED.MP4" if kind == "rename_case" else "Shared.mp4"}
                    elif kind == "create_directory":
                        second = {"op": kind, "parent_path": "/SOURCE", "name": "Shared.mp4"}
                    elif kind == "relocate":
                        second = {"op": kind, "object_ref": refs["Move.mp4"], "target_path": "/SOURCE", "new_name": "Shared.mp4"}
                    elif kind == "transient_rename":
                        # 反向顺序会先移走临时名称，是合法计划，另有正向用例覆盖。
                        if reverse:
                            continue
                        second = {"op": "relocate", "object_ref": refs["Move.mp4"], "target_path": "/target", "new_name": "Shared.mp4"}
                    elif kind == "target_alias":
                        first.update(op="relocate", target_path="/target")
                        second = {"op": "relocate", "object_ref": refs["Move.mp4"], "target_path": "/TARGET//", "new_name": "Shared.mp4"}
                    else:
                        second = {"op": kind, "object_ref": refs["Shared.mp4"], "target_path": "/SOURCE"}
                    operations = [second, first] if reverse else [first, second]
                    before = deepcopy(client.directories)
                    with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, "重复名称"):
                        guangya_fs_change.build_fs_change_plan(
                            client, owner="owner", observation=observation,
                            operations=operations, trigger_strm=False,
                        )
                    self.assertEqual(client.directories, before)
                    self.assertEqual(list(self.plan_dir.glob("*.json")), [], "冲突计划不能落成可确认计划")

    def test_same_planned_name_in_distinct_directories_remains_valid(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {item["object_name"]: item["object_ref"] for item in observed.data["entries"]}
        observation = guangya_workspace.load_directory_observation(observed.data["observation_ref"], owner="owner")
        plan = guangya_fs_change.build_fs_change_plan(
            client, owner="owner", observation=observation, trigger_strm=False,
            operations=[
                {"op": "relocate", "object_ref": refs["Move.mp4"], "new_name": "Shared.mp4", "target_path": "/target"},
                {"op": "rename", "object_ref": refs["广告-ABC.mp4"], "new_name": "Shared.mp4"},
            ],
        )
        guangya_fs_change.confirm_fs_change_plan(plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"])
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
        self.assertFalse(result["partial"])
        self.assertEqual(client.file_info("rename").name, "Shared.mp4")
        self.assertEqual(client.file_info("move").name, "Shared.mp4")
        self.assertEqual(client.file_info("move").parent_id, "target")

    def test_created_destinations_under_distinct_case_sensitive_parents_do_not_collide(self):
        client = FakeGatewayClient()
        client.directories["0"].append(GuangYaFile("other-target", "TARGET", True, parent_id="0", etag="other-target"))
        client.directories["other-target"] = []
        observed = self._query(client)
        refs = {item["object_name"]: item["object_ref"] for item in observed.data["entries"]}
        observation = guangya_workspace.load_directory_observation(observed.data["observation_ref"], owner="owner")
        plan = guangya_fs_change.build_fs_change_plan(
            client, owner="owner", observation=observation, trigger_strm=False,
            operations=[
                {"op": "create_directory", "parent_path": "/target", "name": "New"},
                {"op": "create_directory", "parent_path": "/TARGET", "name": "New"},
                {"op": "relocate", "object_ref": refs["广告-ABC.mp4"], "target_path": "/target/New", "new_name": "Shared.mp4"},
                {"op": "relocate", "object_ref": refs["Move.mp4"], "target_path": "/TARGET/New", "new_name": "Shared.mp4"},
            ],
        )
        guangya_fs_change.confirm_fs_change_plan(plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"])
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["relocated"], 2)
        self.assertNotEqual(client.file_info("rename").parent_id, client.file_info("move").parent_id)

    def test_relocate_checks_new_source_name_conflict_before_any_write(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {
            "op": "relocate", "source_name": "Move.mp4",
            "new_name": "Shared.mp4", "target_path": "/target",
        })
        client.directories["source"].append(GuangYaFile("external", "Shared.mp4", False, parent_id="source", etag="external"))
        before = deepcopy(client.directories)
        with mock.patch.object(client, "rename", wraps=client.rename) as rename:
            with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
            rename.assert_not_called()
        self.assertEqual(client.directories, before)

    def test_old_signed_conflicting_rename_plan_stops_before_any_write(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {item["object_name"]: item["object_ref"] for item in observed.data["entries"]}
        observation = guangya_workspace.load_directory_observation(observed.data["observation_ref"], owner="owner")
        plan = guangya_fs_change.build_fs_change_plan(
            client, owner="owner", observation=observation, trigger_strm=False,
            operations=[
                {"op": "rename", "object_ref": refs["广告-ABC.mp4"], "new_name": "Shared.mp4"},
                {"op": "rename", "object_ref": refs["Move.mp4"], "new_name": "Distinct.mp4"},
            ],
        )
        # 构造旧编译器允许保存的有效签名v1计划，不绕过实际执行器的任何校验。
        plan["operations"][1]["new_name"] = "SHARED.MP4"
        plan["fingerprint"] = guangya_fs_change._fingerprint(plan)
        guangya_fs_change._atomic_write(plan)
        guangya_fs_change.confirm_fs_change_plan(plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"])
        before = deepcopy(client.directories)
        with mock.patch.object(client, "rename", wraps=client.rename) as rename:
            with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, "重复名称"):
                guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
            rename.assert_not_called()
        self.assertEqual(client.directories, before)

    def test_nested_directories_and_batch_relocate_share_one_frozen_plan(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {item["object_name"]: item["object_ref"] for item in observed.data["entries"]}
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        operations = [
            {"op": "batch_relocate", "items": [
                {"object_ref": refs["广告-ABC.mp4"], "episode": 1},
                {"object_ref": refs["Move.mp4"], "episode": 2},
            ], "target_path": "/target/Series/Season 01", "title": "Series", "season": 1, "naming": "season_episode"},
            {"op": "create_directory", "parent_path": "/target/Series", "name": "Season 01"},
            {"op": "create_directory", "parent_path": "/target", "name": "Series"},
        ]
        normalized = change_actions.guangya_fs_change_preview_arguments({"operations": operations})
        plan = guangya_fs_change.build_fs_change_plan(
            client, owner="owner", observation=observation,
            operations=normalized["operations"], trigger_strm=False,
        )
        self.assertEqual(client.directories["target"], [], "预览不能创建目录或移动文件")
        self.assertEqual(plan["stats"]["create_directory"], 2)
        self.assertEqual(plan["stats"]["relocate"], 2)
        self.assertEqual([item["name"] for item in plan["operations"][:2]], ["Series", "Season 01"])
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        payload = self._queued_payload(plan)
        result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: client)
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["created"], 2)
        self.assertEqual(result["stats"]["relocated"], 2)
        series = client.directories["target"][0]
        season = client.directories[series.file_id][0]
        self.assertEqual(season.name, "Season 01")
        self.assertEqual({item.name for item in client.directories[season.file_id]},
                         {"Series - S01E01.mp4", "Series - S01E02.mp4"})
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: client)

    def test_failed_parent_creation_blocks_nested_create_and_move(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        reference = observed.data["entries"][0]["object_ref"]
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        plan = guangya_fs_change.build_fs_change_plan(
            client, owner="owner", observation=observation, trigger_strm=False,
            operations=[
                {"op": "create_directory", "parent_path": "/target", "name": "Series"},
                {"op": "create_directory", "parent_path": "/target/Series", "name": "Season 01"},
                {"op": "move", "object_ref": reference, "target_path": "/target/Series/Season 01"},
            ],
        )
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        before = deepcopy(client.directories)
        with mock.patch.object(client, "create_dir", side_effect=GuangYaWriteRejected("拒绝", code="denied")) as create:
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(result["stats"]["failed"], 3)
        self.assertEqual(result["stats"]["precondition_failed"], 2)
        self.assertEqual(client.directories, before)

    def test_changed_created_ancestor_blocks_remaining_dependent_writes(self):
        for change_after in (1, 2):
            with self.subTest(change_after=change_after):
                client = FakeGatewayClient()
                observed = self._query(client)
                reference = next(item["object_ref"] for item in observed.data["entries"] if item["object_name"] == "Move.mp4")
                observation = guangya_workspace.load_directory_observation(
                    observed.data["observation_ref"], owner="owner"
                )
                plan = guangya_fs_change.build_fs_change_plan(
                    client, owner="owner", observation=observation, trigger_strm=False,
                    operations=[
                        {"op": "create_directory", "parent_path": "/target", "name": "Series"},
                        {"op": "create_directory", "parent_path": "/target/Series", "name": "Season 01"},
                        {"op": "move", "object_ref": reference, "target_path": "/target/Series/Season 01"},
                    ],
                )
                guangya_fs_change.confirm_fs_change_plan(
                    plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
                )
                preflight = guangya_fs_change._preflight_operation
                changed = False

                def change_ancestor(client, item, **kwargs):
                    nonlocal changed
                    created = kwargs.get("created_targets")
                    if created is not None and len(created) == change_after and not changed:
                        changed = True
                        ancestor = client.directories["target"][0]
                        client.rename(ancestor.file_id, "ChangedByOtherActor")
                        client.move([ancestor.file_id], "source")
                    return preflight(client, item, **kwargs)

                with mock.patch.object(guangya_fs_change, "_preflight_operation", side_effect=change_ancestor):
                    result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
                self.assertTrue(changed)
                self.assertEqual(result["stats"]["created"], change_after)
                self.assertEqual(result["stats"]["moved"], 0)
                self.assertEqual(result["stats"]["precondition_failed"], 3 - change_after)
                self.assertEqual(client.file_info("move").parent_id, "source")

    def test_nested_create_orders_root_parent_before_reversed_children(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        plan = guangya_fs_change.build_fs_change_plan(
            client, owner="owner", observation=observation, trigger_strm=False,
            operations=[
                {"op": "create_directory", "parent_path": "/Library/Series", "name": "Season 01"},
                {"op": "create_directory", "parent_path": "/Library", "name": "Series"},
                {"op": "create_directory", "parent_path": "/", "name": "Library"},
            ],
        )
        self.assertEqual([item["name"] for item in plan["operations"]], ["Library", "Series", "Season 01"])
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
        self.assertEqual(result["stats"]["created"], 3)
        self.assertEqual(result["stats"]["failed"], 0)

    def test_created_subdirectory_conflicts_with_incoming_same_name(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {item["object_name"]: item["object_ref"] for item in observed.data["entries"]}
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, "重复名称"):
            guangya_fs_change.build_fs_change_plan(
                client, owner="owner", observation=observation, trigger_strm=False,
                operations=[
                    {"op": "create_directory", "parent_path": "/target", "name": "Series"},
                    {"op": "create_directory", "parent_path": "/target/Series", "name": "Move.mp4"},
                    {"op": "move", "object_ref": refs["Move.mp4"], "target_path": "/target/Series"},
                ],
            )
        self.assertEqual(client.directories["target"], [])

    def test_batch_relocate_can_target_directory_created_in_same_plan(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        refs = {
            item["object_name"]: item["object_ref"] for item in observed.data["entries"]
        }
        normalized = change_actions.guangya_fs_change_preview_arguments(
            {
                "operations": [
                    {
                        "op": "batch_relocate",
                        "items": [
                            {"object_ref": refs["广告-ABC.mp4"], "episode": 1},
                            {"object_ref": refs["Move.mp4"], "episode": 2},
                        ],
                        "target_path": "/target/Series",
                        "title": "Series",
                        "naming": "absolute",
                        "episode_padding": 2,
                    },
                    {
                        "op": "create_directory",
                        "parent_path": "/target",
                        "name": "Series",
                    },
                ],
                "trigger_strm": False,
            }
        )
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        plan = guangya_fs_change.build_fs_change_plan(
            client,
            owner="owner",
            observation=observation,
            operations=normalized["operations"],
            trigger_strm=False,
        )

        self.assertEqual(plan["stats"]["total"], 3)
        self.assertEqual(plan["stats"]["create_directory"], 1)
        self.assertEqual(plan["stats"]["relocate"], 2)
        self.assertEqual(plan["operations"][0]["op"], "create_directory")
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        result = guangya_fs_change.execute_fs_change_plan(
            self._queued_payload(plan), client_factory=lambda: client
        )

        self.assertFalse(result["partial"])
        created = next(
            item for item in client.directories["target"] if item.name == "Series"
        )
        self.assertEqual(
            sorted(item.name for item in client.directories[created.file_id]),
            ["Series - E01.mp4", "Series - E02.mp4"],
        )

    def test_batch_and_create_accept_compact_model_arguments(self):
        normalized = change_actions.guangya_fs_change_preview_arguments(
            {
                "operations": [
                    {"op": "create_directory", "path": "/动漫/Series"},
                    {
                        "op": "batch_relocate",
                        "items": [
                            {
                                "object_ref": "OBJ" + "A" * 24,
                                "episode": 1,
                            }
                        ],
                        "target_path": "/动漫/Series",
                    },
                ]
            }
        )

        self.assertEqual(
            normalized["operations"][0],
            {
                "op": "create_directory",
                "parent_path": "/动漫",
                "name": "Series",
            },
        )
        self.assertEqual(normalized["operations"][1]["title"], "Series")
        self.assertEqual(normalized["operations"][1]["naming"], "absolute")

    def _folder_move_with_child_renames(self, *, count=1, parent_op="move"):
        client = FakeGatewayClient()
        client.directories["source"] = []
        for index in range(count):
            folder, video = f"folder-{index}", f"video-{index}"
            client.directories["source"].append(GuangYaFile(folder, f"Code-{index}", True, parent_id="source", etag="before"))
            client.directories[folder] = [
                GuangYaFile(video, f"site.example@Code-{index}.mp4", False, parent_id=folder, size=100, etag="content"),
                GuangYaFile(f"poster-{index}", "poster.jpg", False, parent_id=folder, size=5, etag="poster"),
            ]
        observed = self._query(client, operation="tree", page_size=50)
        entries = {item["object_name"]: item["object_ref"] for item in observed.data["entries"]}
        operations = []
        for index in range(count):
            # 模型无须自行调度执行顺序，编译器应保证子文件改名完成后才搬目录。
            operations.append({"op": parent_op, "object_ref": entries[f"Code-{index}"], **({"target_path": "/target"} if parent_op == "move" else {})})
            operations.append({"op": "rename", "object_ref": entries[f"site.example@Code-{index}.mp4"], "new_name": f"Code-{index}.mp4"})
        context = ToolContext(owner="owner", session_id="session")
        with mock.patch.object(change_actions, "GuangYaClient", return_value=client):
            preview = change_actions.preview_guangya_fs_change(change_actions.guangya_fs_change_preview_arguments({
                "observation_ref": observed.data["observation_ref"], "operations": operations, "trigger_strm": False,
            }), context)
            confirmation, _fingerprint = change_actions.prepare_guangya_fs_change_confirmation({}, context)
        self.assertIn(f"改名 {count} 项", preview.summary)
        self.assertIn(f"移动 {count} 项", confirmation.summary)
        plan = guangya_fs_change._read(change_actions._flow("owner").plan_id)
        guangya_fs_change.confirm_fs_change_plan(plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"])
        return client, plan

    def test_clean_children_then_move_ten_original_folders_in_one_plan(self):
        client, plan = self._folder_move_with_child_renames(count=10)
        self.assertEqual(plan["stats"]["rename"], 10)
        self.assertEqual(plan["stats"]["move"], 10)
        self.assertEqual([item["op"] for item in plan["operations"]], ["rename"] * 10 + ["move"] * 10)
        original_rename = client.rename

        def rename(file_id, new_name):
            original_rename(file_id, new_name)
            # 真实云盘在修改子文件后会更新父目录etag/更新时间，不能误报外部漂移。
            folder = next(item for item in client.directories["source"] if item.file_id == client.file_info(file_id).parent_id)
            folder.etag = "after-own-rename"
            folder.updated_at += 1
            return True

        with mock.patch.object(client, "rename", side_effect=rename):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["renamed"], 10)
        self.assertEqual(result["stats"]["moved"], 10)
        self.assertEqual(client.directories["source"], [])
        self.assertEqual(len(client.directories["target"]), 10)
        for index in range(10):
            self.assertEqual(client.file_info(f"folder-{index}").parent_id, "target")
            self.assertEqual(client.file_info(f"video-{index}").name, f"Code-{index}.mp4")
            self.assertEqual(client.file_info(f"video-{index}").parent_id, f"folder-{index}")
            self.assertEqual(client.file_info(f"poster-{index}").parent_id, f"folder-{index}")

    def test_failed_child_rename_never_moves_its_parent(self):
        client, plan = self._folder_move_with_child_renames(count=2)
        original_rename = client.rename

        def rename(file_id, new_name):
            if file_id == "video-0":
                raise GuangYaWriteRejected("rename", code="rejected")
            return original_rename(file_id, new_name)

        with mock.patch.object(client, "rename", side_effect=rename):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
        self.assertTrue(result["partial"])
        self.assertEqual(result["stats"]["renamed"], 1)
        self.assertEqual(result["stats"]["moved"], 1)
        self.assertEqual(client.file_info("folder-0").parent_id, "source")
        self.assertEqual(client.file_info("video-0").name, "site.example@Code-0.mp4")
        self.assertEqual(client.file_info("folder-1").parent_id, "target")

    def test_child_rename_does_not_allow_parent_trash(self):
        with self.assertRaisesRegex(AgentToolError, "父目录"):
            self._folder_move_with_child_renames(parent_op="trash")

    def test_cancel_after_child_clean_keeps_parent_and_prevents_replay(self):
        client, plan = self._folder_move_with_child_renames()
        payload = self._queued_payload(plan)

        def cancel():
            if client.file_info("video-0").name == "Code-0.mp4":
                raise RuntimeError("cancelled")

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: client, cancel_check=cancel)
        self.assertEqual(client.file_info("folder-0").parent_id, "source")
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: client)
        self.assertEqual(client.file_info("folder-0").parent_id, "source")

    def test_copy_scope_uses_frozen_operation_counts(self):
        self.assertEqual(change_actions._change_summary({"total": 2, "copy": 2}), "复制 2 项")
        # 展示文案从计划计数取得，不增改历史 preview_safe 参与签名的字段。
        self.assertNotIn("copy_count", change_actions._safe_preview({"total": 2, "sample_changes": []}))

    def test_own_child_rename_does_not_mask_external_parent_rename(self):
        client, plan = self._folder_move_with_child_renames()
        original_rename = client.rename

        def rename(file_id, new_name):
            original_rename(file_id, new_name)
            client.directories["source"][0].name = "externally-changed"
            return True

        with mock.patch.object(client, "rename", side_effect=rename):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
        self.assertTrue(result["partial"])
        self.assertEqual(result["stats"]["moved"], 0)
        self.assertEqual(client.file_info("folder-0").parent_id, "source")

    def test_relocate_with_correct_name_moves_without_rejected_noop_rename(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        target = next(item for item in observed.data["entries"] if item["object_name"] == "Move.mp4")
        observation = guangya_workspace.load_directory_observation(observed.data["observation_ref"], owner="owner")
        plan = guangya_fs_change.build_fs_change_plan(
            client, owner="owner", observation=observation, trigger_strm=False,
            operations=[{"op": "relocate", "object_ref": target["object_ref"], "target_path": "/target", "new_name": "Move.mp4"}],
        )
        guangya_fs_change.confirm_fs_change_plan(plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"])
        with mock.patch.object(client, "rename", side_effect=GuangYaWriteRejected("rename", code="160")) as rename:
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["relocated"], 1)
        self.assertEqual(result["stats"]["failed"], 0)
        rename.assert_not_called()
        self.assertEqual(client.file_info("move").parent_id, "target")
        self.assertEqual(client.file_info("move").name, "Move.mp4")

    def test_relocate_combines_move_and_rename_for_one_observed_object(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        target = next(
            item
            for item in observed.data["entries"]
            if item["object_name"] == "Move.mp4"
        )
        normalized = change_actions.guangya_fs_change_preview_arguments(
            {
                "operations": [
                    {
                        "op": "relocate",
                        "object_ref": target["object_ref"],
                        "target_path": "/target",
                        "new_name": "Series.S01E01.mp4",
                    }
                ],
                "trigger_strm": False,
            }
        )
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        plan = guangya_fs_change.build_fs_change_plan(
            client,
            owner="owner",
            observation=observation,
            trigger_strm=False,
            operations=normalized["operations"],
        )
        self.assertEqual(plan["stats"]["relocate"], 1)
        self.assertIn("移动并改名", plan["samples"][0])
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        result = guangya_fs_change.execute_fs_change_plan(
            self._queued_payload(plan), client_factory=lambda: client
        )
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["relocated"], 1)
        self.assertEqual(
            [item.name for item in client.directories["target"]],
            ["Series.S01E01.mp4"],
        )
        self.assertIsNone(
            next(
                (
                    item
                    for item in client.directories["source"]
                    if item.file_id == "move"
                ),
                None,
            )
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
            self._queued_payload(plan), client_factory=lambda: client
        )
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["renamed"], 1)
        self.assertEqual(result["stats"]["moved"], 1)
        self.assertEqual(result["stats"]["trashed"], 1)
        self.assertEqual(result["stats"]["created"], 1)
        audits = db.list_organize_delete_audits()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["status"], "success")
        self.assertEqual(audits[0]["file_id"], "trash")
        self.assertEqual(
            [item.name for item in client.directories["source"]], ["ABC.mp4"]
        )
        self.assertEqual(
            {item.name for item in client.directories["target"]}, {"Move.mp4", "新目录"}
        )

    def test_child_backup_and_parent_relocation_share_one_confirmed_plan(self):
        for copy_fails in (False, True):
            with self.subTest(copy_fails=copy_fails):
                client = FakeGatewayClient()
                observed = self._query(client, operation="tree", path="/", page_size=50)
                observation = guangya_workspace.load_directory_observation(
                    observed.data["observation_ref"], owner="owner"
                )
                refs = {item["object_name"]: item["object_ref"] for item in observed.data["entries"]}
                plan = guangya_fs_change.build_fs_change_plan(
                    client, owner="owner", observation=observation, trigger_strm=False,
                    operations=[
                        {"op": "relocate", "object_ref": refs["source"],
                         "target_path": "/target", "new_name": "archived"},
                        {"op": "copy", "object_ref": refs["Move.mp4"], "target_path": "/target"},
                    ],
                )
                self.assertEqual([item["op"] for item in plan["operations"]], ["copy", "relocate"])
                self.assertEqual(plan["operations"][1]["rename_dependencies"], ["move"])
                self.assertEqual(client.file_info("source").parent_id, "0", "预览不得写入")
                guangya_fs_change.confirm_fs_change_plan(
                    plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
                )
                payload = self._queued_payload(plan)
                with mock.patch.object(client, "copy", wraps=client.copy) as copy, \
                        mock.patch.object(client, "move", wraps=client.move) as move:
                    if copy_fails:
                        copy.side_effect = GuangYaWriteRejected("copy", code="rejected")
                    result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: client)
                    copy.assert_called_once_with(["move"], "target")
                    self.assertEqual(result["partial"], copy_fails)
                    self.assertEqual(result["stats"]["copied"], int(not copy_fails))
                    self.assertEqual(result["stats"]["relocated"], int(not copy_fails))
                    self.assertEqual(client.file_info("move").parent_id, "source", "复制必须保留源文件")
                    if copy_fails:
                        move.assert_not_called()
                        self.assertEqual(client.file_info("source").name, "source")
                        self.assertEqual(client.file_info("source").parent_id, "0")
                        self.assertEqual(result["stats"]["precondition_failed"], 1)
                    else:
                        move.assert_called_once_with(["source"], "target")
                        self.assertEqual(client.file_info("source").name, "archived")
                        self.assertEqual(client.file_info("source").parent_id, "target")
                        copied = next(row for row in client.list_dir("target") if row.name == "Move.mp4")
                        self.assertNotEqual(copied.file_id, "move")
                        self.assertEqual(copied.size, client.file_info("move").size)
                    with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
                        guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: client)
                    copy.assert_called_once()

    def test_child_backup_does_not_authorize_trashing_nonempty_parent(self):
        client = FakeGatewayClient()
        observed = self._query(client, operation="tree", path="/", page_size=50)
        observation = guangya_workspace.load_directory_observation(
            observed.data["observation_ref"], owner="owner"
        )
        refs = {item["object_name"]: item["object_ref"] for item in observed.data["entries"]}
        with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, "先移出内容"):
            guangya_fs_change.build_fs_change_plan(
                client, owner="owner", observation=observation, trigger_strm=False,
                operations=[
                    {"op": "trash", "object_ref": refs["source"]},
                    {"op": "copy", "object_ref": refs["Move.mp4"], "target_path": "/target"},
                ],
            )
        self.assertEqual(client.file_info("source").parent_id, "0")
        self.assertEqual(client.list_dir("target"), [])

    def test_copy_keeps_source_and_verifies_new_target_object(self):
        client = FakeGatewayClient()
        observed = self._query(client)
        source_ref = next(
            item["object_ref"]
            for item in observed.data["entries"]
            if item["object_name"] == "Move.mp4"
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
                    "op": "copy",
                    "object_ref": source_ref,
                    "target_path": "/target",
                }
            ],
        )
        self.assertEqual(plan["stats"]["copy"], 1)
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )

        result = guangya_fs_change.execute_fs_change_plan(
            self._queued_payload(plan), client_factory=lambda: client
        )

        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["copied"], 1)
        self.assertIsNotNone(client.file_info("move"))
        self.assertEqual(
            [item.name for item in client.directories["target"]], ["Move.mp4"]
        )

    def test_move_waits_for_directory_visibility_without_repeating_write(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {
            "op": "move", "source_name": "Move.mp4", "target_path": "/target",
        })
        original_list = client.list_dir
        original_move = client.move
        pending = []
        reads = 0

        def accept_move(ids, parent):
            pending.append((ids, parent))
            return True

        def delayed_list(parent_id="0"):
            nonlocal reads
            if pending and parent_id == "target":
                reads += 1
                if reads == 3:
                    original_move(*pending[0])
            return original_list(parent_id)

        with mock.patch.object(client, "move", side_effect=accept_move) as write, \
                mock.patch.object(client, "list_dir", side_effect=delayed_list), \
                mock.patch("app.clients.guangya.sleep"):
            result = guangya_fs_change.execute_fs_change_plan(
                self._queued_payload(plan), client_factory=lambda: client
            )
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["moved"], 1)
        write.assert_called_once()
        self.assertGreaterEqual(reads, 3)

    def test_copy_target_does_not_prove_source_was_retained(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {
            "op": "copy", "source_name": "Move.mp4", "target_path": "/target",
        })
        client.copy(["move"], "target")
        client.delete(["move"])
        self.assertFalse(guangya_fs_change._verify_after(
            client, plan["operations"][0], ""
        ))

    def test_rename_unknown_write_outcome_requires_manual_review(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {
            "op": "rename", "source_name": "广告-ABC.mp4", "new_name": "ABC.mp4",
        })
        original_rename = client.rename

        def disconnected(fid, name):
            original_rename(fid, name)
            raise TimeoutError("response lost after accepted write")

        with mock.patch.object(client, "rename", side_effect=disconnected) as write, \
                mock.patch.object(guangya_fs_change, "_verify_after", side_effect=OSError("readback unavailable")):
            result = guangya_fs_change.execute_fs_change_plan(
                self._queued_payload(plan), client_factory=lambda: client
            )
        self.assertTrue(result["partial"])
        self.assertTrue(result["requires_manual"])
        self.assertEqual(guangya_fs_change._read(plan["plan_id"])["status"], "manual_review")
        write.assert_called_once()

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
                self._queued_payload(plan), client_factory=lambda: client
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
                self._queued_payload(plan), client_factory=lambda: client
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
        guangya_fs_change.confirm_fs_change_plan(
            plan["plan_id"], owner="owner", expected_fingerprint=plan["fingerprint"]
        )
        payload = self._queued_payload(plan)
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
            self._queued_payload(plan), client_factory=lambda: client
        )
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["moved"], 2)

    def test_plan_state_cas_allows_only_one_concurrent_execution_claim(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(
            client,
            {"op": "rename", "source_name": "广告-ABC.mp4", "new_name": "ABC.mp4"},
        )
        payload = self._queued_payload(plan)
        barrier = threading.Barrier(3)
        claimed: list[bool] = []

        def claim() -> None:
            barrier.wait()
            try:
                guangya_fs_change.update_fs_change_plan_execution(
                    plan["plan_id"],
                    status="running",
                    execution={"started_at": "now"},
                    expected_statuses={"queued"},
                    expected_job_id=payload["job_id"],
                )
            except guangya_fs_change.GuangYaFSChangeStale:
                claimed.append(False)
            else:
                claimed.append(True)

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(sorted(claimed), [False, True])
        self.assertEqual(guangya_fs_change._read(plan["plan_id"])["status"], "running")

    def test_confirmed_plan_cannot_execute_without_bound_durable_job(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(
            client,
            {"op": "rename", "source_name": "广告-ABC.mp4", "new_name": "ABC.mp4"},
        )
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeError):
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
        self.assertIsNotNone(client.file_info("rename"))
        self.assertEqual(
            guangya_fs_change._read(plan["plan_id"])["status"], "confirmed"
        )

    def test_queued_execution_uses_bound_job_id_after_confirmation_ttl(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(
            client,
            {"op": "rename", "source_name": "广告-ABC.mp4", "new_name": "ABC.mp4"},
        )
        job_id = "a" * 32
        guangya_fs_change.bind_fs_change_plan_job(
            plan["plan_id"],
            owner_digest="digest:owner",
            expected_fingerprint=plan["fingerprint"],
            job_id=job_id,
            queue_until_epoch=10**12,
        )
        queued = guangya_fs_change._read(plan["plan_id"])
        queued["execute_until_epoch"] = 1
        guangya_fs_change._atomic_write(queued)
        base_payload = {
            "version": 1,
            "plan_id": plan["plan_id"],
            "plan_fingerprint": plan["fingerprint"],
            "owner_digest": "digest:owner",
            "credential_generation": 31,
        }
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(
                {**base_payload, "job_id": "b" * 32}, client_factory=lambda: client
            )
        self.assertIsNotNone(client.file_info("rename"))
        result = guangya_fs_change.execute_fs_change_plan(
            {**base_payload, "job_id": job_id}, client_factory=lambda: client
        )
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["renamed"], 1)
        terminal = guangya_fs_change._read(plan["plan_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["job_id"], job_id)

    def test_trash_execution_uses_audited_recycle_bin_writer(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(client, {"op": "trash", "source_name": "垃圾残余"})

        def audited_delete(audit_client, *, candidate, **_kwargs):
            audit_client.delete([candidate.file_id])
            return {"audit_id": 1, "status": "success"}

        with mock.patch.object(
            guangya_fs_change, "execute_recycle_bin_delete", side_effect=audited_delete
        ) as audited:
            result = guangya_fs_change.execute_fs_change_plan(
                self._queued_payload(plan), client_factory=lambda: client
            )
        self.assertFalse(result["partial"])
        self.assertEqual(result["stats"]["trashed"], 1)
        self.assertEqual(audited.call_args.kwargs["candidate"].file_id, "trash")
        self.assertEqual(audited.call_args.kwargs["trigger"], "agent_guangya_fs_change")

    def test_journal_failure_after_remote_write_returns_manual_partial(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(
            client,
            {"op": "rename", "source_name": "广告-ABC.mp4", "new_name": "ABC.mp4"},
        )
        original_append = guangya_fs_change._append_journal
        calls = 0

        def flaky_append(plan_id, event):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("journal unavailable")
            return original_append(plan_id, event)

        with mock.patch.object(
            guangya_fs_change, "_append_journal", side_effect=flaky_append
        ):
            result = guangya_fs_change.execute_fs_change_plan(
                self._queued_payload(plan), client_factory=lambda: client
            )
        self.assertTrue(result["partial"])
        self.assertTrue(result["requires_manual"])
        self.assertEqual(result["stats"]["renamed"], 1)
        self.assertGreaterEqual(result["stats"]["audit_failures"], 1)
        self.assertEqual(
            guangya_fs_change._read(plan["plan_id"])["status"], "manual_review"
        )

    def test_terminal_plan_write_failure_does_not_raise_retryable_failure(self):
        client = FakeGatewayClient()
        plan = self._confirmed_plan(
            client,
            {"op": "rename", "source_name": "广告-ABC.mp4", "new_name": "ABC.mp4"},
        )
        original_update = guangya_fs_change.update_fs_change_plan_execution

        def flaky_update(plan_id, *, status, execution, **kwargs):
            if status != "running":
                raise OSError("plan store unavailable")
            return original_update(
                plan_id, status=status, execution=execution, **kwargs
            )

        with mock.patch.object(
            guangya_fs_change,
            "update_fs_change_plan_execution",
            side_effect=flaky_update,
        ):
            result = guangya_fs_change.execute_fs_change_plan(
                self._queued_payload(plan), client_factory=lambda: client
            )
        self.assertTrue(result["partial"])
        self.assertTrue(result["requires_manual"])
        self.assertEqual(result["stats"]["renamed"], 1)
        self.assertEqual(guangya_fs_change._read(plan["plan_id"])["status"], "running")

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
        self.assertRegex(accepted.data["operation_ref"], "^GY-")
        self.assertEqual(
            manager.start_durable_operation.call_args.kwargs["job_kind"],
            "agent_guangya_fs_change",
        )

    def test_post_enqueue_manager_error_recovers_the_already_active_job(self):
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
            change_actions.preview_guangya_fs_change(arguments, context)
            _confirmation, fingerprint = (
                change_actions.prepare_guangya_fs_change_confirmation({}, context)
            )
        job_id = "c" * 32
        manager = mock.Mock()

        def enqueue_then_report_local_error(*_args, **kwargs):
            payload = kwargs["payload"]
            guangya_fs_change.bind_fs_change_plan_job(
                payload["plan_id"],
                owner_digest=payload["owner_digest"],
                expected_fingerprint=payload["plan_fingerprint"],
                job_id=job_id,
                queue_until_epoch=10**12,
            )
            return {"ok": False, "error_code": "durable_queue_claim_failed"}

        manager.start_durable_operation.side_effect = enqueue_then_report_local_error
        with (
            mock.patch(
                "app.modules.organize_tasks.get_organize_manager", return_value=manager
            ),
            mock.patch.object(
                change_actions,
                "get_organize_operation_job",
                return_value={"status": "pending"},
            ),
            mock.patch.object(
                change_actions, "organize_operation_queue_position", return_value=2
            ),
        ):
            accepted = change_actions.execute_guangya_fs_change_confirmed(
                {}, fingerprint, context
            )
        self.assertEqual(accepted.status, "accepted")
        self.assertTrue(accepted.data["queued"])
        self.assertTrue(accepted.data["replayed"])
        self.assertEqual(accepted.data["queue_position"], 2)

    def test_session_clear_discards_owner_observations_only(self):
        client = FakeGatewayClient()
        owner_result = self._query(client)
        arguments = workspace_actions.guangya_fs_query_arguments(
            {
                "operation": "list",
                "path": "/source",
                "page": 1,
                "page_size": 10,
                "max_items": 100,
            }
        )
        with mock.patch.object(workspace_actions, "GuangYaClient", return_value=client):
            other_result = workspace_actions.query_guangya_filesystem(
                arguments, ToolContext(owner="other", session_id="session")
            )
        removed = workspace_actions.clear_guangya_workspace_context(owner="owner")
        self.assertEqual(removed, 1)
        with self.assertRaises(guangya_workspace.GuangYaWorkspaceError):
            guangya_workspace.load_directory_observation(
                owner_result.data["observation_ref"], owner="owner"
            )
        other = guangya_workspace.load_directory_observation(
            other_result.data["observation_ref"], owner="other"
        )
        self.assertEqual(other["owner_digest"], "digest:other")

    def test_persisted_context_is_authoritative_over_stale_memory_cache(self):

        class EmptyRepository:
            @staticmethod
            def get_latest(**_kwargs):
                return None

        owner = "owner"
        workspace_actions._flows[owner] = object()
        change_actions._flows[owner] = object()
        workspace_actions.configure_guangya_workspace_context(EmptyRepository())
        change_actions.configure_guangya_fs_change_context(EmptyRepository())
        self.assertEqual(workspace_actions.latest_guangya_observation_ref(owner), "")
        self.assertIsNone(change_actions._flow(owner))
        self.assertNotIn(owner, workspace_actions._flows)
        self.assertNotIn(owner, change_actions._flows)

    def _nested_archive_plan(self, *, extra_file=False):
        class Client(FakeGatewayClient):
            def touch(self, *ids):
                for entries in self.directories.values():
                    for item in entries:
                        if item.file_id in ids:
                            item.etag += '-changed'
                            item.updated_at += 1

            def move(self, file_ids, parent_id):
                parents = [self.file_info(file_id).parent_id for file_id in file_ids]
                result = super().move(file_ids, parent_id)
                self.touch(*parents, parent_id)
                return result

            def delete_empty_directory(self, file_id, *, expected_etag='', expected_updated_at=0):
                current = self.file_info(file_id)
                if current is None or current.etag != expected_etag or self.list_dir(file_id):
                    raise RuntimeError('目录已变化，保留')
                result = super().delete([file_id])
                self.touch(current.parent_id)
                return result

        client = Client()
        client.directories['source'] = [
            GuangYaFile('work', 'Work-A', True, parent_id='source', etag='work'),
            GuangYaFile('peer', 'Work-B', True, parent_id='source', etag='peer'),
        ]
        client.directories['work'] = [GuangYaFile('mid', 'pack', True, parent_id='work', etag='mid')]
        client.directories['mid'] = [GuangYaFile('leaf', 'V', True, parent_id='mid', etag='leaf')]
        client.directories['leaf'] = [GuangYaFile('video', 'movie.mp4', False, parent_id='leaf', etag='video', size=100)]
        client.directories['peer'] = [GuangYaFile('peer-video', 'code.mp4', False, parent_id='peer', etag='peer-video', size=200)]
        if extra_file:
            client.directories['leaf'].append(GuangYaFile('poster', 'poster.jpg', False, parent_id='leaf', etag='poster'))
        observed = self._query(client, operation='tree', page_size=50)
        entries = {item['object_name']: item['object_ref'] for item in observed.data['entries']}
        operations = [
            {'op': 'move', 'object_ref': entries['Work-A'], 'target_path': '/target'},
            {'op': 'move', 'object_ref': entries['Work-B'], 'target_path': '/target'},
            {'op': 'trash', 'object_ref': entries['pack']},
            {'op': 'trash', 'object_ref': entries['V']},
            {'op': 'move', 'object_ref': entries['movie.mp4'], 'target_path': '/source/Work-A'},
        ]
        observation = guangya_workspace.load_directory_observation(observed.data['observation_ref'], owner='owner')
        plan = guangya_fs_change.build_fs_change_plan(client, owner='owner', observation=observation,
                                                    operations=operations, trigger_strm=False)
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        return client, plan

    def test_nested_video_extraction_empty_cleanup_and_folder_archival_share_one_plan(self):
        client, plan = self._nested_archive_plan()
        order = [item['source']['file_id'] for item in plan['operations']]
        self.assertLess(order.index('video'), order.index('leaf'))
        self.assertLess(order.index('leaf'), order.index('mid'))
        self.assertLess(order.index('mid'), order.index('work'))
        self.assertTrue(all(item.get('require_empty') for item in plan['operations'] if item['op'] == 'trash'))
        self.assertTrue(client.file_info('leaf'), '预览阶段不能动文件')
        payload = self._queued_payload(plan)
        result = guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: client)
        self.assertFalse(result['partial'])
        self.assertEqual(result['stats']['moved'], 3)
        self.assertEqual(result['stats']['trashed'], 2)
        self.assertEqual(client.file_info('video').parent_id, 'work')
        self.assertEqual(client.file_info('work').parent_id, 'target')
        self.assertEqual(client.file_info('peer').parent_id, 'target')
        self.assertEqual(client.file_info('peer-video').parent_id, 'peer')
        self.assertIsNone(client.file_info('leaf'))
        self.assertIsNone(client.file_info('mid'))
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: client)

    def test_unmoved_companion_blocks_empty_directory_cleanup_at_preview(self):
        with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '未安排移出'):
            self._nested_archive_plan(extra_file=True)

    def test_failed_extraction_blocks_all_dependent_cleanup_and_parent_move(self):
        client, plan = self._nested_archive_plan()
        original_move = client.move
        def move(file_ids, parent_id):
            if 'video' in file_ids:
                raise GuangYaWriteRejected('move', code='rejected')
            return original_move(file_ids, parent_id)
        with mock.patch.object(client, 'move', side_effect=move), mock.patch.object(client, 'delete_empty_directory', side_effect=AssertionError('不得删目录')):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
        self.assertTrue(result['partial'])
        self.assertEqual(result['stats']['moved'], 1, '独立作品仍可以完成')
        self.assertEqual(client.file_info('video').parent_id, 'leaf')
        self.assertEqual(client.file_info('work').parent_id, 'source')
        self.assertTrue(client.file_info('mid'))

    def test_new_child_between_extraction_and_cleanup_is_never_deleted(self):
        client, plan = self._nested_archive_plan()
        original_move = client.move
        def move(file_ids, parent_id):
            result = original_move(file_ids, parent_id)
            if 'video' in file_ids:
                client.directories['leaf'].append(GuangYaFile('new-file', 'new.mp4', False, parent_id='leaf', etag='new'))
            return result
        with mock.patch.object(client, 'move', side_effect=move):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
        self.assertTrue(result['partial'])
        self.assertEqual(result['stats']['trashed'], 0)
        self.assertEqual(client.file_info('new-file').parent_id, 'leaf')
        self.assertEqual(client.file_info('work').parent_id, 'source')
        self.assertEqual(client.file_info('video').parent_id, 'work')

    def test_cancel_after_nested_extraction_cannot_replay_the_plan(self):
        client, plan = self._nested_archive_plan()
        payload = self._queued_payload(plan)
        def cancel():
            if client.file_info('video').parent_id == 'work':
                raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: client, cancel_check=cancel)
        self.assertTrue(client.file_info('mid'))
        self.assertTrue(client.file_info('leaf'))
        self.assertEqual(client.file_info('work').parent_id, 'source')
        with self.assertRaises(guangya_fs_change.GuangYaFSChangeStale):
            guangya_fs_change.execute_fs_change_plan(payload, client_factory=lambda: client)

    def test_operation_field_error_explains_schema_without_silently_dropping_fields(self):
        with self.assertRaisesRegex(AgentToolError, '缺少 target_path；多余 new_name'):
            change_actions.guangya_fs_change_preview_arguments({'operations': [
                {'op': 'move', 'object_ref': 'OBJ' + 'A' * 24, 'new_name': 'wrong'}
            ]})

    def test_noop_rename_does_not_block_move_of_same_file_in_either_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                client = FakeGatewayClient()
                result = self._query(client)
                entry = next(e for e in result.data['entries'] if e['object_name'] == 'Move.mp4')
                observation = guangya_workspace.load_directory_observation(result.data['observation_ref'], owner='owner')
                operations = [{'op': 'rename', 'object_ref': entry['object_ref'], 'new_name': 'Move.mp4'},
                              {'op': 'move', 'object_ref': entry['object_ref'], 'target_path': '/target'}]
                if reverse:
                    operations.reverse()
                plan = guangya_fs_change.build_fs_change_plan(client, owner='owner', observation=observation, operations=operations, trigger_strm=False)
                self.assertEqual(plan['stats']['total'], 1)
                self.assertEqual(plan['stats']['unchanged'], 1)
                self.assertEqual(plan['operations'][0]['op'], 'move')
                guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
                outcome = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
                self.assertFalse(outcome['partial'])
                self.assertEqual(outcome['stats']['moved'], 1)
                self.assertEqual(outcome['stats']['renamed'], 0)
                self.assertEqual(client.file_info('move').parent_id, 'target')

    def test_same_destination_relocate_only_renames_and_noop_move_is_omitted(self):
        client = FakeGatewayClient()
        result = self._query(client)
        entries = {e['object_name']: e['object_ref'] for e in result.data['entries']}
        observation = guangya_workspace.load_directory_observation(result.data['observation_ref'], owner='owner')
        plan = guangya_fs_change.build_fs_change_plan(client, owner='owner', observation=observation, trigger_strm=False, operations=[
            {'op': 'move', 'object_ref': entries['Move.mp4'], 'target_path': '/source'},
            {'op': 'relocate', 'object_ref': entries['广告-ABC.mp4'], 'target_path': '/source', 'new_name': 'ABC.mp4'},
        ])
        self.assertEqual(plan['stats']['unchanged'], 1)
        self.assertEqual(plan['stats']['rename'], 1)
        self.assertEqual(plan['stats']['total'], 1)
        guangya_fs_change.confirm_fs_change_plan(plan['plan_id'], owner='owner', expected_fingerprint=plan['fingerprint'])
        with mock.patch.object(client, 'move', side_effect=AssertionError('原地变更不得调用移动')):
            result = guangya_fs_change.execute_fs_change_plan(self._queued_payload(plan), client_factory=lambda: client)
        self.assertFalse(result['partial'])
        self.assertEqual(client.file_info('rename').name, 'ABC.mp4')
        self.assertEqual(client.file_info('rename').parent_id, 'source')

    def test_all_noops_clear_old_preview_and_never_create_empty_confirmation(self):
        from app.agent.session_context import SQLiteAgentSessionContextRepository
        for persisted in (False, True):
            with self.subTest(persisted=persisted):
                change_actions.reset_guangya_fs_change_context_for_tests()
                if persisted:
                    change_actions.configure_guangya_fs_change_context(SQLiteAgentSessionContextRepository(secret_provider=lambda: 'test-secret'))
                client = FakeGatewayClient()
                result = self._query(client)
                entry = next(e for e in result.data['entries'] if e['object_name'] == 'Move.mp4')
                context = ToolContext(owner='owner', session_id='session')
                args = {'observation_ref': result.data['observation_ref'], 'operations': [
                    {'op': 'rename', 'object_ref': entry['object_ref'], 'new_name': 'Different.mp4'}], 'trigger_strm': False}
                with mock.patch.object(change_actions, 'GuangYaClient', return_value=client):
                    previous = change_actions.preview_guangya_fs_change(change_actions.guangya_fs_change_preview_arguments(args), context)
                    old_plan = change_actions._flow('owner').plan_id
                    self.assertEqual(previous.status, 'ready')
                    before = set(self.plan_dir.glob('*.json'))
                    args['operations'] = [
                        {'op': 'rename', 'object_ref': entry['object_ref'], 'new_name': 'Move.mp4'},
                        {'op': 'move', 'object_ref': entry['object_ref'], 'target_path': '/source'},
                        {'op': 'relocate', 'object_ref': entry['object_ref'], 'target_path': '/source', 'new_name': 'Move.mp4'},
                    ]
                    noops = change_actions.preview_guangya_fs_change(change_actions.guangya_fs_change_preview_arguments(args), context)
                    self.assertEqual(noops.status, 'no_changes')
                    self.assertEqual(noops.data['total'], 0)
                    self.assertEqual(noops.data['unchanged_count'], 3)
                    self.assertIsNone(change_actions._flow('owner'))
                    self.assertFalse(set(self.plan_dir.glob('*.json')) - before)
                    with self.assertRaises(AgentToolError):
                        change_actions.prepare_guangya_fs_change_confirmation({}, context)
                    with self.assertRaises(guangya_fs_change.GuangYaFSChangeError):
                        guangya_fs_change.load_fs_change_plan(old_plan, owner='owner')
                self.assertEqual(client.file_info('move').parent_id, 'source')
                self.assertEqual(client.file_info('move').name, 'Move.mp4')

    def test_noop_does_not_hide_snapshot_drift_or_real_name_conflicts(self):
        client = FakeGatewayClient()
        result = self._query(client)
        entry = next(e for e in result.data['entries'] if e['object_name'] == 'Move.mp4')
        observation = guangya_workspace.load_directory_observation(result.data['observation_ref'], owner='owner')
        client.rename('move', 'Changed.mp4')
        with self.assertRaises(guangya_workspace.GuangYaWorkspaceStale):
            guangya_fs_change.build_fs_change_plan(client, owner='owner', observation=observation, operations=[
                {'op': 'rename', 'object_ref': entry['object_ref'], 'new_name': 'Changed.mp4'}])
        result = self._query(client)
        refs = {e['object_name']: e['object_ref'] for e in result.data['entries']}
        observation = guangya_workspace.load_directory_observation(result.data['observation_ref'], owner='owner')
        with self.assertRaisesRegex(guangya_fs_change.GuangYaFSChangeError, '占用'):
            guangya_fs_change.build_fs_change_plan(client, owner='owner', observation=observation, operations=[
                {'op': 'rename', 'object_ref': refs['Changed.mp4'], 'new_name': 'Changed.mp4'},
                {'op': 'rename', 'object_ref': refs['广告-ABC.mp4'], 'new_name': 'Changed.mp4'}])
