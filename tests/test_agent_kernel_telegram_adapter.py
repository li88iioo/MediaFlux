from __future__ import annotations

import types
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from app.agent.kernel.adapters import ApprovalView, TurnView
from app.agent.kernel.events import AgentEventType, EventFactory
from app.bot import agent_adapter as adapter


class Button:
    def __init__(self, text, callback_data):
        self.text = text
        self.callback_data = callback_data


class Markup:
    def __init__(self, row_width=1):
        self.row_width = row_width
        self.buttons = []

    def add(self, *buttons):
        self.buttons.extend(buttons)


TELEBOT = types.SimpleNamespace(
    types=types.SimpleNamespace(
        InlineKeyboardMarkup=Markup,
        InlineKeyboardButton=Button,
    )
)


@dataclass
class User:
    id: int


@dataclass
class Chat:
    id: int


class Message:
    def __init__(self, text="检查媒体库", *, chat_id=-100, user_id=7, message_id=11):
        self.text = text
        self.chat = Chat(chat_id)
        self.from_user = User(user_id)
        self.message_id = message_id
        self.message_thread_id = None
        self.reply_to_message = None


class Call:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.from_user = User(7)
        self.id = "callback-1"


class FakeBot:
    def __init__(self):
        self.replies = []
        self.edits = []
        self.answers = []
        self.sent = []
        self.actions = []
        self.deleted = []
        self._next = 100

    def reply_to(self, source, text, **kwargs):
        self.replies.append((text, kwargs))
        target = Message(text, chat_id=source.chat.id, user_id=0, message_id=self._next)
        self._next += 1
        return target

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edits.append((text, chat_id, message_id, kwargs))

    def answer_callback_query(self, callback_id, text, **kwargs):
        self.answers.append((callback_id, text, kwargs))

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        target = Message(text, chat_id=chat_id, user_id=0, message_id=self._next)
        self._next += 1
        return target

    def send_chat_action(self, chat_id, action, **kwargs):
        self.actions.append((chat_id, action, kwargs))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    def edit_message_reply_markup(self, chat_id, message_id, **kwargs):
        self.edits.append(("", chat_id, message_id, kwargs))


class FakeDraftBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.drafts = []

    def send_message_draft(self, chat_id, draft_id, text, **kwargs):
        self.drafts.append((chat_id, draft_id, text, kwargs))
        return True


class FakeTelegramTransport:
    def __init__(self, view, *, events=()):
        self.view = view
        self.events = tuple(events)
        self.queries = []
        self.confirmations = []
        self.cancelled = []

    async def query(self, envelope, *, observe=None):
        self.queries.append(envelope)
        if observe is not None:
            for event in self.events:
                await observe(event)
        return self.view

    async def confirm(self, envelope, *, observe=None):
        self.confirmations.append(envelope)
        return TurnView(
            session_id=envelope.session_id,
            turn_id="turn-confirm",
            request_id=envelope.request_id,
            status="effect_completed",
            effect_result={"summary": "订阅已创建"},
        )

    async def cancel_effect(self, envelope):
        self.cancelled.append(envelope)
        return True

    async def cancel(self, *, owner, session_id):
        return True


class FakeStore:
    async def reset_session(self, *, owner, session_id):
        return None


