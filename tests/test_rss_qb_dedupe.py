from __future__ import annotations

import unittest
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import database as db
from app.modules.rss import RSSEngine
from app.clients.qbittorrent import TorrentAddResult
from tests.support import IsolatedDatabaseTestCase


def _entry(entry_id: int, infohash: str, *, processed: bool = False, url: str = "") -> dict:
    return {
        "id": entry_id,
        "title": f"Episode {entry_id}",
        "payload": '{"torrent_url":"%s"}' % (url or f"https://mikanani.me/Download/20260801/{infohash}.torrent"),
        "status": "downloaded" if processed else "pending",
        "processed": 1 if processed else 0,
        "download_method": "qb",
        "qb_save_path": "",
        "gy_target_dir": "",
        "gy_target_dir_name": "",
        "rss_item_id": 1,
    }


class _QBClient:
    def __init__(self, hashes):
        self.hashes = set(hashes)
        self.added = []

    def list_torrents(self):
        return [SimpleNamespace(hash=value) for value in sorted(self.hashes)]

    def add_torrent_detailed(self, *, urls, save_path="", category=""):
        self.added.append(urls)
        return TorrentAddResult(True)


class RSSQBDedupeTests(unittest.TestCase):
    def test_extracts_infohash_from_magnet_and_torrent_url(self):
        value = "a" * 40
        self.assertEqual(
            RSSEngine._torrent_infohash(f"magnet:?xt=urn:btih:{value.upper()}"),
            value,
        )
        self.assertEqual(
            RSSEngine._torrent_infohash(f"https://mikanani.me/Download/x/{value}.torrent"),
            value,
        )
        full_v2_hash = "b" * 64
        self.assertEqual(
            RSSEngine._torrent_infohash(f"magnet:?xt=urn:btmh:1220{full_v2_hash}"),
            full_v2_hash[:40],
        )
        self.assertEqual(RSSEngine._torrent_infohash("https://example.com/file.torrent"), "")

    @patch("app.modules.rss.db.add_download_log")
    @patch("app.modules.rss.db.finalize_rss_qb_download", return_value=True)
    @patch(
        "app.modules.rss.db.claim_rss_qb_download",
        side_effect=lambda *_args, **_kwargs: {
            "status": "claimed", "lease_token": "lease"
        },
    )
    @patch("app.modules.rss.db.get_rss_entry")
    def test_batch_distinguishes_four_new_and_four_existing(
        self, get_entry, claim, finalize, add_log
    ):
        hashes = [f"{index:040x}" for index in range(1, 9)]
        rows = {index + 1: _entry(index + 1, value) for index, value in enumerate(hashes)}
        get_entry.side_effect = rows.get
        client = _QBClient(hashes[:4])
        engine = RSSEngine()

        with patch.object(engine, "_qb_client", return_value=client):
            result = engine.download_many(list(rows))

        self.assertEqual(result["success_count"], 4)
        self.assertEqual(result["existing_count"], 4)
        self.assertEqual(result["unverified_count"], 0)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(len(client.added), 4)
        self.assertEqual(claim.call_count, 8)
        self.assertEqual(finalize.call_count, 8)
        self.assertEqual(add_log.call_count, 8)

    @patch("app.modules.rss.db.add_download_log")
    @patch("app.modules.rss.db.update_rss_entry_status")
    @patch("app.modules.rss.db.claim_rss_entry")
    @patch("app.modules.rss.db.get_rss_entry")
    def test_reselecting_processed_entries_reports_existing_not_failure(
        self, get_entry, claim, update_status, add_log
    ):
        hashes = [f"{index:040x}" for index in range(1, 9)]
        rows = {index + 1: _entry(index + 1, value, processed=True) for index, value in enumerate(hashes)}
        get_entry.side_effect = rows.get
        client = _QBClient(hashes)
        engine = RSSEngine()

        with patch.object(engine, "_qb_client", return_value=client):
            result = engine.download_many(list(rows))

        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["existing_count"], 8)
        self.assertEqual(result["failure_count"], 0)
        claim.assert_not_called()
        update_status.assert_not_called()
        add_log.assert_not_called()
        self.assertEqual(client.added, [])

    @patch("app.modules.rss.db.get_rss_entry")
    def test_qb_snapshot_failure_is_fail_closed(self, get_entry):
        value = "f" * 40
        get_entry.return_value = _entry(1, value)
        client = MagicMock()
        client.list_torrents.side_effect = RuntimeError("offline")
        engine = RSSEngine()

        with patch.object(engine, "_qb_client", return_value=client), patch(
            "app.modules.rss.db.claim_rss_entry"
        ) as claim:
            result = engine.download_many([1])

        self.assertEqual(result["failure_count"], 1)
        self.assertIn("避免重复", result["failed"][0]["error"])
        claim.assert_not_called()
        client.add_torrent_detailed.assert_not_called()

    @patch("app.modules.rss.db.add_download_log")
    @patch("app.modules.rss.db.finalize_rss_qb_download", return_value=True)
    @patch(
        "app.modules.rss.db.claim_rss_qb_download",
        return_value={"status": "claimed", "lease_token": "lease"},
    )
    @patch("app.modules.rss.db.get_rss_entry")
    def test_unparseable_torrent_url_is_reported_as_unverified(
        self, get_entry, claim, finalize, add_log
    ):
        get_entry.return_value = _entry(
            1, "", url="https://example.com/download?id=opaque"
        )
        client = _QBClient([])
        engine = RSSEngine()

        with patch.object(engine, "_qb_client", return_value=client):
            result = engine.download_many([1])

        self.assertEqual(result["unverified_count"], 1)
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(len(client.added), 1)
        claim.assert_called_once()
        finalize.assert_called_once()


