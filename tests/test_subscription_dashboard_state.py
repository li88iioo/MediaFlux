"""看板订阅统计与媒体追更工作流状态的回归测试。

覆盖两个真实观察到的问题：
- 顶栏「订阅」只统计 RSS 源，漏掉媒体追更订阅
- 准入已完成但下载请求行仍停在 submitted 时，订阅永远显示「已推送」
"""
from __future__ import annotations

from app import database as db
from app.repositories.media_subscriptions import list_media_subscription_workflows
from tests.support import IsolatedDatabaseTestCase


class AutomationSubscriptionCountTests(IsolatedDatabaseTestCase):
    """看板「订阅」必须是全站订阅总量。"""

    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_items")
            conn.execute("DELETE FROM media_subscriptions")

    @staticmethod
    def _add_media_subscription(title: str, tmdb_id: str, *, enabled: int = 1) -> None:
        stamp = db.now()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO media_subscriptions(tmdb_id,media_type,title,enabled,"
                "created_at,updated_at) VALUES(?,'tv',?,?,?,?)",
                (tmdb_id, title, enabled, stamp, stamp),
            )

    def test_media_subscriptions_are_counted_in_the_total(self):
        self._add_media_subscription("师兄啊师兄", "218642")
        self._add_media_subscription("光阴之外", "281233")

        summary = db.get_dashboard_automation_summary()

        self.assertEqual(summary["rss_subscriptions"], 0)
        self.assertEqual(summary["media_subscriptions"], 2)
        self.assertEqual(summary["subscriptions_total"], 2)

    def test_disabled_media_subscriptions_are_excluded(self):
        self._add_media_subscription("已暂停", "1", enabled=0)

        summary = db.get_dashboard_automation_summary()

        self.assertEqual(summary["media_subscriptions"], 0)
        self.assertEqual(summary["subscriptions_total"], 0)

    def test_total_covers_both_kinds(self):
        self._add_media_subscription("师兄啊师兄", "218642")
        stamp = db.now()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO rss_items(name,urls,enabled,created_at,updated_at) VALUES(?,?,1,?,?)",
                ("动漫源", "https://example.invalid/rss", stamp, stamp),
            )

        summary = db.get_dashboard_automation_summary()

        self.assertEqual(summary["rss_subscriptions"], 1)
        self.assertEqual(summary["media_subscriptions"], 1)
        self.assertEqual(summary["subscriptions_total"], 2)


class SubscriptionWorkflowTerminalAdmissionTests(IsolatedDatabaseTestCase):
    """终态准入不得继续占用「进行中」工作流。"""

    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_download_admissions")
            conn.execute("DELETE FROM media_subscription_candidates")
            conn.execute("DELETE FROM download_requests")
            conn.execute("DELETE FROM media_subscriptions")
        stamp = db.now()
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO media_subscriptions(tmdb_id,media_type,title,enabled,"
                "status,missing_count,created_at,updated_at) "
                "VALUES('218642','tv','师兄啊师兄',1,'satisfied',0,?,?)",
                (stamp, stamp),
            )
            self.subscription_id = int(cur.lastrowid)

    def _admission(self, admission_status: str, request_status: str) -> None:
        stamp = db.now()
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO download_requests(request_key,kind,title,status,gy_status,created_at,updated_at) "
                "VALUES(?,'magnet','师兄啊师兄 S01E154',?,?,?,?)",
                (f"key-{admission_status}-{stamp}", request_status, request_status, stamp, stamp),
            )
            request_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO media_download_admissions(media_key,tmdb_id,media_type,"
                "season,episode,subscription_id,request_id,status,created_at,updated_at) "
                "VALUES('218642:1:154','218642','tv',1,154,?,?,?,?,?)",
                (self.subscription_id, request_id, admission_status, stamp, stamp),
            )

    def _workflow(self) -> dict:
        return list_media_subscription_workflows([self.subscription_id])[self.subscription_id]

    def test_completed_admission_with_stale_request_reports_no_active_work(self):
        self._admission("completed", "submitted")

        workflow = self._workflow()

        self.assertEqual(workflow["submitted_count"], 0)
        self.assertEqual(workflow["downloading_count"], 0)
        self.assertEqual(workflow["processing_count"], 0)

    def test_released_and_cancelled_admissions_are_terminal_too(self):
        for status in ("released", "cancelled"):
            with self.subTest(admission_status=status):
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM media_download_admissions")
                    conn.execute("DELETE FROM download_requests")
                self._admission(status, "submitted")

                self.assertEqual(self._workflow()["submitted_count"], 0)

    def test_in_flight_admission_is_still_reported(self):
        self._admission("submitted", "submitted")

        self.assertEqual(self._workflow()["submitted_count"], 1)

    def test_failed_admission_is_still_reported(self):
        self._admission("failed", "failed")

        self.assertEqual(self._workflow()["failed_count"], 1)

    def test_downloading_admission_is_still_reported(self):
        self._admission("downloading", "downloading")

        self.assertEqual(self._workflow()["downloading_count"], 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
