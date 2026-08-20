"""Media Agent STRM 失败分诊的安全、路由与 API 回归测试。"""
from __future__ import annotations

import json
import re
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.models import ToolContext
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_strm_failure_triage_message,
    is_strm_failure_write_message,
    strm_failure_retry_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.agent.strm_failure_actions import triage_strm_failures
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class StrmFailureTriageUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_failures")
        reset_agent_service_for_tests()

    def tearDown(self):
        reset_agent_service_for_tests()

    @staticmethod
    def _record(*, source: str, file_id: str, action: str, error: str) -> int:
        return db.record_strm_failure(
            source_id=source,
            source_name=f"source-{source}",
            file_id=file_id,
            parent_id=f"parent-{file_id}",
            filename=f"{file_id}.mkv",
            action=action,
            rel_dir=f"/private/{source}",
            target_rel_path=f"/target/{file_id}.strm",
            error=error,
        )

    def test_database_summary_counts_known_states_without_sensitive_projection(self):
        repeated_id = self._record(
            source="SECRET-SOURCE-A",
            file_id="SECRET-FILE-A",
            action="generate",
            error="TOKEN-SECRET https://private.example/a /private/a",
        )
        self._record(
            source="SECRET-SOURCE-A",
            file_id="SECRET-FILE-A",
            action="generate",
            error="TOKEN-SECRET-2 /private/a2",
        )
        retrying_id = self._record(
            source="SECRET-SOURCE-B",
            file_id="SECRET-FILE-B",
            action="metadata",
            error="UUID-SECRET-B /private/b",
        )
        resolved_id = self._record(
            source="SECRET-SOURCE-C",
            file_id="SECRET-FILE-C",
            action="metadata",
            error="PATH-SECRET-C /private/c",
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_failures SET status='retrying',retry_count=2 WHERE id=?",
                (retrying_id,),
            )
            conn.execute(
                "UPDATE strm_failures SET status='resolved',resolved_at=updated_at WHERE id=?",
                (resolved_id,),
            )
            conn.execute(
                "INSERT INTO strm_failures("
                "source_id,source_name,file_id,parent_id,filename,action,rel_dir,target_rel_path,error,"
                "status,failure_count,retry_count,created_at,updated_at,resolved_at"
                ") VALUES('ignored','ignored','ignored','ignored','ignored','unknown','','','','open',9,9,datetime('now'),datetime('now'),NULL)"
            )

        summary = db.get_strm_failure_triage_summary()
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["open"], 1)
        self.assertEqual(summary["retrying"], 1)
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(summary["active_repeated"], 1)
        self.assertEqual(summary["active_retried"], 1)
        self.assertEqual(summary["by_action"]["generate"], {
            "total": 1, "open": 1, "retrying": 0, "resolved": 0,
        })
        self.assertEqual(summary["by_action"]["metadata"], {
            "total": 2, "open": 0, "retrying": 1, "resolved": 1,
        })
        self.assertIsInstance(repeated_id, int)
        serialized = json.dumps(summary, ensure_ascii=False)
        for secret in ("SECRET", "/private", "UUID", "PATH", "source-", ".mkv"):
            self.assertNotIn(secret, serialized)

    def test_result_states_and_sensitive_fields_are_fixed_and_redacted(self):
        empty = triage_strm_failures({})
        self.assertTrue(empty.ok)
        self.assertEqual(empty.status, "healthy")
        self.assertEqual(empty.data["probe_mode"], "database")
        self.assertFalse(empty.data["network_accessed"])
        self.assertFalse(empty.data["filesystem_accessed"])

        failure_id = self._record(
            source="PRIVATE-SOURCE-ID",
            file_id="PRIVATE-FILE-ID",
            action="generate",
            error="secret-token=https://private.example/token /home/private/video.mkv",
        )
        attention = triage_strm_failures({})
        self.assertFalse(attention.ok)
        self.assertEqual(attention.status, "attention")
        self.assertEqual(attention.data["failures"]["open"], 1)
        serialized = json.dumps(attention.to_dict(), ensure_ascii=False)
        for secret in (
            "PRIVATE-SOURCE-ID", "PRIVATE-FILE-ID", "source-PRIVATE", "video.mkv",
            "/home/private", "private.example", "secret-token",
        ):
            self.assertNotIn(secret, serialized)

        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_failures SET status='retrying',retry_count=1 WHERE id=?",
                (failure_id,),
            )
        running = triage_strm_failures({})
        self.assertTrue(running.ok)
        self.assertEqual(running.status, "running")
        self.assertEqual(running.data["failures"]["retrying"], 1)

        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_failures SET status='resolved',resolved_at=updated_at WHERE id=?",
                (failure_id,),
            )
        resolved = triage_strm_failures({})
        self.assertTrue(resolved.ok)
        self.assertEqual(resolved.status, "healthy")
        self.assertEqual(resolved.data["failures"]["resolved"], 1)

    def test_triage_never_reuses_raw_helpers_filesystem_or_retry(self):
        self._record(
            source="source-a",
            file_id="file-a",
            action="metadata",
            error="private failure",
        )
        with patch("app.database.list_strm_failures", side_effect=AssertionError("raw rows used")) as raw, patch(
            "app.database.summarize_strm_failures", side_effect=AssertionError("source summary used")
        ) as source_summary, patch(
            "app.database.list_strm_index_diagnostics", side_effect=AssertionError("filesystem used")
        ) as diagnostics, patch(
            "app.modules.strm.retry_strm_failures", side_effect=AssertionError("retry used")
        ) as retry:
            result = triage_strm_failures({})
        self.assertEqual(result.status, "attention")
        raw.assert_not_called()
        source_summary.assert_not_called()
        diagnostics.assert_not_called()
        retry.assert_not_called()

    def test_arguments_exception_registry_and_routes_are_strict(self):
        capabilities = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        spec = capabilities["strm.triage_failures"]
        self.assertEqual(spec["risk"], "read")
        self.assertFalse(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])
        with self.assertRaises(AgentToolError):
            get_agent_service().registry.execute("strm.triage_failures", {"unexpected": True})

        with patch(
            "app.agent.strm_failure_actions.db.get_strm_failure_triage_summary",
            side_effect=RuntimeError("token=SECRET /private/database.sqlite"),
        ):
            unavailable = triage_strm_failures({})
        self.assertFalse(unavailable.ok)
        self.assertEqual(unavailable.status, "unavailable")
        self.assertNotIn("SECRET", json.dumps(unavailable.to_dict(), ensure_ascii=False))
        self.assertNotIn("/private", json.dumps(unavailable.to_dict(), ensure_ascii=False))

        for message in (
            "查看 STRM 失败记录",
            "诊断 STRM 失败原因",
            "统计 STRM 异常状态",
            "STRM 失败怎么了",
        ):
            self.assertTrue(is_strm_failure_triage_message(message), message)
            self.assertFalse(is_strm_failure_write_message(message), message)
        for message in (
            "重试 STRM 失败记录",
            "开始重试 STRM 同步失败记录",
            "只重试 STRM 生成失败",
            "重新处理 STRM 元数据错误",
        ):
            self.assertFalse(is_strm_failure_triage_message(message), message)
            self.assertTrue(is_strm_failure_write_message(message), message)
            self.assertIsNotNone(strm_failure_retry_request(message), message)
        for message in (
            "修复 STRM 失败",
            "删除 STRM 错误记录",
            "立即修复 STRM 同步错误",
            "执行删除 STRM 同步失败记录",
        ):
            self.assertFalse(is_strm_failure_triage_message(message), message)
            self.assertTrue(is_strm_failure_write_message(message), message)
            self.assertIsNone(strm_failure_retry_request(message), message)

        registry = Mock()
        registry.execute.return_value = (triage_strm_failures({}), 1)
        agent = AgentOrchestrator(registry)
        response = agent.query("查看 STRM 失败状态")
        self.assertEqual(response["tool_call"]["name"], "strm.triage_failures")
        registry.execute.assert_called_once_with(
            "strm.triage_failures", {}, context=ToolContext(owner="")
        )

        for message in (
            "立即修复 STRM 同步错误",
            "执行删除 STRM 同步失败记录",
        ):
            registry.reset_mock()
            response = agent.query(message, owner="owner-token")
            self.assertEqual(response["result"]["status"], "unsupported", message)
            registry.execute.assert_not_called()
            self.assertIsNone(response.get("confirmation"), message)

        registry.reset_mock()
        registry.execute.return_value = (triage_strm_failures({}), 1)
        self.assertEqual(agent.query("STRM 同步进度")["tool_call"]["name"], "strm.status")
        self.assertEqual(agent.query("检查 STRM 是否健康")["tool_call"]["name"], "strm.diagnose")


class StrmFailureTriageAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_failures")
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
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

    def test_api_rejects_invalid_argument_shapes_before_database_read(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        path = "/api/agent/tools/strm.triage_failures"
        with patch(
            "app.agent.strm_failure_actions.db.get_strm_failure_triage_summary"
        ) as summary:
            for body in (
                {"arguments": {"unexpected": True}},
                {"arguments": []},
                {"arguments": {}, "extra": True},
            ):
                with self.subTest(body=body):
                    response = self.client.post(path, headers=headers, json=body)
                    self.assertEqual(response.status_code, 400, response.text)
            summary.assert_not_called()

    def test_query_and_direct_tool_share_rate_limit_in_reverse(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        for _ in range(4):
            response = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "查看 STRM 失败记录"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["tool_call"]["name"], "strm.triage_failures")
        limited = self.client.post(
            "/api/agent/tools/strm.triage_failures",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {}},
        )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_api_auth_csrf_redaction_and_shared_direct_query_rate_limit(self):
        db.record_strm_failure(
            source_id="API-SECRET-SOURCE",
            source_name="API-SECRET-NAME",
            file_id="API-SECRET-FILE",
            parent_id="API-SECRET-PARENT",
            filename="API-SECRET.mkv",
            action="generate",
            rel_dir="/api/private/source",
            target_rel_path="/api/private/target.strm",
            error="api-token=API-SECRET-TOKEN https://private.example/api",
        )
        path = "/api/agent/tools/strm.triage_failures"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}

        for _ in range(4):
            response = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}})
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["tool_call"]["name"], "strm.triage_failures")
            self.assertEqual(payload["result"]["data"]["failures"]["open"], 1)
            serialized = json.dumps(payload, ensure_ascii=False)
            for secret in ("API-SECRET", "/api/private", "private.example", "api-token"):
                self.assertNotIn(secret, serialized)

        limited = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "查看 STRM 失败记录"},
        )
        self.assertEqual(limited.status_code, 429, limited.text)
