"""Media Agent Telegram 固定测试通知的确认、安全与陈旧状态测试。"""

from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

import requests

from app.agent.telegram_test_actions import (
    prepare_telegram_test_notification,
    send_telegram_test_notification_confirmed,
)


class TelegramTestNotificationTests(unittest.TestCase):
    def tearDown(self) -> None:
        pass

    @staticmethod
    def _config_get(values: dict[str, str]):
        return lambda key, default="": values.get(key, default)

    @staticmethod
    def _serialized(value) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def test_preview_requires_complete_valid_configuration_without_leaking_values(self):
        cases = (
            ({}, "not_configured"),
            (
                {"TG_BOT_TOKEN": "invalid", "TG_CHAT_ID": "not-a-chat"},
                "precondition_failed",
            ),
        )
        for values, expected_status in cases:
            with (
                self.subTest(values=values),
                patch(
                    "app.agent.telegram_test_actions.config.get",
                    side_effect=self._config_get(values),
                ),
            ):
                result, fingerprint = prepare_telegram_test_notification({})
            self.assertFalse(result.ok)
            self.assertEqual(result.status, expected_status)
            self.assertEqual(len(fingerprint), 64)
            serialized = self._serialized(result.to_dict())
            for private_value in values.values():
                self.assertNotIn(private_value, serialized)

    def test_confirmed_send_uses_fixed_message_and_returns_only_safe_state(self):
        values = {
            "TG_BOT_TOKEN": "123456:secret-token-must-not-leak",
            "TG_CHAT_ID": "-100123456789",
        }
        bot = Mock()
        fake_telebot = types.SimpleNamespace(TeleBot=Mock(return_value=bot))
        with patch(
            "app.agent.telegram_test_actions.config.get",
            side_effect=self._config_get(values),
        ):
            _preview, expected = prepare_telegram_test_notification({})
            with (
                patch.dict(sys.modules, {"telebot": fake_telebot}),
                patch("app.agent.telegram_test_actions.configure_telebot_logging"),
            ):
                result = send_telegram_test_notification_confirmed({}, expected)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data, {"sent": True})
        fake_telebot.TeleBot.assert_called_once_with(
            values["TG_BOT_TOKEN"], parse_mode="HTML", threaded=False
        )
        bot.send_message.assert_called_once()
        self.assertEqual(bot.send_message.call_args.args[0], values["TG_CHAT_ID"])
        self.assertIn("MediaFlux 连接测试", bot.send_message.call_args.args[1])
        self.assertEqual(bot.send_message.call_args.kwargs["timeout"], 8)
        serialized = self._serialized(result.to_dict())
        self.assertNotIn(values["TG_BOT_TOKEN"], serialized)
        self.assertNotIn(values["TG_CHAT_ID"], serialized)

    def test_network_failures_are_mapped_without_raw_exception_or_secrets(self):
        values = {
            "TG_BOT_TOKEN": "123456:secret-token-must-not-leak",
            "TG_CHAT_ID": "-100123456789",
        }
        bot = Mock()
        bot.send_message.side_effect = requests.Timeout(
            "raw timeout secret-token-must-not-leak -100123456789"
        )
        fake_telebot = types.SimpleNamespace(TeleBot=Mock(return_value=bot))
        with patch(
            "app.agent.telegram_test_actions.config.get",
            side_effect=self._config_get(values),
        ):
            _preview, expected = prepare_telegram_test_notification({})
            with (
                patch.dict(sys.modules, {"telebot": fake_telebot}),
                patch("app.agent.telegram_test_actions.configure_telebot_logging"),
            ):
                result = send_telegram_test_notification_confirmed({}, expected)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "outcome_unknown")
        self.assertIn("确认", result.summary)
        self.assertIn("先检查 Telegram", result.error)
        serialized = self._serialized(result.to_dict())
        self.assertNotIn("raw timeout", serialized)
        self.assertNotIn(values["TG_BOT_TOKEN"], serialized)
        self.assertNotIn(values["TG_CHAT_ID"], serialized)
