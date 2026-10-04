"""Media Agent 待处理 RSS 条目安全提交的确认、竞态与脱敏回归。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.rss_download_actions import (
    prepare_rss_pending_download,
    submit_pending_rss_to_qb_confirmed,
)
from app.agent.rss_entry_actions import build_rss_submission_result
from app.clients.qbittorrent import QBittorrentClient, TorrentAddResult
from app.modules.rss import RSSEngine
from tests.support import IsolatedDatabaseTestCase, seed_rss_entry_state


def _clear_rss() -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM download_log")
        conn.execute("DELETE FROM download_request_keys")
        conn.execute("DELETE FROM download_requests")
        conn.execute("DELETE FROM rss_entries")
        conn.execute("DELETE FROM rss_items")


class RssPendingDownloadUnitTests(IsolatedDatabaseTestCase):
    def test_qb_snapshot_has_only_the_unified_public_entrypoint(self):
        self.assertTrue(callable(getattr(RSSEngine, "submit_snapshot", None)))
        self.assertFalse(hasattr(RSSEngine, "submit_pending_qb_snapshot"))
        self.assertFalse(hasattr(RSSEngine, "retry_failed_qb_snapshot"))
        self.assertFalse(hasattr(RSSEngine, "_submit_snapshot"))

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
        entry_id = db.add_rss_entry_with_media(
            sub_id, f"Private Episode {index}", f"secret-guid-{index}", payload=payload
        )["id"]
        assert entry_id is not None
        return entry_id

    def test_preview_selects_latest_pending_qb_only_and_is_sanitized(self):
        qb_sub = self._subscription()
        gy_sub = self._subscription("GuangYa RSS", "guangya")
        ids = [self._entry(qb_sub, index) for index in range(1, 5)]
        self._entry(gy_sub, 90)
        seed_rss_entry_state(ids[0], "downloaded")
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
            RSSEngine, "submit_snapshot", return_value=raw
        ) as submit:
            result = submit_pending_rss_to_qb_confirmed({"limit": 2}, fingerprint)
        expected_rows, runtime = submit.call_args.args
        self.assertEqual([item["id"] for item in expected_rows], [second, first])
        self.assertEqual(runtime, self.runtime)
        self.assertIs(
            submit.call_args.kwargs["claim"], db.claim_pending_rss_qb_entries
        )
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
        with patch.object(RSSEngine, "submit_snapshot", return_value=raw):
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
        with patch.object(RSSEngine, "submit_snapshot", return_value=raw):
            result = submit_pending_rss_to_qb_confirmed({"limit": 3}, fingerprint)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertIn("成功 1", result.summary)
        self.assertIn("待核对 1", result.summary)
        self.assertIn("确认失败 1", result.summary)
        self.assertIn("勿直接重复提交", result.suggestions[0])

    def test_no_pending_receipt_golden_matrix_preserves_all_three_agent_contracts(self):
        cases = (
            (
                {"requested": 2, "claimed": 2, "submitted": 2, "failed": 0},
                "completed",
                True,
                {
                    "download": "已向 qBittorrent 提交 2 个 RSS 条目",
                    "retry": "已成功重试 2 个 RSS 失败条目",
                    "entry": "已向 qBittorrent 提交 2 个 RSS 条目",
                },
                {},
                {},
            ),
            (
                {"requested": 2, "claimed": 2, "submitted": 1, "failed": 1},
                "partial",
                True,
                {
                    "download": "RSS 条目部分提交完成：成功 1，失败 1",
                    "retry": "RSS 失败条目部分重试完成：成功 1，失败 1",
                    "entry": "RSS 条目部分提交完成：成功 1，失败 1",
                },
                {
                    "download": "RSS 条目提交未全部成功。",
                    "retry": "RSS 失败条目重试未全部成功。",
                    "entry": "RSS 条目提交未全部成功。",
                },
                {
                    "download": ["请在 RSS 订阅页和下载任务页核对失败项。"],
                    "retry": ["请重新诊断 RSS 失败状态后再决定下一步。"],
                    "entry": [],
                },
            ),
            (
                {"requested": 2, "claimed": 2, "submitted": 0, "failed": 2},
                "failed",
                False,
                {
                    "download": "本次 2 个 RSS 条目均未成功提交",
                    "retry": "本次 2 个 RSS 失败条目仍未成功提交",
                    "entry": "本次 2 个 RSS 条目均未成功提交",
                },
                {
                    "download": "RSS 条目提交未全部成功。",
                    "retry": "RSS 失败条目重试未全部成功。",
                    "entry": "RSS 条目提交未全部成功。",
                },
                {
                    "download": ["请在 RSS 订阅页和下载任务页核对失败项。"],
                    "retry": ["请重新诊断 RSS 失败状态后再决定下一步。"],
                    "entry": [],
                },
            ),
            (
                {
                    "requested": 1,
                    "claimed": 1,
                    "submitted": 0,
                    "failed": 1,
                    "outcome_unknown": 1,
                },
                "review_required",
                False,
                {
                    "download": "RSS 条目提交结果：成功 0，待核对 1，确认失败 0",
                    "retry": "RSS 失败条目提交结果：成功 0，待核对 1，确认失败 0",
                    "entry": "RSS 提交结果：成功 0，待核对 1，失败 0",
                },
                {
                    "download": "部分提交结果未知，请先核对 qBittorrent，勿直接重试。",
                    "retry": "部分提交结果未知，请先核对对应下载器，勿直接重试。",
                    "entry": "部分提交结果未知。",
                },
                {
                    "download": ["请先核对 qBittorrent 中是否已存在对应任务，勿直接重复提交。"],
                    "retry": ["请先核对对应下载器中是否已存在对应任务，勿直接重复提交。"],
                    "entry": ["请先在 qBittorrent 中核对待确认任务，勿直接重复提交。"],
                },
            ),
        )
        for kind in ("download", "retry", "entry"):
            for raw, status, ok, summaries, errors, suggestions in cases:
                with self.subTest(kind=kind, raw=raw):
                    result = build_rss_submission_result(
                        raw,
                        kind=kind,
                        target="qbittorrent",
                        evidence_description="golden receipt",
                    )
                    expected_data = {
                        "target": "qbittorrent",
                        "requested": raw["requested"],
                        "claimed": raw["claimed"],
                        "submitted": raw["submitted"],
                        "failed": raw["failed"],
                    }
                    if raw.get("outcome_unknown"):
                        expected_data["outcome_unknown"] = raw["outcome_unknown"]
                    self.assertEqual(result.data, expected_data)
                    self.assertEqual(
                        (result.ok, result.status, result.summary),
                        (ok, status, summaries[kind]),
                    )
                    expected_error = errors.get(kind, "")
                    expected_suggestions = suggestions.get(kind, [])
                    self.assertEqual(result.error, expected_error)
                    self.assertEqual(result.suggestions, expected_suggestions)

    def test_shared_result_builder_preserves_each_entrypoint_conflict_contract(self):
        raw = {"conflict": True, "requested": 3, "claimed": 1}
        for kind, target, summary in (
            ("download", "qbittorrent", "待处理 RSS 条目已变化，本次未提交"),
            ("retry", "both", "可重试 RSS 失败条目已变化，本次未提交"),
        ):
            with self.subTest(kind=kind):
                result = build_rss_submission_result(
                    raw,
                    kind=kind,
                    target=target,
                    evidence_description="unused on conflict",
                )
                self.assertEqual(
                    result.to_dict(),
                    {
                        "ok": False,
                        "status": "conflict",
                        "summary": summary,
                        "data": {
                            "target": target,
                            "requested": 3,
                            "claimed": 0,
                            "submitted": 0,
                            "failed": 0,
                        },
                        "evidence": [],
                        "suggestions": [],
                        "error": "请重新预检后再确认。",
                    },
                )
        with self.assertRaises(AgentToolError) as caught:
            build_rss_submission_result(
                raw,
                kind="entry",
                target="qbittorrent",
                evidence_description="unused on conflict",
            )
        self.assertEqual(caught.exception.code, "confirmation_stale")
        self.assertEqual(str(caught.exception), "RSS 条目在提交前发生变化，请重新预检")

    def test_pending_snapshot_is_in_progress_not_completed(self):
        sub_id = self._subscription()
        self._entry(sub_id, 1)
        raw = {
            "ok": False,
            "conflict": False,
            "requested": 1,
            "claimed": 1,
            "submitted": 0,
            "failed": 0,
            "pending": 1,
        }
        fingerprint = prepare_rss_pending_download({"limit": 1})[1]
        with patch.object(RSSEngine, "submit_snapshot", return_value=raw):
            result = submit_pending_rss_to_qb_confirmed({"limit": 1}, fingerprint)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "in_progress")
        self.assertEqual(result.data["pending"], 1)
        self.assertEqual(result.data["claimed"], 1)
        self.assertIn("成功 0，提交中 1，失败 0，待核对 0", result.summary)
        self.assertEqual(result.error, "")

    def test_mixed_pending_and_accepted_submission_is_partial(self):
        sub_id = self._subscription()
        for index in range(1, 4):
            self._entry(sub_id, index)
        raw = {
            "ok": False,
            "conflict": False,
            "requested": 3,
            "claimed": 3,
            "submitted": 1,
            "failed": 1,
            "pending": 1,
        }
        fingerprint = prepare_rss_pending_download({"limit": 3})[1]
        with patch.object(RSSEngine, "submit_snapshot", return_value=raw):
            result = submit_pending_rss_to_qb_confirmed({"limit": 3}, fingerprint)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["pending"], 1)
        self.assertIn("成功 1，提交中 1，失败 1，待核对 0", result.summary)

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
            result = RSSEngine().submit_snapshot(
                expected, self.runtime, claim=db.claim_pending_rss_qb_entries
            )
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
            result = RSSEngine().submit_snapshot(
                expected, self.runtime, claim=db.claim_pending_rss_qb_entries
            )
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
            result = RSSEngine().submit_snapshot(
                expected, self.runtime, claim=db.claim_pending_rss_qb_entries
            )
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
