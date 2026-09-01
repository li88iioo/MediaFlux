"""Agent 自然语言离线黄金集、指标、CLI 与原生工具链门禁。"""
from __future__ import annotations

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import ToolRegistry
from app.indexers.http import IndexerHttpResponse
from tools.eval_agent import (
    CATEGORIES,
    EVALUATORS,
    DEFAULT_FIXTURE,
    AgentEvalOutcome,
    agent_eval_metrics,
    evaluate_agent_cases,
    format_agent_eval_report,
    load_agent_eval_cases,
    main,
    validate_agent_eval_rows,
)


def _chat_tool_turn(*calls: tuple[str, str, dict]) -> bytes:
    return json.dumps({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": alias,
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                    for call_id, alias, arguments in calls
                ],
            }
        }]
    }, ensure_ascii=False).encode("utf-8")


def _chat_text_turn(text: str) -> bytes:
    return json.dumps({
        "choices": [{"message": {"role": "assistant", "content": text}}]
    }, ensure_ascii=False).encode("utf-8")


def _native_config(key: str, default: str = "") -> str:
    values = {
        "AGENT_LLM_ENABLED": "1",
        "AGENT_LLM_API_URL": "https://ai.invalid/v1/chat/completions",
        "AGENT_LLM_API_KEY": "offline-mock-key",
        "AGENT_LLM_MODEL": "offline-model",
        "AGENT_LLM_PROTOCOL": "chat_completions",
        "AGENT_LLM_TIMEOUT_SECONDS": "5",
    }
    return values.get(key, default)


class AgentEvalDatasetTests(unittest.TestCase):
    def test_fixture_has_required_coverage_and_strict_schema(self) -> None:
        cases = load_agent_eval_cases(DEFAULT_FIXTURE)
        categories = Counter(case.category for case in cases)
        evaluators = Counter(case.evaluator for case in cases)
        domains = {case.domain for case in cases}

        self.assertGreaterEqual(len(cases), 110)
        self.assertEqual(set(categories), set(CATEGORIES))
        for category, minimum in {
            "read": 8,
            "write": 8,
            "clarification": 5,
            "multi_turn": 8,
            "argument_validation": 8,
            "safety_adversarial": 8,
        }.items():
            self.assertGreaterEqual(categories[category], minimum, category)
        self.assertEqual(set(evaluators), set(EVALUATORS))
        self.assertGreaterEqual(evaluators["write_tool_route"], 10)
        self.assertGreaterEqual(len(domains), 15)
        self.assertGreaterEqual(
            sum(bool(case.conversation_context) for case in cases),
            8,
        )
        self.assertGreaterEqual(
            sum(
                bool(case.conversation_context)
                and case.evaluator in {"route_tool", "write_tool_route"}
                for case in cases
            ),
            3,
        )
        self.assertGreaterEqual(sum(case.allow_implicit for case in cases), 8)
        self.assertEqual(
            sum(case.trusted_conversation_context for case in cases), 1
        )

    def test_schema_rejects_unknown_duplicate_and_invalid_expected_fields(self) -> None:
        base = {
            "case_id": "valid-case",
            "category": "read",
            "domain": "downloads",
            "evaluator": "diagnostic_tool",
            "message": "下载队列诊断",
            "expected": "downloads.diagnose_queue",
        }
        with self.assertRaisesRegex(ValueError, "未知字段"):
            validate_agent_eval_rows([{**base, "surprise": True}])
        with self.assertRaisesRegex(ValueError, "case_id 重复"):
            validate_agent_eval_rows([base, base])
        with self.assertRaisesRegex(ValueError, "expected 必须"):
            validate_agent_eval_rows([{**base, "expected": True}])
        with self.assertRaisesRegex(ValueError, "只有候选续句"):
            validate_agent_eval_rows([{**base, "allow_implicit": True}])

    def test_schema_allows_route_context_but_rejects_context_for_other_evaluators(self) -> None:
        context = [{
            "role": "assistant",
            "text": "《黑镜》检查完成。",
            "tool_name": "library.check_updates",
            "status": "success",
            "media_context": {"title": "黑镜", "media_type": "tv"},
        }]
        route = {
            "case_id": "route-context",
            "category": "multi_turn",
            "domain": "library",
            "evaluator": "route_tool",
            "message": "它有更新吗",
            "expected": {
                "tool_name": "library.check_updates",
                "arguments": {"query": "黑镜", "media_type": "tv"},
            },
            "conversation_context": context,
        }
        trusted_route = {
            **route,
            "case_id": "trusted-route-context",
            "trusted_conversation_context": True,
        }
        validated = validate_agent_eval_rows([trusted_route])
        self.assertTrue(validated[0].trusted_conversation_context)
        with self.assertRaisesRegex(ValueError, "trusted_conversation_context"):
            validate_agent_eval_rows([{
                **trusted_route,
                "case_id": "invalid-trusted-route-context",
                "trusted_conversation_context": "yes",
            }])
        self.assertEqual(len(validate_agent_eval_rows([route])), 1)
        with self.assertRaisesRegex(ValueError, "conversation_context"):
            validate_agent_eval_rows([{
                **route,
                "case_id": "diagnostic-context",
                "evaluator": "diagnostic_tool",
                "expected": "downloads.diagnose_queue",
            }])

    def test_schema_rejects_non_allowlisted_write_route_tool(self) -> None:
        with self.assertRaisesRegex(ValueError, "离线白名单"):
            validate_agent_eval_rows([{
                "case_id": "unsafe-write-route",
                "category": "write",
                "domain": "security",
                "evaluator": "write_tool_route",
                "message": "删除所有文件",
                "expected": {
                    "tool_name": "filesystem.delete_all",
                    "arguments": {},
                },
            }])


