"""Media Agent 统一工作区待办的安全、路由与 API 回归测试。"""
from __future__ import annotations

import json
import re
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.models import ToolContext, ToolResult
from app.agent.orchestrator import AgentOrchestrator, is_workspace_todo_message
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.agent.workspace_todo_actions import summarize_workspace_todo, workspace_todo_arguments
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _automation(**overrides):
    value = {
        "downloads_active": 0,
        "downloads_review": 0,
        "rss_subscriptions": 0,
        "rss_pending": 0,
        "rss_failed": 0,
        "organize_issues": 0,
        "strm_failures": 0,
        "strm_last_status": "",
        "strm_last_at": "",
    }
    value.update(overrides)
    return value


def _local_media(**task_overrides):
    tasks = {
        "waiting_stable": 0,
        "active": 0,
        "requires_manual": 0,
        "planned": 0,
        "failed": 0,
    }
    tasks.update(task_overrides)
    return {
        "sources": {"enabled_without_targets": 0},
        "tasks": tasks,
    }


def _persistent(*, verification=None, patrol=None):
    return {
        "download_verification": {
            "total": 0,
            "pending": 0,
            "running": 0,
            "retry_wait": 0,
            "visible": 0,
            "attention": 0,
            **(verification or {}),
        },
        "library_patrol": {
            "status": "not_created",
            "outcome": "",
            "attempts": 0,
            "checked_series_count": 0,
            "updates_available_count": 0,
            "missing_episode_count": 0,
            "inconclusive_count": 0,
            "unmapped_series_count": 0,
            "findings_truncated": False,
            **(patrol or {}),
        },
    }


