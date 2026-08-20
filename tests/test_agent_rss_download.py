"""Media Agent 待处理 RSS 条目安全提交的确认、竞态与脱敏回归。"""
from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.orchestrator import (
    is_rss_diagnosis_message,
    is_rss_pending_download_write_message,
    rss_pending_download_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.rss_download_actions import (
    preview_rss_pending_download,
    rss_pending_download_arguments,
    rss_pending_download_confirmation_context,
    submit_pending_rss_to_qb,
)
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.main import create_app
from app.clients.qbittorrent import QBittorrentClient, TorrentAddResult
from app.modules.rss import RSSEngine
from tests.support import IsolatedDatabaseTestCase


def _clear_rss() -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM rss_entries")
        conn.execute("DELETE FROM rss_items")
        conn.execute("DELETE FROM download_log")


class RssPendingDownloadUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        _clear_rss()
        reset_agent_service_for_tests()
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
        reset_agent_service_for_tests()

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
            json.dumps({"torrent_url": f"magnet:?xt=urn:btih:SECRET{index}"})
            if url else "{}"
        )
        entry_id = db.add_rss_entry(
            sub_id,
            f"Private Episode {index}",
            f"secret-guid-{index}",
            payload=payload,
        )
        assert entry_id is not None
        return entry_id

    def test_arguments_registry_and_direct_execution_are_strict(self):
        self.assertEqual(rss_pending_download_arguments({}), {"limit": 10})
        self.assertEqual(rss_pending_download_arguments({"limit": 3}), {"limit": 3})
        for invalid in (
            {"limit": 0}, {"limit": 21}, {"limit": True}, {"limit": "2"},
            {"limit": 2, "entry_ids": [1]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                rss_pending_download_arguments(invalid)

        tools = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        spec = tools["rss.submit_pending_to_qb"]
        self.assertEqual(spec["risk"], "danger")
        self.assertTrue(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])
        self.assertEqual(spec["parameters"]["properties"]["limit"]["maximum"], 20)
        with self.assertRaises(AgentToolError) as direct:
            get_agent_service().registry.execute("rss.submit_pending_to_qb", {"limit": 1})
        self.assertEqual(direct.exception.code, "confirmation_required")

    def test_preview_selects_latest_pending_qb_only_and_is_sanitized(self):
        qb_sub = self._subscription()
        gy_sub = self._subscription("GuangYa RSS", "guangya")
        ids = [self._entry(qb_sub, index) for index in range(1, 5)]
        self._entry(gy_sub, 90)
        db.update_rss_entry_status(ids[0], "downloaded")

        with patch("app.clients.qbittorrent.QBittorrentClient.add_torrent") as add:
            result = preview_rss_pending_download({"limit": 2})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["selected_count"], 2)
        self.assertTrue(result.data["has_more"])
        add.assert_not_called()
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "Private Episode", "secret-guid", "SECRET", "private/downloads",
            "qb.internal", "agent-user", "rss-agent", "passkey",
        ):
            self.assertNotIn(secret, serialized)

    def test_confirmation_context_freezes_exact_rows_and_returns_aggregate_only(self):
        sub_id = self._subscription()
        first = self._entry(sub_id, 1)
        second = self._entry(sub_id, 2)
        fingerprint = rss_pending_download_confirmation_context({"limit": 2})
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
        with patch.object(RSSEngine, "submit_pending_qb_snapshot", return_value=raw) as submit:
            result = submit_pending_rss_to_qb({"limit": 2})
        expected_rows, runtime = submit.call_args.args
        self.assertEqual([item["id"] for item in expected_rows], [second, first])
        self.assertEqual(runtime, self.runtime)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(
            result.data,
            {"target": "qbittorrent", "requested": 2, "claimed": 2, "submitted": 1, "failed": 1},
        )
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("QB_SECRET", serialized)
        self.assertNotIn("/private", serialized)

    def test_unknown_submission_requires_qb_review_before_retry(self):
        sub_id = self._subscription()
        self._entry(sub_id, 1)
        rss_pending_download_confirmation_context({"limit": 1})
        raw = {
            "ok": False, "conflict": False, "requested": 1,
            "claimed": 1, "submitted": 0, "failed": 1,
            "outcome_unknown": 1,
        }
        with patch.object(RSSEngine, "submit_pending_qb_snapshot", return_value=raw):
            result = submit_pending_rss_to_qb({"limit": 1})

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
        rss_pending_download_confirmation_context({"limit": 3})
        raw = {
            "ok": False, "conflict": False, "requested": 3,
            "claimed": 3, "submitted": 1, "failed": 2,
            "outcome_unknown": 1,
        }
        with patch.object(RSSEngine, "submit_pending_qb_snapshot", return_value=raw):
            result = submit_pending_rss_to_qb({"limit": 3})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertIn("成功 1", result.summary)
        self.assertIn("待核对 1", result.summary)
        self.assertIn("确认失败 1", result.summary)
        self.assertIn("勿直接重复提交", result.suggestions[0])

    def test_prepare_becomes_stale_when_selected_set_changes(self):
        sub_id = self._subscription()
        self._entry(sub_id, 1)
        service = get_agent_service()
        prepared = service.prepare("rss.submit_pending_to_qb", {"limit": 1}, owner="owner")
        self._entry(sub_id, 2)
        with patch.object(RSSEngine, "submit_pending_qb_snapshot") as submit:
            with self.assertRaises(AgentToolError) as stale:
                service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")
        self.assertEqual(stale.exception.code, "confirmation_stale")
        submit.assert_not_called()

    def test_database_claim_is_all_or_nothing_and_pending_only(self):
        sub_id = self._subscription()
        first = self._entry(sub_id, 1)
        second = self._entry(sub_id, 2)
        rows = db.get_pending_rss_qb_snapshot(default_method="qb", limit=2)
        expected = [{
            "id": int(row["id"]),
            "rss_item_id": int(row["rss_item_id"]),
            "title": str(row["title"] or ""),
            "payload": str(row["payload"] or ""),
            "created_at": str(row["created_at"] or ""),
            "download_method": str(row["download_method"] or ""),
            "qb_save_path": str(row["qb_save_path"] or ""),
        } for row in rows]
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET payload='{}' WHERE id=?", (first,))
        self.assertEqual(db.claim_pending_rss_qb_entries(expected), [])
        self.assertEqual(db.get_rss_entry(first)["status"], "pending")
        self.assertEqual(db.get_rss_entry(second)["status"], "pending")

        fresh = db.get_pending_rss_qb_snapshot(default_method="qb", limit=2)
        fresh_expected = [{
            "id": int(row["id"]),
            "rss_item_id": int(row["rss_item_id"]),
            "title": str(row["title"] or ""),
            "payload": str(row["payload"] or ""),
            "created_at": str(row["created_at"] or ""),
            "download_method": str(row["download_method"] or ""),
            "qb_save_path": str(row["qb_save_path"] or ""),
        } for row in fresh]
        claimed = db.claim_pending_rss_qb_entries(fresh_expected)
        self.assertEqual(len(claimed), 2)
        self.assertEqual(db.get_rss_entry(first)["status"], "submitting")
        self.assertEqual(db.get_rss_entry(second)["status"], "submitting")

        guangya_sub = self._subscription("GuangYa RSS", "guangya")
        guangya_entry = self._entry(guangya_sub, 90)
        guangya_row = db.get_rss_entry(guangya_entry)
        forged = [{
            "id": int(guangya_row["id"]),
            "rss_item_id": int(guangya_row["rss_item_id"]),
            "title": str(guangya_row["title"] or ""),
            "payload": str(guangya_row["payload"] or ""),
            "created_at": str(guangya_row["created_at"] or ""),
            "download_method": str(guangya_row["download_method"] or ""),
            "qb_save_path": str(guangya_row["qb_save_path"] or ""),
        }]
        self.assertEqual(db.claim_pending_rss_qb_entries(forged, default_method="qb"), [])
        self.assertEqual(db.get_rss_entry(guangya_entry)["status"], "pending")

    def test_engine_uses_frozen_config_and_invalid_payload_does_not_stick(self):
        sub_id = self._subscription()
        valid = self._entry(sub_id, 1)
        invalid = self._entry(sub_id, 2, url=False)
        rows = db.get_pending_rss_qb_snapshot(default_method="qb", limit=2)
        expected = [{
            "id": int(row["id"]),
            "rss_item_id": int(row["rss_item_id"]),
            "title": str(row["title"] or ""),
            "payload": str(row["payload"] or ""),
            "created_at": str(row["created_at"] or ""),
            "download_method": str(row["download_method"] or ""),
            "qb_save_path": str(row["qb_save_path"] or ""),
        } for row in rows]
        with patch("app.clients.qbittorrent.QBittorrentClient.__init__", return_value=None) as init, patch(
            "app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed",
            return_value=TorrentAddResult(True),
        ) as add:
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
            urls="magnet:?xt=urn:btih:SECRET1",
            save_path="/private/subscription/path",
            category="rss-agent",
        )
        self.assertEqual(db.get_rss_entry(valid)["status"], "downloaded")
        self.assertEqual(db.get_rss_entry(invalid)["status"], "failed")
        logs = db.list_download_logs(source="qb", limit=5)
        serialized_logs = json.dumps([dict(row) for row in logs], ensure_ascii=False)
        self.assertNotIn("SECRET1", serialized_logs)
        self.assertNotIn("magnet:?", serialized_logs)
        self.assertIn("[magnet]", serialized_logs)

    def test_snapshot_counts_unknown_qb_outcomes_separately(self):
        sub_id = self._subscription()
        entry_id = self._entry(sub_id, 1)
        unique_hash = "b" * 40
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET payload=? WHERE id=?",
                (json.dumps({"torrent_url": f"magnet:?xt=urn:btih:{unique_hash}"}), entry_id),
            )
        rows = db.get_pending_rss_qb_snapshot(default_method="qb", limit=1)
        expected = [{
            "id": int(row["id"]),
            "rss_item_id": int(row["rss_item_id"]),
            "title": str(row["title"] or ""),
            "payload": str(row["payload"] or ""),
            "created_at": str(row["created_at"] or ""),
            "download_method": str(row["download_method"] or ""),
            "qb_save_path": str(row["qb_save_path"] or ""),
        } for row in rows]
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
        expected = [{
            "id": int(row["id"]),
            "rss_item_id": int(row["rss_item_id"]),
            "title": str(row["title"] or ""),
            "payload": str(row["payload"] or ""),
            "created_at": str(row["created_at"] or ""),
            "download_method": str(row["download_method"] or ""),
            "qb_save_path": str(row["qb_save_path"] or ""),
        } for row in rows]

        with patch(
            "app.clients.qbittorrent.QBittorrentClient.__init__", return_value=None,
        ), patch(
            "app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed",
            return_value=TorrentAddResult(True),
        ) as add:
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
            conn.execute("UPDATE rss_entries SET payload='not-json' WHERE id=?", (entry_id,))
        result = RSSEngine().download(entry_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "条目数据无效")
        self.assertEqual(db.get_rss_entry(entry_id)["status"], "failed")

    def test_qb_add_failure_log_does_not_include_upstream_body_or_private_url(self):
        client = QBittorrentClient("http://qb.internal:8080", api_key="token")
        response = MagicMock(status_code=400, text="private passkey=SECRET and magnet:?xt=SECRET")
        client._session.post = MagicMock(return_value=response)
        with patch("app.clients.qbittorrent.QBittorrentClient._parse_add_result", return_value=False), patch(
            "app.clients.qbittorrent.logger.warning"
        ) as warning:
            self.assertFalse(client.add_torrent(urls="magnet:?xt=urn:btih:PRIVATESECRET"))
        warning.assert_called_once_with("qB 添加任务失败: 请求被拒绝 status=%s", 400)
        rendered = repr(warning.call_args)
        self.assertNotIn("PRIVATESECRET", rendered)
        self.assertNotIn("passkey", rendered)

    def test_natural_language_route_is_narrow_and_bounded(self):
        self.assertEqual(rss_pending_download_request("提交 5 个待处理 RSS 条目到 qB"), {"limit": 5})
        self.assertEqual(rss_pending_download_request("下载 RSS 积压条目"), {"limit": 10})
        for message in (
            "下载全部待处理 RSS 条目",
            "把待处理 RSS 条目推送到光鸭",
            "重试 RSS 失败条目",
            "刷新 RSS 订阅",
            "诊断 RSS 待处理积压",
            "提交 21 个待处理 RSS 条目",
        ):
            self.assertIsNone(rss_pending_download_request(message), message)
        self.assertTrue(is_rss_pending_download_write_message("下载全部待处理 RSS 条目"))
        for message in ("查看待处理 RSS 下载状态", "检查 RSS 下载状态"):
            self.assertFalse(is_rss_pending_download_write_message(message), message)
            self.assertIsNone(rss_pending_download_request(message), message)
            self.assertTrue(is_rss_diagnosis_message(message), message)


class RssPendingDownloadAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        _clear_rss()
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
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
        sub_id = db.add_rss_subscription(
            "Private RSS",
            "https://secret.example/rss?passkey=RSS_SECRET",
            download_method="qb",
        )
        self.entry_id = db.add_rss_entry(
            sub_id,
            "Private Episode",
            "secret-guid",
            payload='{"torrent_url":"magnet:?xt=urn:btih:RSSSECRET"}',
        )
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.runtime_patcher.stop()
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        matched = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not matched:
            matched = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not matched:
            raise AssertionError("CSRF token missing")
        return matched.group(1)

    def _login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_query_prepare_confirm_replay_direct_gate_and_sanitization(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        raw = {
            "ok": True, "conflict": False, "requested": 1,
            "claimed": 1, "submitted": 1, "failed": 0,
        }
        with patch.object(RSSEngine, "submit_pending_qb_snapshot", return_value=raw) as submit:
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "提交 1 个待处理 RSS 条目到 qB"},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            body = prepared.json()
            self.assertEqual(body["mode"], "confirmation_required")
            self.assertEqual(body["result"]["data"]["selected_count"], 1)
            confirmation_id = body["confirmation"]["confirmation_id"]
            submit.assert_not_called()

            direct = self.client.post(
                "/api/agent/tools/rss.submit_pending_to_qb",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"limit": 1}},
            )
            self.assertEqual(direct.status_code, 409, direct.text)

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertEqual(confirmed.json()["result"]["status"], "completed")
            expected_rows, runtime = submit.call_args.args
            self.assertEqual([item["id"] for item in expected_rows], [self.entry_id])
            self.assertEqual(runtime, self.runtime)

            replay = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(replay.status_code, 409, replay.text)
            submit.assert_called_once()

        serialized = prepared.text + confirmed.text
        for secret in (
            "Private Episode", "secret-guid", "RSSSECRET", "private/downloads",
            "qb.internal", "QB_SECRET", "passkey",
        ):
            self.assertNotIn(secret, serialized)

    def test_auth_csrf_strict_prepare_and_shared_three_per_minute_limit(self):
        path = "/api/agent/actions/rss.submit_pending_to_qb/prepare"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {"limit": 1}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {"limit": 1}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}
        rejected = self.client.post(
            path,
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"limit": 1, "entry_ids": [self.entry_id]}},
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        agent_rate_limiter.reset()

        for _ in range(3):
            response = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "提交 1 个待处理 RSS 条目"},
            )
            self.assertEqual(response.status_code, 200, response.text)
        limited = self.client.post(
            path,
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"limit": 1}},
        )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    import unittest
    unittest.main()
