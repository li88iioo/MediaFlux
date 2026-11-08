"""今日摘要的全量业务结果贯通 Agent 与原有主动通知队列。"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock, patch

from app import database as db
from app.modules.media_subscription_notifications import today_summary
from app.modules import media_automation_rules as automation
from app.repositories import media_automation_rules as rules
from tests.support import IsolatedDatabaseTestCase


class TodaySummaryDeliveryAuditTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        self.clock = datetime.now().astimezone()
        self.stamp = self.clock.strftime("%Y-%m-%d 12:00:00")
        with db.get_conn() as conn:
            for table in (
                "media_automation_rules",
                "media_subscription_runs",
                "local_media_tasks",
                "rss_entries",
                "download_log",
            ):
                conn.execute(f"DELETE FROM {table}")
            # 今日较早的失败不能被后续80条成功挤出异常摘要。
            conn.execute(
                "INSERT INTO download_log(source,title,status,created_at) VALUES('qb','Earlier failure','failed',?)",
                (self.stamp,),
            )
            conn.executemany(
                "INSERT INTO download_log(source,title,status,created_at) VALUES('qb',?,'success',?)",
                [(f"Later success {i}", self.stamp) for i in range(80)],
            )

    @staticmethod
    def _rule(errors_only=False):
        return {
            "id": "audit-rule",
            "revision": 1,
            "settings": {"hour": 21, "minute": 0, "errors_only": errors_only},
        }

    def test_agent_and_daily_notification_use_the_same_complete_counts(self) -> None:
        result = today_summary()
        self.assertEqual(result["event_count"], 81)
        self.assertEqual(result["event_count"], 81)
        self.assertEqual(result["downloads"], {"success": 80, "failed": 1})
        delivery = automation._daily_summary(self._rule(), self.clock)
        event = delivery.notification
        assert event is not None
        fields = dict(event.fields)
        assert isinstance(fields["下载"], str)
        self.assertIn("成功 80", fields["下载"])
        self.assertIn("失败 1", fields["下载"])
        self.assertIn("全部", str(event.footer))
        self.assertNotIn("每类最多 50", str(event.footer))
        self.assertLessEqual(len(result["content_titles"]), 8)

    def test_errors_only_includes_older_failure_without_unrelated_titles(self) -> None:
        delivery = automation._daily_summary(self._rule(True), self.clock)
        event = delivery.notification
        assert event is not None
        self.assertEqual(delivery.importance, "error")
        self.assertEqual(dict(event.fields)["下载"], "失败 1")
        self.assertNotIn("相关作品", dict(event.fields))
        self.assertEqual(
            automation._daily_summary(self._rule(True), self.clock).logical_key,
            delivery.logical_key,
        )

    def test_repeat_tick_hands_complete_summary_to_existing_center_only_once(
        self,
    ) -> None:
        rule = rules.save_rule(
            "audit-owner",
            kind="daily_summary",
            settings=self._rule(True)["settings"],
            enabled=True,
            next_run_at=self.clock.isoformat(),
        )
        with (
            patch.object(automation, "is_agent_enabled", return_value=True),
            patch.object(
                automation,
                "_authorized_notification_chat",
                return_value="synthetic-chat",
            ),
            patch.object(
                automation,
                "publish_notification_event",
                return_value=Mock(status="queued"),
            ) as publish,
        ):
            self.assertEqual(automation.drain_automation_rules(now=self.clock), 1)
            self.assertEqual(automation.drain_automation_rules(now=self.clock), 0)
        publish.assert_called_once()
        assert rule is not None
        self.assertIn(rule["id"], publish.call_args.args[0])
        self.assertEqual(dict(publish.call_args.args[1].fields)["下载"], "失败 1")
        self.assertFalse(publish.call_args.kwargs["deliver_now"])
