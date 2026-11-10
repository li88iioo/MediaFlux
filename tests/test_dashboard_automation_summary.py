"""看板自动化统计的展示语义。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.repositories import local_media as local_media_repo
from app.repositories import rss as rss_repo
from app.services import build_automation_summary
from tests.support import IsolatedDatabaseTestCase, isolated_test_database


class DashboardAutomationSummaryTests(IsolatedDatabaseTestCase):
    def test_subscription_count_is_not_pending_entry_count(self):
        enabled_id = rss_repo.add_rss_subscription(
            name="Mikan 动画", urls="https://example.invalid/rss", enabled=1
        )
        rss_repo.add_rss_subscription(
            name="停用订阅", urls="https://example.invalid/disabled", enabled=0
        )
        for index in range(3):
            rss_repo.add_rss_entry_with_media(enabled_id, f"第 {index + 1} 集", f"guid-{index}")

        summary = build_automation_summary()

        self.assertEqual(summary["rss_subscriptions"], 1)
        self.assertEqual(summary["rss_pending"], 3)

    def test_issue_source_identifies_single_and_mixed_failure_modules(self):
        cases = (
            ({}, 0, "none"),
            ({"downloads_review": 2}, 2, "downloads"),
            ({"rss_failed": 3}, 3, "rss"),
            ({"organize_issues": 4}, 4, "organize"),
            ({"strm_failures": 5}, 5, "strm"),
            ({"downloads_review": 2, "strm_failures": 5}, 7, "mixed"),
        )

        for database_summary, expected_total, expected_source in cases:
            with self.subTest(expected_source=expected_source):
                with patch(
                    "app.services.db.get_dashboard_automation_summary",
                    return_value=database_summary,
                ):
                    summary = build_automation_summary()

                self.assertEqual(summary["issues"], expected_total)
                self.assertEqual(summary["issue_source"], expected_source)
                self.assertEqual(summary["healthy"], expected_total == 0)


    def test_organize_issues_keep_the_historical_guangya_failure_scope(self):
        with isolated_test_database() as database_path:
            # 生产本地状态形状是 24 完成、1 失败、1 待确认，不应冒充光鸭的 23 项。
            root = str(database_path.parent)
            source_id = local_media_repo.create_local_media_source("本地来源", "default", root, root)
            for index, status in enumerate(["completed"] * 24 + ["failed", "requires_manual"]):
                task_id = local_media_repo.create_local_media_task(source_id, "", f"{root}/task-{index}")
                local_media_repo.update_local_media_task(task_id, status=status)
            summary = build_automation_summary()
            self.assertEqual(summary["organize_issues"], 0)
            self.assertEqual(summary["issues"], 0)
            self.assertEqual(summary["issue_source"], "none")

            failures = ("failed", "interrupted", "partial_failed", "revert_failed")
            for index in range(23):
                db.add_organize_log("光鸭", "/incoming", "/library", f"cloud-{index}", failures[index % 4])
            for status in ("success", "skipped", "reverted", "deleted", "confirmed", "manual"):
                db.add_organize_log("光鸭", "/incoming", "/library", status, status)
            summary = build_automation_summary()
            visible_logs = db.list_organize_timeline(origin="guangya", status="issues", limit=100)
            self.assertEqual(summary["organize_issues"], sum(row["raw_status"] in failures for row in visible_logs))
            self.assertEqual(summary["organize_issues"], 23)
            self.assertEqual(len(visible_logs), 23)
            self.assertEqual(summary["issues"], 23)
            self.assertEqual(summary["issue_source"], "organize")


class DashboardTemplateContractTests(IsolatedDatabaseTestCase):
    def test_progress_widths_avoid_jinja_inside_css_style_attributes(self):
        template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

        self.assertNotIn('style="width:{{', template)
        self.assertEqual(template.count('data-progress-width="{{'), 2)
        self.assertIn("function applyDashboardProgress(root=document)", template)
        self.assertIn("applyDashboardProgress(panel)", template)
        self.assertIn("Math.max(0,Math.min(100,raw))", template)

    def test_pending_indicator_routes_to_the_actual_failure_module(self):
        template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

        self.assertIn("automation.issue_source == 'downloads'", template)
        self.assertIn("automation.issue_source == 'rss'", template)
        self.assertIn("automation.issue_source == 'organize'", template)
        self.assertIn("url_for('pages.logs')", template)
        self.assertNotIn("url_for('pages.organize')", template)
        self.assertNotIn("整理待处理", template)
        self.assertIn("automation.issue_source == 'strm'", template)
        self.assertIn("automation.issue_source == 'mixed'", template)
        self.assertIn("url_for('pages.guangya_strm') ~ '#diagnostics'", template)
        self.assertIn("{% set issue_href = '#dashboardAutomationStatus' %}", template)
        self.assertIn('id="dashboardAutomationStatus{% if not loop.first %}-{{ loop.index }}{% endif %}"', template)
        self.assertIn("data-dashboard-issues-link", template)
        self.assertIn("activePanel?.querySelector('.dashboard-automation-status')", template)
        self.assertIn("{{ url_for('pages.guangya_strm') }}#diagnostics", template)
        self.assertNotIn('<span class="dashboard-quick-status-item', template)
