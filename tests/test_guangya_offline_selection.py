from __future__ import annotations

from dataclasses import replace
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tests.support import InitializedWebTestCase

from app.clients.guangya import GuangYaClient
from app.config import web_credentials
from app.modules import offline
from app.routes import offline_api


RESOLVE_SUBFILES_FIXTURE = {
    "code": 0,
    "msg": "success",
    "data": {
        "resourceId": "bt-resource-1",
        "resourceName": "Demo.Release.2026",
        "resourceType": "magnet",
        "totalSize": 1730150400,
        "excludeIndices": [2],
        "subfiles": [
            {
                "fileIndex": 0,
                "name": "Demo.Release.2026.2160p.mkv",
                "size": 1610612736,
                "type": "file",
                "path": "/Demo.Release.2026.2160p.mkv",
            },
            {
                "file_index": "1",
                "fileName": "sample.mp4",
                "fileSize": "104857600",
                "fileType": "video",
                "path": "/sample.mp4",
            },
            {
                "fileIdx": 2,
                "filename": "trailer.mp4",
                "bytes": 125829120,
                "kind": "video",
                "path": "/trailer.mp4",
            },
            {
                "select_index": 3,
                "displayName": "readme.txt",
                "length": 4096,
                "kind": "file",
                "path": "/readme.txt",
            },
        ],
    },
}

RESOLVE_FILE_LIST_FIXTURE = {
    "success": True,
    "message": "ok",
    "data": {
        "title": "Alternative payload",
        "files": [
            {
                "fileIdx": "7",
                "file_name": "Episode.S01E01.ts",
                "file_size": "734003200",
                "type": "object",
                "excluded": False,
            },
            {
                "selectIndex": 8,
                "title": "Episode.S01E02.ts",
                "size": 0,
                "type": "object",
                "isExcluded": True,
            },
        ],
    },
}


