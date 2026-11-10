"""元数据已安装后的完成交接故障不能消耗下载重试并终止恢复。"""

from __future__ import annotations

from typing import Any

from app.clients.guangya import GuangYaClient
from typing import cast

import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import strm as strm_repo
from app.repositories import strm_request_ownership as strm_request_ownership_repo
from app.clients.guangya import GuangYaFile
from app.modules import strm
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
        self.job: dict[str, Any] = {
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
        self.worker._client = cast(GuangYaClient, self.client)
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
        return dict(strm_repo.list_strm_metadata_queue()[0])

    def make_due(self):
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE strm_metadata_queue SET next_attempt_at='2000-01-01 00:00:00'"
            )

    def fail_manual_job(self):
        self.job.update(rel_dir="", target_rel_path=f"{STRM_SUBDIR}/Movie.nfo")
        self.target = self.root / STRM_SUBDIR / "Movie.nfo"
        strm_repo.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        with patch("app.modules.strm.requests.get", side_effect=OSError("isolated download failure")):
            self.worker._process_one()
        self.assertEqual(self.queue_row()["status"], "failed")

    def retry_manually(self):
        return strm.retry_all_strm_failures("source", "metadata", "test", client=cast(GuangYaClient, self.client), runtime_config={
            "sources": strm.plan_strm_sources([{"id": "source", "name": "整理"}]),
            "strm_root": str(self.root), "base_url": "http://play.invalid", "media_server_refresh": True,
        })

    def test_manual_recovery_closes_failed_job_without_requeuing_installed_file(self):
        self.fail_manual_job()
        with patch("app.modules.strm.requests.get", return_value=_Response()) as download:
            result = self.retry_manually()
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertEqual(download.call_count, 1)
        self.assertEqual(self.queue_row()["status"], "completed")
        self.assertEqual(strm_repo.count_strm_metadata_jobs()["failed"], 0)
        self.assertEqual(db.list_strm_failures(status="open"), [])
        self.assertEqual(len(strm_repo.list_strm_refresh_entries()), 1)
        self.assertEqual(strm_repo.claim_due_strm_metadata_jobs(owner="after-repair"), [])

    def test_manual_recovery_keeps_original_refresh_scope_and_request_owner(self):
        from app.repositories.strm_request_ownership import refresh_work_key

        request, _ = download_requests_repo.create_download_request("manual-recovery-owner", "magnet")
        owners = strm_request_ownership_repo.current_strm_request_owners([request])
        media_root = str(self.root / STRM_SUBDIR)
        self.job["refresh_policy"] = {"media_root": media_root, "allow_emby": False, "request_owners": owners}
        self.fail_manual_job()
        with patch("app.modules.strm.requests.get", return_value=_Response()):
            result = self.retry_manually()
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(self.queue_row()["status"], "completed")
        with db.get_conn() as conn:
            work = list(conn.execute("SELECT request_id,work_key FROM strm_request_work WHERE kind='refresh'"))
        # 根下单文件只刷新文件本身，不把人工恢复扩大为全媒体根扫描。
        refresh_target = str(self.target)
        self.assertEqual([(row["request_id"], row["work_key"]) for row in work], [(request, refresh_work_key(refresh_target, False))])
        self.assertIn((refresh_target, False), [(event["path"], event["allow_emby"]) for event in strm_repo.list_strm_refresh_entries()])

    def test_manual_recovery_ack_failure_rolls_back_and_reuses_installed_file(self):
        self.fail_manual_job()
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_manual_refresh BEFORE INSERT ON strm_refresh_outbox BEGIN SELECT RAISE(ABORT,'isolated refresh failure'); END")
        with patch("app.modules.strm.requests.get", return_value=_Response()):
            result = self.retry_manually()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertEqual(self.queue_row()["status"], "failed")
        self.assertEqual(len(db.list_strm_failures(status="open")), 1)
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER reject_manual_refresh")
        with patch("app.modules.strm.requests.get", side_effect=AssertionError("installed metadata must not download again")) as download:
            result = self.retry_manually()
        self.assertEqual(result["resolved"], 1)
        download.assert_not_called()
        self.assertEqual(self.queue_row()["status"], "completed")

    def test_manual_recovery_does_not_ack_cancelled_active_or_newer_snapshot(self):
        self.fail_manual_job()
        original_ack = strm.resolve_strm_failure_with_refresh
        for state in ("cancelled", "running", "queued", "changed", "relocated"):
            with self.subTest(state=state):
                with db.get_conn() as conn:
                    conn.execute("UPDATE strm_metadata_queue SET status='failed',etag='m1'")
                    conn.execute("UPDATE strm_failures SET status='open'")
                def changed_ack(*args, **kwargs):
                    with db.get_conn() as conn:
                        if state == "changed":
                            conn.execute("UPDATE strm_metadata_queue SET etag='m2',revision=revision+1")
                        elif state == "relocated":
                            conn.execute("UPDATE strm_metadata_queue SET refresh_policy_json=?", (json.dumps({"media_root": str(self.root / "old-root")}),))
                        else:
                            conn.execute("UPDATE strm_metadata_queue SET status=?", (state,))
                    return original_ack(*args, **kwargs)
                with patch.object(strm, "resolve_strm_failure_with_refresh", side_effect=changed_ack), patch("app.modules.strm.requests.get", return_value=_Response()):
                    result = self.retry_manually()
                self.assertEqual(result["resolved"], 1)
                self.assertEqual(self.queue_row()["status"], "failed" if state in {"changed", "relocated"} else state)
                self.assertEqual(self.target.read_bytes(), b"metadata")

    def test_extension_rejection_settles_only_its_claimed_snapshot(self):
        for change in ("unchanged", "revision", "lease"):
            with self.subTest(change=change):
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM strm_metadata_queue")
                strm_repo.enqueue_strm_metadata_jobs([{**self.job, "filename": "Movie.jpg", "target_rel_path": "整理/Movie/Movie.jpg"}])
                successor = []
                def settings(key, default="", *, change=change, successor=successor):
                    if key == "STRM_METADATA_EXTS":
                        if change == "revision":
                            strm_repo.enqueue_strm_metadata_jobs([self.job])
                        elif change == "lease":
                            strm_repo.recover_stale_strm_metadata_jobs(force=True, owner=self.worker._owner)
                            successor.extend(strm_repo.claim_due_strm_metadata_jobs(owner="successor"))
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
                self.assertEqual(strm_repo.list_strm_refresh_entries(), [])
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
        strm_repo.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
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
        self.assertEqual(len(strm_repo.list_strm_index("guangya-meta:source")), 1)
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])
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
            str(self.target), {row["path"] for row in strm_repo.list_strm_refresh_entries()}
        )
        self.assertEqual(self.worker._failed_session, 0)
        self.assertEqual(self.worker._completed_session, 1)

    def test_real_download_failure_still_exhausts_budget_and_records_failure(self):
        strm_repo.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
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
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])

    def test_six_ack_failures_keep_prior_attempt_count_without_download_breaker(self):
        strm_repo.enqueue_strm_metadata_jobs([self.job], max_attempts=2)
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
        strm_repo.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER fail_old_failure_ack BEFORE UPDATE OF status ON strm_failures WHEN new.status='resolved' BEGIN SELECT RAISE(ABORT,'isolated-ledger-store'); END"
            )
        with patch("app.modules.strm.requests.get", return_value=_Response()):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertEqual(self.queue_row()["status"], "retry_wait")
        self.assertEqual(self.queue_row()["attempts"], 0)
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])
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
        self.assertEqual(len(strm_repo.list_strm_refresh_entries()), 1)

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
                strm_repo.enqueue_strm_metadata_jobs([self.job], max_attempts=1)

                def interrupt(*_args, interruption=interruption, **_kwargs):
                    if interruption == "revision":
                        strm_repo.enqueue_strm_metadata_jobs(
                            [{**self.job, "etag": "new-version"}]
                        )
                    elif interruption == "cancel":
                        strm_repo.cancel_strm_metadata_job(
                            "source", "meta", reason="source removed"
                        )
                    else:
                        strm_repo.recover_stale_strm_metadata_jobs(
                            force=True, owner=self.worker._owner
                        )
                        self.assertEqual(
                            len(strm_repo.claim_due_strm_metadata_jobs(owner="successor")), 1
                        )
                    raise sqlite3.OperationalError("isolated stale completion")

                with (
                    patch.object(
                        strm_repo, "settle_strm_metadata_job", side_effect=interrupt
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
                self.assertEqual(strm_repo.list_strm_refresh_entries(), [])

    def test_changed_revision_success_does_not_ack_the_new_failure_ledger(self):
        from app.modules import strm_metadata_worker as metadata

        self.record_old_failure()
        strm_repo.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        original = metadata.commit_strm_metadata_job

        def commit_then_revise(*args, **kwargs):
            result = original(*args, **kwargs)
            strm_repo.enqueue_strm_metadata_jobs([{**self.job, "etag": "new-version"}])
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
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])

    def crash_after_install(self, stage="before_ack", *, check_empty_refresh=True):
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
checkpoint={"after_index":"app.modules.strm._delete_owned_file","after_delete":"app.modules.strm.recover_pending_paths","after_refresh":"app.repositories.strm.delete_strm_path_cleanup","before_ack":"app.repositories.strm.settle_strm_metadata_job"}[sys.argv[3]]
with patch("app.modules.strm_metadata_worker.get_bool",return_value=True), patch("app.modules.strm_metadata_worker.get",side_effect=lambda key,default="": {"STRM_ROOT":sys.argv[2],"STRM_METADATA_EXTS":"nfo"}.get(key,default)), patch("app.modules.strm.requests.get",return_value=_Response()), patch(checkpoint,side_effect=lambda *_a,**_k: os._exit(79)):
 worker._process_one()
