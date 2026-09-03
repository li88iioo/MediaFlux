"""自动化链路 Agent 诊断的状态、脱敏与 API 契约。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.agent.automation_actions import (
    automation_pipeline_arguments,
    diagnose_automation_pipeline,
)
from app.agent.errors import AgentToolError
from tests.support import IsolatedDatabaseTestCase


def _aggregate(**overrides):
    value = {
        "downloads_active": 0,
        "downloads_review": 0,
        "rss_subscriptions": 0,
        "rss_pending": 0,
        "rss_failed": 0,
        "organize_issues": 0,
        "strm_failures": 0,
        "strm_last_status": "",
        "strm_last_at": "",
    }
    value.update(overrides)
    return value


class AutomationPipelineUnitTests(IsolatedDatabaseTestCase):
    def test_arguments_reject_every_extra_field(self):
        self.assertEqual(automation_pipeline_arguments({}), {})
        with self.assertRaisesRegex(
            AgentToolError, "^automation\\.diagnose_pipeline 不接受参数$"
        ):
            automation_pipeline_arguments({"token": "PIPELINE_SECRET"})

    def test_empty_local_database_is_explicitly_not_configured(self):
        result = diagnose_automation_pipeline({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "not_configured")
        self.assertEqual(result.data["probe_mode"], "local")
        self.assertFalse(result.data["network_accessed"])
        self.assertEqual(result.data["attention"], {"total": 0, "blockers": []})
        self.assertEqual(result.data["stages"]["rss"]["status"], "not_configured")
        self.assertEqual(result.data["stages"]["strm"]["last_run"], "not_observed")

    def test_active_work_without_issues_is_healthy(self):
        snapshot = _aggregate(
            downloads_active=3,
            rss_subscriptions=2,
            rss_pending=7,
            strm_last_status="completed",
        )
        with patch(
            "app.agent.automation_actions.db.get_dashboard_automation_summary",
            return_value=snapshot,
        ):
            result = diagnose_automation_pipeline({})
        self.assertEqual(result.status, "healthy")
        stages = result.data["stages"]
        self.assertEqual(
            stages["downloads"], {"status": "active", "active": 3, "needs_review": 0}
        )
        self.assertEqual(stages["rss"]["status"], "active")
        self.assertEqual(stages["guangya_organize"]["status"], "healthy")
        self.assertEqual(
            stages["strm"],
            {"status": "healthy", "open_failures": 0, "last_run": "completed"},
        )

    def test_attention_is_bounded_to_counts_and_fixed_machine_codes(self):
        snapshot = _aggregate(
            downloads_active=1,
            downloads_review=2,
            rss_subscriptions=1,
            rss_failed=3,
            organize_issues=4,
            strm_failures=5,
            strm_last_status="failed",
        )
        with patch(
            "app.agent.automation_actions.db.get_dashboard_automation_summary",
            return_value=snapshot,
        ):
            result = diagnose_automation_pipeline({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["attention"]["total"], 14)
        self.assertEqual(
            result.data["attention"]["blockers"],
            [
                {"code": "downloads_need_review", "stage": "downloads", "count": 2},
                {"code": "rss_failed_entries", "stage": "rss", "count": 3},
                {
                    "code": "organize_historical_issues",
                    "stage": "guangya_organize",
                    "count": 4,
                },
                {"code": "strm_open_failures", "stage": "strm", "count": 5},
            ],
        )
        self.assertEqual(result.data["stages"]["strm"]["last_run"], "failed")

    def test_failed_last_strm_run_without_open_failure_marks_pipeline_attention(self):
        snapshot = _aggregate(rss_subscriptions=1, strm_last_status="failed")
        with patch(
            "app.agent.automation_actions.db.get_dashboard_automation_summary",
            return_value=snapshot,
        ):
            result = diagnose_automation_pipeline({})
        self.assertEqual(result.status, "attention")
        self.assertEqual(
            result.data["attention"],
            {
                "total": 1,
                "blockers": [
                    {"code": "strm_last_run_failed", "stage": "strm", "count": 1}
                ],
            },
        )
        self.assertEqual(result.data["stages"]["strm"]["status"], "attention")

    def test_malformed_values_are_sanitized_and_sensitive_fields_are_not_projected(
        self,
    ):
        snapshot = _aggregate(
            downloads_active=-8,
            downloads_review="not-a-count",
            rss_subscriptions="2",
            rss_pending=True,
            strm_last_status="TOKEN_9f /srv/private 192.0.2.10",
            token="TOKEN_9f",
            path="/srv/private",
            url="http://192.0.2.10",
            host="private-host",
            hash="HASH_SECRET",
            guid="GUID_SECRET",
            id="ID_SECRET",
            title="TITLE_SECRET",
        )
        with patch(
            "app.agent.automation_actions.db.get_dashboard_automation_summary",
            return_value=snapshot,
        ):
            result = diagnose_automation_pipeline({})
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("TOKEN_9f", serialized)
        self.assertNotIn("/srv/private", serialized)
        self.assertNotIn("192.0.2.10", serialized)
        self.assertNotIn('"token"', serialized)
        self.assertNotIn('"path"', serialized)
        self.assertNotIn('"url"', serialized)
        for secret in (
            "private-host",
            "HASH_SECRET",
            "GUID_SECRET",
            "ID_SECRET",
            "TITLE_SECRET",
        ):
            self.assertNotIn(secret, serialized)
        self.assertEqual(result.data["stages"]["strm"]["last_run"], "unknown")

    def test_database_exception_returns_fixed_sanitized_error(self):
        with patch(
            "app.agent.automation_actions.db.get_dashboard_automation_summary",
            side_effect=RuntimeError("PIPELINE_SECRET /srv/hidden"),
        ):
            result = diagnose_automation_pipeline({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.data["network_accessed"])
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("PIPELINE_SECRET", serialized)
        self.assertNotIn("/srv/hidden", serialized)
