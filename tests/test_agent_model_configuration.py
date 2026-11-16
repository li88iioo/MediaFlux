"""未配置模型的真实TG入口给出可操作指引，不尝试连接Provider。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import telebot

from app import config
from app.agent.kernel import bootstrap
from app.bot.agent_adapter import handle_agent_message
from tests import test_agent_generation_profiles as profiles

local_config = profiles.local_config
no_external_io = profiles.no_external_io


class Bot:
    def __init__(self):
        self.messages = []

    def send_chat_action(self, *args, **kwargs):
        return True

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=99)

    def reply_to(self, source, text, **kwargs):
        return self.send_message(source.chat.id, text, **kwargs)

    def edit_message_text(self, text, *args, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=99)


@pytest.mark.parametrize("updates,missing", [
    ({"AGENT_LLM_API_URL": ""}, ["API Base URL"]),
    ({"AGENT_LLM_MODEL": ""}, ["模型名称"]),
    ({"AGENT_LLM_API_URL": "", "AGENT_LLM_MODEL": ""}, ["API Base URL", "模型名称"]),
])
def test_telegram_model_missing_configuration_has_actionable_reply(local_config, updates, missing):
    config.set_and_save({"TG_CHAT_ID": "1001", "TG_AGENT_ALLOWED_USER_IDS": "1001", "TG_AGENT_ENABLED": "1", **updates})
    bot = Bot()
    message = SimpleNamespace(chat=SimpleNamespace(id=1001, type="private"), from_user=SimpleNamespace(id=1001),
        text="查看当前运行状态", message_id=98, message_thread_id=None)
    with patch.object(bootstrap, "OpenAICompatibleModelAdapter", side_effect=AssertionError("未配置时不能构建模型客户端")) as model:
        assert handle_agent_message(bot, telebot, message)
    assert bot.messages
    reply = bot.messages[-1]
    assert "模型尚未配置" in reply
    assert all(field in reply for field in missing)
    assert "设置 → Media Agent" in reply and "保存后重试" in reply
    assert "历史对话仍可查看" in reply
    assert "Agent 运行失败" not in reply
    model.assert_not_called()