class AgentKernelTelegramAdapterTests(unittest.TestCase):
    def setUp(self):
        self.config_values = {
            "TG_AGENT_ALLOWED_USER_IDS": "7",
            "TG_CHAT_ID": "-100",
            "TG_AGENT_ENABLED": "1",
        }

    def _get(self, key, default=""):
        return self.config_values.get(key, default)

    def _patch_access(self):
        return (
            patch.object(adapter.config, "get", side_effect=self._get),
            patch.object(adapter, "is_agent_enabled", return_value=True),
            patch.object(adapter.agent_rate_limiter, "allow", return_value=True),
        )

    def test_owner_and_session_are_stable_and_user_scoped(self):
        with self._patch_access()[0]:
            self.assertTrue(adapter.telegram_user_is_allowed(7))
        self.assertEqual(adapter.telegram_agent_owner(-100, 7), "tg:v1:-100\x1f7")
        self.assertEqual(
            adapter.telegram_agent_session_id(-100, 7),
            adapter.telegram_agent_session_id(-100, 7),
        )
        self.assertNotEqual(
            adapter.telegram_agent_session_id(-100, 7),
            adapter.telegram_agent_session_id(-100, 8),
        )

    def test_disabled_agent_does_not_capture_normal_telegram_text(self):
        bot = FakeBot()
        with patch.object(adapter, "is_agent_enabled", return_value=False):
            self.assertFalse(adapter.handle_agent_message(bot, TELEBOT, Message()))
        self.assertEqual(bot.replies, [])

    def test_query_streams_typing_and_renders_markdown_as_telegram_html(self):
        factory = EventFactory(
            session_id="tg_session",
            turn_id="turn-stream",
            request_id="request-stream",
        )
        answer = (
            "### 2026 新番推荐\n"
            "1. **《葬送的芙莉莲》第二季**\n"
            "   - **题材**：奇幻 / 冒险\n\n"
            "---\n"
            "> 定档信息可能变化。"
        )
        transport = FakeTelegramTransport(
            TurnView(
                session_id="tg_session",
                turn_id="turn-stream",
                request_id="request-stream",
                status="success",
                answer=answer,
            ),
            events=(
                factory.create(AgentEventType.MODEL_STARTED, {"round": 1}),
                factory.create(
                    AgentEventType.MODEL_DELTA,
                    {"round": 1, "delta": answer[:35]},
                ),
                factory.create(
                    AgentEventType.MODEL_DELTA,
                    {"round": 1, "delta": answer[35:]},
                ),
            ),
        )
        runtime = types.SimpleNamespace(telegram=transport, store=FakeStore())
        bot = FakeDraftBot()
        patches = self._patch_access()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
        ):
            handled = adapter.handle_agent_message(
                bot, TELEBOT, Message("2026 新番推荐")
            )

        self.assertTrue(handled)
        self.assertTrue(any(action == "typing" for _, action, _ in bot.actions))
        streamed = [text for _chat, _draft, text, _kwargs in bot.drafts if "正在输出" in text]
        self.assertTrue(streamed)
        self.assertIn("<b>2026 新番推荐</b>", streamed[0])
        _chat_id, final_text, final_kwargs = bot.sent[-1]
        self.assertEqual(final_kwargs["parse_mode"], "HTML")
        self.assertIn("<b>2026 新番推荐</b>", final_text)
        self.assertIn("<b>《葬送的芙莉莲》第二季</b>", final_text)
        self.assertIn("────────", final_text)
        self.assertIn("<blockquote>定档信息可能变化。</blockquote>", final_text)
        self.assertNotIn("###", final_text)
        self.assertNotIn("**", final_text)
        self.assertNotIn("正在输出", final_text)

    def test_query_renders_kernel_approval_with_direct_effect_buttons(self):
        approval = ApprovalView(
            plan_id="plan_1234567890abcdef",
            tool_name="rss.create_subscription",
            effect="WRITE",
            preview={"summary": "将创建 RSS 订阅"},
            result={},
            expires_at="2026-09-03T12:00:00Z",
        )
        transport = FakeTelegramTransport(
            TurnView(
                session_id="tg_session",
                turn_id="turn",
                request_id="request",
                status="approval_required",
                approval=approval,
            )
        )
        runtime = types.SimpleNamespace(telegram=transport, store=FakeStore())
        bot = FakeBot()
        patches = self._patch_access()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
        ):
            handled = adapter.handle_agent_message(
                bot, TELEBOT, Message("创建 RSS 订阅")
            )
        self.assertTrue(handled)
        self.assertEqual(len(transport.queries), 1)
        markup = bot.edits[-1][3]["reply_markup"]
        self.assertEqual(
            [button.callback_data for button in markup.buttons],
            ["agk:c:plan_1234567890abcdef", "agk:x:plan_1234567890abcdef"],
        )
        self.assertIn("等待确认", bot.edits[-1][0])

    def test_confirm_callback_executes_plan_without_model_protocol(self):
        transport = FakeTelegramTransport(None)
        runtime = types.SimpleNamespace(telegram=transport, store=FakeStore())
        bot = FakeBot()
        message = Message("preview", user_id=0, message_id=33)
        call = Call("agk:c:plan_1234567890abcdef", message)
        patches = self._patch_access()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
        ):
            adapter.handle_agent_callback(bot, call, TELEBOT)
        self.assertEqual(len(transport.confirmations), 1)
        self.assertIn("订阅已创建", bot.edits[-1][0])

    def test_old_callback_is_explicitly_retired(self):
        bot = FakeBot()
        call = Call("invalid:callback", Message(user_id=0))
        patches = self._patch_access()
        with patches[0], patches[1], patches[2]:
            adapter.handle_agent_callback(bot, call, TELEBOT)
        self.assertIn("旧操作已失效", bot.answers[-1][1])


if __name__ == "__main__":
    unittest.main()
