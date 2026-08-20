"""Media Agent 工作区下一步的安全投影、路由与 API 回归测试。"""
from __future__ import annotations

import json
import re
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.llm_router import LLMToolSelection
from app.agent.models import Evidence, RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, is_workspace_next_actions_message
from app.agent.rate_limit import agent_rate_limiter, tool_rate_limit_policy
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.agent.workspace_next_actions import (
    resolve_workspace_action_handoff,
    summarize_workspace_next_actions,
    workspace_action_handoff_arguments,
    workspace_next_actions_arguments,
)
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


_ACTION_KEYS = {
    "action_key", "source", "status", "attention_count", "reason_codes",
    "label", "why", "target_tool", "prompt", "risk",
    "requires_confirmation", "precondition", "staleness",
}


def _todo(*, ok=True, status="attention", areas=None, **data) -> ToolResult:
    return ToolResult(
        ok=ok,
        status=status,
        summary="PRIVATE CHILD SUMMARY",
        data={"areas": list(areas or []), **data},
        evidence=[Evidence("PRIVATE", "https://private.example/token", "2026-08-05")],
        suggestions=["PRIVATE CHILD SUGGESTION"],
        error="PRIVATE CHILD ERROR",
    )


def _area(source: str, reason: str, count: object = 1, **extra):
    return {
        "source": source,
        "status": "attention",
        "attention_count": count,
        "active_count": 0,
        "waiting_count": 0,
        "reason_codes": [reason, "PRIVATE_REASON"],
        "title": "PRIVATE-TITLE",
        "path": "/private/path",
        "url": "https://private.example/?token=SECRET",
        **extra,
    }


