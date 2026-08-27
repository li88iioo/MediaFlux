"""Agent 回合语义契约测试。"""
from __future__ import annotations

import unittest

from app.agent.response_contract import (
    attach_response_contract,
    build_response_contract,
    ensure_response_contract,
    infer_response_contract,
    resource_candidates_are_primary,
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
        self.assertFalse(resource_candidates_are_primary(response))

    def test_resource_candidate_presentation_requires_primary_resource_task(self):
        with self.assertRaises(ValueError):
            build_response_contract(
                task_kind="informational",
                presentation="resource_candidates",
                resource_candidates="primary",
            )

    def test_legacy_tool_response_is_inferred_once_at_response_boundary(self):
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

    def test_confirmation_contract_is_inferred_without_channel_logic(self):
        contract = infer_response_contract({
            "mode": "confirmation_required",
            "confirmation": {"confirmation_id": "opaque"},
        })

        self.assertEqual(contract, {
            "task_kind": "action",
            "presentation": "confirmation",
            "resource_candidates": "none",
        })

    def test_legacy_response_has_no_contract_decision(self):
        self.assertEqual(response_contract({"mode": "read_only"}), {})
        self.assertIsNone(resource_candidates_are_primary({"mode": "read_only"}))


if __name__ == "__main__":
    unittest.main()
