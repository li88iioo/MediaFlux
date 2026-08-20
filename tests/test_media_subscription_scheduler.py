"""媒体订阅调度器停止边界与有界并发测试。"""
from __future__ import annotations

from unittest import TestCase
from unittest.mock import AsyncMock, patch

from app.modules.media_subscription_scheduler import MediaSubscriptionScheduler


class _FakeThread:
    instances: list["_FakeThread"] = []

    def __init__(self, *, target=None, args=(), name="", daemon=False):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.alive = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout=None) -> None:
        self.alive = False


class _StubbornThread(_FakeThread):
    def join(self, timeout=None) -> None:
        return None


class MediaSubscriptionSchedulerTests(TestCase):
    def setUp(self) -> None:
        _FakeThread.instances.clear()

    def test_run_due_after_stop_does_not_query_or_start_workers(self) -> None:
        scheduler = MediaSubscriptionScheduler()
        scheduler._stop_event.set()
        with patch(
            "app.modules.media_subscription_scheduler.db.recover_stale_media_subscription_checks"
        ) as recover, patch(
            "app.modules.media_subscription_scheduler.db.list_due_media_subscriptions"
        ) as due, patch(
            "app.modules.media_subscription_scheduler.threading.Thread", _FakeThread
        ):
            self.assertEqual(scheduler.run_due(), 0)
        recover.assert_not_called()
        due.assert_not_called()
        self.assertEqual(_FakeThread.instances, [])

    def test_run_due_caps_workers_and_deduplicates_subscription_ids(self) -> None:
        scheduler = MediaSubscriptionScheduler()
        rows = [{"id": 1}, {"id": 1}, {"id": 2}, {"id": 3}]
        with patch(
            "app.modules.media_subscription_scheduler.db.recover_stale_media_subscription_checks",
            return_value=0,
        ), patch(
            "app.modules.media_subscription_scheduler.db.list_due_media_subscriptions",
            return_value=rows,
        ), patch(
            "app.modules.media_subscription_scheduler.threading.Thread", _FakeThread
        ):
            self.assertEqual(scheduler.run_due(), 2)

        self.assertEqual(sorted(scheduler._workers), [1, 2])
        self.assertEqual([thread.args for thread in _FakeThread.instances], [(1,), (2,)])

    def test_execute_passes_shared_stop_event_to_service(self) -> None:
        scheduler = MediaSubscriptionScheduler()
        service = type("Service", (), {})()
        service.check_subscription = AsyncMock(return_value={})
        with patch(
            "app.modules.media_subscription_scheduler.get_media_subscription_service",
            return_value=service,
        ):
            scheduler._execute(7)
        service.check_subscription.assert_awaited_once_with(
            7, trigger="scheduler", cancel_event=scheduler._stop_event
        )

    def test_stop_timeout_keeps_live_thread_references(self) -> None:
        scheduler = MediaSubscriptionScheduler()
        scheduler_thread = _StubbornThread()
        scheduler_thread.alive = True
        worker = _StubbornThread()
        worker.alive = True
        scheduler._thread = scheduler_thread
        scheduler._workers = {9: worker}

        self.assertFalse(scheduler.stop(timeout=0))
        self.assertIs(scheduler._thread, scheduler_thread)
        self.assertEqual(scheduler._workers, {9: worker})

    def test_stop_clears_finished_scheduler_and_workers(self) -> None:
        scheduler = MediaSubscriptionScheduler()
        scheduler_thread = _FakeThread()
        scheduler_thread.alive = True
        worker = _FakeThread()
        worker.alive = True
        scheduler._thread = scheduler_thread
        scheduler._workers = {11: worker}

        self.assertTrue(scheduler.stop(timeout=1))
        self.assertIsNone(scheduler._thread)
        self.assertEqual(scheduler._workers, {})
        self.assertTrue(scheduler._stop_event.is_set())
        self.assertTrue(scheduler._wake_event.is_set())
