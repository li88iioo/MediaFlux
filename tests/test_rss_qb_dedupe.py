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
from tests.support import seed_rss_entry_state


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
        entry_id = db.add_rss_entry_with_media(
            sub_id,
            f"Episode {guid}",
            guid,
            payload=json.dumps({"torrent_url": url}),
        )["id"]
        assert entry_id is not None
        if processed:
            seed_rss_entry_state(entry_id, "downloaded")
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
        self.assertTrue(db.get_rss_entry(first)["failure_retryable"])
        second_result = engine.download(second)

        self.assertFalse(first_result["ok"])
        self.assertEqual(db.get_rss_entry(first)["status"], "downloaded")
        self.assertFalse(db.get_rss_entry(first)["failure_retryable"])
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

    @patch("app.indexers.downloads.submit_download_input")
    def test_processed_entries_preserve_receipt_despite_missing_or_invalid_payload(self, enqueue) -> None:
        payloads = ("{broken-json", "null", "[]", "{}", '{"torrent_url":""}')
        for method in ("qb", "guangya"):
            sub_id = db.add_rss_subscription(
                name=method, urls="https://example.com/feed.xml", download_method=method,
            )
            for index, payload in enumerate(payloads):
                with self.subTest(method=method, payload=payload):
                    entry_id = self._entry(sub_id, f"processed-{index}", "", processed=True)
                    with db.get_conn() as conn:
                        conn.execute("UPDATE rss_entries SET payload=? WHERE id=?", (payload, entry_id))
                    before = dict(db.get_rss_entry(entry_id))
                    for _ in range(2):
                        result = RSSEngine().download(entry_id)
                        self.assertTrue(result["ok"])
                        self.assertTrue(result["existing"])
                        self.assertTrue(result["already_processed"])
                        self.assertEqual(result["method"], method)
                    self.assertEqual(dict(db.get_rss_entry(entry_id)), before)
        enqueue.assert_not_called()

    @patch("app.indexers.downloads.submit_download_input")
    def test_unprocessed_bad_payloads_keep_specific_failure_codes(self, enqueue) -> None:
        sub_id = self._subscription()
        for index, (payload, code) in enumerate((
            ("{broken-json", "invalid_payload"), ("null", "invalid_payload"),
            ("[]", "invalid_payload"), ("{}", "missing_torrent_url"),
        )):
            with self.subTest(payload=payload):
                entry_id = self._entry(sub_id, f"invalid-{index}", "")
                with db.get_conn() as conn:
                    conn.execute("UPDATE rss_entries SET payload=? WHERE id=?", (payload, entry_id))
                self.assertFalse(RSSEngine().download(entry_id)["ok"])
                row = db.get_rss_entry(entry_id)
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["failure_code"], code)
        enqueue.assert_not_called()

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



    def test_concurrent_duplicate_waits_for_actual_backend_receipt(self) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from app.modules import download_dispatcher as dispatcher

        for backend in ('qb', 'guangya'):
            for outcome in ('accepted', 'rejected', 'unknown'):
                with self.subTest(backend=backend, outcome=outcome):
                    _clear()
                    sid = db.add_rss_subscription('concurrent', 'https://fixture.invalid/rss', download_method=backend)
                    entries = [self._entry(sid, str(i), 'magnet:?xt=urn:btih:' + 'a' * 40) for i in range(2)]
                    entered, release = threading.Event(), threading.Event()

                    def submit(*_args, **_kwargs):
                        entered.set()
                        if not release.wait(10):
                            raise RuntimeError('fixture timeout')
                        return {'ok': outcome == 'accepted', 'task_id': 'task' if outcome == 'accepted' else '',
                                'outcome_unknown': outcome == 'unknown', 'failure_code': backend + '_rate_limited',
                                'retryable': outcome == 'rejected', 'error': '' if outcome == 'accepted' else 'fixture rejection'}

                    with patch.object(dispatcher, '_submit_qb' if backend == 'qb' else '_submit_guangya', side_effect=submit) as remote, ThreadPoolExecutor(max_workers=1) as pool:
                        first = pool.submit(RSSEngine().download, entries[0])
                        try:
                            self.assertTrue(entered.wait(5))
                            second = RSSEngine().download(entries[1])
                            same_entry = RSSEngine().download(entries[0])
                            before = dict(db.get_rss_entry(entries[1]))
                        finally:
                            release.set()
                        first = first.result()
                    remote.assert_called_once()
                    self.assertTrue(second.get('pending'), second)
                    self.assertTrue(same_entry.get('pending'), same_entry)
                    self.assertFalse(second['ok'])
                    self.assertEqual(second['status'], 'submitting')
                    self.assertEqual((before['status'], before['processed']), ('submitting', 0))
                    self.assertEqual(first['request_id'], second['request_id'])
                    final = [db.get_rss_entry(entry_id) for entry_id in entries]
                    self.assertEqual([row['status'] for row in final], ['downloaded' if outcome == 'accepted' else 'failed'] * 2)
                    self.assertEqual([row['processed'] for row in final], [int(outcome == 'accepted')] * 2)
                    if outcome != 'accepted':
                        self.assertEqual(final[0]['failure_code'], final[1]['failure_code'])
                        self.assertEqual(final[0]['failure_retryable'], final[1]['failure_retryable'])

    def test_manual_mark_cannot_override_a_submission_in_progress(self) -> None:
        from app.modules import download_dispatcher as dispatcher

        sid = self._subscription()
        entry = self._entry(sid, 'user-mark', 'magnet:?xt=urn:btih:' + 'b' * 40)
        def submit(*_args, **_kwargs):
            self.assertEqual(db.update_rss_entries_processed([entry], True), 0)
            return {'ok': True, 'task_id': 'b' * 40}
        with patch.object(dispatcher, '_submit_qb', side_effect=submit):
            result = RSSEngine().download(entry)
        self.assertTrue(result['ok'])
        self.assertEqual(db.get_rss_entry(entry)['status'], 'downloaded')


    def test_failure_feedback_ignores_superseded_request_and_other_backends(self) -> None:
        from app.repositories.rss import bind_rss_entry_download
        from app.modules.download_dispatcher import request_key

        sid = self._subscription()
        url = 'magnet:?xt=urn:btih:' + 'c' * 40
        key = request_key(normalize_download_url(url))
        entries = [self._entry(sid, str(i), url) for i in range(3)]
        old, _ = db.create_download_request(key, 'magnet', source_value=url)
        db.update_download_request(old, status='failed', qb_status='failed')
        new, _ = db.create_download_request(key, 'magnet', source_value=url)
        self.assertNotEqual(old, new)
        for entry_id, backend in zip(entries, ('qb', 'qb', 'guangya')):
            self.assertTrue(db.claim_rss_entry(entry_id))
            bind_rss_entry_download(entry_id, key, backend)
        db.record_rss_entry_failure(entries[0], 'qb_rate_limited', True, request_id=old)
        self.assertEqual([db.get_rss_entry(i)['status'] for i in entries], ['submitting'] * 3)
        db.record_rss_entry_failure(entries[0], 'qb_rate_limited', True, request_id=new)
        self.assertEqual([db.get_rss_entry(i)['status'] for i in entries], ['failed', 'failed', 'submitting'])


    def test_failure_feedback_covers_current_verified_content_aliases(self) -> None:
        from dataclasses import replace
        from app.repositories.rss import bind_rss_entry_download

        item = replace(normalize_download_url('https://fixture.invalid/seed.torrent'),
                       torrent_data=b'd4:infod6:lengthi10485760e4:name9:Movie.mkvee')
        keys = request_keys(item)
        self.assertEqual(len(keys), 2)
        request, _ = db.create_download_request(keys[0], 'http', source_value=item.source_value,
            torrent_data=item.torrent_data, alternate_request_keys=keys[1:])
        sid = self._subscription()
        entries = [self._entry(sid, str(i), item.source_value) for i in range(3)]
        for entry, key, backend in zip(entries, (keys[0], keys[1], keys[1]), ('qb', 'qb', 'guangya')):
            self.assertTrue(db.claim_rss_entry(entry))
            bind_rss_entry_download(entry, key, backend)
        db.record_rss_entry_failure(entries[0], 'qb_rate_limited', True, request_id=request)
        self.assertEqual([db.get_rss_entry(i)['status'] for i in entries], ['failed', 'failed', 'submitting'])


    def test_batch_and_confirmed_snapshot_do_not_count_inflight_as_submitted(self) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from app.modules import download_dispatcher as dispatcher

        sid = self._subscription()
        entries = [self._entry(sid, str(i), 'magnet:?xt=urn:btih:' + 'd' * 40) for i in range(4)]
        entered, release = threading.Event(), threading.Event()
        def submit(*_args, **_kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('fixture timeout')
            return {'ok': True, 'task_id': 'd' * 40}
        with patch.object(dispatcher, '_submit_qb', side_effect=submit) as remote, ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(RSSEngine().download, entries[0])
            try:
                self.assertTrue(entered.wait(5))
                engine = RSSEngine()
                batch = engine.download_many(entries[1:3])
                snapshot = engine.submit_snapshot(
                    [dict(row) for row in db.get_pending_rss_qb_snapshot()],
                    {'url': 'http://qb.local', 'default_method': 'qb'},
                    claim=db.claim_pending_rss_qb_entries,
                )
                self.assertEqual((batch['pending_count'], batch['success_count'], batch['existing_count'], batch['failure_count']), (2, 0, 0, 0))
                self.assertEqual((snapshot['pending'], snapshot['submitted'], snapshot['failed']), (1, 0, 0))
                self.assertFalse(snapshot['ok'])
                self.assertEqual([db.get_rss_entry(i)['status'] for i in entries], ['submitting'] * 4)
            finally:
                release.set()
            self.assertTrue(first.result()['ok'])
        remote.assert_called_once()
        self.assertEqual([db.get_rss_entry(i)['status'] for i in entries], ['downloaded'] * 4)


if __name__ == "__main__":
    unittest.main()
