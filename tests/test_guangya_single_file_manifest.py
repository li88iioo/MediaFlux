"""光鸭真实单文件 BT 响应回归：根文件描述不是空清单，也不是任意缺树补零。"""
from __future__ import annotations

from typing import Any, cast

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from guangyaclient import GuangyaClient as RawClient

from app.clients.guangya import GuangYaClient
from app.modules import offline
from tests.test_guangya_offline_selection import FakeSelectionClient

SINGLE: dict[str, Any] = {
    "msg": "success",
    "data": {
        "resType": 1,
        "btResInfo": {
            "infoHash": "a" * 40,
            "fileName": "Fixture.Single.mp4",
            "fileSize": 160 * 1024 * 1024,
        },
    },
}


class GuangYaSingleFileManifestTests(unittest.TestCase):
    def setUp(self):
        self.rules = offline.OfflineRules(
            magnet_enabled=True, ed2k_enabled=True, http_enabled=True,
            target_dir_id="fixture-target", target_dir_name="测试目录",
            secondary_enabled=False, secondary_dir_id="0", secondary_dir_name="",
            secondary_keywords=(), exclude_keywords=(), min_file_mb=50,
            allowed_exts=("mkv", "mp4"),
        )
        for target in (
            patch.object(offline.OfflineRules, "from_config", return_value=self.rules),
            patch.object(offline.time, "sleep"),
            patch("socket.socket.connect", side_effect=AssertionError("external network forbidden")),
            patch("socket.getaddrinfo", side_effect=AssertionError("external network forbidden")),
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_complete_single_bt_root_is_one_file(self):
        files = GuangYaClient.normalize_offline_files(copy.deepcopy(SINGLE))
        self.assertEqual(files, [{
            "index": 0, "name": "Fixture.Single.mp4", "size": 160 * 1024 * 1024, "excluded": False,
        }])

    def test_magnet_and_torrent_submit_verified_single_file_without_indexes(self):
        for torrent in (None, b"fixture-original-torrent"):
            with self.subTest(torrent=torrent is not None):
                client = FakeSelectionClient(copy.deepcopy(SINGLE))
                result = offline.submit_offline("magnet:?xt=urn:btih:" + "a" * 40,
                                                client=cast(GuangYaClient, client), torrent_data=torrent)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["selected_count"], 1)
                self.assertEqual(result["selection_mode"], "single_file")
                self.assertEqual(client.selection_calls, [])
                self.assertEqual(len(client.legacy_calls), 1)
                self.assertEqual(client.legacy_calls[0]["target_dir_id"], self.rules.target_dir_id)
                self.assertEqual(result["excluded_count"], 0)
                self.assertFalse(result["unverified_manifest"])
                self.assertEqual(len(client.resolve_calls), 0 if torrent else 1)
                self.assertEqual(len(client.torrent_resolve_calls), 1 if torrent else 0)

    def test_one_file_in_a_bt_tree_still_submits_its_real_index(self):
        response = copy.deepcopy(SINGLE)
        response["data"]["btResInfo"].update(subfilesNum=1, subfiles=[{
            "fileIndex": 0, "fileName": "Fixture.Single.mp4", "fileSize": 160 * 1024 * 1024,
        }])
        client = FakeSelectionClient(response)
        result = offline.submit_offline("magnet:?xt=urn:btih:" + "a" * 40, client=cast(GuangYaClient, client))
        self.assertTrue(result["ok"])
        self.assertEqual(result["selection_mode"], "files")
        self.assertEqual(client.selection_calls[0]["file_indexes"], [0])
        self.assertEqual(client.legacy_calls, [])

    def test_single_file_sdk_payload_omits_file_indexes_and_retains_task_id(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        client = GuangYaClient(token_file=Path(folder) / "missing-token.json")
        raw = RawClient(access_token="fixture-token", device_id="fixture-device")
        raw.request = Mock(side_effect=[
            httpx.Response(200, json=copy.deepcopy(SINGLE)),
            httpx.Response(200, json={"msg": "success", "data": {"taskId": "single-task"}}),
        ])
        client._raw = raw
        self.addCleanup(client.close)
        magnet = "magnet:?xt=urn:btih:" + "a" * 40
        result = offline.submit_offline(magnet, client=client)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["task_ids"], ["single-task"])
        self.assertEqual((result["batch_count"], result["completed_batches"], result["selected_count"]), (1, 1, 1))
        self.assertEqual(raw.request.call_count, 2)
        create = raw.request.call_args
        self.assertTrue(create.args[0].endswith("/create_task"))
        self.assertEqual(create.kwargs["json"], {"url": magnet, "parentId": self.rules.target_dir_id})

    def test_multifile_hints_without_tree_do_not_become_single_file(self):
        for key, value in (
            ("subfilesNum", 0), ("subfilesNum", 1), ("subfilesNum", 2),
            ("subFilesNum", 2), ("fileCount", 2), ("file_count", 2),
            ("subfiles", []), ("subFiles", []), ("files", []), ("fileList", []),
            ("fileTree", {}), ("file_tree", {}), ("children", []),
        ):
            with self.subTest(key=key, value=value):
                response = copy.deepcopy(SINGLE)
                response["data"]["btResInfo"][key] = value
                self.assertEqual(GuangYaClient.normalize_offline_files(response), [])

    def test_incomplete_or_untyped_root_is_not_a_manifest(self):
        for key, value in (
            ("infoHash", ""), ("infoHash", "not-a-hash"), ("fileName", ""),
            ("fileName", "."), ("fileName", ".."),
            ("fileName", "folder/Fixture.mp4"), ("fileName", "folder\\Fixture.mp4"),
            ("fileSize", 0), ("fileSize", -1), ("fileSize", False),
            ("fileSize", 12.5), ("fileSize", None), ("type", "folder"), ("isDir", True),
        ):
            with self.subTest(key=key, value=value):
                response = copy.deepcopy(SINGLE)
                response["data"]["btResInfo"][key] = value
                self.assertEqual(GuangYaClient.normalize_offline_files(response), [])
        for res_type in (None, False, True, 0, 1.0, 2, "1", "http"):
            with self.subTest(res_type=res_type):
                response = copy.deepcopy(SINGLE)
                response["data"]["resType"] = res_type
                self.assertEqual(GuangYaClient.normalize_offline_files(response), [])

    def test_invalid_or_nonzero_explicit_root_index_is_not_overwritten(self):
        for value in (False, -1, 0.25, 3, None):
            with self.subTest(value=value):
                response = copy.deepcopy(SINGLE)
                response["data"]["btResInfo"]["fileIndex"] = value
                with self.assertRaises(ValueError):
                    GuangYaClient.normalize_offline_files(response)

    def test_root_and_parent_exclusions_are_honored(self):
        for location in ("parent", "root", "flag"):
            with self.subTest(location=location):
                response = copy.deepcopy(SINGLE)
                if location == "parent":
                    response["data"]["excludeIndices"] = [0]
                elif location == "root":
                    response["data"]["btResInfo"]["excludeIndices"] = [0]
                else:
                    response["data"]["btResInfo"]["excluded"] = True
                self.assertTrue(GuangYaClient.normalize_offline_files(response)[0]["excluded"])

    def test_single_file_still_obeys_video_and_size_rules(self):
        for name, size in (("notes.txt", 160 * 1024 * 1024), ("Small.mp4", 1024), ("Sample.mp4", 160 * 1024 * 1024)):
            with self.subTest(name=name):
                response = copy.deepcopy(SINGLE)
                response["data"]["btResInfo"].update(fileName=name, fileSize=size)
                client = FakeSelectionClient(response)
                client.create_dir = Mock()
                result = offline.submit_offline("magnet:?xt=urn:btih:" + "a" * 40,
                                                client=cast(GuangYaClient, client), isolate_task=True)
                self.assertFalse(result["ok"])
                self.assertIn("仅视频规则", result["error"])
                client.create_dir.assert_not_called()
                self.assertEqual(client.selection_calls, [])
                self.assertEqual(client.legacy_calls, [])

    def test_nested_summary_is_not_a_root_manifest(self):
        response = {"msg": "success", "data": {"metadata": copy.deepcopy(SINGLE["data"])}}
        self.assertEqual(GuangYaClient.normalize_offline_files(response), [])
        self.assertEqual(GuangYaClient.normalize_offline_files(copy.deepcopy(SINGLE["data"])), [])

    def test_conflicting_failure_envelopes_do_not_use_stale_single_metadata(self):
        for location in ("response", "data"):
            for key, value in (
                ("success", False), ("ok", False), ("code", 403), ("error", {"kind": "failed"}),
                ("msg", "error"), ("message", "failed"), ("state", "failed"), ("status", "pending"),
            ):
                with self.subTest(location=location, key=key):
                    response = copy.deepcopy(SINGLE)
                    container = response if location == "response" else response["data"]
                    container[key] = value
                    self.assertEqual(GuangYaClient.normalize_offline_files(response), [])
        for msg in (None, "error", "pending"):
            with self.subTest(msg=msg):
                response = copy.deepcopy(SINGLE)
                response["msg"] = msg
                self.assertEqual(GuangYaClient.normalize_offline_files(response), [])

    def test_nonzero_exclusion_is_evidence_against_a_single_file(self):
        for location in ("response", "data", "root"):
            with self.subTest(location=location):
                response = copy.deepcopy(SINGLE)
                container = (response if location == "response" else response["data"]
                             if location == "data" else response["data"]["btResInfo"])
                container["excludeIndices"] = [0, 5]
                self.assertEqual(GuangYaClient.normalize_offline_files(response), [])

    def test_top_level_tree_or_count_hint_prevents_root_inference(self):
        for key, value in (("files", []), ("fileList", None), ("subfilesNum", 11)):
            with self.subTest(key=key):
                response = copy.deepcopy(SINGLE)
                response[key] = value
                self.assertEqual(GuangYaClient.normalize_offline_files(response), [])

    def test_each_explicit_root_index_alias_must_be_zero(self):
        for indexes in (
            {"fileIndex": 0, "selectIndex": 7}, {"fileIndex": 0, "file_index": 7},
            {"selectIndex": 0, "fileIndex": 7},
        ):
            with self.subTest(indexes=indexes):
                response = copy.deepcopy(SINGLE)
                response["data"]["btResInfo"].update(indexes)
                with self.assertRaises(ValueError):
                    GuangYaClient.normalize_offline_files(response)
        response = copy.deepcopy(SINGLE)
        response["data"]["btResInfo"].update(fileIndex=0, selectIndex="0", isDir=False)
        self.assertEqual(GuangYaClient.normalize_offline_files(response)[0]["index"], 0)

    def test_single_with_explicit_subfiles_uses_tree_only(self):
        response = copy.deepcopy(SINGLE)
        response["data"]["btResInfo"].update(subfilesNum=1, subfiles=[{
            "fileName": "Fixture.Single.mp4", "fileSize": 160 * 1024 * 1024,
        }])
        self.assertEqual(len(GuangYaClient.normalize_offline_files(response)), 1)
