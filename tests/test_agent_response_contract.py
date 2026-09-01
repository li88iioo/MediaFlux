"""Agent 回合语义契约测试。"""
from __future__ import annotations

import unittest

from app.agent.response_contract import (
    attach_response_contract,
    build_response_contract,
    ensure_response_contract,
    infer_response_contract,
    response_contract,
)
from app.agent.tool_semantics import (
    default_result_presentation,
    default_stages_resource_candidates,
)


class AgentResponseContractTests(unittest.TestCase):
    def test_informational_turn_can_keep_resource_candidates_as_supporting_evidence(self):
        response = attach_response_contract(
            {"mode": "read_plan"},
            task_kind="informational",
            presentation="narrative",
            resource_candidates="supporting",
        )

        self.assertEqual(response_contract(response), {
            "task_kind": "informational",
            "presentation": "narrative",
            "resource_candidates": "supporting",
        })
        self.assertEqual(
            response_contract(response)["resource_candidates"], "supporting"
        )

    def test_resource_candidate_presentation_requires_primary_resource_task(self):
        with self.assertRaises(ValueError):
            build_response_contract(
                task_kind="informational",
                presentation="resource_candidates",
                resource_candidates="primary",
            )
        with self.assertRaises(ValueError):
            build_response_contract(
                task_kind="informational",
                presentation="confirmation",
            )

    def test_deterministic_tool_response_is_derived_once_at_response_boundary(self):
        response = ensure_response_contract({
            "mode": "read_only",
            "tool_call": {"name": "indexer.search_resources"},
        })

        self.assertEqual(response["response_contract"], {
            "task_kind": "resource_search",
            "presentation": "resource_candidates",
            "resource_candidates": "primary",
        })

    def test_candidate_evidence_and_primary_presentation_are_independent(self):
        self.assertEqual(
            default_result_presentation("indexer.search_resources"),
            "resource_candidates",
        )
        self.assertTrue(
            default_stages_resource_candidates("indexer.search_resources")
        )
        self.assertEqual(
            default_result_presentation("media.subscription_updates"),
            "narrative",
        )
        self.assertTrue(
            default_stages_resource_candidates("media.subscription_updates")
        )
        self.assertEqual(
            default_result_presentation("library.search_media"),
            "narrative",
        )
        self.assertFalse(
            default_stages_resource_candidates("library.search_media")
        )

    def test_action_plan_contract_is_inferred_without_channel_logic(self):
        contract = infer_response_contract({
            "mode": "confirmation_required",
            "action_plan": {
                "version": 1,
                "plan_id": "opaque-action-plan-123456",
                "status": "awaiting_approval",
                "risk": "write",
                "preflight_at": "2026-08-31T12:00:00+08:00",
            },
        })

        self.assertEqual(contract, {
            "task_kind": "action",
            "presentation": "confirmation",
            "resource_candidates": "none",
        })

    def test_valid_action_plan_overrides_conflicting_narrative_contract(self):
        contract = infer_response_contract({
            "mode": "confirmation_required",
            "action_plan": {
                "version": 1,
                "plan_id": "opaque-action-plan-123456",
                "status": "awaiting_approval",
                "risk": "write",
                "preflight_at": "2026-08-31T12:00:00+08:00",
            },
            "response_contract": {
                "task_kind": "informational",
                "presentation": "narrative",
                "resource_candidates": "none",
            },
        })

        self.assertEqual(contract, {
            "task_kind": "action",
            "presentation": "confirmation",
            "resource_candidates": "none",
        })

    def test_terminal_action_drops_stale_confirmation_contract(self):
        contract = infer_response_contract({
            "mode": "confirmed_action",
            "action_plan": {
                "version": 1,
                "plan_id": "opaque-action-plan-123456",
                "status": "completed",
                "risk": "write",
                "preflight_at": "2026-08-31T12:00:00+08:00",
            },
            "response_contract": {
                "task_kind": "action",
                "presentation": "confirmation",
                "resource_candidates": "none",
            },
        })

        self.assertEqual(contract, {
            "task_kind": "action",
            "presentation": "narrative",
            "resource_candidates": "none",
        })

    def test_confirmed_action_preserves_explicit_primary_resource_continuation(self):
        contract = infer_response_contract({
            "mode": "confirmed_action",
            "tool_call": {"name": "indexer.search_resources"},
            "response_contract": {
                "task_kind": "action",
                "presentation": "resource_candidates",
                "resource_candidates": "primary",
            },
        })

        self.assertEqual(contract, {
            "task_kind": "action",
            "presentation": "resource_candidates",
            "resource_candidates": "primary",
        })

    def test_confirmed_action_drops_implicit_resource_search_contract(self):
        contract = infer_response_contract({
            "mode": "confirmed_action",
            "tool_call": {"name": "indexer.search_resources"},
            "response_contract": {
                "task_kind": "resource_search",
                "presentation": "resource_candidates",
                "resource_candidates": "primary",
            },
        })

        self.assertEqual(contract, {
            "task_kind": "action",
            "presentation": "narrative",
            "resource_candidates": "none",
        })

    def test_action_mode_cannot_be_reinterpreted_as_resource_candidates(self):
        contract = infer_response_contract({
            "mode": "confirmation_required",
            "action_plan": {"plan_id": "malformed-plan-123456"},
            "tool_call": {"name": "indexer.search_resources"},
            "response_contract": {
                "task_kind": "resource_search",
                "presentation": "resource_candidates",
                "resource_candidates": "primary",
            },
        })

        self.assertEqual(contract, {
            "task_kind": "action",
            "presentation": "narrative",
            "resource_candidates": "none",
        })

    def test_confirmation_presentation_requires_a_valid_action_plan(self):
        missing = infer_response_contract({"mode": "confirmation_required"})
        malformed_explicit = infer_response_contract({
            "mode": "confirmation_required",
            "action_plan": {"plan_id": "short"},
            "response_contract": {
                "task_kind": "action",
                "presentation": "confirmation",
                "resource_candidates": "none",
            },
        })

        expected = {
            "task_kind": "action",
            "presentation": "narrative",
            "resource_candidates": "none",
        }
        self.assertEqual(missing, expected)
        self.assertEqual(malformed_explicit, expected)

    def test_unprojected_response_has_no_contract_decision(self):
        self.assertEqual(response_contract({"mode": "read_only"}), {})


if __name__ == "__main__":
    unittest.main()
