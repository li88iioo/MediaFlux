"""HTTP种子载体必须进入BT解析/选集提交，不能作为普通文件落盘。"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from app import database as db
from app.modules import offline
from app.modules.download_dispatcher import parse_torrent_metadata
from app.modules.rss import RSSEngine
from tests.support import isolated_test_database
from tests.test_guangya_offline_selection import FakeSelectionClient

TORRENT = b"d4:infod6:lengthi10485760e4:name9:Movie.mkvee"
INFOHASH = parse_torrent_metadata(TORRENT)[1]
TORRENT_URL = f"https://feed.invalid/download/{INFOHASH}.torrent?passkey=fixture-secret"
TREE = {
    "data": {
        "subfiles": [
            {"fileIndex": 0, "name": "Movie.mkv", "size": 10485760},
            {"fileIndex": 1, "name": "README.txt", "size": 100},
        ]
    }
}


class RssGuangyaTorrentInputTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.rules = offline.OfflineRules(
            True, True, False, "target", "测试目录", False, "0", "", (), (), 0, ("mkv",)
        )
        self.enterContext(
            patch.object(offline.OfflineRules, "from_config", return_value=self.rules)
        )
        self.fetch = self.enterContext(
            patch(
                "app.modules.rss._fetch_rss_payload",
                return_value=(TORRENT, {"content-type": "application/x-bittorrent"}),
            )
        )
        self.enterContext(
            patch(
                "socket.socket.connect",
                side_effect=AssertionError("unexpected network"),
            )
        )

    def client(self):
        client = FakeSelectionClient(TREE)
        client.create_dir = Mock(return_value="staging")
        client.close = Mock()
        return client

    def test_uploaded_torrent_retries_empty_manifest_then_submits_once(self):
        client = self.client()
        client.resolve_torrent = Mock(side_effect=[{}, TREE])

        with patch.object(offline, "get_int", return_value=3), patch.object(
            offline, "get", return_value="0"
        ), patch.object(offline.time, "sleep"):
            result = offline.submit_offline(
                TORRENT_URL, client=client, torrent_data=TORRENT
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["resolve_attempts"], 2)
        self.assertEqual(client.resolve_torrent.call_count, 2)
        self.assertEqual(client.selection_calls[0]["file_indexes"], [0])
        self.assertEqual(len(client.selection_calls), 1)
        client.create_dir.assert_not_called()
        self.assertEqual(client.legacy_calls, [])

    def test_uploaded_torrent_empty_manifest_exhaustion_fails_without_writes(self):
        client = self.client()
        client.resolve_torrent = Mock(side_effect=[{}, {}, {}])

        with patch.object(offline, "get_int", return_value=3), patch.object(
            offline, "get", return_value="0"
        ), patch.object(offline.time, "sleep"):
            result = offline.submit_offline(
                TORRENT_URL, client=client, torrent_data=TORRENT,
                isolate_task=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["resolve_attempts"], 3)
        self.assertEqual(client.resolve_torrent.call_count, 3)
        client.create_dir.assert_not_called()
        self.assertEqual(client.selection_calls, [])
        self.assertEqual(client.legacy_calls, [])
        self.assertIn("已阻止整单下载", result["error"])

    def test_uploaded_torrent_resolver_exception_is_not_retried(self):
        client = self.client()
        client.resolve_torrent = Mock(side_effect=RuntimeError("authentication rejected"))

        with patch.object(offline, "get_int", return_value=3), patch.object(
            offline, "get", return_value="0"
        ), patch.object(offline.time, "sleep"):
            result = offline.submit_offline(
                TORRENT_URL, client=client, torrent_data=TORRENT,
                isolate_task=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["resolve_attempts"], 1)
        client.resolve_torrent.assert_called_once_with(TORRENT)
        client.create_dir.assert_not_called()
        self.assertEqual(client.selection_calls, [])
        self.assertEqual(client.legacy_calls, [])

    def test_http_disabled_does_not_block_bt_torrent_link(self):
        client = self.client()
        result = offline.submit_offline(TORRENT_URL, client=client)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["decision"]["protocol"], "magnet")
        self.assertEqual(client.torrent_resolve_calls, [TORRENT])
        self.assertEqual(client.resolve_calls, [])
        self.assertEqual(client.legacy_calls, [])
        self.assertEqual(client.selection_calls[0]["file_indexes"], [0])
        self.assertTrue(
            client.selection_calls[0]["url"].startswith(
                "magnet:?xt=urn:btih:" + INFOHASH
            )
        )
        self.assertNotIn("fixture-secret", client.selection_calls[0]["url"])

    def test_real_rss_dispatch_keeps_http_identity_but_submits_torrent_contents(self):
        sub = db.add_rss_subscription(
            name="RSS BT",
            urls="https://feed.invalid/rss",
            download_method="guangya",
            gy_target_dir="rss-target",
            gy_target_dir_name="RSS目标",
        )
        entry = db.add_rss_entry_with_media(
            sub,
            "Movie Episode 1",
            "rss-guid",
            payload=json.dumps({"torrent_url": TORRENT_URL}),
        )["id"]
        client = self.client()
        normalize = offline.GuangYaClient.normalize_offline_files
        with patch.object(offline, "GuangYaClient", return_value=client) as factory:
            factory.normalize_offline_files = normalize
            result = RSSEngine().download(entry)
        self.assertTrue(result["ok"], result)
        row = db.get_download_request(result["request_id"])
        self.assertEqual(row["kind"], "http")
        self.assertEqual(row["source_value"], TORRENT_URL)
        self.assertEqual(row["gy_status"], "submitted")
        self.assertEqual(client.torrent_resolve_calls, [TORRENT])
        self.assertEqual(client.legacy_calls, [])
        self.assertEqual(client.selection_calls[0]["file_indexes"], [0])
        self.assertTrue(client.selection_calls[0]["url"].startswith("magnet:?"))
        self.assertEqual(client.create_dir.call_args.args[1], "rss-target")
        again = RSSEngine().download(entry)
        self.assertTrue(again["ok"])
        self.assertEqual(len(client.selection_calls), 1)

    def test_preview_and_manual_selection_use_same_torrent_conversion(self):
        for mode in ("preview", "manual"):
            with self.subTest(mode=mode):
                client = self.client()
                if mode == "preview":
                    result = offline.preview_offline_selection(
                        TORRENT_URL, client=client, rules=self.rules
                    )
                    self.assertEqual(result["default_selected_indexes"], [0])
                    self.assertEqual(client.selection_calls, [])
                else:
                    result = offline.submit_offline_selection(
                        TORRENT_URL, [0], client=client, rules=self.rules
                    )
                    self.assertEqual(client.selection_calls[0]["file_indexes"], [0])
                    self.assertTrue(
                        client.selection_calls[0]["url"].startswith("magnet:?")
                    )
                self.assertTrue(result["ok"], result)
                self.assertEqual(client.torrent_resolve_calls, [TORRENT])
                self.assertEqual(client.resolve_calls, [])
                self.assertEqual(client.legacy_calls, [])

    def test_bad_html_timeout_and_hash_mismatch_never_fall_back_to_http_task(self):
        for mode in ("automatic", "preview", "manual"):
            for bad in (
                b"<html>denied</html>",
                TORRENT.replace(b"Movie", b"Other"),
                TimeoutError("https://private.invalid/?passkey=fixture-secret"),
            ):
                with self.subTest(mode=mode, failure=type(bad).__name__):
                    client = self.client()
                    self.fetch.side_effect = bad if isinstance(bad, Exception) else None
                    self.fetch.return_value = (bad, {})
                    rules = replace(self.rules, http_enabled=True)
                    with patch.object(
                        offline.OfflineRules, "from_config", return_value=rules
                    ):
                        if mode == "automatic":
                            result = offline.submit_offline(
                                TORRENT_URL, client=client, isolate_task=True
                            )
                        elif mode == "preview":
                            result = offline.preview_offline_selection(
                                TORRENT_URL, client=client, rules=rules
                            )
                        else:
                            result = offline.submit_offline_selection(
                                TORRENT_URL, [0], client=client, rules=rules
                            )
                    self.assertFalse(result["ok"])
                    self.assertEqual(client.legacy_calls, [])
                    self.assertEqual(client.selection_calls, [])
                    self.assertEqual(client.torrent_resolve_calls, [])
                    client.create_dir.assert_not_called()
                    self.assertNotIn("fixture-secret", json.dumps(result))

    def test_http_enabled_cannot_bypass_disabled_bt_policy(self):
        rules = replace(self.rules, http_enabled=True, magnet_enabled=False)
        client = self.client()
        with patch.object(offline.OfflineRules, "from_config", return_value=rules):
            result = offline.submit_offline(TORRENT_URL, client=client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["decision"]["protocol"], "magnet")
        self.fetch.assert_not_called()
        self.assertEqual(client.legacy_calls, [])

    def test_regular_media_http_remains_an_http_file_download(self):
        client = FakeSelectionClient({})
        rules = replace(self.rules, http_enabled=True)
        with patch.object(offline.OfflineRules, "from_config", return_value=rules):
            result = offline.submit_offline(
                "https://feed.invalid/Movie.mkv", client=client
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["decision"]["protocol"], "http")
        self.fetch.assert_not_called()
        self.assertEqual(client.torrent_resolve_calls, [])
        self.assertEqual(
            client.legacy_calls[0]["url"], "https://feed.invalid/Movie.mkv"
        )

    def test_existing_bytes_are_reused_and_encoded_torrent_filenames_are_supported(
        self,
    ):
        for url in (
            "https://feed.invalid/Movie%2EtOrReNt?token=fixture-secret",
            "https://feed.invalid/download?filename=Movie.torrent",
        ):
            with self.subTest(url_shape=url.split("?", 1)[0]):
                client = self.client()
                result = offline.submit_offline(
                    url, client=client, torrent_data=TORRENT
                )
                self.assertTrue(result["ok"], result)
                self.fetch.assert_not_called()
                self.assertEqual(client.torrent_resolve_calls, [TORRENT])
                self.assertTrue(client.selection_calls[0]["url"].startswith("magnet:?"))

    def test_historical_http_request_can_retry_under_bt_policy(self):
        from app.modules.download_dispatcher import (
            create_request,
            download_resubmit_capabilities,
            normalize_download_url,
            resubmit_download_request,
        )

        row = create_request(
            normalize_download_url(TORRENT_URL), "", "", origin="rss:old"
        )
        request_id = int(row["id"])
        db.update_download_request(
            request_id, status="failed", gy_status="failed", error="HTTP 协议已禁用"
        )
        row = db.get_download_request(request_id)
        self.assertTrue(download_resubmit_capabilities(row)["guangya"]["enabled"])
        client = self.client()
        normalize = offline.GuangYaClient.normalize_offline_files
        with patch.object(offline, "GuangYaClient", return_value=client) as factory:
            factory.normalize_offline_files = normalize
            result = resubmit_download_request(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assertEqual(client.torrent_resolve_calls, [TORRENT])
        self.assertEqual(client.legacy_calls, [])
