from __future__ import annotations

import unittest

from app.agent.progress_events import emit_agent_progress
from app.agent.response_contract import attach_response_contract
from app.agent.turn_runtime import (
    active_agent_turn,
    begin_agent_turn,
    record_agent_capabilities,
    reset_agent_turn,
)


class AgentTurnRuntimeTests(unittest.TestCase):
    def test_turn_tracks_intent_capabilities_observations_and_contract(self):
        token = begin_agent_turn(
            message="沧元图官方更新到多集啦？",
            owner="session-a",
            request_id="request-1",
        )
        try:
            record_agent_capabilities([
                "web.search",
                "library.check_updates",
                "indexer.search_resources",
            ])
            emit_agent_progress("tool_start", tool_name="web.search")
            emit_agent_progress("tool_finish", tool_name="web.search", ok=True)
            attach_response_contract(
                {},
                task_kind="informational",
                presentation="narrative",
                resource_candidates="supporting",
            )
            turn = active_agent_turn()
            self.assertIsNotNone(turn)
            snapshot = turn.snapshot()
        finally:
            reset_agent_turn(token)

        self.assertEqual(snapshot["request_id"], "request-1")
        self.assertTrue(snapshot["owner_bound"])
        self.assertIn("official_progress", snapshot["intent"]["domains"])
        self.assertEqual(snapshot["capability_names"], [
            "web.search",
            "library.check_updates",
            "indexer.search_resources",
        ])
        self.assertEqual(snapshot["observations"], [{
            "tool_name": "web.search",
            "state": "tool_finish",
            "ok": True,
        }])
        self.assertEqual(snapshot["response_contract"], {
            "task_kind": "informational",
            "presentation": "narrative",
            "resource_candidates": "supporting",
        })
        self.assertTrue(snapshot["completed"])
        self.assertEqual(snapshot["phase"], "completed")
        self.assertNotIn("message", snapshot)

    def test_turn_context_is_request_scoped(self):
        token = begin_agent_turn(message="检查下载队列", request_id="request-2")
        self.assertIsNotNone(active_agent_turn())
        reset_agent_turn(token)
        self.assertIsNone(active_agent_turn())


if __name__ == "__main__":
    unittest.main()
