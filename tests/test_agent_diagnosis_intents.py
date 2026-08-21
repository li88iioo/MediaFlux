from __future__ import annotations

import unittest

from app.agent.intents import ReadIntentSpec, match_read_intent
from app.agent.orchestrator import (
    _DIAGNOSTIC_READ_INTENTS,
    is_config_diagnosis_message,
)


class DiagnosisIntentTableTests(unittest.TestCase):
    def test_table_order_is_part_of_the_routing_contract(self):
        self.assertEqual(
            [spec.tool_name for spec in _DIAGNOSTIC_READ_INTENTS],
            [
                "indexer.diagnose_readiness",
                "downloads.diagnose_queue",
                "rss.diagnose",
                "local_media.diagnose",
                "automation.diagnose_pipeline",
                "workspace.health",
            ],
        )

    def test_config_diagnosis_requires_explicit_read_intent(self):
        for message in (
            "检查项目配置", "检查配置", "环境诊断", "项目诊断", "请诊断",
            "检查媒体服务器配置", "检查资源站配置",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_config_diagnosis_message(message))

        for message in (
            "配置", "设置", "怎么配置下载器", "如何设置代理", "设置自动整理",
            "修改配置", "联网搜索配置文档", "搜索配置相关的资源",
        ):
            with self.subTest(message=message):
                self.assertFalse(is_config_diagnosis_message(message))

    def test_first_match_wins(self):
        specs = (
            ReadIntentSpec("specific", lambda _: True),
            ReadIntentSpec("broad", lambda _: True),
        )
        self.assertEqual(match_read_intent("状态", specs), "specific")

    def test_no_match_returns_none(self):
        specs = (ReadIntentSpec("unused", lambda _: False),)
        self.assertIsNone(match_read_intent("普通会话", specs))


if __name__ == "__main__":
    unittest.main()
