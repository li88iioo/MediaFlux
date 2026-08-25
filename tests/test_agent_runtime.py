import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from app.modules import agent_runtime


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        agent_runtime._reconcile_requested.clear()
        agent_runtime._shutdown_requested.clear()

    def tearDown(self):
        agent_runtime._shutdown_requested.set()
        agent_runtime._reconcile_requested.set()
        worker = agent_runtime._worker
        if worker is not None:
            worker.join(timeout=2)
        agent_runtime._shutdown_requested.clear()
        agent_runtime._reconcile_requested.clear()

    def test_disabled_runtime_stops_all_schedulers(self):
        schedulers = tuple(MagicMock() for _ in range(3))
        for scheduler in schedulers:
            scheduler.stop.return_value = True

        with patch.object(agent_runtime, "is_agent_enabled", return_value=False), patch.object(
            agent_runtime, "_schedulers", return_value=schedulers
        ):
            agent_runtime.reconcile_agent_runtime()

        for scheduler in schedulers:
            scheduler.stop.assert_called_once_with(timeout=1.0)
            scheduler.start.assert_not_called()

    def test_enabled_runtime_restarts_clean_scheduler_threads(self):
        schedulers = tuple(MagicMock() for _ in range(3))
        for scheduler in schedulers:
            scheduler.stop.return_value = True

        with patch.object(agent_runtime, "is_agent_enabled", return_value=True), patch.object(
            agent_runtime, "_schedulers", return_value=schedulers
        ):
            agent_runtime.reconcile_agent_runtime()

        for scheduler in schedulers:
            scheduler.stop.assert_called_once_with(timeout=1.0)
            scheduler.start.assert_called_once_with()

    def test_request_returns_before_slow_scheduler_stops(self):
        entered = threading.Event()
        release = threading.Event()
        scheduler = MagicMock()

        def slow_stop(*, timeout):
            entered.set()
            release.wait(timeout=2)
            return True

        scheduler.stop.side_effect = slow_stop
        schedulers = (scheduler, MagicMock(), MagicMock())
        schedulers[1].stop.return_value = True
        schedulers[2].stop.return_value = True

        with patch.object(agent_runtime, "is_agent_enabled", return_value=False), patch.object(
            agent_runtime, "_schedulers", return_value=schedulers
        ):
            started = time.monotonic()
            self.assertTrue(agent_runtime.request_agent_runtime_reconcile())
            elapsed = time.monotonic() - started
            self.assertTrue(entered.wait(timeout=1))
            self.assertLess(elapsed, 0.2)
            release.set()
            worker = agent_runtime._worker
            if worker is not None:
                worker.join(timeout=2)

    def test_shutdown_prevents_pending_worker_from_starting_schedulers(self):
        entered = threading.Event()
        release = threading.Event()
        scheduler = MagicMock()

        def slow_stop(*, timeout):
            entered.set()
            release.wait(timeout=2)
            return True

        scheduler.stop.side_effect = slow_stop
        schedulers = (scheduler, MagicMock(), MagicMock())
        schedulers[1].stop.return_value = True
        schedulers[2].stop.return_value = True

        with patch.object(agent_runtime, "is_agent_enabled", return_value=True), patch.object(
            agent_runtime, "_schedulers", return_value=schedulers
        ):
            self.assertTrue(agent_runtime.request_agent_runtime_reconcile())
            self.assertTrue(entered.wait(timeout=1))
            agent_runtime._shutdown_requested.set()
            release.set()
            worker = agent_runtime._worker
            if worker is not None:
                worker.join(timeout=2)

        for item in schedulers:
            item.start.assert_not_called()

    def test_state_change_during_stop_is_reconciled_to_latest_value(self):
        schedulers = tuple(MagicMock() for _ in range(3))
        for scheduler in schedulers:
            scheduler.stop.return_value = True
        states = iter([False, True, True, True, True, True, True, True, True])

        with patch.object(
            agent_runtime, "is_agent_enabled", side_effect=lambda: next(states)
        ), patch.object(agent_runtime, "_schedulers", return_value=schedulers):
            agent_runtime.reconcile_agent_runtime()

        for scheduler in schedulers:
            scheduler.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
