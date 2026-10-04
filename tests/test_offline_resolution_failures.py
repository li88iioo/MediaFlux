"""解析 HTTP/传输错误不伪装成空清单，且任何失败都不创建远端任务。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

import httpx

from app.modules import offline
from app.modules.download_dispatcher import public_dispatch_summary
from tests.test_guangya_offline_selection import (
    FakeSelectionClient,
    RESOLVE_EXCLUDED_ONLY_FIXTURE,
    RESOLVE_SUBFILES_FIXTURE,
)


class OfflineResolutionFailureTests(unittest.TestCase):
    def setUp(self):
        self.url = "magnet:?xt=urn:btih:" + "a" * 40
        self.rules = offline.OfflineRules(
            magnet_enabled=True, ed2k_enabled=True, http_enabled=True,
            target_dir_id="fixture-target", target_dir_name="测试目录",
            secondary_enabled=False, secondary_dir_id="0", secondary_dir_name="",
            secondary_keywords=(), exclude_keywords=("sample",), min_file_mb=0,
            allowed_exts=("mkv", "mp4"),
        )
        rules_patch = patch.object(offline.OfflineRules, "from_config", return_value=self.rules)
        rules_patch.start()
        self.addCleanup(rules_patch.stop)
        sleep_patch = patch.object(offline.time, "sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        net_patch = patch("socket.socket.connect", side_effect=AssertionError("external network forbidden"))
        net_patch.start()
        self.addCleanup(net_patch.stop)

    def http_error(self, status):
        request = httpx.Request("POST", "https://example.invalid/private?token=fixture-secret")
        return httpx.HTTPStatusError(
            "unsafe fixture-secret upstream-body", request=request,
            response=httpx.Response(status, request=request, text="upstream-body"),
        )

    def client(self, error=None, payload=None):
        client = FakeSelectionClient(payload or {})
        client.create_dir = Mock()
        if error is not None:
            client.resolve_torrent = Mock(side_effect=error)
            client.resolve_url = Mock(side_effect=error)
        return client

    def assert_no_writes(self, client):
        client.create_dir.assert_not_called()
        self.assertEqual(client.selection_calls, [])
        self.assertEqual(client.legacy_calls, [])

    def assert_safe_failure(self, result, client, expected):
        self.assertFalse(result["ok"])
        self.assertIn(expected, result["error"])
        self.assertNotIn("未解析到可验证", result["error"])
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertNotIn("example.invalid", json.dumps(result))
        self.assertNotIn("upstream-body", json.dumps(result))
        self.assert_no_writes(client)

    def test_torrent_http_failures_preserve_safe_status_in_submit_and_public_summary(self):
        for status in (400, 401, 403, 415, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                client = self.client(self.http_error(status))
                result = offline.submit_offline(self.url, client=client, torrent_data=b"fixture", isolate_task=True)
                self.assert_safe_failure(result, client, f"HTTP {status}")
                self.assertEqual(result["resolve_attempts"], 1)
                self.assertEqual(result["resolve_http_status"], status)
                self.assertEqual(result["resolve_error_type"], "HTTPStatusError")
                client.resolve_torrent.assert_called_once_with(b"fixture")
                client.resolve_url.assert_not_called()
                summary = public_dispatch_summary({
                    "succeeded": [], "failed": ["guangya"], "error": "guangya: " + result["error"],
                })
                self.assertEqual(summary["error"], f"光鸭资源解析请求失败（HTTP {status}）")

    def test_timeouts_transport_and_invalid_response_are_distinct(self):
        for error, message in (
            (httpx.ReadTimeout("fixture-secret"), "光鸭资源解析请求超时"),
            (TimeoutError("fixture-secret"), "光鸭资源解析请求超时"),
            (httpx.ConnectError("fixture-secret"), "光鸭资源解析网络异常"),
            (ValueError("fixture-secret"), "光鸭资源解析响应无效"),
            (RuntimeError("fixture-secret"), "光鸭资源解析失败"),
        ):
            with self.subTest(error=type(error).__name__):
                client = self.client(error)
                result = offline.submit_offline(self.url, client=client, torrent_data=b"fixture", isolate_task=True)
                self.assert_safe_failure(result, client, message)
                self.assertEqual(result["resolve_error_type"], type(error).__name__)
                self.assertEqual(result["resolve_http_status"], 0)
                summary = public_dispatch_summary({"failed": ["guangya"], "error": result["error"]})
                self.assertEqual(summary["error"], message)

    def test_preview_manual_and_automatic_share_safe_failures_without_extra_http_replays(self):
        # 客户端已承担有界 HTTP 重试；外层磁力轮询只等待成功但尚无元数据的响应。
        for protocol_url in (self.url, "https://example.invalid/video.mp4", "ed2k://|file|video.mp4|42|hash|/"):
            for entry in ("automatic", "preview", "manual"):
                with self.subTest(entry=entry, url=protocol_url.split(":")[0]):
                    client = self.client(self.http_error(403))
                    if entry == "automatic":
                        result = offline.submit_offline(protocol_url, client=client, isolate_task=True)
                    elif entry == "preview":
                        result = offline.preview_offline_selection(protocol_url, client=client, rules=self.rules)
                    else:
                        result = offline.submit_offline_selection(protocol_url, selected_indexes=[0], client=client, rules=self.rules)
                    self.assert_safe_failure(result, client, "HTTP 403")
                    client.resolve_url.assert_called_once_with(protocol_url)
                    self.assertEqual(result["resolve_attempts"], 1)

    def test_empty_and_excluded_torrent_manifests_still_fail_closed(self):
        for payload in ({"data": {"files": []}}, RESOLVE_EXCLUDED_ONLY_FIXTURE):
            with self.subTest(payload=payload):
                client = self.client(payload=payload)
                result = offline.submit_offline(self.url, client=client, torrent_data=b"fixture", isolate_task=True)
                self.assertFalse(result["ok"])
                self.assertIn("种子文件未解析到可验证文件列表", result["error"])
                self.assertNotIn("resolve_error_type", result)
                self.assertEqual(result["resolve_attempts"], 4)
                self.assert_no_writes(client)

    def test_valid_torrent_uses_only_verified_remote_indexes(self):
        client = self.client(payload=RESOLVE_SUBFILES_FIXTURE)
        result = offline.submit_offline(self.url, client=client, torrent_data=b"fixture")
        self.assertTrue(result["ok"])
        self.assertEqual(client.torrent_resolve_calls, [b"fixture"])
        self.assertEqual(client.resolve_calls, [])
        self.assertEqual([item["file_indexes"] for item in client.selection_calls], [[0]])
        self.assertEqual(client.legacy_calls, [])

    def test_exception_after_excluded_response_never_reuses_previous_manifest(self):
        client = self.client()
        client.resolve_url = Mock(side_effect=[RESOLVE_EXCLUDED_ONLY_FIXTURE, self.http_error(503)])
        result = offline.submit_offline(self.url, client=client, isolate_task=True)
        self.assert_safe_failure(result, client, "HTTP 503")
        self.assertEqual(result["resolve_attempts"], 2)
        self.assertEqual(client.resolve_url.call_count, 2)

    def test_duplicate_indexes_are_invalid_response_not_an_empty_manifest(self):
        client = self.client(payload={"data": {"files": [
            {"fileIndex": 0, "name": "a.mkv", "size": 100},
            {"fileIndex": 0, "name": "b.mkv", "size": 100},
        ]}})
        result = offline.submit_offline(self.url, client=client, torrent_data=b"fixture")
        self.assert_safe_failure(result, client, "光鸭资源解析响应无效")

    def test_wrapped_http_error_is_classified_without_polling_again(self):
        error = RuntimeError("fixture-secret")
        error.__cause__ = self.http_error(415)
        client = self.client(error)
        result = offline.submit_offline(self.url, client=client, isolate_task=True)
        self.assert_safe_failure(result, client, "HTTP 415")
        self.assertEqual(result["resolve_error_type"], "HTTPStatusError")
        client.resolve_url.assert_called_once_with(self.url)

    def test_business_runtime_error_preserves_bounded_magnet_polling(self):
        client = self.client(RuntimeError("fixture-secret"))
        result = offline.submit_offline(self.url, client=client, isolate_task=True)
        self.assert_safe_failure(result, client, "光鸭资源解析失败")
        self.assertEqual(result["resolve_attempts"], 4)
        self.assertEqual(client.resolve_url.call_count, 4)
