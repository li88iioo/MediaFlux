"""受控多步骤只读计划的安全边界与编排测试。"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from app.agent.llm_router import (
    LLMReadPlan,
    LLMToolSelection,
    _parse_read_plan,
    _request_read_plan,
    is_compound_read_request,
    read_plan_capabilities,
    select_read_plan,
)
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.indexers.http import IndexerHttpResponse


def _empty(arguments):
    if arguments:
        raise ValueError("no arguments")
    return {}


def _registry(*, calls=None, fail_download=False) -> ToolRegistry:
    calls = calls if calls is not None else []
    registry = ToolRegistry()

    def read_handler(name, *, fail=False):
        def handler(arguments):
            calls.append(name)
            if fail:
                return ToolResult(False, "unavailable", f"{name} unavailable", error="暂时不可用")
            return ToolResult(True, "ok", f"{name} ok", suggestions=[f"查看 {name}"])
        return handler

    for name, fail in (
        ("workspace.health", False),
        ("downloads.diagnose_queue", fail_download),
        ("strm.status", False),
    ):
        registry.register(ToolSpec(
            name=name,
            description=f"读取 {name}",
            risk=RiskLevel.READ,
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            validator=_empty,
            handler=read_handler(name, fail=fail),
            llm_read=True,
            llm_read_plan=True,
        ))
    registry.register(ToolSpec(
        name="config.set_feature_state",
        description="修改功能开关",
        risk=RiskLevel.WRITE,
        parameters={"type": "object", "properties": {}},
        validator=lambda arguments: dict(arguments),
        requires_confirmation=True,
        confirmation_preparer=lambda arguments: (
            ToolResult(True, "preview", "preview"),
            "feature-state",
        ),
        confirmed_handler=lambda arguments, _expected_context: ToolResult(
            True, "ok", "changed"
        ),
    ))
    return registry


class ReadPlanParsingTests(unittest.TestCase):
    def setUp(self):
        agent_rate_limiter.reset()

    def test_compound_request_detection_is_explicit_and_read_only(self):
        self.assertTrue(is_compound_read_request("请同时检查配置、下载队列和 STRM 状态"))
        self.assertTrue(is_compound_read_request("综合诊断 RSS、资源站和自动化"))
        self.assertFalse(is_compound_read_request("检查下载队列状态"))
        self.assertFalse(is_compound_read_request("检查配置并关闭媒体探索"))
        self.assertFalse(is_compound_read_request("同时开始下载并检查 STRM"))

    def test_parse_plan_requires_two_to_four_unique_allowlisted_steps(self):
        allowed = {"workspace.health", "downloads.diagnose_queue", "strm.status"}
        valid = {
            "steps": [
                {"tool_name": "workspace.health", "arguments_json": "{}"},
                {"tool_name": "downloads.diagnose_queue", "arguments_json": "{}"},
            ]
        }
        self.assertEqual(
            _parse_read_plan(valid, allowed),
            LLMReadPlan((
                LLMToolSelection("workspace.health", {}),
                LLMToolSelection("downloads.diagnose_queue", {}),
            )),
        )
        invalid = (
            {"steps": []},
            {"steps": valid["steps"][:1]},
            {"steps": valid["steps"] * 3},
            {"steps": [valid["steps"][0], valid["steps"][0]]},
            {"steps": [{"tool_name": "unknown", "arguments_json": "{}"}, valid["steps"][0]]},
            {"steps": [{"tool_name": "workspace.health", "arguments_json": "[]"}, valid["steps"][1]]},
            {"steps": valid["steps"], "extra": True},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertIsNone(_parse_read_plan(payload, allowed))

    def test_plan_capabilities_are_fixed_diagnostic_read_subset(self):
        names = {item["name"] for item in read_plan_capabilities(_registry())}
        self.assertEqual(names, {"workspace.health", "downloads.diagnose_queue", "strm.status"})
        self.assertNotIn("config.set_feature_state", names)

    def test_request_uses_strict_bounded_schema_and_pinned_host(self):
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured["client"] = kwargs

            async def post_json(self, url, *, json: dict, headers, max_redirects):
                captured.update(url=url, body=json, headers=headers, max_redirects=max_redirects)
                content = __import__("json").dumps({"steps": [
                    {"tool_name": "workspace.health", "arguments_json": "{}"},
                    {"tool_name": "downloads.diagnose_queue", "arguments_json": "{}"},
                ]})
                return IndexerHttpResponse(
                    url=url, status_code=200, headers={"content-type": "application/json"},
                    body=__import__("json").dumps({"choices": [{"message": {"content": content}}]}).encode(),
                )

            async def aclose(self):
                captured["closed"] = True

        values = {
            "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
            "AGENT_LLM_API_KEY": "secret-key",
            "AGENT_LLM_MODEL": "compatible-model",
            "AGENT_LLM_TIMEOUT_SECONDS": "8",
        }
        with patch("app.agent.llm_router.get", side_effect=lambda key, default="": values.get(key, default)):
            plan = asyncio.run(_request_read_plan(
                "请同时检查工作区和下载队列",
                read_plan_capabilities(_registry()),
                client_factory=FakeClient,
            ))
        self.assertEqual(len(plan.steps), 2)
        schema = captured["body"]["response_format"]["json_schema"]["schema"]
        self.assertTrue(captured["body"]["response_format"]["json_schema"]["strict"])
        self.assertEqual(schema["properties"]["steps"]["minItems"], 2)
        self.assertEqual(schema["properties"]["steps"]["maxItems"], 4)
        self.assertEqual(
            set(schema["properties"]["steps"]["items"]["properties"]["tool_name"]["enum"]),
            {"workspace.health", "downloads.diagnose_queue", "strm.status"},
        )
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret-key")
        self.assertEqual(captured["client"]["allowed_hosts"], {"ai.invalid"})
        self.assertTrue(captured["client"]["pin_resolved_address"])
        self.assertEqual(captured["max_redirects"], 0)
        self.assertTrue(captured["closed"])

    def test_selector_fails_closed_before_external_call(self):
        registry = _registry()
        values = {"AGENT_LLM_ENABLED": "1"}
        with patch("app.agent.llm_router.get", side_effect=lambda key, default="": values.get(key, default)), patch(
            "app.agent.llm_router._request_read_plan"
        ) as request:
            self.assertIsNone(select_read_plan("检查下载队列", registry, owner="a"))
            self.assertIsNone(select_read_plan("同时检查配置并关闭探索", registry, owner="a"))
            self.assertIsNone(select_read_plan("authorization=Bearer private-token，同时检查配置、下载", registry, owner="a"))
        request.assert_not_called()

    def test_selector_is_rate_limited_per_owner(self):
        values = {"AGENT_LLM_ENABLED": "1", "AGENT_LLM_REQUESTS_PER_MINUTE": "1"}

        async def planned(*args, **kwargs):
            return LLMReadPlan((
                LLMToolSelection("workspace.health", {}),
                LLMToolSelection("downloads.diagnose_queue", {}),
            ))

        with patch("app.agent.llm_router.get", side_effect=lambda key, default="": values.get(key, default)), patch(
            "app.agent.llm_router._request_read_plan", side_effect=planned
        ) as request:
            self.assertIsNotNone(select_read_plan("同时检查工作区、下载队列", _registry(), owner="a"))
            self.assertIsNone(select_read_plan("同时检查工作区、下载队列", _registry(), owner="a"))
            self.assertIsNotNone(select_read_plan("同时检查工作区、下载队列", _registry(), owner="b"))
        self.assertEqual(request.call_count, 2)


class ReadPlanExecutionTests(unittest.TestCase):
    def setUp(self):
        agent_rate_limiter.reset()

    @staticmethod
    def _plan():
        return LLMReadPlan((
            LLMToolSelection("workspace.health", {}),
            LLMToolSelection("downloads.diagnose_queue", {}),
        ))

    def test_registry_validates_without_executing(self):
        calls = []
        registry = _registry(calls=calls)
        self.assertEqual(registry.validate_read_call("workspace.health", {}), {})
        self.assertEqual(calls, [])
        with self.assertRaises(AgentToolError):
            registry.validate_read_call("config.set_feature_state", {})

    def test_query_executes_ordered_plan_and_aggregates_results(self):
        calls = []
        with patch("app.agent.orchestrator.select_read_plan", return_value=self._plan()), patch(
            "app.agent.orchestrator.select_read_tool"
        ) as single_selector:
            response = AgentOrchestrator(_registry(calls=calls)).query(
                "请同时检查工作区、下载队列状态",
                owner="web-session",
                llm_rate_owner="login-owner",
                query_tool_rate_identity="127.0.0.1",
            )
        self.assertEqual(response["mode"], "read_plan")
        self.assertEqual(response["tool_call"]["name"], "agent.read_plan")
        self.assertEqual(calls, ["workspace.health", "downloads.diagnose_queue"])
        data = response["result"]["data"]
        self.assertEqual((data["step_count"], data["completed"], data["failed"]), (2, 2, 0))
        self.assertEqual([step["position"] for step in data["steps"]], [1, 2])
        self.assertEqual([step["tool_name"] for step in data["steps"]], calls)
        single_selector.assert_not_called()

    def test_retry_replays_the_complete_plan_in_original_order(self):
        calls = []
        agent = AgentOrchestrator(_registry(calls=calls))
        first = agent._execute_read_plan(self._plan(), owner="web-session")
        retried = agent.query(
            "稍后继续完成未完成的检查", owner="web-session", present=False
        )

        self.assertEqual(first["mode"], "read_plan")
        self.assertEqual(retried["mode"], "read_plan")
        self.assertEqual(calls, [
            "workspace.health",
            "downloads.diagnose_queue",
            "workspace.health",
            "downloads.diagnose_queue",
        ])
        self.assertEqual(
            [step["tool_name"] for step in retried["result"]["data"]["steps"]],
            ["workspace.health", "downloads.diagnose_queue"],
        )

    def test_all_steps_are_prevalidated_before_first_handler(self):
        calls = []
        bad = LLMReadPlan((
            LLMToolSelection("workspace.health", {}),
            LLMToolSelection("downloads.diagnose_queue", {"unexpected": True}),
        ))
        with self.assertRaises(AgentToolError):
            AgentOrchestrator(_registry(calls=calls))._execute_read_plan(bad)
        self.assertEqual(calls, [])

    def test_non_read_step_is_rejected_before_execution(self):
        calls = []
        plan = LLMReadPlan((
            LLMToolSelection("workspace.health", {}),
            LLMToolSelection("config.set_feature_state", {}),
        ))
        with self.assertRaises(AgentToolError):
            AgentOrchestrator(_registry(calls=calls))._execute_read_plan(plan)
        self.assertEqual(calls, [])

    def test_actual_target_rate_limit_is_precharged(self):
        calls = []
        checks = []

        def allow(identity, tool_name):
            checks.append((identity, tool_name))
            return tool_name != "downloads.diagnose_queue"

        with patch("app.agent.orchestrator.allow_agent_tool", side_effect=allow):
            with self.assertRaises(AgentToolError) as raised:
                AgentOrchestrator(_registry(calls=calls))._execute_read_plan(
                    self._plan(), rate_identity="127.0.0.1"
                )
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(calls, [])
        self.assertEqual(checks, [
            ("127.0.0.1", "workspace.health"),
            ("127.0.0.1", "downloads.diagnose_queue"),
        ])

    def test_failed_step_produces_partial_result(self):
        response = AgentOrchestrator(_registry(fail_download=True))._execute_read_plan(self._plan())
        self.assertFalse(response["result"]["ok"])
        self.assertEqual(response["result"]["status"], "partial")
        self.assertEqual(response["result"]["data"]["failed"], 1)
        self.assertEqual(response["result"]["error"], "部分检查未能正常完成。")

    def test_invalid_compound_plan_falls_back_without_second_llm_call(self):
        with patch("app.agent.orchestrator.select_read_plan", return_value=None), patch(
            "app.agent.orchestrator.select_read_tool"
        ) as single_selector:
            response = AgentOrchestrator(_registry()).query(
                "请同时检查工作区、下载队列状态", owner="web-session"
            )
        self.assertEqual(response["tool_call"]["name"], "downloads.diagnose_queue")
        self.assertEqual(response["result"]["status"], "ok")
        single_selector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
