"""自动 RSS 下载的轮次预算不能把界面查询上限当成真实积压数量。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app import database as db
from app.modules.rss import RSSEngine
from tests.support import isolated_test_database
from tests.support import seed_rss_entry_state


class RSSBacklogSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.sid = db.add_rss_subscription("Backlog", "https://synthetic.invalid/feed")
        db.update_rss_subscription(self.sid, {"action": "download"})

    def seed(self, count, *, excluded=0):
        for index in range(count):
            title = (
                f"Episode {index}" if index < count - excluded else f"trailer {index}"
            )
            db.add_rss_entry_with_media(self.sid, title, f"backlog:{index}")

    def run_round(self):
        engine = RSSEngine()
        with (
            patch.object(
                engine, "refresh", return_value={"total": 0, "new": 0, "skipped": 0}
            ),
            patch.object(
                engine,
                "_download_entry",
                return_value={"ok": True, "method": "qBittorrent"},
            ) as submit,
        ):
            result = engine.auto_download(self.sid)
        self.assertEqual(submit.call_count, 100)
        self.assertEqual(result["downloaded"], 100)
        return result

    def test_deferred_count_includes_backlog_beyond_display_limit(self):
        self.seed(451)
        # 其它订阅和非 pending 条目不能计入此轮。
        other = db.add_rss_subscription("Other", "https://synthetic.invalid/other")
        db.add_rss_entry_with_media(other, "Other entry", "other:1")
        done = db.add_rss_entry_with_media(self.sid, "Already done", "done:1")["id"]
        seed_rss_entry_state(done, "downloaded")
        self.assertEqual(self.run_round()["deferred"], 351)

    def test_filtered_rows_are_subtracted_once_from_full_pending_snapshot(self):
        self.seed(451, excluded=20)
        db.update_rss_subscription(self.sid, {"exclude_keywords": "trailer"})
        result = self.run_round()
        self.assertEqual(result["filtered"], 20)
        self.assertEqual(result["deferred"], 331)
        self.assertEqual(
            len(db.list_rss_entries(sub_id=self.sid, status="skipped")), 20
        )

    def test_counted_projection_keeps_bounded_rows_and_default_api_shape(self):
        self.seed(451)
        page = db.list_rss_entries(
            sub_id=self.sid, status="pending", include_total=True
        )
        self.assertEqual(len(page), 300)
        self.assertEqual({row["total_count"] for row in page}, {451})
        ordinary = db.list_rss_entries(sub_id=self.sid, status="pending", limit=1)
        self.assertNotIn("total_count", ordinary[0].keys())
        self.assertEqual(
            db.list_rss_entries(sub_id=self.sid, status="failed", include_total=True),
            [],
        )