class WorkspaceNextActionsUnitTests(IsolatedDatabaseTestCase):
    def test_arguments_registry_and_rate_contract_are_strict(self):
        self.assertEqual(workspace_next_actions_arguments({}), {})
        with self.assertRaisesRegex(AgentToolError, r"^workspace\.next_actions 不接受参数$"):
            workspace_next_actions_arguments({"token": "PRIVATE"})

        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        spec = capabilities["workspace.next_actions"]
        self.assertEqual(spec["risk"], "read")
        self.assertFalse(spec["requires_confirmation"])
        self.assertEqual(spec["parameters"]["properties"], {})
        self.assertFalse(spec["parameters"]["additionalProperties"])
        self.assertEqual(
            tool_rate_limit_policy("workspace.next_actions"),
            ("workspace-next-actions", 4, 1),
        )

    def test_handoff_arguments_and_fresh_resolution_are_strict(self):
        self.assertEqual(
            workspace_action_handoff_arguments({"action_key": " review_rss "}),
            {"action_key": "review_rss"},
        )
        for invalid in (
            {},
            {"action_key": 1},
            {"action_key": "review_rss", "target_tool": "rss.retry_failed_to_qb"},
            {"action_key": "rss.retry_failed_to_qb"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                workspace_action_handoff_arguments(invalid)

        fresh = ToolResult(
            True,
            "attention",
            "有 1 个下一步",
            data={"actions": [{"action_key": "review_rss", "target_tool": "PRIVATE"}]},
        )
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_next_actions",
            return_value=fresh,
        ) as summarize:
            resolved = resolve_workspace_action_handoff({"action_key": "review_rss"})
        summarize.assert_called_once_with({})
        self.assertEqual(resolved, {
            "action_key": "review_rss",
            "label": "检查 RSS 订阅",
            "target_tool": "rss.diagnose",
            "arguments": {},
        })

    def test_every_handoff_mapping_targets_an_existing_read_tool(self):
        registry = build_tool_registry()
        expected = {
            "review_downloads": "downloads.diagnose_queue",
            "review_rss": "rss.diagnose",
            "review_organize": "guangya.organize.status",
            "review_strm": "strm.triage_failures",
            "review_local_media": "local_media.diagnose",
            "review_download_verification": "downloads.diagnose_queue",
            "review_library_patrol": "library.patrol_status",
        }
        for action_key, target_tool in expected.items():
            snapshot = ToolResult(
                True,
                "attention",
                "有下一步",
                data={"actions": [{"action_key": action_key}]},
            )
            with self.subTest(action_key=action_key), patch(
                "app.agent.workspace_next_actions.summarize_workspace_next_actions",
                return_value=snapshot,
            ):
                resolved = resolve_workspace_action_handoff({"action_key": action_key})
                self.assertEqual(resolved["target_tool"], target_tool)
                self.assertIs(registry.risk_for(target_tool), RiskLevel.READ)
                self.assertEqual(resolved["arguments"], {})

    def test_handoff_rejects_stale_or_unavailable_snapshot(self):
        stale = ToolResult(True, "empty", "没有下一步", data={"actions": []})
        unavailable = ToolResult(False, "unavailable", "不可用", data={"actions": []})
        for snapshot in (stale, unavailable):
            with self.subTest(status=snapshot.status), patch(
                "app.agent.workspace_next_actions.summarize_workspace_next_actions",
                return_value=snapshot,
            ), self.assertRaises(AgentToolError) as captured:
                resolve_workspace_action_handoff({"action_key": "review_downloads"})
            self.assertEqual(captured.exception.code, "precondition_failed")

    def test_projection_is_allowlisted_deduplicated_and_stably_ordered(self):
        areas = [
            _area("library_patrol", "library_patrol_updates_available", 7),
            _area("rss", "rss_failed", 3),
            _area("downloads", "download_needs_review", 2),
            _area("rss", "rss_failed", 99),
            _area("organize", "organize_issue", 4),
            _area("strm", "strm_open_failure", 5),
            _area("local_media", "local_media_failed", 6),
            _area("download_verification", "download_verification_attention", 8),
            _area("unknown", "unknown_reason", 9),
        ]
        child = _todo(
            areas=areas,
            attention_total=999,
            active_total=2,
            waiting_total=3,
            unavailable_areas=["unknown", "rss", "downloads", "rss"],
            token="PRIVATE-DATA",
        )
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_todo",
            return_value=child,
        ) as summarize:
            result = summarize_workspace_next_actions({})

        summarize.assert_called_once_with({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        actions = result.data["actions"]
        self.assertEqual(
            [item["source"] for item in actions],
            [
                "downloads", "rss", "organize", "strm", "local_media",
                "download_verification", "library_patrol",
            ],
        )
        self.assertEqual(actions[1]["attention_count"], 3)
        for action in actions:
            self.assertEqual(set(action), _ACTION_KEYS)
            self.assertEqual(action["risk"], "read")
            self.assertFalse(action["requires_confirmation"])
            self.assertEqual(action["status"], "attention")
            self.assertNotIn("PRIVATE_REASON", action["reason_codes"])
        self.assertEqual(result.data["unavailable_areas"], ["downloads", "rss"])
        self.assertFalse(result.data["network_accessed"])
        self.assertFalse(result.data["filesystem_accessed"])
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "PRIVATE-TITLE", "/private/path", "private.example", "SECRET",
            "PRIVATE-DATA", "PRIVATE CHILD", "PRIVATE_REASON",
        ):
            self.assertNotIn(secret, payload)

    def test_unknown_reason_non_attention_and_invalid_counts_do_not_create_actions(self):
        areas = [
            _area("downloads", "future_reason", 1),
            _area("rss", "rss_failed", 0),
            _area("organize", "organize_issue", -2),
            _area("strm", "strm_open_failure", "bad"),
            _area("local_media", "local_media_failed", 3, status="active"),
        ]
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_todo",
            return_value=_todo(status="empty", areas=areas),
        ):
            result = summarize_workspace_next_actions({})
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.data["actions"], [])

    def test_partial_and_unavailable_results_do_not_leak_child_errors(self):
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_todo",
            return_value=_todo(
                status="partial",
                areas=[_area("downloads", "download_needs_review")],
                unavailable_areas=["rss", "PRIVATE-SOURCE"],
            ),
        ):
            partial = summarize_workspace_next_actions({})
        self.assertTrue(partial.ok)
        self.assertEqual(partial.status, "partial")
        self.assertEqual(partial.data["unavailable_areas"], ["rss"])
        self.assertEqual(len(partial.data["actions"]), 1)

        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_todo",
            return_value=_todo(ok=False, status="unavailable", areas=[]),
        ):
            unavailable = summarize_workspace_next_actions({})
        self.assertFalse(unavailable.ok)
        self.assertEqual(unavailable.status, "unavailable")
        self.assertNotIn("PRIVATE", json.dumps(unavailable.to_dict(), ensure_ascii=False))

    def test_active_waiting_and_empty_have_no_action_cards(self):
        for status in ("active", "waiting", "empty"):
            with self.subTest(status=status), patch(
                "app.agent.workspace_next_actions.summarize_workspace_todo",
                return_value=_todo(status=status, areas=[]),
            ):
                result = summarize_workspace_next_actions({})
                self.assertTrue(result.ok)
                self.assertEqual(result.status, status)
                self.assertEqual(result.data["actions"], [])


