"""元数据已安装后的完成交接故障不能消耗下载重试并终止恢复。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.strm import STRM_SUBDIR
from app.modules.strm_metadata_worker import STRMMetadataWorker
from tests.support import isolated_test_database
from tests.test_strm_metadata_queue import _Response, _TreeClient


class StrmMetadataCommitRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.database_path = self.enterContext(isolated_test_database("mediaflux.db"))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.remote = GuangYaFile("meta", "Movie.nfo", False, 8, "m1", "source")
        self.client = _TreeClient({"source": [self.remote]})
        self.job = {
            "source_id": "source",
            "source_name": "整理",
            "file_id": self.remote.file_id,
            "parent_id": self.remote.parent_id,
            "filename": self.remote.name,
            "etag": self.remote.etag,
            "size": self.remote.size,
            "rel_dir": "Movie",
            "target_rel_path": f"{STRM_SUBDIR}/Movie/Movie.nfo",
        }
        self.target = self.root / STRM_SUBDIR / "Movie" / "Movie.nfo"
        self.worker = STRMMetadataWorker()
        self.worker._client = self.client
        self.enterContext(
            patch("app.modules.strm_metadata_worker.get_bool", return_value=True)
        )
        self.enterContext(
            patch(
                "app.modules.strm_metadata_worker.get",
                side_effect=lambda key, default="": {
                    "STRM_ROOT": str(self.root),
                    "STRM_METADATA_EXTS": "nfo",
                }.get(key, default),
            )
        )
        self.enterContext(patch.object(self.worker, "_flush_media_refresh"))

    def queue_row(self):
        return dict(db.list_strm_metadata_queue()[0])

    def make_due(self):
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_metadata_queue SET next_attempt_at='2000-01-01 00:00:00'"
            )

    def test_extension_rejection_settles_only_its_claimed_snapshot(self):
        for change in ("unchanged", "revision", "lease"):
            with self.subTest(change=change):
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM strm_metadata_queue")
                db.enqueue_strm_metadata_jobs([{**self.job, "filename": "Movie.jpg", "target_rel_path": "整理/Movie/Movie.jpg"}])
                successor = []
                def settings(key, default=""):
                    if key == "STRM_METADATA_EXTS":
                        if change == "revision":
                            db.enqueue_strm_metadata_jobs([self.job])
                        elif change == "lease":
                            db.recover_stale_strm_metadata_jobs(force=True, owner=self.worker._owner)
                            successor.extend(db.claim_due_strm_metadata_jobs(owner="successor"))
                        return "nfo"
                    return str(self.root) if key == "STRM_ROOT" else default
                with (
                    patch("app.modules.strm_metadata_worker.get", side_effect=settings),
                    patch("app.modules.strm_metadata_worker.prepare_strm_metadata_job") as prepare,
                ):
                    self.assertTrue(self.worker._process_one())
                prepare.assert_not_called()
                row = self.queue_row()
                self.assertEqual(row["status"], {"unchanged": "cancelled", "revision": "queued", "lease": "running"}[change])
                self.assertEqual(db.list_strm_refresh_entries(), [])
                self.assertEqual(db.list_strm_failures(status="open"), [])
                self.assertEqual(self.worker._completed_session, 0)
                self.assertEqual(self.worker._failed_session, 0)
                self.assertFalse(self.target.exists())
                if change == "unchanged":
                    self.assertIn("扩展名", row["last_error"])
                elif change == "revision":
                    self.assertEqual(row["filename"], "Movie.nfo")
                    self.assertEqual(row["last_error"], "")
                    with patch("app.modules.strm.requests.get", return_value=_Response()):
                        self.assertTrue(self.worker._process_one())
                    self.assertEqual(self.queue_row()["status"], "completed")
                    self.assertEqual(self.target.read_bytes(), b"metadata")
                    self.target.unlink()
                    self.worker._completed_session = 0
                    with db.get_conn() as conn:
                        conn.execute("DELETE FROM strm_index")
                        conn.execute("DELETE FROM strm_refresh_outbox")
                else:
                    self.assertEqual(row["lease_owner"], "successor")
                    self.assertEqual(row["lease_generation"], successor[0]["lease_generation"])

    def test_completed_file_at_attempt_limit_retries_handoff_without_redownloading(
        self,
    ):
        db.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER fail_metadata_refresh BEFORE INSERT ON strm_refresh_outbox BEGIN SELECT RAISE(ABORT,'isolated-refresh-store'); END"
            )
        with patch(
            "app.modules.strm.requests.get", return_value=_Response()
        ) as download:
            self.assertTrue(self.worker._process_one())
        self.assertEqual(download.call_count, 1)
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertEqual(len(db.list_strm_index("guangya-meta:source")), 1)
        self.assertEqual(db.list_strm_refresh_entries(), [])
        self.assertEqual(self.queue_row()["status"], "retry_wait")
        self.assertEqual(self.queue_row()["attempts"], 0)
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM strm_failures").fetchone()[0], 0
            )
            conn.execute("DROP TRIGGER fail_metadata_refresh")
        self.make_due()
        with patch(
            "app.modules.strm.requests.get",
            side_effect=AssertionError("installed metadata must not download again"),
        ) as download:
            self.assertTrue(self.worker._process_one())
        download.assert_not_called()
        self.assertEqual(self.queue_row()["status"], "completed")
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertIn(
            str(self.target), {row["path"] for row in db.list_strm_refresh_entries()}
        )
        self.assertEqual(self.worker._failed_session, 0)
        self.assertEqual(self.worker._completed_session, 1)

    def test_real_download_failure_still_exhausts_budget_and_records_failure(self):
        db.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        with patch(
            "app.modules.strm.requests.get",
            side_effect=OSError("isolated download failure"),
        ):
            self.assertTrue(self.worker._process_one())
        self.assertFalse(self.target.exists())
        self.assertEqual(self.queue_row()["status"], "failed")
        self.assertEqual(self.queue_row()["attempts"], 1)
        self.assertEqual(len(db.list_strm_failures(status="open")), 1)
        self.assertEqual(self.worker._failed_session, 1)
        self.assertEqual(db.list_strm_refresh_entries(), [])

    def test_six_ack_failures_keep_prior_attempt_count_without_download_breaker(self):
        db.enqueue_strm_metadata_jobs([self.job], max_attempts=2)
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_metadata_queue SET attempts=1")
            conn.execute(
                "CREATE TRIGGER fail_repeated_ack BEFORE UPDATE OF status ON strm_metadata_queue WHEN new.status='completed' BEGIN SELECT RAISE(ABORT,'isolated-ack-store'); END"
            )
        with patch(
            "app.modules.strm.requests.get", return_value=_Response()
        ) as download:
            for _ in range(6):
                self.make_due()
                self.assertTrue(self.worker._process_one())
                self.assertEqual(self.queue_row()["status"], "retry_wait")
                self.assertEqual(self.queue_row()["attempts"], 1)
            self.assertEqual(download.call_count, 1)
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertEqual(self.worker._consecutive_failures, 0)
        self.assertEqual(self.worker._failed_session, 0)
        self.assertEqual(self.worker._breaker_until, 0)
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER fail_repeated_ack")
        self.make_due()
        with patch(
            "app.modules.strm.requests.get",
            side_effect=AssertionError("must reuse installation"),
        ):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.queue_row()["status"], "completed")
        self.assertEqual(self.queue_row()["attempts"], 0)

    def record_old_failure(self):
        return db.record_strm_failure(
            source_id="source",
            source_name="整理",
            file_id="meta",
            parent_id="source",
            filename="Movie.nfo",
            action="metadata",
            rel_dir="Movie",
            target_rel_path=self.job["target_rel_path"],
            error="previous transfer error",
        )

    def test_failure_ledger_ack_refresh_and_completion_share_one_transaction(self):
        self.record_old_failure()
        db.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER fail_old_failure_ack BEFORE UPDATE OF status ON strm_failures WHEN new.status='resolved' BEGIN SELECT RAISE(ABORT,'isolated-ledger-store'); END"
            )
        with patch("app.modules.strm.requests.get", return_value=_Response()):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertEqual(self.queue_row()["status"], "retry_wait")
        self.assertEqual(self.queue_row()["attempts"], 0)
        self.assertEqual(db.list_strm_refresh_entries(), [])
        self.assertEqual(len(db.list_strm_failures(status="open")), 1)
        self.assertEqual(db.list_strm_failures(status="resolved"), [])
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER fail_old_failure_ack")
        self.make_due()
        with patch(
            "app.modules.strm.requests.get",
            side_effect=AssertionError("must reuse installation"),
        ):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.queue_row()["status"], "completed")
        self.assertEqual(db.list_strm_failures(status="open"), [])
        self.assertEqual(len(db.list_strm_failures(status="resolved")), 1)
        self.assertEqual(len(db.list_strm_refresh_entries()), 1)

    def test_superseded_postcommit_error_never_changes_new_revision_or_owner(self):
        import sqlite3

        for interruption in ("revision", "cancel", "lease"):
            with self.subTest(interruption=interruption):
                with db.get_conn() as conn:
                    for table in (
                        "strm_metadata_queue",
                        "strm_index",
                        "strm_failures",
                        "strm_refresh_outbox",
                    ):
                        conn.execute(f"DELETE FROM {table}")
                self.target.unlink(missing_ok=True)
                self.record_old_failure()
                db.enqueue_strm_metadata_jobs([self.job], max_attempts=1)

                def interrupt(*_args, **_kwargs):
                    if interruption == "revision":
                        db.enqueue_strm_metadata_jobs(
                            [{**self.job, "etag": "new-version"}]
                        )
                    elif interruption == "cancel":
                        db.cancel_strm_metadata_job(
                            "source", "meta", reason="source removed"
                        )
                    else:
                        db.recover_stale_strm_metadata_jobs(
                            force=True, owner=self.worker._owner
                        )
                        self.assertEqual(
                            len(db.claim_due_strm_metadata_jobs(owner="successor")), 1
                        )
                    raise sqlite3.OperationalError("isolated stale completion")

                with (
                    patch.object(
                        db, "settle_strm_metadata_job", side_effect=interrupt
                    ),
                    patch("app.modules.strm.requests.get", return_value=_Response()),
                ):
                    self.assertTrue(self.worker._process_one())
                row = self.queue_row()
                self.assertEqual(
                    row["status"],
                    {"revision": "queued", "cancel": "cancelled", "lease": "running"}[
                        interruption
                    ],
                )
                self.assertEqual(row["attempts"], 0)
                if interruption == "lease":
                    self.assertEqual(row["lease_owner"], "successor")
                self.assertEqual(self.worker._failed_session, 0)
                self.assertEqual(len(db.list_strm_failures(status="open")), 1)
                self.assertEqual(db.list_strm_refresh_entries(), [])

    def test_changed_revision_success_does_not_ack_the_new_failure_ledger(self):
        from app.modules import strm_metadata_worker as metadata

        self.record_old_failure()
        db.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        original = metadata.commit_strm_metadata_job

        def commit_then_revise(*args, **kwargs):
            result = original(*args, **kwargs)
            db.enqueue_strm_metadata_jobs([{**self.job, "etag": "new-version"}])
            return result

        with (
            patch.object(
                metadata, "commit_strm_metadata_job", side_effect=commit_then_revise
            ),
            patch("app.modules.strm.requests.get", return_value=_Response()),
        ):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.queue_row()["status"], "queued")
        self.assertEqual(self.queue_row()["etag"], "new-version")
        self.assertEqual(len(db.list_strm_failures(status="open")), 1)
        self.assertEqual(db.list_strm_refresh_entries(), [])

    def crash_after_install(self):
        import subprocess
        import sys

        script = r"""
