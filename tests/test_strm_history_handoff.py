"""历史未索引STRM副本必须先持久登记，再复用路径清理与刷新交接。"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app import database as db
from app.repositories import strm as strm_repo
from tests.support import isolated_test_database
from tests.test_strm_move_recovery import sync


class StrmHistoricalHandoffTests(unittest.TestCase):
    def setUp(self):
        self.path = self.enterContext(isolated_test_database("mediaflux.db"))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        sync(self.root, "Current")
        self.current = next(self.root.rglob("*.strm"))
        self.old = self.current.parent.parent / "Legacy" / "Copy.strm"
        self.old.parent.mkdir()
        self.old.write_bytes(self.current.read_bytes())
        with db.get_conn() as conn:
            conn.execute("DELETE FROM strm_refresh_outbox")

    @staticmethod
    def sink(paths):
        strm_repo.enqueue_strm_refresh_paths(paths)

    def crash_after_historical_unlink(self):
        script = r"""
import tests
import os, socket, sys
from pathlib import Path
from unittest.mock import patch
from app import database as db
from app.repositories import strm as strm_repo
from app.modules import strm
from tests.test_strm_move_recovery import sync
socket.socket.connect = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network forbidden"))
db.configure_database(Path(sys.argv[1]), test_mode=True)
old = Path(sys.argv[3])
actual = strm._delete_owned_file
def crash(path, *args, **kwargs):
 result = actual(path, *args, **kwargs)
 if Path(path) == old and result: os._exit(73)
 return result
with patch.object(strm, "_delete_owned_file", side_effect=crash):
 sync(sys.argv[2], "Current", on_refresh_paths=strm_repo.enqueue_strm_refresh_paths)
