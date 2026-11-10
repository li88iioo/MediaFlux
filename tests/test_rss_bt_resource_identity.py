"""RSS附件类型与BT内容身份：真实XML/SQLite，云盘及种子传输替身。"""

from __future__ import annotations
import json
from dataclasses import replace
from unittest.mock import patch
from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import rss as rss_repo
from app.modules import offline
from app.modules.rss import RSSEngine
from tests import test_rss_guangya_torrent_input as _fixtures

INFOHASH, TORRENT = _fixtures.INFOHASH, _fixtures.TORRENT


class FeedInputAudit(_fixtures.RssTorrentFixture):
    def pipeline(self, enc):
        xml = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Test feed</title><link>https://feed.invalid</link><description>Fixture</description><item><title>[Group] Example - 01 [1080p]</title><guid>item1</guid><link>https://feed.invalid/detail/1</link>{enc}</item></channel></rss>""".encode()

        def fetch(url, **kwargs):
            if str(url).endswith("/rss.xml"):
                return xml, {"content-type": "application/rss+xml"}
            return TORRENT, {"content-type": "application/x-bittorrent"}

        self.fetch.side_effect = fetch
        sub = rss_repo.add_rss_subscription(
            name="Audit feed",
            urls="https://feed.invalid/rss.xml",
            download_method="guangya",
            gy_target_dir="target",
            gy_target_dir_name="Test",
        )
        engine = RSSEngine()
        refreshed = engine.refresh(sub)
        self.assertEqual(refreshed["new"], 1, refreshed)
        with db.get_conn() as c:
            entry = c.execute(
                "SELECT id,payload FROM rss_entries WHERE rss_item_id=?", (sub,)
            ).fetchone()
        client = self.client()
        normalize = offline.GuangYaClient.normalize_offline_files
        with patch.object(offline, "GuangYaClient", return_value=client) as factory:
            factory.normalize_offline_files = normalize
            result = engine.download(int(entry["id"]))
        print(
            json.dumps(
                {
                    "case": self._testMethodName,
                    "stored_payload": json.loads(entry["payload"]),
                    "result": result,
                    "torrent_resolve_count": len(client.torrent_resolve_calls),
                    "ordinary_http_calls": len(client.legacy_calls),
                    "provider_resolve_urls": client.resolve_calls,
                    "submitted_urls": [call["url"] for call in client.selection_calls],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return result, client

    def test_control_torrent_extension_succeeds(self):
        result, client = self.pipeline(
            '<enclosure url="https://feed.invalid/1.torrent" type="application/x-bittorrent" length="42"/>'
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(client.torrent_resolve_calls, [TORRENT])

    def test_explicit_bittorrent_mime_with_dynamic_url_is_bt(self):
        result, client = self.pipeline(
            '<enclosure url="https://feed.invalid/download.php?id=1" type="application/x-bittorrent" length="42"/>'
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(client.torrent_resolve_calls, [TORRENT])

    def test_dynamic_torrent_with_http_enabled_never_downloads_seed_file(self):
        with patch.object(
            offline.OfflineRules,
            "from_config",
            return_value=replace(self.rules, http_enabled=True),
        ):
            result, client = self.pipeline(
                '<enclosure url="https://feed.invalid/download.php?id=1" type="application/x-bittorrent" length="42"/>'
            )
        self.assertEqual(client.resolve_calls, [], "种子仍作为普通HTTP链接发送给云盘")
        self.assertEqual(client.torrent_resolve_calls, [TORRENT])

    def test_bittorrent_enclosure_not_cover_image_is_selected(self):
        result, client = self.pipeline(
            '<enclosure url="https://feed.invalid/cover.jpg" type="image/jpeg" length="42"/><enclosure url="https://feed.invalid/1.torrent" type="application/x-bittorrent" length="42"/>'
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(client.torrent_resolve_calls, [TORRENT])


class ContentIdentityAudit(_fixtures.RssTorrentFixture):
    def pipeline(self, urls):
        client = self.client()
        normalize = offline.GuangYaClient.normalize_offline_files
        rows = []
        engine = RSSEngine()
        with patch.object(offline, "GuangYaClient", return_value=client) as factory:
            factory.normalize_offline_files = normalize
            for i, url in enumerate(urls):
                sub = rss_repo.add_rss_subscription(
                    name=f"Feed {i}",
                    urls=f"https://feed.invalid/rss/{i}",
                    download_method="guangya",
                    gy_target_dir="target",
                    gy_target_dir_name="Same target",
                )
                entry = rss_repo.add_rss_entry_with_media(
                    sub,
                    "Example S01E01",
                    f"guid-{i}",
                    payload=json.dumps({"torrent_url": url}),
                )["id"]
                result = engine.download(entry)
                self.assertTrue(result["ok"], result)
                rows.append(result)
        print(
            json.dumps(
                {
                    "case": self._testMethodName,
                    "requests": rows,
                    "submission_count": len(client.selection_calls),
                    "resolved_BT_urls": [r["url"] for r in client.selection_calls],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        self.assertEqual(
            len(client.selection_calls), 1, "同一BT内容经不同RSS载体重复调用云盘提交"
        )
        self.assertEqual(rows[0]["request_id"], rows[1]["request_id"])

    def test_control_hash_in_paths_deduplicates(self):
        self.pipeline(
            [
                f"https://one.invalid/{INFOHASH}.torrent",
                f"https://two.invalid/{INFOHASH}.torrent",
            ]
        )

    def test_torrent_url_without_hash_and_same_bt_magnet_deduplicate(self):
        self.pipeline(
            ["https://feed.invalid/Example.torrent", f"magnet:?xt=urn:btih:{INFOHASH}"]
        )

    def test_two_filename_torrent_links_with_same_bytes_deduplicate(self):
        self.pipeline(
            [
                "https://one.invalid/Example.torrent",
                "https://two.invalid/Example.torrent",
            ]
        )


class ContentIdentityConcurrencyTests(_fixtures.RssTorrentFixture):
    def test_parallel_http_sources_submit_one_cloud_task(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from app.indexers.downloads import submit_download_input
        from app.modules.download_dispatcher import normalize_download_url

        barrier = threading.Barrier(2)

        def fetch(*args, **kwargs):
            barrier.wait(timeout=5)
            return TORRENT, {"content-type": "application/x-bittorrent"}

        self.fetch.side_effect = fetch
        client = self.client()
        normalize = offline.GuangYaClient.normalize_offline_files
        with patch.object(offline, "GuangYaClient", return_value=client) as factory:
            factory.normalize_offline_files = normalize
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [
                    pool.submit(
                        submit_download_input,
                        normalize_download_url(
                            f"https://site-{i}.invalid/show.torrent"
                        ),
                        "guangya",
                        origin=f"rss:{i}",
                    )
                    for i in range(2)
                ]
                results = [job.result(timeout=10) for job in jobs]
        self.assertEqual(len({item["request_id"] for item in results}), 1)
        self.assertEqual(len(client.selection_calls), 1)
        self.assertEqual(client.create_dir.call_count, 1)

    def test_legacy_pending_http_request_cannot_bypass_existing_magnet_owner(self):
        from app.indexers.downloads import submit_download_input
        from app.modules.download_dispatcher import (
            create_request,
            dispatch_request,
            normalize_download_url,
        )

        legacy = create_request(
            normalize_download_url("https://fixture.invalid/show.torrent"), "", ""
        )
        client = self.client()
        normalize = offline.GuangYaClient.normalize_offline_files
        with patch.object(offline, "GuangYaClient", return_value=client) as factory:
            factory.normalize_offline_files = normalize
            first = submit_download_input(
                normalize_download_url(f"magnet:?xt=urn:btih:{INFOHASH}"),
                "guangya",
                origin="rss:existing",
            )
            late = dispatch_request(legacy["id"], "guangya")
        self.assertEqual(len(client.selection_calls), 1)
        self.assertEqual(client.create_dir.call_count, 1)
        self.assertFalse(late["ok"])
        self.assertEqual(
            late["results"]["guangya"]["existing_request_id"], first["request_id"]
        )

    def test_prepared_dynamic_torrent_survives_reusing_legacy_pending_request(self):
        from app.indexers.downloads import submit_download_input
        from app.modules.download_dispatcher import (
            create_request,
            normalize_download_url,
        )

        raw = normalize_download_url("https://fixture.invalid/download.php?id=17")
        legacy = create_request(raw, "", "")
        client = self.client()
        normalize = offline.GuangYaClient.normalize_offline_files
        with patch.object(offline, "GuangYaClient", return_value=client) as factory:
            factory.normalize_offline_files = normalize
            prepared = replace(raw, content_type="application/x-bittorrent")
            result = submit_download_input(prepared, "guangya", origin="rss:legacy")
            self.assertTrue(result["dispatch"]["ok"], result)
            duplicate = submit_download_input(
                normalize_download_url(f"magnet:?xt=urn:btih:{INFOHASH}"),
                "guangya",
                origin="rss:other",
            )
        self.assertTrue(duplicate["dispatch"]["duplicate"], duplicate)
        self.assertEqual(result["request_id"], legacy["id"])
        self.assertEqual(duplicate["request_id"], legacy["id"])
        self.assertEqual(client.torrent_resolve_calls, [TORRENT])
        self.assertEqual(len(client.selection_calls), 1)
        self.assertEqual(client.resolve_calls, [])

    def test_dynamic_torrent_retry_keeps_bt_type_after_blob_retention(self):
        from app.indexers.downloads import submit_download_input
        from app.modules.download_dispatcher import (
            normalize_download_url,
            resubmit_download_request,
        )

        raw = normalize_download_url("https://fixture.invalid/download.php?id=18")
        client = self.client()
        normalize = offline.GuangYaClient.normalize_offline_files
        with patch.object(offline, "GuangYaClient", return_value=client) as factory:
            factory.normalize_offline_files = normalize
            first = submit_download_input(
                replace(raw, content_type="application/x-bittorrent"),
                "guangya",
                origin="rss:retention",
            )
            self.assertTrue(first["dispatch"]["ok"], first)
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE download_requests SET status='failed',gy_status='failed',"
                    "updated_at=datetime('now','localtime','-31 days'),"
                    "completed_at=datetime('now','localtime','-31 days') WHERE id=?",
                    (first["request_id"],),
                )
            self.assertEqual(download_requests_repo.purge_expired_download_request_torrent_data(30), 1)
            stored = download_requests_repo.get_download_request(first["request_id"])
            assert stored is not None
            self.assertIsNone(stored["torrent_data"])
            self.assertEqual(stored["source_value"], raw.source_value)
            retried = resubmit_download_request(first["request_id"], "guangya")
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(client.torrent_resolve_calls, [TORRENT, TORRENT])
        self.assertEqual(
            len(client.selection_calls), 2
        )  # 显式用户重试，不是自动重复提交。
        self.assertEqual(client.resolve_calls, [])
