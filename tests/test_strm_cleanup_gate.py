"""STRM 清理安全门（Sprint 3）需求驱动测试。

覆盖场景来自实施计划的验收条件：
- Task 3.1 结构化 CleanupDecision 与安全矩阵
- Task 3.2 退役来源清理不得绕过安全门
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.modules.strm import (
    CleanupDecision,
    apply_cleanup_decision,
    clean_retired_strm_sources,
    evaluate_cleanup_decision,
)


class CleanupDecisionMatrixTests(unittest.TestCase):
    """Task 3.1：任一安全条件失败即禁止删除未知旧对象。"""

    def test_complete_and_consistent_scan_allows_cleanup(self):
        decision = evaluate_cleanup_decision({})

        self.assertTrue(decision.cleanup_allowed)
        self.assertTrue(decision.scan_completed)
        self.assertEqual(decision.reasons, ())

    def test_matrix_of_unsafe_conditions_blocks_cleanup(self):
        cases = {
            "scan_partial": ({"scan_incomplete": True}, {}),
            "provider_error": ({}, {"scan_errors": 1}),
            "inconsistent_snapshot": ({}, {"consistency_errors": 1}),
            "cancelled_run": ({"stopped": True}, {}),
            "explicit_cancel": ({}, {"cancelled": True}),
        }
        for label, (stats, kwargs) in cases.items():
            with self.subTest(case=label):
                decision = evaluate_cleanup_decision(dict(stats), **kwargs)

                self.assertFalse(decision.cleanup_allowed)
                self.assertTrue(decision.reasons)

    def test_partial_scan_reports_the_recorded_limit_reason(self):
        decision = evaluate_cleanup_decision({
            "scan_incomplete": True, "scan_limit_reason": "entries",
        })

        self.assertIn("entries", decision.reasons)
        self.assertTrue(decision.scan_partial)
        self.assertFalse(decision.scan_completed)

    def test_decision_is_json_ready_and_recorded_in_stats(self):
        stats: dict = {}
        decision = evaluate_cleanup_decision({}, scan_errors=2)

        allowed = apply_cleanup_decision(stats, decision)

        self.assertFalse(allowed)
        self.assertTrue(stats["clean_skipped"])
        payload = stats["cleanup_decision"]
        self.assertEqual(set(payload), {
            "scan_completed", "scan_partial", "provider_error", "cancelled",
            "snapshot_consistent", "cleanup_allowed", "reasons",
        })
        self.assertFalse(payload["cleanup_allowed"])

    def test_allowed_decision_does_not_mark_clean_skipped(self):
        stats: dict = {}

        self.assertTrue(apply_cleanup_decision(stats, CleanupDecision()))
        self.assertNotIn("clean_skipped", stats)


class RetiredSourceCleanupGateTests(unittest.TestCase):
    """Task 3.2：活跃来源集不完整时禁止把来源判为退役。"""

    def test_incomplete_active_set_keeps_every_retired_source(self):
        with patch(
            "app.modules.strm.db.list_strm_retired_sources",
            return_value=[{"source_id": "gone"}],
        ) as listed, patch(
            "app.modules.strm.db.delete_strm_retired_source"
        ) as deleted:
            result = clean_retired_strm_sources(set(), active_ids_complete=False)

        listed.assert_called_once()
        deleted.assert_not_called()
        self.assertEqual(result["cleaned"], 0)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["sources"], 1)
        self.assertTrue(result["errors"])

    def test_incomplete_active_set_without_retired_rows_reports_nothing(self):
        with patch("app.modules.strm.db.list_strm_retired_sources", return_value=[]):
            result = clean_retired_strm_sources(set(), active_ids_complete=False)

        self.assertEqual(result["blocked"], 0)
        self.assertEqual(result["errors"], [])

    def test_complete_active_set_still_reaches_normal_cleanup(self):
        with patch(
            "app.modules.strm.db.list_strm_retired_sources", return_value=[]
        ), patch(
            "app.modules.strm.db.list_strm_index_by_prefix", return_value=[]
        ):
            result = clean_retired_strm_sources({"live"}, active_ids_complete=True)

        self.assertEqual(result["blocked"], 0)
        self.assertFalse(result["stopped"])


if __name__ == "__main__":
    unittest.main()
