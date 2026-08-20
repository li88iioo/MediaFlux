"""TMDB 人工映射锁 Repository 与兼容门面测试。"""
from __future__ import annotations

import unittest

from app import database as db
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


if __name__ == "__main__":
    unittest.main()
