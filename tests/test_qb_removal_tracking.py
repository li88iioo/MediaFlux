"""用户移除 qB 任务后，下载跟踪必须持久收口；只使用隔离 DB 和假客户端。"""
from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.modules.download_tracker import DownloadTracker
from app.modules.qb_control import (
    QBControlSafetyUnavailable,
    QBTaskRemovalUnconfirmed,
    remove_qb_tasks,
)
from app.modules.telegram_download_lifecycle import build_download_lifecycle_event
from tests.support import isolated_test_database


class QbRemovalTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(isolated_test_database())
        self.serial = 0

    def request(self, *, task_hash="a" * 40, qb="downloading", gy="", **fields):
        self.serial += 1
        request_id, _ = db.create_download_request(
            f"removal-{self.serial}", "magnet", title="同名测试任务", origin="telegram",
        )
        db.update_download_request(
            request_id, status="downloading", qb_status=qb, qb_task_id=task_hash,
            gy_status=gy, targets="both" if gy else "qb",
        )
        if fields:
            db.update_download_request(request_id, **fields)
        db.add_download_log(
            "qb", title="同名测试任务", request_id=request_id,
            backend_task_id=task_hash, status="success" if qb == "completed" else qb,
            progress=1 if qb == "completed" else 0.1,
        )
        return request_id

    def test_cancel_is_durable_before_remote_request_and_after_restart(self):
        request_id = self.request()
        client = Mock()

        def interrupted(*_args, **kwargs):
            self.assertFalse(kwargs["delete_files"])
            row = db.get_download_request(request_id)
            assert row is not None
            self.assertEqual((row["status"], row["qb_status"]), ("cancelled", "cancelled"))
            raise SystemExit("模拟进程在远端调用时退出")

        client.delete_torrents.side_effect = interrupted
        with self.assertRaises(SystemExit):
            remove_qb_tasks(client, ["A" * 40])
        db.init_db()
        get_download_request_result = db.get_download_request(request_id)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result["qb_status"], "cancelled")
        self.assertEqual(db.list_active_download_requests(include_local_import=True), [])

    def test_timeout_and_explicit_rejection_do_not_claim_remote_deleted(self):
        for failure in (TimeoutError("read timeout"), False):
            with self.subTest(failure=type(failure).__name__), isolated_test_database():
                request_id = self.request()
                client = Mock()
                if isinstance(failure, Exception):
                    client.delete_torrents.side_effect = failure
                else:
                    client.delete_torrents.return_value = failure
                with self.assertRaisesRegex(QBTaskRemovalUnconfirmed, "本地跟踪已停止.*移除未确认"):
                    remove_qb_tasks(client, ["a" * 40])
                get_download_request_result_2 = db.get_download_request(request_id)
                assert get_download_request_result_2 is not None
                self.assertEqual(get_download_request_result_2["status"], "cancelled")
                client.delete_torrents.assert_called_once_with("a" * 40, delete_files=False)

    def test_db_unavailable_prevents_remote_deletion(self):
        client = Mock()
        with patch.object(db, "cancel_qb_download_tracking", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(QBControlSafetyUnavailable):
                remove_qb_tasks(client, ["a" * 40])
        client.delete_torrents.assert_not_called()

    def test_cancelled_snapshot_cannot_trigger_late_completion_or_missing_notice(self):
        request_id = self.request(qb_task_missing_since="2000-01-01 00:00:00")
        old = db.get_download_request(request_id)
        db.cancel_qb_download_tracking(["a" * 40])
        task = SimpleNamespace(hash="a" * 40, name="同名测试任务", progress=1, state="uploading", content_path="/isolated/test.mkv")
        tracker = DownloadTracker()
        with patch.object(tracker, "_start_local_import") as local, patch.object(tracker, "_notify_completion") as notify:
            tracker._update_request(old, [task], [])
            tracker._update_request(db.get_download_request(request_id), [], [])
        local.assert_not_called()
        notify.assert_not_called()
        get_download_request_result_3 = db.get_download_request(request_id)
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3["status"], "cancelled")

    def test_batch_exact_identity_preserves_completed_and_other_backend(self):
        first = self.request(gy="downloading", error="历史光鸭提示")
        complete = self.request(task_hash="b" * 40, qb="completed", local_import_status="completed")
        other = self.request(task_hash="c" * 40)
        legacy = self.request(task_hash="d" * 40)
        db.update_download_request(legacy, qb_task_id="")
        result = db.cancel_qb_download_tracking(["A" * 40, "a" * 40, "b" * 40, "d" * 40])
        self.assertEqual(result, [first])
        get_download_request_result_4 = db.get_download_request(legacy)
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4["qb_status"], "downloading")
        row = db.get_download_request(first)
        assert row is not None
        self.assertEqual((row["status"], row["qb_status"], row["gy_status"]), ("downloading", "cancelled", "downloading"))
        self.assertEqual(row["error"], "历史光鸭提示")
        get_download_request_result_5 = db.get_download_request(complete)
        assert get_download_request_result_5 is not None
        self.assertEqual(get_download_request_result_5["qb_status"], "completed")
        get_download_request_result_6 = db.get_download_request(complete)
        assert get_download_request_result_6 is not None
        self.assertEqual(get_download_request_result_6["local_import_status"], "completed")
        get_download_request_result_7 = db.get_download_request(other)
        assert get_download_request_result_7 is not None
        self.assertEqual(get_download_request_result_7["qb_status"], "downloading")
        self.assertEqual(db.cancel_qb_download_tracking(["a" * 40, "d" * 40]), [])

    def test_cancelled_qb_does_not_prevent_other_backend_finishing_or_failing(self):
        for gy_task, expected in (({"id": "gy-1", "status": "failed", "progress": 0}, "failed"), ({"id": "gy-1", "status": "completed", "progress": 1}, "completed")):
            with self.subTest(expected=expected), isolated_test_database():
                request_id = self.request(gy="downloading", gy_task_id="gy-1")
                db.cancel_qb_download_tracking(["a" * 40])
                tracker = DownloadTracker()
                with patch.object(tracker, "_notify_completion"), patch.object(tracker, "_staging_ready_for_organize", return_value=False):
                    tracker._update_request(db.get_download_request(request_id), [], [gy_task])
                row = db.get_download_request(request_id)
                assert row is not None
                self.assertEqual((row["status"], row["gy_status"], row["qb_status"]), (expected, expected, "cancelled"))
                self.assertEqual(row["notification_delivery_status"], "pending")

    def test_cancelled_branch_does_not_resurrect_resubmitted_root(self):
        request_id = self.request(gy="downloading", status="resubmitted")
        db.cancel_qb_download_tracking(["a" * 40])
        get_download_request_result_8 = db.get_download_request(request_id)
        assert get_download_request_result_8 is not None
        self.assertEqual(get_download_request_result_8["status"], "resubmitted")
        self.assertIn(request_id, [row["id"] for row in db.list_active_download_requests()])

    def test_unobserved_external_removal_keeps_missing_guard_and_never_assumes_cancel(self):
        request_id = self.request(qb_task_missing_since="2000-01-01 00:00:00")
        tracker = DownloadTracker()
        with patch.object(tracker, "_notify_completion"):
            tracker._update_request(db.get_download_request(request_id), [], [], qb_available=False)
            get_download_request_result_9 = db.get_download_request(request_id)
            assert get_download_request_result_9 is not None
            self.assertEqual(get_download_request_result_9["qb_status"], "downloading")
            tracker._update_request(db.get_download_request(request_id), [], [], qb_available=True)
        row = db.get_download_request(request_id)
        assert row is not None
        self.assertEqual((row["status"], row["qb_status"]), ("manual_review", "manual_review"))
        with patch("app.modules.telegram_download_lifecycle.get_notification_thread_event", return_value=None):
            event = build_download_lifecycle_event(row, probe_progress={})
        organize_status = dict(event.fields)["自动整理"]
        assert isinstance(organize_status, str)
        self.assertIn("未启动", organize_status)
        self.assertIn("无法仅凭缺失判断", str(event.footer))
        self.assertIn("请勿直接重试", str(event.footer))
        self.assertEqual(dict(event.fields)["发现 qB 缺失"], "2000-01-01 00:00:00")
        self.assertIn("请求创建", dict(event.fields))

    def test_explicit_cancel_clears_only_own_pending_notification(self):
        request_id = self.request(qb="manual_review", status="manual_review", notification_event_status="manual_review", notification_delivery_status="pending")
        db.cancel_qb_download_tracking(["a" * 40])
        row = db.get_download_request(request_id)
        assert row is not None
        self.assertEqual(row["notification_delivery_status"], "")
        self.assertIsNone(db.claim_download_request_notification(request_id))

    def test_no_fuzzy_identity_or_invalid_batch_partial_write(self):
        request_id = self.request()
        self.assertEqual(db.cancel_qb_download_tracking(["b" * 40]), [])
        with self.assertRaises(ValueError):
            db.cancel_qb_download_tracking(["a" * 40, "bad"])
        get_download_request_result_10 = db.get_download_request(request_id)
        assert get_download_request_result_10 is not None
        self.assertEqual(get_download_request_result_10["qb_status"], "downloading")

    def test_public_delete_uses_same_durable_tracking_contract(self):
        from app.modules import qb_control

        request_id = self.request()
        client = Mock()
        task = SimpleNamespace(hash="a" * 40, name="测试任务", size=123, added_on=1)
        client.list_torrents.side_effect = [[task], [task], []]
        def delete(*_args, **_kwargs):
            row = db.get_download_request(request_id)
            self.assertIsNotNone(row)
            self.assertEqual(row["qb_status"], "cancelled")
            return True
        client.delete_torrents.side_effect = delete
        with patch.object(qb_control.config, "get_many", return_value={}), patch.object(qb_control, "configured_qb_client", return_value=client):
            snapshot = qb_control.prepare_qb_control("delete", ["a" * 40])
            result = qb_control.execute_qb_control(snapshot)
        self.assertEqual(result["verification"], "verified")
        client.delete_torrents.assert_called_once_with("a" * 40, delete_files=False)

    def test_public_db_preflight_failure_is_not_a_remote_unknown_write(self):
        from app.modules import qb_control

        request_id = self.request()
        client = Mock()
        client.list_torrents.return_value = [SimpleNamespace(hash="a" * 40, name="测试任务", size=123, added_on=1)]
        with patch.object(qb_control.config, "get_many", return_value={}), patch.object(qb_control, "configured_qb_client", return_value=client):
            snapshot = qb_control.prepare_qb_control("delete", ["a" * 40])
            with patch.object(db, "cancel_qb_download_tracking", side_effect=sqlite3.OperationalError("locked")):
                with self.assertRaises(QBControlSafetyUnavailable):
                    qb_control.execute_qb_control(snapshot)
        client.delete_torrents.assert_not_called()
        request = db.get_download_request(request_id)
        assert request is not None
        self.assertEqual(request["qb_status"], "downloading")
