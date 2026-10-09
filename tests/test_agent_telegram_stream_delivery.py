from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from app.agent.kernel.events import AgentEventType, EventFactory
from app.bot import agent_adapter as adapter


class ControlledProgress:
    """进度接收器：可暂停首次传输，并记录并发与发送快照。"""

    mode = "edit"

    def __init__(
        self,
        *,
        block_first: bool = False,
        fail_first: bool = False,
        watch_for: str = "",
    ) -> None:
        self.block_first = block_first
        self.fail_first = fail_first
        self.watch_for = watch_for
        self.first_update_started = threading.Event()
        self.release_first_update = threading.Event()
        self.watched_update_started = threading.Event()
        self.update_called = threading.Event()
        self._lock = threading.Lock()
        self.started: list[str] = []
        self.completed: list[str] = []
        self.active = 0
        self.max_active = 0

    def update(self, rendered: str) -> bool:
        with self._lock:
            index = len(self.started)
            self.started.append(rendered)
            self.active += 1
            self.max_active = max(self.max_active, self.active)

        if index == 0:
            self.first_update_started.set()
        self.update_called.set()
        if self.watch_for and self.watch_for in rendered:
            self.watched_update_started.set()

        try:
            if (
                index == 0
                and self.block_first
                and not self.release_first_update.wait(timeout=5)
            ):
                raise TimeoutError("测试未及时释放模拟中的 Telegram 传输")
            if index == 0 and self.fail_first:
                raise RuntimeError("模拟 Telegram edit 失败")
            with self._lock:
                self.completed.append(rendered)
            return True
        finally:
            with self._lock:
                self.active -= 1

    def started_snapshot(self) -> list[str]:
        with self._lock:
            return list(self.started)

    def completed_snapshot(self) -> list[str]:
        with self._lock:
            return list(self.completed)


def _factory() -> EventFactory:
    return EventFactory(session_id="session", turn_id="turn", request_id="request")


async def _wait_for_thread_event(event: threading.Event) -> None:
    ready = await asyncio.wait_for(asyncio.to_thread(event.wait, 3), timeout=4)
    if not ready:
        raise AssertionError("等待模拟 Telegram 传输超时")