RESOLVE_EXCLUDED_ONLY_FIXTURE = {
    "code": 0,
    "msg": "success",
    "data": {
        "resourceType": "magnet",
        "resourceName": "resolver excluded payload",
        "excludeIndices": [0, 1, 2],
        "subfiles": [
            {"fileIndex": 0, "name": "ad-1.png", "size": 1468006},
            {"fileIndex": 1, "name": "ad-2.png", "size": 1363148},
            {"fileIndex": 2, "name": "ad-3.png", "size": 1258291},
        ],
    },
}


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class GuangYaOfflineSelectionDomainTests(unittest.TestCase):
    def _client_with_raw(self, raw) -> GuangYaClient:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        client = GuangYaClient(token_file=Path(temp_dir.name) / "missing-token.json")
        raw.token = ""
        raw.refresh_token_value = ""
        raw.token_expires_at = None
        client._raw = raw
        return client

    def test_offline_rules_from_mapping_remains_available_after_preview_store_addition(self):
        factory = getattr(offline.OfflineRules, "from_mapping", None)
        self.assertIsNotNone(factory)

        rules = factory({"target_dir_id": "9001", "allowed_exts": "mkv,mp4"})

        self.assertEqual(rules.target_dir_id, "9001")
        self.assertEqual(rules.allowed_exts, ("mkv", "mp4"))

    def test_normalize_offline_files_accepts_field_variants_and_resolver_exclusions(self):
        normalize = getattr(GuangYaClient, "normalize_offline_files", None)
        self.assertIsNotNone(normalize, "GuangYaClient 应提供离线资源文件归一化")

        first = normalize(RESOLVE_SUBFILES_FIXTURE)
        second = normalize(RESOLVE_FILE_LIST_FIXTURE)

        self.assertEqual(
            first,
            [
                {"index": 0, "name": "Demo.Release.2026.2160p.mkv", "size": 1610612736, "excluded": False},
                {"index": 1, "name": "sample.mp4", "size": 104857600, "excluded": False},
                {"index": 2, "name": "trailer.mp4", "size": 125829120, "excluded": True},
                {"index": 3, "name": "readme.txt", "size": 4096, "excluded": False},
            ],
        )
        self.assertEqual(
            second,
            [
                {"index": 7, "name": "Episode.S01E01.ts", "size": 734003200, "excluded": False},
                {"index": 8, "name": "Episode.S01E02.ts", "size": 0, "excluded": True},
            ],
        )

    def test_normalize_offline_files_recovers_omitted_zero_index_in_bt_manifest(self):
        response = {
            "msg": "success",
            "data": {
                "btResInfo": {
                    "infoHash": "example",
                    "fileName": "Complete.Series",
                    "subfilesNum": 3,
                    "excludeIndices": [2],
                    "subfiles": [
                        {"fileName": "Show.S01E01.mkv", "fileSize": 700},
                        {"fileIndex": 1, "fileName": "Show.S01E02.mkv", "fileSize": 800},
                        {"fileIndex": 2, "fileName": "poster.png", "fileSize": 10},
                    ],
                },
            },
        }

        files = GuangYaClient.normalize_offline_files(response)

        self.assertEqual(files, [
            {"index": 0, "name": "Show.S01E01.mkv", "size": 700, "excluded": False},
            {"index": 1, "name": "Show.S01E02.mkv", "size": 800, "excluded": False},
            {"index": 2, "name": "poster.png", "size": 10, "excluded": True},
        ])

    def test_normalize_offline_files_does_not_guess_ambiguous_bt_indexes(self):
        response = {
            "data": {
                "btResInfo": {
                    "infoHash": "example",
                    "subfilesNum": 2,
                    "subfiles": [
                        {"fileName": "unknown.mkv", "fileSize": 700},
                        {"fileIndex": 7, "fileName": "indexed.mkv", "fileSize": 800},
                    ],
                },
            },
        }

        files = GuangYaClient.normalize_offline_files(response)

        self.assertEqual(files, [
            {"index": 7, "name": "indexed.mkv", "size": 800, "excluded": False},
        ])

    def test_normalize_offline_files_ignores_unrelated_id_only_metadata_lists(self):
        response = {
            "code": 0,
            "msg": "success",
            "data": {
                "trackers": [{"id": 77, "status": "online"}],
                "subfiles": [{"fileIndex": 3, "name": "Feature.mkv", "size": 1024, "type": "file"}],
            },
        }

        files = GuangYaClient.normalize_offline_files(response)

        self.assertEqual(files, [{"index": 3, "name": "Feature.mkv", "size": 1024, "excluded": False}])

    def test_normalize_offline_files_only_accepts_explicit_file_index_fields_in_known_trees(self):
        response = {
            "code": 0,
            "msg": "success",
            "data": {
                "trackers": [
                    {"fileIndex": 90, "name": "tracker-should-not-be-a-file.mkv", "size": 10},
                ],
                "files": [
                    {"index": 1, "name": "generic-index.mkv", "size": 100, "type": "file"},
                    {"id": 2, "name": "generic-id.mkv", "size": 100, "type": "file"},
                    {"seq": 3, "name": "generic-seq.mkv", "size": 100, "type": "file"},
                    {"order": 4, "name": "generic-order.mkv", "size": 100, "type": "file"},
                    {"fileIndex": 5, "name": "real-file.mkv", "size": 100, "type": "file"},
                ],
            },
        }

        files = GuangYaClient.normalize_offline_files(response)

        self.assertEqual(files, [
            {"index": 5, "name": "real-file.mkv", "size": 100, "excluded": False},
        ])

    def test_normalize_offline_files_does_not_synthesize_missing_indexes(self):
        response = {
            "code": 0,
            "data": {
                "fileList": [
                    {"name": "missing-index.mkv", "size": 100, "type": "file"},
                    {"file_index": 7, "name": "indexed.mkv", "size": 200, "type": "file"},
                ],
            },
        }

        files = GuangYaClient.normalize_offline_files(response)

        self.assertEqual(files, [
            {"index": 7, "name": "indexed.mkv", "size": 200, "excluded": False},
        ])

    def test_normalize_offline_files_rejects_duplicate_real_indexes_in_nested_trees(self):
        response = {
            "code": 0,
            "data": {
                "files": [
                    {"fileIndex": 11, "name": "first.mkv", "size": 100, "type": "file"},
                    {
                        "name": "Season",
                        "type": "folder",
                        "subfiles": [
                            {"file_index": 11, "name": "second.mkv", "size": 200, "type": "file"},
                        ],
                    },
                ],
            },
        }

        with self.assertRaisesRegex(ValueError, "解析结果包含重复文件索引: 11"):
            GuangYaClient.normalize_offline_files(response)

    def test_default_selection_applies_extension_size_keyword_and_resolver_filters(self):
        build_choices = getattr(offline, "build_offline_file_choices", None)
        self.assertIsNotNone(build_choices, "offline 模块应提供默认选集规则")
        rules = offline.OfflineRules(
            magnet_enabled=True,
            ed2k_enabled=True,
            http_enabled=True,
            target_dir_id="9001",
            target_dir_name="电影",
            secondary_enabled=False,
            secondary_dir_id="0",
            secondary_dir_name="",
            secondary_keywords=(),
            exclude_keywords=("sample", "trailer"),
            min_file_mb=200,
            allowed_exts=("mkv", "mp4"),
        )
        files = [
            {"index": 0, "name": "Feature.mkv", "size": 500 * 1024 * 1024, "excluded": False},
            {"index": 1, "name": "sample.mp4", "size": 400 * 1024 * 1024, "excluded": False},
            {"index": 2, "name": "Small.mp4", "size": 100 * 1024 * 1024, "excluded": False},
            {"index": 3, "name": "Poster.jpg", "size": 2 * 1024 * 1024, "excluded": False},
            {"index": 4, "name": "Resolver.mkv", "size": 800 * 1024 * 1024, "excluded": True},
        ]

        choices = build_choices(files, rules)

        self.assertEqual([item["index"] for item in choices if item["selected"]], [0])
        self.assertEqual(
            {item["index"]: item["exclude_reason"] for item in choices},
            {0: "", 1: "命中排除词: sample", 2: "小于 200 MB", 3: "扩展名不允许", 4: "解析器标记为排除"},
        )
        self.assertTrue(choices[-1]["locked"])
        self.assertFalse(choices[1]["locked"])

    def test_selected_task_submission_splits_501_indexes_into_500_item_batches(self):
        raw = Mock()
        raw.request.side_effect = [
            FakeResponse({"code": 0, "msg": "success", "data": {"taskId": "task-a"}}),
            FakeResponse({"code": 0, "msg": "success", "data": {"taskId": "task-b"}}),
        ]
        client = self._client_with_raw(raw)
        submit = getattr(client, "add_offline_selection", None)
        self.assertIsNotNone(submit, "GuangYaClient 应提供选集创建接口")

        result = submit(
            "magnet:?xt=urn:btih:selection",
            "1927445875113771071",
            list(range(501)),
        )

        self.assertEqual(result["task_ids"], ["task-a", "task-b"])
        self.assertIn("ok", result)
        self.assertTrue(result["ok"])
        self.assertFalse(result["partial_success"])
        self.assertEqual(result["completed_batches"], 2)
        self.assertEqual(result["completed_indexes"], list(range(501)))
        self.assertEqual(result["remaining_indexes"], [])
        self.assertIsNone(result["failed_batch"])
        self.assertEqual(result["error"], "")
        self.assertEqual(result["batch_count"], 2)
        self.assertEqual(raw.request.call_count, 2)
        first_call, second_call = raw.request.call_args_list
        self.assertEqual(first_call.kwargs["method"], "POST")
        self.assertTrue(first_call.args[0].endswith("/nd.bizcloudcollection.s/v1/create_task"))
        self.assertEqual(first_call.kwargs["json"]["fileIndexes"], list(range(500)))
        self.assertEqual(second_call.kwargs["json"]["fileIndexes"], [500])
        self.assertEqual(first_call.kwargs["json"]["url"], "magnet:?xt=urn:btih:selection")
        self.assertEqual(first_call.kwargs["json"]["parentId"], "1927445875113771071")

    def test_torrent_resolution_uploads_original_bytes_through_sdk(self):
        raw = Mock()
        raw.cloud_resolve_torrent.return_value = RESOLVE_SUBFILES_FIXTURE
        client = self._client_with_raw(raw)

        result = client.resolve_torrent(b"private-torrent-bytes")

        self.assertEqual(result, RESOLVE_SUBFILES_FIXTURE)
        raw.cloud_resolve_torrent.assert_called_once_with(b"private-torrent-bytes")

    def test_selected_task_submission_reports_second_batch_failure(self):
        raw = Mock()
        raw.request.side_effect = [
            FakeResponse({"code": 0, "msg": "success", "data": {"taskId": "task-a"}}),
            FakeResponse({"code": 503, "msg": "cloud capacity limited", "data": {}}),
        ]
        client = self._client_with_raw(raw)

        try:
            result = client.add_offline_selection(
                "magnet:?xt=urn:btih:selection-failure",
                "1927445875113771071",
                list(range(501)),
            )
        except Exception as exc:
            self.fail(f"部分成功必须结构化返回，不能抛异常: {exc}")

        self.assertEqual(raw.request.call_count, 2)
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial_success"])
        self.assertEqual(result["completed_batches"], 1)
        self.assertEqual(result["task_ids"], ["task-a"])
        self.assertEqual(result["completed_indexes"], list(range(500)))
        self.assertEqual(result["remaining_indexes"], [500])
        self.assertEqual(result["failed_batch"], 2)
        self.assertEqual(result["error"], "cloud capacity limited")

    def test_selected_task_submission_rejects_explicit_and_ambiguous_failure_payloads(self):
        cases = [
            ({"error": "quota exceeded"}, "quota exceeded"),
            ({"errors": ["quota exceeded"]}, "quota exceeded"),
            ({"success": 0}, "接口返回失败"),
            ({"success": "false"}, "接口返回失败"),
            ({"success": "error"}, "接口返回失败"),
            ({"state": 0}, "接口返回失败"),
            ({"state": "failed"}, "接口返回失败"),
            ({"data": {}}, "接口返回结果不明确"),
        ]

        for payload, expected_error in cases:
            with self.subTest(payload=payload):
                raw = Mock()
                raw.request.return_value = FakeResponse(payload)
                client = self._client_with_raw(raw)

                result = client.add_offline_selection(
                    "magnet:?xt=urn:btih:conservative-response",
                    "1927445875113771071",
                    [0],
                )

                self.assertFalse(result["ok"])
                self.assertEqual(result["completed_batches"], 0)
                self.assertEqual(result["remaining_indexes"], [0])
                self.assertIn(expected_error, result["error"])

    def test_selected_task_submission_accepts_normalized_success_payloads(self):
        cases = [
            {"code": 0},
            {"success": True},
            {"success": 1},
            {"success": "true"},
            {"state": 1},
            {"state": "1"},
            {"state": "success"},
            {"msg": "success"},
            {"message": "SUCCESS"},
            {"data": {"taskId": "task-normalized"}},
        ]

        for payload in cases:
            with self.subTest(payload=payload):
                raw = Mock()
                raw.request.return_value = FakeResponse(payload)
                client = self._client_with_raw(raw)

                result = client.add_offline_selection(
                    "magnet:?xt=urn:btih:normalized-success",
                    "1927445875113771071",
                    [0],
                )

                self.assertTrue(result["ok"])
                self.assertEqual(result["completed_batches"], 1)
                self.assertEqual(result["remaining_indexes"], [])



