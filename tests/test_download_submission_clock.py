"""后端提交时钟不被另一后端的进度、通知或暂存目录更新续期。"""
from __future__ import annotations

import socket
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from tests.support import assert_present, isolated_test_database
from app import database as db
from app import database_migrations
from app.modules import download_dispatcher as dispatcher
from app.modules.download_tracker import DownloadTracker
from app.repositories.download_requests import request_download_notification_refresh


class DownloadSubmissionClockTests(unittest.TestCase):
    def setUp(self):
        for owner, name in (
            (socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "getaddrinfo"),
        ):
            self.enterContext(patch.object(owner, name, side_effect=AssertionError("禁止外联")))
        self.path = self.enterContext(isolated_test_database("download-clock.db"))
        self.current = datetime(2026, 1, 1, 12)
        self.serial = 0
        sql_clock = sqlite3.connect(":memory:")
        self.addCleanup(sql_clock.close)
        real_get_conn = db.get_conn

        def datetime_at(*args):
            values = list(args)
            if values and values[0] == "now":
                values[0] = self.stamp()
                values = [value for value in values if value != "localtime"]
            query = "SELECT datetime(" + ",".join("?" for _ in values) + ")"
            return sql_clock.execute(query, values).fetchone()[0]

        @contextmanager
        def get_conn_at():
            with real_get_conn() as conn:
                conn.create_function("datetime", -1, datetime_at)
                yield conn

        self.enterContext(patch.object(db, "get_conn", get_conn_at))
        self.enterContext(patch.object(db, "now", side_effect=self.stamp))

    def stamp(self):
        return self.current.strftime("%Y-%m-%d %H:%M:%S")

    def advance(self, minutes):
        self.current += timedelta(minutes=minutes)

    def request(self, *, initial_targets=""):
        self.serial += 1
        result = dispatcher.create_request(
            dispatcher.DownloadInput("magnet", "Audit", f"magnet:?xt=urn:btih:{self.serial:040x}"),
            "", "", origin="test",
            initial_targets=initial_targets,
        )
        return int(result["id"])

    def row(self, request_id):
        return assert_present(db.get_download_request(request_id))

    def test_peer_tracker_cannot_extend_failed_receipt_persistence_timeout(self):
        request_id = self.request()
        with patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "a" * 40}):
            self.assertTrue(dispatcher.dispatch_request(request_id, "qb")["ok"])
        with (
            patch.object(dispatcher, "_submit_guangya", return_value={"ok": True, "task_id": "accepted-cloud"}) as backend,
            patch.object(dispatcher, "_finalize_submission_state", side_effect=sqlite3.OperationalError("temporary lock")),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                dispatcher.dispatch_missing_targets(request_id, "guangya")
            backend.assert_called_once()
        control = self.request(initial_targets="guangya")
        tracker = DownloadTracker()
        task = SimpleNamespace(hash="a" * 40, name="Audit", progress=0.5, state="downloading")
        with (
            patch.object(tracker, "_run_staging_reconciliation_if_due", return_value=0),
            patch.object(tracker, "_run_torrent_data_cleanup_if_due", return_value=0),
            patch.object(tracker, "_qb_tasks", return_value=(True, [task])),
            patch.object(tracker, "_gy_tasks", side_effect=AssertionError("未取得云端任务 ID")),
            patch.object(tracker, "_notify_completion"),
        ):
            for _ in range(32):
                self.advance(1)
                tracker.run_once()
        self.assertEqual(self.row(control)["gy_status"], "manual_review")
        self.assertEqual(self.row(request_id)["gy_status"], "manual_review")
        self.assertEqual(self.row(request_id)["qb_status"], "downloading")

    def test_claim_starts_each_clock_and_duplicates_do_not_renew_it(self):
        request_id = self.request()
        self.assertIsNone(self.row(request_id)["qb_submitting_at"])
        self.assertIsNone(self.row(request_id)["gy_submitting_at"])
        self.advance(20)  # 等待用户确认不计入后端提交租约。
        claimed_at = self.stamp()
        self.assertTrue(db.claim_download_request(request_id, "qb"))
        self.advance(5)
        self.assertFalse(db.claim_download_request(request_id, "both"))
        self.assertEqual(db.claim_download_request_targets(request_id, "qb"), ())
        self.assertEqual(self.row(request_id)["qb_submitting_at"], claimed_at)
        self.assertIsNone(self.row(request_id)["gy_submitting_at"])
        self.assertEqual(db.recover_stale_submitting_download_requests(), 0)
        atomic = self.request(initial_targets="both")
        self.assertEqual((self.row(atomic)["qb_submitting_at"], self.row(atomic)["gy_submitting_at"]),
                         (self.stamp(), self.stamp()))

    def test_expired_backend_does_not_expire_fresh_peer_or_reject_its_receipt(self):
        for first, peer, prefix, peer_prefix in (
            ("qb", "guangya", "qb", "gy"), ("guangya", "qb", "gy", "qb"),
        ):
            with self.subTest(first=first):
                request_id = self.request(initial_targets=first)
                first_at = self.stamp()
                self.advance(10)
                peer_at = self.stamp()
                self.assertEqual(db.claim_download_request_targets(request_id, peer), (peer,))
                self.assertEqual(self.row(request_id)[f"{prefix}_submitting_at"], first_at)
                self.assertEqual(self.row(request_id)[f"{peer_prefix}_submitting_at"], peer_at)
                self.advance(5)
                self.assertEqual(db.recover_stale_submitting_download_requests(), 0)
                self.advance(1)
                self.assertEqual(db.recover_stale_submitting_download_requests(), 1)
                self.assertEqual(self.row(request_id)[f"{prefix}_status"], "manual_review")
                self.assertEqual(self.row(request_id)[f"{peer_prefix}_status"], "submitting")
                self.assertEqual(db.recover_stale_submitting_download_requests(), 0)
                self.assertEqual(db.finalize_download_request_submission(
                    request_id, (peer,), **{f"{peer_prefix}_status": "submitted"},
                ), "manual_review")
                self.assertIsNone(db.finalize_download_request_submission(
                    request_id, (first,), **{f"{prefix}_status": "submitted"},
                ))
                self.assertEqual(self.row(request_id)[f"{peer_prefix}_submitting_at"], peer_at)

    def test_staging_progress_and_notification_updates_preserve_submission_clock(self):
        request_id = self.request(initial_targets="both")
        started_at = self.stamp()
        self.advance(10)
        self.assertTrue(db.bind_download_request_guangya_staging(
            request_id, staging_id="stage", parent_id="parent", staging_name="MF-Audit", target_name="Audit",
        ))
        db.update_download_request(request_id, qb_status="downloading")
        request_download_notification_refresh(request_id)
        self.assertEqual(self.row(request_id)["updated_at"], self.stamp())
        self.assertEqual(self.row(request_id)["gy_submitting_at"], started_at)
        self.advance(6)
        self.assertEqual(db.recover_stale_submitting_download_requests(), 1)
        row = self.row(request_id)
        self.assertEqual((row["qb_status"], row["gy_status"]), ("downloading", "manual_review"))
        self.assertEqual((row["gy_target_dir"], row["gy_isolated"]), ("stage", 1))

    def test_in_place_retry_renews_only_selected_clock(self):
        request_id = self.request(initial_targets="both")
        first_at = self.stamp()
        db.finalize_download_request_submission(
            request_id, ("qb", "guangya"), qb_status="submitted", gy_status="failed",
        )
        self.advance(10)

        def submit(row, **_kwargs):
            current = self.row(row["id"])
            self.assertEqual((current["qb_submitting_at"], current["gy_submitting_at"]),
                             (first_at, self.stamp()))
            return {"ok": True, "task_id": "cloud-retry"}

        with (
            patch.object(dispatcher, "get", return_value="http://qb.invalid"),
            patch.object(dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True)),
            patch.object(dispatcher, "_submit_guangya", side_effect=submit) as backend,
            patch.object(dispatcher, "_submit_qb") as peer,
        ):
            result = dispatcher.resubmit_download_request(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["request_id"], request_id)
        backend.assert_called_once()
        peer.assert_not_called()
        self.assertEqual(self.row(request_id)["gy_submitting_at"], self.stamp())

    def test_successor_has_new_clock_without_rewriting_original_attempt(self):
        request_id = self.request(initial_targets="qb")
        first_at = self.stamp()
        db.finalize_download_request_submission(request_id, ("qb",), qb_status="failed")
        self.advance(10)
        with (
            patch.object(dispatcher, "get", return_value="http://qb.invalid"),
            patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "a" * 40}),
        ):
            result = dispatcher.resubmit_download_request(request_id, "qb")
        self.assertTrue(result["ok"], result)
        successor_id = result["request_id"]
        self.assertNotEqual(successor_id, request_id)
        self.assertEqual(self.row(successor_id)["qb_submitting_at"], self.stamp())
        self.assertIsNone(self.row(successor_id)["gy_submitting_at"])
        self.assertEqual(self.row(request_id)["qb_submitting_at"], first_at)

    def test_share_creation_retry_recovery_and_late_receipt_use_cloud_clock(self):
        request_id, created = db.create_share_transfer_request("clock-share", title="Audit")
        self.assertTrue(created)
        self.assertEqual(self.row(request_id)["gy_submitting_at"], self.stamp())
        self.assertIsNone(self.row(request_id)["qb_submitting_at"])
        self.assertTrue(db.finish_share_transfer_request(
            request_id, success=False, target_dir_id="parent", target_dir_name="Parent", title="Audit",
        ))
        self.advance(10)
        self.assertTrue(db.claim_failed_share_transfer_request(request_id))
        claimed_at = self.stamp()
        self.advance(1)
        self.assertFalse(db.claim_failed_share_transfer_request(request_id))
        self.assertEqual(self.row(request_id)["gy_submitting_at"], claimed_at)
        self.advance(15)
        db.update_download_request(request_id, gy_target_dir="stage")
        self.assertEqual(db.recover_stale_submitting_download_requests(), 1)
        before = dict(self.row(request_id))
        self.assertFalse(db.finish_share_transfer_request(
            request_id, success=True, target_dir_id="late", target_dir_name="Late", title="Audit",
        ))
        self.assertEqual(dict(self.row(request_id)), before)
        self.assertEqual(len(db.list_download_logs(source="guangya_share")), 1)

    def test_late_dispatch_receipts_cannot_revive_expired_claims(self):
        for path in ("first", "supplement", "retry", "successor"):
            with self.subTest(path=path):
                request_id = self.request()
                if path in {"supplement", "retry"}:
                    self.assertTrue(db.claim_download_request(request_id, "guangya"))
                    db.finalize_download_request_submission(request_id, ("guangya",), gy_status="submitted")
                    if path == "retry":
                        self.assertEqual(db.claim_download_request_targets(request_id, "qb"), ("qb",))
                        db.finalize_download_request_submission(request_id, ("qb",), qb_status="failed")
                elif path == "successor":
                    self.assertTrue(db.claim_download_request(request_id, "qb"))
                    db.finalize_download_request_submission(request_id, ("qb",), qb_status="failed")

                recovered = {}

                def submit(row, **_kwargs):
                    self.advance(16)
                    self.assertEqual(db.recover_stale_submitting_download_requests(), 1)
                    recovered.update(dict(self.row(row["id"])))
                    return {"ok": True, "task_id": "a" * 40}

                operation = {
                    "first": dispatcher.dispatch_request,
                    "supplement": dispatcher.dispatch_missing_targets,
                    "retry": dispatcher.resubmit_download_request,
                    "successor": dispatcher.resubmit_download_request,
                }[path]
                with (
                    patch.object(dispatcher, "get", return_value="http://qb.invalid"),
                    patch.object(dispatcher, "_submit_qb", side_effect=submit),
                ):
                    result = operation(request_id, "qb")
                self.assertTrue(result["stale_result"], result)
                self.assertEqual(dict(self.row(result["request_id"])), recovered)
                self.assertIn("迟到", db.list_download_logs()[0]["error"])

    def test_boot_recovers_recent_claims_and_missing_legacy_clock_ignores_updates(self):
        recent = self.request(initial_targets="both")
        claimed_at = self.stamp()
        self.assertEqual(db.recover_stale_submitting_download_requests(), 0)
        db.init_db()
        self.assertEqual((self.row(recent)["qb_status"], self.row(recent)["gy_status"]),
                         ("manual_review", "manual_review"))
        self.assertEqual((self.row(recent)["qb_submitting_at"], self.row(recent)["gy_submitting_at"]),
                         (claimed_at, claimed_at))
        legacy = self.request(initial_targets="guangya")
        self.advance(16)
        with db.get_conn() as conn:
            conn.execute("UPDATE download_requests SET gy_submitting_at=NULL,updated_at=? WHERE id=?",
                         (self.stamp(), legacy))
        self.assertEqual(db.recover_stale_submitting_download_requests(), 1)
        self.assertEqual(self.row(legacy)["gy_status"], "manual_review")

    def test_migration_backfills_only_missing_active_clocks_once(self):
        active = self.request(initial_targets="both")
        pending = self.request()
        completed = self.request(initial_targets="qb")
        db.finalize_download_request_submission(completed, ("qb",), qb_status="completed")
        fallback = self.request(initial_targets="guangya")
        undated = self.request(initial_targets="qb")
        created_at = self.stamp()
        self.advance(5)
        migrated_at = self.stamp()
        with db.get_conn() as conn:
            conn.execute("UPDATE download_requests SET qb_submitting_at=NULL,gy_submitting_at=NULL")
            conn.execute("UPDATE download_requests SET updated_at=? WHERE id=?", (migrated_at, active))
            conn.execute("UPDATE download_requests SET updated_at='' WHERE id=?", (fallback,))
            conn.execute("UPDATE download_requests SET created_at='',updated_at='' WHERE id=?", (undated,))
            with patch.object(database_migrations, "now", return_value=migrated_at):
                database_migrations._migrate_download_submission_clocks_v35(conn)
        expected = {
            active: (migrated_at, migrated_at), pending: (None, None),
            completed: (None, None), fallback: (None, created_at), undated: (migrated_at, None),
        }
        self.advance(20)
        with db.get_conn() as conn:
            conn.execute("UPDATE download_requests SET updated_at=?", (self.stamp(),))
            database_migrations._migrate_download_submission_clocks_v35(conn)
        for request_id, clocks in expected.items():
            with self.subTest(request_id=request_id):
                row = self.row(request_id)
                self.assertEqual((row["qb_submitting_at"], row["gy_submitting_at"]), clocks)
        self.assertEqual(self.row(completed)["qb_status"], "completed")

    def test_init_upgrades_versioned_unversioned_and_incomplete_baselines(self):
        for version in (34, 0, db.SCHEMA_VERSION):
            with self.subTest(version=version), isolated_test_database():
                active = self.request(initial_targets="both")
                db.update_download_request(active, qb_status="completed", qb_task_id="known")
                pending = self.request()
                share, _ = db.create_share_transfer_request("legacy-share", title="Legacy")
                db.update_download_request(share, status="pending", gy_status="completed", gy_task_id="known-share")
                old_time = "2000-01-01 00:00:00"
                with db.get_conn() as conn:
                    conn.execute("UPDATE download_requests SET updated_at=?", (old_time,))
                    conn.execute("ALTER TABLE download_requests DROP COLUMN qb_submitting_at")
                    conn.execute("ALTER TABLE download_requests DROP COLUMN gy_submitting_at")
                    conn.execute(f"PRAGMA user_version={version}")
                db.init_db()
                with db.get_conn() as conn:
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
                row = self.row(active)
                self.assertEqual((row["status"], row["qb_status"], row["gy_status"]),
                                 ("manual_review", "completed", "manual_review"))
                self.assertEqual((row["qb_submitting_at"], row["gy_submitting_at"], row["qb_task_id"]),
                                 (None, old_time, "known"))
                row = self.row(share)
                self.assertEqual((row["status"], row["gy_status"], row["gy_task_id"], row["gy_submitting_at"]),
                                 ("manual_review", "completed", "known-share", old_time))
                row = self.row(pending)
                self.assertEqual((row["status"], row["qb_submitting_at"], row["gy_submitting_at"]),
                                 ("pending", None, None))
                before = [dict(self.row(request_id)) for request_id in (active, pending, share)]
                self.advance(5)
                db.init_db()
                self.assertEqual([dict(self.row(request_id)) for request_id in (active, pending, share)], before)

    def test_migration_failure_rolls_back_columns_data_and_version_then_retries(self):
        request_id = self.request(initial_targets="both")
        with db.get_conn() as conn:
            conn.execute("ALTER TABLE download_requests DROP COLUMN qb_submitting_at")
            conn.execute("ALTER TABLE download_requests DROP COLUMN gy_submitting_at")
            conn.execute("PRAGMA user_version=34")
        before = dict(self.row(request_id))
        with patch.object(database_migrations, "now", side_effect=[self.stamp(), RuntimeError("clock-migration-fault")]):
            with self.assertRaisesRegex(RuntimeError, "clock-migration-fault"):
                db.init_db()
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 34)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(download_requests)")}
            self.assertNotIn("qb_submitting_at", columns)
            self.assertNotIn("gy_submitting_at", columns)
        self.assertEqual(dict(self.row(request_id)), before)
        db.init_db()
        row = self.row(request_id)
        self.assertEqual((row["qb_submitting_at"], row["gy_submitting_at"]),
                         (before["updated_at"], before["updated_at"]))
        self.assertEqual((row["qb_status"], row["gy_status"]), ("manual_review", "manual_review"))