class WorkspaceTodoUnitTests(IsolatedDatabaseTestCase):
    def test_arguments_and_registry_contract_are_strict(self):
        self.assertEqual(workspace_todo_arguments({}), {})
        with self.assertRaisesRegex(AgentToolError, r"^workspace\.todo 不接受参数$"):
            workspace_todo_arguments({"token": "PRIVATE"})

        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        spec = capabilities["workspace.todo"]
        self.assertEqual(spec["risk"], "read")
        self.assertFalse(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])
        self.assertEqual(spec["parameters"]["properties"], {})

    def test_persistent_health_database_summary_reads_only_anonymous_fields(self):
        def cleanup():
            with db.get_conn() as conn:
                conn.execute("DELETE FROM agent_download_verifications")
                conn.execute("DELETE FROM agent_library_patrol_notification_outbox")
                conn.execute("DELETE FROM agent_library_patrol")
                conn.execute("DELETE FROM download_requests")

        self.addCleanup(cleanup)
        request_id, _ = db.create_download_request(
            "workspace-health-secret",
            "magnet",
            title="PRIVATE-TITLE",
            origin="agent",
        )
        db.update_download_request(
            request_id,
            targets="qb",
            status="submitted",
            qb_status="submitted",
        )
        self.assertTrue(db.enqueue_agent_download_verification(
            request_id,
            title="https://private.example/path?token=PRIVATE",
            tmdb_id="12345",
            season=1,
            episode=2,
            as_of="2026-08-03",
        ))
        db.ensure_agent_library_patrol(next_run_at="2026-08-03 20:00:00")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_download_verifications SET status='attention' "
                "WHERE request_id=?",
                (request_id,),
            )
            conn.execute(
                "UPDATE agent_library_patrol SET status='retry_wait',"
                "outcome='updates_available',checked_series_count=5,"
                "updates_available_count=2,missing_episode_count=3,"
                "inconclusive_count=1,unmapped_series_count=4,"
                "findings_truncated=1,projection_json=?,error_type=? "
                "WHERE patrol_key='default'",
                (
                    '{"findings":[{"title":"PRIVATE-PATROL"}]}',
                    "PRIVATE-ERROR",
                ),
            )

        summary = db.get_agent_persistent_health_summary()

        self.assertEqual(summary["download_verification"], {
            "total": 1,
            "pending": 0,
            "running": 0,
            "retry_wait": 0,
            "visible": 0,
            "attention": 1,
        })
        self.assertEqual(summary["library_patrol"], {
            "status": "retry_wait",
            "outcome": "updates_available",
            "checked_series_count": 5,
            "updates_available_count": 2,
            "missing_episode_count": 3,
            "inconclusive_count": 1,
            "unmapped_series_count": 4,
            "findings_truncated": True,
        })
        serialized = json.dumps(summary, ensure_ascii=False)
        for secret in ("PRIVATE-TITLE", "private.example", "PRIVATE-PATROL", "PRIVATE-ERROR"):
            self.assertNotIn(secret, serialized)

    def test_maps_fixed_area_order_counts_reasons_and_next_tools(self):
        automation = _automation(
            downloads_active=2,
            downloads_review=3,
            rss_pending=5,
            rss_failed=7,
            organize_issues=11,
            strm_failures=13,
            secret_title="PRIVATE-TITLE",
            raw_error="https://private.example/token",
        )
        local = _local_media(
            waiting_stable=17,
            active=19,
            requires_manual=23,
            planned=29,
            failed=31,
        )
        local["sources"]["enabled_without_targets"] = 37
        local["private_path"] = "/private/media"
        persistent = _persistent(
            verification={
                "pending": 41,
                "running": 43,
                "retry_wait": 47,
                "visible": 53,
                "attention": 59,
                "title": "PRIVATE-VERIFICATION-TITLE",
                "request_id": "PRIVATE-REQUEST-ID",
            },
            patrol={
                "status": "retry_wait",
                "outcome": "updates_available",
                "checked_series_count": 61,
                "updates_available_count": 67,
                "missing_episode_count": 71,
                "inconclusive_count": 73,
                "unmapped_series_count": 79,
                "findings_truncated": True,
                "projection_json": "https://private.example/patrol?token=PRIVATE",
                "error_type": "PRIVATE-ERROR",
            },
        )

        with patch(
            "app.agent.workspace_todo_actions.db.get_dashboard_automation_summary",
            return_value=automation,
        ), patch(
            "app.agent.workspace_todo_actions.db.get_local_media_diagnostic_summary",
            return_value=local,
        ), patch(
            "app.agent.workspace_todo_actions.db.get_agent_persistent_health_summary",
            return_value=persistent,
        ):
            result = summarize_workspace_todo({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(
            result.data["attention_total"],
            3 + 7 + 11 + 13 + 23 + 31 + 37 + 59 + 67,
        )
        self.assertEqual(result.data["active_total"], 2 + 19 + 43)
        self.assertEqual(result.data["waiting_total"], 5 + 17 + 29 + 41 + 47 + 1)
        self.assertFalse(result.data["network_accessed"])
        self.assertFalse(result.data["filesystem_accessed"])
        self.assertEqual(result.data["unavailable_areas"], [])

        areas = result.data["areas"]
        self.assertEqual([item["source"] for item in areas], [
            "downloads", "rss", "organize", "strm", "local_media",
            "download_verification", "library_patrol",
        ])
        self.assertEqual(areas[0], {
            "source": "downloads",
            "status": "attention",
            "attention_count": 3,
            "active_count": 2,
            "waiting_count": 0,
            "reason_codes": ["download_needs_review"],
            "next_tool": "downloads.diagnose_queue",
        })
        self.assertEqual(areas[1]["reason_codes"], ["rss_failed", "rss_pending"])
        self.assertEqual(areas[2]["next_tool"], "guangya.organize.status")
        self.assertEqual(areas[3]["reason_codes"], ["strm_open_failure"])
        self.assertEqual(areas[4]["reason_codes"], [
            "local_media_requires_manual",
            "local_media_failed",
            "local_media_missing_target",
            "local_media_active",
            "local_media_waiting",
        ])
        self.assertEqual(areas[5]["status"], "attention")
        self.assertEqual(areas[5]["attention_count"], 59)
        self.assertEqual(areas[5]["active_count"], 43)
        self.assertEqual(areas[5]["waiting_count"], 41 + 47)
        self.assertEqual(areas[5]["visible_count"], 53)
        self.assertEqual(areas[6]["status"], "attention")
        self.assertEqual(areas[6]["attention_count"], 67)
        self.assertEqual(areas[6]["waiting_count"], 1)
        self.assertEqual(areas[6]["missing_episode_count"], 71)
        self.assertTrue(areas[6]["findings_truncated"])
        self.assertEqual(result.suggestions, [
            "检查下载队列里的异常",
            "检查 RSS 订阅为什么需要关注",
            "查看云盘整理任务状态",
            "检查 STRM 同步失败原因",
            "诊断本地媒体待处理项",
            "检查下载后入库复核状态",
            "查看缺集巡检需要关注的内容",
        ])
        self.assertNotIn("可调用", " ".join(result.suggestions))

        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "PRIVATE-TITLE", "private.example", "/private/media", "secret_title", "raw_error",
            "PRIVATE-VERIFICATION-TITLE", "PRIVATE-REQUEST-ID", "PRIVATE-ERROR",
        ):
            self.assertNotIn(secret, serialized)

    def test_empty_active_waiting_and_malformed_states_are_deterministic(self):
        scenarios = (
            (_automation(), _local_media(), "empty", 0, 0, 0),
            (_automation(downloads_active=2), _local_media(active=3), "active", 0, 5, 0),
            (_automation(rss_pending=4), _local_media(waiting_stable=5, planned=6), "waiting", 0, 0, 15),
            (
                _automation(downloads_review=-9, rss_failed="bad", organize_issues=None),
                {"sources": {"enabled_without_targets": -1}, "tasks": {"failed": "bad"}},
                "empty",
                0,
                0,
                0,
            ),
        )
        for automation, local, status, attention, active, waiting in scenarios:
            with self.subTest(status=status), patch(
                "app.agent.workspace_todo_actions.db.get_dashboard_automation_summary",
                return_value=automation,
            ), patch(
                "app.agent.workspace_todo_actions.db.get_local_media_diagnostic_summary",
                return_value=local,
            ):
                result = summarize_workspace_todo({})
            self.assertEqual(result.status, status)
            self.assertEqual(result.data["attention_total"], attention)
            self.assertEqual(result.data["active_total"], active)
            self.assertEqual(result.data["waiting_total"], waiting)

    def test_strm_last_run_state_is_preserved_without_exposing_raw_status(self):
        scenarios = (
            ("failed", "attention", 1, 0, ["strm_last_run_failed"]),
            ("running", "active", 0, 1, ["strm_running"]),
            ("completed", "empty", 0, 0, []),
        )
        for last_status, overall, attention, active, reasons in scenarios:
            with self.subTest(last_status=last_status), patch(
                "app.agent.workspace_todo_actions.db.get_dashboard_automation_summary",
                return_value=_automation(strm_last_status=last_status),
            ), patch(
                "app.agent.workspace_todo_actions.db.get_local_media_diagnostic_summary",
                return_value=_local_media(),
            ):
                result = summarize_workspace_todo({})
            strm = result.data["areas"][3]
            self.assertEqual(result.status, overall)
            self.assertEqual(strm["attention_count"], attention)
            self.assertEqual(strm["active_count"], active)
            self.assertEqual(strm["reason_codes"], reasons)

    def test_strm_open_failures_preserve_concurrent_last_run_state(self):
        scenarios = (
            ("failed", 0, ["strm_open_failure", "strm_last_run_failed"]),
            ("running", 1, ["strm_open_failure", "strm_running"]),
        )
        for last_status, active, reasons in scenarios:
            with self.subTest(last_status=last_status), patch(
                "app.agent.workspace_todo_actions.db.get_dashboard_automation_summary",
                return_value=_automation(strm_failures=2, strm_last_status=last_status),
            ), patch(
                "app.agent.workspace_todo_actions.db.get_local_media_diagnostic_summary",
                return_value=_local_media(),
            ):
                result = summarize_workspace_todo({})
            strm = result.data["areas"][3]
            self.assertEqual(result.status, "attention")
            self.assertEqual(strm["attention_count"], 2)
            self.assertEqual(strm["active_count"], active)
            self.assertEqual(strm["reason_codes"], reasons)

    def test_partial_and_total_failures_never_expose_exception_text(self):
        with patch(
            "app.agent.workspace_todo_actions.db.get_dashboard_automation_summary",
            side_effect=RuntimeError("token=PRIVATE-A /secret/a"),
        ), patch(
            "app.agent.workspace_todo_actions.db.get_local_media_diagnostic_summary",
            return_value=_local_media(failed=2),
        ):
            partial = summarize_workspace_todo({})

        self.assertTrue(partial.ok)
        self.assertEqual(partial.status, "partial")
        self.assertEqual(partial.data["unavailable_areas"], [
            "downloads", "rss", "organize", "strm",
        ])
        self.assertEqual(partial.data["attention_total"], 2)

        with patch(
            "app.agent.workspace_todo_actions.db.get_dashboard_automation_summary",
            side_effect=RuntimeError("PRIVATE-A"),
        ), patch(
            "app.agent.workspace_todo_actions.db.get_local_media_diagnostic_summary",
            side_effect=RuntimeError("PRIVATE-B"),
        ), patch(
            "app.agent.workspace_todo_actions.db.get_agent_persistent_health_summary",
            side_effect=RuntimeError("PRIVATE-C"),
        ):
            unavailable = summarize_workspace_todo({})

        self.assertFalse(unavailable.ok)
        self.assertEqual(unavailable.status, "unavailable")
        self.assertEqual(len(unavailable.data["unavailable_areas"]), 7)
        serialized = json.dumps({
            "partial": partial.to_dict(),
            "unavailable": unavailable.to_dict(),
        }, ensure_ascii=False)
        self.assertNotIn("PRIVATE-A", serialized)
        self.assertNotIn("PRIVATE-B", serialized)
        self.assertNotIn("PRIVATE-C", serialized)
        self.assertNotIn("/secret/a", serialized)

    def test_natural_language_uses_explicit_anchors_without_stealing_domain_routes(self):
        positives = (
            "统一待办",
            "查看统一待办",
            "工作区待办概览",
            "全局待办汇总",
            "待办概览",
            "待办中心有什么需要处理",
        )
        for message in positives:
            self.assertTrue(is_workspace_todo_message(message), message)
        for message in (
            "待办",
            "查看 RSS 待处理",
            "诊断下载任务",
            "检查本地媒体状态",
            "诊断自动化链路",
            "查看 STRM 失败记录",
            "全局搜索《沙丘2》",
            "统一待办，查看 STRM 失败记录",
            "统一待办有哪些能力",
        ):
            self.assertFalse(is_workspace_todo_message(message), message)

        registry = Mock()
        registry.execute.return_value = (ToolResult(True, "empty", "ok"), 1)
        agent = AgentOrchestrator(registry)
        for message in positives:
            registry.reset_mock()
            response = agent.query(message)
            self.assertEqual(response["tool_call"]["name"], "workspace.todo", message)
            registry.execute.assert_called_once_with(
                "workspace.todo", {}, context=ToolContext(owner="")
            )

        expected = (
            ("诊断下载任务", "downloads.diagnose_queue"),
            ("查看 RSS 待处理", "rss.diagnose"),
            ("检查本地媒体状态", "local_media.diagnose"),
            ("诊断自动化链路", "automation.diagnose_pipeline"),
            ("查看 STRM 失败记录", "strm.triage_failures"),
            ("全局搜索《沙丘2》", "workspace.search"),
            ("统一待办，查看 STRM 失败记录", "strm.triage_failures"),
            ("统一待办有哪些能力", "agent.capabilities"),
        )
        for message, tool_name in expected:
            registry.reset_mock()
            response = agent.query(message)
            self.assertEqual(response["tool_call"]["name"], tool_name, message)
        registry.reset_mock()
        response = agent.query("待办")
        self.assertEqual(response["mode"], "clarification")
        self.assertEqual(response["result"]["status"], "clarification_required")
        registry.execute.assert_not_called()


class WorkspaceTodoAPITests(IsolatedDatabaseTestCase):
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

    def test_auth_csrf_and_direct_then_query_shared_rate_limit(self):
        path = "/api/agent/tools/workspace.todo"
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json={"session_id": "test_session_identifier_0001", "arguments": {}}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}
        for _ in range(4):
            response = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}})
            self.assertEqual(response.status_code, 200, response.text)
        limited = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "查看统一待办"},
        )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_query_then_direct_share_limit_but_workspace_search_is_isolated(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        for message in ("统一待办", "工作区待办概览", "全局待办汇总", "待办中心有什么需要处理"):
            response = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": message},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('"name":"workspace.todo"', response.text)
        limited = self.client.post(
            "/api/agent/tools/workspace.todo",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {}},
        )
        self.assertEqual(limited.status_code, 429, limited.text)

        with patch("app.agent.workspace_actions.search_media_servers", return_value=[]):
            isolated = self.client.post(
                "/api/agent/tools/workspace.search",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"query": "沙丘2", "sections": ["library"]}},
            )
        self.assertEqual(isolated.status_code, 200, isolated.text)

    def test_invalid_arguments_fail_before_reading_aggregates(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        with patch(
            "app.agent.workspace_todo_actions.db.get_dashboard_automation_summary"
        ) as automation, patch(
            "app.agent.workspace_todo_actions.db.get_local_media_diagnostic_summary"
        ) as local_media:
            response = self.client.post(
                "/api/agent/tools/workspace.todo",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"token": "PRIVATE"}},
            )
        self.assertEqual(response.status_code, 400, response.text)
        automation.assert_not_called()
        local_media.assert_not_called()
