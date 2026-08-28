import unittest
from unittest.mock import patch

from app.agent.action_plan import (
    ACTION_PLAN_VERSION,
    action_plan_model_context,
    build_action_plan,
    sanitize_action_plan,
)
from app.agent.confirmation_contract import build_confirmation_contract
from app.agent.confirmation import ConfirmationStore
from app.agent.models import RiskLevel, ToolContext, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator
from app.agent.pending_action_actions import cancel_pending_action, pending_action_arguments
from app.agent.registry import AgentToolError, ToolRegistry


class AgentActionPlanTests(unittest.TestCase):
    def _contract(self):
        return build_confirmation_contract(
            tool_name="strm.run_once",
            risk=RiskLevel.DANGER,
            preview=ToolResult(True, "ready", "已完成 STRM 同步预检"),
            preflight_at="2026-08-28T12:00:00+08:00",
        )

    def test_builds_stable_execute_cancel_plan_without_tool_or_arguments(self):
        plan = build_action_plan(
            plan_id="plan-safe-token",
            confirmation_contract=self._contract(),
            expires_in=120,
        )
        self.assertEqual(plan["version"], ACTION_PLAN_VERSION)
        self.assertEqual(plan["plan_id"], "plan-safe-token")
        self.assertEqual(plan["status"], "awaiting_approval")
        self.assertEqual(plan["risk"], "danger")
        self.assertEqual(
            plan["decisions"],
            [{"id": "execute", "label": "执行"}, {"id": "cancel", "label": "取消"}],
        )
        self.assertNotIn("tool", plan)
        self.assertNotIn("arguments", plan)

    def test_sanitizer_rejects_invalid_or_tampered_plan(self):
        self.assertEqual(sanitize_action_plan({}), {})
        self.assertEqual(
            sanitize_action_plan({
                "version": ACTION_PLAN_VERSION,
                "plan_id": "x",
                "status": "awaiting_approval",
                "risk": "root",
            }),
            {},
        )

    def test_terminal_plan_does_not_advertise_replay_decisions(self):
        for status in ("completed", "failed", "cancelled", "expired"):
            plan = build_action_plan(
                plan_id=f"plan-terminal-{status}",
                confirmation_contract=self._contract(),
                expires_in=0,
                status=status,
            )
            self.assertEqual(plan["decisions"], [])
            self.assertEqual(sanitize_action_plan(plan)["decisions"], [])

    def test_sanitizer_handles_non_finite_numeric_fields(self):
        self.assertEqual(sanitize_action_plan({
            "version": float("inf"),
            "plan_id": "x",
            "status": "awaiting_approval",
            "risk": "write",
        }), {})
        plan = build_action_plan(
            plan_id="plan-safe-token",
            confirmation_contract=self._contract(),
            expires_in=float("inf"),
        )
        self.assertNotIn("expires_in", plan)

    def test_model_context_excludes_execution_token(self):
        plan = build_action_plan(
            plan_id="secret-plan-token",
            confirmation_contract=self._contract(),
            expires_in=60,
        )
        context = action_plan_model_context(plan)
        self.assertIn("尚未执行", context)
        self.assertIn("STRM", context)
        self.assertNotIn("secret-plan-token", context)
        self.assertNotIn("strm.run_once", context)

    @staticmethod
    def _service(*, tokens=None):
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="write.demo",
            description="测试受控写操作",
            risk=RiskLevel.DANGER,
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            validator=lambda _arguments: {},
            preview_handler=lambda _arguments: ToolResult(
                True, "confirmation_required", "预检通过：将执行测试操作"
            ),
            handler=lambda _arguments: ToolResult(
                True, "accepted", "测试操作已执行"
            ),
            requires_confirmation=True,
            llm_confirmation=True,
        ))
        token_iter = iter(tokens or (
            "plan-ticket-first-123456",
            "plan-ticket-second-12345",
        ))
        return AgentOrchestrator(
            registry,
            ConfirmationStore(token_factory=lambda: next(token_iter)),
        )

    def test_orchestrator_projects_same_plan_through_prepare_execute_and_cancel(self):
        service = self._service()

        prepared = service.prepare("write.demo", {}, owner="owner-a")
        plan = prepared["action_plan"]
        self.assertEqual(plan["plan_id"], prepared["confirmation"]["confirmation_id"])
        self.assertEqual(plan["status"], "awaiting_approval")
        self.assertEqual([item["label"] for item in plan["decisions"]], ["执行", "取消"])
        self.assertNotIn("write.demo", str(plan))

        completed = service.confirm(plan["plan_id"], owner="owner-a")
        self.assertEqual(completed["mode"], "confirmed_action")
        self.assertEqual(completed["action_plan"]["status"], "completed")
        self.assertEqual(completed["action_plan"]["plan_id"], plan["plan_id"])

        prepared_again = service.prepare("write.demo", {}, owner="owner-a")
        cancelled = service.resolve_confirmation_reply("取消", owner="owner-a")
        self.assertIsNotNone(cancelled)
        self.assertEqual(cancelled["mode"], "cancelled_action")
        self.assertEqual(cancelled["action_plan"]["status"], "cancelled")
        self.assertEqual(
            cancelled["action_plan"]["plan_id"],
            prepared_again["action_plan"]["plan_id"],
        )

    def test_failed_confirmed_action_has_failed_terminal_plan(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="write.failed",
            description="测试失败的受控写操作",
            risk=RiskLevel.DANGER,
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            validator=lambda _arguments: {},
            preview_handler=lambda _arguments: ToolResult(
                True, "confirmation_required", "预检通过"
            ),
            handler=lambda _arguments: ToolResult(
                False, "conflict", "测试操作未执行"
            ),
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry,
            ConfirmationStore(token_factory=lambda: "plan-failed-terminal-123456"),
        )
        prepared = service.prepare("write.failed", {}, owner="owner-a")

        failed = service.confirm(
            prepared["action_plan"]["plan_id"], owner="owner-a"
        )

        self.assertFalse(failed["result"]["ok"])
        self.assertEqual(failed["action_plan"]["status"], "failed")

    def test_pending_action_validator_rejects_non_object(self):
        with self.assertRaises(AgentToolError):
            pending_action_arguments([])

    def test_pending_plan_cancel_is_owner_bound_and_does_not_execute_handler(self):
        calls = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="write.demo",
            description="测试受控写操作",
            risk=RiskLevel.DANGER,
            parameters={"type": "object", "additionalProperties": False},
            validator=lambda _arguments: {},
            preview_handler=lambda _arguments: ToolResult(
                True, "confirmation_required", "预检通过"
            ),
            handler=lambda _arguments: calls.append("executed") or ToolResult(
                True, "accepted", "done"
            ),
            requires_confirmation=True,
            llm_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry,
            ConfirmationStore(token_factory=lambda: "plan-cancel-owner-123456"),
        )
        prepared = service.prepare("write.demo", {}, owner="owner-a")
        with patch("app.agent.service.get_agent_service", return_value=service):
            result = cancel_pending_action({}, ToolContext(owner="owner-a"))

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(calls, [])
        with self.assertRaises(AgentToolError):
            service.confirm(prepared["action_plan"]["plan_id"], owner="owner-a")

    def test_pending_plan_cancel_rejects_ambiguous_owner_state(self):
        service = self._service(tokens=(
            "plan-ambiguous-first-1234",
            "plan-ambiguous-second-123",
        ))
        service.confirmation_store.issue(
            owner="owner-a", tool_name="write.demo", arguments={}
        )
        service.confirmation_store.issue(
            owner="owner-a", tool_name="write.demo", arguments={}
        )
        with patch("app.agent.service.get_agent_service", return_value=service):
            with self.assertRaises(AgentToolError) as raised:
                cancel_pending_action({}, ToolContext(owner="owner-a"))
        self.assertEqual(raised.exception.code, "selection_required")


if __name__ == "__main__":
    unittest.main()
