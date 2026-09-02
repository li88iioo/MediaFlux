from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import database as db
from app.clients.qbittorrent import TorrentAddResult, TorrentTask
from app.modules.download_dispatcher import normalize_download_url, request_keys
from app.modules.download_tracker import DownloadTracker
from app.modules.rss import RSSEngine
from tests.support import IsolatedDatabaseTestCase


def _clear() -> None:
    with db.get_conn() as conn:
        for table in (
            "download_log",
            "download_request_keys",
            "download_requests",
            "rss_entry_media",
            "rss_entries",
            "rss_items",
        ):
            conn.execute(f"DELETE FROM {table}")


class RSSQBUnifiedDownloadTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        _clear()
        config = {
            "QB_URL": "http://qb.local",
            "QB_USERNAME": "",
            "QB_PASSWORD": "",
            "QB_API_KEY": "",
        }
        self._dispatcher_config = patch(
            "app.modules.download_dispatcher.get",
            side_effect=lambda key, default="": config.get(key, default),
        )
        self._dispatcher_config.start()
        self.addCleanup(self._dispatcher_config.stop)

    @staticmethod
    def _subscription(name: str = "RSS", *, save_path: str = "/downloads") -> int:
        return db.add_rss_subscription(
            name=name,
            urls="https://example.com/feed.xml",
            download_method="qb",
            qb_save_path=save_path,
        )

    @staticmethod
    def _entry(sub_id: int, guid: str, url: str, *, processed: bool = False) -> int:
        entry_id = db.add_rss_entry(
            sub_id,
            f"Episode {guid}",
            guid,
            payload=json.dumps({"torrent_url": url}),
        )
        assert entry_id is not None
        if processed:
            db.update_rss_entry_status(entry_id, "downloaded")
        return entry_id

    @staticmethod
    def _task(infohash: str, *, progress: float = 1.0, state: str = "uploading") -> TorrentTask:
        return TorrentTask(
            hash=infohash,
            name="Episode",
            progress=progress,
            state=state,
            save_path="/downloads",
            content_path="/downloads/Episode.mkv",
            size=1,
            downloaded=1,
            dlspeed=0,
            upspeed=0,
            eta=0,
            ratio=0,
            category="rss",
            added_on=1,
        )

    def test_extracts_infohash_from_magnet_and_torrent_url(self) -> None:
        value = "a" * 40
        self.assertEqual(
            RSSEngine._torrent_infohash(f"magnet:?xt=urn:btih:{value.upper()}"),
            value,
        )
        self.assertEqual(
            RSSEngine._torrent_infohash(
                f"https://mikanani.me/Download/x/{value}.torrent?passkey=secret"
            ),
            value,
        )
        full_v2_hash = "b" * 64
        self.assertEqual(
            RSSEngine._torrent_infohash(f"magnet:?xt=urn:btmh:1220{full_v2_hash}"),
            full_v2_hash[:40],
        )
        self.assertEqual(
            RSSEngine._torrent_infohash("https://example.com/file.torrent"), ""
        )

    def test_identity_hint_cannot_override_non_http_identity(self) -> None:
        from app.modules.download_dispatcher import DownloadInput

        real_hash = "4" * 40
        spoofed_hash = "5" * 40
        item = DownloadInput(
            kind="magnet",
            title="Episode",
            source_value=f"magnet:?xt=urn:btih:{real_hash}",
            identity_hint=f"btih:{spoofed_hash}",
        )

        self.assertEqual(
            request_keys(item),
            request_keys(normalize_download_url(item.source_value)),
        )

    def test_http_torrent_btih_hint_shares_request_identity_with_magnet(self) -> None:
        infohash = "c" * 40
        entry = {
            "title": "Episode",
        }
        item = RSSEngine._download_input(
            entry,
            f"https://mikanani.me/Download/x/{infohash}.torrent?passkey=secret",
        )
        magnet = normalize_download_url(f"magnet:?xt=urn:btih:{infohash}")
        self.assertEqual(request_keys(item)[0], request_keys(magnet)[0])
        self.assertEqual(item.kind, "http")
        self.assertIn("passkey=secret", item.source_value)

    @patch("app.modules.download_dispatcher.close_qbittorrent_client")
    @patch("app.modules.download_dispatcher.QBittorrentClient")
    def test_same_infohash_across_feeds_creates_one_tracked_request(
        self, client_cls, close_client
    ) -> None:
        client = client_cls.return_value
        client.add_torrent_detailed.return_value = TorrentAddResult(True)
        infohash = "d" * 40
        first_sub = self._subscription("first")
        second_sub = self._subscription("second")
        first = self._entry(
            first_sub,
            "first",
            f"https://mikanani.me/Download/a/{infohash}.torrent?token=one",
        )
        second = self._entry(
            second_sub,
            "second",
            f"https://mikanani.me/Download/b/{infohash}.torrent?token=two",
        )

        result = RSSEngine().download_many([first, second])

        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["existing_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        client.add_torrent_detailed.assert_called_once()
        request_id = int((result["succeeded"] + result["existing"])[0]["request_id"])
        self.assertTrue(request_id)
        request = db.get_download_request(request_id)
        self.assertEqual(request["origin"], f"rss:{first_sub}")
        self.assertEqual(request["qb_task_id"], infohash)
        self.assertEqual(request["qb_status"], "submitted")
        self.assertEqual(db.get_rss_entry(first)["status"], "downloaded")
        self.assertEqual(db.get_rss_entry(second)["status"], "downloaded")
        logs = db.list_download_logs(source="qb", limit=10)
        self.assertEqual(len(logs), 2)
        self.assertEqual({int(row["request_id"]) for row in logs}, {request_id})
        self.assertTrue(all("token=" not in str(row["path"] or "") for row in logs))
        close_client.assert_called_once()

    @patch("app.modules.download_dispatcher.close_qbittorrent_client")
    @patch("app.modules.download_dispatcher.QBittorrentClient")
    def test_unknown_qb_outcome_blocks_duplicate_without_second_submission(
        self, client_cls, _close_client
    ) -> None:
        client_cls.return_value.add_torrent_detailed.return_value = TorrentAddResult(
            False, "qb_outcome_unknown", False
        )
        infohash = "e" * 40
        sub_id = self._subscription()
        first = self._entry(sub_id, "first", f"magnet:?xt=urn:btih:{infohash}")
        second = self._entry(sub_id, "second", f"magnet:?xt=urn:btih:{infohash}")
        engine = RSSEngine()

        first_result = engine.download(first)
        second_result = engine.download(second)

        self.assertFalse(first_result["ok"])
        self.assertTrue(first_result["review_required"])
        self.assertFalse(second_result["ok"])
        self.assertTrue(second_result["review_required"])
        client_cls.return_value.add_torrent_detailed.assert_called_once()
        self.assertEqual(db.get_rss_entry(first)["failure_code"], "qb_outcome_unknown")
        self.assertEqual(db.get_rss_entry(second)["failure_code"], "qb_outcome_unknown")

    @patch("app.modules.download_dispatcher.close_qbittorrent_client")
    @patch("app.modules.download_dispatcher.QBittorrentClient")
    def test_known_retryable_failure_allows_a_new_request_attempt(
        self, client_cls, _close_client
    ) -> None:
        client_cls.return_value.add_torrent_detailed.side_effect = (
            TorrentAddResult(False, "qb_rate_limited", True),
            TorrentAddResult(True),
        )
        infohash = "f" * 40
        sub_id = self._subscription()
        first = self._entry(sub_id, "first", f"magnet:?xt=urn:btih:{infohash}")
        second = self._entry(sub_id, "second", f"magnet:?xt=urn:btih:{infohash}")
        engine = RSSEngine()

        first_result = engine.download(first)
        second_result = engine.download(second)

        self.assertFalse(first_result["ok"])
        self.assertTrue(db.get_rss_entry(first)["failure_retryable"])
        self.assertTrue(second_result["ok"])
        self.assertNotEqual(first_result["request_id"], second_result["request_id"])
        self.assertEqual(client_cls.return_value.add_torrent_detailed.call_count, 2)

    @patch("app.modules.download_dispatcher.close_qbittorrent_client")
    @patch("app.modules.download_dispatcher.QBittorrentClient")
    def test_http_torrent_without_identity_is_visible_as_unverified(
        self, client_cls, _close_client
    ) -> None:
        client_cls.return_value.add_torrent_detailed.return_value = TorrentAddResult(True)
        sub_id = self._subscription()
        entry_id = self._entry(
            sub_id, "opaque", "https://example.com/download/file.torrent?token=secret"
        )

        result = RSSEngine().download(entry_id)

        self.assertTrue(result["ok"])
        self.assertTrue(result["unverified"])
        request = db.get_download_request(result["request_id"])
        self.assertEqual(request["qb_task_id"], "")
        log = db.list_download_logs(source="qb", limit=1)[0]
        self.assertNotIn("secret", str(log["path"] or ""))

    @patch("app.modules.download_dispatcher.QBittorrentClient")
    def test_reselecting_processed_entry_does_not_resubmit(self, client_cls) -> None:
        infohash = "1" * 40
        sub_id = self._subscription()
        entry_id = self._entry(
            sub_id,
            "processed",
            f"magnet:?xt=urn:btih:{infohash}",
            processed=True,
        )

        result = RSSEngine().download(entry_id)

        self.assertTrue(result["ok"])
        self.assertTrue(result["existing"])
        client_cls.assert_not_called()

    @patch.object(DownloadTracker, "_notify_completion")
    @patch.object(DownloadTracker, "_start_local_import")
    def test_local_path_never_overrides_incomplete_qb_api_state(
        self, start_local_import, _notify_completion
    ) -> None:
        infohash = "6" * 40
        from app.modules.download_dispatcher import DownloadInput, create_request

        created = create_request(
            DownloadInput(
                kind="magnet",
                title="Episode",
                source_value=f"magnet:?xt=urn:btih:{infohash}",
            ),
            "",
            "",
            origin="rss:1",
        )
        request_id = int(created["id"])
        db.update_download_request(
            request_id,
            status="submitted",
            targets="qb",
            qb_status="submitted",
            qb_task_id=infohash,
        )
        tracker = DownloadTracker()

        tracker._update_request(
            db.get_download_request(request_id),
            [self._task(infohash, progress=0.99, state="downloading")],
            [],
            qb_available=True,
            gy_available=False,
        )

        self.assertEqual(db.get_download_request(request_id)["qb_status"], "downloading")
        start_local_import.assert_not_called()

    @patch.object(DownloadTracker, "_notify_completion")
    @patch.object(DownloadTracker, "_start_local_import")
    def test_qb_api_state_drives_completion_before_disk_import(
        self, start_local_import, _notify_completion
    ) -> None:
        infohash = "2" * 40
        from app.modules.download_dispatcher import DownloadInput, create_request

        created = create_request(
            DownloadInput(
                kind="magnet",
                title="Episode",
                source_value=f"magnet:?xt=urn:btih:{infohash}",
            ),
            "",
            "",
            origin="rss:1",
        )
        request_id = int(created["id"])
        db.update_download_request(
            request_id,
            status="submitted",
            targets="qb",
            qb_status="submitted",
            qb_task_id=infohash,
        )
        tracker = DownloadTracker()

        tracker._update_request(
            db.get_download_request(request_id),
            [self._task(infohash)],
            [],
            qb_available=True,
            gy_available=False,
        )

        request = db.get_download_request(request_id)
        self.assertEqual(request["qb_status"], "completed")
        start_local_import.assert_called_once()
        matched_task = start_local_import.call_args.args[1]
        self.assertEqual(matched_task.content_path, "/downloads/Episode.mkv")

    def test_fresh_database_has_no_legacy_rss_backend_claim_tables(self) -> None:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('rss_qb_download_claims','rss_guangya_download_claims')"
            ).fetchall()

        self.assertEqual(rows, [])

    def test_stale_rss_submission_becomes_manual_review_without_backend_claims(self) -> None:
        sub_id = self._subscription()
        entry_id = self._entry(
            sub_id, "stale", "magnet:?xt=urn:btih:" + "3" * 40
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET status='submitting',submitted_at='2000-01-01 00:00:00' "
                "WHERE id=?",
                (entry_id,),
            )

        self.assertEqual(db.recover_stale_submitting_rss_entries(stale_minutes=15), 1)
        entry = db.get_rss_entry(entry_id)
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["failure_code"], "submission_outcome_unknown")
        self.assertFalse(entry["failure_retryable"])


if __name__ == "__main__":
    unittest.main()
