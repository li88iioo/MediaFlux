"""启动与存活期恢复共享策略，已确认后端事实不得被恢复器改写。"""

from __future__ import annotations

from tests.support import assert_present

import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.clients.guangya import GuangYaFile
from app.modules import download_dispatcher
from app.modules.download_tracker import DownloadTracker
from app.modules.organize_rules import OrganizeRules
from tests.support import isolated_test_database


class DownloadRecoveryUnificationTests(unittest.TestCase):
    def setUp(self):
        self.path = self.enterContext(isolated_test_database("mediaflux.db"))

    def share(self, key, *, status="submitting", backend="submitting", stale=True):
        request, _ = download_requests_repo.create_share_transfer_request(key, title=key, origin="web")
        download_requests_repo.update_download_request(
            request,
            status=status,
            gy_status=backend,
            gy_task_id="acknowledged-task" if backend not in ("", "submitting") else "",
            gy_task_ids='["acknowledged-task"]'
            if backend not in ("", "submitting")
            else "[]",
            error="保留的诊断",
            gy_target_dir="preserved-target",
        )
        if stale:
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE download_requests SET gy_submitting_at='2000-01-01 00:00:00' WHERE id=?",
                    (request,),
                )
        return request

    def test_boot_preserves_confirmed_share_backend_facts(self):
        requests = {
            backend: self.share(backend, backend=backend)
            for backend in ("submitted", "downloading", "completed", "failed")
        }
        before = {
            key: dict(assert_present(download_requests_repo.get_download_request(rid))) for key, rid in requests.items()
        }
        db.init_db()
        for backend, rid in requests.items():
            with self.subTest(backend=backend):
                row = download_requests_repo.get_download_request(rid)
                assert row is not None
                self.assertEqual(row["status"], "manual_review")
                for field in (
                    "gy_status",
                    "gy_task_id",
                    "gy_task_ids",
                    "gy_target_dir",
                ):
                    assert row is not None
                    self.assertEqual(row[field], before[backend][field], field)
                self.assertIn("保留的诊断", row["error"])

    def test_live_recovery_covers_stale_legacy_shares_but_not_recent_or_terminal(self):
        expected = [
            self.share(f"old-{status}-{backend}", status=status, backend=backend)
            for status in ("pending", "submitting")
            for backend in ("", "submitting", "completed", "failed")
        ]
        controls = [self.share("recent", stale=False)]
        controls += [
            self.share(f"terminal-{status}", status=status, backend="completed")
            for status in ("completed", "failed", "cancelled", "resubmitted")
        ]
        before = {rid: dict(assert_present(download_requests_repo.get_download_request(rid))) for rid in controls}
        self.assertEqual(download_requests_repo.recover_stale_submitting_download_requests(), len(expected))
        for rid in expected:
            get_download_request_result = download_requests_repo.get_download_request(rid)
            assert get_download_request_result is not None
            self.assertEqual(get_download_request_result["status"], "manual_review")
        for rid in controls:
            get_download_request_result_5 = download_requests_repo.get_download_request(rid)
            assert get_download_request_result_5 is not None
            self.assertEqual(dict(get_download_request_result_5), before[rid])
        self.assertEqual(download_requests_repo.recover_stale_submitting_download_requests(), 0)

    def regular(self, key="regular"):
        rid, _ = download_requests_repo.create_download_request(key, "magnet")
        self.assertTrue(download_requests_repo.claim_download_request(rid, "both"))
        download_requests_repo.update_download_request(rid, qb_status="completed", qb_task_id="known-qb")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE download_requests SET gy_submitting_at='2000-01-01 00:00:00' WHERE id=?",
                (rid,),
            )
        return rid

    def test_boot_and_live_project_the_same_mixed_history_without_late_reactivation(
        self,
    ):
        snapshots = []
        fields = (
            "status",
            "qb_status",
            "gy_status",
            "qb_task_id",
            "gy_task_id",
            "gy_task_ids",
            "error",
        )
        for mode in ("boot", "live"):
            with self.subTest(mode=mode), isolated_test_database():
                ids = [self.regular()]
                ids += [
                    self.share(f"share-{value}", backend=value)
                    for value in ("", "submitting", "submitted", "completed", "failed")
                ]
                ordinary, _ = download_requests_repo.create_download_request("untouched", "magnet")
                if mode == "boot":
                    db.init_db()
                else:
                    self.assertEqual(
                        download_requests_repo.recover_stale_submitting_download_requests(), len(ids)
                    )
                snapshots.append(
                    [
                        tuple(assert_present(download_requests_repo.get_download_request(rid))[key] for key in fields)
                        for rid in ids
                    ]
                )
                self.assertEqual(
                    download_requests_repo.count_download_requests_requiring_attention(), len(ids)
                )
                get_download_request_result_2 = download_requests_repo.get_download_request(ordinary)
                assert get_download_request_result_2 is not None
                self.assertEqual(get_download_request_result_2["status"], "pending")
                before = [dict(assert_present(download_requests_repo.get_download_request(rid))) for rid in ids]
                for rid in ids:
                    self.assertIsNone(
                        download_requests_repo.finalize_download_request_submission(
                            rid,
                            ("guangya",),
                            gy_status="completed",
                            error="late-success",
                        )
                    )
                self.assertEqual(
                    [dict(assert_present(download_requests_repo.get_download_request(rid))) for rid in ids], before
                )
                db.init_db()
                self.assertEqual(
                    [dict(assert_present(download_requests_repo.get_download_request(rid))) for rid in ids], before
                )
                self.assertEqual(download_requests_repo.recover_stale_submitting_download_requests(), 0)
        self.assertEqual(snapshots[0], snapshots[1])

    def test_live_empty_timeout_keeps_recent_submission_active(self):
        from typing import Any, cast

        rid = self.share("recent-default-timeout", stale=False)
        get_download_request_result_6 = download_requests_repo.get_download_request(rid)
        assert get_download_request_result_6 is not None
        before = dict(get_download_request_result_6)
        for value in (None, 0, 15):
            with self.subTest(value=value):
                self.assertEqual(
                    download_requests_repo.recover_stale_submitting_download_requests(cast(Any, value)), 0
                )
                get_download_request_result_7 = download_requests_repo.get_download_request(rid)
                assert get_download_request_result_7 is not None
                self.assertEqual(dict(get_download_request_result_7), before)

    def test_second_family_sql_failure_rolls_back_all_request_facts(self):
        import sqlite3

        ids = [self.regular(), self.share("share-fault", backend="completed")]
        before = [dict(assert_present(download_requests_repo.get_download_request(rid))) for rid in ids]
        with db.get_conn() as conn:
            conn.execute(
                f"CREATE TRIGGER fail_share_recovery BEFORE UPDATE ON download_requests WHEN NEW.id={ids[-1]} AND NEW.status='manual_review' BEGIN SELECT RAISE(ABORT,'recovery-write-fault'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "recovery-write-fault"):
            download_requests_repo.recover_stale_submitting_download_requests()
        self.assertEqual([dict(assert_present(download_requests_repo.get_download_request(rid))) for rid in ids], before)
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER fail_share_recovery")
        self.assertEqual(download_requests_repo.recover_stale_submitting_download_requests(), 2)
        get_download_request_result_3 = download_requests_repo.get_download_request(ids[-1])
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3["gy_status"], "completed")
        self.assertEqual(download_requests_repo.recover_stale_submitting_download_requests(), 0)

    def test_real_exit_mid_recovery_and_zip_restore_preserve_backend_facts(self):
        import subprocess
        import sys
        from app.modules import backup
        from tests.test_ten_pass_process_restore_audit import runtime_paths

        ids = [self.regular(), self.share("share-process", backend="completed")]
        before = [dict(assert_present(download_requests_repo.get_download_request(rid))) for rid in ids]
        script = r"""
import tests
import os, sys, socket
from contextlib import contextmanager
from unittest.mock import patch
from app import database as db
from app.repositories import download_requests as download_requests_repo
socket.socket.connect = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network forbidden"))
db.configure_database(sys.argv[1], test_mode=True)
actual = db.get_conn
count = 0
def interrupt(sql):
 global count
 if sql.startswith("UPDATE download_requests SET status='manual_review'"):
  count += 1
  if count == 2: os._exit(47)
@contextmanager
def traced():
 with actual() as conn:
  conn.set_trace_callback(interrupt)
  yield conn
with patch.object(db, "get_conn", traced):
 download_requests_repo.recover_stale_submitting_download_requests()
raise AssertionError("recovery window not reached")
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(self.path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(child.returncode, 47, child.stderr)
        self.assertEqual([dict(assert_present(download_requests_repo.get_download_request(rid))) for rid in ids], before)
        paths = runtime_paths(self.path)
        archive = backup.create_backup(paths, reason="interrupted-recovery")
        backup.verify_backup(archive)
        fields = (
            "status",
            "qb_status",
            "gy_status",
            "qb_task_id",
            "gy_task_id",
            "gy_task_ids",
            "error",
        )
        db.init_db()
        expected = [
            tuple(assert_present(download_requests_repo.get_download_request(rid))[key] for key in fields) for rid in ids
        ]
        get_download_request_result_4 = download_requests_repo.get_download_request(ids[-1])
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4["gy_status"], "completed")
        backup.restore_backup(paths, archive)
        db.init_db()
        self.assertEqual(
            [tuple(assert_present(download_requests_repo.get_download_request(rid))[key] for key in fields) for rid in ids],
            expected,
        )
        self.assertEqual(download_requests_repo.recover_stale_submitting_download_requests(), 0)


class DownloadOrganizeHandoffRecoveryTests(unittest.TestCase):
    def setUp(self):
        for owner, name in (
            (socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "getaddrinfo"),
        ):
            self.enterContext(patch.object(owner, name, side_effect=AssertionError("禁止外联")))
        self.enterContext(isolated_test_database("download-handoff.db"))
        self.serial = 0

    def completed_request(self, **fields):
        self.serial += 1
        request_id, _ = download_requests_repo.create_download_request(f"handoff-{self.serial}", "magnet")
        download_requests_repo.update_download_request(
            request_id, targets="guangya", status="completed", gy_status="completed",
            gy_task_id="cloud-task", gy_target_dir="stage", gy_isolated=1,
            gy_staging_parent_dir="parent", gy_staging_name="MF-Audit",
        )
        if fields:
            download_requests_repo.update_download_request(request_id, **fields)
        self.assertTrue(download_requests_repo.claim_download_request_organize(request_id))
        return request_id

    def test_boot_requeues_claimed_handoff_and_tracker_starts_once(self):
        request_id = download_dispatcher.create_request(
            download_dispatcher.DownloadInput(
                "magnet", "Audit", "magnet:?xt=urn:btih:" + "a" * 40,
            ), "", "", origin="test",
        )["id"]
        with patch.object(download_dispatcher, "_submit_guangya", return_value={
            "ok": True, "task_id": "cloud-task", "task_ids": ["cloud-task"], "selected_count": 1,
            "decision": {"target_dir_id": "stage", "target_dir_name": "MF-Audit"},
            "staging": {"isolated": True, "parent_id": "parent", "name": "MF-Audit"},
        }) as backend:
            self.assertTrue(download_dispatcher.dispatch_request(request_id, "guangya")["ok"])
            backend.assert_called_once()

        tracker = DownloadTracker()
        client = SimpleNamespace(
            logged_in=True,
            file_info=lambda _id: GuangYaFile("stage", "MF-Audit", True, parent_id="parent"),
            list_dir=lambda _id: [GuangYaFile("video", "Audit.mkv", False, size=1000, parent_id="stage")],
            close=lambda: None,
        )
        manager = SimpleNamespace(start=Mock(side_effect=SystemExit("交接窗口进程中断")))
        with (
            patch("app.modules.download_tracker.GuangYaClient", return_value=client),
            patch.object(DownloadTracker, "_publish_lifecycle", return_value=True),
            patch("app.modules.download_tracker.get", side_effect=lambda key, default="":
                  "dest" if key == "GY_ORGANIZE_TARGET_DIR" else default),
            patch("app.modules.download_tracker.get_organize_manager", return_value=manager),
            patch.object(OrganizeRules, "from_config", return_value=OrganizeRules(target_dir_id="dest")),
        ):
            tracker._update_request(
                download_requests_repo.get_download_request(request_id), [], [{"id": "cloud-task", "status": 2}],
            )
            self.assertEqual(assert_present(download_requests_repo.get_download_request(request_id))["notification_delivery_status"], "sent")
            download_requests_repo.update_download_request(request_id, organize_next_retry_at=None, organize_attempts=2)
            with self.assertRaises(SystemExit):
                tracker._update_request(download_requests_repo.get_download_request(request_id), [], [])
            manager.start.assert_called_once()
            before = dict(assert_present(download_requests_repo.get_download_request(request_id)))
            self.assertEqual((before["organize_started"], before["organize_status"]), (1, "starting"))

            db.init_db()
            recovered = dict(assert_present(download_requests_repo.get_download_request(request_id)))
            self.assertEqual((recovered["organize_started"], recovered["organize_status"]), (0, "queued"))
            self.assertIsNone(recovered["organize_next_retry_at"])
            for field in ("status", "gy_status", "gy_task_id", "gy_target_dir", "gy_isolated",
                          "gy_staging_parent_dir", "gy_staging_name", "gy_settle_snapshot", "organize_attempts"):
                self.assertEqual(recovered[field], before[field], field)
            self.assertIn(request_id, [row["id"] for row in download_requests_repo.list_active_download_requests()])
            self.assertEqual(download_requests_repo.count_download_requests_requiring_attention(request_id), 0)
            db.init_db()
            self.assertEqual(dict(assert_present(download_requests_repo.get_download_request(request_id))), recovered)

            def start(*_args, **_kwargs):
                row = assert_present(download_requests_repo.get_download_request(request_id))
                self.assertEqual((row["organize_started"], row["organize_status"]), (1, "starting"))
                download_requests_repo.update_download_request(
                    request_id, organize_status="running", organize_task_id="resumed-task",
                )
                return {"ok": True}

            manager.start.reset_mock()
            manager.start.side_effect = start
            for _ in range(2):
                tracker._update_request(download_requests_repo.get_download_request(request_id), [], [])
            manager.start.assert_called_once()
            self.assertEqual(manager.start.call_args.kwargs["download_request_ids"], [request_id])

    def test_boot_keeps_running_interruption_for_review_without_replay(self):
        request_id = self.completed_request()
        download_requests_repo.update_download_request(request_id, organize_status="running", organize_task_id="existing-task")
        db.init_db()
        row = dict(assert_present(download_requests_repo.get_download_request(request_id)))
        self.assertEqual((row["organize_started"], row["organize_status"]), (-1, "failed"))
        self.assertEqual(row["organize_task_id"], "existing-task")
        self.assertIn("需人工核验", row["organize_error"])
        self.assertEqual(download_requests_repo.count_download_requests_requiring_attention(request_id), 1)
        self.assertNotIn(request_id, [item["id"] for item in download_requests_repo.list_active_download_requests()])
        self.assertFalse(download_requests_repo.claim_download_request_organize(request_id))
        db.init_db()
        self.assertEqual(dict(assert_present(download_requests_repo.get_download_request(request_id))), row)

    def test_boot_does_not_requeue_a_handoff_that_lost_admission(self):
        for fields in (
            {"status": "cancelled"},
            {"status": "resubmitted", "qb_status": "completed"},
            {"attention_cleared_at": "2026-01-01 00:00:00"},
            {"gy_status": "failed"},
            {"organize_started": -1},
            {"organize_status": "requires_manual"},
        ):
            with self.subTest(fields=fields):
                request_id = self.completed_request()
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE download_requests SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                        (*fields.values(), request_id),
                    )
                before = dict(assert_present(download_requests_repo.get_download_request(request_id)))
                db.init_db()
                self.assertEqual(dict(assert_present(download_requests_repo.get_download_request(request_id))), before)
                self.assertFalse(download_requests_repo.claim_download_request_organize(request_id))

    def test_boot_preserves_cloud_handoff_when_only_qb_was_resubmitted(self):
        request_id = self.completed_request(targets="both", status="resubmitted", qb_status="resubmitted")
        db.init_db()
        row = assert_present(download_requests_repo.get_download_request(request_id))
        self.assertEqual((row["status"], row["qb_status"]), ("resubmitted", "resubmitted"))
        self.assertEqual((row["organize_started"], row["organize_status"]), (0, "queued"))
        self.assertIn(request_id, [item["id"] for item in download_requests_repo.list_active_download_requests()])
        self.assertTrue(download_requests_repo.claim_download_request_organize(request_id))
        self.assertFalse(download_requests_repo.claim_download_request_organize(request_id))