class WorkspaceNextActionsRoutingTests(IsolatedDatabaseTestCase):
    @staticmethod
    def _agent() -> AgentOrchestrator:
        registry = ToolRegistry()
        names = (
            "workspace.next_actions", "workspace.briefing", "workspace.todo",
            "workspace.health", "downloads.diagnose_queue", "rss.diagnose",
            "config.diagnose", "workspace.search", "indexer.search_resources",
            "agent.capabilities",
        )
        for name in names:
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: ToolResult(
                    True, "success", tool, data=dict(arguments)
                ),
                validator=(
                    workspace_next_actions_arguments
                    if name == "workspace.next_actions"
                    else lambda arguments: dict(arguments)
                ),
                # 该测试验证统一模型路由的预算和严格参数校验，因此 fixture
                # 必须与生产注册表一样显式公开目标只读工具。
                llm_read=name == "workspace.next_actions",
            ))
        return AgentOrchestrator(registry)

    def test_handoff_invokes_only_fresh_read_target_and_charges_target_budget(self):
        agent_rate_limiter.reset()
        self.addCleanup(agent_rate_limiter.reset)
        agent = self._agent()
        resolution = {
            "action_key": "review_downloads",
            "label": "检查下载队列",
            "target_tool": "downloads.diagnose_queue",
            "arguments": {},
        }
        with patch(
            "app.agent.orchestrator.resolve_workspace_action_handoff",
            return_value=resolution,
        ) as resolve:
            for _ in range(4):
                response = agent.invoke_workspace_action(
                    "review_downloads", rate_identity="handoff-owner"
                )
                self.assertEqual(
                    response["tool_call"]["name"], "downloads.diagnose_queue"
                )
                self.assertEqual(response["tool_call"]["arguments"], {})
            with self.assertRaisesRegex(AgentToolError, "请求过于频繁"):
                agent.invoke_workspace_action(
                    "review_downloads", rate_identity="handoff-owner"
                )
        self.assertEqual(resolve.call_count, 5)

    def test_handoff_refuses_non_read_target(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="danger.write",
            description="danger",
            risk=RiskLevel.DANGER,
            parameters={},
            handler=lambda arguments: ToolResult(True, "success", "should-not-run"),
            validator=lambda arguments: dict(arguments),
            requires_confirmation=True,
            preview_handler=lambda arguments: ToolResult(True, "ready", "preview"),
        ))
        agent = AgentOrchestrator(registry)
        resolution = {
            "action_key": "review_downloads",
            "label": "检查下载队列",
            "target_tool": "danger.write",
            "arguments": {},
        }
        with patch(
            "app.agent.orchestrator.resolve_workspace_action_handoff",
            return_value=resolution,
        ), self.assertRaises(AgentToolError) as captured:
            agent.invoke_workspace_action("review_downloads", rate_identity="owner")
        self.assertEqual(captured.exception.code, "precondition_failed")

    def test_high_confidence_messages_route_to_next_actions(self):
        agent = self._agent()
        for message in (
            "工作区下一步做什么", "系统下一步", "全局下一步行动",
            "接下来该做什么", "接下来优先处理什么", "现在该处理什么",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_workspace_next_actions_message(message))
                self.assertEqual(
                    agent.query(message)["tool_call"]["name"],
                    "workspace.next_actions",
                )

    def test_specialized_write_health_and_briefing_messages_are_not_stolen(self):
        agent = self._agent()
        expected = (
            ("工作区下一步，诊断下载队列", "downloads.diagnose_queue"),
            ("系统下一步，查看 RSS 待处理", "rss.diagnose"),
            ("媒体系统健康总检，接下来该做什么", "workspace.health"),
            ("系统简报，接下来该做什么", "workspace.briefing"),
            ("工作区待办", "workspace.todo"),
        )
        for message, tool_name in expected:
            with self.subTest(message=message):
                self.assertFalse(is_workspace_next_actions_message(message))
                self.assertEqual(agent.query(message)["tool_call"]["name"], tool_name)
        for message in ("执行下一步", "下一步下载什么", "搜索下一步资源", "删除下一步"):
            with self.subTest(message=message):
                self.assertFalse(is_workspace_next_actions_message(message))
                response = agent.query(message)
                if response.get("tool_call"):
                    self.assertNotEqual(response["tool_call"]["name"], "workspace.next_actions")

    def test_llm_selection_uses_same_tool_budget_and_strict_validator(self):
        agent_rate_limiter.reset()
        self.addCleanup(agent_rate_limiter.reset)
        agent = self._agent()
        selection = LLMToolSelection("workspace.next_actions", {})
        with patch("app.agent.orchestrator.select_orchestration_tool", return_value=selection):
            for _ in range(4):
                response = agent.query(
                    "请帮我安排工作顺序",
                    llm_tool_rate_identity="owner-llm",
                )
                self.assertEqual(
                    response["tool_call"]["name"], "workspace.next_actions"
                )
        with self.assertRaisesRegex(AgentToolError, "请求过于频繁"):
            agent.query(
                "工作区下一步做什么",
                query_tool_rate_identity="owner-llm",
            )

        invalid = LLMToolSelection("workspace.next_actions", {"token": "PRIVATE"})
        with patch("app.agent.orchestrator.select_orchestration_tool", return_value=invalid):
            response = agent.query(
                "请重新安排工作顺序",
                llm_tool_rate_identity="another-owner",
            )
        self.assertIsNone(response["tool_call"])
        self.assertEqual(response["mode"], "clarification")
        self.assertEqual(response["result"]["status"], "clarification_required")

    def test_query_tool_rate_limit_is_enforced(self):
        agent_rate_limiter.reset()
        self.addCleanup(agent_rate_limiter.reset)
        agent = self._agent()
        for _ in range(4):
            self.assertEqual(
                agent.query(
                    "工作区下一步做什么",
                    query_tool_rate_identity="owner-a",
                )["tool_call"]["name"],
                "workspace.next_actions",
            )
        with self.assertRaisesRegex(AgentToolError, "请求过于频繁"):
            agent.query(
                "工作区下一步做什么",
                query_tool_rate_identity="owner-a",
            )


