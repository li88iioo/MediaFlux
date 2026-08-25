"""本地媒体来源、目标和任务数据库契约。"""
from __future__ import annotations

import threading
import unittest

from app import database as db
from app.modules.local_media_models import LocalMediaSource, LocalMediaTask
from tests.support import IsolatedDatabaseTestCase


class LocalMediaDatabaseTests(IsolatedDatabaseTestCase):
    def test_source_target_and_task_round_trip(self):
        source_id = db.create_local_media_source(
            name="qB 下载目录 1",
            qb_profile="configured:qb",
            qb_path_prefix="/downloads/1",
            local_root="/mnt/downloads/1",
            enabled=1,
            stable_seconds=300,
            scan_enabled=1,
            scan_interval_minutes=10,
            owner="admin",
        )
        target_id = db.upsert_local_library_target(
            source_id, "movie", "/mnt/media/Movies",
            provider="jellyfin", library_name="电影", library_id="movies",
            server_path="//NAS/Media/Movies", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "hash-1", "/mnt/downloads/1/Movie.mkv", owner="admin"
        )

        source = db.get_local_media_source(source_id, owner="admin")
        task = db.get_local_media_task(task_id, owner="admin")
        self.assertIsInstance(source, LocalMediaSource)
        self.assertIsInstance(task, LocalMediaTask)
        self.assertEqual(source.mode, "move")
        self.assertEqual(task.status, "waiting_stable")
        target = db.list_local_library_targets(source_id, owner="admin")[0]
        self.assertEqual(target.id, target_id)
        self.assertEqual(target.library_id, "movies")
        self.assertEqual(target.library_name, "电影")
        self.assertEqual(target.server_path, "//NAS/Media/Movies")
        self.assertTrue(db.claim_local_media_task(task_id, expected="waiting_stable", owner="admin"))
        self.assertFalse(db.claim_local_media_task(task_id, expected="waiting_stable", owner="admin"))
        self.assertEqual(db.get_local_media_task(task_id, owner="admin").status, "recognizing")

    def test_init_db_purges_deprecated_smb_credentials_but_keeps_schema_columns(self):
        source_id = db.create_local_media_source(
            name="legacy-smb", qb_profile="", qb_path_prefix="",
            local_root="/media/downloads", owner="admin",
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE local_media_sources SET smb_user=?,smb_pass=? WHERE id=?",
                ("nas-user", "nas-secret", source_id),
            )
        db.init_db()
        source = db.get_local_media_source(source_id, owner="admin")
        self.assertEqual((source.smb_user, source.smb_pass), ("", ""))
        with db.get_conn() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(local_media_sources)")}
        self.assertTrue({"smb_user", "smb_pass"}.issubset(columns))

    def test_qb_hash_is_idempotent_and_owner_isolated(self):
        source_id = db.create_local_media_source(
            name="source-idempotent", qb_profile="qb", qb_path_prefix="/downloads",
            local_root="/mnt/downloads", owner="admin",
        )
        first = db.create_local_media_task(source_id, "HASH", "/mnt/downloads/A", owner="admin")
        second = db.create_local_media_task(source_id, "hash", "/mnt/downloads/A", owner="admin")
        self.assertEqual(first, second)
        self.assertIsNone(db.get_local_media_source(source_id, owner="other"))
        self.assertIsNone(db.get_local_media_task(first, owner="other"))
        self.assertEqual(db.list_local_media_tasks(owner="other"), [])

    def test_task_items_and_atomic_concurrent_claim(self):
        source_id = db.create_local_media_source(
            name="source-items", qb_profile="", qb_path_prefix="",
            local_root="/tmp/downloads-items", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", "/tmp/downloads/A.mkv", owner="admin", trigger="manual"
        )
        item_id = db.add_local_media_task_item(
            task_id, "/tmp/downloads/A.mkv", "/tmp/library/A.mkv",
            role="video", size=123, owner="admin",
        )
        self.assertGreater(item_id, 0)
        self.assertEqual(db.list_local_media_task_items(task_id, owner="admin")[0]["role"], "video")

        results: list[bool] = []
        barrier = threading.Barrier(2)

        def claim() -> None:
            barrier.wait()
            results.append(db.claim_local_media_task(task_id, expected="waiting_stable", owner="admin"))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False, True])

    def test_selected_log_clear_skips_busy_tasks_and_cascades_children(self):
        source_id = db.create_local_media_source(
            name="source-clear", qb_profile="", qb_path_prefix="",
            local_root="/tmp/downloads-clear", owner="admin",
        )
        completed_id = db.create_local_media_task(
            source_id, "", "/tmp/downloads-clear/Done.mkv", owner="admin", trigger="manual",
        )
        db.add_local_media_task_item(
            completed_id, "/tmp/downloads-clear/Done.mkv", "/tmp/library/Done.mkv",
            role="video", owner="admin",
        )
        completed = db.get_local_media_task(completed_id, owner="admin")
        db.add_local_media_operation_step(
            completed_id, completed.operation_token, 0, "move", owner="admin",
        )
        db.update_local_media_task(completed_id, owner="admin", status="completed")
        busy_id = db.create_local_media_task(
            source_id, "", "/tmp/downloads-clear/Busy.mkv", owner="admin", trigger="scan",
        )

        result = db.delete_local_media_tasks([completed_id, busy_id, 999999], owner="admin")

        self.assertEqual(result, {"requested": 3, "deleted": 1, "skipped_busy": 1, "missing": 1})
        self.assertIsNone(db.get_local_media_task(completed_id, owner="admin"))
        self.assertEqual(db.list_local_media_task_items(completed_id, owner="admin"), [])
        self.assertEqual(db.list_local_media_operation_steps(completed_id, owner="admin"), [])
        self.assertIsNotNone(db.get_local_media_task(busy_id, owner="admin"))

    def test_invalid_status_category_and_cross_owner_target_are_rejected(self):
        source_id = db.create_local_media_source(
            name="source-validation", qb_profile="", qb_path_prefix="",
            local_root="/tmp/downloads-validation", owner="admin",
        )
        with self.assertRaisesRegex(ValueError, "分类"):
            db.upsert_local_library_target(source_id, "music", "/tmp/music", owner="admin")
        with self.assertRaisesRegex(LookupError, "来源"):
            db.upsert_local_library_target(source_id, "movie", "/tmp/movies", owner="other")
        task_id = db.create_local_media_task(source_id, "x", "/tmp/downloads/x", owner="admin")
        with self.assertRaisesRegex(ValueError, "状态"):
            db.update_local_media_task(task_id, owner="admin", status="running")

    def test_source_bundle_rolls_back_fields_and_targets_on_database_failure(self):
        before_ids = {item.id for item in db.list_local_media_sources(owner="admin")}
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER fail_tv_target BEFORE INSERT ON local_library_targets "
                "WHEN NEW.category='tv' BEGIN SELECT RAISE(ABORT, 'simulated target failure'); END"
            )
        try:
            with self.assertRaisesRegex(Exception, "simulated target failure"):
                db.save_local_media_source_bundle(
                    name="atomic", qb_profile="configured:qb", qb_path_prefix="/downloads",
                    local_root="/mnt/downloads", enabled=True, stable_seconds=0, scan_enabled=False,
                    scan_interval_minutes=10, media_type="auto", mode="move", owner="admin",
                    targets=[
                        {"category": "movie", "path": "/media/movies"},
                        {"category": "tv", "path": "/media/tv"},
                    ],
                )
        finally:
            with db.get_conn() as conn:
                conn.execute("DROP TRIGGER IF EXISTS fail_tv_target")
        sources = db.list_local_media_sources(owner="admin")
        self.assertEqual({item.id for item in sources}, before_ids)
        self.assertNotIn("atomic", {item.name for item in sources})

    def test_manual_task_persists_position_overrides_and_retry_keeps_selection(self):
        source_id = db.create_local_media_source(
            name="manual-position", qb_profile="", qb_path_prefix="",
            local_root="/tmp/manual-position", media_type="tv", owner="admin",
        )
        task_id = db.prepare_manual_local_media_task(
            source_id, "/tmp/manual-position/Show.S01E07.mkv", owner="admin",
            tmdb_id="42", media_type="tv", season_override=2, episode_override=7,
        )
        task = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual((task.season_override, task.episode_override), (2, 7))
        db.update_local_media_task(
            task_id, owner="admin", status="requires_manual",
            recognition_summary='{"schema_version":1,"media":[]}',
        )
        self.assertTrue(db.reset_local_media_task(task_id, owner="admin"))
        retried = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(retried.recognition_summary, "")
        self.assertEqual(retried.tmdb_id, "42")
        self.assertEqual(retried.media_type, "tv")
        self.assertEqual((retried.season_override, retried.episode_override), (2, 7))

    def test_task_recognition_summary_round_trip_is_versioned(self):
        source_id = db.create_local_media_source(
            name="recognition-summary", qb_profile="", qb_path_prefix="",
            local_root="/tmp/recognition-summary", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", "/tmp/recognition-summary/Show.S02E08.mkv",
            owner="admin", trigger="scan",
        )
        before = db.get_local_media_task(task_id, owner="admin")
        summary = '{"schema_version":1,"media":[]}'
        self.assertTrue(db.update_local_media_task(
            task_id, owner="admin", recognition_summary=summary,
        ))
        after = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(after.recognition_summary, summary)
        self.assertEqual(after.version, before.version + 1)
        self.assertEqual(
            db.list_local_media_tasks(owner="admin")[0].recognition_summary, summary,
        )

    def test_local_confirmation_claim_is_versioned_and_atomic(self):
        source_id = db.create_local_media_source(
            name="telegram-confirm", qb_profile="", qb_path_prefix="",
            local_root="/tmp/telegram-confirm", media_type="movie", owner="admin",
        )
        task_id = db.create_local_media_task(
            source_id, "", "/tmp/telegram-confirm/Movie.mkv",
            owner="admin", trigger="scan",
        )
        db.update_local_media_task(
            task_id, owner="admin", status="requires_manual", snapshot_digest="digest-1"
        )
        task = db.get_local_media_task(task_id, owner="admin")

        self.assertFalse(db.claim_local_media_confirmation_task(
            task_id,
            owner="admin",
            expected_version=task.version + 1,
            expected_snapshot_digest="digest-1",
            tmdb_id="101",
            media_type="movie",
            rules_snapshot="{}",
        ))
        self.assertTrue(db.claim_local_media_confirmation_task(
            task_id,
            owner="admin",
            expected_version=task.version,
            expected_snapshot_digest="digest-1",
            tmdb_id="101",
            media_type="movie",
            rules_snapshot="{}",
            title="电影甲",
            year="2026",
        ))
        claimed = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(claimed.status, "recognizing")
        self.assertEqual((claimed.tmdb_id, claimed.media_type), ("101", "movie"))
        self.assertEqual((claimed.title, claimed.year), ("电影甲", "2026"))

    def test_manual_task_prepare_never_resets_an_active_task(self):
        source_id = db.create_local_media_source(
            name="manual-race", qb_profile="", qb_path_prefix="", local_root="/tmp/manual", owner="admin"
        )
        task_id = db.prepare_manual_local_media_task(
            source_id, "/tmp/manual/Movie.mkv", owner="admin", tmdb_id="1", media_type="movie"
        )
        self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
        with self.assertRaisesRegex(ValueError, "正在处理中"):
            db.prepare_manual_local_media_task(
                source_id, "/tmp/manual/Movie.mkv", owner="admin", tmdb_id="2", media_type="movie"
            )
        task = db.get_local_media_task(task_id, owner="admin")
        self.assertEqual(task.status, "recognizing")
        self.assertEqual(task.tmdb_id, "1")


if __name__ == "__main__":
    unittest.main()