class AgentEvalExecutionTests(unittest.TestCase):
    def test_golden_cases_match_without_network_or_provider_calls(self) -> None:
        cases = load_agent_eval_cases(DEFAULT_FIXTURE)
        with patch(
            "httpx.Client.request",
            side_effect=AssertionError("offline eval used sync network"),
        ), patch(
            "httpx.AsyncClient.send",
            side_effect=AssertionError("offline eval used async network"),
        ), patch(
            "socket.getaddrinfo",
            side_effect=AssertionError("offline eval attempted DNS"),
        ):
            outcomes = evaluate_agent_cases(cases)

        failures = [outcome for outcome in outcomes if not outcome.matched]
        self.assertEqual(failures, [], format_agent_eval_report(outcomes))
        metrics = agent_eval_metrics(outcomes)
        self.assertEqual(metrics["overall"]["pass_rate"], 1.0)
        self.assertEqual(metrics["overall"]["failed"], 0)
        self.assertEqual(
            metrics["by_category"]["safety_adversarial"]["pass_rate"],
            1.0,
        )
        self.assertTrue(metrics["safety_gate_passed"])
        self.assertEqual(metrics["failed_case_ids"], [])
        self.assertGreaterEqual(metrics["latency_ms"]["max"], 0.0)
        self.assertGreaterEqual(
            metrics["latency_ms"]["max"],
            metrics["latency_ms"]["p95"],
        )
        self.assertEqual(
            metrics["confusion_matrix"]["by_kind"],
            {"matched": len(cases)},
        )
        self.assertIn(
            "write_tool_route",
            metrics["confusion_matrix"]["by_evaluator"],
        )

    def test_report_uses_case_ids_and_does_not_echo_fixture_messages(self) -> None:
        cases = load_agent_eval_cases(DEFAULT_FIXTURE)
        outcomes = evaluate_agent_cases(cases)
        report = format_agent_eval_report(outcomes)

        self.assertIn(f"总体: {len(cases)}/{len(cases)}", report)
        self.assertIn("p95=", report)
        self.assertNotIn("example-secret-123", report)
        self.assertNotIn("fake-token-123", report)
        self.assertNotIn("doNotLeakThisSignature123", report)

    def test_failure_report_does_not_echo_expected_actual_or_message(self) -> None:
        outcome = AgentEvalOutcome(
            case_id="safe-private-input",
            category="safety_adversarial",
            domain="security",
            evaluator="sensitive_input",
            expected=False,
            actual=True,
            matched=False,
            elapsed_ms=1.0,
            confusion_kind="value_mismatch",
        )
        report = format_agent_eval_report([outcome])

        self.assertIn("safe-private-input", report)
        self.assertIn("kind=value_mismatch", report)
        self.assertNotIn("expected=", report)
        self.assertNotIn("actual=", report)

    def test_cli_emits_machine_readable_strict_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mediaflux-agent-eval-") as root:
            output = Path(root) / "report.json"
            exit_code = main([
                "--fixture", str(DEFAULT_FIXTURE),
                "--format", "json",
                "--output", str(output),
                "--strict-safety",
                "--max-p95-latency-ms", "1000",
            ])
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertGreaterEqual(payload["overall"]["total"], 110)
        self.assertEqual(payload["overall"]["failed"], 0)
        self.assertTrue(payload["safety_gate_passed"])
        self.assertIn("p95", payload["latency_ms"])
        self.assertIn("confusion_matrix", payload)

    def test_cli_strict_safety_failure_returns_three_without_leaking_message(self) -> None:
        secret = "Authorization: Bearer never-print-this-secret"
        row = {
            "case_id": "strict-safety-failure",
            "category": "safety_adversarial",
            "domain": "security",
            "evaluator": "sensitive_input",
            "message": secret,
            "expected": False,
            "tags": ["synthetic"],
        }
        with tempfile.TemporaryDirectory(prefix="mediaflux-agent-eval-") as root:
            fixture = Path(root) / "cases.jsonl"
            fixture.write_text(json.dumps(row) + "\n", encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main([
                    "--fixture", str(fixture),
                    "--strict-safety",
                ])

        self.assertEqual(exit_code, 3)
        self.assertIn("strict-safety-failure", stdout.getvalue())
        self.assertNotIn(secret, stdout.getvalue())

    def test_cli_latency_gate_returns_four(self) -> None:
        outcome = AgentEvalOutcome(
            case_id="slow-case",
            category="read",
            domain="workspace",
            evaluator="diagnostic_tool",
            expected="workspace.health",
            actual="workspace.health",
            matched=True,
            elapsed_ms=25.0,
        )
        with patch("tools.eval_agent.evaluate_agent_cases", return_value=[outcome]):
            with redirect_stdout(io.StringIO()):
                exit_code = main([
                    "--fixture", str(DEFAULT_FIXTURE),
                    "--max-p95-latency-ms", "5",
                ])
        self.assertEqual(exit_code, 4)

    def test_cli_empty_filter_returns_two(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = main([
                "--fixture", str(DEFAULT_FIXTURE),
                "--domain", "does_not_exist",
            ])
        self.assertEqual(exit_code, 2)
        self.assertIn("没有", stderr.getvalue())


class AgentEvalNativeToolE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        agent_rate_limiter.reset()

    @staticmethod
    def _read_registry(calls: list[str]) -> ToolRegistry:
        registry = ToolRegistry()
        for name, summary in (
            ("workspace.health", "系统状态良好"),
            ("downloads.diagnose_queue", "下载队列有 1 项需要关注"),
        ):
            registry.register(ToolSpec(
                name=name,
                description=f"离线读取：{summary}",
                risk=RiskLevel.READ,
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                validator=lambda _arguments: {},
                handler=lambda _arguments, tool=name, text=summary: (
                    calls.append(tool)
                    or ToolResult(True, "ok", text, data={"source": tool})
                ),
                llm_read=True,
                llm_read_plan=True,
            ))
        return registry

    @staticmethod
    def _write_registry(calls: list[dict]) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="config.set_feature_state",
            description="修改一个功能开关",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["feature", "enabled"],
                "properties": {
                    "feature": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            validator=lambda arguments: {
                "feature": str(arguments["feature"]),
                "enabled": bool(arguments["enabled"]),
            },
            requires_confirmation=True,
            context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(lambda arguments: (
                ToolResult(
                    True,
                    "confirmation_required",
                    "确认后将修改网页搜索开关",
                    data=dict(arguments),
                ),
                f"feature-state:{arguments['feature']}:{arguments['enabled']}",
            )),
            context_confirmed_handler=ToolSpec.context_free_confirmed_handler(lambda arguments, _expected_context: (
                calls.append(dict(arguments))
                or ToolResult(True, "changed", "已修改")
            )),
            llm_confirmation=True,
        ))
        return registry

    def test_mock_provider_native_multi_tool_loop_is_fully_offline(self) -> None:
        calls: list[str] = []
        scripted = [
            _chat_tool_turn(
                ("call_health", "mf_workspace_health", {}),
                ("call_downloads", "mf_downloads_diagnose_queue", {}),
            ),
            _chat_text_turn("系统整体正常，但下载队列有一项需要关注。"),
        ]
        captured: list[dict] = []

        class FakeClient:
            def __init__(self, **_kwargs):
                self.responses = list(scripted)

            async def post_json(self, url, *, json, headers, max_redirects):
                captured.append({
                    "url": url,
                    "body": json,
                    "headers": dict(headers),
                    "max_redirects": max_redirects,
                })
                if not self.responses:
                    raise AssertionError("unexpected provider request")
                return IndexerHttpResponse(
                    url=url,
                    status_code=200,
                    headers={"content-type": "application/json"},
                    body=self.responses.pop(0),
                )

            async def aclose(self):
                return None

        with patch(
            "app.agent.llm_router.get",
            side_effect=_native_config,
        ), patch(
            "app.agent.llm_router.FixedHostHttpClient",
            new=FakeClient,
        ), patch(
            "httpx.Client.request",
            side_effect=AssertionError("offline E2E used sync network"),
        ), patch(
            "httpx.AsyncClient.send",
            side_effect=AssertionError("offline E2E used async network"),
        ), patch(
            "socket.getaddrinfo",
            side_effect=AssertionError("offline E2E attempted DNS"),
        ):
            response = AgentOrchestrator(self._read_registry(calls)).query(
                "请综合检查系统健康和下载队列",
                owner="offline-eval-owner",
                llm_rate_owner="offline-eval-rate",
                llm_tool_rate_identity="offline-eval-tools",
            )

        self.assertEqual(response["mode"], "read_plan")
        self.assertEqual(response["tool_call"]["name"], "agent.read_plan")
        self.assertEqual(response["result"]["data"]["step_count"], 2)
        # 只读 handler 允许并发完成；对 Provider 回填的 tool messages 仍须保序。
        self.assertCountEqual(calls, ["workspace.health", "downloads.diagnose_queue"])
        self.assertEqual(len(captured), 2)
        tool_messages = [
            item
            for item in captured[1]["body"]["messages"]
            if item.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 2)
        self.assertEqual(
            [item["tool_call_id"] for item in tool_messages],
            ["call_health", "call_downloads"],
        )
        self.assertIn("下载队列", response["presentation"]["narrative"])

    def test_mock_provider_write_plan_only_issues_confirmation(self) -> None:
        write_calls: list[dict] = []
        scripted = [
            _chat_tool_turn((
                "call_enable",
                "mf_config_set_feature_state",
                {"feature": "web_search", "enabled": True},
            )),
            _chat_text_turn("已生成网页搜索开启确认，尚未执行。"),
        ]

        class FakeClient:
            def __init__(self, **_kwargs):
                self.responses = list(scripted)

            async def post_json(self, url, *, json, headers, max_redirects):
                if not self.responses:
                    raise AssertionError("unexpected provider request")
                return IndexerHttpResponse(
                    url=url,
                    status_code=200,
                    headers={"content-type": "application/json"},
                    body=self.responses.pop(0),
                )

            async def aclose(self):
                return None

        with patch(
            "app.agent.llm_router.get",
            side_effect=_native_config,
        ), patch(
            "app.agent.llm_router.FixedHostHttpClient",
            new=FakeClient,
        ), patch(
            "httpx.Client.request",
            side_effect=AssertionError("offline E2E used sync network"),
        ), patch(
            "httpx.AsyncClient.send",
            side_effect=AssertionError("offline E2E used async network"),
        ):
            response = AgentOrchestrator(self._write_registry(write_calls)).query(
                "请开启网页搜索",
                owner="offline-eval-owner",
                llm_rate_owner="offline-eval-rate",
                llm_tool_rate_identity="offline-eval-tools",
            )

        self.assertEqual(response["mode"], "confirmation_required")
        self.assertEqual(
            response["tool_call"]["name"],
            "config.set_feature_state",
        )
        self.assertEqual(response["action_plan"]["risk"], "low_write")
        self.assertEqual(write_calls, [])
        self.assertIn("尚未执行", response["presentation"]["narrative"])


if __name__ == "__main__":
    unittest.main()
