"""Media Agent Telegram 固定测试通知的确认、安全与陈旧状态测试。"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

import requests
import pytest

from app import config
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import notification_test_tool
from tests.test_agent_feature_state import backend as backend  # noqa: PLC0414
from tests.test_agent_sdk_effects import context_for

from app.modules.telegram_connection_test import prepare_test, send_configured_test
from app.agent.models import ToolResult


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
                    "app.modules.telegram_connection_test.config.get",
                    side_effect=self._config_get(values),
                ),
            ):
                value, fingerprint = prepare_test()
                result = ToolResult(**value)
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
            "app.modules.telegram_connection_test.config.get",
            side_effect=self._config_get(values),
        ):
            _preview, expected = prepare_test()
            with (
                patch.dict(sys.modules, {"telebot": fake_telebot}),
                patch("app.modules.telegram_connection_test.configure_telebot_logging"),
            ):
                result = ToolResult(**send_configured_test(expected))
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
            "app.modules.telegram_connection_test.config.get",
            side_effect=self._config_get(values),
        ):
            _preview, expected = prepare_test()
            with (
                patch.dict(sys.modules, {"telebot": fake_telebot}),
                patch("app.modules.telegram_connection_test.configure_telebot_logging"),
            ):
                result = ToolResult(**send_configured_test(expected))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "outcome_unknown")
        self.assertIn("确认", result.summary)
        assert result.error is not None
        self.assertIn("先检查 Telegram", result.error)
        serialized = self._serialized(result.to_dict())
        self.assertNotIn("raw timeout", serialized)
        self.assertNotIn(values["TG_BOT_TOKEN"], serialized)
        self.assertNotIn(values["TG_CHAT_ID"], serialized)


@pytest.mark.parametrize("change", ["none", "cancel", "recipient", "timeout", "foreign"])
def test_native_notification_cold_confirmation_does_not_replay_or_change_recipient(backend, monkeypatch, change):
    config.set_and_save({"TG_BOT_TOKEN": "123456:fixture-secret", "TG_CHAT_ID": "100001"})
    sent = Mock(side_effect=requests.Timeout("private") if change == "timeout" else None)
    monkeypatch.setattr("app.modules.telegram_connection_test.send_test_message", sent)

    def executor():
        store = SQLiteKernelStore()
        return EffectExecutor(catalog=ToolCatalog([notification_test_tool()]), state_store=store, reference_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()), authorization=MediaFluxAuthorizationPolicy())

    async def exercise():
        effects = executor()
        context = await context_for(effects.state_store, owner="webk:v1:" + "b" * 64)
        plan = (await effects.prepare("settings.test_notification", {}, context=context)).effect_plan
        assert plan is not None and not sent.called
        assert "fixture-secret" not in json.dumps([plan.public_dict(), dict(plan.metadata)])
        cold = executor()
        if change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "recipient":
            config.set_and_save({"TG_CHAT_ID": "200002"})
        elif change == "foreign":
            context = await context_for(effects.state_store, owner="webk:v1:" + "c" * 64)
        if change in {"cancel", "recipient", "foreign"}:
            with pytest.raises(ToolPipelineError):
                await cold.execute_confirmed(plan.plan_id, context=context)
            sent.assert_not_called()
        else:
            result = (await cold.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
            assert result["status"] == ("outcome_unknown" if change == "timeout" else "completed")
            sent.assert_called_once_with("123456:fixture-secret", "100001", timeout=8)
            with pytest.raises(ToolPipelineError):
                await executor().execute_confirmed(plan.plan_id, context=context)
            assert sent.call_count == 1
    asyncio.run(exercise())
