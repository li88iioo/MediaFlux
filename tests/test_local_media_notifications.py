"""本地媒体 Telegram 通知格式测试。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import database as db
from app.modules.local_media_notifications import build_local_media_event, notify_local_media_task
from app.notifier import render_event
from tests.support import IsolatedDatabaseTestCase


class LocalMediaNotificationTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_media_sources")
        self.source_id = db.create_local_media_source(
            name="qB 下载", qb_profile="", qb_path_prefix="", local_root="/downloads", owner="admin"
        )
        self.task_id = db.create_local_media_task(
            self.source_id, "", "/downloads/Movie/file.mkv", owner="admin", trigger="manual"
        )

    def test_completed_event_is_detailed_and_hides_absolute_paths(self):
        task = db.get_local_media_task(self.task_id, owner="admin")
        source = db.get_local_media_source(self.source_id, owner="admin")
        event = build_local_media_event(task, source, {
            "status": "completed",
            "moved": ["/media/Movies/Film/Film.mkv"],
            "deleted_junk": ["/downloads/Movie/ad.url"],
            "warnings": [],
            "media": [{
                "title": "封神第二部：战火西岐", "year": "2025", "tmdb_id": "1155281",
                "target_name": "封神第二部：战火西岐.2025.1080p.mkv",
            }],
        })
        rendered = render_event(event)
        self.assertIn("本地媒体整理完成", rendered)
        self.assertIn("封神第二部：战火西岐.2025.1080p.mkv", rendered)
        self.assertIn("1 个确认垃圾文件", rendered)
        self.assertNotIn("/media/", rendered)
        self.assertNotIn("/downloads/", rendered)


    def test_refresh_failure_is_not_reported_as_completed(self):
        task = db.get_local_media_task(self.task_id, owner="admin")
        source = db.get_local_media_source(self.source_id, owner="admin")
        rendered = render_event(build_local_media_event(task, source, {
            "status": "completed",
            "moved": ["/media/Movies/Film.mkv"],
            "deleted_junk": [],
            "warnings": ["Jellyfin 刷新失败: /media/Movies"],
            "media_refresh_status": "failed",
        }))
        self.assertIn("媒体库刷新：</b>失败（需处理）", rendered)
        self.assertNotIn("媒体库刷新：</b>完成", rendered)

    def test_unrelated_warning_does_not_change_successful_refresh_status(self):
        task = db.get_local_media_task(self.task_id, owner="admin")
        source = db.get_local_media_source(self.source_id, owner="admin")
        rendered = render_event(build_local_media_event(task, source, {
            "status": "completed",
            "moved": [],
            "deleted_junk": [],
            "warnings": ["qB 任务移除失败"],
            "media_refresh_status": "completed",
        }))
        self.assertIn("媒体库刷新：</b>完成", rendered)
        self.assertNotIn("完成（有警告）", rendered)

    def test_requires_manual_event_contains_reason_and_candidate(self):
        task = db.get_local_media_task(self.task_id, owner="admin")
        source = db.get_local_media_source(self.source_id, owner="admin")
        event = build_local_media_event(task, source, {
            "status": "requires_manual",
            "preview": {
                "reason": "剧集文件缺少集数",
                "candidate": {"title": "攻壳机动队", "year": "2026", "tmdb_id": "255358", "confidence": 0.82},
            },
        })
        rendered = render_event(event)
        self.assertIn("本地媒体待确认", rendered)
        self.assertIn("剧集文件缺少集数", rendered)
        self.assertIn("82%", rendered)

    def test_notify_uses_structured_sender_without_real_transport(self):
        result = {"status": "completed", "moved": [], "deleted_junk": [], "warnings": []}
        with patch("app.modules.local_media_notifications.send", return_value=True) as sender:
            self.assertTrue(notify_local_media_task(self.task_id, result, owner="admin"))
        sender.assert_called_once()

    def test_warning_and_failure_details_never_expose_absolute_paths(self):
        task = db.get_local_media_task(self.task_id, owner="admin")
        source = db.get_local_media_source(self.source_id, owner="admin")
        completed = render_event(build_local_media_event(task, source, {
            "status": "completed", "moved": [], "deleted_junk": [],
            "warnings": ["Jellyfin 刷新失败: /srv/media/Movies"],
        }))
        failed = render_event(build_local_media_event(
            task, source, {"status": "failed"}, error="移动失败: C:\\Media\\Movie.mkv"
        ))
        self.assertNotIn("/srv/media", completed)
        self.assertNotIn("C:\\Media", failed)
        self.assertIn("1 条警告", completed)
        self.assertIn("任务详情", failed)


if __name__ == "__main__":
    unittest.main()
