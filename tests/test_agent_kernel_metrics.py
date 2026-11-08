from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.agent.kernel.events import AgentEventType, EventFactory
from app.agent.kernel.metrics import KernelMetrics


COMPLETE_TASK = {
    "v": 1,
    "task_id": "task-1",
    "epoch": 1,
    "revision": 1,
    "acceptance_scope": "declared_obligations",
    "status": "complete",
    "root_goal": "检查状态",
    "obligations": [
        {"id": "check", "description": "检查状态", "required": True, "state": "verified"}
    ],
    "remaining_obligation_ids": [],
    "blockers": [],
}


async def stream(events, error=None):
    for event in events:
        await asyncio.sleep(0)
        yield event
    if error is not None:
        raise error


class AgentKernelMetricsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.factory = EventFactory(session_id="s", turn_id="t", request_id="r")
        self.started = self.factory.create(AgentEventType.TURN_STARTED)
        self.completed = self.factory.create(
            AgentEventType.TURN_COMPLETED,
            {"status": "success", "task": COMPLETE_TASK},
        )

    def assert_incomplete_turn(self, metrics, status, *, request_id="r"):
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["turns"], 1)
        self.assertEqual(snapshot["statuses"], {status: 1})
        self.assertEqual(snapshot["task_statuses_by_turn"], {})
        self.assertEqual(len(snapshot["recent"]), 1)
        recent = snapshot["recent"][0]
        self.assertEqual(recent["status"], status)
        self.assertEqual(recent["turn_status"], status)
        self.assertEqual(recent["task_status"], "unknown")
        self.assertEqual(recent["request_id"], request_id)
        return recent

    async def test_metrics_are_derived_from_real_events_without_user_content(
        self,
    ) -> None:
        factory = EventFactory(session_id="s", turn_id="t", request_id="r")
        events = [
            factory.create(AgentEventType.TURN_STARTED, {"channel": "web"}),
            factory.create(AgentEventType.MODEL_STARTED, {"round": 1}),
            factory.create(AgentEventType.TOOL_STARTED, {"tool": "library.search"}),
            factory.create(AgentEventType.TOOL_COMPLETED, {"tool": "library.search"}),
            factory.create(AgentEventType.MODEL_STARTED, {"round": 2}),
            factory.create(
                AgentEventType.MODEL_DELTA, {"delta": "敏感用户正文不应进入指标"}
            ),
            factory.create(
                AgentEventType.TURN_COMPLETED, {"status": "success", "answer": "完成"}
            ),
        ]
        metrics = KernelMetrics()
        observed = [
            event async for event in metrics.track(stream(events), channel="web")
        ]
        self.assertEqual(observed, events)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["detached_deliveries"], 0)
        self.assertEqual(snapshot["turns"], 1)
        self.assertEqual(snapshot["statuses"], {"success": 1})
        self.assertEqual(snapshot["average_model_calls"], 2.0)
        self.assertEqual(snapshot["average_tool_calls"], 1.0)
        self.assertNotIn("敏感用户正文", str(snapshot))

    async def test_normal_terminal_statuses_and_task_classifications_are_preserved(self):
        for event_type, payload, status, task_status in (
            (AgentEventType.TURN_COMPLETED, {"task": COMPLETE_TASK}, "success", "complete"),
            (AgentEventType.TURN_COMPLETED, {"status": "partial"}, "partial", "untracked"),
            (AgentEventType.TURN_COMPLETED, {"status": "waiting"}, "waiting", "untracked"),
            (AgentEventType.TURN_COMPLETED, {"status": "approval_required"}, "approval_required", "untracked"),
            (AgentEventType.TURN_COMPLETED, {"task": {}}, "success", "unknown"),
            (AgentEventType.TURN_FAILED, {"task": COMPLETE_TASK}, "failed", "complete"),
            (AgentEventType.TURN_CANCELLED, {"task": COMPLETE_TASK}, "cancelled", "complete"),
        ):
            with self.subTest(event_type=event_type, payload=payload):
                terminal = self.factory.create(event_type, payload)
                metrics = KernelMetrics()
                tracked = metrics.track(stream([terminal]), channel="web")
                self.assertEqual([event async for event in tracked], [terminal])
                snapshot = metrics.snapshot()
                self.assertEqual(snapshot["turns"], 1)
                self.assertEqual(snapshot["statuses"], {status: 1})
                self.assertEqual(snapshot["task_statuses_by_turn"], {task_status: 1})
                self.assertEqual(snapshot["recent"][0]["task_status"], task_status)
                await tracked.aclose()
                await tracked.aclose()
                self.assertEqual(metrics.snapshot(), snapshot)

    async def test_empty_and_unterminated_streams_are_interrupted(self):
        for events in ([], [self.started]):
            with self.subTest(events=events):
                metrics = KernelMetrics()
                tracked = metrics.track(stream(events), channel="web")
                self.assertEqual([event async for event in tracked], events)
                recent = self.assert_incomplete_turn(
                    metrics, "interrupted", request_id="r" if events else ""
                )
                self.assertIsNone(recent["ttft_ms"])
                await tracked.aclose()
                self.assertEqual(metrics.snapshot()["turns"], 1)

    async def test_only_last_terminal_task_snapshot_is_counted_per_turn(self):
        last = self.factory.create(AgentEventType.TURN_COMPLETED, {"status": "partial"})
        metrics = KernelMetrics(history_size=20)
        for _ in range(25):
            _ = [
                event
                async for event in metrics.track(
                    stream([self.started, self.completed, last]), channel="web"
                )
            ]
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["turns"], 25)
        self.assertEqual(snapshot["statuses"], {"partial": 25})
        self.assertEqual(snapshot["task_statuses_by_turn"], {"untracked": 25})
        self.assertEqual(len(snapshot["recent"]), 20)
        self.assertTrue(all(item["task_status"] == "untracked" for item in snapshot["recent"]))
        self.assertIn("正常耗尽", snapshot["task_statuses_by_turn_unit"])

    async def test_upstream_exceptions_propagate_and_record_once(self):
        for error_type, status in (
            (RuntimeError, "failed"),
            (ValueError, "failed"),
            (asyncio.CancelledError, "cancelled"),
            (GeneratorExit, "interrupted"),
        ):
            for events in ([], [self.started], [self.started, self.completed]):
                with self.subTest(error_type=error_type, events=events):
                    error = error_type("upstream interrupted")
                    metrics = KernelMetrics()
                    tracked = metrics.track(stream(events, error), channel="web")
                    observed = []
                    with self.assertRaises(error_type) as raised:
                        async for event in tracked:
                            observed.append(event)
                    self.assertIs(raised.exception, error)
                    self.assertEqual(observed, events)
                    self.assert_incomplete_turn(
                        metrics, status, request_id="r" if events else ""
                    )
                    snapshot = metrics.snapshot()
                    await tracked.aclose()
                    await tracked.aclose()
                    with self.assertRaises(StopAsyncIteration):
                        await anext(tracked)
                    self.assertEqual(metrics.snapshot(), snapshot)

    async def test_early_close_records_once_without_accepting_partial_terminal(self):
        for events in ([self.started], [self.started, self.completed]):
            with self.subTest(events=events):
                metrics = KernelMetrics()
                source = stream(events)
                tracked = metrics.track(source, channel="web")
                for event in events:
                    self.assertIs(await anext(tracked), event)
                self.assertEqual(metrics.snapshot()["turns"], 0)
                await tracked.aclose()
                self.assert_incomplete_turn(metrics, "interrupted")
                snapshot = metrics.snapshot()
                await tracked.aclose()
                await tracked.aclose()
                with self.assertRaises(StopAsyncIteration):
                    await anext(tracked)
                self.assertEqual(metrics.snapshot(), snapshot)
                await source.aclose()

    async def test_closing_unstarted_tracker_does_not_invent_a_turn(self):
        metrics = KernelMetrics()
        tracked = metrics.track(stream([self.started]), channel="web")
        snapshot = metrics.snapshot()
        await tracked.aclose()
        await tracked.aclose()
        self.assertEqual(metrics.snapshot(), snapshot)

    async def test_consumer_thrown_exception_or_cancel_propagates(self):
        for error_type, status in (
            (RuntimeError, "failed"),
            (asyncio.CancelledError, "cancelled"),
            (GeneratorExit, "interrupted"),
        ):
            with self.subTest(error_type=error_type):
                metrics = KernelMetrics()
                source = stream([self.completed])
                tracked = metrics.track(source, channel="web")
                self.assertIs(await anext(tracked), self.completed)
                error = error_type("consumer interrupted")
                with self.assertRaises(error_type) as raised:
                    await tracked.athrow(error)
                self.assertIs(raised.exception, error)
                self.assert_incomplete_turn(metrics, status)
                snapshot = metrics.snapshot()
                await tracked.aclose()
                await tracked.aclose()
                self.assertEqual(metrics.snapshot(), snapshot)
                await source.aclose()

    async def test_consumer_task_cancellation_during_upstream_wait_records_once(self):
        for events in ([], [self.started], [self.started, self.completed]):
            with self.subTest(events=events):
                waiting, closed = asyncio.Event(), asyncio.Event()

                async def source():
                    try:
                        for event in events:
                            yield event
                        waiting.set()
                        await asyncio.Event().wait()
                    finally:
                        closed.set()

                metrics = KernelMetrics()
                tracked = metrics.track(source(), channel="web")

                async def consume():
                    return [event async for event in tracked]

                consumer = asyncio.create_task(consume())
                await asyncio.wait_for(waiting.wait(), 1)
                self.assertEqual(metrics.snapshot()["turns"], 0)
                consumer.cancel("consumer cancelled")
                with self.assertRaises(asyncio.CancelledError) as raised:
                    await consumer
                self.assertEqual(raised.exception.args, ("consumer cancelled",))
                self.assertTrue(consumer.cancelled())
                self.assertTrue(closed.is_set())
                self.assert_incomplete_turn(
                    metrics, "cancelled", request_id="r" if events else ""
                )
                snapshot = metrics.snapshot()
                await tracked.aclose()
                await tracked.aclose()
                self.assertEqual(metrics.snapshot(), snapshot)

    async def test_incomplete_turn_preserves_call_counts_and_observed_latency(self):
        events = [
            self.started,
            self.factory.create(AgentEventType.MODEL_STARTED),
            self.factory.create(AgentEventType.TOOL_STARTED),
            self.factory.create(AgentEventType.MODEL_DELTA, {"delta": "private text"}),
        ]
        metrics = KernelMetrics()
        with patch("app.agent.kernel.metrics.time") as clock:
            clock.monotonic.side_effect = [10, 10.125, 10.25, 10.375, 10.5, 10.75]
            with self.assertRaisesRegex(RuntimeError, "private upstream error"):
                _ = [
                    event
                    async for event in metrics.track(
                        stream(events, RuntimeError("private upstream error")), channel="web"
                    )
                ]
        recent = self.assert_incomplete_turn(metrics, "failed")
        self.assertEqual(recent["ttfe_ms"], 125)
        self.assertEqual(recent["ttft_ms"], 500)
        self.assertEqual(recent["total_ms"], 750)
        self.assertEqual(recent["model_calls"], 1)
        self.assertEqual(recent["tool_calls"], 1)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["latency_ms"]["total_p50"], 750)
        self.assertEqual(snapshot["average_model_calls"], 1.0)
        self.assertEqual(snapshot["average_tool_calls"], 1.0)
        self.assertNotIn("private", str(snapshot))
