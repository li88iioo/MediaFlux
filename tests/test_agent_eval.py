"""Agent 自然语言离线黄金集、指标与 CLI 门禁。"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.eval_agent import (
    CATEGORIES,
    EVALUATORS,
    DEFAULT_FIXTURE,
    agent_eval_metrics,
    evaluate_agent_cases,
    format_agent_eval_report,
    load_agent_eval_cases,
    main,
    validate_agent_eval_rows,
)


class AgentEvalDatasetTests(unittest.TestCase):
    def test_fixture_has_required_coverage_and_strict_schema(self) -> None:
        cases = load_agent_eval_cases(DEFAULT_FIXTURE)
        categories = Counter(case.category for case in cases)
        evaluators = {case.evaluator for case in cases}
        domains = {case.domain for case in cases}

        self.assertGreaterEqual(len(cases), 50)
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
        self.assertEqual(evaluators, set(EVALUATORS))
        self.assertGreaterEqual(len(domains), 12)
        self.assertGreaterEqual(
            sum(bool(case.conversation_context) for case in cases), 5
        )
        self.assertGreaterEqual(sum(case.allow_implicit for case in cases), 5)

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


class AgentEvalExecutionTests(unittest.TestCase):
    def test_golden_cases_match_without_network_or_provider_calls(self) -> None:
        cases = load_agent_eval_cases(DEFAULT_FIXTURE)
        with patch("httpx.Client.request", side_effect=AssertionError("offline eval used network")):
            outcomes = evaluate_agent_cases(cases)

        failures = [outcome for outcome in outcomes if not outcome.matched]
        self.assertEqual(failures, [], format_agent_eval_report(outcomes))
        metrics = agent_eval_metrics(outcomes)
        self.assertEqual(metrics["overall"]["pass_rate"], 1.0)
        self.assertEqual(metrics["overall"]["failed"], 0)
        self.assertEqual(
            metrics["by_category"]["safety_adversarial"]["pass_rate"], 1.0
        )
        self.assertEqual(metrics["failed_case_ids"], [])

    def test_report_uses_case_ids_and_does_not_echo_fixture_messages(self) -> None:
        cases = load_agent_eval_cases(DEFAULT_FIXTURE)
        outcomes = evaluate_agent_cases(cases)
        report = format_agent_eval_report(outcomes)

        self.assertIn("总体: 68/68", report)
        self.assertNotIn("example-secret-123", report)
        self.assertNotIn("fake-token-123", report)

    def test_cli_can_emit_machine_readable_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mediaflux-agent-eval-") as root:
            output = Path(root) / "report.json"
            exit_code = main([
                "--fixture", str(DEFAULT_FIXTURE),
                "--format", "json",
                "--output", str(output),
            ])
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["overall"]["total"], 68)
        self.assertEqual(payload["overall"]["failed"], 0)


if __name__ == "__main__":
    unittest.main()
