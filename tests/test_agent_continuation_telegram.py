"""后台续行 Telegram 边缘：原生事件/按钮 + 隔离 outbox，无真实发信。"""
from __future__ import annotations
from starlette.requests import Request
from app.agent.kernel.session import AgentSession
from typing import cast

import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

import requests

from app import database as db
from app.agent.kernel.adapters import ApprovalView, consume_events
from app.agent.kernel.events import AgentEventType, EventFactory
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.transports import (
    EffectEnvelope,
    ExplainEnvelope,
    QueryEnvelope,
    TelegramKernelTransport,
    TransportInputError,
    WebKernelTransport,
)
from app.bot import agent_adapter as adapter
from app.bot.telegram_markdown import telegram_html_text_length
from app.modules import telegram_notification_center as center
from app.modules.telegram_topic_routing import (
    create_session_callback_route,
    resolve_session_callback_route,
)
from app.notifier import NotificationEvent
from app.repositories.telegram_notifications import (
    claim_due_notifications,
    complete_notification,
    purge_notifications,
    recover_notifications,
)
from tests.support import isolated_test_database
from tests.test_agent_kernel_telegram_adapter import TELEBOT, Call, FakeBot, Message

OWNER = "tg:v1:-100\x1f7"
SESSION = "tg_continuation_fixture"
PLAN = "plan_1234567890abcdef"
ROUTE = {"chat_id": "-100", "message_thread_id": 42}


class TelegramAPIErrorFixture(Exception):
    def __init__(self, code):
        super().__init__("fixture: Telegram rejected delivery")
        self.error_code = code
        self.result_json = {"error_code": code, "description": "fixture delivery error"}


class NativeBotFixture(FakeBot):
    """替换仅 Bot I/O；真实 renderer、TelegramSendResult 和 SDK markup 不替换。"""

    def __init__(self):
        super().__init__()
        self.failure = None
        self.attempts = []

    def send_message(self, chat_id, text, **kwargs):
        self.attempts.append((chat_id, text, kwargs))
        if self.failure is not None:
            raise self.failure
        return super().send_message(chat_id, text, **kwargs)


class NativeEventSessionFixture:
    cancel_effect: AsyncMock
    """只替代模型/核心驱动，入口仍用真实 TelegramKernelTransport/consume_events。"""

    def __init__(self):
        self.last_input = None
        self.last_confirmation = None
        self.query_approval = False
        self.pipeline = types.SimpleNamespace(
            get_pending_plan=AsyncMock(return_value=types.SimpleNamespace(plan_id=PLAN)),
        )

    async def run(self, agent_input):
        self.last_input = agent_input
        factory = EventFactory(
            session_id=agent_input.session_id, turn_id="turn-native-query", request_id=agent_input.request_id,
        )
        yield factory.create(AgentEventType.TURN_STARTED)
        if self.query_approval:
            yield factory.create(AgentEventType.EFFECT_APPROVAL_REQUIRED, {"plan": {
                "plan_id": PLAN, "tool_name": "media.subscribe", "effect": "write",
                "preview": {"summary": "候选已预检"}, "expires_at": "2099-01-01T00:00:00+00:00",
            }})
        yield factory.create(
            AgentEventType.TURN_COMPLETED,
            status="approval_required" if self.query_approval else "success",
            answer="**续行结果**：处理完成。",
        )

    async def confirm(self, **kwargs):
        self.last_confirmation = kwargs
        factory = EventFactory(
            session_id=kwargs["session_id"], turn_id="turn-native-confirm", request_id=kwargs["request_id"],
        )
        yield factory.create(AgentEventType.TURN_STARTED)
        yield factory.create(AgentEventType.TURN_COMPLETED, status="success", answer="确认已受理。")


class AgentContinuationTelegramTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database("continuation-telegram.db"))
        self.bot = NativeBotFixture()
        self.session = NativeEventSessionFixture()
        self.runtime = types.SimpleNamespace(
            session=self.session,
            telegram=TelegramKernelTransport(cast(AgentSession, self.session)),
            store=types.SimpleNamespace(load=AsyncMock(return_value=types.SimpleNamespace(pending_effect_plan_id=PLAN))),
        )
        self.values = {"TG_CHAT_ID": "-100", "TG_AGENT_ALLOWED_USER_IDS": "7",
                       "AGENT_LLM_ENABLED": "1", "TG_AGENT_ENABLED": "1"}
        self.enterContext(patch.object(adapter.config, "get", side_effect=lambda key, default="": self.values.get(key, default)))
        self.enterContext(patch.object(center, "allows_notification", return_value=False))
        self.enterContext(patch.object(center, "wake_telegram_notification_dispatcher", return_value=False))
        self.enterContext(patch.object(center, "notification_target_chat_id", side_effect=lambda chat_id="": chat_id))
        self.enterContext(patch("app.notifier.get_bot", return_value=self.bot))
        self.enterContext(patch.object(adapter, "get_agent_kernel_runtime", return_value=self.runtime))
        self.enterContext(patch.object(adapter.agent_rate_limiter, "allow", return_value=True))
        self.enterContext(patch("app.modules.telegram_model_preferences.get_telegram_model_preference", return_value=""))
        was_stopped = center._dispatch_stop.is_set()
        center._dispatch_stop.clear()
        self.addCleanup(center._dispatch_stop.set if was_stopped else center._dispatch_stop.clear)
        _, state = asyncio.run(SQLiteKernelStore().begin_turn(owner=OWNER, session_id=SESSION, request_id="delivery-fixture"))
        self.lifecycle_id = state.lifecycle_id

    def rows(self):
        with db.get_conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM telegram_notification_outbox ORDER BY id")]

    def view(self, *, approval=False, answer="**续行结果**：处理完成。", status="success"):
        async def events():
            factory = EventFactory(session_id=SESSION, turn_id="turn-durable", request_id="resume-fixture")
            yield factory.create(AgentEventType.TURN_STARTED)
            if approval:
                card = ApprovalView(
                    PLAN, "media.subscribe", "write", {"summary": "将创建新的订阅"}, {},
                    "2099-01-01T00:00:00+00:00",
                )
                yield factory.create(AgentEventType.EFFECT_APPROVAL_REQUIRED, {"plan": card.to_dict(), "result": {}})
            yield factory.create(
                AgentEventType.TURN_COMPLETED,
                status="approval_required" if approval else status,
                answer=answer,
            )

        return asyncio.run(consume_events(events())).to_dict()

    def prepare(self, **kwargs):
        return adapter.prepare_agent_continuation_delivery(
            self.view(**kwargs), owner=OWNER, session_id=SESSION, delivery_route=ROUTE,
            delivery_id="original-continuation-claim",
            lifecycle_id=self.lifecycle_id,
        )

    def publish(self, payload):
        return asyncio.run(adapter.publish_agent_continuation_delivery(payload))

    def test_query_extracts_physical_message_route_not_model_reply_context(self):
        message = Message("继续检查", chat_id=-100, user_id=7)
        message.message_thread_id = 42
        message.reply_to_message = Message("模型引用", chat_id=-999, user_id=0)
        message.reply_to_message.message_thread_id = 999
        adapter._execute_query(self.bot, TELEBOT, message, chat_id="-100", user_id="7", text=message.text)
        assert self.session.last_input is not None
        self.assertEqual(self.session.last_input.delivery_route, ROUTE)
        self.assertNotIn("delivery_route", self.session.last_input.reply_context)

    def test_confirm_extracts_callback_message_physical_route(self):
        message = Message("冻结计划", chat_id=-100, user_id=0, message_id=91)
        message.message_thread_id = 42
        token = create_session_callback_route(OWNER, SESSION, 42)
        adapter.handle_agent_callback(self.bot, Call(f"agk:c:{token}:{PLAN}", message), TELEBOT)
        assert self.session.last_confirmation is not None
        self.assertEqual(self.session.last_confirmation["delivery_route"], ROUTE)
        self.assertEqual(self.session.last_confirmation["session_id"], SESSION)

    def test_resource_selection_preview_preserves_physical_route_into_new_approval(self):
        from app.bot import agent_candidates

        self.session.query_approval = True
        message = Message("资源选择", chat_id=-100, user_id=0, message_id=92)
        message.message_thread_id = 42
        message.reply_to_message = Message("不可信引用", chat_id=-999, user_id=0)
        message.reply_to_message.message_thread_id = 999
        handle = "ref_" + "a" * 24
        view = {"selection_ref": "ref_" + "b" * 24, "targets": [{"value": "qb", "available": True}]}
        draft = {"handle": handle, "phase": "select", "positions": [1], "target": "qb"}
        with patch("app.agent.kernel.bootstrap.get_agent_kernel_runtime", return_value=self.runtime), patch.object(
            agent_candidates, "load_draft", new=AsyncMock(return_value=(view, draft)),
        ), patch.object(
            agent_candidates, "save_draft", new=AsyncMock(side_effect=lambda *args, **kwargs: kwargs["draft"]),
        ):
            agent_candidates.handle_callback(
                self.bot, Call(f"agk:s:{handle}:p", message), TELEBOT,
                owner=OWNER, session_id=SESSION, message_thread_id=42,
            )
        assert self.session.last_input is not None
        self.assertEqual(self.session.last_input.delivery_route, ROUTE)
        self.assertEqual(self.session.last_input.metadata["selection"]["positions"], [1])
        self.assertTrue(any("候选已预检" in text for text, *_ in self.bot.edits))
        markups = [kwargs["reply_markup"] for *_, kwargs in self.bot.edits if kwargs.get("reply_markup")]
        self.assertEqual([button.text for button in markups[-1].buttons], ["确认执行", "取消", "只解释影响"])

    def test_envelopes_validate_owner_channel_and_physical_ids(self):
        invalid = [
            {"chat_id": "-999"}, {"chat_id": False}, {"chat_id": 0},
            {"chat_id": "-100", "message_thread_id": True},
            {"chat_id": "-100", "message_thread_id": 0},
            {"chat_id": "-100", "message_thread_id": -1},
            {"chat_id": "-100", "message_thread_id": 1.5},
            {"chat_id": "-100", "message_thread_id": "42x"},
            {"chat_id": "-100", "message_thread_id": "9" * 20},
            {"chat_id": "-100", "reply_context": {}},
        ]
        for route in invalid:
            with self.subTest(route=route):
                with self.assertRaises(TransportInputError):
                    QueryEnvelope(OWNER, SESSION, "任务", channel="telegram", delivery_route=route).to_agent_input()
                with self.assertRaises(TransportInputError):
                    EffectEnvelope(OWNER, SESSION, PLAN, channel="telegram", delivery_route=route).normalized()
        for channel in ("web", "api", "test"):
            with self.subTest(channel=channel), self.assertRaises(TransportInputError):
                QueryEnvelope(OWNER, SESSION, "任务", channel=channel, delivery_route=ROUTE).to_agent_input()

    def test_web_transport_never_forwards_delivery_route(self):
        web = WebKernelTransport(cast(AgentSession, self.session))
        asyncio.run(web.query_view(QueryEnvelope(OWNER, SESSION, "任务", channel="telegram", delivery_route=ROUTE)))
        assert self.session.last_input is not None
        self.assertEqual(self.session.last_input.delivery_route, {})
        asyncio.run(web.confirm_view(EffectEnvelope(OWNER, SESSION, PLAN, channel="telegram", delivery_route=ROUTE)))
        assert self.session.last_confirmation is not None
        self.assertEqual(self.session.last_confirmation["delivery_route"], {})
        self.assertEqual(self.session.last_confirmation["channel"], "web")

    def test_http_payload_cannot_inject_delivery_route(self):
        from app.routes import agent_api

        for endpoint, payload in (
            (agent_api.query, {"message": "任务", "session_id": SESSION}),
            (agent_api.confirm_action, {"plan_id": PLAN, "session_id": SESSION}),
        ):
            with self.subTest(endpoint=endpoint.__name__), patch.object(agent_api, "require_api_login"), patch.object(agent_api, "get_agent_kernel_runtime") as runtime:
                response = asyncio.run(endpoint(cast(Request, None), {**payload, "delivery_route": ROUTE}))
                self.assertEqual(response.status_code, 400)
                runtime.assert_not_called()

    def test_explain_normalization_does_not_inject_confirmation_only_argument(self):
        self.assertEqual(ExplainEnvelope(OWNER, SESSION, PLAN, "解释", channel="telegram").normalized().message, "解释")

    def test_json_safe_payload_only_enqueues_and_replay_is_idempotent(self):
        payload = json.loads(json.dumps(self.prepare(), ensure_ascii=False))
        result = self.publish(payload)
        self.assertTrue(result.accepted)
        self.assertTrue(result.queued)
        self.assertFalse(result.delivered)
        self.assertEqual(self.bot.attempts, [])
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(self.publish(payload).accepted)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(center.drain_telegram_notifications())
        chat, body, kwargs = self.bot.sent[-1]
        self.assertEqual((chat, kwargs["message_thread_id"]), ("-100", 42))
        self.assertIn("<b>续行结果</b>", body)
        self.assertIsNone(kwargs["reply_markup"])
        self.assertTrue(self.publish(payload).delivered)
        center.drain_telegram_notifications()
        self.assertEqual(len(self.bot.sent), 1)

    def test_restart_uses_persisted_route_and_html_without_replaying_model(self):
        payload = self.prepare()
        self.assertTrue(self.publish(payload).accepted)
        self.values["TG_CHAT_ID"] = "-999"
        # 地址授权撤销不能转投配置的新 chat；记录保留可诊断的失败。
        center.drain_telegram_notifications()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.rows()[0]["status"], "failed")
        self.assertEqual(self.rows()[0]["last_error"], "AgentDeliveryUnauthorized")
        self.assertIsNone(self.session.last_input)

    def test_valid_approval_sends_native_buttons_bound_to_owner_session_and_topic(self):
        self.publish(self.prepare(approval=True))
        self.assertTrue(center.drain_telegram_notifications())
        _, _, kwargs = self.bot.sent[-1]
        buttons = [button for row in kwargs["reply_markup"].keyboard for button in row]
        self.assertEqual([button.text for button in buttons], ["确认执行", "取消", "只解释影响"])
        for button in buttons:
            match = adapter._CALLBACK_RE.fullmatch(button.callback_data)
            assert match is not None
            self.assertEqual(match.group("plan"), PLAN)
            self.assertEqual(resolve_session_callback_route(OWNER, match.group("route"), 42), SESSION)
            self.assertEqual(resolve_session_callback_route(OWNER, match.group("route"), 43), "")
            self.assertEqual(resolve_session_callback_route("tg:v1:-100\x1f8", match.group("route"), 42), "")
        self.session.pipeline.get_pending_plan.assert_awaited_once_with(owner=OWNER, session_id=SESSION)

    def test_non_topic_approval_uses_exact_session_not_session_hash_inference(self):
        payload = adapter.prepare_agent_continuation_delivery(
            self.view(approval=True), owner=OWNER, session_id=SESSION,
            delivery_route={"chat_id": -100}, delivery_id="plain-chat-claim",
            lifecycle_id=self.lifecycle_id,
        )
        self.publish(payload)
        center.drain_telegram_notifications()
        kwargs = self.bot.sent[-1][2]
        self.assertNotIn("message_thread_id", kwargs)
        button = kwargs["reply_markup"].keyboard[0][0]
        match = adapter._CALLBACK_RE.fullmatch(button.callback_data)
        assert match is not None
        self.assertEqual(resolve_session_callback_route(OWNER, match.group("route"), None), SESSION)

    def test_next_private_approval_keeps_explicit_session_and_can_be_cancelled(self):
        next_plan = "plan_abcdef1234567890"

        async def confirm(**kwargs):
            self.session.last_confirmation = kwargs
            factory = EventFactory(session_id=kwargs["session_id"], turn_id="next-card", request_id=kwargs["request_id"])
            yield factory.create(AgentEventType.TURN_STARTED)
            yield factory.create(AgentEventType.EFFECT_APPROVAL_REQUIRED, {"plan": {
                "plan_id": next_plan, "tool_name": "media.subscribe", "effect": "write",
                "preview": {"summary": "下一项需确认"}, "expires_at": "2099-01-01T00:00:00+00:00",
            }})
            yield factory.create(AgentEventType.TURN_COMPLETED, status="approval_required", answer="第一项已完成。")

        self.session.confirm = confirm
        self.session.cancel_effect = AsyncMock(return_value=True)
        route = create_session_callback_route(OWNER, SESSION, None)
        message = Message("第一张确认卡", user_id=0)
        adapter.handle_agent_callback(self.bot, Call(f"agk:c:{route}:{PLAN}", message), TELEBOT)
        assert self.session.last_confirmation is not None
        self.assertEqual(self.session.last_confirmation["session_id"], SESSION)
        markup = self.bot.edits[-1][3]["reply_markup"]
        cancel = next(b for b in markup.buttons if b.callback_data.startswith("agk:x:"))
        match = adapter._CALLBACK_RE.fullmatch(cancel.callback_data)
        assert match is not None
        self.assertTrue(match.group("route"), "后续确认卡丢失了显式会话路由")
        self.assertEqual(resolve_session_callback_route(OWNER, match.group("route"), None), SESSION)
        self.runtime.store.load.return_value.pending_effect_plan_id = next_plan
        adapter.handle_agent_callback(self.bot, Call(cancel.callback_data, message), TELEBOT)
        self.session.cancel_effect.assert_awaited_once_with(
            owner=OWNER, session_id=SESSION, plan_id=next_plan, request_id="tgcb_callback-1",
        )
        self.assertIsNone(self.bot.edits[-1][3]["reply_markup"])

    def test_expired_cancelled_or_replaced_plan_sends_fact_without_old_buttons(self):
        for current in (None, types.SimpleNamespace(plan_id="new-plan")):
            with self.subTest(current=current):
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM telegram_notification_outbox")
                self.session.pipeline.get_pending_plan.return_value = current
                self.publish(self.prepare(approval=True))
                center.drain_telegram_notifications()
                _, body, kwargs = self.bot.sent[-1]
                self.assertIsNone(kwargs["reply_markup"])
                self.assertIn("将创建新的订阅", body)
                self.assertIn("仅为事实记录", body)
                self.assertNotIn("<b>等待确认</b>", body)

    def test_plan_state_error_is_retryable_not_silently_converted_to_fact(self):
        self.session.pipeline.get_pending_plan.side_effect = RuntimeError("fixture unavailable")
        self.publish(self.prepare(approval=True))
        self.assertFalse(center.drain_telegram_notifications())
        self.assertEqual(self.bot.attempts, [])
        self.assertEqual(self.rows()[0]["status"], "retry_wait")
        self.assertIn("AgentPlanUnavailable", self.rows()[0]["last_error"])

    def test_long_results_and_approval_keep_buttons_only_on_last_part(self):
        view = self.view(approval=True)
        view["approval"]["preview"] = {"summary": "长计划", "details": "长" * 12000}
        # _preview_lines 的原生安全投影限制卡片详情；使用普通长结果验证所有分段完整入队。
        payload = self.prepare(answer="分段正文。" * 3000)
        self.assertGreater(len(payload["parts"]), 1)
        self.publish(payload)
        center.drain_telegram_notifications(limit=100)
        self.assertEqual(len(self.bot.sent), len(payload["parts"]))
        for _, body, kwargs in self.bot.sent:
            self.assertLessEqual(telegram_html_text_length(body), 4096)
            self.assertIsNone(kwargs["reply_markup"])
        # 增加一段事实 fixture，验证多段确认交付只在最后一段携带按钮。
        approval_payload = adapter.prepare_agent_continuation_delivery(
            view, owner=OWNER, session_id=SESSION, delivery_route=ROUTE, delivery_id="long-approval",
            lifecycle_id=self.lifecycle_id,
        )
        approval_payload["parts"].insert(0, {"html": "<b>前段事实</b>"})
        self.assertIn("approval", approval_payload["parts"][-1])
        self.assertTrue(all("approval" not in part for part in approval_payload["parts"][:-1]))
        self.bot.sent.clear()
        self.publish(approval_payload)
        center.drain_telegram_notifications(limit=100)
        self.assertIsNone(self.bot.sent[0][2]["reply_markup"])
        assert (self.bot.sent[-1][2]["reply_markup"]) is not None

    def test_partial_persist_failure_retains_pending_and_retry_fills_missing_parts(self):
        payload = self.prepare(answer="长结果。" * 2000)
        original = center.upsert_notification
        count = 0

        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("fixture DB interruption")
            return original(*args, **kwargs)

        with patch.object(center, "upsert_notification", side_effect=fail_second):
            result = self.publish(payload)
        self.assertFalse(result.accepted)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(self.publish(payload).accepted)
        self.assertEqual(len(self.rows()), len(payload["parts"]))

    def test_known_send_failure_retries_persisted_result_after_restart(self):
        self.bot.failure = TelegramAPIErrorFixture(429)
        payload = self.prepare()
        self.publish(payload)
        self.assertFalse(center.drain_telegram_notifications())
        self.assertEqual(self.rows()[0]["status"], "retry_wait")
        self.assertIn("fixture delivery error", self.rows()[0]["last_error"])
        self.bot.failure = None
        with db.get_conn() as conn:
            conn.execute("UPDATE telegram_notification_outbox SET next_attempt_at='2000-01-01 00:00:00'")
        recover_notifications()
        self.assertTrue(center.drain_telegram_notifications())
        self.assertEqual(self.rows()[0]["status"], "sent")
        self.assertEqual(self.bot.sent[-1][2]["message_thread_id"], 42)
        self.assertIsNone(self.session.last_input)

    def test_permanent_send_rejection_retains_failed_payload_and_error(self):
        self.bot.failure = TelegramAPIErrorFixture(400)
        self.publish(self.prepare())
        self.assertFalse(center.drain_telegram_notifications())
        row = self.rows()[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("fixture delivery error", row["last_error"])
        self.assertIn("续行结果", row["event_json"])
        recover_notifications()
        center.drain_telegram_notifications()
        self.assertEqual(len(self.bot.attempts), 1)

    def test_segment_failure_blocks_later_buttons_without_blocking_other_results(self):
        for failure in (TelegramAPIErrorFixture(429), requests.ReadTimeout("unknown first-part result")):
            with self.subTest(failure=type(failure).__name__):
                identity = type(failure).__name__
                payload = self.prepare(approval=True)
                payload["delivery_id"] = identity
                first_body = f"<b>{identity}: required first part</b>"
                payload["parts"].insert(0, {"html": first_body})
                self.publish(payload)
                original_send = self.bot.send_message
                remaining = [failure]

                def send(chat_id, text, **kwargs):
                    self.bot.failure = remaining.pop() if text == first_body and remaining else None
                    try:
                        return original_send(chat_id, text, **kwargs)
                    finally:
                        self.bot.failure = None

                start = len(self.bot.sent)
                with patch.object(self.bot, "send_message", side_effect=send):
                    self.assertFalse(center.drain_telegram_notifications(limit=100))
                    self.assertEqual(self.bot.sent[start:], [], "a later confirmation card must not overtake failed facts")
                    independent = self.prepare(answer="unrelated result")
                    independent["delivery_id"] = identity + "-independent"
                    self.publish(independent)
                    center.drain_telegram_notifications(limit=100)
                    self.assertEqual(len(self.bot.sent), start + 1)
                    self.assertIn("unrelated result", self.bot.sent[-1][1])
                    self.assertIsNone(self.bot.sent[-1][2]["reply_markup"])
                    if isinstance(failure, requests.ReadTimeout):
                        recover_notifications()
                        center.drain_telegram_notifications(limit=100)
                        self.assertEqual(len(self.bot.sent), start + 1)
                    else:
                        with db.get_conn() as conn:
                            conn.execute("UPDATE telegram_notification_outbox SET next_attempt_at='2000-01-01 00:00:00' WHERE status='retry_wait'")
                        center.drain_telegram_notifications(limit=100)
                        self.assertEqual(len(self.bot.sent), start + 3)
                        self.assertEqual(self.bot.sent[-2][1], first_body)
                        self.assertIsNone(self.bot.sent[-2][2]["reply_markup"])
                        assert (self.bot.sent[-1][2]["reply_markup"]) is not None

    def test_existing_part_keys_are_claimed_in_numeric_order_across_workers(self):
        from concurrent.futures import ThreadPoolExecutor

        payload = self.prepare(approval=True)
        payload["parts"] = [{"html": f"fact {index}"} for index in range(12)] + payload["parts"]
        self.publish(payload)
        for index in range(13):
            with ThreadPoolExecutor(max_workers=2) as pool:
                claims = list(pool.map(lambda _: claim_due_notifications(limit=100), range(2)))
            claimed = [row for group in claims for row in group]
            self.assertEqual(len(claimed), 1, "one reply cannot have two in-flight segments")
            row = claimed[0]
            self.assertEqual(row["event_key"].rsplit(":", 2)[-2], str(index))
            self.assertNotIn("predecessor", row, "existing queue rows need no parallel ordering state")
            self.assertTrue(complete_notification(
                row["id"], lease_generation=row["lease_generation"],
                claimed_revision=row["revision"], message_id=700 + index,
            ))
        self.assertEqual(claim_due_notifications(limit=100), [])

    def test_tail_claim_cannot_bypass_unknown_failed_or_missing_prefix(self):
        payload = self.prepare(approval=True)
        payload["parts"].insert(0, {"html": "required prefix"})
        self.publish(payload)
        head, tail = self.rows()
        for status in ("pending", "retry_wait", "sending", "outcome_unknown", "failed", "suppressed"):
            with self.subTest(status=status):
                with db.get_conn() as conn:
                    conn.execute("UPDATE telegram_notification_outbox SET status=? WHERE id=?", (status, head["id"]))
                self.assertEqual(claim_due_notifications(event_key=tail["event_key"]), [])
        with db.get_conn() as conn:
            conn.execute("DELETE FROM telegram_notification_outbox WHERE id=?", (head["id"],))
        self.assertEqual(claim_due_notifications(event_key=tail["event_key"]), [], "absence cannot prove delivery")

    def test_purge_keeps_prefix_receipts_until_the_reply_has_no_pending_parts(self):
        payload = self.prepare(approval=True)
        payload["parts"].insert(0, {"html": "required prefix"})
        self.publish(payload)
        head = claim_due_notifications(limit=100)[0]
        self.assertTrue(complete_notification(head["id"], lease_generation=head["lease_generation"],
                                              claimed_revision=head["revision"], message_id=701))
        with db.get_conn() as conn:
            conn.execute("UPDATE telegram_notification_outbox SET updated_at='2000-01-01 00:00:00' WHERE id=?", (head["id"],))
        self.assertEqual(purge_notifications(retention_days=1), 0)
        tail = claim_due_notifications(limit=100)
        self.assertEqual(len(tail), 1)
        self.assertTrue(complete_notification(tail[0]["id"], lease_generation=tail[0]["lease_generation"],
                                              claimed_revision=tail[0]["revision"], message_id=702))
        self.assertEqual(purge_notifications(retention_days=1), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_missing_sdk_receipt_is_unknown_not_pretended_success(self):
        self.publish(self.prepare())
        with patch.object(self.bot, "send_message", return_value=None):
            self.assertFalse(center.drain_telegram_notifications())
        self.assertEqual(self.rows()[0]["status"], "outcome_unknown")
        self.assertEqual(self.rows()[0]["last_error"], "MissingTelegramReceipt")

    def test_unknown_send_outcome_is_retained_and_never_blindly_replayed(self):
        self.bot.failure = requests.ReadTimeout("fixture: response missing")
        payload = self.prepare()
        self.publish(payload)
        self.assertFalse(center.drain_telegram_notifications())
        self.assertEqual(self.rows()[0]["status"], "outcome_unknown")
        self.assertIn("ReadTimeout", self.rows()[0]["last_error"])
        self.assertEqual(self.publish(payload).status, "outcome_unknown")
        recover_notifications()
        center.drain_telegram_notifications()
        self.assertEqual(len(self.bot.attempts), 1)

    def test_inflight_crash_keeps_unknown_delivery_instead_of_dropping_result(self):
        self.publish(self.prepare())
        claim_due_notifications(limit=1)
        with db.get_conn() as conn:
            conn.execute("UPDATE telegram_notification_outbox SET updated_at='2000-01-01 00:00:00'")
        recover_notifications()
        self.assertEqual(self.rows()[0]["status"], "outcome_unknown")
        center.drain_telegram_notifications()
        self.assertEqual(self.bot.attempts, [])

    def test_late_interaction_receipt_keeps_safe_topic_route_in_existing_outbox(self):
        delivery = {"version": 1, "owner": OWNER, "session_id": SESSION, "delivery_id": PLAN,
                    "lifecycle_id": self.lifecycle_id, "delivery_route": ROUTE}
        key = center._effect_delivery_key(delivery)
        with patch.object(center, "send_event_result", return_value=types.SimpleNamespace(ok=True, message_id=99)) as send:
            result = center.publish_agent_interaction_reply(
                key, NotificationEvent("延迟回执"), delivery=delivery,
            )
            self.assertTrue(result.accepted)
            self.assertEqual(json.loads(self.rows()[0]["event_json"])["_delivery_route"], ROUTE)
            center.drain_telegram_notifications()
            self.assertEqual(send.call_args.kwargs, {"chat_id": "-100", "message_thread_id": 42})
        for thread in (False, 0, -1, "42x", 1.2):
            with self.subTest(thread=thread):
                result = center.publish_agent_interaction_reply(
                    key, NotificationEvent("回执"),
                    delivery={**delivery, "delivery_route": {"chat_id": "-100", "message_thread_id": thread}},
                )
                self.assertFalse(result.accepted)
                self.assertEqual(result.status, "invalid_delivery_route")

    def test_missing_physical_route_never_falls_back_to_hash_or_reply_context(self):
        view = self.view()
        view["reply_context"] = {"chat_id": -100, "message_thread_id": 42}
        for route in ({}, {"chat_id": -999}):
            with self.subTest(route=route), self.assertRaises(TransportInputError):
                adapter.prepare_agent_continuation_delivery(
                    view, owner=OWNER, session_id=SESSION, delivery_route=route,
                    delivery_id="invalid-route", lifecycle_id=self.lifecycle_id,
                )
        with self.assertRaises(TransportInputError):
            adapter.prepare_agent_continuation_delivery(
                view, owner=OWNER, session_id="different-session", delivery_route=ROUTE,
                delivery_id="invalid-session", lifecycle_id=self.lifecycle_id,
            )

    def test_disabled_agent_cannot_clear_core_pending_by_accepting_without_persist(self):
        with patch.object(center, "_agent_interaction_enabled", return_value=False):
            self.assertFalse(self.publish(self.prepare()).accepted)
        self.assertEqual(self.rows(), [])

    def test_corrupt_outbox_route_is_diagnosable_and_never_sent(self):
        self.publish(self.prepare())
        row = self.rows()[0]
        stored = json.loads(row["event_json"])
        stored["_agent_continuation_delivery"]["delivery_route"]["chat_id"] = "-999"
        with db.get_conn() as conn:
            conn.execute("UPDATE telegram_notification_outbox SET event_json=? WHERE id=?", (json.dumps(stored), row["id"]))
        self.assertFalse(center.drain_telegram_notifications())
        self.assertEqual(self.bot.attempts, [])
        self.assertEqual(self.rows()[0]["status"], "suppressed")
        self.assertEqual(self.rows()[0]["last_error"], "InvalidAgentContinuationDelivery")


if __name__ == "__main__":
    unittest.main()