raise AssertionError("historical unlink window not reached")
"""
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(self.path),
                str(self.root),
                str(self.old),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(child.returncode, 73, child.stderr)

    def test_process_exit_after_unindexed_copy_delete_keeps_refresh_receipt(self):
        self.crash_after_historical_unlink()
        self.assertFalse(self.old.exists())
        self.assertTrue(self.current.exists())
        pending = strm_repo.list_strm_path_cleanup("guangya:source")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["strm_path"], str(self.old))
        sync(self.root, "Current", on_refresh_paths=self.sink)
        entries = strm_repo.list_strm_refresh_entries()
        self.assertIn(str(self.old.parent), {row["path"] for row in entries})
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya:source"), [])
        strm_repo.acknowledge_strm_refresh_paths(entries)
        sync(self.root, "Current", on_refresh_paths=self.sink)
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])

    def historical(self, *, sink=None, sources=None):
        from app.modules.strm_recovery import reconcile_historical_strm
        from app.modules.scheduler import STRMScheduler
        from tests.test_strm_move_recovery import BASE_URL

        stats = STRMScheduler._empty_stats()
        reconcile_historical_strm(
            str(self.root),
            BASE_URL,
            sources or [{"id": "source"}],
            stats,
            on_refresh_paths=sink or self.sink,
        )
        return stats

    def test_receipt_is_visible_from_another_connection_before_unlink(self):
        from unittest.mock import patch
        from app.modules import strm

        actual = strm._delete_owned_file
        observed = []

        def inspect(path, *args, **kwargs):
            if Path(path) == self.old:
                records = strm_repo.list_strm_path_cleanup("guangya:source")
                observed.append([row["strm_path"] for row in records])
                self.assertTrue(self.old.exists())
            return actual(path, *args, **kwargs)

        with patch.object(strm, "_delete_owned_file", side_effect=inspect):
            stats = self.historical()
        self.assertEqual(observed, [[str(self.old)]])
        self.assertEqual(stats["cleaned"], 1)
        self.assertFalse(stats["clean_skipped"])
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya:source"), [])
        self.assertIn(
            str(self.old.parent),
            {row["path"] for row in strm_repo.list_strm_refresh_entries()},
        )

    def test_refresh_failure_keeps_deleted_path_receipt_until_retry(self):
        def unavailable(_paths):
            raise OSError("isolated refresh-store failure")

        stats = self.historical(sink=unavailable)
        self.assertFalse(self.old.exists())
        self.assertTrue(stats["stopped"])
        self.assertEqual(stats["stop_stage"], "refresh-persist")
        self.assertEqual(len(strm_repo.list_strm_path_cleanup("guangya:source")), 1)
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])
        recovered = self.historical()
        self.assertEqual(recovered["cleaned"], 0)
        self.assertFalse(recovered["clean_skipped"])
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya:source"), [])
        self.assertIn(
            str(self.old.parent),
            {row["path"] for row in strm_repo.list_strm_refresh_entries()},
        )

    def test_receipt_write_failure_leaves_file_untouched_and_retryable(self):
        original = self.old.read_bytes()
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER fail_history_receipt BEFORE INSERT ON strm_path_cleanup BEGIN SELECT RAISE(ABORT,'receipt-storage-failure'); END"
            )
        stats = self.historical()
        self.assertEqual(self.old.read_bytes(), original)
        self.assertEqual(stats["cleaned"], 0)
        self.assertTrue(stats["clean_skipped"])
        self.assertEqual(stats["stop_stage"], "recovery-persist")
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya:source"), [])
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER fail_history_receipt")
        self.assertEqual(self.historical()["cleaned"], 1)
        self.assertFalse(self.old.exists())

    def test_501_copies_register_two_batches_and_use_one_cleanup_consumer(self):
        from unittest.mock import patch
        from app.modules import strm_recovery

        payload = self.current.read_bytes()
        for index in range(500):
            (self.old.parent / f"Copy-{index:03d}.strm").write_bytes(payload)
        actual = strm_repo.enqueue_strm_path_cleanup
        batch_sizes = []

        def record(items):
            batch_sizes.append(len(items))
            return actual(items)

        with (
            patch.object(strm_repo, "enqueue_strm_path_cleanup", side_effect=record),
            patch.object(
                strm_recovery,
                "recover_pending_paths",
                wraps=strm_recovery.recover_pending_paths,
            ) as consume,
        ):
            stats = self.historical()
        self.assertEqual(batch_sizes, [500, 1])
        self.assertEqual(consume.call_count, 1)
        self.assertEqual(stats["cleaned"], 501)
        self.assertFalse(stats["clean_skipped"])
        self.assertEqual(self.current.read_bytes(), payload)
        self.assertEqual(list(self.old.parent.glob("*.strm")), [])
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya:source"), [])
        self.assertIn(
            str(self.old.parent),
            {row["path"] for row in strm_repo.list_strm_refresh_entries()},
        )

    def test_zip_restore_after_process_loss_replays_refresh_without_deleting_again(
        self,
    ):
        from app.modules import backup
        from tests.test_ten_pass_process_restore_audit import runtime_paths

        expected = self.current.read_bytes()
        self.crash_after_historical_unlink()
        paths = runtime_paths(self.path)
        archive = backup.create_backup(paths, reason="historical-strm-unlink")
        backup.verify_backup(archive)
        self.historical()
        strm_repo.acknowledge_strm_refresh_paths(strm_repo.list_strm_refresh_entries())
        backup.restore_backup(paths, archive)
        db.init_db()
        self.assertEqual(len(strm_repo.list_strm_path_cleanup("guangya:source")), 1)
        stats = self.historical()
        self.assertEqual(stats["cleaned"], 0)
        self.assertEqual(self.current.read_bytes(), expected)
        self.assertFalse(self.old.exists())
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya:source"), [])
        entries = strm_repo.list_strm_refresh_entries()
        self.assertIn(str(self.old.parent), {row["path"] for row in entries})
        strm_repo.acknowledge_strm_refresh_paths(entries)
        self.historical()
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])

    def test_matching_alternate_source_is_used_if_first_replacement_is_missing(self):
        from unittest.mock import patch

        payload = self.current.read_bytes()
        row = strm_repo.list_strm_index("guangya:source")[0]
        alternate = self.current.parent.parent / "Alternate" / self.current.name
        alternate.parent.mkdir()
        alternate.write_bytes(payload)
        strm_repo.upsert_strm_index(
            "guangya:other",
            row["file_id"],
            row["etag"],
            row["size"],
            row["filename"],
            str(alternate),
            row["content_fingerprint"],
        )
        self.current.unlink()
        actual = strm_repo.enqueue_strm_path_cleanup
        recorded_sources = []

        def record(items):
            recorded_sources.extend(item["source"] for item in items)
            return actual(items)

        with patch.object(strm_repo, "enqueue_strm_path_cleanup", side_effect=record):
            stats = self.historical(sources=[{"id": "source"}, {"id": "other"}])
        self.assertIn("guangya:other", recorded_sources)
        self.assertEqual(stats["cleaned"], 1)
        self.assertFalse(self.old.exists())
        self.assertEqual(alternate.read_bytes(), payload)

    def test_new_path_owner_after_receipt_commit_is_rechecked_by_shared_consumer(self):
        from unittest.mock import patch

        actual = strm_repo.enqueue_strm_path_cleanup
        row = strm_repo.list_strm_index("guangya:source")[0]
        payload = self.old.read_bytes()

        def acquire_owner(items):
            count = actual(items)
            strm_repo.upsert_strm_index(
                "other-provider",
                "new-owner",
                row["etag"],
                row["size"],
                row["filename"],
                str(self.old),
                row["content_fingerprint"],
            )
            return count

        with patch.object(strm_repo, "enqueue_strm_path_cleanup", side_effect=acquire_owner):
            stats = self.historical()
        self.assertEqual(stats["cleaned"], 0)
        self.assertEqual(self.old.read_bytes(), payload)
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya:source"), [])
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])
