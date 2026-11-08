"""分享转存必须使用与普通下载一致的持久认领和迟到结果收尾合同。"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app import database as db
from app.clients.guangya import GuangYaClient
from app.modules.share_transfer import ShareTransferPreviewStore, create_share_request
from tests.support import isolated_test_database
from tests.test_telegram_guangya_share import FakeShareClient


class LifecycleShareClient(FakeShareClient, Mock):
    """保留共享替身行为，以真实客户端 spec 约束测试 Mock。"""

    def __init__(self):
        Mock.__init__(self, spec=GuangYaClient)
        FakeShareClient.__init__(self)


class ShareTransferSubmissionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.store = ShareTransferPreviewStore()
        self.client = LifecycleShareClient()
        self.preview = self.store.create(self.client.inspected, "chat", "user")
        self.enterContext(
            patch("app.modules.share_transfer.get", return_value="archive")
        )
        self.tracker = self.enterContext(
            patch(
                "app.modules.download_tracker.get_download_tracker", return_value=Mock()
            )
        ).return_value

    def submit(self, *, injected=True):
        return create_share_request(
            self.preview,
            ["file-1"],
            "target",
            "chat",
            user_id="user",
            store=self.store,
            client=self.client if injected else None,
        )

    @staticmethod
    def request():
        with db.get_conn() as conn:
            return conn.execute(
                "SELECT * FROM download_requests WHERE kind='guangya_share'"
            ).fetchone()

    def test_native_prepare_is_read_only_and_cold_confirmation_uses_frozen_files(self):
        from app.modules.share_transfer import prepare_share_transfer, submit_prepared_share_transfer
        from tests.test_telegram_guangya_share import SHARE_URL, SECRET_TOKEN, SIGNED_URL, inspected_payload
        import json

        self.client.inspected = inspected_payload(201)
        prepared = prepare_share_transfer(SHARE_URL, ["file-201", "file-1", "file-201"],
                                         "chat", "user", client=self.client, store=self.store)
        self.assertEqual(self.client.restore_calls, [])
        self.assertIsNone(self.request())
        self.assertNotIn(SECRET_TOKEN, json.dumps(prepared))
        self.assertNotIn(SIGNED_URL, json.dumps(prepared))
        self.assertEqual([f["id"] for f in prepared["snapshot"]["files"]], ["file-1", "file-201"])
        result = submit_prepared_share_transfer(prepared["snapshot"], "chat", user_id="user",
                                               client=self.client, store=ShareTransferPreviewStore())
        self.assertTrue(result["success"])
        self.assertEqual(self.client.restore_calls[0][1], ["file-1", "file-201"])
        self.assertEqual(result["request"]["request_number"], result["request_id"])
        self.assertEqual(self.client.inspect_calls, [SHARE_URL, SHARE_URL])

    def test_native_confirmation_rejects_foreign_owner_and_file_or_target_drift(self):
        from app.modules.share_transfer import prepare_share_transfer, submit_prepared_share_transfer
        from tests.test_telegram_guangya_share import SHARE_URL
        from copy import deepcopy

        prepared = prepare_share_transfer(SHARE_URL, ["file-1"], "chat", "user",
                                         client=self.client, store=self.store)["snapshot"]
        original = deepcopy(self.client.inspected)
        for change in ("owner", "file", "target"):
            with self.subTest(change=change):
                self.client.inspected = deepcopy(original)
                if change == "file":
                    self.client.inspected["files"][0]["size"] += 1
                with patch("app.modules.share_transfer.get", return_value="changed" if change == "target" else "archive"):
                    with self.assertRaises(ValueError):
                        submit_prepared_share_transfer(prepared, "other" if change == "owner" else "chat", user_id="user",
                                                       client=self.client, store=ShareTransferPreviewStore())
                self.assertEqual(self.client.restore_calls, [])
                self.assertIsNone(self.request())

    def test_native_unknown_receipt_is_not_replayed_by_new_previews(self):
        from app.modules.share_transfer import prepare_share_transfer, submit_prepared_share_transfer
        from tests.test_telegram_guangya_share import SHARE_URL

        self.client.restore_share = Mock(return_value={"success": False, "retry_safe": False})
        first = prepare_share_transfer(SHARE_URL, ["file-1"], "chat", "user", client=self.client, store=self.store)
        one = submit_prepared_share_transfer(first["snapshot"], "chat", user_id="user", client=self.client, store=self.store)
        two = submit_prepared_share_transfer(first["snapshot"], "chat", user_id="user", client=self.client, store=ShareTransferPreviewStore())
        self.assertEqual((one["status"], two["status"]), ("manual_review", "manual_review"))
        self.assertFalse(two["success"])
        self.assertFalse(two["accepted"])
        self.assertTrue(two["duplicate"])
        self.assertEqual(one["request_id"], two["request_id"])
        self.client.restore_share.assert_called_once()

    def test_request_is_durably_claimed_before_first_provider_write(self):
        observed = []
        actual = self.client.create_dir

        def create(name, parent_id="0"):
            row = self.request()
            observed.append((row["status"], row["gy_status"], row["targets"]))
            return actual(name, parent_id)

        self.client.create_dir = create
        result = self.submit()
        self.assertTrue(result["success"])
        self.assertEqual(observed, [("submitting", "submitting", "guangya")])

    def test_recovered_or_cancelled_request_rejects_late_success_and_followup(self):
        for status in ("manual_review", "cancelled", "resubmitted"):
            with self.subTest(status=status):
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM download_log")
                    conn.execute("DELETE FROM download_request_keys")
                    conn.execute("DELETE FROM download_requests")
                self.tracker.reset_mock()

                def complete_after_recovery(*args, **kwargs):
                    row = self.request()
                    db.update_download_request(
                        row["id"],
                        status=status,
                        gy_status=status,
                        error="recovered-owner",
                    )
                    return {"success": True}

                self.client.restore_share = complete_after_recovery
                result = self.submit()
                row = self.request()
                self.assertEqual(row["status"], status)
                self.assertEqual(row["gy_status"], status)
                self.assertEqual(row["error"], "recovered-owner")
                self.assertFalse(result["success"])
                self.assertEqual(result["status"], status)
                self.tracker.reload.assert_not_called()

    def test_client_preparation_failure_is_retryable_without_unknown_cloud_result(self):
        with patch(
            "app.modules.share_transfer.GuangYaClient",
            side_effect=OSError("isolated client startup failure"),
        ):
            first = self.submit(injected=False)
        self.assertFalse(first["success"])
        self.assertEqual(first["status"], "failed")
        self.assertEqual(self.request()["status"], "failed")
        self.assertEqual(self.client.restore_calls, [])
        self.tracker.reload.assert_not_called()
        with patch(
            "app.modules.share_transfer.GuangYaClient", return_value=self.client
        ):
            second = self.submit(injected=False)
        self.assertTrue(second["success"])
        self.assertTrue(second["retried"])
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(len(self.client.restore_calls), 1)
        self.assertEqual(self.client.close_calls, 1)
        third = self.submit()
        self.assertTrue(third["duplicate"])
        self.assertEqual(len(self.client.restore_calls), 1)

    def test_first_submission_is_visible_to_live_timeout_recovery(self):
        recovered_counts = []

        def complete_after_timeout(*args, **kwargs):
            row = self.request()
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE download_requests SET gy_submitting_at=? WHERE id=?",
                    ("2000-01-01 00:00:00", row["id"]),
                )
            recovered_counts.append(db.recover_stale_submitting_download_requests())
            return {"success": True}

        self.client.restore_share = complete_after_timeout
        result = self.submit()
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(recovered_counts, [1])
        self.assertEqual(db.recover_stale_submitting_download_requests(), 0)
        self.assertEqual(db.list_download_logs(source="guangya_share"), [])
        self.tracker.reload.assert_not_called()

    def test_cancel_during_directory_creation_stops_before_restore(self):
        actual = self.client.create_dir

        def create_after_cancel(*args, **kwargs):
            target = actual(*args, **kwargs)
            db.update_download_request(
                self.request()["id"], status="cancelled", gy_status="cancelled"
            )
            return target

        self.client.create_dir = create_after_cancel
        result = self.submit()
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["success"])
        self.assertEqual(self.client.restore_calls, [])
        self.tracker.reload.assert_not_called()

    def test_concurrent_repeat_does_not_acquire_or_finish_first_submission(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        entered, release = threading.Event(), threading.Event()
        actual = self.client.restore_share

        def blocked_restore(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")
            return actual(*args, **kwargs)

        self.client.restore_share = blocked_restore
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.submit)
            try:
                self.assertTrue(entered.wait(5))
                duplicate = self.submit()
                self.assertTrue(duplicate["duplicate"])
                self.assertTrue(duplicate["accepted"])
                self.assertFalse(duplicate["success"])
                self.assertEqual(duplicate["status"], "submitting")
                self.tracker.reload.assert_not_called()
            finally:
                release.set()
            completed = first.result(timeout=5)
        self.assertEqual(completed["request_id"], duplicate["request_id"])
        self.assertTrue(completed["success"])
        self.assertEqual(len(self.client.restore_calls), 1)
        self.tracker.reload.assert_called_once()

    def test_creation_claim_failure_rolls_back_request_and_key_together(self):
        with patch(
            "app.repositories.download_requests._claim_download_request_conn",
            side_effect=RuntimeError("claim failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "claim failure"):
                self.submit()
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM download_request_keys").fetchone()[
                    0
                ],
                0,
            )
        self.assertEqual(self.client.restore_calls, [])

    def test_log_insert_failure_rolls_back_finalization_and_allows_only_same_claim_finish(
        self,
    ):
        request_id, _ = db.create_share_transfer_request("atomic-log", title="atomic")
        fields = dict(
            success=True,
            target_dir_id="target",
            target_dir_name="target",
            title="atomic",
        )
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER reject_share_log BEFORE INSERT ON download_log BEGIN SELECT RAISE(ABORT,'injected log failure'); END"
            )
        import sqlite3

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected log failure"):
            db.finish_share_transfer_request(request_id, **fields)
        get_download_request_result = db.get_download_request(request_id)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result["status"], "submitting")
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER reject_share_log")
        self.assertTrue(db.finish_share_transfer_request(request_id, **fields))
        self.assertFalse(db.finish_share_transfer_request(request_id, **fields))
        self.assertEqual(len(db.list_download_logs(source="guangya_share")), 1)

    def test_legacy_pending_duplicate_and_boot_recovery_do_not_replay(self):
        from app.modules.share_transfer import share_request_key

        key = share_request_key("demo123", ["file-1"], "target", "chat", "user")
        legacy, _ = db.create_download_request(key, "guangya_share", title="legacy")
        regular, _ = db.create_download_request("ordinary-pending", "magnet")
        self.assertEqual(self.submit()["status"], "pending")
        self.assertEqual(self.client.restore_calls, [])
        db.init_db()
        self.assertEqual(self.submit()["status"], "manual_review")
        get_download_request_result_2 = db.get_download_request(regular)
        assert get_download_request_result_2 is not None
        self.assertEqual(get_download_request_result_2["status"], "pending")
        get_download_request_result_4 = db.get_download_request(legacy)
        assert get_download_request_result_4 is not None
        recovered = dict(get_download_request_result_4)
        db.init_db()
        get_download_request_result_5 = db.get_download_request(legacy)
        assert get_download_request_result_5 is not None
        self.assertEqual(dict(get_download_request_result_5), recovered)
        self.assertEqual(self.client.restore_calls, [])


class ShareTransferProcessRestoreTests(unittest.TestCase):
    def test_exit_during_provider_write_and_zip_restore_keep_manual_gate(self):
        import subprocess
        import sys
        from pathlib import Path

        from app.modules import backup
        from tests.test_ten_pass_process_restore_audit import runtime_paths

        with isolated_test_database("mediaflux.db") as path:
            paths = runtime_paths(path)
            script = r"""
