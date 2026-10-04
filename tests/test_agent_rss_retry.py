"""Media Agent RSS 失败分类与安全重试回归。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import requests

from app import database as db
from app.agent.rss_retry_actions import (
    prepare_rss_failure_retry,
    retry_failed_rss_confirmed,
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


class _Response:
    def __init__(self, status: int, text: str = "", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class RssFailureRetryUnitTests(IsolatedDatabaseTestCase):
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
    def _entry(sub_id: int, index: int, *, payload: str | None = None) -> int:
        entry_id = db.add_rss_entry_with_media(
            sub_id,
            f"Private Episode {index}",
            f"secret-guid-{index}",
            payload=payload
            or json.dumps(
                {
                    "torrent_url": f"magnet:?xt=urn:btih:{index:040x}&dn=PRIVATESECRET{index}"
                }
            ),
        )["id"]
        assert entry_id is not None
        return entry_id

    def _failed(
        self, sub_id: int, index: int, code="qb_unavailable", retryable=True
    ) -> int:
        entry_id = self._entry(sub_id, index)
        db.record_rss_entry_failure(entry_id, code, retryable)
        return entry_id

    def test_qb_add_detailed_classifies_http_and_transport_failures(self):
        cases = (
            (_Response(401, "secret"), "qb_auth_failed", False),
            (_Response(403, "secret"), "qb_auth_failed", False),
            (_Response(400, "private passkey=SECRET"), "qb_rejected", False),
            (_Response(429), "qb_rate_limited", True),
            (_Response(503), "qb_outcome_unknown", False),
            (requests.ConnectionError("private host"), "qb_outcome_unknown", False),
            (requests.ConnectTimeout("private host"), "qb_unavailable", True),
            (requests.ReadTimeout("outcome unknown"), "qb_outcome_unknown", False),
            (requests.Timeout("outcome unknown"), "qb_outcome_unknown", False),
        )
        for response_or_error, code, retryable in cases:
            with self.subTest(code=code, error=type(response_or_error).__name__):
                client = QBittorrentClient("http://qb.internal:8080", api_key="token")
                if isinstance(response_or_error, Exception):
                    client._session.post = MagicMock(side_effect=response_or_error)
                else:
                    client._session.post = MagicMock(return_value=response_or_error)
                result = client.add_torrent_detailed(
                    urls="magnet:?xt=urn:btih:PRIVATESECRET"
                )
                self.assertEqual(result, TorrentAddResult(False, code, retryable))
        success = QBittorrentClient("http://qb.internal:8080", api_key="token")
        success._session.post = MagicMock(return_value=_Response(200, "Ok."))
        self.assertEqual(
            success.add_torrent_detailed(urls="magnet:?xt=test"), TorrentAddResult(True)
        )

    def test_qb_login_failure_is_classified_without_response_body_leak(self):
        client = QBittorrentClient(
            "http://qb.internal:8080", username="u", password="secret"
        )
        client._session.post = MagicMock(
            return_value=_Response(200, "Fails. passkey=SECRET")
        )
        with patch("app.clients.qbittorrent.logger.warning") as warning:
            result = client.add_torrent_detailed(urls="magnet:?xt=PRIVATESECRET")
        self.assertEqual(result, TorrentAddResult(False, "qb_auth_failed", False))
        rendered = repr(warning.call_args_list)
        self.assertNotIn("PRIVATESECRET", rendered)
        self.assertNotIn("passkey", rendered)

    def test_qb_login_timeout_is_safe_to_retry_before_torrent_submission(self):
        client = QBittorrentClient(
            "http://qb.internal:8080", username="u", password="secret"
        )
        client._session.post = MagicMock(
            side_effect=requests.ReadTimeout("login timeout")
        )
        result = client.add_torrent_detailed(urls="magnet:?xt=urn:btih:PRIVATESECRET")
        self.assertEqual(result, TorrentAddResult(False, "qb_unavailable", True))
        client._session.post.assert_called_once()
        self.assertTrue(
            client._session.post.call_args.args[0].endswith("/api/v2/auth/login")
        )

    def test_preview_selects_retryable_failed_qb_only_and_is_sanitized(self):
        qb_sub = self._subscription()
        gy_sub = self._subscription("GuangYa RSS", "guangya")
        selected = [self._failed(qb_sub, index) for index in range(1, 4)]
        self._failed(qb_sub, 10, "qb_auth_failed", False)
        self._failed(gy_sub, 90)
        db.update_rss_entries_processed([selected[0]], True)
        with patch(
            "app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed"
        ) as add:
            result, _context = prepare_rss_failure_retry({"limit": 2})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["selected_count"], 2)
        self.assertFalse(result.data["has_more"])
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
            "qb_unavailable",
        ):
            self.assertNotIn(secret, serialized)

    def test_database_claim_is_all_or_nothing_and_increments_retry_count(self):
        sub_id = self._subscription()
        first = self._failed(sub_id, 1)
        second = self._failed(sub_id, 2)
        rows = db.get_retryable_failed_rss_snapshot(default_method="qb", limit=2)
        expected = [db.rss_retry_entry_snapshot(row) for row in rows]
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET failure_code='qb_auth_failed' WHERE id=?",
                (first,),
            )
        self.assertEqual(db.claim_retryable_failed_rss_entries(expected), [])
        self.assertEqual(db.get_rss_entry(first)["status"], "failed")
        self.assertEqual(db.get_rss_entry(second)["status"], "failed")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET failure_code='qb_unavailable' WHERE id=?",
                (first,),
            )
        fresh_rows = db.get_retryable_failed_rss_snapshot(
            default_method="qb", limit=2
        )
        fresh = [db.rss_retry_entry_snapshot(row) for row in fresh_rows]
        claimed = db.claim_retryable_failed_rss_entries(fresh)
        self.assertEqual(len(claimed), 2)
        for entry_id in (first, second):
            row = db.get_rss_entry(entry_id)
            self.assertEqual(row["status"], "submitting")
            self.assertEqual(row["retry_count"], 1)
            self.assertEqual(row["failure_code"], "")
            self.assertEqual(row["failure_retryable"], 0)

    def test_retry_snapshot_enforces_rate_limit_cooldown_and_attempt_cap(self):
        sub_id = self._subscription()
        rate_limited = self._failed(sub_id, 1, "qb_rate_limited", True)
        capped = self._failed(sub_id, 2)
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET retry_count=5 WHERE id=?", (capped,))
        self.assertEqual(
            db.get_retryable_failed_rss_snapshot(default_method="qb", limit=10), []
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET failed_at=datetime('now','localtime','-61 seconds') WHERE id=?",
                (rate_limited,),
            )
        rows = db.get_retryable_failed_rss_snapshot(default_method="qb", limit=10)
        self.assertEqual([row["id"] for row in rows], [rate_limited])

    def test_unknown_retry_requires_qb_review_before_another_attempt(self):
        sub_id = self._subscription()
        self._failed(sub_id, 1)
        raw = {
            "ok": False,
            "conflict": False,
            "requested": 1,
            "claimed": 1,
            "submitted": 0,
            "failed": 1,
            "outcome_unknown": 1,
        }
        fingerprint = prepare_rss_failure_retry({"limit": 1})[1]
        with patch.object(
            RSSEngine, "submit_snapshot", return_value=raw
        ) as submit:
            result = retry_failed_rss_confirmed({"limit": 1}, fingerprint)
        self.assertIs(
            submit.call_args.kwargs["claim"],
            db.claim_retryable_failed_rss_entries,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.data["outcome_unknown"], 1)
        self.assertIn("待核对 1", result.summary)
        self.assertIn("勿直接重试", result.error)

    def test_mixed_unknown_retry_reports_all_three_outcomes(self):
        sub_id = self._subscription()
        for index in range(1, 4):
            self._failed(sub_id, index)
        raw = {
            "ok": False,
            "conflict": False,
            "requested": 3,
            "claimed": 3,
            "submitted": 1,
            "failed": 2,
            "outcome_unknown": 1,
        }
        fingerprint = prepare_rss_failure_retry({"limit": 3})[1]
        with patch.object(RSSEngine, "submit_snapshot", return_value=raw):
            result = retry_failed_rss_confirmed({"limit": 3}, fingerprint)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertIn("成功 1", result.summary)
        self.assertIn("待核对 1", result.summary)
        self.assertIn("确认失败 1", result.summary)
        self.assertIn("勿直接重复提交", result.suggestions[0])

    def test_pending_retry_is_in_progress_and_not_failed(self):
        sub_id = self._subscription()
        self._failed(sub_id, 1)
        raw = {
            "ok": False,
            "conflict": False,
            "requested": 1,
            "claimed": 1,
            "submitted": 0,
            "failed": 0,
            "pending": 1,
        }
        fingerprint = prepare_rss_failure_retry({"limit": 1})[1]
        with patch.object(RSSEngine, "submit_snapshot", return_value=raw):
            result = retry_failed_rss_confirmed({"limit": 1}, fingerprint)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "in_progress")
        self.assertEqual(result.data["pending"], 1)
        self.assertIn("成功 0，提交中 1，失败 0，待核对 0", result.summary)
        self.assertEqual(result.error, "")

    def test_pending_retry_mixed_with_failure_is_partial_but_not_ok(self):
        sub_id = self._subscription()
        self._failed(sub_id, 1)
        self._failed(sub_id, 2)
        raw = {
            "ok": False,
            "conflict": False,
            "requested": 2,
            "claimed": 2,
            "submitted": 0,
            "failed": 1,
            "pending": 1,
        }
        fingerprint = prepare_rss_failure_retry({"limit": 2})[1]
        with patch.object(RSSEngine, "submit_snapshot", return_value=raw):
            result = retry_failed_rss_confirmed({"limit": 2}, fingerprint)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["pending"], 1)
        self.assertIn("成功 0，提交中 1，失败 1，待核对 0", result.summary)

    def test_retry_engine_propagates_unknown_outcome_count(self):
        sub_id = self._subscription()
        entry_id = self._failed(sub_id, 77)
        unique_hash = "c" * 40
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET payload=? WHERE id=?",
                (
                    json.dumps({"torrent_url": f"magnet:?xt=urn:btih:{unique_hash}"}),
                    entry_id,
                ),
            )
        rows = db.get_retryable_failed_rss_snapshot(default_method="qb", limit=1)
        expected = [db.rss_retry_entry_snapshot(row) for row in rows]
        with patch(
            "app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed",
            return_value=TorrentAddResult(False, "qb_outcome_unknown", False),
        ):
            result = RSSEngine().submit_snapshot(
                expected, self.runtime, claim=db.claim_retryable_failed_rss_entries
            )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["outcome_unknown"], 1)
        row = db.get_rss_entry(entry_id)
        self.assertEqual(row["failure_code"], "qb_outcome_unknown")
        self.assertEqual(row["failure_retryable"], 0)

    def test_engine_records_retry_result_and_preserves_aggregate_contract(self):
        sub_id = self._subscription()
        success_id = self._failed(sub_id, 1)
        failed_id = self._failed(sub_id, 2)
        rows = db.get_retryable_failed_rss_snapshot(default_method="qb", limit=2)
        expected = [db.rss_retry_entry_snapshot(row) for row in rows]
        outcomes = [
            TorrentAddResult(False, "qb_rate_limited", True),
            TorrentAddResult(True),
        ]
        with patch(
            "app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed",
            side_effect=outcomes,
        ):
            result = RSSEngine().submit_snapshot(
                expected, self.runtime, claim=db.claim_retryable_failed_rss_entries
            )
        self.assertEqual(
            result,
            {
                "ok": False,
                "conflict": False,
                "requested": 2,
                "claimed": 2,
                "submitted": 1,
                "failed": 1,
            },
        )
        retried_failure = db.get_rss_entry(failed_id)
        self.assertEqual(retried_failure["status"], "failed")
        self.assertEqual(retried_failure["failure_code"], "qb_rate_limited")
        self.assertEqual(retried_failure["failure_retryable"], 1)
        self.assertEqual(retried_failure["retry_count"], 1)
        self.assertEqual(db.get_rss_entry(success_id)["status"], "downloaded")

    def test_cloud_retry_needs_no_qb_and_rejects_duplicate_confirmation(self):
        from app.agent.errors import AgentToolError
        from app.modules import download_dispatcher as dispatcher

        sub = self._subscription("Cloud", "guangya")
        db.update_rss_subscription(sub, {"gy_target_dir": "chosen-cloud", "gy_target_dir_name": "Chosen"})
        entry = self._failed(sub, 400, "guangya_manifest_unavailable", True)
        with patch("app.modules.rss.capture_rss_qb_runtime_config", return_value=({"default_method": "qb"}, "未配置 qBittorrent 地址")):
            preview, fingerprint = prepare_rss_failure_retry({"limit": 10})
            self.assertTrue(preview.ok, preview)
            self.assertEqual(preview.data["target"], "guangya")
            with patch.object(dispatcher, "_submit_guangya", return_value={"ok": True, "task_id": "cloud-accepted"}) as gy, patch.object(dispatcher, "_submit_qb") as qb:
                result = retry_failed_rss_confirmed({"limit": 10}, fingerprint)
                self.assertTrue(result.ok, result)
                self.assertEqual(result.data["submitted"], 1)
                gy.assert_called_once()
                self.assertEqual(gy.call_args.kwargs["target_dir_id"], "chosen-cloud")
                qb.assert_not_called()
                with self.assertRaises(AgentToolError):
                    retry_failed_rss_confirmed({"limit": 10}, fingerprint)
                self.assertEqual(gy.call_count, 1)
        self.assertEqual(db.get_rss_entry(entry)["status"], "downloaded")
        self.assertEqual(db.get_rss_entry(entry)["retry_count"], 1)

    def test_mixed_retry_keeps_each_backend_and_does_not_leak_snapshot(self):
        from app.modules import download_dispatcher as dispatcher

        qb_sub = self._subscription("QB", "qb")
        gy_sub = self._subscription("Cloud", "guangya")
        first = self._failed(qb_sub, 401)
        second = self._failed(gy_sub, 402, "guangya_unavailable", True)
        preview, fingerprint = prepare_rss_failure_retry({"limit": 10})
        self.assertEqual(preview.data["target"], "both")
        self.assertEqual(preview.data["selected_count"], 2)
        with patch.object(dispatcher, "_submit_guangya", return_value={"ok": True, "task_id": "gy"}) as gy, patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "qb"}) as qb:
            result = retry_failed_rss_confirmed({"limit": 10}, fingerprint)
        gy.assert_called_once()
        qb.assert_called_once()
        self.assertEqual(result.data["submitted"], 2)
        self.assertTrue(all(db.get_rss_entry(e)["status"] == "downloaded" for e in (first, second)))
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for private in ("PRIVATESECRET", "private/subscription", "cloud_rules", "QB_SECRET", "torrent_url"):
            self.assertNotIn(private, serialized)

    def test_cloud_directory_or_rules_change_invalidates_confirmation(self):
        from dataclasses import replace
        from app.agent.errors import AgentToolError
        from app.modules import download_dispatcher as dispatcher
        from app.modules.offline import OfflineRules

        sub = self._subscription("Cloud", "guangya")
        entry = self._failed(sub, 403, "guangya_manifest_unavailable", True)
        _, fingerprint = prepare_rss_failure_retry({"limit": 10})
        db.update_rss_subscription(sub, {"gy_target_dir": "changed"})
        with patch.object(dispatcher, "_submit_guangya") as submit:
            with self.assertRaises(AgentToolError):
                retry_failed_rss_confirmed({"limit": 10}, fingerprint)
            _, fingerprint = prepare_rss_failure_retry({"limit": 10})
            rules = replace(OfflineRules.from_config(), target_dir_id="changed-default")
            with patch.object(OfflineRules, "from_config", return_value=rules):
                with self.assertRaises(AgentToolError):
                    retry_failed_rss_confirmed({"limit": 10}, fingerprint)
            submit.assert_not_called()
        self.assertEqual(db.get_rss_entry(entry)["status"], "failed")

    def test_cloud_unknown_receipt_overrides_even_retryable_failure_marker(self):
        for receipt in ({"task_ids": ["accepted"]}, {"partial_success": True}, {"outcome_unknown": True}):
            with self.subTest(receipt=receipt):
                code, retryable, review = RSSEngine._backend_failure("guangya", {
                    "dispatch": {"results": {"guangya": {
                        "ok": False, "failure_code": "guangya_unavailable", "retryable": True, **receipt,
                    }}},
                })
                self.assertEqual(code, "guangya_outcome_unknown")
                self.assertFalse(retryable)
                self.assertTrue(review)

    def test_web_batch_retries_safe_cloud_failure_and_keeps_unknown_untouched(self):
        from app.modules import download_dispatcher as dispatcher

        sub = self._subscription("Cloud", "guangya")
        safe = self._failed(sub, 404, "guangya_manifest_unavailable", True)
        unknown = self._failed(sub, 405, "guangya_outcome_unknown", False)
        with patch.object(dispatcher, "_submit_guangya", return_value={"ok": True, "task_id": "gy"}) as submit:
            result = RSSEngine().download_many([safe, unknown])
        submit.assert_called_once()
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(db.get_rss_entry(unknown)["failure_code"], "guangya_outcome_unknown")
        self.assertEqual(db.get_rss_entry(safe)["status"], "downloaded")

    def test_cloud_manual_and_agent_retry_share_cooldown_and_attempt_cap(self):
        from app.modules import download_dispatcher as dispatcher

        sub = self._subscription("Cloud", "guangya")
        limited = self._failed(sub, 406, "guangya_rate_limited", True)
        exhausted = self._failed(sub, 407, "guangya_unavailable", True)
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET retry_count=5 WHERE id=?", (exhausted,))
        self.assertEqual(db.get_retryable_failed_rss_snapshot(), [])
        with patch.object(dispatcher, "_submit_guangya") as submit:
            self.assertFalse(RSSEngine().download(limited)["ok"])
            self.assertFalse(RSSEngine().download(exhausted)["ok"])
            submit.assert_not_called()
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET failed_at=datetime('now','localtime','-61 seconds') WHERE id=?", (limited,))
        self.assertEqual([row["id"] for row in db.get_retryable_failed_rss_snapshot()], [limited])

    def test_cloud_retry_reason_is_readable_without_raw_errors(self):
        from app.agent.rss_entry_actions import list_rss_entry_summaries

        sub = self._subscription("Cloud", "guangya")
        self._failed(sub, 408, "guangya_manifest_unavailable", True)
        result = list_rss_entry_summaries({"status": "failed", "limit": 10})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["entries"][0]["failure_code"], "光鸭暂未取得文件清单")
        self.assertTrue(result.data["entries"][0]["failure_retryable"])