import tests
import os, socket, sys
from pathlib import Path
from unittest.mock import patch
from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.strm_metadata_worker import STRMMetadataWorker
from tests.test_strm_metadata_queue import _Response, _TreeClient
socket.socket.connect=lambda *_a,**_k: (_ for _ in ()).throw(AssertionError("external connection forbidden"))
db.configure_database(Path(sys.argv[1]),test_mode=True)
worker=STRMMetadataWorker()
worker._client=_TreeClient({"source":[GuangYaFile("meta","Movie.nfo",False,8,"m1","source")]})
with patch("app.modules.strm_metadata_worker.get_bool",return_value=True), patch("app.modules.strm_metadata_worker.get",side_effect=lambda key,default="": {"STRM_ROOT":sys.argv[2],"STRM_METADATA_EXTS":"nfo"}.get(key,default)), patch("app.modules.strm.requests.get",return_value=_Response()), patch.object(db,"settle_strm_metadata_job",side_effect=lambda *_a,**_k: os._exit(79)):
 worker._process_one()
raise AssertionError("crash checkpoint not reached")
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.database_path), str(self.root)],
            timeout=20,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertEqual(self.queue_row()["status"], "running")
        self.assertEqual(db.list_strm_refresh_entries(), [])

    def recover_without_download(self):
        self.assertEqual(db.recover_stale_strm_metadata_jobs(force=True), 1)
        with patch(
            "app.modules.strm.requests.get",
            side_effect=AssertionError("restart must reuse committed bytes"),
        ):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.queue_row()["status"], "completed")
        self.assertEqual(db.list_strm_failures(status="open"), [])
        self.assertEqual(len(db.list_strm_refresh_entries()), 1)

    def test_real_exit_after_install_before_ack_recovers_without_repeating_download(
        self,
    ):
        self.record_old_failure()
        db.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        self.crash_after_install()
        self.assertEqual(len(db.list_strm_failures(status="open")), 1)
        self.recover_without_download()
        self.assertFalse(self.worker._process_one())

    def test_zip_restores_pending_installation_handoff_and_old_failure_together(self):
        from app.modules import backup
        from tests.test_ten_pass_process_restore_audit import runtime_paths

        self.record_old_failure()
        db.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        self.crash_after_install()
        paths = runtime_paths(self.database_path)
        archive = backup.create_backup(paths, reason="metadata-installed-before-ack")
        backup.verify_backup(archive)
        self.recover_without_download()
        db.acknowledge_strm_refresh_paths(db.list_strm_refresh_entries())
        backup.restore_backup(paths, archive)
        db.init_db()
        self.assertEqual(self.queue_row()["status"], "running")
        self.assertEqual(len(db.list_strm_failures(status="open")), 1)
        self.recover_without_download()
