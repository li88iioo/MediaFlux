"""媒体反代播放记录写入器的背压、失败隔离与关闭契约。"""
from __future__ import annotations

import asyncio
import threading
import unittest

from app.modules.media_proxy_recorder import PlaybackRecordWriter


def _record(status_code: int = 206, *, failure_stage: str = "") -> dict:
    return {
        "instance_id": 7,
        "route_class": "stream",
        "method": "GET",
        "status_code": status_code,
        "source": "upstream",
        "cache_hit": False,
        "upstream_latency_ms": 10,
        "total_latency_ms": 12,
        "failure_stage": failure_stage,
        "error": "",
    }


class PlaybackRecordWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_reserves_capacity_for_errors_and_never_waits_for_database(self):
        entered = threading.Event()
        release = threading.Event()
        calls: list[dict] = []

        def write(payload):
            calls.append(dict(payload))
            if len(calls) == 1:
                entered.set()
                release.wait(timeout=2)

        writer = PlaybackRecordWriter(capacity=2, write_record=write)
        await writer.start()
        try:
            self.assertTrue(writer.enqueue(_record()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertTrue(writer.enqueue(_record()))
            self.assertFalse(writer.enqueue(_record()))
            self.assertTrue(writer.enqueue(_record(502, failure_stage="upstream")))

            metrics = writer.metrics()
            self.assertEqual(metrics["dropped_full"], 1)
            self.assertEqual(metrics["dropped_low_priority"], 1)
            self.assertEqual(metrics["dropped_error"], 0)
            self.assertEqual(metrics["enqueued"], 3)

            release.set()
            self.assertTrue(await writer.stop())
        finally:
            release.set()
            await writer.stop()

        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1]["status_code"], 502)
        self.assertEqual(writer.metrics()["written"], 3)

    async def test_single_write_failure_does_not_stop_later_records(self):
        call_count = 0

        def write(_payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated write failure")

        writer = PlaybackRecordWriter(capacity=4, write_record=write)
        await writer.start()
        self.assertTrue(writer.enqueue(_record(500, failure_stage="proxy")))
        self.assertTrue(writer.enqueue(_record()))

        self.assertTrue(await writer.stop())
        metrics = writer.metrics()
        self.assertEqual(call_count, 2)
        self.assertEqual(metrics["failed"], 1)
        self.assertEqual(metrics["written"], 1)
        self.assertEqual(metrics["pending"], 0)

    async def test_stop_waits_for_inflight_and_queued_records(self):
        entered = threading.Event()
        release = threading.Event()
        calls: list[dict] = []

        def write(payload):
            calls.append(dict(payload))
            if len(calls) == 1:
                entered.set()
                release.wait(timeout=2)

        writer = PlaybackRecordWriter(capacity=4, write_record=write)
        await writer.start()
        try:
            self.assertTrue(writer.enqueue(_record()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertTrue(writer.enqueue(_record(500, failure_stage="proxy")))
            stop_task = asyncio.create_task(writer.stop())
            await asyncio.sleep(0)
            self.assertFalse(stop_task.done())
            release.set()
            self.assertTrue(await stop_task)
        finally:
            release.set()
            await writer.stop()

        self.assertEqual(len(calls), 2)
        self.assertTrue(writer.metrics()["drained"])
        self.assertFalse(writer.metrics()["worker_running"])


    async def test_cancelling_stop_still_waits_for_worker_cleanup(self):
        entered = threading.Event()
        release = threading.Event()

        def write(_payload):
            entered.set()
            release.wait(timeout=2)

        writer = PlaybackRecordWriter(
            capacity=4,
            drain_timeout_seconds=1,
            write_record=write,
        )
        await writer.start()
        self.assertTrue(writer.enqueue(_record()))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))

        stop_task = asyncio.create_task(writer.stop())
        await asyncio.sleep(0)
        stop_task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(stop_task.done())
        self.assertTrue(writer.metrics()["worker_running"])
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await stop_task
        self.assertFalse(writer.metrics()["worker_running"])

    async def test_shutdown_timeout_drops_only_queued_records_and_returns(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def write(_payload):
            entered.set()
            release.wait(timeout=2)
            finished.set()

        writer = PlaybackRecordWriter(
            capacity=4,
            drain_timeout_seconds=0.05,
            write_record=write,
        )
        await writer.start()
        self.assertTrue(writer.enqueue(_record()))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        self.assertTrue(writer.enqueue(_record()))

        try:
            stop_task = asyncio.create_task(writer.stop())
            await asyncio.sleep(0.08)
            self.assertFalse(stop_task.done())
            self.assertTrue(writer.metrics()["worker_running"])
            release.set()
            self.assertFalse(await stop_task)
            metrics = writer.metrics()
            self.assertFalse(metrics["drained"])
            self.assertEqual(metrics["dropped_shutdown"], 1)
            self.assertFalse(metrics["worker_running"])
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))
        finally:
            release.set()
            await writer.stop()


if __name__ == "__main__":
    unittest.main()
