from __future__ import annotations

from typing import Any, cast

from dataclasses import replace
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tests.support import InitializedWebTestCase

from app.clients.guangya import GuangYaClient
from app.config import web_credentials
from app.modules import offline


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
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def client_with_raw(test_case: unittest.TestCase, raw) -> GuangYaClient:
    temp_dir = tempfile.TemporaryDirectory()
    test_case.addCleanup(temp_dir.cleanup)
    client = GuangYaClient(token_file=Path(temp_dir.name) / "missing-token.json")
    raw.token = ""
    raw.refresh_token_value = ""
    raw.token_expires_at = None
    client._raw = raw
    return client


class GuangYaOfflineSelectionDomainTests(unittest.TestCase):

    def test_offline_rules_from_mapping_uses_current_fields(self):
        factory = getattr(offline.OfflineRules, "from_mapping", None)
        assert (factory) is not None

        rules = factory({"target_dir_id": "9001", "allowed_exts": "mkv,mp4"})

        self.assertEqual(rules.target_dir_id, "9001")
        self.assertEqual(rules.allowed_exts, ("mkv", "mp4"))

    def test_normalize_offline_files_accepts_field_variants_and_resolver_exclusions(self):
        normalize = getattr(GuangYaClient, "normalize_offline_files", None)
        assert (normalize) is not None, "GuangYaClient 应提供离线资源文件归一化"

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

        with self.assertRaisesRegex(ValueError, "文件索引缺失"):
            GuangYaClient.normalize_offline_files(response)

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

        with self.assertRaisesRegex(ValueError, "文件索引缺失"):
            GuangYaClient.normalize_offline_files(response)

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

        with self.assertRaisesRegex(ValueError, "文件索引缺失"):
            GuangYaClient.normalize_offline_files(response)

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
        assert (build_choices) is not None, "offline 模块应提供默认选集规则"
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
        client = client_with_raw(self, raw)
        submit = getattr(client, "add_offline_selection", None)
        assert (submit) is not None, "GuangYaClient 应提供选集创建接口"

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
        client = client_with_raw(self, raw)

        result = client.resolve_torrent(b"private-torrent-bytes")

        self.assertEqual(result, RESOLVE_SUBFILES_FIXTURE)
        raw.cloud_resolve_torrent.assert_called_once_with(b"private-torrent-bytes")

    def test_selected_task_submission_reports_second_batch_failure(self):
        raw = Mock()
        raw.request.side_effect = [
            FakeResponse({"code": 0, "msg": "success", "data": {"taskId": "task-a"}}),
            FakeResponse({"code": 503, "msg": "cloud capacity limited", "data": {}}),
        ]
        client = client_with_raw(self, raw)

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

    def test_selection_cancellation_preserves_only_accepted_batches(self):
        for accepted in (0, 1):
            with self.subTest(accepted=accepted):
                raw = Mock()
                client = client_with_raw(self, raw)
                self.addCleanup(client.close)
                raw.request.return_value = FakeResponse({"code": 0, "data": {"taskId": "task-a"}})
                result = client.add_offline_selection(
                    "magnet:?xt=urn:btih:cancelled-selection", "target", list(range(501)),
                    can_proceed=lambda: raw.request.call_count < accepted,
                )
                self.assertEqual(raw.request.call_count, accepted)
                self.assertFalse(result["ok"])
                self.assertEqual(result["partial_success"], bool(accepted))
                self.assertFalse(result["outcome_unknown"])
                self.assertEqual(result["completed_batches"], accepted)
                self.assertEqual(result["task_ids"], ["task-a"] if accepted else [])
                self.assertEqual(result["completed_indexes"], list(range(500)) if accepted else [])
                self.assertEqual(result["remaining_indexes"], [500] if accepted else list(range(501)))
                self.assertEqual(result["batch_count"], 2)
                self.assertEqual(result["error"], "操作已取消，未提交下载后端")

    def test_single_and_selection_check_cancellation_after_raw_refresh(self):
        for selection in (False, True):
            with self.subTest(selection=selection):
                raw = Mock()
                client = client_with_raw(self, raw)
                self.addCleanup(client.close)
                raw.token_expires_at = 1
                stopped = False

                def refresh():
                    nonlocal stopped
                    raw.token_expires_at = None
                    stopped = True

                raw.refresh_token.side_effect = refresh
                result = (
                    client.add_offline_selection("magnet:?xt=urn:btih:refresh-stop", "target", [0],
                                                 can_proceed=lambda: not stopped)
                    if selection else
                    client.add_offline_task("magnet:?xt=urn:btih:refresh-stop", "target",
                                            can_proceed=lambda: not stopped)
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["task_ids"], [])
                self.assertIn("操作已取消，未提交下载后端", result["error"])
                raw.refresh_token.assert_called_once_with()
                raw.request.assert_not_called()
                raw.cloud_create_task.assert_not_called()

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

        for selection in (False, True):
            for payload, expected_error in cases:
                with self.subTest(selection=selection, payload=payload):
                    raw = Mock()
                    raw.request.return_value = raw.cloud_create_task.return_value = FakeResponse(payload)
                    client = client_with_raw(self, raw)
                    self.addCleanup(client.close)
                    result = (
                        client.add_offline_selection("magnet:?xt=urn:btih:conservative-response", "target", [0])
                        if selection else
                        client.add_offline_task("magnet:?xt=urn:btih:conservative-response", "target")
                    )
                    self.assertFalse(result["ok"])
                    self.assertEqual(result.get("outcome_unknown", False), payload == {"data": {}})
                    self.assertEqual(result["task_ids"], [])
                    self.assertIn(expected_error, result["error"])
                    if selection:
                        self.assertEqual(result["completed_batches"], 0)
                        self.assertEqual(result["remaining_indexes"], [0])

    def test_unrecognized_create_receipts_remain_unknown(self):
        for payload in ([], {"code": "unrecognized"}, {"state": "pending"}):
            with self.subTest(payload=payload):
                raw = Mock()
                raw.request.return_value = FakeResponse(payload)
                client = client_with_raw(self, raw)
                self.addCleanup(client.close)
                result = client.add_offline_selection("magnet:?xt=urn:btih:unknown-ack", "target", [0])
                self.assertFalse(result["ok"])
                self.assertTrue(result["outcome_unknown"])
                self.assertFalse(result["partial_success"])
                self.assertEqual(result["task_ids"], [])

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
            {"data": {"taskID": ["task-a", "task-b"]}, "task_id": "task-a"},
        ]

        for payload in cases:
            with self.subTest(payload=payload):
                raw = Mock()
                raw.request.return_value = FakeResponse(payload)
                client = client_with_raw(self, raw)

                result = client.add_offline_selection(
                    "magnet:?xt=urn:btih:normalized-success",
                    "1927445875113771071",
                    [0],
                )

                self.assertTrue(result["ok"])
                self.assertEqual(result["completed_batches"], 1)
                self.assertEqual(result["remaining_indexes"], [])
                if "task_id" in payload:
                    self.assertEqual(result["task_ids"], ["task-a", "task-b"])



