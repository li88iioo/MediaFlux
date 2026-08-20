"""Media Agent RSS 失败分类与安全重试回归。"""
from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

from app import database as db
from app.agent.orchestrator import (
    is_rss_diagnosis_message,
    is_rss_failure_retry_write_message,
    rss_failure_retry_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.rss_retry_actions import (
    preview_rss_failure_retry,
    retry_failed_rss_to_qb,
    rss_failure_retry_arguments,
    rss_failure_retry_confirmation_context,
)
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.clients.qbittorrent import QBittorrentClient, TorrentAddResult
from app.main import create_app
from app.modules.rss import RSSEngine
from tests.support import IsolatedDatabaseTestCase


def _clear_rss() -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM rss_entries")
        conn.execute("DELETE FROM rss_items")
        conn.execute("DELETE FROM download_log")


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
    def _entry(sub_id: int, index: int, *, payload: str | None = None) -> int:
        entry_id = db.add_rss_entry(
            sub_id,
            f"Private Episode {index}",
            f"secret-guid-{index}",
            payload=payload or json.dumps({
                "torrent_url": f"magnet:?xt=urn:btih:SECRET{index}"
            }),
        )
        assert entry_id is not None
        return entry_id

    def _failed(self, sub_id: int, index: int, code="qb_unavailable", retryable=True) -> int:
        entry_id = self._entry(sub_id, index)
        db.record_rss_entry_failure(entry_id, code, retryable)
        return entry_id

    def test_arguments_registry_and_natural_language_are_strict(self):
        self.assertEqual(rss_failure_retry_arguments({}), {"limit": 10})
        self.assertEqual(rss_failure_retry_arguments({"limit": 3}), {"limit": 3})
        for invalid in (
            {"limit": 0}, {"limit": 21}, {"limit": True}, {"limit": "2"},
            {"limit": 2, "entry_ids": [1]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                rss_failure_retry_arguments(invalid)

        tools = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        spec = tools["rss.retry_failed_to_qb"]
        self.assertEqual(spec["risk"], "danger")
        self.assertTrue(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])
        with self.assertRaises(AgentToolError) as direct:
            get_agent_service().registry.execute("rss.retry_failed_to_qb", {"limit": 1})
        self.assertEqual(direct.exception.code, "confirmation_required")

        self.assertEqual(rss_failure_retry_request("重试 5 个 RSS 失败条目"), {"limit": 5})
        self.assertEqual(rss_failure_retry_request("重新提交 RSS 失败条目"), {"limit": 10})
        for message in (
            "重试全部 RSS 失败条目", "重试 21 个 RSS 失败条目",
            "重试光鸭 RSS 失败条目", "查看 RSS 失败状态", "诊断 RSS 失败原因",
            "提交待处理 RSS 条目",
        ):
            with self.subTest(message=message):
                self.assertIsNone(rss_failure_retry_request(message))
        self.assertTrue(is_rss_failure_retry_write_message("重试 RSS 失败条目"))
        self.assertTrue(is_rss_diagnosis_message("查看 RSS 失败状态"))

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
        self.assertEqual(success.add_torrent_detailed(urls="magnet:?xt=test"), TorrentAddResult(True))
        self.assertTrue(success.add_torrent(urls="magnet:?xt=test"))

    def test_qb_login_failure_is_classified_without_response_body_leak(self):
        client = QBittorrentClient("http://qb.internal:8080", username="u", password="secret")
        client._session.post = MagicMock(return_value=_Response(200, "Fails. passkey=SECRET"))
        with patch("app.clients.qbittorrent.logger.warning") as warning:
            result = client.add_torrent_detailed(urls="magnet:?xt=PRIVATESECRET")
        self.assertEqual(result, TorrentAddResult(False, "qb_auth_failed", False))
        rendered = repr(warning.call_args_list)
        self.assertNotIn("PRIVATESECRET", rendered)
        self.assertNotIn("passkey", rendered)

    def test_preview_selects_retryable_failed_qb_only_and_is_sanitized(self):
        qb_sub = self._subscription()
        gy_sub = self._subscription("GuangYa RSS", "guangya")
        selected = [self._failed(qb_sub, index) for index in range(1, 4)]
        self._failed(qb_sub, 10, "qb_auth_failed", False)
        self._failed(gy_sub, 90)
        db.update_rss_entries_processed([selected[0]], True)

        with patch("app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed") as add:
            result = preview_rss_failure_retry({"limit": 2})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["selected_count"], 2)
        self.assertFalse(result.data["has_more"])
        add.assert_not_called()
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "Private Episode", "secret-guid", "SECRET", "private/downloads",
            "qb.internal", "agent-user", "rss-agent", "passkey", "qb_unavailable",
        ):
            self.assertNotIn(secret, serialized)

    def test_database_claim_is_all_or_nothing_and_increments_retry_count(self):
        sub_id = self._subscription()
        first = self._failed(sub_id, 1)
        second = self._failed(sub_id, 2)
        rows = db.get_retryable_failed_rss_qb_snapshot(default_method="qb", limit=2)
        expected = [dict(row) for row in rows]
        expected = [{key: item[key] for key in (
            "id", "rss_item_id", "title", "payload", "created_at", "failure_code",
            "failure_retryable", "retry_count", "failed_at", "download_method", "qb_save_path",
        )} for item in expected]
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET failure_code='qb_auth_failed' WHERE id=?", (first,))
        self.assertEqual(db.claim_retryable_failed_rss_qb_entries(expected), [])
        self.assertEqual(db.get_rss_entry(first)["status"], "failed")
        self.assertEqual(db.get_rss_entry(second)["status"], "failed")
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET failure_code='qb_unavailable' WHERE id=?", (first,))

        fresh_rows = db.get_retryable_failed_rss_qb_snapshot(default_method="qb", limit=2)
        fresh = [{key: row[key] for key in (
            "id", "rss_item_id", "title", "payload", "created_at", "failure_code",
            "failure_retryable", "retry_count", "failed_at", "download_method", "qb_save_path",
        )} for row in fresh_rows]
        claimed = db.claim_retryable_failed_rss_qb_entries(fresh)
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
            db.get_retryable_failed_rss_qb_snapshot(default_method="qb", limit=10),
            [],
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET failed_at=datetime('now','localtime','-61 seconds') "
                "WHERE id=?",
                (rate_limited,),
            )
        rows = db.get_retryable_failed_rss_qb_snapshot(default_method="qb", limit=10)
        self.assertEqual([row["id"] for row in rows], [rate_limited])

    def test_confirmation_stale_and_confirm_response_are_aggregate_only(self):
        sub_id = self._subscription()
        first = self._failed(sub_id, 1)
        second = self._failed(sub_id, 2)
        fingerprint = rss_failure_retry_confirmation_context({"limit": 2})
        self.assertEqual(len(fingerprint), 64)
        raw = {
            "ok": True, "conflict": False, "requested": 2,
            "claimed": 2, "submitted": 1, "failed": 1,
            "error": "QB_SECRET /private/path qb_unavailable",
        }
        with patch.object(RSSEngine, "retry_failed_qb_snapshot", return_value=raw) as retry:
            result = retry_failed_rss_to_qb({"limit": 2})
        expected_rows, runtime = retry.call_args.args
        self.assertEqual([item["id"] for item in expected_rows], [second, first])
        self.assertEqual(runtime, self.runtime)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data, {
            "target": "qbittorrent", "requested": 2, "claimed": 2,
            "submitted": 1, "failed": 1,
        })
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("QB_SECRET", serialized)
        self.assertNotIn("/private", serialized)
        self.assertNotIn("qb_unavailable", serialized)

        third = self._failed(sub_id, 3)
        service = get_agent_service()
        prepared = service.prepare("rss.retry_failed_to_qb", {"limit": 1}, owner="owner")
        db.record_rss_entry_failure(third, "qb_rate_limited", True)
        with patch.object(RSSEngine, "retry_failed_qb_snapshot") as handler:
            with self.assertRaises(AgentToolError) as stale:
                service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")
        self.assertEqual(stale.exception.code, "confirmation_stale")
        handler.assert_not_called()

    def test_unknown_retry_requires_qb_review_before_another_attempt(self):
        sub_id = self._subscription()
        self._failed(sub_id, 1)
        rss_failure_retry_confirmation_context({"limit": 1})
        raw = {
            "ok": False, "conflict": False, "requested": 1,
            "claimed": 1, "submitted": 0, "failed": 1,
            "outcome_unknown": 1,
        }
        with patch.object(RSSEngine, "retry_failed_qb_snapshot", return_value=raw):
            result = retry_failed_rss_to_qb({"limit": 1})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.data["outcome_unknown"], 1)
        self.assertIn("待核对 1", result.summary)
        self.assertIn("勿直接重试", result.error)

    def test_mixed_unknown_retry_reports_all_three_outcomes(self):
        sub_id = self._subscription()
        for index in range(1, 4):
            self._failed(sub_id, index)
        rss_failure_retry_confirmation_context({"limit": 3})
        raw = {
            "ok": False, "conflict": False, "requested": 3,
            "claimed": 3, "submitted": 1, "failed": 2,
            "outcome_unknown": 1,
        }
        with patch.object(RSSEngine, "retry_failed_qb_snapshot", return_value=raw):
            result = retry_failed_rss_to_qb({"limit": 3})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertIn("成功 1", result.summary)
        self.assertIn("待核对 1", result.summary)
        self.assertIn("确认失败 1", result.summary)
        self.assertIn("勿直接重复提交", result.suggestions[0])

    def test_retry_engine_propagates_unknown_outcome_count(self):
        sub_id = self._subscription()
        entry_id = self._failed(sub_id, 77)
        unique_hash = "c" * 40
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET payload=? WHERE id=?",
                (json.dumps({"torrent_url": f"magnet:?xt=urn:btih:{unique_hash}"}), entry_id),
            )
        rows = db.get_retryable_failed_rss_qb_snapshot(default_method="qb", limit=1)
        expected = [{key: row[key] for key in (
            "id", "rss_item_id", "title", "payload", "created_at", "failure_code",
            "failure_retryable", "retry_count", "failed_at", "download_method", "qb_save_path",
        )} for row in rows]

        with patch(
            "app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed",
            return_value=TorrentAddResult(False, "qb_outcome_unknown", False),
        ):
            result = RSSEngine().retry_failed_qb_snapshot(expected, self.runtime)

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["outcome_unknown"], 1)
        row = db.get_rss_entry(entry_id)
        self.assertEqual(row["failure_code"], "qb_outcome_unknown")
        self.assertEqual(row["failure_retryable"], 0)

    def test_engine_records_retry_result_and_preserves_aggregate_contract(self):
        sub_id = self._subscription()
        success_id = self._failed(sub_id, 1)
        failed_id = self._failed(sub_id, 2)
        rows = db.get_retryable_failed_rss_qb_snapshot(default_method="qb", limit=2)
        expected = [{key: row[key] for key in (
            "id", "rss_item_id", "title", "payload", "created_at", "failure_code",
            "failure_retryable", "retry_count", "failed_at", "download_method", "qb_save_path",
        )} for row in rows]
        outcomes = [
            TorrentAddResult(False, "qb_rate_limited", True),
            TorrentAddResult(True),
        ]
        with patch("app.clients.qbittorrent.QBittorrentClient.add_torrent_detailed", side_effect=outcomes):
            result = RSSEngine().retry_failed_qb_snapshot(expected, self.runtime)
        self.assertEqual(result, {
            "ok": False, "conflict": False, "requested": 2,
            "claimed": 2, "submitted": 1, "failed": 1,
        })
        # rows are newest first: failed_id receives first outcome.
        retried_failure = db.get_rss_entry(failed_id)
        self.assertEqual(retried_failure["status"], "failed")
        self.assertEqual(retried_failure["failure_code"], "qb_rate_limited")
        self.assertEqual(retried_failure["failure_retryable"], 1)
        self.assertEqual(retried_failure["retry_count"], 1)
        self.assertEqual(db.get_rss_entry(success_id)["status"], "downloaded")


