"""深审：单个订阅候选读取必须按主键清理，不能反复扫描全部候选。"""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from app import database as db
from app.repositories import media_subscriptions as repository
from tests.support import isolated_test_database


class SubscriptionCandidateLookupTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database("subscription-lookup.db"))
        self._sequence = 0

    def seed(self, count, *, expired=False):
        self._sequence += 1
        subscription_id = repository.add_media_subscription(
            provider="tmdb", external_id=str(self._sequence), tmdb_id=str(self._sequence),
            media_type="tv", title="本地验收剧集",
        )
        stamp = db.now()
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO media_subscription_candidates("
                "subscription_id,media_key,result_id,title,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                [
                    (subscription_id, f"tmdb:{self._sequence}:tv:S01E{i:03d}", f"result-{i}",
                     f"Episode {i}", "2000-01-01 00:00:00" if expired else "2099-01-01 00:00:00",
                     stamp, stamp)
                    for i in range(1, count + 1)
                ],
            )
            return [int(row[0]) for row in conn.execute(
                "SELECT id FROM media_subscription_candidates WHERE subscription_id=? ORDER BY id",
                (subscription_id,),
            )]

    def lookup_work(self, candidate_id, *, repetitions=20):
        original = repository.get_conn
        ticks = 0

        def step():
            nonlocal ticks
            ticks += 1
            return 0

        @contextmanager
        def measured_connection():
            with original() as connection:
                connection.set_progress_handler(step, 100)
                try:
                    yield connection
                finally:
                    connection.set_progress_handler(None, 0)

        with mock.patch.object(repository, "get_conn", measured_connection):
            for _ in range(repetitions):
                result = repository.get_media_subscription_candidate(candidate_id)
                assert result is not None
                self.assertEqual(result["status"], "available")
        return ticks

    def test_point_lookup_work_does_not_scale_with_unrelated_candidates(self):
        candidate_id = self.seed(100)[0]
        small = self.lookup_work(candidate_id)
        self.seed(1000)
        large = self.lookup_work(candidate_id)
        self.assertLessEqual(large, small * 2 + 5, f"SQLite VM blocks: {small} -> {large}")

    def test_point_cleanup_and_read_share_one_database_transaction(self):
        candidate_id = self.seed(1)[0]
        with mock.patch.object(repository, "get_conn", wraps=repository.get_conn) as connection:
            for _ in range(5):
                get_media_subscription_candidate_result = repository.get_media_subscription_candidate(candidate_id)
                assert get_media_subscription_candidate_result is not None
                self.assertEqual(get_media_subscription_candidate_result["status"], "available")
        self.assertEqual(connection.call_count, 5)

    def test_expired_lookup_updates_only_the_requested_candidate(self):
        first, other = self.seed(2, expired=True)
        result = repository.get_media_subscription_candidate(first)
        assert result is not None
        self.assertEqual(result["status"], "expired")
        with db.get_conn() as connection:
            untouched = connection.execute(
                "SELECT status FROM media_subscription_candidates WHERE id=?", (other,),
            ).fetchone()[0]
        self.assertEqual(untouched, "available")
        # 全量到期维护仍可明确执行，不改变它的旧接口。
        self.assertEqual(repository.expire_media_subscription_candidates(), 1)
        get_media_subscription_candidate_result_2 = repository.get_media_subscription_candidate(other)
        assert get_media_subscription_candidate_result_2 is not None
        self.assertEqual(get_media_subscription_candidate_result_2["status"], "expired")

    def test_repeated_expired_lookup_preserves_submitted_history(self):
        expired, submitted = self.seed(2, expired=True)
        repository.update_media_subscription_candidate(submitted, status="submitted")
        for _ in range(2):
            get_media_subscription_candidate_result_3 = repository.get_media_subscription_candidate(expired)
            assert get_media_subscription_candidate_result_3 is not None
            self.assertEqual(get_media_subscription_candidate_result_3["status"], "expired")
            get_media_subscription_candidate_result_4 = repository.get_media_subscription_candidate(submitted)
            assert get_media_subscription_candidate_result_4 is not None
            self.assertEqual(get_media_subscription_candidate_result_4["status"], "submitted")
        self.assertIsNone(repository.get_media_subscription_candidate(999_999))
