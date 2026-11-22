"""GCID 重试凭据与明细随 SQLite/ZIP 恢复；未知结果保留，不猜测重放。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from app import database as db
from app.modules import gcid_import
from app.modules.backup import create_backup, recover_pending_restore, restore_backup
from app.runtime_paths import RuntimePaths
from tests.support import isolated_test_database
from tests.test_gcid_import_api import (
    TEST_ACCOUNT_KEY,
    TEST_FILE_MD5,
    FakeImporter,
    FakeOutcome,
    mock_importer,
)


class GCIDHistoryRecoveryAuditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(
            gcid_import, "_open_importer",
            side_effect=AssertionError("恢复历史的重放读取不得请求写入器"),
        ))
        self.enterContext(patch.object(gcid_import.notifier, "notify_gcid_import_started"))
        self.enterContext(patch.object(gcid_import.notifier, "notify_gcid_import_finished"))

    @staticmethod
    def _task():
        task_id = db.create_gcid_import_task(
            operation_token="historical-initial",
            manifest_digest="a" * 64,
            target_dir_id="target",
            target_account_key=TEST_ACCOUNT_KEY,
            file_count=2,
            total_size=2,
        )
        db.replace_gcid_import_items(
            task_id,
            [
                {
                    "path": "Film/A.mkv",
                    "size": 1,
                    "gcid": "a",
                    "status": "success",
                    "remote_file_id": "existing-a",
                },
                {
                    "path": "Film/B.mkv", "size": 1, "gcid": "b",
                    "md5": TEST_FILE_MD5, "status": "failed",
                },
            ],
        )
        gcid_import._finish_task(task_id)
        return task_id

    def test_sqlite_zip_restore_keeps_retry_receipt_and_successful_item_identity(self):
        with isolated_test_database("mediaflux.db") as database_path:
            task_id = self._task()
            original_items = db.list_gcid_import_items(task_id)
            fake = FakeImporter({"Film/B.mkv": [FakeOutcome(False)]})
            with mock_importer(fake):
                first, replayed = gcid_import.retry_task(
                    task_id, operation_token="saved-retry"
                )
            self.assertFalse(replayed)
            self.assertEqual(first["status"], "partial_success")
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
            paths.env_file.write_text("WEB_PORT=1258\n", encoding="utf-8")
            archive = create_backup(paths)
            db.kv_set(gcid_import._RETRY_REPLAY_KEY, "[]")
            db.update_gcid_import_task(task_id, status="success", failed_count=0)
            restore_backup(paths, archive)
            db.configure_database(database_path, test_mode=True)
            db.init_db()
            gcid_import.reset_runtime_state()
            self.assertFalse(recover_pending_restore(paths))
            # 恢复后的重放仅返回已保存的结果。
            old, replayed = gcid_import.retry_task(task_id, operation_token="saved-retry")
            self.assertTrue(replayed)
            self.assertEqual(old["status"], "partial_success")
            success = FakeImporter()
            with mock_importer(success):
                final, replayed = gcid_import.retry_task(
                    task_id, operation_token="explicit-new-retry"
                )
            self.assertFalse(replayed)
            self.assertEqual(final["status"], "success")
            self.assertEqual(
                [(call["path"], call["md5"]) for call in success.calls],
                [("Film/B.mkv", TEST_FILE_MD5)],
            )
            self.assertEqual(
                [row["id"] for row in db.list_gcid_import_items(task_id)],
                [row["id"] for row in original_items],
            )
            self.assertEqual(
                dict(db.list_gcid_import_items(task_id)[0]), dict(original_items[0])
            )

    def test_interruption_after_private_call_keeps_unknown_running_without_replay(self):
        class InterruptedImporter:
            calls = 0

            def import_file(self, **kwargs):
                self.calls += 1
                raise KeyboardInterrupt("synthetic interruption after possible write")

        with isolated_test_database("mediaflux.db"):
            task_id = self._task()
            fake = InterruptedImporter()
            with mock_importer(fake):
                with self.assertRaises(KeyboardInterrupt):
                    gcid_import.retry_task(task_id, operation_token="interrupted")
                gcid_import.reset_runtime_state()
                db.init_db()
                for token in ("interrupted", "different-token"):
                    with self.subTest(token=token):
                        current, replayed = gcid_import.retry_task(
                            task_id, operation_token=token
                        )
                        self.assertTrue(replayed)
                        self.assertEqual(current["status"], "running")
                        self.assertFalse(current["can_retry"])
            self.assertEqual(fake.calls, 1)
            self.assertEqual(
                [row["status"] for row in db.list_gcid_import_items(task_id)],
                ["success", "running"],
            )

    def test_retry_receipt_write_failure_rolls_back_admission_before_private_call(self):
        with isolated_test_database("mediaflux.db"):
            task_id = self._task()
            get_gcid_import_task_result = db.get_gcid_import_task(task_id)
            assert get_gcid_import_task_result is not None
            before = dict(get_gcid_import_task_result)
            before_ledger = db.kv_get(gcid_import._RETRY_REPLAY_KEY)
            fake = FakeImporter()
            with mock_importer(fake):
                with patch.object(
                    gcid_import,
                    "_write_retry_replays",
                    side_effect=sqlite3.OperationalError(
                        "synthetic receipt storage failure"
                    ),
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        gcid_import.retry_task(
                            task_id, operation_token="receipt-failure"
                        )
                self.assertEqual(fake.calls, [])
                get_gcid_import_task_result_2 = db.get_gcid_import_task(task_id)
                assert get_gcid_import_task_result_2 is not None
                self.assertEqual(dict(get_gcid_import_task_result_2), before)
                self.assertEqual(
                    db.kv_get(gcid_import._RETRY_REPLAY_KEY), before_ledger
                )
                final, replayed = gcid_import.retry_task(
                    task_id, operation_token="receipt-failure"
                )
            self.assertFalse(replayed)
            self.assertEqual(final["status"], "success")
            self.assertEqual(len(fake.calls), 1)

    def test_corrupted_historical_retry_ledger_is_not_overwritten_or_bypassed(self):
        with isolated_test_database("mediaflux.db"):
            task_id = self._task()
            get_gcid_import_task_result_3 = db.get_gcid_import_task(task_id)
            assert get_gcid_import_task_result_3 is not None
            before = dict(get_gcid_import_task_result_3)
            fake = FakeImporter()
            for raw in ('{"broken":', "{}", '[[true,"token"]]', '[[1,""]]'):
                with self.subTest(raw=raw), mock_importer(fake):
                    db.kv_set(gcid_import._RETRY_REPLAY_KEY, raw)
                    with self.assertRaisesRegex(ValueError, "凭据损坏"):
                        gcid_import.retry_task(task_id, operation_token="new")
                    self.assertEqual(db.kv_get(gcid_import._RETRY_REPLAY_KEY), raw)
                    get_gcid_import_task_result_4 = db.get_gcid_import_task(task_id)
                    assert get_gcid_import_task_result_4 is not None
                    self.assertEqual(dict(get_gcid_import_task_result_4), before)
            self.assertEqual(fake.calls, [])