raise AssertionError("crash checkpoint not reached")
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.database_path), str(self.root), stage],
            timeout=20,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertEqual(self.queue_row()["status"], "running")
        if check_empty_refresh:
            self.assertEqual(strm_repo.list_strm_refresh_entries(), [])

    def queue_move(self, *, enabled=True, allow_emby=True):
        old = self.root / STRM_SUBDIR / "Old" / "Movie.nfo"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"metadata")
        strm_repo.upsert_strm_index(
            "guangya-meta:source", "meta", "m1", 8, old.name,
            str(old), strm._content_fingerprint(old),
        )
        self.target = self.root / STRM_SUBDIR / "New" / "Movie.nfo"
        self.job.update(
            rel_dir="New", target_rel_path=f"{STRM_SUBDIR}/New/Movie.nfo",
            refresh_policy={
                "media_root": str(self.root / STRM_SUBDIR),
                "enabled": enabled, "allow_emby": allow_emby,
            },
        )
        strm_repo.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        return old

    def assert_move_restart(self, stage):
        old = self.queue_move()
        self.crash_after_install(stage, check_empty_refresh=False)
        entries = {row["path"] for row in strm_repo.list_strm_refresh_entries()}
        self.assertTrue(
            str(old.parent) in entries or strm_repo.list_strm_path_cleanup("guangya-meta:source"),
            "确认旧凭据前必须先持久化刷新",
        )
        self.assertEqual(strm_repo.recover_stale_strm_metadata_jobs(force=True), 1)
        with patch("app.modules.strm.requests.get", side_effect=AssertionError("must reuse installed bytes")):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.queue_row()["status"], "completed")
        self.assertFalse(old.exists())
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertEqual(
            {row["path"] for row in strm_repo.list_strm_refresh_entries()},
            {str(old.parent), str(self.target.parent)},
        )
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya-meta:source"), [])
        strm_repo.acknowledge_strm_refresh_paths(strm_repo.list_strm_refresh_entries())
        self.assertFalse(self.worker._process_one())
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])

    def test_move_exit_after_index_recovers_without_scan_or_download(self):
        self.assert_move_restart("after_index")

    def test_move_exit_after_delete_recovers_without_scan_or_download(self):
        self.assert_move_restart("after_delete")

    def test_move_exit_after_refresh_before_receipt_ack_is_replayable(self):
        self.assert_move_restart("after_refresh")

    def test_move_exit_before_job_ack_keeps_old_directory_refresh(self):
        self.assert_move_restart("before_ack")

    def test_normal_move_uses_receipts_once_and_refreshes_both_directories(self):
        old = self.queue_move()
        with patch("app.modules.strm.requests.get", return_value=_Response()) as download:
            self.assertTrue(self.worker._process_one())
        self.assertEqual(download.call_count, 1)
        self.assertEqual(self.queue_row()["status"], "completed")
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya-meta:source"), [])
        self.assertEqual(
            {row["path"] for row in strm_repo.list_strm_refresh_entries()},
            {str(old.parent), str(self.target.parent)},
        )

    def test_move_old_refresh_failure_preserves_receipt_and_download_budget(self):
        old = self.queue_move()
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER fail_old_refresh BEFORE INSERT ON strm_refresh_outbox "
                "WHEN new.path LIKE '%/Old' BEGIN SELECT RAISE(ABORT,'old-refresh-store'); END"
            )
        with patch("app.modules.strm.requests.get", return_value=_Response()) as download:
            self.worker._process_one()
        self.assertEqual(download.call_count, 1)
        self.assertEqual(self.target.read_bytes(), b"metadata")
        self.assertFalse(old.exists())
        self.assertNotEqual(self.queue_row()["status"], "completed")
        self.assertEqual(self.queue_row()["attempts"], 0)
        self.assertEqual(self.worker._failed_session, 0)
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])
        self.assertEqual(len(strm_repo.list_strm_path_cleanup("guangya-meta:source")), 1)
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER fail_old_refresh")
        self.make_due()
        with patch("app.modules.strm.requests.get", side_effect=AssertionError("must not redownload")):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.queue_row()["status"], "completed")
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya-meta:source"), [])
        self.assertEqual(
            {row["path"] for row in strm_repo.list_strm_refresh_entries()},
            {str(old.parent), str(self.target.parent)},
        )

    def test_move_ack_failure_after_receipt_confirmation_keeps_old_refresh(self):
        old = self.queue_move()
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER fail_move_ack BEFORE UPDATE OF status ON strm_metadata_queue "
                "WHEN new.status='completed' BEGIN SELECT RAISE(ABORT,'move-ack-store'); END"
            )
        with patch("app.modules.strm.requests.get", return_value=_Response()):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.queue_row()["status"], "retry_wait")
        self.assertEqual(self.queue_row()["attempts"], 0)
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya-meta:source"), [])
        self.assertEqual(
            {row["path"] for row in strm_repo.list_strm_refresh_entries()}, {str(old.parent)},
        )
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER fail_move_ack")
        self.make_due()
        with patch("app.modules.strm.requests.get", side_effect=AssertionError("must reuse installed bytes")):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.queue_row()["status"], "completed")
        self.assertEqual(
            {row["path"] for row in strm_repo.list_strm_refresh_entries()},
            {str(old.parent), str(self.target.parent)},
        )

    def test_move_stop_before_receipt_handoff_keeps_task_and_receipt(self):
        old = self.queue_move()
        original = strm.recover_pending_paths

        def stop_then_recover(*args, **kwargs):
            self.worker._stop_event.set()
            return original(*args, **kwargs)

        with patch.object(strm, "recover_pending_paths", side_effect=stop_then_recover), patch("app.modules.strm.requests.get", return_value=_Response()):
            self.assertFalse(self.worker._process_one())
        self.assertEqual(self.queue_row()["status"], "retry_wait")
        self.assertEqual(self.queue_row()["attempts"], 0)
        self.assertEqual(len(strm_repo.list_strm_path_cleanup("guangya-meta:source")), 1)
        self.assertEqual(strm_repo.list_strm_refresh_entries(), [])
        self.worker._stop_event.clear()
        with patch("app.modules.strm.requests.get", side_effect=AssertionError("must reuse installed bytes")):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.queue_row()["status"], "completed")
        self.assertEqual(strm_repo.list_strm_path_cleanup("guangya-meta:source"), [])
        self.assertEqual(
            {row["path"] for row in strm_repo.list_strm_refresh_entries()},
            {str(old.parent), str(self.target.parent)},
        )

    def test_moved_receipt_recovery_keeps_frozen_refresh_policy(self):
        for enabled, allow_emby in ((False, False), (True, False)):
            with self.subTest(enabled=enabled, allow_emby=allow_emby):
                old = self.queue_move(enabled=enabled, allow_emby=allow_emby)
                self.crash_after_install("after_index", check_empty_refresh=False)
                self.assertEqual(strm_repo.recover_stale_strm_metadata_jobs(force=True), 1)
                with patch("app.modules.strm.requests.get", side_effect=AssertionError("must reuse installed bytes")):
                    self.assertTrue(self.worker._process_one())
                self.assertEqual(self.queue_row()["status"], "completed")
                entries = strm_repo.list_strm_refresh_entries()
                self.assertEqual(
                    {row["path"] for row in entries},
                    {str(old.parent), str(self.target.parent)} if enabled else set(),
                )
                self.assertTrue(all(not row["allow_emby"] for row in entries))
                self.assertEqual(strm_repo.list_strm_path_cleanup("guangya-meta:source"), [])
                with db.get_conn() as conn:
                    for table in ("strm_metadata_queue", "strm_index", "strm_refresh_outbox"):
                        conn.execute(f"DELETE FROM {table}")
                self.target.unlink()

    def test_moved_receipt_handoff_does_not_ack_new_revision_cancel_or_lease(self):
        original = strm_repo.settle_strm_metadata_job
        for change in ("revision", "cancel", "lease"):
            with self.subTest(change=change):
                old = self.queue_move()

                def change_then_settle(*args, change=change, **kwargs):
                    if change == "revision":
                        strm_repo.enqueue_strm_metadata_jobs([{**self.job, "etag": "new"}])
                    elif change == "cancel":
                        strm_repo.cancel_strm_metadata_job("source", "meta", reason="removed")
                    else:
                        strm_repo.recover_stale_strm_metadata_jobs(force=True, owner=self.worker._owner)
                        self.assertEqual(len(strm_repo.claim_due_strm_metadata_jobs(owner="successor")), 1)
                    return original(*args, **kwargs)

                with patch.object(strm_repo, "settle_strm_metadata_job", side_effect=change_then_settle), patch("app.modules.strm.requests.get", return_value=_Response()):
                    self.assertTrue(self.worker._process_one())
                row = self.queue_row()
                self.assertEqual(row["status"], {"revision": "queued", "cancel": "cancelled", "lease": "running"}[change])
                self.assertEqual(row["attempts"], 0)
                if change == "lease":
                    self.assertEqual(row["lease_owner"], "successor")
                self.assertEqual(
                    {entry["path"] for entry in strm_repo.list_strm_refresh_entries()},
                    {str(old.parent)},
                    "已落盘旧目录变化持久交接，但不能提前确认新目录或继任任务",
                )
                self.assertEqual(strm_repo.list_strm_path_cleanup("guangya-meta:source"), [])
                with db.get_conn() as conn:
                    for table in ("strm_metadata_queue", "strm_index", "strm_path_cleanup", "strm_refresh_outbox"):
                        conn.execute(f"DELETE FROM {table}")
                self.target.unlink()

    def test_move_cancelled_or_reclaimed_before_commit_keeps_old_installation(self):
        for change in ("revision", "cancel", "lease"):
            with self.subTest(change=change):
                old = self.queue_move()

                def prepare_then_change(*args, change=change, **kwargs):
                    result = strm.prepare_strm_metadata_job(*args, **kwargs)
                    if change == "revision":
                        strm_repo.enqueue_strm_metadata_jobs([{**self.job, "etag": "new"}])
                    elif change == "cancel":
                        strm_repo.cancel_strm_metadata_job("source", "meta", reason="removed")
                    else:
                        strm_repo.recover_stale_strm_metadata_jobs(force=True, owner=self.worker._owner)
                        self.assertEqual(len(strm_repo.claim_due_strm_metadata_jobs(owner="successor")), 1)
                    return result

                with patch("app.modules.strm_metadata_worker.prepare_strm_metadata_job", side_effect=prepare_then_change), patch("app.modules.strm.requests.get", return_value=_Response()):
                    self.assertTrue(self.worker._process_one())
                self.assertEqual(old.read_bytes(), b"metadata")
                self.assertFalse(self.target.exists())
                self.assertEqual(strm_repo.list_strm_index("guangya-meta:source")[0]["strm_path"], str(old))
                self.assertEqual(strm_repo.list_strm_path_cleanup("guangya-meta:source"), [])
                self.assertEqual(strm_repo.list_strm_refresh_entries(), [])
                row = self.queue_row()
                self.assertEqual(row["status"], {"revision": "queued", "cancel": "cancelled", "lease": "running"}[change])
                if change == "lease":
                    self.assertEqual(row["lease_owner"], "successor")
                with db.get_conn() as conn:
                    for table in ("strm_metadata_queue", "strm_index"):
                        conn.execute(f"DELETE FROM {table}")

    def test_move_restart_preserves_edited_old_file_and_other_source_owner(self):
        for change in ("edited", "other_owner"):
            with self.subTest(change=change):
                old = self.queue_move()
                self.crash_after_install("after_index", check_empty_refresh=False)
                if change == "edited":
                    old.write_bytes(b"user-edited")
                else:
                    strm_repo.upsert_strm_index(
                        "guangya-meta:other", "owner", "other", 8, old.name,
                        str(old), strm._content_fingerprint(old),
                    )
                self.assertEqual(strm_repo.recover_stale_strm_metadata_jobs(force=True), 1)
                with patch("app.modules.strm.requests.get", side_effect=AssertionError("must reuse installed bytes")):
                    self.assertTrue(self.worker._process_one())
                self.assertEqual(old.read_bytes(), b"user-edited" if change == "edited" else b"metadata")
                self.assertEqual(self.target.read_bytes(), b"metadata")
                self.assertEqual(self.queue_row()["status"], "completed")
                self.assertEqual(
                    len(strm_repo.list_strm_path_cleanup("guangya-meta:source")),
                    int(change == "edited"),
                )
                if change == "other_owner":
                    self.assertEqual(strm_repo.list_strm_index("guangya-meta:other")[0]["strm_path"], str(old))
                self.assertEqual(
                    {entry["path"] for entry in strm_repo.list_strm_refresh_entries()},
                    {str(self.target.parent)},
                )
                with db.get_conn() as conn:
                    for table in ("strm_metadata_queue", "strm_index", "strm_path_cleanup", "strm_refresh_outbox"):
                        conn.execute(f"DELETE FROM {table}")
                self.target.unlink()

    def recover_without_download(self, expected_refresh_count=1):
        self.assertEqual(strm_repo.recover_stale_strm_metadata_jobs(force=True), 1)
        with patch(
            "app.modules.strm.requests.get",
            side_effect=AssertionError("restart must reuse committed bytes"),
        ):
            self.assertTrue(self.worker._process_one())
        self.assertEqual(self.queue_row()["status"], "completed")
        self.assertEqual(db.list_strm_failures(status="open"), [])
        self.assertEqual(len(strm_repo.list_strm_refresh_entries()), expected_refresh_count)

    def assert_refresh_policy_survives_crash(self, *, enabled, allow_emby):
        strm_repo.enqueue_strm_metadata_jobs([{
            **self.job,
            "refresh_policy": {
                "media_root": str(self.root / STRM_SUBDIR),
                "enabled": enabled, "allow_emby": allow_emby,
            },
        }], max_attempts=1)
        self.crash_after_install()
        self.recover_without_download(expected_refresh_count=int(enabled))
        self.assertEqual(
            [entry["allow_emby"] for entry in strm_repo.list_strm_refresh_entries()],
            [allow_emby] if enabled else [],
        )

    def test_real_exit_keeps_refresh_disabled_after_restart(self):
        self.assert_refresh_policy_survives_crash(enabled=False, allow_emby=False)

    def test_real_exit_keeps_emby_disabled_after_restart(self):
        self.assert_refresh_policy_survives_crash(enabled=True, allow_emby=False)

    def test_real_exit_after_install_before_ack_recovers_without_repeating_download(
        self,
    ):
        self.record_old_failure()
        strm_repo.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        self.crash_after_install()
        self.assertEqual(len(db.list_strm_failures(status="open")), 1)
        self.recover_without_download()
        self.assertFalse(self.worker._process_one())

    def test_zip_restores_pending_installation_handoff_and_old_failure_together(self):
        from app.modules import backup
        from tests.test_ten_pass_process_restore_audit import runtime_paths

        self.record_old_failure()
        strm_repo.enqueue_strm_metadata_jobs([self.job], max_attempts=1)
        self.crash_after_install()
        paths = runtime_paths(self.database_path)
        archive = backup.create_backup(paths, reason="metadata-installed-before-ack")
        backup.verify_backup(archive)
        self.recover_without_download()
        strm_repo.acknowledge_strm_refresh_paths(strm_repo.list_strm_refresh_entries())
        backup.restore_backup(paths, archive)
        db.init_db()
        self.assertEqual(self.queue_row()["status"], "running")
        self.assertEqual(len(db.list_strm_failures(status="open")), 1)
        self.recover_without_download()