class RssFailureRetryAPITests(IsolatedDatabaseTestCase):
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
        assert self.entry_id is not None
        db.record_rss_entry_failure(self.entry_id, "qb_unavailable", True)
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

    def test_query_confirm_replay_direct_gate_csrf_and_sanitization(self):
        path = "/api/agent/actions/rss.retry_failed_to_qb/prepare"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {"limit": 1}}).status_code, 401)
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {"limit": 1}}).status_code, 403)
        raw = {
            "ok": True, "conflict": False, "requested": 1,
            "claimed": 1, "submitted": 1, "failed": 0,
        }
        with patch.object(RSSEngine, "retry_failed_qb_snapshot", return_value=raw) as retry:
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "重试 1 个 RSS 失败条目"},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            body = prepared.json()
            self.assertEqual(body["mode"], "confirmation_required")
            confirmation_id = body["confirmation"]["confirmation_id"]

            direct = self.client.post(
                "/api/agent/tools/rss.retry_failed_to_qb",
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
            replay = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(replay.status_code, 409, replay.text)
            retry.assert_called_once()

        serialized = prepared.text + confirmed.text
        for secret in (
            "Private Episode", "secret-guid", "RSSSECRET", "private/downloads",
            "qb.internal", "QB_SECRET", "passkey", "qb_unavailable",
        ):
            self.assertNotIn(secret, serialized)

    def test_query_and_explicit_prepare_share_three_per_minute_limit(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        for _ in range(3):
            response = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "重试 1 个 RSS 失败条目"},
            )
            self.assertEqual(response.status_code, 200, response.text)
        limited = self.client.post(
            "/api/agent/actions/rss.retry_failed_to_qb/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"limit": 1}},
        )
        self.assertEqual(limited.status_code, 429, limited.text)