class RSSQBPersistentClaimTests(IsolatedDatabaseTestCase):
    def _entry(self, guid: str, infohash: str = "", *, url: str = "") -> tuple[int, int]:
        sub_id = db.add_rss_subscription(
            f"qb-{guid}", "https://example.invalid/rss", download_method="qb"
        )
        entry_id = db.add_rss_entry(
            sub_id, guid, guid,
            payload=json.dumps({
                "torrent_url": url or f"magnet:?xt=urn:btih:{infohash}"
            }),
        )
        self.assertIsNotNone(entry_id)
        return sub_id, int(entry_id)

    def test_same_infohash_unknown_result_blocks_second_entry(self):
        infohash = "a" * 40
        _sub1, first = self._entry("first", infohash)
        _sub2, second = self._entry("second", infohash)
        engine = RSSEngine()
        client = _QBClient([])
        unknown = TorrentAddResult(False, "qb_outcome_unknown", False)

        with patch.object(engine, "_qb_client", return_value=client), patch.object(
            engine, "_push_qb_detailed", return_value=unknown
        ) as push:
            first_result = engine.download(first)
            second_result = engine.download(second)

        self.assertFalse(first_result["ok"])
        self.assertTrue(first_result["review_required"])
        self.assertFalse(second_result["ok"])
        self.assertTrue(second_result["review_required"])
        self.assertIn("待核对", second_result["error"])
        push.assert_called_once()
        self.assertEqual(db.get_rss_entry(first)["failure_code"], "qb_outcome_unknown")
        self.assertEqual(db.get_rss_entry(second)["status"], "pending")

    def test_batch_preserves_unknown_outcome_review_details(self):
        infohash = "9" * 40
        _sub, entry_id = self._entry("batch-unknown", infohash)
        engine = RSSEngine()
        client = _QBClient([])

        with patch.object(engine, "_qb_client", return_value=client), patch.object(
            engine,
            "_push_qb_detailed",
            return_value=TorrentAddResult(False, "qb_outcome_unknown", False),
        ):
            result = engine.download_many([entry_id])

        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["outcome_unknown_count"], 1)
        self.assertTrue(result["review_required"])
        self.assertTrue(result["failed"][0]["review_required"])
        self.assertIn("勿直接重复提交", result["failed"][0]["error"])

    def test_agent_batch_busy_claim_remains_retryable(self):
        infohash = "8" * 40
        _sub, entry_id = self._entry("batch-busy", infohash)
        snapshot = db.get_pending_rss_qb_snapshot()
        expected = [{
            "id": int(row["id"]),
            "rss_item_id": int(row["rss_item_id"]),
            "title": str(row["title"] or ""),
            "payload": str(row["payload"] or ""),
            "created_at": str(row["created_at"] or ""),
            "download_method": str(row["download_method"] or ""),
            "qb_save_path": str(row["qb_save_path"] or ""),
        } for row in snapshot if int(row["id"]) == entry_id]
        claimed = db.claim_pending_rss_qb_entries(expected)
        self.assertEqual(len(claimed), 1)

        with patch(
            "app.modules.rss.db.claim_rss_qb_download",
            return_value={"status": "busy", "lease_token": ""},
        ):
            submitted, failed, unknown = RSSEngine()._submit_claimed_qb_rows(
                claimed,
                {"url": "http://qb.invalid", "timeout": 1},
            )

        self.assertEqual((submitted, failed, unknown), (0, 1, 0))
        entry = db.get_rss_entry(entry_id)
        self.assertEqual(entry["failure_code"], "qb_dedupe_busy")
        self.assertEqual(int(entry["failure_retryable"]), 1)

    def test_same_opaque_url_across_entries_is_submitted_once(self):
        url = "https://example.invalid/download?id=opaque-same"
        _sub1, first = self._entry("opaque-first", url=url)
        _sub2, second = self._entry("opaque-second", url=url)
        engine = RSSEngine()
        client = _QBClient([])

        with patch.object(engine, "_qb_client", return_value=client), patch.object(
            engine,
            "_push_qb_detailed",
            return_value=TorrentAddResult(True, "", False),
        ) as push:
            first_result = engine.download(first)
            second_result = engine.download(second)

        self.assertTrue(first_result["ok"])
        self.assertTrue(first_result["unverified"])
        self.assertTrue(second_result["ok"])
        self.assertTrue(second_result["existing"])
        push.assert_called_once()

    def test_opaque_unknown_claim_requires_owner_reset_before_retry(self):
        url = "https://example.invalid/download?id=opaque-unknown"
        _sub, first = self._entry("opaque-unknown", url=url)
        engine = RSSEngine()
        client = _QBClient([])

        with patch.object(engine, "_qb_client", return_value=client), patch.object(
            engine,
            "_push_qb_detailed",
            return_value=TorrentAddResult(False, "qb_outcome_unknown", False),
        ) as push:
            first_result = engine.download(first)
            blocked_result = engine.download(first)

        self.assertFalse(first_result["ok"])
        self.assertFalse(blocked_result["ok"])
        self.assertTrue(blocked_result["review_required"])
        push.assert_called_once()
        self.assertEqual(db.update_rss_entries_processed([first], False), 1)

        with patch.object(engine, "_qb_client", return_value=client), patch.object(
            engine,
            "_push_qb_detailed",
            return_value=TorrentAddResult(True, "", False),
        ) as retry_push:
            retry_result = engine.download(first)
        self.assertTrue(retry_result["ok"])
        retry_push.assert_called_once()

    def test_concurrent_entries_only_one_claims_same_infohash(self):
        infohash = "e" * 40
        _sub1, first = self._entry("concurrent-first", infohash)
        _sub2, second = self._entry("concurrent-second", infohash)
        barrier = Barrier(2)

        def claim(entry_id: int) -> dict:
            barrier.wait(timeout=2)
            return db.claim_rss_qb_download(infohash, entry_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, (first, second)))

        self.assertEqual(
            sorted(result["status"] for result in results),
            ["busy", "claimed"],
        )

    def test_claim_is_fenced_and_known_failure_can_retry(self):
        infohash = "b" * 40
        _sub, entry_id = self._entry("fenced", infohash)
        claim = db.claim_rss_qb_download(infohash, entry_id)
        self.assertEqual(claim["status"], "claimed")
        self.assertFalse(db.finalize_rss_qb_download(
            infohash, entry_id, "wrong", outcome="submitted"
        ))
        self.assertTrue(db.finalize_rss_qb_download(
            infohash, entry_id, claim["lease_token"], outcome="failed",
            failure_code="qb_unavailable", retryable=True,
        ))
        retry = db.claim_rss_qb_download(infohash, entry_id)
        self.assertEqual(retry["status"], "claimed")

    def test_existing_qb_snapshot_becomes_durable_submitted_claim(self):
        infohash = "c" * 40
        _sub1, first = self._entry("existing-first", infohash)
        _sub2, second = self._entry("existing-second", infohash)
        engine = RSSEngine()
        client = _QBClient([infohash])

        with patch.object(engine, "_qb_client", return_value=client):
            first_result = engine.download(first)
        self.assertTrue(first_result["ok"])
        self.assertTrue(first_result["existing"])
        self.assertEqual(client.added, [])

        # 即使下一次 qB 列表暂时未包含该任务，持久化 claim 仍阻止再次 add。
        empty_client = _QBClient([])
        with patch.object(engine, "_qb_client", return_value=empty_client):
            second_result = engine.download(second)
        self.assertTrue(second_result["ok"])
        self.assertTrue(second_result["existing"])
        self.assertEqual(empty_client.added, [])

    def test_stale_claim_requires_manual_reset_before_retry(self):
        infohash = "d" * 40
        _sub, entry_id = self._entry("stale", infohash)
        db.claim_rss_qb_download(infohash, entry_id)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_qb_download_claims SET updated_at='2000-01-01 00:00:00' "
                "WHERE infohash=?", (infohash,),
            )
        db.recover_stale_submitting_rss_entries(stale_minutes=15)
        self.assertEqual(db.get_rss_entry(entry_id)["failure_code"], "qb_outcome_unknown")
        self.assertEqual(db.claim_rss_qb_download(infohash, entry_id)["status"], "unknown")
        self.assertEqual(db.update_rss_entries_processed([entry_id], False), 1)
        self.assertEqual(db.claim_rss_qb_download(infohash, entry_id)["status"], "claimed")


