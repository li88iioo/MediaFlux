"""失败源不能靠固定 ID 顺序永远占满 RSS 调度窗口。"""

from __future__ import annotations

from tests.support import assert_present

import unittest
from unittest.mock import patch

from app import database as db
from app.repositories import rss as rss_repo
from app.modules import rss_scheduler as module
from app.modules.rss import MikanParser, rss_subscription_refresh_revision
from tests.support import isolated_test_database


class HeldThread:
    instances: list[HeldThread] = []
    fail_start = False

    def __init__(self, *, target, args, name, daemon):
        self.target, self.args, self.name = target, args, name

    def start(self):
        if self.fail_start:
            raise RuntimeError("cannot start thread")
        self.instances.append(self)

    def is_alive(self):
        return True


class RSSSchedulerProjectionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        HeldThread.instances = []
        HeldThread.fail_start = False
        self.enterContext(patch.object(module.threading, "Thread", HeldThread))
        self.enterContext(patch.object(module.RSSScheduler, "_notify_issue"))

    def test_due_projection_runs_bound_refresh_and_auto_download(self):
        bindings: tuple[tuple[str, int, int], ...] = (
            ("", 1, 0), ("123", 1, 0), ("123", 0, 0), ("123", 1, 1),
        )
        for action in ("subscribe", "download"):
            for tmdb_id, season, skip_existing in bindings:
                with self.subTest(action=action, tmdb_id=tmdb_id, season=season, skip_existing=skip_existing):
                    sid = rss_repo.add_rss_subscription(
                        "scheduled", "https://example.invalid/feed",
                        action=action, refresh_interval_minutes=10,
                        media_tmdb_id=tmdb_id, media_default_season=season,
                        skip_existing_episodes=skip_existing,
                    )
                    due = rss_repo.list_due_rss_subscriptions()
                    self.assertEqual([row["id"] for row in due], [sid])
                    self.assertEqual(
                        rss_subscription_refresh_revision(due[0]),
                        rss_subscription_refresh_revision(rss_repo.get_rss_subscription(sid)),
                    )
                    scheduler = module.RSSScheduler()
                    with patch.object(MikanParser, "parse", return_value=[]) as parse:
                        self.assertEqual(scheduler.run_due(), 1)
                        worker = HeldThread.instances.pop()
                        with patch.object(module.threading, "current_thread", return_value=worker):
                            worker.target(*worker.args)
                    parse.assert_called_once_with("https://example.invalid/feed")
                    self.assertTrue(assert_present(rss_repo.get_rss_subscription(sid))["last_refreshed_at"])
                    self.assertEqual(rss_repo.list_due_rss_subscriptions(), [])

    def test_due_filters_and_order_survive_media_binding_join(self):
        ids = [
            rss_repo.add_rss_subscription(
                f"feed-{index}", "https://example.invalid/feed",
                media_tmdb_id="123", refresh_interval_minutes=10,
            )
            for index in range(7)
        ]
        for sid, fields in zip(ids, (
            {"enabled": 0}, {"refresh_interval_minutes": 0},
            {"last_refreshed_at": "2026-11-17 12:00:00"},
            {"last_refreshed_at": "2026-11-17 11:50:00"},
            {"last_refreshed_at": "invalid"}, {"last_refreshed_at": ""}, {},
        )):
            rss_repo.update_rss_subscription(sid, fields)
        self.assertEqual(
            [row["id"] for row in rss_repo.list_due_rss_subscriptions("2026-11-17 12:00:00")],
            ids[3:],
        )


class RSSSchedulerFairnessAuditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        HeldThread.instances = []
        HeldThread.fail_start = False
        self.enterContext(patch.object(module.threading, "Thread", HeldThread))
        self.enterContext(patch.object(module.RSSScheduler, "_notify_issue"))
        engine = self.enterContext(patch.object(module, "RSSEngine"))
        engine.return_value.refresh.return_value = {
            "error": "源不可用",
            "error_code": "all_sources_failed",
        }
        self.ids = [
            rss_repo.add_rss_subscription(
                f"feed-{index}",
                f"https://feed{index}.invalid/rss",
                refresh_interval_minutes=10,
            )
            for index in range(10)
        ]

    def drain(self):
        batch = list(HeldThread.instances)
        HeldThread.instances.clear()
        for worker in batch:
            with patch.object(module.threading, "current_thread", return_value=worker):
                worker.target(*worker.args)
        return [worker.args[0] for worker in batch]

    def test_permanently_failed_first_page_does_not_starve_later_subscriptions(self):
        scheduler = module.RSSScheduler()
        observed = []
        for _ in range(3):
            self.assertEqual(scheduler.run_due(), 4)
            observed += self.drain()
        self.assertEqual(set(observed), set(self.ids))
        self.assertEqual(observed[:10], self.ids)
        self.assertTrue(
            all(not assert_present(rss_repo.get_rss_subscription(i))["last_refreshed_at"] for i in self.ids)
        )

    def test_new_scheduler_resumes_after_persisted_last_admission(self):
        first = module.RSSScheduler()
        first.run_due()
        self.assertEqual(self.drain(), self.ids[:4])
        db.init_db()
        restarted = module.RSSScheduler()
        restarted.run_due()
        self.assertEqual(self.drain(), self.ids[4:8])

    def test_full_capacity_does_not_move_cursor_or_launch_duplicate_workers(self):
        scheduler = module.RSSScheduler()
        self.assertEqual(scheduler.run_due(), 4)
        self.assertEqual(scheduler.run_due(), 0)
        self.assertEqual(self.drain(), self.ids[:4])
        self.assertEqual(scheduler.run_due(), 4)
        self.assertEqual(self.drain(), self.ids[4:8])

    def test_thread_start_failure_leaves_admission_retryable(self):
        scheduler = module.RSSScheduler()
        HeldThread.fail_start = True
        with self.assertRaisesRegex(RuntimeError, "cannot start thread"):
            scheduler.run_due()
        self.assertFalse(scheduler._running_ids)
        self.assertFalse(scheduler._workers)
        HeldThread.fail_start = False
        scheduler.run_due()
        self.assertEqual(self.drain(), self.ids[:4])

    def test_deleted_cursor_row_and_invalid_historical_cursor_are_safe(self):
        scheduler = module.RSSScheduler()
        scheduler.run_due()
        self.drain()
        rss_repo.delete_rss_subscription(self.ids[3])
        scheduler.run_due()
        self.assertEqual(self.drain(), self.ids[4:8])
        for raw in ("not-an-integer", "-20", "999999999999999999"):
            with self.subTest(cursor=raw):
                db.kv_set("rss.scheduler.last_admitted_id", raw)
                scheduler = module.RSSScheduler()
                self.assertEqual(scheduler.run_due(), 4)
                self.assertEqual(self.drain(), [*self.ids[:3], self.ids[4]])
