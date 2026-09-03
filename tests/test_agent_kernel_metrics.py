from __future__ import annotations

import asyncio
import unittest

from app.agent.kernel.events import AgentEventType, EventFactory
from app.agent.kernel.metrics import KernelMetrics


async def stream(events):
    for event in events:
        await asyncio.sleep(0)
        yield event


class AgentKernelMetricsTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(snapshot["turns"], 1)
        self.assertEqual(snapshot["statuses"], {"success": 1})
        self.assertEqual(snapshot["average_model_calls"], 2.0)
        self.assertEqual(snapshot["average_tool_calls"], 1.0)
        self.assertNotIn("敏感用户正文", str(snapshot))
