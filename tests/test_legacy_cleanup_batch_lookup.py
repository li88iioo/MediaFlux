"""历史确认补建按批查找，业务队列仍只使用唯一的确认终态入队器。"""

from __future__ import annotations

from contextlib import contextmanager
import unittest
from unittest.mock import patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import download_staging_reconcile as queue
from tests.support import isolated_test_database


class LegacyCleanupBatchLookupTests(unittest.TestCase):
    def setUp(self):
        from tests.test_download_staging_reconciliation import (
            DownloadStagingReconciliationTests,
        )

        self.path = self.enterContext(isolated_test_database("mediaflux.db"))
        # 复用已有业务 fixture，不继承并重复收集它的整组测试。
        self.fixture = DownloadStagingReconciliationTests(methodName="runTest")
        self.fixture.test_db_path = self.path
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def seed(self, count):
        f = self.fixture
        ids = [f.request_id]
        for index in range(1, count):
            root = f"stage-{index}"
            f.cloud.add_dir(root, "source")
            ids.append(f.request(str(index), root))
            f.confirm(str(index), payload={**f.payload, "source_dir_id": root})
        return ids

    def test_forty_old_downloads_lookup_confirmations_once_and_cleanup_once(self):
        ids = self.seed(40)
        statements = []
        actual = db.get_conn

        @contextmanager
        def traced():
            with actual() as conn:
                conn.set_trace_callback(statements.append)
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)

        with patch.object(db, "get_conn", traced):
            self.assertEqual(queue.discover_legacy_confirmation_cleanup(limit=40), 40)
        candidate_reads = [
            sql
            for sql in statements
            if "FROM organize_confirmations c" in sql
            and "json_extract(c.payload_json" in sql
            and "ORDER BY c.id DESC" in sql
        ]
        self.assertEqual(len(candidate_reads), 1)
        with db.get_conn() as conn:
            plan = " ".join(
                str(row[3])
                for row in conn.execute("EXPLAIN QUERY PLAN " + candidate_reads[0])
            )
        self.assertNotIn("USE TEMP B-TREE FOR ORDER BY", plan)
        self.fixture.drain(limit=40)
        self.assertEqual(len(self.fixture.cloud.deleted), 40)
        for rid in ids:
            get_download_request_result = download_requests_repo.get_download_request(rid)
            assert get_download_request_result is not None
            self.assertEqual(
                get_download_request_result["gy_staging_cleanup_status"], "completed"
            )
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(limit=40), 0)
        self.fixture.drain(limit=40)
        self.assertEqual(len(self.fixture.cloud.deleted), 40)

    def test_latest_incomplete_or_invalid_card_does_not_fall_back_to_old_success(self):
        f = self.fixture
        second = f.request("second", "stage-second")
        f.cloud.add_dir("stage-second", "source")
        f.confirm("second-done", payload={**f.payload, "source_dir_id": "stage-second"})
        f.confirm("newer-pending", status="pending", fingerprint="done")
        f.confirm(
            "newer-unsafe",
            payload={**f.payload, "source_dir_id": "stage-second"},
            stats={"moved": 0, "failed": 1},
        )
        # 损坏的历史JSON也不能使整页抛错或恢复较早的成功卡。
        f.confirm("invalid-json")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_confirmations SET payload_json='{' WHERE token='invalid-json'"
            )
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(), 0)
        self.assertEqual(queue.list_due_cleanup_intents(), [])
        self.assertEqual(db.kv_get(queue.LEGACY_CURSOR_KEY), str(second))
        f.drain()
        self.assertEqual(f.cloud.deleted, [])

    def test_repeated_source_has_one_admission_attempt_and_no_ambiguous_cleanup(self):
        self.fixture.request("shared-owner", "stage")
        actual = queue.enqueue_confirmation_cleanup
        with patch.object(
            queue, "enqueue_confirmation_cleanup", wraps=actual
        ) as enqueue:
            self.assertEqual(queue.discover_legacy_confirmation_cleanup(), 0)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(queue.list_due_cleanup_intents(), [])
        self.fixture.drain()
        self.assertEqual(self.fixture.cloud.deleted, [])

    def test_batch_failure_and_zip_recovery_preserve_cursor_and_do_not_repeat_deletes(
        self,
    ):
        import sqlite3
        from app.modules import backup
        from tests.test_ten_pass_process_restore_audit import runtime_paths

        ids = self.seed(8)
        with db.get_conn() as conn:
            conn.execute(
                f"CREATE TRIGGER fail_cleanup_insert BEFORE INSERT ON download_staging_reconcile WHEN NEW.request_id={ids[-1]} BEGIN SELECT RAISE(ABORT,'cleanup-page-fault'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cleanup-page-fault"):
            queue.discover_legacy_confirmation_cleanup()
        self.assertEqual(queue.list_due_cleanup_intents(), [])
        self.assertEqual(db.kv_get(queue.LEGACY_CURSOR_KEY), "0")
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER fail_cleanup_insert")
        paths = runtime_paths(self.path)
        archive = backup.create_backup(paths, reason="legacy-cleanup-page")
        backup.verify_backup(archive)
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(), 8)
        self.fixture.drain()
        self.assertEqual(len(self.fixture.cloud.deleted), 8)
        backup.restore_backup(paths, archive)
        db.init_db()
        self.assertEqual(db.kv_get(queue.LEGACY_CURSOR_KEY), "0")
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(), 8)
        self.fixture.drain()
        self.assertEqual(len(self.fixture.cloud.deleted), 8)
        self.assertEqual(queue.list_due_cleanup_intents(), [])
        self.assertEqual(queue.discover_legacy_confirmation_cleanup(), 0)