class FakeSelectionClient:
    close: Mock
    create_dir: Mock
    list_dir: Mock
    delete: Mock
    file_info: Mock
    supports_guarded_empty_directory_delete: bool
    delete_empty_directory: Mock

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

    def add_offline_selection(self, url: str, target_dir_id: str, file_indexes: list[int], *, can_proceed=None) -> dict:
        if can_proceed is not None and not can_proceed():
            return {"ok": False, "error": "操作已取消，未提交下载后端"}
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

    def add_offline_task(self, url: str, target_dir_id: str = "0", task_type: str = "magnet", *, can_proceed=None) -> dict:
        if can_proceed is not None and not can_proceed():
            return {"ok": False, "error": "操作已取消，未提交下载后端"}
        self.legacy_calls.append({
            "url": url,
            "target_dir_id": target_dir_id,
            "task_type": task_type,
        })
        return {"ok": True, "task_ids": [], "batch_count": 1, "error": "", "response": {"code": 0}}


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

    def test_prepared_submission_stop_blocks_files_and_verified_single_files_without_creating_staging(self):
        from tests.test_guangya_single_file_manifest import SINGLE

        for url, payload in (
            ("magnet:?xt=urn:btih:" + "a" * 40, RESOLVE_SUBFILES_FIXTURE),
            ("magnet:?xt=urn:btih:" + "a" * 40, SINGLE),
            ("https://fixture.invalid/Feature.mkv", RESOLVE_SUBFILES_FIXTURE),
            ("ed2k://|file|Feature.mkv|536870912|" + "a" * 32 + "|/", {}),
        ):
            with self.subTest(url=url, single=payload is SINGLE):
                client = FakeSelectionClient(payload)
                client.create_dir = Mock()
                with patch.object(offline.OfflineRules, "from_config", return_value=replace(self.rules, min_file_mb=50)):
                    result = offline.submit_offline(
                        url, client=cast(GuangYaClient, client), isolate_task=True,
                        can_proceed=lambda: False,
                    )
                self.assertFalse(result["ok"])
                self.assertIn("已取消", result["error"])
                self.assertEqual(len(client.resolve_calls), 1)
                client.create_dir.assert_not_called()
                self.assertEqual(client.selection_calls, [])
                self.assertEqual(client.legacy_calls, [])

    def test_ed2k_single_file_facts_obey_the_same_video_size_and_exclusion_rules(self):
        cases = (
            ("Feature%20%E7%89%87%E5%90%8D.mkv", 200 * 1024 * 1024, ""),
            ("readme.txt", 500 * 1024 * 1024, "扩展名不允许"),
            ("Small.mp4", 200 * 1024 * 1024 - 1, "小于 200 MB"),
            ("Feature.s%61mple.mkv", 500 * 1024 * 1024, "命中排除词"),
            ("Sample.mkv", 500 * 1024 * 1024, "样片不自动下载"),
        )
        for name, size, excluded_reason in cases:
            for isolated in (False, True):
                with self.subTest(name=name, isolated=isolated):
                    client = FakeSelectionClient({"code": 0, "data": {}})
                    client.create_dir = Mock(return_value="ed2k-staging")
                    rules = replace(self.rules, exclude_keywords=() if name == "Sample.mkv" else ("sample",))
                    with patch.object(offline.OfflineRules, "from_config", return_value=rules):
                        result = offline.submit_offline(
                            f"ed2k://|file|{name}|{size}|{'a' * 32}|/",
                            client=cast(GuangYaClient, client), isolate_task=isolated,
                        )
                    self.assertEqual(client.selection_calls, [])
                    if excluded_reason:
                        self.assertFalse(result["ok"])
                        self.assertIn(excluded_reason, result["error"])
                        self.assertEqual(result["selected_count"], 0)
                        self.assertEqual(result["excluded_count"], 1)
                        client.create_dir.assert_not_called()
                        self.assertEqual(client.legacy_calls, [])
                    else:
                        self.assertTrue(result["ok"], result)
                        self.assertEqual(result["selection_mode"], "single_file")
                        self.assertEqual(result["selected_count"], 1)
                        self.assertEqual(result["excluded_count"], 0)
                        self.assertFalse(result["unverified_manifest"])
                        self.assertEqual(len(client.legacy_calls), 1)
                        self.assertEqual(client.legacy_calls[0]["task_type"], "ed2k")
                        self.assertEqual(client.create_dir.call_count, int(isolated))

    def test_stop_after_staging_persistence_uses_the_same_rollback_once(self):
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        client.create_dir = Mock(return_value="staging-stopped")
        allowed = True

        def persist(snapshot):
            nonlocal allowed
            self.assertEqual(snapshot["id"], "staging-stopped")
            allowed = False

        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules), \
                patch.object(offline, "_remove_empty_staging", return_value=("retained", "目录非空")) as cleanup:
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:" + "a" * 40, client=cast(GuangYaClient, client),
                isolate_task=True, on_staging_created=persist, can_proceed=lambda: allowed,
            )
        self.assertFalse(result["ok"])
        self.assertIn("已取消", result["error"])
        self.assertEqual(result["staging"]["cleanup_status"], "retained")
        self.assertEqual(client.selection_calls, [])
        cleanup.assert_called_once_with(
            client, "staging-stopped", expected_parent_id="9001", expected_name=result["staging"]["name"],
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
                client=cast(GuangYaClient, client),
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

    def test_automatic_sample_only_manifest_does_not_create_a_download(self):
        client = FakeSelectionClient({"data": {"files": [
            {"fileIndex": 0, "name": "Sample.mkv", "size": 331776368},
        ]}})
        client.create_dir = Mock()
        rules = replace(self.rules, exclude_keywords=(), min_file_mb=50)
        with patch.object(offline.OfflineRules, "from_config", return_value=rules):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:sample-only", client=cast(GuangYaClient, client), isolate_task=True,
            )
        self.assertFalse(result["ok"])
        self.assertIn("样片", result["error"])
        client.create_dir.assert_not_called()
        self.assertEqual(client.selection_calls, [])

    def test_automatic_selection_skips_sample_without_rejecting_similarly_named_media(self):
        client = FakeSelectionClient({"data": {"files": [
            {"fileIndex": 0, "name": "Movie.1999.mkv", "size": 28255299040},
            {"fileIndex": 1, "name": "Sample.mkv", "size": 331776368},
            {"fileIndex": 2, "name": "Corps Samples (2021).mkv", "size": 331776368},
        ]}})
        with patch.object(offline.OfflineRules, "from_config", return_value=replace(self.rules, exclude_keywords=(), min_file_mb=50)):
            result = offline.submit_offline("magnet:?xt=urn:btih:sample-choice", client=cast(GuangYaClient, client))
        self.assertTrue(result["ok"], result)
        self.assertEqual(client.selection_calls[0]["file_indexes"], [0, 2])

    def test_incomplete_manifest_never_creates_staging_or_partial_download(self):
        client = FakeSelectionClient({"data": {"btResInfo": {
            "subfilesNum": 3,
            "subfiles": [
                {"fileName": "Feature.mkv", "fileSize": 28000000000},
                {"fileName": "Sample.mkv", "fileIndex": 4, "fileSize": 331776368},
            ],
        }}})
        client.create_dir = Mock()
        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:incomplete", client=cast(GuangYaClient, client), isolate_task=True,
            )
        self.assertFalse(result["ok"])
        self.assertIn("资源解析响应无效", result["error"])
        client.create_dir.assert_not_called()
        self.assertEqual(client.selection_calls, [])
        self.assertEqual(client.legacy_calls, [])

    def test_isolated_directory_is_persisted_before_provider_submission(self):
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        client.create_dir = Mock(return_value="staging-persisted")
        sequence: list[str] = []

        def persist(snapshot: dict) -> None:
            sequence.append("persist")
            self.assertEqual(snapshot["id"], "staging-persisted")
            self.assertEqual(snapshot["parent_id"], "9001")

        original_submit = client.add_offline_selection

        def submit(*args, **kwargs):
            sequence.append("submit")
            return original_submit(*args, **kwargs)

        client.add_offline_selection = Mock(side_effect=submit)
        with patch.object(offline.OfflineRules, "from_config", return_value=self.rules):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:persist-before-submit",
                title="Demo Release",
                client=cast(GuangYaClient, client),
                isolate_task=True,
                task_key="persisted",
                on_staging_created=persist,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(sequence, ["persist", "submit"])

    def test_staging_persistence_failure_prevents_provider_submission(self):
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        client.create_dir = Mock(return_value="staging-rejected")
        with patch.object(
            offline.OfflineRules, "from_config", return_value=self.rules
        ), patch.object(
            offline, "_remove_empty_staging", return_value=("completed", "")
        ) as cleanup:
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:persist-rejected",
                title="Demo Release",
                client=cast(GuangYaClient, client),
                isolate_task=True,
                task_key="rejected",
                on_staging_created=lambda _snapshot: (_ for _ in ()).throw(
                    RuntimeError("database unavailable")
                ),
            )

        self.assertFalse(result["ok"])
        self.assertIn("未提交离线任务", result["error"])
        self.assertEqual(client.selection_calls, [])
        cleanup.assert_called_once_with(
            client,
            "staging-rejected",
            expected_parent_id="9001",
            expected_name=result["staging"]["name"],
        )

    def test_empty_staging_cleanup_uses_version_guard_and_delete_audit(self):
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        info = SimpleNamespace(
            file_id="staging-clean",
            name="MF-clean",
            is_dir=True,
            parent_id="9001",
            size=0,
            etag="etag-clean",
            updated_at=123,
        )
        client.file_info = Mock(return_value=info)
        client.list_dir = Mock(return_value=[])
        client.supports_guarded_empty_directory_delete = True
        client.delete_empty_directory = Mock(return_value=True)

        def execute(_client, **kwargs):
            kwargs["delete_operation"]()
            return {"status": "success"}

        with patch(
            "app.modules.organize_delete_audit.execute_recycle_bin_delete",
            side_effect=execute,
        ) as audited_delete:
            status, error = offline._remove_empty_staging(
                cast(GuangYaClient, client),
                "staging-clean",
                expected_parent_id="9001",
                expected_name="MF-clean",
            )

        self.assertEqual((status, error), ("completed", ""))
        client.delete_empty_directory.assert_called_once_with(
            "staging-clean",
            expected_etag="etag-clean",
            expected_updated_at=123,
        )
        self.assertEqual(
            audited_delete.call_args.kwargs["trigger"],
            "offline_staging_rollback",
        )

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
                client=cast(GuangYaClient, client),
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
                client=cast(GuangYaClient, client),
                torrent_data=b"private-torrent-bytes",
            )

        self.assertFalse(result["ok"])
        self.assertIn("种子文件未解析到可验证文件列表", result["error"])
        self.assertNotIn("磁力资源连续", result["error"])
        self.assertEqual(client.torrent_resolve_calls, [b"private-torrent-bytes"] * 4)

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
                client=cast(GuangYaClient, client),
                isolate_task=True,
                task_key="unknown",
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["outcome_unknown"])
        self.assertTrue(result["tracking_incomplete"])
        self.assertFalse(result.get("retryable"))
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
                client=cast(GuangYaClient, client),
                isolate_task=True,
                task_key="timeout",
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["outcome_unknown"])
        self.assertFalse(result.get("retryable"))
        client.add_offline_selection.assert_called_once()
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
                client=cast(GuangYaClient, client),
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
                client=cast(GuangYaClient, client),
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
                client=cast(GuangYaClient, client),
                isolate_task=True,
                task_key="excluded",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_code"], "guangya_manifest_unavailable")
        self.assertTrue(result["retryable"])
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
                client=cast(GuangYaClient, client),
                isolate_task=False,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_code"], "guangya_manifest_unavailable")
        self.assertTrue(result["retryable"])
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
                client=cast(GuangYaClient, client),
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
                client=cast(GuangYaClient, client),
                isolate_task=True,
                task_key="9",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["resolve_attempts"], 2)
        self.assertEqual(client.resolve_url.call_count, 2)

    def test_automatic_rule_no_match_is_not_retryable(self):
        client = FakeSelectionClient(RESOLVE_SUBFILES_FIXTURE)
        rules = replace(
            self.rules,
            exclude_keywords=("demo.release", "sample", "trailer"),
            min_file_mb=0,
        )

        with patch.object(offline.OfflineRules, "from_config", return_value=rules):
            result = offline.submit_offline(
                "magnet:?xt=urn:btih:no-rule-match", client=cast(GuangYaClient, client),
            )

        self.assertFalse(result["ok"])
        self.assertIn("没有符合仅视频规则的文件", result["error"])
        self.assertFalse(result.get("retryable"))
        self.assertEqual(client.selection_calls, [])
        self.assertEqual(client.legacy_calls, [])

class GuangYaOfflineRulesApiTests(InitializedWebTestCase):
    def setUp(self):
        from app.main import create_app

        self.app = create_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)
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

    @staticmethod
    def _csrf(html: str) -> str:
        match = re.search(r'name="csrf-token" content="([^"]+)"', html)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

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

    def test_retired_mutations_are_not_registered_and_never_call_cloud(self):
        paths = self.app.openapi()["paths"]
        with patch.object(offline, "GuangYaClient") as client:
            for path in ("/api/offline/preview", "/api/offline/submit"):
                with self.subTest(path=path):
                    self.assertNotIn(path, paths)
                    response = self.client.post(path, headers=self.headers, json={"url": "magnet:?xt=urn:btih:" + "a" * 40})
                    self.assertEqual(response.status_code, 404)
            rules = self.client.get("/api/offline/rules")
        self.assertEqual(rules.status_code, 200)
        self.assertIn("protocols", rules.json())
        client.assert_not_called()

    def test_rules_view_requires_login(self):
        anonymous = TestClient(self.client.app, raise_server_exceptions=False)
        self.assertEqual(anonymous.get("/api/offline/rules").status_code, 401)



if __name__ == "__main__":
    unittest.main()
