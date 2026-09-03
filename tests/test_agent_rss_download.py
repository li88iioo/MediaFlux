"""Media Agent 待处理 RSS 条目安全提交的确认、竞态与脱敏回归。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app import database as db
from app.agent.rss_download_actions import (
    prepare_rss_pending_download,
    submit_pending_rss_to_qb_confirmed,
)
from app.clients.qbittorrent import QBittorrentClient, TorrentAddResult
from app.modules.rss import RSSEngine
from tests.support import IsolatedDatabaseTestCase


def _clear_rss() -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM download_log")
        conn.execute("DELETE FROM download_request_keys")
        conn.execute("DELETE FROM download_requests")
        conn.execute("DELETE FROM rss_entries")
        conn.execute("DELETE FROM rss_items")


class RssPendingDownloadUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        _clear_rss()
        self.runtime = {
            "url": "http://qb.internal:8080",
            "username": "agent-user",
            "password": "QB_SECRET_PASSWORD",
            "api_key": "QB_SECRET_API_KEY",
            "category": "rss-agent",
            "default_save_path": "/private/downloads",
            "default_method": "qb",
            "timeout": 10,
        }
        self.runtime_patcher = patch(
            "app.modules.rss.capture_rss_qb_runtime_config",
            return_value=(self.runtime, ""),
        )
        self.runtime_patcher.start()

    def tearDown(self):
        self.runtime_patcher.stop()

    @staticmethod
    def _subscription(name: str = "Private RSS", method: str = "qb") -> int:
        return db.add_rss_subscription(
            name=name,
            urls="https://secret.example/rss?passkey=RSS_SECRET",
            download_method=method,
            qb_save_path="/private/subscription/path",
        )

    @staticmethod
    def _entry(sub_id: int, index: int, *, url: bool = True) -> int:
        payload = (
            json.dumps(
                {
                    "torrent_url": f"magnet:?xt=urn:btih:{index:040x}&dn=PRIVATESECRET{index}"
                }
            )
            if url
            else "{}"
        )
        entry_id = db.add_rss_entry(
            sub_id, f"Private Episode {index}", f"secret-guid-{index}", payload=payload
        )
        assert entry_id is not None
        return entry_id

    def test_preview_selects_latest_pending_qb_only_and_is_sanitized(self):
        qb_sub = self._subscription()
        gy_sub = self._subscription("GuangYa RSS", "guangya")
        ids = [self._entry(qb_sub, index) for index in range(1, 5)]
        self._entry(gy_sub, 90)
        db.update_rss_entry_status(ids[0], "downloaded")
        with patch(
            "app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed"
        ) as add:
            result, _context = prepare_rss_pending_download({"limit": 2})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["selected_count"], 2)
        self.assertTrue(result.data["has_more"])
        add.assert_not_called()
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "Private Episode",
            "secret-guid",
            "SECRET",
            "private/downloads",
            "qb.internal",
            "agent-user",
            "rss-agent",
            "passkey",
        ):
            self.assertNotIn(secret, serialized)

    def test_confirmation_context_freezes_exact_rows_and_returns_aggregate_only(self):
        sub_id = self._subscription()
        first = self._entry(sub_id, 1)
        second = self._entry(sub_id, 2)
        fingerprint = prepare_rss_pending_download({"limit": 2})[1]
        self.assertEqual(len(fingerprint), 64)
        raw = {
            "ok": True,
            "conflict": False,
            "requested": 2,
            "claimed": 2,
            "submitted": 1,
            "failed": 1,
            "error": "QB_SECRET /private/path",
        }
        with patch.object(
            RSSEngine, "submit_pending_qb_snapshot", return_value=raw
        ) as submit:
            result = submit_pending_rss_to_qb_confirmed({"limit": 2}, fingerprint)
        expected_rows, runtime = submit.call_args.args
        self.assertEqual([item["id"] for item in expected_rows], [second, first])
        self.assertEqual(runtime, self.runtime)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(
            result.data,
            {
                "target": "qbittorrent",
                "requested": 2,
                "claimed": 2,
                "submitted": 1,
                "failed": 1,
            },
        )
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("QB_SECRET", serialized)
        self.assertNotIn("/private", serialized)

    def test_unknown_submission_requires_qb_review_before_retry(self):
        sub_id = self._subscription()
        self._entry(sub_id, 1)
        raw = {
            "ok": False,
            "conflict": False,
            "requested": 1,
            "claimed": 1,
            "submitted": 0,
            "failed": 1,
            "outcome_unknown": 1,
        }
        fingerprint = prepare_rss_pending_download({"limit": 1})[1]
        with patch.object(RSSEngine, "submit_pending_qb_snapshot", return_value=raw):
            result = submit_pending_rss_to_qb_confirmed({"limit": 1}, fingerprint)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.data["outcome_unknown"], 1)
        self.assertIn("待核对 1", result.summary)
        self.assertIn("勿直接重试", result.error)
        self.assertIn("勿直接重复提交", result.suggestions[0])

    def test_mixed_unknown_submission_reports_all_three_outcomes(self):
        sub_id = self._subscription()
        for index in range(1, 4):
            self._entry(sub_id, index)
        raw = {
            "ok": False,
            "conflict": False,
            "requested": 3,
            "claimed": 3,
            "submitted": 1,
            "failed": 2,
            "outcome_unknown": 1,
        }
        fingerprint = prepare_rss_pending_download({"limit": 3})[1]
        with patch.object(RSSEngine, "submit_pending_qb_snapshot", return_value=raw):
            result = submit_pending_rss_to_qb_confirmed({"limit": 3}, fingerprint)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertIn("成功 1", result.summary)
        self.assertIn("待核对 1", result.summary)
        self.assertIn("确认失败 1", result.summary)
        self.assertIn("勿直接重复提交", result.suggestions[0])

    def test_database_claim_is_all_or_nothing_and_pending_only(self):
        sub_id = self._subscription()
        first = self._entry(sub_id, 1)
        second = self._entry(sub_id, 2)
        rows = db.get_pending_rss_qb_snapshot(default_method="qb", limit=2)
        expected = [
            {
                "id": int(row["id"]),
                "rss_item_id": int(row["rss_item_id"]),
                "title": str(row["title"] or ""),
                "payload": str(row["payload"] or ""),
                "created_at": str(row["created_at"] or ""),
                "download_method": str(row["download_method"] or ""),
                "qb_save_path": str(row["qb_save_path"] or ""),
            }
            for row in rows
        ]
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET payload='{}' WHERE id=?", (first,))
        self.assertEqual(db.claim_pending_rss_qb_entries(expected), [])
        self.assertEqual(db.get_rss_entry(first)["status"], "pending")
        self.assertEqual(db.get_rss_entry(second)["status"], "pending")
        fresh = db.get_pending_rss_qb_snapshot(default_method="qb", limit=2)
        fresh_expected = [
            {
                "id": int(row["id"]),
                "rss_item_id": int(row["rss_item_id"]),
                "title": str(row["title"] or ""),
                "payload": str(row["payload"] or ""),
                "created_at": str(row["created_at"] or ""),
                "download_method": str(row["download_method"] or ""),
                "qb_save_path": str(row["qb_save_path"] or ""),
            }
            for row in fresh
        ]
        claimed = db.claim_pending_rss_qb_entries(fresh_expected)
        self.assertEqual(len(claimed), 2)
        self.assertEqual(db.get_rss_entry(first)["status"], "submitting")
        self.assertEqual(db.get_rss_entry(second)["status"], "submitting")
        guangya_sub = self._subscription("GuangYa RSS", "guangya")
        guangya_entry = self._entry(guangya_sub, 90)
        guangya_row = db.get_rss_entry(guangya_entry)
        forged = [
            {
                "id": int(guangya_row["id"]),
                "rss_item_id": int(guangya_row["rss_item_id"]),
                "title": str(guangya_row["title"] or ""),
                "payload": str(guangya_row["payload"] or ""),
                "created_at": str(guangya_row["created_at"] or ""),
                "download_method": str(guangya_row["download_method"] or ""),
                "qb_save_path": str(guangya_row["qb_save_path"] or ""),
            }
        ]
        self.assertEqual(
            db.claim_pending_rss_qb_entries(forged, default_method="qb"), []
        )
        self.assertEqual(db.get_rss_entry(guangya_entry)["status"], "pending")

    def test_engine_uses_frozen_config_and_invalid_payload_does_not_stick(self):
        sub_id = self._subscription()
        valid = self._entry(sub_id, 1)
        invalid = self._entry(sub_id, 2, url=False)
        rows = db.get_pending_rss_qb_snapshot(default_method="qb", limit=2)
        expected = [
            {
                "id": int(row["id"]),
                "rss_item_id": int(row["rss_item_id"]),
                "title": str(row["title"] or ""),
                "payload": str(row["payload"] or ""),
                "created_at": str(row["created_at"] or ""),
                "download_method": str(row["download_method"] or ""),
                "qb_save_path": str(row["qb_save_path"] or ""),
            }
            for row in rows
        ]
        with (
            patch(
                "app.clients.qbittorrent.QBittorrentClient.__init__", return_value=None
            ) as init,
            patch(
                "app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed",
                return_value=TorrentAddResult(True),
            ) as add,
        ):
            result = RSSEngine().submit_pending_qb_snapshot(expected, self.runtime)
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(result["failed"], 1)
        init.assert_called_once_with(
            url="http://qb.internal:8080",
            username="agent-user",
            password="QB_SECRET_PASSWORD",
            api_key="QB_SECRET_API_KEY",
            timeout=10,
        )
        add.assert_called_once_with(
            urls=f"magnet:?xt=urn:btih:{1:040x}&dn=PRIVATESECRET1",
            save_path="/private/subscription/path",
            category="rss-agent",
            torrents=None,
        )
        self.assertEqual(db.get_rss_entry(valid)["status"], "downloaded")
        self.assertEqual(db.get_rss_entry(invalid)["status"], "failed")
        logs = db.list_download_logs(source="qb", limit=5)
        serialized_logs = json.dumps([dict(row) for row in logs], ensure_ascii=False)
        self.assertNotIn("PRIVATESECRET1", serialized_logs)
        self.assertNotIn("magnet:?", serialized_logs)
        self.assertIn("[magnet]", serialized_logs)

    def test_snapshot_counts_unknown_qb_outcomes_separately(self):
        sub_id = self._subscription()
        entry_id = self._entry(sub_id, 1)
        unique_hash = "b" * 40
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET payload=? WHERE id=?",
                (
                    json.dumps({"torrent_url": f"magnet:?xt=urn:btih:{unique_hash}"}),
                    entry_id,
                ),
            )
        rows = db.get_pending_rss_qb_snapshot(default_method="qb", limit=1)
        expected = [
            {
                "id": int(row["id"]),
                "rss_item_id": int(row["rss_item_id"]),
                "title": str(row["title"] or ""),
                "payload": str(row["payload"] or ""),
                "created_at": str(row["created_at"] or ""),
                "download_method": str(row["download_method"] or ""),
                "qb_save_path": str(row["qb_save_path"] or ""),
            }
            for row in rows
        ]
        with patch(
            "app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed",
            return_value=TorrentAddResult(False, "qb_outcome_unknown", False),
        ):
            result = RSSEngine().submit_pending_qb_snapshot(expected, self.runtime)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["outcome_unknown"], 1)

    def test_agent_snapshot_dedupes_same_opaque_url_across_entries(self):
        sub_id = self._subscription()
        first = self._entry(sub_id, 1)
        second = self._entry(sub_id, 2)
        opaque_url = "https://example.invalid/download?id=agent-opaque-same"
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET payload=? WHERE id IN (?,?)",
                (json.dumps({"torrent_url": opaque_url}), first, second),
            )
        rows = db.get_pending_rss_qb_snapshot(default_method="qb", limit=2)
        expected = [
            {
                "id": int(row["id"]),
                "rss_item_id": int(row["rss_item_id"]),
                "title": str(row["title"] or ""),
                "payload": str(row["payload"] or ""),
                "created_at": str(row["created_at"] or ""),
                "download_method": str(row["download_method"] or ""),
                "qb_save_path": str(row["qb_save_path"] or ""),
            }
            for row in rows
        ]
        with (
            patch(
                "app.clients.qbittorrent.QBittorrentClient.__init__", return_value=None
            ),
            patch(
                "app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed",
                return_value=TorrentAddResult(True),
            ) as add,
        ):
            result = RSSEngine().submit_pending_qb_snapshot(expected, self.runtime)
        self.assertTrue(result["ok"])
        self.assertEqual(result["submitted"], 2)
        self.assertEqual(result["failed"], 0)
        add.assert_called_once()
        self.assertEqual(str(db.get_rss_entry(first)["status"]), "downloaded")
        self.assertEqual(str(db.get_rss_entry(second)["status"]), "downloaded")

    def test_standard_download_invalid_payload_converges_to_failed(self):
        sub_id = self._subscription()
        entry_id = self._entry(sub_id, 1)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET payload='not-json' WHERE id=?", (entry_id,)
            )
        result = RSSEngine().download(entry_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "条目数据无效")
        self.assertEqual(db.get_rss_entry(entry_id)["status"], "failed")

    def test_qb_add_failure_log_does_not_include_upstream_body_or_private_url(self):
        client = QBittorrentClient("http://qb.internal:8080", api_key="token")
        response = MagicMock(
            status_code=400, text="private passkey=SECRET and magnet:?xt=SECRET"
        )
        client._session.post = MagicMock(return_value=response)
        with (
            patch(
                "app.clients.qbittorrent.QBittorrentClient._parse_add_result",
                return_value=False,
            ),
            patch("app.clients.qbittorrent.logger.warning") as warning,
        ):
            self.assertFalse(
                client.add_torrent_detailed(urls="magnet:?xt=urn:btih:PRIVATESECRET").ok
            )
        warning.assert_called_once_with("qB 添加任务失败: 请求被拒绝 status=%s", 400)
        rendered = repr(warning.call_args)
        self.assertNotIn("PRIVATESECRET", rendered)
        self.assertNotIn("passkey", rendered)
