"""STRM 批量租约、持久刷新交接与事务失败的交叉回归。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app import database as db
from app.repositories import strm as repository
from tests.support import isolated_test_database
from tests.test_strm_metadata_queue import _job


class STRMBatchHandoffAuditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())

    @staticmethod
    def complete(job, path=""):
        return db.settle_strm_metadata_job(
            job["id"],
            expected_owner=job["lease_owner"],
            expected_lease_generation=job["lease_generation"],
            expected_revision=job["revision"],
            refresh_paths=(path,),
        )

    def test_owner_recovery_and_new_revisions_fence_every_old_batch_result(self):
        items = [_job(file_id=f"meta-{i}", etag="old") for i in range(24)]
        self.assertEqual(db.enqueue_strm_metadata_jobs(items)["created"], 24)
        first = db.claim_due_strm_metadata_jobs(owner="first", limit=12)
        second = db.claim_due_strm_metadata_jobs(owner="second", limit=12)
        self.assertEqual(len(first), 12)
        self.assertEqual(len(second), 12)
        updated = [dict(item, etag="latest") for item in items]
        self.assertEqual(db.enqueue_strm_metadata_jobs(updated)["dirty"], 24)
        self.assertEqual(
            db.recover_stale_strm_metadata_jobs(force=True, owner="first"), 12
        )
        for row in first:
            self.assertEqual(
                self.complete(row, f"/synthetic/stale/{row['id']}"), "stale"
            )
        for row in second:
            self.assertEqual(
                self.complete(row, f"/synthetic/stale/{row['id']}"), "queued"
            )
        self.assertEqual(db.count_strm_refresh_paths(), 0)
        resumed = db.claim_due_strm_metadata_jobs(owner="resumed", limit=30)
        self.assertEqual(len(resumed), 24)
        for row in resumed:
            self.assertEqual(row["etag"], "latest")
            self.assertEqual(
                self.complete(row, f"/synthetic/current/{row['id']}"), "completed"
            )
        before = db.list_strm_refresh_entries()
        for row in [*first, *second]:
            self.assertEqual(self.complete(row, "/synthetic/late"), "stale")
        self.assertEqual(db.list_strm_refresh_entries(), before)
        self.assertEqual(len(before), 24)
        db.init_db()
        self.assertEqual(db.list_strm_refresh_entries(), before)

    def test_outbox_failure_rolls_back_completion_and_allows_single_retry(self):
        db.enqueue_strm_metadata_jobs([_job()])
        row = db.claim_due_strm_metadata_jobs(owner="worker")[0]
        before = dict(db.list_strm_metadata_queue(status="all")[0])
        with patch.object(
            repository,
            "_enqueue_strm_refresh_paths",
            side_effect=OSError("disk unavailable"),
        ):
            with self.assertRaises(OSError):
                self.complete(row, "/synthetic/movie.nfo")
        self.assertEqual(dict(db.list_strm_metadata_queue(status="all")[0]), before)
        self.assertEqual(db.count_strm_refresh_paths(), 0)
        self.assertEqual(self.complete(row, "/synthetic/movie.nfo"), "completed")
        self.assertEqual(self.complete(row, "/synthetic/movie.nfo"), "stale")
        self.assertEqual(db.count_strm_refresh_paths(), 1)

    def test_refresh_reopen_and_stale_ack_preserve_new_event_and_provider_scope(self):
        paths = [f"/synthetic/batch/{i}.nfo" for i in range(31)]
        old = db.enqueue_strm_refresh_paths(paths, allow_emby=False)
        db.enqueue_strm_refresh_paths(paths, allow_emby=True)
        latest = db.enqueue_strm_refresh_paths(paths, allow_emby=False)
        db.init_db()
        self.assertEqual(db.acknowledge_strm_refresh_paths(old), 0)
        self.assertEqual(db.count_strm_refresh_paths(), 62)
        self.assertEqual(db.acknowledge_strm_refresh_paths(latest), 31)
        remaining = db.list_strm_refresh_entries()
        self.assertEqual({item["path"] for item in remaining}, set(paths))
        self.assertTrue(all(item["allow_emby"] for item in remaining))
        self.assertEqual(db.acknowledge_strm_refresh_paths(remaining), 31)
        self.assertEqual(db.count_strm_refresh_paths(), 0)
