"""下载跟踪器启动、停止与线程收敛边界测试。"""
from __future__ import annotations

from threading import Thread
from typing import cast

import threading
import unittest
from unittest.mock import patch

from app.modules.download_tracker import DownloadTracker
from app.modules.telegram_download_lifecycle import build_download_lifecycle_event


class DownloadTrackerLifecycleTests(unittest.TestCase):
    def test_stop_invalidates_start_waiting_in_startup_reconciliation(self) -> None:
        tracker = DownloadTracker()
        entered = threading.Event()
        release = threading.Event()

        def reconcile():
            entered.set()
            release.wait(2)
            return 0, 0

        with patch(
            "app.repositories.media_subscriptions.reconcile_startup_media_download_admissions",
            side_effect=reconcile,
        ) as reconcile_call:
            starter = threading.Thread(target=tracker.start)
            starter.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(tracker.stop(timeout=0.1))
            release.set()
            starter.join(timeout=2)

        self.assertFalse(starter.is_alive())
        self.assertIsNone(tracker._thread)
        reconcile_call.assert_called_once_with()

    def test_manual_review_without_candidate_button_points_to_web(self) -> None:
        row = {
            "id": 1, "title": "待核对任务", "status": "manual_review",
            "qb_status": "manual_review", "gy_status": "",
            "local_import_status": "", "organize_status": "", "strm_status": "",
            "notification_payload_json": "{}",
        }
        with patch(
            "app.modules.telegram_download_lifecycle.get_notification_thread_event",
            return_value=None,
        ):
            event = build_download_lifecycle_event(row)

        self.assertIn("Web 下载任务", str(event.footer))
        self.assertIn("请勿直接重试", str(event.footer))
        self.assertNotIn("候选卡会单独发送", str(event.footer))

    def test_requires_manual_explains_separate_candidate_card(self) -> None:
        row = {
            "id": 2, "title": "待确认任务", "status": "requires_manual",
            "qb_status": "completed", "gy_status": "",
            "local_import_status": "requires_manual", "organize_status": "",
            "strm_status": "", "notification_payload_json": "{}",
        }
        with patch(
            "app.modules.telegram_download_lifecycle.get_notification_thread_event",
            return_value=None,
        ):
            event = build_download_lifecycle_event(row)

        self.assertIn("候选卡会单独发送", str(event.footer))
        self.assertIn("Web 待确认队列", str(event.footer))

    def test_stop_returns_false_while_worker_remains_alive(self) -> None:
        class StubbornThread:
            def is_alive(self) -> bool:
                return True

            def join(self, timeout=None) -> None:
                return None

        tracker = DownloadTracker()
        tracker._thread = cast(Thread, StubbornThread())

        self.assertFalse(tracker.stop(timeout=0))
        assert (tracker._thread) is not None
        self.assertTrue(tracker._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
