"""下载跟踪器启动、停止与线程收敛边界测试。"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from app.modules.download_tracker import DownloadTracker


class DownloadTrackerLifecycleTests(unittest.TestCase):
    def test_stop_invalidates_start_waiting_in_startup_reconciliation(self) -> None:
        tracker = DownloadTracker()
        entered = threading.Event()
        release = threading.Event()

        def reconcile(*_args, **_kwargs):
            entered.set()
            release.wait(2)
            return 0, 0

        with patch(
            "app.modules.download_tracker.db.reconcile_startup_media_download_admissions",
            side_effect=reconcile,
        ):
            starter = threading.Thread(target=tracker.start)
            starter.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(tracker.stop(timeout=0.1))
            release.set()
            starter.join(timeout=2)

        self.assertFalse(starter.is_alive())
        self.assertIsNone(tracker._thread)

    def test_stop_returns_false_while_worker_remains_alive(self) -> None:
        class StubbornThread:
            def is_alive(self) -> bool:
                return True

            def join(self, timeout=None) -> None:
                return None

        tracker = DownloadTracker()
        tracker._thread = StubbornThread()

        self.assertFalse(tracker.stop(timeout=0))
        self.assertIsNotNone(tracker._thread)
        self.assertTrue(tracker._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