class WorkspaceNextActionsAPITests(IsolatedDatabaseTestCase):
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

    def test_auth_csrf_and_direct_then_query_share_tool_budget(self):
        path = "/api/agent/tools/workspace.next_actions"
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
            json={"session_id": "test_session_identifier_0001", "message": "工作区下一步做什么"},
        )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_query_then_direct_share_tool_budget(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        for message in (
            "工作区下一步", "系统下一步", "全局下一步",
            "接下来该做什么",
        ):
            response = self.client.post(
                "/api/agent/query", headers=headers, json={"session_id": "test_session_identifier_0001", "message": message}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('"name":"workspace.next_actions"', response.text)
        limited = self.client.post(
            "/api/agent/tools/workspace.next_actions",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {}},
        )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_invalid_arguments_fail_before_reading_todo_snapshot(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_todo"
        ) as summarize:
            response = self.client.post(
                "/api/agent/tools/workspace.next_actions",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"token": "PRIVATE"}},
            )
        self.assertEqual(response.status_code, 400, response.text)
        summarize.assert_not_called()

    def test_request_body_and_arguments_must_be_objects(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        path = "/api/agent/tools/workspace.next_actions"
        self.assertEqual(
            self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": [], "extra": 1}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": []}).status_code,
            400,
        )

    def test_workspace_action_endpoint_is_auth_csrf_and_schema_protected(self):
        path = "/api/agent/workspace-actions/invoke"
        self.assertEqual(
            self.client.post(path, json={"session_id": "test_session_identifier_0001", "action_key": "review_local_media"}).status_code,
            401,
        )
        csrf = self._login()
        self.assertEqual(
            self.client.post(path, json={"session_id": "test_session_identifier_0001", "action_key": "review_local_media"}).status_code,
            403,
        )
        headers = {"X-CSRF-Token": csrf}
        for body in (
            None,
            {},
            {"action_key": 1},
            {"action_key": "review_local_media", "target_tool": "local_media.diagnose"},
            {"action_key": "local_media.diagnose"},
        ):
            response = self.client.post(path, headers=headers, json=body)
            self.assertEqual(response.status_code, 400, response.text)

    def test_workspace_action_endpoint_revalidates_and_invokes_fixed_target(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        path = "/api/agent/workspace-actions/invoke"
        snapshot = ToolResult(
            True,
            "attention",
            "有 1 个下一步",
            data={"actions": [{"action_key": "review_local_media"}]},
        )
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_next_actions",
            return_value=snapshot,
        ) as summarize:
            response = self.client.post(
                path, headers=headers, json={"session_id": "test_session_identifier_0001", "action_key": "review_local_media"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["tool_call"]["name"], "local_media.diagnose")
        self.assertEqual(payload["tool_call"]["arguments"], {})
        summarize.assert_called_once_with({})

    def test_workspace_action_endpoint_rejects_stale_action_without_target_call(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_next_actions",
            return_value=ToolResult(True, "empty", "没有下一步", data={"actions": []}),
        ), patch(
            "app.agent.local_media_actions.diagnose_local_media"
        ) as diagnose:
            response = self.client.post(
                "/api/agent/workspace-actions/invoke",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "action_key": "review_local_media"},
            )
        self.assertEqual(response.status_code, 409, response.text)
        diagnose.assert_not_called()

    def test_workspace_action_endpoint_charges_actual_target_budget(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        snapshot = ToolResult(
            True,
            "attention",
            "有 1 个下一步",
            data={"actions": [{"action_key": "review_local_media"}]},
        )
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_next_actions",
            return_value=snapshot,
        ):
            for _ in range(4):
                response = self.client.post(
                    "/api/agent/workspace-actions/invoke",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "action_key": "review_local_media"},
                )
                self.assertEqual(response.status_code, 200, response.text)
            limited = self.client.post(
                "/api/agent/tools/local_media.diagnose",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {}},
            )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_workspace_action_endpoint_records_safe_session_turn(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        session_id = "workspace_action_session_001"
        snapshot = ToolResult(
            True,
            "attention",
            "有 1 个下一步",
            data={"actions": [{"action_key": "review_local_media"}]},
        )
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_next_actions",
            return_value=snapshot,
        ):
            response = self.client.post(
                "/api/agent/workspace-actions/invoke",
                headers=headers,
                json={
                    "action_key": "review_local_media",
                    "session_id": session_id,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        history = self.client.get(f"/api/agent/sessions/{session_id}")
        self.assertEqual(history.status_code, 200, history.text)
        messages = history.json()["session"]["messages"]
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(
            messages[0]["data"]["text"],
            "执行工作区行动 · 本地媒体诊断",
        )
        self.assertEqual(
            messages[1]["data"]["tool_label"],
            "本地媒体诊断",
        )
        self.assertNotIn("tool_name", messages[1]["data"])