class FakeSelectionClient:
    def __init__(self, resolve_payload: dict):
        self.logged_in = True
        self.resolve_payload = resolve_payload
        self.resolve_calls: list[str] = []
        self.torrent_resolve_calls: list[bytes] = []
        self.selection_calls: list[dict] = []
        self.legacy_calls: list[dict] = []
        self.selection_result: dict | None = None

    def resolve_url(self, url: str) -> dict:
        self.resolve_calls.append(url)
        return self.resolve_payload

    def resolve_torrent(self, torrent_data: bytes) -> dict:
        self.torrent_resolve_calls.append(torrent_data)
        return self.resolve_payload

    def add_offline_selection(self, url: str, target_dir_id: str, file_indexes: list[int]) -> dict:
        self.selection_calls.append({
            "url": url,
            "target_dir_id": target_dir_id,
            "file_indexes": list(file_indexes),
        })
        return self.selection_result or {
            "task_ids": ["selected-task"],
            "ok": True,
            "partial_success": False,
            "completed_batches": 1,
            "completed_indexes": list(file_indexes),
            "remaining_indexes": [],
            "failed_batch": None,
            "error": "",
            "selected_count": len(file_indexes),
            "batch_count": 1,
            "responses": [{"code": 0, "msg": "success", "data": {"taskId": "selected-task"}}],
        }

    def add_offline_task(self, url: str, target_dir_id: str = "0", task_type: str = "magnet") -> bool:
        self.legacy_calls.append({
            "url": url,
            "target_dir_id": target_dir_id,
            "task_type": task_type,
        })
        return True


class GuangYaOfflineSelectionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.rules = offline.OfflineRules(
            magnet_enabled=True,
            ed2k_enabled=True,
            http_enabled=True,
            target_dir_id="9001",
            target_dir_name="电影",
            secondary_enabled=False,
            secondary_dir_id="0",
            secondary_dir_name="",
            secondary_keywords=(),
            exclude_keywords=("sample",),
            min_file_mb=200,
            allowed_exts=("mkv", "mp4"),
        )

    def test_automatic_submit_filters_files_and_uses_isolated_task_directory(self):
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        client.create_dir = Mock(return_value="staging-7")
        client.list_dir = Mock(return_value=[])
        client.delete = Mock(return_value=True)

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:auto-filter",
                title="Demo Release",
                client=client,
                isolate_task=True,
                task_key="7",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["excluded_count"], 3)
        self.assertEqual(result["decision"]["target_dir_id"], "staging-7")
        client.create_dir.assert_called_once()
        self.assertEqual(client.selection_calls[0]["file_indexes"], [0])
        self.assertEqual(client.selection_calls[0]["target_dir_id"], "staging-7")
        self.assertEqual(client.legacy_calls, [])

    def test_torrent_submit_uploads_original_metadata_before_creating_magnet_selection(self):
        response = {
            "code": 0,
            "msg": "success",
            "data": {
                "btResInfo": {
                    "infoHash": "a1754038e29417449a65271a689dde5699575d54",
                    "fileName": "[GM-Team][国漫][牧神记][97][1080P].mp4",
                    "subfilesNum": 1,
                    "subfiles": [{
                        "fileName": "[GM-Team][国漫][牧神记][97][1080P].mp4",
                        "fileSize": 989755300,
                    }],
                },
            },
        }
        client = FakeSelectionClient(response)
        client.create_dir = Mock(return_value="staging-torrent")
        client.list_dir = Mock(return_value=[])
        client.delete = Mock(return_value=True)
        torrent_data = b"d4:infod4:name12:demo.mp4ee"
        magnet = "magnet:?xt=urn:btih:a1754038e29417449a65271a689dde5699575d54"

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules):
            result = offline.submit_offline(
                magnet,
                title="牧神记 97",
                client=client,
                isolate_task=True,
                task_key="torrent",
                torrent_data=torrent_data,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(client.resolve_calls, [])
        self.assertEqual(client.torrent_resolve_calls, [torrent_data])
        self.assertEqual(client.selection_calls, [{
            "url": magnet,
            "target_dir_id": "staging-torrent",
            "file_indexes": [0],
        }])

    def test_unresolved_torrent_reports_torrent_specific_failure(self):
        client = FakeSelectionClient({
            "code": 0,
            "msg": "success",
            "data": {"resourceType": "torrent", "resourceName": "unresolved"},
        })

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:" + "a" * 40,
                client=client,
                torrent_data=b"private-torrent-bytes",
            )

        self.assertFalse(result["ok"])
        self.assertIn("种子文件未解析到可验证文件列表", result["error"])
        self.assertNotIn("磁力资源连续", result["error"])
        self.assertEqual(client.torrent_resolve_calls, [b"private-torrent-bytes"])

    def test_unknown_submit_outcome_retains_isolated_task_directory(self):
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        client.selection_result = {
            "ok": False,
            "outcome_unknown": True,
            "tracking_incomplete": True,
            "task_ids": [],
            "batch_count": 1,
            "completed_batches": 0,
            "error": "upstream timeout",
        }
        client.create_dir = Mock(return_value="staging-unknown")
        client.list_dir = Mock(return_value=[])
        client.delete = Mock(return_value=True)

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:unknown-outcome",
                title="Demo Release",
                client=client,
                isolate_task=True,
                task_key="unknown",
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["outcome_unknown"])
        self.assertTrue(result["tracking_incomplete"])
        self.assertEqual(result["staging"]["cleanup_status"], "retained")
        client.list_dir.assert_not_called()
        client.delete.assert_not_called()

    def test_submit_exception_retains_isolated_task_directory(self):
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        client.add_offline_selection = Mock(side_effect=TimeoutError("read timeout"))
        client.create_dir = Mock(return_value="staging-timeout")
        client.list_dir = Mock(return_value=[])
        client.delete = Mock(return_value=True)

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:timeout-outcome",
                title="Demo Release",
                client=client,
                isolate_task=True,
                task_key="timeout",
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["outcome_unknown"])
        self.assertEqual(result["staging"]["cleanup_status"], "retained")
        client.list_dir.assert_not_called()
        client.delete.assert_not_called()

    def test_automatic_submit_fails_closed_before_creating_isolated_whole_magnet(self):
        client = FakeSelectionClient({
            "code": 0,
            "msg": "success",
            "data": {"resourceType": "magnet", "resourceName": "unresolved torrent"},
        })
        client.create_dir = Mock(return_value="must-not-be-created")

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules), patch.object(
            offline.time, "sleep",
        ):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:no-whole-fallback",
                title="Demo Release",
                client=client,
                isolate_task=True,
                task_key="8",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["resolve_attempts"], 4)
        self.assertIn("已阻止整单下载", result["error"])
        client.create_dir.assert_not_called()
        self.assertEqual(client.selection_calls, [])
        self.assertEqual(client.legacy_calls, [])

    def test_automatic_submit_retries_excluded_only_manifest_and_recovers_selection(self):
        client = FakeSelectionClient(RESOLVE_EXCLUDED_ONLY_FIXTURE)
        client.resolve_url = Mock(side_effect=[
            RESOLVE_EXCLUDED_ONLY_FIXTURE,
            RESOLVE_SUBFILES_FIXTURE,
        ])
        client.create_dir = Mock(return_value="staging-recovered")
        client.list_dir = Mock(return_value=[])
        client.delete = Mock(return_value=True)

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules), patch.object(
            offline.time, "sleep",
        ):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:excluded-then-valid",
                title="Demo Release",
                client=client,
                isolate_task=True,
                task_key="recovered",
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["unverified_manifest"])
        self.assertEqual(result["selection_mode"], "files")
        self.assertEqual(result["resolve_attempts"], 2)
        self.assertEqual(client.resolve_url.call_count, 2)
        self.assertEqual(client.selection_calls[0]["file_indexes"], [0])
        self.assertEqual(client.legacy_calls, [])

    def test_automatic_submit_fails_closed_when_manifest_only_has_resolver_exclusions(self):
        client = FakeSelectionClient(RESOLVE_EXCLUDED_ONLY_FIXTURE)
        client.resolve_url = Mock(side_effect=[RESOLVE_EXCLUDED_ONLY_FIXTURE] * 4)
        client.create_dir = Mock(return_value="must-not-be-created")

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules), patch.object(
            offline.time, "sleep",
        ):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:excluded-only",
                title="Demo Release",
                client=client,
                isolate_task=True,
                task_key="excluded",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["resolve_attempts"], 4)
        self.assertIn("已阻止整单下载", result["error"])
        self.assertEqual(client.resolve_url.call_count, 4)
        client.create_dir.assert_not_called()
        self.assertEqual(client.selection_calls, [])
        self.assertEqual(client.legacy_calls, [])

    def test_automatic_submit_fails_closed_without_isolation_for_excluded_only_manifest(self):
        client = FakeSelectionClient(RESOLVE_EXCLUDED_ONLY_FIXTURE)
        client.resolve_url = Mock(side_effect=[RESOLVE_EXCLUDED_ONLY_FIXTURE] * 4)
        client.create_dir = Mock(return_value="must-not-be-created")

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules), patch.object(
            offline.time, "sleep",
        ):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:excluded-no-isolation",
                title="Demo Release",
                client=client,
                isolate_task=False,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["resolve_attempts"], 4)
        self.assertIn("已阻止整单下载", result["error"])
        client.create_dir.assert_not_called()
        self.assertEqual(client.selection_calls, [])
        self.assertEqual(client.legacy_calls, [])

    def test_automatic_submit_does_not_fallback_when_only_junk_files_are_available(self):
        client = FakeSelectionClient({
            "code": 0,
            "data": {
                "resourceType": "magnet",
                "subfiles": [
                    {"fileIndex": 0, "name": "xx.com poster.png", "size": 4096},
                    {"fileIndex": 1, "name": "readme.txt", "size": 4096},
                    {"fileIndex": 2, "name": "website.url", "size": 512},
                    {"fileIndex": 3, "name": "theme.flac", "size": 4096},
                ],
            },
        })
        client.create_dir = Mock(return_value="must-not-be-created")

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:user-filtered",
                title="Demo Release",
                client=client,
                isolate_task=True,
                task_key="filtered",
            )

        self.assertFalse(result["ok"])
        self.assertIn("没有符合仅视频规则", result["error"])
        self.assertIn("扩展名不允许", result["error"])
        client.create_dir.assert_not_called()
        self.assertEqual(client.selection_calls, [])
        self.assertEqual(client.legacy_calls, [])

    def test_empty_extension_config_defaults_to_video_only(self):
        rules = replace(self.rules, min_file_mb=0, exclude_keywords=(), allowed_exts=())
        choices = offline.build_offline_file_choices([
            {"index": 0, "name": "Movie.mkv", "size": 1024},
            {"index": 1, "name": "Episode.webm", "size": 1024},
            {"index": 2, "name": "theme.mp3", "size": 1024},
            {"index": 3, "name": "poster.jpg", "size": 1024},
            {"index": 4, "name": "subtitle.ass", "size": 1024},
            {"index": 5, "name": "website.url", "size": 1024},
        ], rules)

        self.assertEqual(
            [item["index"] for item in choices if item["selected"]],
            [0, 1],
        )
        self.assertTrue(all(
            item["exclude_reason"] == "扩展名不允许" for item in choices[2:]
        ))

    def test_magnet_manifest_retries_transient_resolve_exception(self):
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        client.resolve_url = Mock(side_effect=[
            RuntimeError("temporary upstream failure"),
            RESOLVE_SUBFILES_FIXTURE,
        ])
        client.create_dir = Mock(return_value="staging-9")
        client.list_dir = Mock(return_value=[])
        client.delete = Mock(return_value=True)

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules), patch.object(
            offline.time, "sleep"
        ):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:retry",
                title="Demo Release",
                client=client,
                isolate_task=True,
                task_key="9",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["resolve_attempts"], 2)
        self.assertEqual(client.resolve_url.call_count, 2)

    def test_preview_resolves_resource_and_returns_default_file_selection(self):
        preview_selection = getattr(offline, "preview_offline_selection", None)
        self.assertIsNotNone(preview_selection, "offline 模块应提供资源选集预览")
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)

        result = preview_selection(
            "magnet:?xt=urn:btih:preview",
            title="Demo Release",
            client=client,
            rules=self.rules,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["has_file_tree"])
        self.assertEqual(result["default_selected_indexes"], [0])
        self.assertEqual([item["index"] for item in result["files"]], [0, 1, 2, 3])
        self.assertEqual(client.resolve_calls, ["magnet:?xt=urn:btih:preview"])

    def test_preview_reports_magnet_without_file_tree_as_unverifiable(self):
        preview_selection = getattr(offline, "preview_offline_selection", None)
        self.assertIsNotNone(preview_selection)
        client = FakeSelectionClient({
            "code": 0,
            "msg": "success",
            "data": {"resourceType": "magnet", "resourceName": "unresolved torrent"},
        })

        result = preview_selection(
            "magnet:?xt=urn:btih:missing-preview-tree",
            client=client,
            rules=self.rules,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "磁力资源未解析到可验证的文件列表")

    def test_submit_re_resolves_and_rejects_tampered_file_index(self):
        submit_selection = getattr(offline, "submit_offline_selection", None)
        self.assertIsNotNone(submit_selection, "offline 模块应提供服务端复核后的选集提交")
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)

        result = submit_selection(
            "magnet:?xt=urn:btih:submit",
            selected_indexes=[0, 999],
            title="Demo Release",
            client=client,
            rules=self.rules,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "选择中包含不存在的文件索引: 999")
        self.assertEqual(client.resolve_calls, ["magnet:?xt=urn:btih:submit"])
        self.assertEqual(client.selection_calls, [])
        self.assertEqual(client.legacy_calls, [])


    def test_submit_rejects_resolver_locked_index_even_when_it_exists(self):
        submit_selection = getattr(offline, "submit_offline_selection", None)
        self.assertIsNotNone(submit_selection)
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)

        result = submit_selection(
            "magnet:?xt=urn:btih:locked",
            selected_indexes=[2],
            client=client,
            rules=self.rules,
        )

        self.assertFalse(result["ok"])
        self.assertIn("选择中包含被下载规则排除的文件", result["error"])
        self.assertIn("解析器标记为排除", result["error"])
        self.assertEqual(client.selection_calls, [])

    def test_submit_uses_selected_create_task_after_server_validation(self):
        submit_selection = getattr(offline, "submit_offline_selection", None)
        self.assertIsNotNone(submit_selection)
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)

        result = submit_selection(
            "magnet:?xt=urn:btih:valid",
            selected_indexes=[1, 0, 1],
            client=client,
            rules=replace(self.rules, exclude_keywords=(), min_file_mb=0),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["selection_mode"], "files")
        self.assertEqual(result["selected_count"], 2)
        self.assertEqual(
            client.selection_calls,
            [{
                "url": "magnet:?xt=urn:btih:valid",
                "target_dir_id": "9001",
                "file_indexes": [1, 0],
            }],
        )

    def test_submit_propagates_structured_partial_success_without_reporting_whole_success(self):
        submit_selection = getattr(offline, "submit_offline_selection", None)
        self.assertIsNotNone(submit_selection)
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        client.selection_result = {
            "ok": False,
            "partial_success": True,
            "completed_batches": 1,
            "task_ids": ["task-a"],
            "completed_indexes": [0],
            "remaining_indexes": [1],
            "failed_batch": 2,
            "error": "cloud capacity limited",
            "selected_count": 2,
            "batch_count": 2,
            "responses": [{"code": 0, "data": {"taskId": "task-a"}}],
        }

        result = submit_selection(
            "magnet:?xt=urn:btih:partial",
            selected_indexes=[0, 1],
            client=client,
            rules=replace(self.rules, exclude_keywords=(), min_file_mb=0),
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["partial_success"])
        self.assertEqual(result["completed_batches"], 1)
        self.assertEqual(result["task_ids"], ["task-a"])
        self.assertEqual(result["completed_indexes"], [0])
        self.assertEqual(result["remaining_indexes"], [1])
        self.assertEqual(result["failed_batch"], 2)
        self.assertEqual(result["error"], "cloud capacity limited")

    def test_submit_without_file_tree_falls_back_to_legacy_create_task(self):
        submit_selection = getattr(offline, "submit_offline_selection", None)
        self.assertIsNotNone(submit_selection)
        client = FakeSelectionClient({
            "code": 0,
            "msg": "success",
            "data": {
                "resourceType": "http",
                "resourceName": "direct-download.iso",
                "url": "https://example.invalid/direct-download.iso",
                "size": 2147483648,
            },
        })

        result = submit_selection(
            "https://example.invalid/direct-download.iso",
            selected_indexes=[],
            client=client,
            rules=self.rules,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["selection_mode"], "legacy")
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(client.selection_calls, [])
        self.assertEqual(
            client.legacy_calls,
            [{
                "url": "https://example.invalid/direct-download.iso",
                "target_dir_id": "9001",
                "task_type": "http",
            }],
        )

    def test_legacy_create_connection_error_is_marked_outcome_unknown(self):
        submit_selection = getattr(offline, "submit_offline_selection", None)
        self.assertIsNotNone(submit_selection)
        client = FakeSelectionClient({
            "code": 0,
            "msg": "success",
            "data": {
                "resourceType": "http",
                "resourceName": "direct-download.iso",
                "url": "https://example.invalid/direct-download.iso",
                "size": 2147483648,
            },
        })
        client.add_offline_task = Mock(side_effect=TimeoutError("upstream timeout"))

        result = submit_selection(
            "https://example.invalid/direct-download.iso",
            selected_indexes=[],
            client=client,
            rules=self.rules,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["outcome_unknown"])
        self.assertTrue(result["review_required"])
        self.assertIn("待核对", result["error"])

    def test_submit_magnet_without_file_tree_does_not_fall_back_to_whole_task(self):
        submit_selection = getattr(offline, "submit_offline_selection", None)
        self.assertIsNotNone(submit_selection)
        client = FakeSelectionClient({
            "code": 0,
            "msg": "success",
            "data": {"resourceType": "magnet", "resourceName": "unresolved torrent"},
        })

        result = submit_selection(
            "magnet:?xt=urn:btih:missing-tree",
            selected_indexes=[],
            client=client,
            rules=self.rules,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "磁力资源未解析到可验证的文件列表")
        self.assertEqual(client.legacy_calls, [])


class OfflinePreviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.rules = offline.OfflineRules(
            magnet_enabled=True,
            ed2k_enabled=True,
            http_enabled=True,
            target_dir_id="9001",
            target_dir_name="电影",
            secondary_enabled=False,
            secondary_dir_id="0",
            secondary_dir_name="",
            secondary_keywords=(),
            exclude_keywords=("sample",),
            min_file_mb=200,
            allowed_exts=("mkv", "mp4"),
        )

    def test_preview_snapshot_is_one_time_and_keeps_server_selection_context(self):
        store_class = getattr(offline, "OfflinePreviewStore", None)
        self.assertIsNotNone(store_class, "offline 模块应提供一次性预览快照存储")
        store = store_class(ttl_seconds=60, clock=lambda: 100.0, token_factory=lambda: "preview-one")

        preview_id = store.create(
            url="magnet:?xt=urn:btih:snapshot",
            title="Demo",
            rules=self.rules,
            target_dir_id="9001",
            target_dir_name="电影",
            file_indexes=[0, 1, 2],
            locked_indexes=[2],
        )
        snapshot = store.claim(
            preview_id,
            url="magnet:?xt=urn:btih:snapshot",
            title="Demo",
            rules=self.rules,
        )

        self.assertEqual(snapshot.file_indexes, (0, 1, 2))
        self.assertEqual(snapshot.locked_indexes, (2,))
        self.assertEqual(snapshot.target_dir_id, "9001")
        with self.assertRaisesRegex(ValueError, "预览已过期或已使用"):
            store.claim(
                preview_id,
                url="magnet:?xt=urn:btih:snapshot",
                title="Demo",
                rules=self.rules,
            )

    def test_preview_snapshot_rule_or_url_change_invalidates_snapshot(self):
        store_class = getattr(offline, "OfflinePreviewStore", None)
        self.assertIsNotNone(store_class)
        store = store_class(ttl_seconds=60, clock=lambda: 100.0, token_factory=lambda: "preview-change")
        preview_id = store.create(
            url="magnet:?xt=urn:btih:snapshot",
            title="Demo",
            rules=self.rules,
            target_dir_id="9001",
            target_dir_name="电影",
            file_indexes=[0],
            locked_indexes=[],
        )
        changed_rules = offline.OfflineRules(
            **{**self.rules.__dict__, "target_dir_id": "9002", "target_dir_name": "剧集"}
        )

        with self.assertRaisesRegex(ValueError, "预览上下文已变化"):
            store.claim(
                preview_id,
                url="magnet:?xt=urn:btih:snapshot",
                title="Demo",
                rules=changed_rules,
            )
        with self.assertRaisesRegex(ValueError, "预览已过期或已使用"):
            store.claim(
                preview_id,
                url="magnet:?xt=urn:btih:snapshot",
                title="Demo",
                rules=self.rules,
            )

        url_store = store_class(
            ttl_seconds=60,
            clock=lambda: 100.0,
            token_factory=lambda: "preview-url-change",
        )
        url_preview_id = url_store.create(
            url="magnet:?xt=urn:btih:original",
            title="Demo",
            rules=self.rules,
            target_dir_id="9001",
            target_dir_name="电影",
            file_indexes=[0],
            locked_indexes=[],
        )
        with self.assertRaisesRegex(ValueError, "预览上下文已变化"):
            url_store.claim(
                url_preview_id,
                url="magnet:?xt=urn:btih:changed",
                title="Demo",
                rules=self.rules,
            )

    def test_preview_snapshot_expires(self):
        store_class = getattr(offline, "OfflinePreviewStore", None)
        self.assertIsNotNone(store_class)
        now = [100.0]
        store = store_class(ttl_seconds=10, clock=lambda: now[0], token_factory=lambda: "preview-expired")
        preview_id = store.create(
            url="https://example.invalid/file.iso",
            title="",
            rules=self.rules,
            target_dir_id="9001",
            target_dir_name="电影",
            file_indexes=[],
            locked_indexes=[],
        )
        now[0] = 111.0

        with self.assertRaisesRegex(ValueError, "预览已过期或已使用"):
            store.claim(
                preview_id,
                url="https://example.invalid/file.iso",
                title="",
                rules=self.rules,
            )

    def test_preview_store_evicts_oldest_snapshot_when_capacity_is_exceeded(self):
        store_class = getattr(offline, "OfflinePreviewStore", None)
        self.assertIsNotNone(store_class)
        preview_ids = iter(("preview-oldest", "preview-middle", "preview-newest"))
        store = store_class(
            ttl_seconds=60,
            max_entries=2,
            clock=lambda: 100.0,
            token_factory=lambda: next(preview_ids),
        )

        created = []
        for suffix in ("oldest", "middle", "newest"):
            created.append(store.create(
                url=f"magnet:?xt=urn:btih:{suffix}",
                title=suffix,
                rules=self.rules,
                target_dir_id="9001",
                target_dir_name="电影",
                file_indexes=[0],
                locked_indexes=[],
            ))

        self.assertEqual(store.entry_count, 2)
        with self.assertRaisesRegex(ValueError, "预览已过期或已使用"):
            store.claim(
                created[0],
                url="magnet:?xt=urn:btih:oldest",
                title="oldest",
                rules=self.rules,
            )
        middle = store.claim(
            created[1],
            url="magnet:?xt=urn:btih:middle",
            title="middle",
            rules=self.rules,
        )
        newest = store.claim(
            created[2],
            url="magnet:?xt=urn:btih:newest",
            title="newest",
            rules=self.rules,
        )

        self.assertEqual(middle.preview_id, "preview-middle")
        self.assertEqual(newest.preview_id, "preview-newest")


