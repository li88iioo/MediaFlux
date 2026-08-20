"""Media Agent STRM 失败安全重试的确认、脱敏与路由回归。"""
from __future__ import annotations

import json
import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.orchestrator import strm_failure_retry_request
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.agent.strm_retry_actions import (
    preview_strm_failure_retry,
    retry_strm_failure_records,
    strm_failure_retry_arguments,
    strm_failure_retry_confirmation_context,
)
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class StrmFailureRetryUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_failures")
        reset_agent_service_for_tests()
        self.runtime_config = {
            "sources": [{"id": "source", "name": "Source", "source_key": "source", "rel_prefix": ""}],
            "strm_root": "/safe/strm",
            "base_url": "http://media.local",
        }
        self.runtime_patcher = patch(
            "app.modules.strm.capture_strm_retry_runtime_config",
            return_value=(self.runtime_config, ""),
        )
        self.runtime_patcher.start()

    def tearDown(self):
        self.runtime_patcher.stop()
        reset_agent_service_for_tests()

    @staticmethod
    def _record(index: int, action: str = "generate") -> int:
        return db.record_strm_failure(
            source_id=f"secret-source-{index}",
            source_name=f"Secret Source {index}",
            file_id=f"private-file-{index}",
            parent_id=f"private-parent-{index}",
            filename=f"private-{index}.mkv",
            action=action,
            rel_dir=f"/private/source/{index}",
            target_rel_path=f"/private/target/{index}.strm",
            error=f"token=SECRET-{index} https://private.example/{index}",
        )

    def test_arguments_and_registry_contract_are_strict(self):
        self.assertEqual(strm_failure_retry_arguments({}), {"scope": "all"})
        for invalid in (
            {"scope": "ALL"},
            {"scope": " all"},
            {"scope": "invalid"},
            {"scope": True},
            {"scope": "all", "ids": [1]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                strm_failure_retry_arguments(invalid)

        tools = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        spec = tools["strm.retry_failures"]
        self.assertEqual(spec["risk"], "danger")
        self.assertTrue(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])
        self.assertEqual(
            spec["parameters"]["properties"]["scope"]["enum"],
            ["all", "generate", "metadata"],
        )
        with self.assertRaises(AgentToolError) as direct:
            get_agent_service().registry.execute("strm.retry_failures", {"scope": "all"})
        self.assertEqual(direct.exception.code, "confirmation_required")

    def test_preview_is_read_only_bounded_and_never_projects_sensitive_rows(self):
        self._record(1, "generate")
        self._record(2, "metadata")
        with patch("app.modules.strm.retry_strm_failures") as retry:
            result = preview_strm_failure_retry({"scope": "all"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["selected_count"], 2)
        self.assertEqual(result.data["by_action"], {"generate": 1, "metadata": 1})
        retry.assert_not_called()
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in ("secret-source", "private-file", "/private", "SECRET", "private.example"):
            self.assertNotIn(secret, serialized)

        for index in range(3, 103):
            self._record(index)
        limited = preview_strm_failure_retry({"scope": "all"})
        self.assertFalse(limited.ok)
        self.assertEqual(limited.status, "conflict")
        self.assertNotIn("private-", json.dumps(limited.to_dict(), ensure_ascii=False))

    def test_confirmation_context_freezes_ids_and_handler_returns_only_counts(self):
        first = self._record(1, "generate")
        second = self._record(2, "metadata")
        fingerprint = strm_failure_retry_confirmation_context({"scope": "all"})
        self.assertEqual(len(fingerprint), 64)
        raw = {
            "ok": True,
            "requested": 2,
            "matched": 2,
            "resolved": 1,
            "failed": 1,
            "missing": 1,
            "stale": 1,
            "error": "token=SECRET /private/path",
            "failures": [{"filename": "private.mkv"}],
        }
        with patch("app.modules.strm.retry_strm_failures", return_value=raw) as retry:
            result = retry_strm_failure_records({"scope": "all"})
        retry.assert_called_once_with([second, first], "agent", runtime_config=self.runtime_config)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(
            result.data,
            {
                "requested": 2,
                "matched": 2,
                "resolved": 1,
                "failed": 1,
                "missing": 1,
                "stale": 1,
                "deferred": 0,
                "scope": "all",
            },
        )
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("/private", serialized)
        self.assertNotIn("filename", serialized)

    def test_prepare_binds_the_exact_preview_snapshot_without_a_second_capture(self):
        self._record(1)
        from app.agent import strm_retry_actions

        with patch(
            "app.agent.strm_retry_actions._capture",
            wraps=strm_retry_actions._capture,
        ) as capture:
            prepared = get_agent_service().prepare(
                "strm.retry_failures", {"scope": "all"}, owner="owner-token"
            )
        self.assertEqual(prepared["result"]["data"]["selected_count"], 1)
        self.assertEqual(capture.call_count, 1)

    def test_same_second_repeated_failure_invalidates_confirmation(self):
        fixed_time = "2026-08-02 11:30:00"
        with patch("app.database.now", return_value=fixed_time):
            self._record(1)
            service = get_agent_service()
            prepared = service.prepare(
                "strm.retry_failures", {"scope": "all"}, owner="owner-token"
            )
            confirmation_id = prepared["confirmation"]["confirmation_id"]
            self._record(1)
        with patch("app.modules.strm.retry_strm_failures") as retry:
            with self.assertRaises(AgentToolError) as stale:
                service.confirm(confirmation_id, owner="owner-token")
        self.assertEqual(stale.exception.code, "confirmation_stale")
        retry.assert_not_called()

    def test_preview_rejects_invalid_runtime_config_without_leaking_reason(self):
        self._record(1)
        with patch(
            "app.modules.strm.capture_strm_retry_runtime_config",
            return_value=({}, "secret /private/path token=SECRET"),
        ):
            result = preview_strm_failure_retry({"scope": "all"})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "not_configured")
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("/private", serialized)

    def test_partially_claimed_snapshot_reports_partial_completion(self):
        self._record(1)
        self._record(2)
        strm_failure_retry_confirmation_context({"scope": "all"})
        raw = {
            "ok": True,
            "requested": 2,
            "matched": 1,
            "resolved": 1,
            "failed": 0,
            "missing": 0,
            "stale": 0,
        }
        with patch("app.modules.strm.retry_strm_failures", return_value=raw):
            result = retry_strm_failure_records({"scope": "all"})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertIn("状态已变化", result.summary)

    def test_zero_matches_after_confirmation_is_reported_as_conflict(self):
        self._record(1)
        strm_failure_retry_confirmation_context({"scope": "all"})
        raw = {
            "ok": True,
            "requested": 1,
            "matched": 0,
            "resolved": 0,
            "failed": 0,
            "missing": 0,
            "stale": 0,
        }
        with patch("app.modules.strm.retry_strm_failures", return_value=raw):
            result = retry_strm_failure_records({"scope": "all"})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.data["matched"], 0)

    def test_confirmation_becomes_stale_when_failure_snapshot_changes(self):
        failure_id = self._record(1)
        service = get_agent_service()
        prepared = service.prepare(
            "strm.retry_failures", {"scope": "all"}, owner="owner-token"
        )
        confirmation_id = prepared["confirmation"]["confirmation_id"]
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_failures SET updated_at=? WHERE id=?",
                ("2099-01-01T00:00:00+00:00", failure_id),
            )
        with patch("app.modules.strm.retry_strm_failures") as retry:
            with self.assertRaises(AgentToolError) as stale:
                service.confirm(confirmation_id, owner="owner-token")
        self.assertEqual(stale.exception.code, "confirmation_stale")
        retry.assert_not_called()

    def test_retry_intent_extracts_safe_scope_and_rejects_destructive_requests(self):
        self.assertEqual(strm_failure_retry_request("重试 STRM 失败记录"), {"scope": "all"})
        self.assertEqual(strm_failure_retry_request("只重试 STRM 生成失败"), {"scope": "generate"})
        self.assertEqual(strm_failure_retry_request("重新处理 STRM 字幕错误"), {"scope": "metadata"})
        for message in (
            "删除 STRM 失败记录",
            "修复 STRM 错误",
            "重试并删除 STRM 失败",
            "重试并恢复 STRM 失败记录",
            "不要重试 STRM 失败记录",
            "不用重试 STRM 失败记录",
            "能否重试 STRM 失败记录",
            "是否可以重试 STRM 失败记录",
            "如何重试 STRM 失败记录？",
            "重试 STRM 失败记录吗？",
            "如果重试 STRM 失败记录会怎样",
            "查看 STRM 失败重试状态",
            "STRM 失败重试情况怎么样",
        ):
            self.assertIsNone(strm_failure_retry_request(message), message)


class StrmFailureRetryAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_failures")
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.runtime_config = {
            "sources": [{"id": "source", "name": "Source", "source_key": "source", "rel_prefix": ""}],
            "strm_root": "/safe/strm",
            "base_url": "http://media.local",
        }
        self.runtime_patcher = patch(
            "app.modules.strm.capture_strm_retry_runtime_config",
            return_value=(self.runtime_config, ""),
        )
        self.runtime_patcher.start()
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

    @staticmethod
    def _record() -> int:
        return db.record_strm_failure(
            source_id="secret-source",
            source_name="Secret Source",
            file_id="private-file",
            parent_id="private-parent",
            filename="private.mkv",
            action="generate",
            rel_dir="/private/source",
            target_rel_path="/private/target.strm",
            error="token=SECRET https://private.example/resource",
        )

    def test_query_prepare_confirm_replay_and_direct_call_are_gated(self):
        failure_id = self._record()
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        raw = {
            "ok": True,
            "requested": 1,
            "matched": 1,
            "resolved": 1,
            "failed": 0,
            "missing": 0,
            "stale": 0,
            "error": "token=SECRET /private/path",
        }
        with patch("app.modules.strm.retry_strm_failures", return_value=raw) as retry:
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "只重试 STRM 生成失败"},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            body = prepared.json()
            self.assertEqual(body["mode"], "confirmation_required")
            self.assertEqual(body["result"]["data"]["selected_count"], 1)
            confirmation_id = body["confirmation"]["confirmation_id"]
            retry.assert_not_called()

            direct = self.client.post(
                "/api/agent/tools/strm.retry_failures",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"scope": "generate"}},
            )
            self.assertEqual(direct.status_code, 409, direct.text)
            retry.assert_not_called()

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertEqual(confirmed.json()["result"]["status"], "completed")
            retry.assert_called_once_with([failure_id], "agent", runtime_config=self.runtime_config)

            replay = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
            self.assertEqual(replay.status_code, 409, replay.text)
            retry.assert_called_once()

        serialized = prepared.text + confirmed.text
        for secret in ("secret-source", "private-file", "/private", "SECRET", "private.example"):
            self.assertNotIn(secret, serialized)

    def test_prepare_is_strict_and_query_shares_three_per_minute_limit(self):
        self._record()
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        rejected = self.client.post(
            "/api/agent/actions/strm.retry_failures/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"scope": "all", "ids": [1]}},
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        agent_rate_limiter.reset()

        for _ in range(3):
            response = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "重试 STRM 失败记录"},
            )
            self.assertEqual(response.status_code, 200, response.text)
        limited = self.client.post(
            "/api/agent/actions/strm.retry_failures/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"scope": "all"}},
        )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    import unittest

    unittest.main()
