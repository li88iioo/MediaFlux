"""用真实 SDK 解码/分发事件；网络发送只检查请求参数，不连接 Telegram。"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import telebot
from telebot.apihelper import ApiTelegramException

from app import database as db
from app import notifier
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.rate_limit import AgentRateLimiter
from app.agent.transports import TelegramKernelTransport
from app.bot import agent_adapter, handlers
from app.bot.agent_adapter import (
    _delivery_route,
    _session_for_source,
    telegram_agent_session_id,
)
from app.bot.client import TopicRoutingBot
from app.bot.progress import TelegramProgress, recover_stale_operations
from app.modules import telegram_topic_routing as routing
from tests.support import IsolatedDatabaseTestCase
from tests.test_agent_kernel_core import ScriptedModel


def update(message_id, *, created=False, chat_id=100, actor_id=None, date=1000, **fields):
    message = {
        "message_id": message_id, "date": date,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": actor_id or chat_id, "is_bot": False, "first_name": "User"},
    }
    if created:
        message.update(
            message_thread_id=message_id, is_topic_message=True,
            forum_topic_created={"name": "你是谁", "icon_color": 0, "is_name_implicit": True},
        )
    else:
        message["text"] = "你是谁"
    message.update(fields)
    decoded = telebot.types.Update.de_json({"update_id": message_id, "message": message})
    assert decoded is not None
    return decoded


class TelegramTopicIngressTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        config = patch("app.bot.client.config.get", side_effect=lambda key, default="":
                       "100" if key == "TG_CHAT_ID" else default)
        config.start()
        self.addCleanup(config.stop)
        self.bot = TopicRoutingBot("123456:local-mock-only", threaded=False)
        self.received = []
        self.bot.register_message_handler(self.received.append, content_types=["text", "document"])
        self.owner = "tg:v1:100\x1f100"
        routing.set_topic_mode(self.owner, True)

    def test_first_message_and_followup_share_topic_session_and_delivery(self):
        creation, first = update(500, created=True), update(501)
        assert first is not None
        assert first.message is not None
        self.assertIsNone(first.message.message_thread_id)
        self.assertIsNone(first.message.is_topic_message)
        self.bot.process_new_updates([creation, first])
        self.bot.process_new_updates([update(503, text="你会做什么", message_thread_id=500)])
        self.assertEqual(len(self.received), 2)  # 服务事件不触发 Agent 文本处理。
        for message in self.received:
            owner, session, thread = _session_for_source(100, 100, message)
            self.assertEqual(thread, 500)
            self.assertEqual(session, telegram_agent_session_id(100, 100, 500))
            self.assertNotEqual(session, telegram_agent_session_id(100, 100))
            self.assertEqual(_delivery_route(message, owner=owner)["message_thread_id"], 500)
        key = routing._topic_naming_key(self.owner, 100, 500)
        self.assertEqual(db.kv_get(key), "eligible")

    def test_two_topics_across_poll_batches_do_not_share_context(self):
        self.bot.process_new_updates([update(500, created=True)])
        self.bot.process_new_updates([update(501), update(600, created=True)])
        self.bot.process_new_updates([update(601), update(602)])
        self.assertEqual([m.message_thread_id for m in self.received], [500, 600, None])
        self.assertNotEqual(
            _session_for_source(100, 100, self.received[0])[1],
            _session_for_source(100, 100, self.received[1])[1],
        )

    def test_route_and_naming_are_ready_before_any_worker_is_dispatched(self):
        updates = [update(500, created=True), update(501)]

        def check_before_dispatch(messages):
            self.assertEqual(messages[1].message_thread_id, 500)
            self.assertEqual(db.kv_get(routing._topic_naming_key(self.owner, 100, 500)), "eligible")

        with patch.object(telebot.TeleBot, "process_new_messages", side_effect=check_before_dispatch) as dispatch:
            self.bot.process_new_updates(updates)
        dispatch.assert_called_once()

    def test_streaming_final_chunks_and_buttons_use_recovered_thread(self):
        self.bot.process_new_updates([update(500, created=True), update(501)])
        first = self.received[0]
        response = {"message_id": 510, "date": 1000, "chat": {"id": 100, "type": "private"}, "text": "测试"}
        buttons = telebot.types.InlineKeyboardMarkup()
        buttons.add(telebot.types.InlineKeyboardButton("确认", callback_data="test"))
        with (
            patch("telebot.apihelper._make_request", return_value=response) as request,
            patch("app.bot.progress.threading.Thread.start"),
        ):
            progress = TelegramProgress(
                self.bot, telebot, 100, "测试", source_message=first,
                prefer_persistent_message=True,
            ).begin("正在处理")
            self.addCleanup(progress._claim_finished)
            self.assertTrue(progress.update("部分回复"))
            self.assertTrue(progress.finish_many(("完整回复", "第二段"), reply_markup=buttons))
        sent = [call.kwargs["params"] for call in request.call_args_list if call.args[1] == "sendMessage"]
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(params["message_thread_id"] == 500 for params in sent))
        self.assertEqual(json.loads(sent[0]["reply_parameters"])["message_id"], 501)
        self.assertTrue(any("reply_markup" in call.kwargs.get("params", {}) for call in request.call_args_list))

    def test_agent_query_survives_rejected_placeholder_and_recovers_in_original_topic(self):
        def open_store():
            return SQLiteKernelStore(secret_provider=lambda: "telegram-placeholder-test")

        def submit(function, *args, **_kwargs):
            function(*args)
            return True

        answer = "已核查媒体库，共 37 集。"
        model = ScriptedModel([[
            ModelEvent(ModelEventType.TEXT_DELTA, text=answer),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
        ]])
        store, catalog = open_store(), ToolCatalog([])
        session = AgentSession(
            model=model, catalog=catalog, state_store=store, journal=store,
            pipeline=ToolPipeline(catalog=catalog, state_store=store),
        )
        runtime = SimpleNamespace(telegram=TelegramKernelTransport(session), store=store)
        attempted, delivered = [], []

        def request(_token, api_method, *, params, **_kwargs):
            if api_method in {"sendChatAction", "setMyCommands"}:
                return True
            self.assertEqual(api_method, "sendMessage")
            attempted.append(params)
            if len(attempted) <= 2:
                message = {"error_code": 429, "description": "Too Many Requests",
                           "parameters": {"retry_after": 30}}
                raise ApiTelegramException(api_method, None, message)
            delivered.append(params)
            return {"message_id": 510 + len(delivered), "date": 1000,
                    "chat": {"id": 100, "type": "private"}, "text": params["text"]}

        self.bot.message_handlers.clear()
        with (
            patch.object(agent_adapter, "telegram_agent_access", return_value="allowed"),
            patch.object(agent_adapter, "agent_rate_limiter", AgentRateLimiter()),
            patch.object(agent_adapter, "get_agent_kernel_runtime", return_value=runtime),
            patch.object(agent_adapter.AGENT_EXECUTOR, "start", return_value=True),
            patch.object(agent_adapter.AGENT_EXECUTOR, "submit", side_effect=submit),
            patch.object(handlers, "get", side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default),
            patch.object(routing, "auto_name_private_topic"),
            patch.object(TelegramProgress, "_typing_heartbeat"),
            patch("telebot.apihelper._make_request", side_effect=request),
        ):
            handlers._register_commands(self.bot, telebot)
            question = update(501, text="媒体库有多少集")
            self.bot.process_new_updates([update(500, created=True), question])
            session_id = telegram_agent_session_id(100, 100, 500)
            saved = open_store().load_existing_session(owner=self.owner, session_id=session_id)
            self.assertIsNotNone(
                saved,
                f"原话题会话未落盘，Bot 尝试发送 {[params['text'] for params in attempted]}，"
                f"模型调用 {len(model.requests)} 次",
            )
            assert saved is not None
            self.assertTrue(any(row.get("role") == "assistant" and answer in row["content"] for row in saved.conversation))
            self.assertIsNone(open_store().load_existing_session(owner=self.owner, session_id=telegram_agent_session_id(100, 100)))
            self.assertEqual(len(attempted), 2)
            self.assertEqual(attempted[0]["text"], "思考中…")
            self.assertIn(answer, attempted[1]["text"])
            self.assertEqual(delivered, [])

            pending_key = "telegram_pending_operations_v1"
            pending, = json.loads(db.kv_get(pending_key, "[]"))
            self.assertEqual((pending["terminal_delivery_state"], pending["message_thread_id"]), ("retry_wait", 500))
            with patch("app.bot.progress.time.time", return_value=pending["retry_not_before"] + 1):
                db.init_db()
                self.assertEqual(recover_stale_operations(self.bot, telebot), 1)
                self.assertEqual(recover_stale_operations(self.bot, telebot), 0)
            self.bot.process_new_updates([question])

        self.assertEqual(len(model.requests), 1)
        self.assertEqual(len(delivered), 2)
        self.assertIn(answer, delivered[0]["text"])
        self.assertIn("该消息已经处理", delivered[1]["text"])
        self.assertTrue(all(params["message_thread_id"] == 500 for params in attempted))
        self.assertEqual(db.kv_get(pending_key), "[]")

    def test_no_inference_for_unrelated_or_explicitly_routed_messages(self):
        cases = [
            ("another user", {}, {"actor_id": 200}),
            ("another chat", {}, {"chat_id": 200}),
            ("old creation", {}, {"date": 1004}),
            ("out of order", {}, {"date": 999}),
            ("not adjacent", {}, {"message_id": 502}),
            ("manual title", {"forum_topic_created": {"name": "自定义", "icon_color": 0}}, {}),
            ("bot created", {"from": {"id": 123456, "is_bot": True, "first_name": "Bot"}}, {}),
            ("group", {"chat": {"id": 100, "type": "supergroup"}}, {}),
            ("reply elsewhere", {}, {"reply_to_message": {
                "message_id": 20, "date": 900, "chat": {"id": 100, "type": "private"},
                "text": "旧会话", "message_thread_id": 10,
            }}),
            ("explicit topic", {}, {"message_thread_id": 700}),
        ]
        for label, creation_fields, text_fields in cases:
            with self.subTest(label=label):
                bot = TopicRoutingBot("123456:local-mock-only", threaded=False)
                creation = update(500, created=True, **creation_fields)
                text = update(**{"message_id": 501, **text_fields})
                assert text is not None
                assert text.message is not None
                expected = text.message.message_thread_id
                bot.process_new_updates([creation, text])
                self.assertEqual(text.message.message_thread_id, expected)

    def test_transport_keeps_topic_even_when_context_isolation_is_disabled(self):
        routing.set_topic_mode(self.owner, False)
        self.bot.process_new_updates([update(500, created=True), update(501)])
        owner, session, thread = _session_for_source(100, 100, self.received[0])
        self.assertEqual(thread, 500)
        self.assertEqual(session, telegram_agent_session_id(100, 100))
        self.assertEqual(_delivery_route(self.received[0], owner=owner)["message_thread_id"], 500)

    def test_normal_first_message_consumes_creation_without_rerouting_next_message(self):
        self.bot.process_new_updates([
            update(500, created=True), update(501, message_thread_id=500), update(502),
        ])
        self.assertEqual([m.message_thread_id for m in self.received], [500, None])

    def test_notification_client_uses_the_same_ingress(self):
        with (
            patch.object(notifier, "_initialized", False),
            patch.object(notifier, "_bot", None),
            patch.object(notifier, "_chat_id", None),
            patch.object(notifier, "_env_enabled", return_value=False),
            patch.object(notifier, "get", side_effect=lambda key: {
                "TG_BOT_TOKEN": "123456:local-mock-only", "TG_CHAT_ID": "100",
            }[key]),
        ):
            bot = notifier.get_bot()
            assert isinstance(bot, TopicRoutingBot)
            self.assertIs(bot, notifier.get_bot())
            bot.stop_bot()