import tests
import os, sys, socket
from pathlib import Path
from unittest.mock import patch
from app import database as db
from app.modules.share_transfer import ShareTransferPreviewStore, create_share_request
from tests.test_telegram_guangya_share import FakeShareClient
socket.socket.connect = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network forbidden"))
db.configure_database(sys.argv[1], test_mode=True)
db.init_db()
client = FakeShareClient()
store = ShareTransferPreviewStore()
preview = store.create(client.inspected, "chat", "user")
def accepted_then_exit(*args):
 Path(sys.argv[1]).with_suffix(".cloud-marker").write_text("accepted-once")
 os._exit(29)
client.restore_share = accepted_then_exit
with patch("app.modules.share_transfer.get", return_value="archive"):
 create_share_request(preview, ["file-1"], "target", "chat", user_id="user", store=store, client=client)
raise AssertionError("provider window not reached")
"""
            child = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(child.returncode, 29, child.stderr)
            self.assertEqual(
                Path(path).with_suffix(".cloud-marker").read_text(), "accepted-once"
            )
            with db.get_conn() as conn:
                row = conn.execute("SELECT * FROM download_requests").fetchone()
                request_id = row["id"]
                self.assertEqual(
                    (row["status"], row["gy_status"]), ("submitting", "submitting")
                )
                self.assertEqual(row["gy_target_dir"], "staging-target")
            archive = backup.create_backup(paths, reason="share-interrupted")
            backup.verify_backup(archive)
            db.init_db()
            get_download_request_result_3 = db.get_download_request(request_id)
            assert get_download_request_result_3 is not None
            self.assertEqual(
                get_download_request_result_3["status"], "manual_review"
            )
            db.update_download_request(
                request_id, status="cancelled", gy_status="cancelled"
            )
            backup.restore_backup(paths, archive)
            db.init_db()
            recovered_row = db.get_download_request(request_id)
            assert recovered_row is not None
            recovered = dict(recovered_row)
            self.assertEqual(recovered["status"], "manual_review")
            self.assertEqual(recovered["gy_target_dir"], "staging-target")
            self.assertFalse(db.claim_failed_share_transfer_request(request_id))
            self.assertFalse(
                db.finish_share_transfer_request(
                    request_id,
                    success=True,
                    target_dir_id="target",
                    target_dir_name="target",
                    title="late",
                )
            )
            client = LifecycleShareClient()
            store = ShareTransferPreviewStore()
            preview = store.create(client.inspected, "chat", "user")
            result = create_share_request(
                preview,
                ["file-1"],
                "target",
                "chat",
                user_id="user",
                store=store,
                client=client,
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "manual_review")
            self.assertEqual(client.restore_calls, [])
            db.init_db()
            current = db.get_download_request(request_id)
            assert current is not None
            self.assertEqual(dict(current), recovered)
