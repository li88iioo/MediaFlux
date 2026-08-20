"""本地媒体意图规则的模块边界与兼容测试。"""
from __future__ import annotations

import subprocess
import sys
import unittest

from app.agent.local_media_intents import (
    is_local_media_diagnosis_message,
    is_local_media_history_summary_message,
    is_local_media_review_queue_summary_message,
    is_local_media_source_summaries_message,
    is_local_media_source_trigger_control_message,
    local_media_source_summary_request,
    local_media_source_trigger_control_request,
)


class LocalMediaIntentModuleTests(unittest.TestCase):
    def test_source_read_and_control_contracts(self):
        self.assertEqual(
            local_media_source_summary_request("查看本地媒体来源 #12 详情"),
            {"source_number": 12},
        )
        self.assertTrue(is_local_media_source_summaries_message("所有本地媒体来源概览"))
        self.assertEqual(
            local_media_source_trigger_control_request(
                "关闭本地媒体来源 3 的 qB 下载完成自动接管"
            ),
            (
                "local_media.set_source_trigger_enabled",
                {"source_number": 3, "trigger": "qb_completed", "enabled": False},
            ),
        )
        self.assertTrue(
            is_local_media_source_trigger_control_message("关闭本地媒体来源的自动扫描")
        )

    def test_summary_and_diagnosis_routes_remain_disjoint(self):
        self.assertTrue(
            is_local_media_review_queue_summary_message("查看本地媒体待确认汇总")
        )
        self.assertTrue(
            is_local_media_history_summary_message("查看本地整理处理历史摘要")
        )
        self.assertTrue(is_local_media_diagnosis_message("检查本地媒体调度状态"))
        self.assertFalse(is_local_media_diagnosis_message("查看本地媒体待确认汇总"))

    def test_module_import_does_not_load_orchestrator(self):
        code = (
            "import sys; import app.agent.local_media_intents; "
            "raise SystemExit(1 if 'app.agent.orchestrator' in sys.modules else 0)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
