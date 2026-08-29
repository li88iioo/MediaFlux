"""本地媒体 Telegram 通知格式测试。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import database as db
from app.modules.local_media_notifications import build_local_media_event, notify_local_media_task
from app.modules.telegram_notification_center import NotificationPublishResult
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
        db.update_local_media_task(
            self.task_id, owner="admin", status="requires_manual", error="剧集文件缺少集数"
        )
        task = db.get_local_media_task(self.task_id, owner="admin")
        source = db.get_local_media_source(self.source_id, owner="admin")
        event = build_local_media_event(task, source, {
            "status": "requires_manual",
            "preview": {
                "reason": "剧集文件缺少集数",
                "snapshot_digest": "digest-1",
                "rules_snapshot": "{}",
                "files": [{"name": "file.mkv"}],
                "candidate": {
                    "title": "攻壳机动队", "year": "2026", "tmdb_id": "255358",
                    "media_type": "tv", "confidence": 0.82, "provider": "tmdb",
                },
            },
        })
        rendered = render_event(event)
        self.assertIn("本地媒体待确认", rendered)
        self.assertIn("剧集文件缺少集数", rendered)
        self.assertIn("82%", rendered)
        self.assertEqual(event.actions, ())
        self.assertIn("Web", str(event.footer))

    def test_actionable_manual_event_reuses_telegram_candidate_buttons(self):
        db.update_local_media_task(
            self.task_id, owner="admin", status="requires_manual", error="匹配置信度不足"
        )
        task = db.get_local_media_task(self.task_id, owner="admin")
        source = db.get_local_media_source(self.source_id, owner="admin")
        event = build_local_media_event(task, source, {
            "status": "requires_manual",
            "preview": {
                "reason": "匹配置信度不足",
                "snapshot_digest": "digest-1",
                "rules_snapshot": "{}",
                "files": [{"name": "file.mkv"}],
                "candidate": {
                    "title": "封神第二部", "year": "2025", "tmdb_id": "1155281",
                    "media_type": "movie", "confidence": 0.82, "provider": "tmdb",
                },
            },
        })

        self.assertEqual(len(event.actions), 2)
        self.assertRegex(event.actions[0].callback_data, r"^orgc:[A-Za-z0-9_-]+:0$")
        self.assertEqual(str(event.actions[-1].label), "暂不处理")
        self.assertIn("请选择下方候选", str(event.footer))

    def test_confirmation_button_failure_falls_back_to_web_without_changing_task(self):
        db.update_local_media_task(
            self.task_id, owner="admin", status="requires_manual", error="匹配置信度不足"
        )
        task = db.get_local_media_task(self.task_id, owner="admin")
        source = db.get_local_media_source(self.source_id, owner="admin")
        with patch(
            "app.modules.organize_confirmations.create_local_media_confirmation_actions",
            side_effect=RuntimeError("database busy"),
        ):
            event = build_local_media_event(task, source, {
                "status": "requires_manual",
                "preview": {"reason": "匹配置信度不足"},
            })

        self.assertEqual(event.actions, ())
        self.assertIn("Web", str(event.footer))
        self.assertEqual(
            db.get_local_media_task(self.task_id, owner="admin").status,
            "requires_manual",
        )

    def test_notify_uses_structured_sender_without_real_transport(self):
        result = {"status": "completed", "moved": [], "deleted_junk": [], "warnings": []}
        with patch(
            "app.modules.telegram_notification_center.publish_notification_thread",
            return_value=NotificationPublishResult(True, delivered=True, status="sent"),
        ) as publisher:
            self.assertTrue(notify_local_media_task(self.task_id, result, owner="admin"))
        publisher.assert_called_once()
        self.assertEqual(publisher.call_args.args[0], f"local-media:{self.task_id}")
        self.assertIn("本地媒体整理完成", publisher.call_args.args[1].title)

    def test_notify_binds_local_confirmation_buttons_to_explicit_chat(self):
        db.update_local_media_task(
            self.task_id, owner="admin", status="requires_manual", error="匹配置信度不足",
        )
        result = {
            "status": "requires_manual",
            "preview": {
                "reason": "匹配置信度不足",
                "snapshot_digest": "digest-1",
                "rules_snapshot": "{}",
                "files": [{"name": "file.mkv"}],
                "candidate": {
                    "title": "封神第二部", "year": "2025", "tmdb_id": "1155281",
                    "media_type": "movie", "confidence": 0.82, "provider": "tmdb",
                },
            },
        }
        with patch(
            "app.modules.organize_confirmations.publish_confirmation_event",
            return_value=True,
        ) as publisher:
            self.assertTrue(notify_local_media_task(
                self.task_id, result, owner="admin", chat_id="-100",
            ))

        event = publisher.call_args.args[0]
        self.assertTrue(event.actions)
        self.assertEqual(publisher.call_args.kwargs["chat_id"], "-100")

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
