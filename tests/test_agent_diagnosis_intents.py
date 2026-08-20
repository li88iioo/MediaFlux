from __future__ import annotations

import unittest

from app.agent.intents import ReadIntentSpec, match_read_intent
from app.agent.orchestrator import _DIAGNOSTIC_READ_INTENTS


class DiagnosisIntentTableTests(unittest.TestCase):
    def test_table_order_is_part_of_the_routing_contract(self):
        self.assertEqual(
            [spec.tool_name for spec in _DIAGNOSTIC_READ_INTENTS],
            [
                "indexer.diagnose_readiness",
                "downloads.diagnose_queue",
                "rss.diagnose",
                "local_media.diagnose",
                "automation.diagnose_pipeline",
                "workspace.health",
            ],
        )

    def test_first_match_wins(self):
        specs = (
            ReadIntentSpec("specific", lambda _: True),
            ReadIntentSpec("broad", lambda _: True),
        )
        self.assertEqual(match_read_intent("状态", specs), "specific")

    def test_no_match_returns_none(self):
        specs = (ReadIntentSpec("unused", lambda _: False),)
        self.assertIsNone(match_read_intent("普通会话", specs))


if __name__ == "__main__":
    unittest.main()