class TelegramStreamDeliveryTests(unittest.TestCase):
    def test_slow_update_does_not_backpressure_events_and_sends_only_latest_merge(self):
        progress = ControlledProgress(block_first=True, watch_for="LATEST-END")
        observer = adapter._TelegramEventObserver(progress)
        factory = _factory()
        # 与录屏规模一致：673 个 delta、共 1,143 个字符。
        fragments = ["S"] + ["xy"] * 461 + ["x"] * 210 + ["LATEST-END"]
        expected_text = "".join(fragments)

        async def run():
            async def operation():
                await observer(factory.create(AgentEventType.MODEL_STARTED, {"round": 1}))
                await observer(factory.create(
                    AgentEventType.MODEL_DELTA,
                    {"round": 1, "delta": fragments[0]},
                ))
                await _wait_for_thread_event(progress.first_update_started)

                # 首个网络编辑仍阻塞时，整轮剩余 delta 必须在限定时间内被消费。
                async def emit_remaining_deltas():
                    for fragment in fragments[1:]:
                        await observer(factory.create(
                            AgentEventType.MODEL_DELTA,
                            {"round": 1, "delta": fragment},
                        ))

                await asyncio.wait_for(emit_remaining_deltas(), timeout=1)
                self.assertEqual(len(progress.started_snapshot()), 1)
                self.assertEqual(progress.max_active, 1)

                progress.release_first_update.set()
                await _wait_for_thread_event(progress.watched_update_started)
                await observer(factory.create(AgentEventType.TURN_COMPLETED))
                return "final-view"

            try:
                return await observer.consume(operation())
            finally:
                progress.release_first_update.set()

        with (
            patch.object(adapter, "_STREAM_EDIT_INTERVAL_SECONDS", 0.01),
            patch.object(adapter, "_STREAM_DRAFT_INTERVAL_SECONDS", 0.01),
        ):
            result = asyncio.run(run())

        self.assertEqual(result, "final-view")
        sent = progress.started_snapshot()
        self.assertEqual(len(sent), 2, "在途编辑后应只发送一个最新合并快照")
        self.assertIn(expected_text, sent[-1])
        self.assertEqual(progress.max_active, 1, "Telegram update 不得并发")

    def test_consume_waits_for_inflight_update_on_terminal_and_exception(self):
        for ending in ("terminal", "exception"):
            with self.subTest(ending=ending):
                self._assert_consume_waits_for_inflight_update(ending)

    def _assert_consume_waits_for_inflight_update(self, ending: str) -> None:
        progress = ControlledProgress(block_first=True)
        observer = adapter._TelegramEventObserver(progress)
        factory = _factory()
        operation_finished = threading.Event()
        final_view = object()

        async def run():
            async def operation():
                await observer(factory.create(AgentEventType.MODEL_STARTED, {"round": 1}))
                await observer(factory.create(
                    AgentEventType.MODEL_DELTA,
                    {"round": 1, "delta": "IN-FLIGHT"},
                ))
                await _wait_for_thread_event(progress.first_update_started)
                # 首次 I/O 阻塞期间产生的草稿，终态不能排队补播。
                await observer(factory.create(
                    AgentEventType.MODEL_DELTA,
                    {"round": 1, "delta": "STALE-DRAFT"},
                ))
                operation_finished.set()
                if ending == "terminal":
                    await observer(factory.create(AgentEventType.TURN_COMPLETED))
                    return final_view
                raise RuntimeError("模拟 runtime.query 异常")

            try:
                task = asyncio.create_task(observer.consume(operation()))
                await _wait_for_thread_event(operation_finished)
                await asyncio.sleep(0.02)
                self.assertFalse(task.done(), "consume 必须等待真实在途 update")
                progress.release_first_update.set()
                if ending == "terminal":
                    return await task
                with self.assertRaisesRegex(RuntimeError, "模拟 runtime.query 异常"):
                    await task
                return None
            finally:
                progress.release_first_update.set()

        with (
            patch.object(adapter, "_STREAM_EDIT_INTERVAL_SECONDS", 0.01),
            patch.object(adapter, "_STREAM_DRAFT_INTERVAL_SECONDS", 0.01),
        ):
            result = asyncio.run(run())

        if ending == "terminal":
            self.assertIs(result, final_view)
        else:
            self.assertIsNone(result)

        # consume 已等待 worker 收尾；此后写入终态不得被旧草稿覆盖。
        progress.update("FINAL VIEW")
        time.sleep(0.05)
        completed = progress.completed_snapshot()
        self.assertEqual(completed[-1], "FINAL VIEW")
        self.assertFalse(any("STALE-DRAFT" in text for text in completed))
        self.assertEqual(progress.max_active, 1)
        self.assertEqual(len(progress.started_snapshot()), 2)

    def test_stream_off_keeps_status_without_model_delta_text(self):
        progress = ControlledProgress(watch_for="正在查询媒体库")
        observer = adapter._TelegramEventObserver(progress)
        factory = _factory()
        final_view = object()

        async def operation():
            await observer(factory.create(
                AgentEventType.TURN_STARTED,
                {"stream_display_enabled": False},
            ))
            await observer(factory.create(AgentEventType.MODEL_STARTED, {"round": 1}))
            await observer(factory.create(
                AgentEventType.MODEL_DELTA,
                {"round": 1, "delta": "PRIVATE-DRAFT-BODY"},
            ))
            await observer(factory.create(
                AgentEventType.TOOL_PROGRESS,
                {
                    "phase": "background_job",
                    "tool": "library.search",
                    "summary": "正在查询媒体库",
                },
            ))
            await _wait_for_thread_event(progress.watched_update_started)
            await observer(factory.create(AgentEventType.TURN_COMPLETED))
            return final_view

        async def run():
            return await observer.consume(operation())

        with (
            patch.object(adapter, "_STREAM_EDIT_INTERVAL_SECONDS", 0.01),
            patch.object(adapter, "_STREAM_DRAFT_INTERVAL_SECONDS", 0.01),
        ):
            result = asyncio.run(run())

        self.assertIs(result, final_view)
        sent = "\n".join(progress.started_snapshot())
        self.assertIn("正在查询媒体库", sent)
        self.assertNotIn("PRIVATE-DRAFT-BODY", sent)

    def test_update_failure_does_not_change_final_view(self):
        progress = ControlledProgress(fail_first=True)
        observer = adapter._TelegramEventObserver(progress)
        factory = _factory()
        final_view = object()

        async def operation():
            await observer(factory.create(AgentEventType.MODEL_STARTED, {"round": 1}))
            await observer(factory.create(
                AgentEventType.MODEL_DELTA,
                {"round": 1, "delta": "DRAFT"},
            ))
            await _wait_for_thread_event(progress.update_called)
            return final_view

        async def run():
            return await observer.consume(operation())

        with (
            patch.object(adapter, "_STREAM_EDIT_INTERVAL_SECONDS", 0.01),
            patch.object(adapter, "_STREAM_DRAFT_INTERVAL_SECONDS", 0.01),
        ):
            result = asyncio.run(run())

        self.assertIs(result, final_view)
        self.assertEqual(len(progress.started_snapshot()), 1)
        self.assertEqual(progress.completed_snapshot(), [])
