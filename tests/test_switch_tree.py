"""Media Agent 总开关（AGENT_LLM_ENABLED）：所有 AI 能力共用一个开关；真实安装与真实 user.env。

关闭时 Agent 各功能与整理 AI 回退在运行时都不生效，子开关已保存的值保持不变；
已退役的 AGENT_ENABLED 残留值不再影响任何判断。
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app import config
from tests.support import initialized_web_runtime

AGENT_CHILDREN = {
    "AGENT_RECOGNITION_REVIEW_ENABLED": "1",
    "AGENT_EPISODE_RESEARCH_ENABLED": "1",
    "TG_AGENT_ENABLED": "1",
    "AGENT_LLM_API_URL": "https://model.invalid/v1",
    "AGENT_LLM_MODEL": "fixture-model",
}


def _write(**values: str) -> None:
    config.set_and_save(values)


class MediaAgentSwitchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(initialized_web_runtime())
        # 发布器会同步进程环境（与生产一致）；用例结束后恢复，避免泄漏到其他用例。
        self.enterContext(patch.dict(os.environ))

    def test_one_switch_gates_every_agent_feature_and_keeps_child_values(self):
        from app.agent.feature_gate import is_agent_enabled, is_agent_llm_enabled, recognition_review_enabled
        from app.bot.handlers import _telegram_agent_available
        from app.modules.episode_research_service import episode_research_enabled

        _write(**AGENT_CHILDREN)
        # 残留的旧键无论取值都不参与判断。
        for switch, legacy, expected in (("1", "0", True), ("1", "1", True), ("0", "1", False), ("0", "0", False)):
            with self.subTest(switch=switch, legacy=legacy):
                _write(AGENT_LLM_ENABLED=switch, AGENT_ENABLED=legacy)
                self.assertEqual(
                    [is_agent_enabled(), is_agent_llm_enabled(), recognition_review_enabled(),
                     episode_research_enabled(), _telegram_agent_available()],
                    [expected] * 5,
                )
                self.assertEqual({key: config.get(key) for key in AGENT_CHILDREN}, AGENT_CHILDREN)

    def test_switch_change_reconciles_agent_runtime_and_command_menu(self):
        from app.modules.feature_config import reload_runtime

        with patch("app.modules.agent_runtime.request_agent_runtime_reconcile") as reconcile, patch(
            "app.bot.handlers.request_command_menu_refresh",
        ) as menu:
            reload_runtime({"AGENT_LLM_ENABLED"}, background_services=True)
        reconcile.assert_called_once_with()
        menu.assert_called_once_with()

    def test_telegram_enable_and_disable_all_flip_the_one_switch(self):
        from app.agent.feature_gate import is_agent_enabled
        from app.bot.agent_adapter import _apply_agent_control_action

        _write(AGENT_LLM_ENABLED="0", TG_AGENT_ENABLED="0")
        with patch("app.modules.agent_runtime.request_agent_runtime_reconcile"):
            self.assertEqual(_apply_agent_control_action("enable_all"), "Media Agent 已开启")
            self.assertTrue(is_agent_enabled())
            self.assertEqual(config.get("TG_AGENT_ENABLED"), "1")
            self.assertEqual(_apply_agent_control_action("disable_all"), "Media Agent 已关闭")
        self.assertFalse(is_agent_enabled())

    def test_retired_agent_key_is_no_longer_accepted_by_the_settings_api(self):
        from fastapi.testclient import TestClient

        from app.main import create_app
        from tests.test_config_empty_values_api import login

        client = TestClient(create_app(), raise_server_exceptions=False)
        self.addCleanup(client.close)
        response = client.post("/api/config", json={"AGENT_ENABLED": "1"}, headers=login(client))
        self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()
