"""解析 HTTP/传输错误不伪装成空清单，且任何失败都不创建远端任务。"""
from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient

import json
import unittest
from unittest.mock import Mock, patch

import httpx

from app.modules import offline
from app.modules.download_dispatcher import public_dispatch_summary
from tests.test_guangya_offline_selection import (
    RESOLVE_EXCLUDED_ONLY_FIXTURE,
    RESOLVE_SUBFILES_FIXTURE,
    FakeSelectionClient,
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

    def test_http_torrent_fetch_failures_are_not_cloud_resolution_failures(self):
        url = "https://example.invalid/movie.torrent?token=fixture-secret"
        failures = (
            (httpx.ReadTimeout("fixture-secret"), "torrent_fetch_timeout", True, "种子读取超时", 0),
            (self.http_error(403), "torrent_http_error", False, "HTTP 403", 403),
            (self.http_error(503), "torrent_http_error", True, "HTTP 503", 503),
        )
        for error, code, retryable, message, status in failures:
            for isolated in (False, True):
                with self.subTest(code=code, status=status, isolated=isolated):
                    client = self.client()
                    with patch("app.modules.rss._fetch_rss_payload", side_effect=error):
                        result = offline.submit_offline(url, client=cast(GuangYaClient, client), isolate_task=isolated)
                    self.assert_safe_failure(result, client, message)
                    self.assertEqual(result["failure_code"], code)
                    self.assertEqual(result["retryable"], retryable)
                    self.assertEqual(result["resolve_http_status"], status)
                    self.assertEqual(result["resolve_attempts"], 0)
                    self.assertEqual(client.resolve_calls, [])
                    self.assertEqual(client.torrent_resolve_calls, [])

    def test_torrent_http_failures_preserve_safe_status_in_submit_and_public_summary(self):
        expected_classification = {
            400: (None, None),
            401: ("guangya_auth_failed", False),
            403: ("guangya_auth_failed", False),
            415: (None, None),
            429: ("guangya_rate_limited", True),
            500: ("guangya_unavailable", True),
            502: ("guangya_unavailable", True),
            503: ("guangya_unavailable", True),
            504: ("guangya_unavailable", True),
        }
        for status in (400, 401, 403, 415, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                client = self.client(self.http_error(status))
                result = offline.submit_offline(self.url, client=cast(GuangYaClient, client), torrent_data=b"fixture", isolate_task=True)
                self.assert_safe_failure(result, client, f"HTTP {status}")
                self.assertEqual(
                    (result.get("failure_code"), result.get("retryable")),
                    expected_classification[status],
                )
                self.assertEqual(result["resolve_attempts"], 1)
                self.assertEqual(result["resolve_http_status"], status)
                self.assertEqual(result["resolve_error_type"], "HTTPStatusError")
                cast(Mock, client.resolve_torrent).assert_called_once_with(b"fixture")
                cast(Mock, client.resolve_url).assert_not_called()
                summary = public_dispatch_summary({
                    "succeeded": [], "failed": ["guangya"], "error": "guangya: " + result["error"],
                })
                self.assertEqual(summary["error"], f"光鸭资源解析请求失败（HTTP {status}）")

    def test_timeouts_transport_and_invalid_response_are_distinct(self):
        for error, message, failure_code, retryable in (
            (httpx.ReadTimeout("fixture-secret"), "光鸭资源解析请求超时", "guangya_unavailable", True),
            (TimeoutError("fixture-secret"), "光鸭资源解析请求超时", "guangya_unavailable", True),
            (httpx.ConnectError("fixture-secret"), "光鸭资源解析网络异常", "guangya_unavailable", True),
            (ConnectionError("fixture-secret"), "光鸭资源解析失败", "guangya_unavailable", True),
            (ValueError("fixture-secret"), "光鸭资源解析响应无效", None, None),
            (RuntimeError("fixture-secret"), "光鸭资源解析失败", None, None),
        ):
            with self.subTest(error=type(error).__name__):
                client = self.client(error)
                result = offline.submit_offline(self.url, client=cast(GuangYaClient, client), torrent_data=b"fixture", isolate_task=True)
                self.assert_safe_failure(result, client, message)
                self.assertEqual(result.get("failure_code"), failure_code)
                self.assertEqual(result.get("retryable"), retryable)
                self.assertEqual(result["resolve_error_type"], type(error).__name__)
                self.assertEqual(result["resolve_http_status"], 0)
                summary = public_dispatch_summary({"failed": ["guangya"], "error": result["error"]})
                self.assertEqual(summary["error"], message)

    def test_direct_and_isolated_submissions_share_safe_failures_without_http_replays(self):
        # 客户端已承担有界 HTTP 重试；外层磁力轮询只等待成功但尚无元数据的响应。
        for protocol_url in (self.url, "https://example.invalid/video.mp4", "ed2k://|file|video.mp4|42|hash|/"):
            for isolated in (False, True):
                with self.subTest(isolated=isolated, url=protocol_url.split(":")[0]):
                    client = self.client(self.http_error(403))
                    result = offline.submit_offline(protocol_url, client=cast(GuangYaClient, client), isolate_task=isolated)
                    self.assert_safe_failure(result, client, "HTTP 403")
                    self.assertEqual(result["failure_code"], "guangya_auth_failed")
                    self.assertFalse(result["retryable"])
                    cast(Mock, client.resolve_url).assert_called_once_with(protocol_url)
                    self.assertEqual(result["resolve_attempts"], 1)

    def test_empty_and_excluded_torrent_manifests_still_fail_closed(self):
        for payload in ({"data": {"files": []}}, RESOLVE_EXCLUDED_ONLY_FIXTURE):
            with self.subTest(payload=payload):
                client = self.client(payload=payload)
                result = offline.submit_offline(self.url, client=cast(GuangYaClient, client), torrent_data=b"fixture", isolate_task=True)
                self.assertFalse(result["ok"])
                self.assertIn("种子文件未解析到可验证文件列表", result["error"])
                self.assertEqual(result["failure_code"], "guangya_manifest_unavailable")
                self.assertTrue(result["retryable"])
                self.assertNotIn("resolve_error_type", result)
                self.assertEqual(result["resolve_attempts"], 4)
                self.assert_no_writes(client)

    def test_empty_and_resolver_excluded_magnet_manifests_are_retryable(self):
        for payload in ({"data": {"files": []}}, RESOLVE_EXCLUDED_ONLY_FIXTURE):
            with self.subTest(payload=payload):
                client = self.client(payload=payload)
                result = offline.submit_offline(self.url, client=cast(GuangYaClient, client))

                self.assertFalse(result["ok"])
                self.assertEqual(result["failure_code"], "guangya_manifest_unavailable")
                self.assertTrue(result["retryable"])
                self.assert_no_writes(client)

    def test_not_logged_in_is_nonretryable_auth_failure_without_resolution(self):
        for isolated in (False, True):
            with self.subTest(isolated=isolated):
                client = self.client(payload=RESOLVE_SUBFILES_FIXTURE)
                client.logged_in = False
                result = offline.submit_offline(self.url, client=cast(GuangYaClient, client), isolate_task=isolated)

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "光鸭未登录")
                self.assertEqual(result["failure_code"], "guangya_auth_failed")
                self.assertFalse(result["retryable"])
                self.assertEqual(client.resolve_calls, [])
                self.assertEqual(client.torrent_resolve_calls, [])
                self.assert_no_writes(client)

    def test_rss_auth_error_survives_auto_download_and_notification_outbox(self):
        from app import database as db
        from app.modules import rss, rss_scheduler, telegram_notification_center as center
        from app.notifier import render_event
        from app.repositories import rss as rss_repo
        from tests.support import isolated_test_database

        client = self.client(self.http_error(401))
        client.close = Mock()
        torrent = b"d4:infod6:lengthi10485760e4:name9:Movie.mkvee"
        with isolated_test_database():
            sid = rss_repo.add_rss_subscription(
                "蜜柑", "https://example.invalid/rss", action="download", download_method="guangya",
            )
            eid = rss_repo.add_rss_entry_with_media(
                sid, "测试剧集 E20", "auth-episode",
                payload=json.dumps({"torrent_url": "https://example.invalid/episode.torrent"}),
            )["id"]
            with (
                patch.object(rss.RSSEngine, "refresh", return_value={"total": 1, "new": 0}),
                patch.object(rss, "_fetch_rss_payload", return_value=(torrent, {})),
                patch.object(offline, "GuangYaClient", return_value=client),
                patch.object(center, "notification_target_chat_id", return_value="test-chat"),
                patch.object(center, "allows_notification", return_value=True),
                patch.object(center._dispatch_stop, "is_set", return_value=True),
            ):
                rss_scheduler.RSSScheduler()._execute(sid, "download")

            row = rss_repo.get_rss_entry(int(eid))
            assert row is not None
            self.assertEqual(row["failure_code"], "guangya_auth_failed")
            with db.get_conn() as conn:
                rows = conn.execute("SELECT event_json FROM telegram_notification_outbox").fetchall()
            self.assertEqual(len(rows), 1)
            rendered = render_event(center.deserialize_notification_event(rows[0]["event_json"]))
            self.assertIn("光鸭认证失败 · 1 条", rendered)
            self.assertIn("测试剧集 E20", rendered)
            self.assertIn("HTTP 401", rendered)
            self.assertNotIn("fixture-secret", rendered)
            self.assertNotIn("upstream-body", rendered)
            self.assert_no_writes(client)

    def test_valid_torrent_uses_only_verified_remote_indexes(self):
        client = self.client(payload=RESOLVE_SUBFILES_FIXTURE)
        result = offline.submit_offline(self.url, client=cast(GuangYaClient, client), torrent_data=b"fixture")
        self.assertTrue(result["ok"])
        self.assertEqual(client.torrent_resolve_calls, [b"fixture"])
        self.assertEqual(client.resolve_calls, [])
        self.assertEqual([item["file_indexes"] for item in client.selection_calls], [[0]])
        self.assertEqual(client.legacy_calls, [])

    def test_exception_after_excluded_response_never_reuses_previous_manifest(self):
        client = self.client()
        client.resolve_url = Mock(side_effect=[RESOLVE_EXCLUDED_ONLY_FIXTURE, self.http_error(503)])
        result = offline.submit_offline(self.url, client=cast(GuangYaClient, client), isolate_task=True)
        self.assert_safe_failure(result, client, "HTTP 503")
        self.assertEqual(result["resolve_attempts"], 2)
        self.assertEqual(client.resolve_url.call_count, 2)

    def test_duplicate_indexes_are_invalid_response_not_an_empty_manifest(self):
        client = self.client(payload={"data": {"files": [
            {"fileIndex": 0, "name": "a.mkv", "size": 100},
            {"fileIndex": 0, "name": "b.mkv", "size": 100},
        ]}})
        result = offline.submit_offline(self.url, client=cast(GuangYaClient, client), torrent_data=b"fixture")
        self.assert_safe_failure(result, client, "光鸭资源解析响应无效")

    def test_wrapped_http_error_is_classified_without_polling_again(self):
        error = RuntimeError("fixture-secret")
        error.__cause__ = self.http_error(415)
        client = self.client(error)
        result = offline.submit_offline(self.url, client=cast(GuangYaClient, client), isolate_task=True)
        self.assert_safe_failure(result, client, "HTTP 415")
        self.assertEqual(result["resolve_error_type"], "HTTPStatusError")
        cast(Mock, client.resolve_url).assert_called_once_with(self.url)

    def test_business_runtime_error_preserves_bounded_magnet_polling(self):
        client = self.client(RuntimeError("fixture-secret"))
        result = offline.submit_offline(self.url, client=cast(GuangYaClient, client), isolate_task=True)
        self.assert_safe_failure(result, client, "光鸭资源解析失败")
        self.assertEqual(result["resolve_attempts"], 4)
        self.assertEqual(cast(Mock, client.resolve_url).call_count, 4)