class GuangYaOfflineSelectionApiTests(InitializedWebTestCase):
    def setUp(self):
        from app.main import create_app

        self.client = TestClient(create_app(), raise_server_exceptions=False)
        store = getattr(offline_api, "_offline_preview_store", None)
        if store is not None:
            store.clear()
        login_page = self.client.get("/login")
        csrf = self._csrf(login_page.text)
        username, password = web_credentials()
        login = self.client.post(
            "/login",
            data={"csrf_token": csrf, "username": username, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 302)
        page = self.client.get("/guangya/offline")
        self.assertEqual(page.status_code, 200)
        self.headers = {"X-CSRF-Token": self._csrf(page.text)}
        self.rules_payload = {
            "magnet_enabled": True,
            "ed2k_enabled": True,
            "http_enabled": True,
            "target_dir_id": "9001",
            "target_dir_name": "电影",
            "secondary_enabled": False,
            "secondary_dir_id": "0",
            "secondary_dir_name": "",
            "secondary_keywords": "",
            "exclude_keywords": "sample",
            "min_file_mb": 200,
            "allowed_exts": "mkv,mp4",
        }

    @staticmethod
    def _csrf(html: str) -> str:
        match = re.search(r'name="csrf-token" content="([^"]+)"', html)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def test_preview_api_returns_file_choices_and_default_indexes(self):
        fake = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        with patch("app.routes.offline_api.GuangYaClient", return_value=fake):
            response = self.client.post(
                "/api/offline/preview",
                headers=self.headers,
                json={
                    "url": "magnet:?xt=urn:btih:api-preview",
                    "title": "Demo Release",
                    "rules": self.rules_payload,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("ok", payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["has_file_tree"])
        self.assertIn("preview_id", payload)
        self.assertTrue(payload["preview_id"])
        self.assertEqual(payload["default_selected_indexes"], [0])
        self.assertEqual(payload["files"][0]["name"], "Demo.Release.2026.2160p.mkv")

    def test_offline_page_only_exposes_rules_for_telegram_automation(self):
        page = self.client.get("/guangya/offline")

        self.assertEqual(page.status_code, 200)
        self.assertIn('id="offlineForm"', page.text)
        self.assertIn('class="offline-settings-grid"', page.text)
        self.assertIn('id="offlineTargetDir"', page.text)
        self.assertIn('id="offlineSecondaryEnabled"', page.text)
        self.assertIn('id="saveOfflineBtn"', page.text)
        self.assertIn("Telegram Bot", page.text)
        for marker in (
            'id="offlinePreviewTitle"', 'id="offlinePreviewUrl"', 'id="offlinePreviewBtn"',
            'id="offlineSelectionPanel"', 'id="offlineFileList"', 'id="offlineSelectAll"',
            'id="offlineSelectDefault"', 'id="offlineSubmitBtn"', 'id="offlineSubmitFeedback"',
            "renderSelection(", "setSelectionBusy(", "previewButton.addEventListener",
            "submitButton.addEventListener", "fetch('/api/offline/preview'", "fetch('/api/offline/submit'",
        ):
            self.assertNotIn(marker, page.text)

    def test_unauthenticated_offline_api_is_rejected_before_cloud_client_use(self):
        from app.main import create_app

        anonymous = TestClient(create_app(), raise_server_exceptions=False)
        login_page = anonymous.get("/login")
        csrf = self._csrf(login_page.text)
        rules_response = anonymous.get("/api/offline/rules")
        preview_response = anonymous.post(
            "/api/offline/preview",
            headers={"X-CSRF-Token": csrf},
            json={"url": "magnet:?xt=urn:btih:anonymous", "rules": self.rules_payload},
        )

        self.assertEqual(rules_response.status_code, 401)
        self.assertEqual(preview_response.status_code, 401)

    def test_offline_mutations_require_csrf_after_login(self):
        preview = self.client.post(
            "/api/offline/preview",
            json={"url": "magnet:?xt=urn:btih:no-csrf", "rules": self.rules_payload},
        )
        submit = self.client.post(
            "/api/offline/submit",
            json={"preview_id": "missing", "url": "magnet:?xt=urn:btih:no-csrf", "file_indexes": []},
        )

        self.assertEqual(preview.status_code, 403)
        self.assertEqual(submit.status_code, 403)

    def test_preview_api_keeps_malicious_filename_as_json_data_not_page_markup(self):
        malicious_name = '<img src=x onerror="alert(1)">.mkv'
        fake = FakeSelectionClient({
            "code": 0,
            "msg": "success",
            "data": {
                "subfiles": [
                    {
                        "fileIndex": 0,
                        "name": malicious_name,
                        "size": 300 * 1024 * 1024,
                        "type": "file",
                    },
                ],
            },
        })
        with patch("app.routes.offline_api.GuangYaClient", return_value=fake):
            response = self.client.post(
                "/api/offline/preview",
                headers=self.headers,
                json={
                    "url": "magnet:?xt=urn:btih:malicious-name",
                    "rules": self.rules_payload,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.json()["files"][0]["name"], malicious_name)
        page = self.client.get("/guangya/offline")
        self.assertNotIn(malicious_name, page.text)

    def test_submit_api_rejects_index_not_present_in_preview_snapshot(self):
        fake = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        with patch("app.routes.offline_api.GuangYaClient", return_value=fake):
            preview = self.client.post(
                "/api/offline/preview",
                headers=self.headers,
                json={
                    "url": "magnet:?xt=urn:btih:api-submit",
                    "title": "Demo Release",
                    "rules": self.rules_payload,
                },
            )
            self.assertIn("preview_id", preview.json())
            response = self.client.post(
                "/api/offline/submit",
                headers=self.headers,
                json={
                    "preview_id": preview.json()["preview_id"],
                    "url": "magnet:?xt=urn:btih:api-submit",
                    "title": "Demo Release",
                    "file_indexes": [0, 999],
                    "rules": self.rules_payload,
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "选择中包含不属于该预览的文件索引: 999")
        self.assertEqual(fake.selection_calls, [])

    def test_submit_api_reparses_and_rejects_index_removed_since_preview(self):
        fake = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        with patch("app.routes.offline_api.GuangYaClient", return_value=fake):
            preview = self.client.post(
                "/api/offline/preview",
                headers=self.headers,
                json={
                    "url": "magnet:?xt=urn:btih:changed-tree",
                    "title": "Demo Release",
                    "rules": self.rules_payload,
                },
            ).json()
            fake.resolve_payload = {
                "code": 0,
                "msg": "success",
                "data": {
                    "subfiles": [
                        {"fileIndex": 0, "name": "only-current.mkv", "size": 300 * 1024 * 1024},
                    ],
                },
            }
            response = self.client.post(
                "/api/offline/submit",
                headers=self.headers,
                json={
                    "preview_id": preview["preview_id"],
                    "url": "magnet:?xt=urn:btih:changed-tree",
                    "title": "Demo Release",
                    "file_indexes": [1],
                    "rules": self.rules_payload,
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "选择中包含不存在的文件索引: 1")
        self.assertEqual(fake.selection_calls, [])

    def test_submit_api_rejects_target_drift_before_creating_cloud_task(self):
        fake = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        with patch("app.routes.offline_api.GuangYaClient", return_value=fake):
            preview = self.client.post(
                "/api/offline/preview",
                headers=self.headers,
                json={
                    "url": "magnet:?xt=urn:btih:target-drift",
                    "title": "Demo Release",
                    "rules": self.rules_payload,
                },
            ).json()
            with patch(
                "app.modules.offline.analyze_offline_url",
                return_value=offline.OfflineDecision(
                    True,
                    "magnet",
                    "unexpected-target",
                    "错误目录",
                    "规则允许提交",
                ),
            ):
                response = self.client.post(
                    "/api/offline/submit",
                    headers=self.headers,
                    json={
                        "preview_id": preview["preview_id"],
                        "url": "magnet:?xt=urn:btih:target-drift",
                        "title": "Demo Release",
                        "file_indexes": [0],
                        "rules": self.rules_payload,
                    },
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "预览目标已变化，请重新解析")
        self.assertEqual(fake.selection_calls, [])

    def test_submit_api_keeps_legacy_submission_for_resource_without_file_tree(self):
        fake = FakeSelectionClient({
            "code": 0,
            "msg": "success",
            "data": {
                "resourceType": "http",
                "resourceName": "direct-download.iso",
                "url": "https://example.invalid/direct-download.iso",
                "size": 2147483648,
            },
        })
        with patch("app.routes.offline_api.GuangYaClient", return_value=fake):
            preview = self.client.post(
                "/api/offline/preview",
                headers=self.headers,
                json={
                    "url": "https://example.invalid/direct-download.iso",
                    "file_indexes": [],
                    "rules": self.rules_payload,
                },
            )
            self.assertIn("preview_id", preview.json())
            response = self.client.post(
                "/api/offline/submit",
                headers=self.headers,
                json={
                    "preview_id": preview.json()["preview_id"],
                    "url": "https://example.invalid/direct-download.iso",
                    "file_indexes": [],
                    "rules": self.rules_payload,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["selection_mode"], "legacy")
        self.assertEqual(len(fake.legacy_calls), 1)

    def test_submit_api_requires_preview_id(self):
        fake = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        with patch("app.routes.offline_api.GuangYaClient", return_value=fake):
            response = self.client.post(
                "/api/offline/submit",
                headers=self.headers,
                json={
                    "url": "magnet:?xt=urn:btih:no-preview",
                    "file_indexes": [0],
                    "rules": self.rules_payload,
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "缺少有效 preview_id，请重新解析")
        self.assertEqual(fake.selection_calls, [])

    def test_submit_api_rejects_changed_rules_and_consumes_snapshot(self):
        fake = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        with patch("app.routes.offline_api.GuangYaClient", return_value=fake):
            preview = self.client.post(
                "/api/offline/preview",
                headers=self.headers,
                json={
                    "url": "magnet:?xt=urn:btih:bound-rules",
                    "title": "Demo Release",
                    "rules": self.rules_payload,
                },
            ).json()
            self.assertIn("preview_id", preview)
            changed = {**self.rules_payload, "target_dir_id": "9002", "target_dir_name": "剧集"}
            rejected = self.client.post(
                "/api/offline/submit",
                headers=self.headers,
                json={
                    "preview_id": preview["preview_id"],
                    "url": "magnet:?xt=urn:btih:bound-rules",
                    "title": "Demo Release",
                    "file_indexes": [0],
                    "rules": changed,
                },
            )
            replay = self.client.post(
                "/api/offline/submit",
                headers=self.headers,
                json={
                    "preview_id": preview["preview_id"],
                    "url": "magnet:?xt=urn:btih:bound-rules",
                    "title": "Demo Release",
                    "file_indexes": [0],
                    "rules": self.rules_payload,
                },
            )

        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()["error"], "预览上下文已变化，请重新解析")
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["error"], "预览已过期或已使用，请重新解析")
        self.assertEqual(fake.selection_calls, [])

    def test_submit_api_partial_success_returns_207_and_blocks_preview_replay(self):
        fake = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        fake.selection_result = {
            "ok": False,
            "partial_success": True,
            "completed_batches": 1,
            "task_ids": ["task-a"],
            "completed_indexes": [0],
            "remaining_indexes": [1],
            "failed_batch": 2,
            "error": "cloud capacity limited",
            "selected_count": 2,
            "batch_count": 2,
            "responses": [],
        }
        permissive_rules = {**self.rules_payload, "exclude_keywords": "", "min_file_mb": 0}
        with patch("app.routes.offline_api.GuangYaClient", return_value=fake):
            preview = self.client.post(
                "/api/offline/preview",
                headers=self.headers,
                json={
                    "url": "magnet:?xt=urn:btih:partial-api",
                    "title": "Demo Release",
                    "rules": permissive_rules,
                },
            ).json()
            self.assertIn("preview_id", preview)
            body = {
                "preview_id": preview["preview_id"],
                "url": "magnet:?xt=urn:btih:partial-api",
                "title": "Demo Release",
                "file_indexes": [0, 1],
                "rules": permissive_rules,
            }
            response = self.client.post("/api/offline/submit", headers=self.headers, json=body)
            replay = self.client.post("/api/offline/submit", headers=self.headers, json=body)

        self.assertEqual(response.status_code, 207)
        payload = response.json()
        self.assertTrue(payload["partial_success"])
        self.assertEqual(payload["completed_indexes"], [0])
        self.assertEqual(payload["remaining_indexes"], [1])
        self.assertEqual(payload["failed_batch"], 2)
        self.assertEqual(replay.status_code, 409)


if __name__ == "__main__":
    unittest.main()
