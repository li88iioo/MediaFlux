from __future__ import annotations

import json
import unittest

from app.agent.kernel.adapters import TurnViewBuilder, consume_events, iter_ndjson
from app.agent.kernel.events import AgentEvent, AgentEventType, EventFactory


async def event_stream(events):
    for event in events:
        yield event


async def append_event(target, event):
    target.append(event)


class AgentKernelAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_and_telegram_consume_the_same_event_truth(self) -> None:
        factory = EventFactory(session_id="s1", turn_id="t1", request_id="r1")
        events = [
            factory.create(AgentEventType.TURN_STARTED, {"channel": "test"}),
            factory.create(AgentEventType.MODEL_STARTED, {"round": 1}),
            factory.create(AgentEventType.MODEL_DELTA, {"delta": "共有 37 集"}),
            factory.create(
                AgentEventType.TURN_COMPLETED,
                {"status": "success", "answer": "共有 37 集"},
            ),
        ]
        observed: list[AgentEvent] = []
        telegram_view = await consume_events(
            event_stream(events),
            observe=lambda event: append_event(observed, event),
        )
        web_lines = [line async for line in iter_ndjson(event_stream(events))]
        web_events = [json.loads(line) for line in web_lines]

        self.assertEqual(
            [item.event_id for item in observed],
            [item["event_id"] for item in web_events],
        )
        self.assertEqual(telegram_view.status, "success")
        self.assertEqual(telegram_view.answer, "共有 37 集")
        self.assertEqual(telegram_view.event_count, len(events))

    async def test_approval_is_a_channel_neutral_structured_view(self) -> None:
        factory = EventFactory(session_id="s2", turn_id="t2", request_id="r2")
        events = [
            factory.create(AgentEventType.TURN_STARTED),
            factory.create(
                AgentEventType.MODEL_TOOL_CALL,
                {"call_id": "c1", "tool": "download.pause", "arguments": {}},
            ),
            factory.create(
                AgentEventType.EFFECT_APPROVAL_REQUIRED,
                {
                    "tool": "download.pause",
                    "plan": {
                        "plan_id": "plan-1",
                        "tool_name": "download.pause",
                        "effect": "WRITE",
                        "preview": {"summary": "暂停 1 个下载任务"},
                        "expires_at": "2026-09-03T22:00:00+00:00",
                    },
                    "result": {"summary": "等待确认"},
                },
            ),
            factory.create(
                AgentEventType.TURN_COMPLETED,
                {"status": "approval_required", "plan_id": "plan-1"},
            ),
        ]

        view = await consume_events(event_stream(events))
        self.assertEqual(view.status, "approval_required")
        self.assertIsNotNone(view.approval)
        assert view.approval is not None
        self.assertEqual(view.approval.plan_id, "plan-1")
        self.assertEqual(view.approval.preview["summary"], "暂停 1 个下载任务")
        self.assertEqual(view.tool_calls, ("download.pause",))

    async def test_terminal_view_only_exposes_approval_when_awaiting_confirmation(self) -> None:
        for event_type, payload in (
            (AgentEventType.TURN_CANCELLED, {"reason": "user_cancelled"}),
            (AgentEventType.TURN_FAILED, {"message": "检查点失败"}),
            (AgentEventType.TURN_COMPLETED, {"status": "partial", "answer": "未完成"}),
            (AgentEventType.TURN_COMPLETED, {"status": "success", "answer": "已完成"}),
        ):
            with self.subTest(terminal=event_type, payload=payload):
                factory = EventFactory(session_id="s", turn_id="t", request_id="r")
                receipt = {"ok": True, "summary": "前一步已完成"}
                view = await consume_events(event_stream([
                    factory.create(AgentEventType.TURN_STARTED),
                    factory.create(AgentEventType.EFFECT_COMPLETED, {"result": receipt}),
                    factory.create(AgentEventType.EFFECT_APPROVAL_REQUIRED, {"plan": {"plan_id": "next-plan"}}),
                    factory.create(event_type, payload),
                ]))
                self.assertIsNone(view.approval)
                self.assertEqual(view.effect_result, receipt)

    async def test_tool_failure_is_not_terminal_when_model_recovers(self) -> None:
        factory = EventFactory(session_id="s3", turn_id="t3", request_id="r3")
        events = [
            factory.create(AgentEventType.TURN_STARTED),
            factory.create(
                AgentEventType.TOOL_FAILED,
                {
                    "tool": "resource.search",
                    "code": "provider_timeout",
                    "message": "超时",
                },
            ),
            factory.create(
                AgentEventType.TURN_COMPLETED,
                {"status": "success", "answer": "已改用备用来源完成查询"},
            ),
        ]
        view = await consume_events(event_stream(events))
        self.assertEqual(view.status, "success")
        self.assertEqual(view.answer, "已改用备用来源完成查询")
        self.assertEqual(view.error_code, "")
        self.assertEqual(view.error_message, "")

    async def test_partial_completion_is_terminal_and_does_not_leak_tool_failure(self) -> None:
        factory = EventFactory(session_id="partial", turn_id="turn", request_id="request")
        events = [
            factory.create(AgentEventType.TURN_STARTED),
            factory.create(AgentEventType.TOOL_FAILED, {"code": "tool_budget_exceeded", "message": "未执行"}),
            factory.create(AgentEventType.TURN_COMPLETED, {"status": "partial", "answer": "已核对两部，其余可继续。"}),
        ]
        view = await consume_events(event_stream(events))
        self.assertTrue(view.terminal)
        self.assertEqual(view.status, "partial")
        self.assertEqual(view.error_code, "")
        self.assertEqual(view.error_message, "")
        self.assertIn("其余可继续", view.answer)

    async def test_step_receipt_without_turn_terminal_is_incomplete_not_success(self):
        factory = EventFactory(session_id="s", turn_id="t", request_id="r")
        result = {"ok": True, "summary": "目录已移动"}
        events = [factory.create(AgentEventType.TURN_STARTED), factory.create(AgentEventType.EFFECT_COMPLETED, {"result": result})]
        builder = TurnViewBuilder()
        for event in events:
            builder.apply(event)
        self.assertFalse(builder.build().terminal)
        view = await consume_events(event_stream(events))
        self.assertEqual(view.status, "partial")
        self.assertEqual(view.effect_result, result)
        self.assertIn("尚未确认", view.answer)
        events.append(factory.create(AgentEventType.TURN_COMPLETED, {"status": "effect_completed"}))
        terminal = await consume_events(event_stream(events))
        self.assertEqual(terminal.status, "effect_completed")
        self.assertFalse(terminal.error_code)

    def test_rejects_mixed_turns(self) -> None:
        builder = TurnViewBuilder()
        builder.apply(AgentEvent(AgentEventType.TURN_STARTED, "s", "t", "r", 1))
        with self.assertRaises(ValueError):
            builder.apply(
                AgentEvent(AgentEventType.MODEL_STARTED, "s", "other", "r", 2)
            )
