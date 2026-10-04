"""真实 SQLite/ZIP 往返恢复后，订阅旧租约和刷新在途/新增路径保持单轨。"""

from __future__ import annotations

import unittest

from app import database as db
from app.modules.backup import create_backup, recover_pending_restore, restore_backup
from app.repositories import media_refresh_queue as queue
from app.runtime_paths import RuntimePaths
from tests.support import isolated_test_database


class HistoricalWorkflowRoundtripTests(unittest.TestCase):
    def test_backup_restore_preserves_requests_and_fences_stale_owners(self):
        with isolated_test_database("mediaflux.db") as database_path:
            root = database_path.parent
            paths = RuntimePaths(
                root / "program",
                root,
                root,
                root / "cache",
                root / "logs",
                root / "strm",
                root / "trash",
            )
            paths.ensure_writable_dirs()
            self.assertEqual(paths.database_path, database_path)
            paths.env_file.write_text("WEB_PORT=1258\n", encoding="utf-8")
            sid = db.add_media_subscription(
                provider="tmdb",
                external_id="1",
                tmdb_id="1",
                media_type="tv",
                title="Synthetic restored show",
            )
            old_run = db.claim_media_subscription_check_run(
                sid, trigger_type="scheduler"
            )
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE media_subscriptions SET updated_at=datetime('now','localtime','-2 hours') WHERE id=?",
                    (sid,),
                )
            group = queue.enqueue_media_refresh(
                "jellyfin", ["/media/Show/E01.mkv"], debounce_seconds=0, now_epoch=100
            )
            old_lease = queue.claim_due_media_refreshes(
                owner="old-worker", now_epoch=100
            )[0]
            queue.enqueue_media_refresh(
                "jellyfin", ["/media/Show/E02.mkv"], debounce_seconds=0, now_epoch=101
            )
            archive = create_backup(paths)

            # 模拟备份后停用/清空；恢复必须以归档快照为准，而不是当前连接缓存。
            db.delete_media_subscription(sid)
            queue.clear_media_refresh_queue()
            paths.env_file.write_text("WEB_PORT=9999\n", encoding="utf-8")
            restore_backup(paths, archive)
            db.configure_database(database_path, test_mode=True)
            db.init_db()
            self.assertEqual(
                paths.env_file.read_text(encoding="utf-8"), "WEB_PORT=1258\n"
            )
            self.assertFalse(recover_pending_restore(paths))
            self.assertEqual(
                db.get_media_subscription(sid)["title"], "Synthetic restored show"
            )
            self.assertTrue(db.get_media_subscription(sid)["enabled"])

            self.assertEqual(db.recover_stale_media_subscription_checks(), 1)
            self.assertEqual(db.recover_stale_media_subscription_checks(), 0)
            runs = db.list_media_subscription_runs(subscription_id=sid)
            self.assertEqual(
                [(row["id"], row["status"]) for row in runs], [(old_run, "failed")]
            )
            new_run = db.claim_media_subscription_check_run(sid, trigger_type="retry")
            self.assertNotEqual(new_run, old_run)
            self.assertFalse(db.media_subscription_check_is_active(sid, run_id=old_run))
            self.assertTrue(db.media_subscription_check_is_active(sid, run_id=new_run))

            self.assertEqual(queue.recover_media_refresh_leases(now_epoch=102), 1)
            self.assertEqual(queue.recover_media_refresh_leases(now_epoch=102), 0)
            new_lease = queue.claim_due_media_refreshes(
                owner="new-worker", now_epoch=102
            )[0]
            self.assertEqual(
                new_lease["paths"], ["/media/Show/E01.mkv", "/media/Show/E02.mkv"]
            )
            self.assertGreater(
                new_lease["lease_generation"], old_lease["lease_generation"]
            )
            self.assertFalse(
                queue.settle_media_refresh(
                    group["group_key"], outcome="completed",
                    owner="old-worker",
                    lease_generation=old_lease["lease_generation"],
                    now_epoch=103,
                )
            )
            self.assertTrue(
                queue.settle_media_refresh(
                    group["group_key"], outcome="completed",
                    owner="new-worker",
                    lease_generation=new_lease["lease_generation"],
                    refreshed_target_ids=("series-1",),
                    now_epoch=103,
                )
            )
            db.init_db()
            self.assertEqual(
                queue.media_refresh_queue_status(now_epoch=104)["paths"], 0
            )
            self.assertEqual(
                queue.recent_media_refresh_target_ids("jellyfin", now_epoch=104),
                ("series-1",),
            )
            self.assertEqual(
                len(db.list_media_subscription_runs(subscription_id=sid)), 2
            )
            self.assertTrue(db.media_subscription_check_is_active(sid, run_id=new_run))
