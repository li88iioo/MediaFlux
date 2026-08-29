"""本地媒体 Agent 诊断的聚合、脱敏、路由与 API 契约。"""
from __future__ import annotations

import json
import re
from unittest.mock import ANY, Mock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.local_media_actions import (
    diagnose_local_media,
    local_media_diagnosis_arguments,
)
from app.agent.models import ToolContext
from app.agent.local_media_intents import is_local_media_diagnosis_message
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_automation_pipeline_diagnosis_message,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class LocalMediaDiagnosisUnitTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")

    @staticmethod
    def _source(*, owner: str = "admin", enabled: int = 1, scan_enabled: int = 0,
                mode: str = "move", suffix: str = "one") -> int:
        return db.create_local_media_source(
            name=f"SECRET_SOURCE_{suffix}",
            qb_profile="configured:qb",
            qb_path_prefix=f"/remote/SECRET_{suffix}",
            local_root=f"/private/SECRET_{suffix}",
            enabled=enabled,
            scan_enabled=scan_enabled,
            owner=owner,
            mode=mode,
        )

    @staticmethod
    def _task(source_id: int, status: str, *, trigger: str = "manual",
              owner: str = "admin", suffix: str = "one") -> int:
        task_id = db.create_local_media_task(
            source_id,
            f"HASH_SECRET_{suffix}",
            f"/private/TITLE_SECRET_{suffix}.mkv",
            owner=owner,
            trigger=trigger,
        )
        if status != "waiting_stable":
            db.update_local_media_task(
                task_id,
                owner=owner,
                status=status,
                title=f"TITLE_SECRET_{suffix}",
                error=f"ERROR_SECRET_{suffix} /private/path",
                warning=f"WARNING_SECRET_{suffix}",
                tmdb_id="987654",
            )
        return task_id

    def test_arguments_reject_every_extra_field(self):
        self.assertEqual(local_media_diagnosis_arguments({}), {})
        with self.assertRaisesRegex(AgentToolError, r"^local_media\.diagnose 不接受参数$"):
            local_media_diagnosis_arguments({"path": "/private"})

    def test_database_summary_is_owner_scoped_and_classifies_states(self):
        source = self._source(scan_enabled=1)
        db.upsert_local_library_target(source, "movie", "/library/SECRET", owner="admin")
        self._task(source, "waiting_stable", trigger="qb_completed", suffix="waiting")
        self._task(source, "recognizing", trigger="scan", suffix="active")
        self._task(source, "requires_manual", trigger="manual", suffix="manual")
        self._task(source, "planned", trigger="manual", suffix="planned")
        self._task(source, "failed", trigger="manual", suffix="failed")
        self._task(source, "completed", trigger="qb_completed", suffix="completed")
        other = self._source(owner="other", suffix="other")
        self._task(other, "failed", owner="other", suffix="other")

        summary = db.get_local_media_diagnostic_summary(owner="admin")

        self.assertEqual(summary["sources"], {
            "total": 1,
            "enabled": 1,
            "disabled": 0,
            "scan_enabled": 1,
            "move_mode": 1,
            "preview_only_mode": 0,
            "enabled_without_targets": 0,
        })
        self.assertEqual(summary["tasks"], {
            "total": 6,
            "waiting_stable": 1,
            "active": 1,
            "requires_manual": 1,
            "planned": 1,
            "failed": 1,
            "completed": 1,
            "qb_completed": 2,
            "scan": 1,
            "manual": 3,
        })

    def test_empty_and_inactive_states_are_explicit(self):
        with patch("app.agent.local_media_actions.peek_local_media_scheduler_status") as scheduler:
            scheduler.return_value = {"running": False, "interval_seconds": 10}
            empty = diagnose_local_media({})
            self._source(enabled=0)
            inactive = diagnose_local_media({})

        self.assertEqual(empty.status, "not_configured")
        self.assertTrue(empty.ok)
        self.assertEqual(inactive.status, "inactive")
        self.assertEqual(inactive.data["sources"]["disabled"], 1)

    def test_attention_and_active_states_preserve_planned_semantics(self):
        source = self._source()
        db.upsert_local_library_target(source, "default", "/library/default", owner="admin")
        self._task(source, "planned", suffix="planned")
        self._task(source, "waiting_stable", suffix="waiting")
        with patch("app.agent.local_media_actions.peek_local_media_scheduler_status") as scheduler:
            scheduler.return_value = {"running": True, "interval_seconds": 5}
            active = diagnose_local_media({})
        self.assertEqual(active.status, "active")
        self.assertEqual(active.data["attention"]["total"], 0)
        self.assertNotIn("scan_enabled", active.data["sources"])
        self.assertEqual(active.data["tasks"]["planned"], 1)

        self._task(source, "requires_manual", suffix="manual")
        self._task(source, "failed", suffix="failed")
        source_without_target = self._source(suffix="unmapped")
        self.assertIsInstance(source_without_target, int)
        with patch("app.agent.local_media_actions.peek_local_media_scheduler_status") as scheduler:
            scheduler.return_value = {"running": True, "interval_seconds": 5}
            attention = diagnose_local_media({})
        self.assertEqual(attention.status, "attention")
        self.assertEqual(attention.data["attention"], {
            "total": 3,
            "categories": {
                "requires_manual": 1,
                "failed": 1,
                "enabled_sources_without_targets": 1,
                "scheduler_not_running": 0,
            },
        })

    def test_scheduler_stopped_and_inactive_failures_keep_attention_consistent(self):
        source = self._source()
        db.upsert_local_library_target(source, "default", "/library/default", owner="admin")
        with patch(
            "app.agent.local_media_actions.peek_local_media_scheduler_status",
            return_value={"running": False, "interval_seconds": 0},
        ):
            stopped = diagnose_local_media({})
        self.assertEqual(stopped.status, "attention")
        self.assertEqual(stopped.data["attention"]["categories"]["scheduler_not_running"], 1)
        self.assertIn("调度器当前未运行", stopped.suggestions[-1])

        with db.get_conn() as conn:
            conn.execute("UPDATE local_media_sources SET enabled=0 WHERE owner='admin'")
        self._task(source, "failed", suffix="inactive-failed")
        with patch(
            "app.agent.local_media_actions.peek_local_media_scheduler_status",
            return_value={"running": False, "interval_seconds": 0},
        ):
            inactive_failed = diagnose_local_media({})
        self.assertEqual(inactive_failed.status, "attention")
        self.assertEqual(inactive_failed.data["attention"]["categories"]["failed"], 1)

    def test_diagnosis_does_not_initialize_scheduler_or_read_env_file(self):
        with patch("app.modules.local_media_scheduler._scheduler", None), patch(
            "app.config._read_env_file"
        ) as read_env:
            result = diagnose_local_media({})
            from app.modules import local_media_scheduler

            self.assertIsNone(local_media_scheduler._scheduler)
        read_env.assert_not_called()
        self.assertEqual(result.data["scheduler"], {
            "running": False,
            "interval_seconds": 0.0,
        })

    def test_sensitive_database_fields_never_leave_projection(self):
        source = self._source(suffix="leak")
        self._task(source, "failed", suffix="leak")
        with patch("app.agent.local_media_actions.peek_local_media_scheduler_status") as scheduler:
            scheduler.return_value = {"running": False, "interval_seconds": 10}
            result = diagnose_local_media({})

        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "SECRET_SOURCE_leak", "HASH_SECRET_leak", "TITLE_SECRET_leak",
            "ERROR_SECRET_leak", "WARNING_SECRET_leak", "/private", "987654",
        ):
            self.assertNotIn(secret, serialized)
        for forbidden_key in (
            '"source_id"', '"task_id"', '"content_path"', '"qb_hash"',
            '"title"', '"tmdb_id"', '"warning"',
        ):
            self.assertNotIn(forbidden_key, serialized)
        self.assertEqual(result.error, "")
        self.assertFalse(result.data["network_accessed"])

    def test_exception_returns_fixed_sanitized_error(self):
        with patch(
            "app.agent.local_media_actions.db.get_local_media_diagnostic_summary",
            side_effect=RuntimeError("LOCAL_SECRET /private/path"),
        ):
            result = diagnose_local_media({})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("LOCAL_SECRET", serialized)
        self.assertNotIn("/private/path", serialized)
        self.assertFalse(result.data["network_accessed"])

    def test_registry_and_natural_language_route_are_read_only_and_explicit(self):
        capabilities = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        spec = capabilities["local_media.diagnose"]
        self.assertEqual(spec["risk"], "read")
        self.assertFalse(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])

        for message in (
            "诊断本地媒体",
            "检查本地整理状态",
            "查看本地入库状态",
            "本地媒体调度健康吗",
            "检查本地媒体配置状态",
        ):
            self.assertTrue(is_local_media_diagnosis_message(message), message)
            self.assertFalse(is_automation_pipeline_diagnosis_message(message), message)
        for message in (
            "配置本地媒体",
            "新增本地媒体来源",
            "立即扫描本地媒体",
            "重试本地整理失败任务",
            "停止本地媒体任务",
            "启用本地媒体任务",
            "关闭本地媒体任务",
            "取消本地媒体任务",
            "运行本地媒体任务",
            "创建本地媒体任务",
            "清空本地媒体任务",
            "暂停本地整理任务",
            "恢复本地整理任务",
        ):
            self.assertFalse(is_local_media_diagnosis_message(message), message)

        registry = Mock()
        registry.execute.return_value = (diagnose_local_media({}), 1)
        agent = AgentOrchestrator(registry)
        response = agent.query("检查本地媒体自动化流程状态")
        self.assertEqual(response["tool_call"]["name"], "local_media.diagnose")
        registry.execute.assert_called_once_with(
            "local_media.diagnose", {}, context=ToolContext(owner="", request_id=ANY)
        )

        registry.reset_mock()
        response = agent.query("检查本地媒体配置状态")
        self.assertEqual(response["tool_call"]["name"], "local_media.diagnose")
        registry.execute.assert_called_once_with(
            "local_media.diagnose", {}, context=ToolContext(owner="", request_id=ANY)
        )

        for message in (
            "停止本地媒体任务",
            "启用本地媒体任务",
            "取消本地媒体任务",
            "运行本地媒体任务",
            "暂停本地整理任务",
        ):
            registry.reset_mock()
            response = agent.query(message)
            self.assertEqual(response["mode"], "clarification", message)
            self.assertEqual(
                response["result"]["status"], "clarification_required", message
            )
            registry.execute.assert_not_called()


class LocalMediaDiagnosisAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
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

    def test_api_auth_csrf_and_shared_direct_query_rate_limit(self):
        path = "/api/agent/tools/local_media.diagnose"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}

        for _ in range(4):
            response = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()["result"]
            self.assertEqual(result["data"]["probe_mode"], "local")
            self.assertFalse(result["data"]["network_accessed"])
        limited = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "检查本地媒体状态"},
        )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    import unittest
    unittest.main()
