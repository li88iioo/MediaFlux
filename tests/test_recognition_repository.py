"""TMDB 人工映射锁 Repository 与兼容门面测试。"""
from __future__ import annotations

import unittest
import json
import sqlite3
from unittest.mock import patch

from app import database as db
from app import database_migrations
from app.repositories import recognition
from tests.support import isolated_test_database


class RecognitionRepositoryTests(unittest.TestCase):
    def test_database_facade_keeps_management_entrypoints(self):
        self.assertIs(db.list_tmdb_locks, recognition.list_tmdb_locks)
        self.assertIs(db.delete_tmdb_lock, recognition.delete_tmdb_lock)
        self.assertIs(db.get_tmdb_lock, recognition.get_tmdb_lock)
        self.assertIs(db.upsert_tmdb_lock, recognition.upsert_tmdb_lock)

    def test_lock_round_trip_uses_full_context_and_manual_source(self):
        with isolated_test_database("recognition-repository.db"):
            recognition.upsert_tmdb_lock(
                raw_name="Demo.S02E03.mkv",
                parent_path="TV/Demo/Season 02",
                tmdb_id="42",
                title="Demo",
                year="2026",
                media_type="tv",
                season=2,
                lock_source="manual",
            )
            row = recognition.get_tmdb_lock(
                raw_name="Demo.S02E03.mkv",
                parent_path="TV/Demo/Season 02",
                media_type="tv",
                season=2,
            )
            assert row is not None
            self.assertEqual(row["tmdb_id"], "42")
            self.assertEqual(row["season"], 2)
            self.assertIsNone(recognition.get_tmdb_lock(
                raw_name="Demo.S02E03.mkv",
                parent_path="TV/Demo/Season 02",
                media_type="tv",
                season=1,
            ))
            locks = db.list_tmdb_locks("Demo")
            self.assertEqual(len(locks), 1)
            self.assertTrue(db.delete_tmdb_lock(int(locks[0]["id"])))


class ManualEpisodeConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database("manual-episode-confirmations.db"))

    @staticmethod
    def position(episode=5):
        return {"manual_position": {"version": 1, "source": "manual_correction", "season": 2, "episode": episode}}

    def learn(self, **overrides):
        fields = dict(source="guangya", file_id="video", status="success", tmdb_id="73223",
                      original_name="[ANi] 黑色五葉草 Season 2 - 175 [1080P].mp4",
                      original_path="incoming", original_parent_id="source", new_path="archive/S02E05.mp4",
                      media_type="tv", season=2, episode=5, release_parse=self.position(), operation_token="manual")
        fields.update(overrides)
        return db.add_organize_log(**fields)

    @staticmethod
    def records(tmdb_id="73223"):
        return [dict(row) for row in recognition.list_manual_episode_confirmations(tmdb_id)]

    @staticmethod
    def stored_log(log_id: int) -> dict:
        row = db.get_organize_log(log_id)
        assert row is not None, f"整理日志 {log_id} 应存在"
        return dict(row)

    def test_recycle_and_filtered_log_clear_keep_independent_learning(self):
        lid = self.learn()
        before = self.records()
        # 移入回收站只是文件生命周期，不是否定已经确认的编号。
        db.update_organize_log(lid, status="deleted", operation_type="delete")
        self.assertEqual(self.records(), before)
        result = db.clear_organize_logs()
        self.assertEqual(result["deleted_guangya"], 1)
        db.init_db()
        self.assertEqual(self.records(), before)
        self.assertIsNone(db.get_organize_log(lid))

    def test_same_file_correction_replaces_knowledge_even_after_log_cleanup(self):
        self.learn()
        db.clear_organize_logs()
        lid = self.learn(tmdb_id="123", episode=6, release_parse=self.position(6))
        self.assertEqual(self.records(), [])
        self.assertEqual([(r["log_id"], r["episode"]) for r in self.records("123")], [(lid, 6)])
        db.update_organize_log(lid, media_type="movie", release_parse_json="{}", operation_type="reorganize")
        self.assertEqual(self.records("123"), [])

    def test_old_log_maintenance_cannot_replay_a_superseded_confirmation(self):
        old = self.learn()
        latest = self.learn(tmdb_id="123", episode=6, release_parse=self.position(6))
        for fields in ({"error": "后处理更新"}, {"status": "success", "error": "失败操作恢复原状态"}):
            db.update_organize_log(old, **fields)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.records("123")[0]["log_id"], latest)
        # 不是按日志 ID 判新旧：用户明确再次纠正旧日志仍应更新。
        db.update_organize_log(old, status="success", operation_type="reorganize", episode=7,
                               release_parse_json=json.dumps(self.position(7)))
        self.assertEqual(self.records("123"), [])
        self.assertEqual(self.records()[0]["episode"], 7)

    def test_automatic_or_failed_attempts_do_not_replace_previous_success(self):
        self.learn()
        before = self.records()
        for fields in ({"status": "failed"}, {"status": "skipped"},
                       {"release_parse": {}}, {"status": "reorganizing"}):
            with self.subTest(fields=fields):
                lid = self.learn(tmdb_id="999", **fields)
                self.assertEqual(self.records(), before)
                self.assertEqual(self.records("999"), [])
                if fields.get("status") == "reorganizing":
                    db.init_db()
                    self.assertEqual(self.stored_log(lid)["status"], "interrupted")
                    self.assertEqual(self.records(), before)

    def test_invalid_markers_and_agent_confirmation_do_not_become_learning(self):
        markers = [{}, {"manual_position": []}]
        markers.extend({"manual_position": {**self.position()["manual_position"], **changes}} for changes in (
            {"version": True}, {"version": 2}, {"source": "automatic"}, {"season": True},
            {"episode": True}, {"episode": None}, {"episode": 1000}, {"episode": 6},
        ))
        for marker in markers:
            with self.subTest(marker=marker):
                self.learn(release_parse=marker)
                self.assertEqual(self.records(), [])
        self.learn(operation_token="agent-confirm")
        db.mark_organize_logs_confirmation_actor("agent-confirm", "agent")
        self.assertEqual(self.records(), [])
        db.init_db()
        self.assertEqual(self.records(), [])

    def test_failed_learning_write_rolls_back_the_business_result(self):
        lid = self.learn(status="skipped")
        before = self.stored_log(lid)
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_learning BEFORE INSERT ON manual_episode_confirmations "
                         "BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic disk failure"):
            db.update_organize_log(lid, status="success", operation_type="reorganize")
        self.assertEqual(self.stored_log(lid), before)
        self.assertEqual(self.records(), [])

    def test_snapshot_undo_restores_previous_knowledge_and_actor(self):
        lid = self.learn()
        before = db.capture_organize_business_snapshot(lid)
        db.update_organize_log(lid, tmdb_id="123", episode=6, release_parse_json=json.dumps(self.position(6)),
                               confirmation_actor="human", operation_type="reorganize")
        self.assertEqual(self.records(), [])
        self.assertEqual(self.records("123")[0]["episode"], 6)
        db.restore_organize_business_snapshot(lid, before, status="reverted", operation_type="revert")
        self.assertEqual(self.records("123"), [])
        self.assertEqual(self.records(), [before["manual_episode_confirmation"]])
        db.update_organize_log(lid, error="后处理提示不应改变识别事实")
        db.init_db()
        self.assertEqual(self.records(), [before["manual_episode_confirmation"]])
        self.assertEqual(self.stored_log(lid)["confirmation_actor"], before["log"]["confirmation_actor"])
        db.update_organize_log(lid, status="reverted", operation_type="return_to_source")
        self.assertEqual(self.records(), [])

    def test_legacy_snapshot_without_learning_still_restores_business(self):
        lid = self.learn(status="skipped", release_parse={})
        before = db.capture_organize_business_snapshot(lid)
        before.pop("manual_episode_confirmation")
        before["log"].pop("confirmation_actor")
        db.update_organize_log(lid, status="success", release_parse_json=json.dumps(self.position()))
        self.assertEqual(len(self.records()), 1)
        db.restore_organize_business_snapshot(lid, before, status="reverted", operation_type="revert")
        self.assertEqual(self.records(), [])
        self.assertEqual(self.stored_log(lid)["release_parse_json"], "")

    def test_restart_revokes_learning_only_for_completed_return_receipts(self):
        for completed in (True, False):
            with self.subTest(completed=completed), isolated_test_database(f"return-{completed}.db"):
                lid = self.learn(current_parent_id="archive", current_name="S02E05.mp4")
                before = self.records()
                self.assertTrue(db.claim_organize_log_operation(lid, "return", "returning", ("success",)))
                db.add_organize_log_items(lid, [{
                    "file_id": "video", "role": "video", "original_parent_id": "source",
                    "original_name": "original.mp4", "current_parent_id": "source" if completed else "archive",
                    "current_name": "original.mp4" if completed else "S02E05.mp4",
                    "status": "success" if completed else "running",
                }])
                db.add_organize_operation_step(lid, "return", 1, "return_to_source", file_id="video",
                                               to_parent_id="source", to_name="original.mp4",
                                               status="success" if completed else "running")
                db.init_db()
                self.assertEqual(self.stored_log(lid)["status"], "reverted" if completed else "interrupted")
                self.assertEqual(self.records(), [] if completed else before)

    def test_multiple_files_and_providers_are_not_collapsed_into_one_example(self):
        self.learn()
        self.learn(file_id="other-video", episode=8, release_parse=self.position(8))
        self.learn(source="another-account", episode=9, release_parse=self.position(9))
        self.assertEqual(sorted(r["episode"] for r in self.records()), [5, 8, 9])
        self.learn(file_id="adult", provider="metatube")
        self.assertEqual(len(self.records()), 3)

    def test_upgrade_migrates_valid_history_once_in_both_supported_entrypoints(self):
        for version in (36, 0):
            with self.subTest(version=version), isolated_test_database(f"legacy-{version}.db"):
                self.learn(file_id="valid")
                self.learn(file_id="failed", status="failed")
                bad = self.learn(file_id="bad-json")
                agent = self.learn(file_id="agent", operation_token="agent")
                db.mark_organize_logs_confirmation_actor("agent", "agent")
                with db.get_conn() as conn:
                    conn.execute("UPDATE organize_log SET release_parse_json='not-json' WHERE id=?", (bad,))
                    conn.execute("DROP TABLE manual_episode_confirmations")
                    conn.execute(f"PRAGMA user_version={version}")
                db.init_db()
                self.assertEqual([r["file_id"] for r in self.records()], ["valid"])
                self.assertEqual(self.stored_log(agent)["confirmation_actor"], "agent")
                with db.get_conn() as conn:
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                    # 模拟经验被明确撤销，普通启动不能重新从旧日志学回来。
                    conn.execute("DELETE FROM manual_episode_confirmations")
                db.init_db()
                self.assertEqual(self.records(), [])

    def test_upgrade_failure_rolls_back_data_ddl_and_version(self):
        lid = self.learn()
        with db.get_conn() as conn:
            conn.execute("DROP TABLE manual_episode_confirmations")
            conn.execute("PRAGMA user_version=36")
        migrate = database_migrations._SCHEMA_MIGRATIONS[36]

        def fail_after_migration(conn):
            migrate(conn)
            raise RuntimeError("synthetic migration failure")

        with patch.dict(database_migrations._SCHEMA_MIGRATIONS, {36: fail_after_migration}):
            with self.assertRaisesRegex(RuntimeError, "synthetic migration failure"):
                db.init_db()
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 36)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='manual_episode_confirmations'").fetchone())
        self.assertEqual(self.stored_log(lid)["status"], "success")
        db.init_db()
        self.assertEqual([r["log_id"] for r in self.records()], [lid])

    def test_upgrade_uses_confirmation_steps_not_later_log_maintenance_time(self):
        old = self.learn()
        latest = self.learn(tmdb_id="123", episode=6, release_parse=self.position(6))
        for lid, hour in ((old, "10"), (latest, "11")):
            db.add_organize_operation_step(lid, f"manual-{lid}", 1, "move_rename", status="success",
                                          finished_at=f"2026-11-02 {hour}:00:00")
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_log SET updated_at='2026-11-02 12:00:00' WHERE id=?", (old,))
            conn.execute("DROP TABLE manual_episode_confirmations")
            conn.execute("PRAGMA user_version=36")
        db.init_db()
        self.assertEqual(self.records(), [])
        self.assertEqual([r["log_id"] for r in self.records("123")], [latest])


if __name__ == "__main__":
    unittest.main()
