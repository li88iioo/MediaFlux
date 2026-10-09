from __future__ import annotations

import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import telebot


class TelegramTopicControlsTests(unittest.TestCase):
    def setUp(self):
        from app.bot import handlers

        self.registered_callbacks = []
        self.registered_messages = []
        self.authorized_chat_id = "100"
        self.bot = telebot.TeleBot("123456:local-mock-only", threaded=False)
        self.bot.message_handler = lambda **filters: self._capture(
            self.registered_messages, filters
        )
        self.bot.callback_query_handler = lambda **filters: self._capture(
            self.registered_callbacks, filters
        )
        self.bot.answer_callback_query = Mock(return_value=True)
        self.bot.get_me = Mock(
            return_value=SimpleNamespace(
                has_topics_enabled=True,
                allows_users_to_create_topics=True,
            )
        )

        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(
            patch("app.bot.agent_adapter.AGENT_EXECUTOR.start", return_value=True)
        )
        self.patches.enter_context(patch("app.bot.handlers._set_command_menu"))
        self.patches.enter_context(
            patch("app.bot.handlers.get", side_effect=self._get_config)
        )
        handlers._register_commands(self.bot, telebot)
        self.topic_callback = next(
            handler
            for filters, handler in self.registered_callbacks
            if filters.get("func", lambda _call: False)(SimpleNamespace(data="tgt:off"))
        )

    def _capture(self, registered, filters):
        def decorate(handler):
            registered.append((filters, handler))
            return handler

        return decorate

    def _get_config(self, key, default=""):
        if key == "TG_CHAT_ID":
            return self.authorized_chat_id
        return default

    @staticmethod
    def _call(action="off", *, chat_id=100, chat_type="private"):
        return SimpleNamespace(
            id="callback-1",
            data=f"tgt:{action}",
            from_user=SimpleNamespace(id=7),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=chat_id, type=chat_type),
                message_id=42,
            ),
        )

    def test_service_messages_are_routed_without_starting_an_agent_turn(self):
        handler = next(handler for filters, handler in self.registered_messages
                       if "forum_topic_created" in filters.get("content_types", []))
        message = self._call().message
        message.from_user = SimpleNamespace(id=999, is_bot=True)
        with patch("app.modules.telegram_topic_routing.record_private_topic_service_message") as record:
            handler(message)
        record.assert_called_once_with("tg:v1:100\x1f100", message)

    def test_success_edits_original_message_and_transmits_empty_inline_keyboard(self):
        call = self._call("off")
        with (
            patch("app.modules.telegram_topic_routing.set_topic_mode") as set_mode,
            patch("telebot.apihelper._make_request", return_value=True) as request,
        ):
            self.topic_callback(call)

        set_mode.assert_called_once_with("tg:v1:100\x1f7", False)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1], "editMessageText")
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["text"], "私聊话题隔离已关闭")
        self.assertEqual(params["chat_id"], 100)
        self.assertEqual(params["message_id"], 42)
        self.assertEqual(
            json.loads(params["reply_markup"]),
            {"inline_keyboard": []},
        )
        self.bot.answer_callback_query.assert_called_once_with(
            call.id, "私聊话题隔离已关闭"
        )

    def test_on_remains_blocked_when_platform_topics_are_unavailable(self):
        call = self._call("on")
        self.bot.get_me.return_value = SimpleNamespace(
            has_topics_enabled=False,
            allows_users_to_create_topics=True,
        )
        with (
            patch("app.modules.telegram_topic_routing.set_topic_mode") as set_mode,
            patch("telebot.apihelper._make_request") as request,
        ):
            self.topic_callback(call)

        set_mode.assert_not_called()
        request.assert_not_called()
        self.bot.answer_callback_query.assert_called_once()
        self.assertIn("BotFather", self.bot.answer_callback_query.call_args.args[1])
        self.assertTrue(self.bot.answer_callback_query.call_args.kwargs["show_alert"])

    def test_unauthorized_and_non_private_callbacks_are_rejected(self):
        with patch("app.modules.telegram_topic_routing.set_topic_mode") as set_mode:
            self.topic_callback(self._call("off", chat_id=999))
            self.assertEqual(
                self.bot.answer_callback_query.call_args.args[1], "未授权会话"
            )
            self.bot.answer_callback_query.reset_mock()

            self.topic_callback(self._call("off", chat_type="group"))

        set_mode.assert_not_called()
        self.bot.get_me.assert_not_called()
        self.bot.answer_callback_query.assert_called_once_with(
            "callback-1", "仅支持 Bot 私聊话题", show_alert=True
        )

    def test_storage_failure_does_not_edit_or_report_success(self):
        call = self._call("off")
        with (
            patch(
                "app.modules.telegram_topic_routing.set_topic_mode",
                side_effect=OSError("storage unavailable"),
            ),
            patch("telebot.apihelper._make_request") as request,
        ):
            self.topic_callback(call)

        request.assert_not_called()
        self.bot.answer_callback_query.assert_called_once()
        text = self.bot.answer_callback_query.call_args.args[1]
        self.assertIn("切换失败", text)
        self.assertNotIn("已关闭", text)
        self.assertTrue(self.bot.answer_callback_query.call_args.kwargs["show_alert"])

    def test_edit_failure_reports_saved_state_without_claiming_message_updated(self):
        call = self._call("off")
        with (
            patch("app.modules.telegram_topic_routing.set_topic_mode") as set_mode,
            patch(
                "telebot.apihelper._make_request",
                side_effect=OSError("Telegram unavailable"),
            ) as request,
        ):
            self.topic_callback(call)

        set_mode.assert_called_once_with("tg:v1:100\x1f7", False)
        request.assert_called_once()
        text = self.bot.answer_callback_query.call_args.args[1]
        self.assertIn("设置已保存", text)
        self.assertIn("消息更新失败", text)
        self.assertNotEqual(text, "私聊话题隔离已关闭")
        self.assertTrue(self.bot.answer_callback_query.call_args.kwargs["show_alert"])