class RSSClaimStateTests(IsolatedDatabaseTestCase):
    def _entry(self) -> int:
        sub_id = db.add_rss_subscription(
            "claim-state",
            "https://example.invalid/rss",
            download_method="qb",
        )
        entry_id = db.add_rss_entry(
            sub_id,
            "Episode",
            "claim-state-guid",
            payload=json.dumps({"torrent_url": "https://example.invalid/file.torrent"}),
        )
        self.assertIsNotNone(entry_id)
        return int(entry_id)

    def test_periodic_recovery_marks_stale_submitting_unknown(self):
        entry_id = self._entry()
        self.assertTrue(db.claim_rss_entry(entry_id))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET submitted_at=datetime('now','localtime','-16 minutes') "
                "WHERE id=?",
                (entry_id,),
            )

        self.assertEqual(db.recover_stale_submitting_rss_entries(), 1)

        row = db.get_rss_entry(entry_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["processed"], 0)
        self.assertEqual(row["failure_code"], "submission_outcome_unknown")
        self.assertEqual(row["failure_retryable"], 0)
        self.assertTrue(row["failed_at"])

    def test_periodic_recovery_keeps_recent_submitting_inflight(self):
        entry_id = self._entry()
        self.assertTrue(db.claim_rss_entry(entry_id))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET submitted_at=datetime('now','localtime','-14 minutes') "
                "WHERE id=?",
                (entry_id,),
            )

        self.assertEqual(db.recover_stale_submitting_rss_entries(), 0)
        self.assertEqual(db.get_rss_entry(entry_id)["status"], "submitting")

    def test_nonretryable_unknown_outcome_cannot_be_claimed_again(self):
        entry_id = self._entry()
        db.record_rss_entry_failure(entry_id, "qb_outcome_unknown", False)
        before = db.get_rss_entry(entry_id)

        self.assertFalse(db.claim_rss_entry(entry_id))

        after = db.get_rss_entry(entry_id)
        self.assertEqual(after["status"], "failed")
        self.assertEqual(after["failure_code"], "qb_outcome_unknown")
        self.assertEqual(after["failure_retryable"], 0)
        self.assertEqual(after["retry_count"], before["retry_count"])
        self.assertEqual(after["failed_at"], before["failed_at"])

    def test_retryable_failure_remains_manually_claimable(self):
        entry_id = self._entry()
        db.record_rss_entry_failure(entry_id, "qb_unavailable", True)

        self.assertTrue(db.claim_rss_entry(entry_id))

        row = db.get_rss_entry(entry_id)
        self.assertEqual(row["status"], "submitting")
        self.assertEqual(row["retry_count"], 1)
        self.assertEqual(row["failure_code"], "")
        self.assertEqual(row["failure_retryable"], 0)


if __name__ == "__main__":
    unittest.main()
