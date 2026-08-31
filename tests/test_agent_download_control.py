"""qB 控制只允许通过统一 Provider 写计划链。"""
from __future__ import annotations

import unittest

from app.agent.orchestrator import download_task_control_request
from app.agent.tools import build_tool_registry


class DownloadControlTests(unittest.TestCase):
    def test_natural_language_maps_to_provider_operations(self) -> None:
        self.assertEqual(
            download_task_control_request("暂停下载任务《Example.Show.S01E01》"),
            ("qb.torrents.pause", "Example.Show.S01E01"),
        )
        self.assertEqual(
            download_task_control_request("恢复 qBittorrent 任务『Example.Show.S01E01』"),
            ("qb.torrents.resume", "Example.Show.S01E01"),
        )
        self.assertEqual(
            download_task_control_request("删除下载任务《Example.Show.S01E01》"),
            ("qb.torrents.delete_task", "Example.Show.S01E01"),
        )
        self.assertIsNone(download_task_control_request("删除下载任务"))

    def test_registry_has_only_provider_write_chain(self) -> None:
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertNotIn("downloads.pause_task", capabilities)
        self.assertNotIn("downloads.resume_task", capabilities)
        self.assertNotIn("downloads.delete_task", capabilities)
        self.assertEqual(capabilities["provider.change.preview"]["risk"], "read")
        self.assertEqual(capabilities["provider.change.execute"]["risk"], "write")
        self.assertTrue(
            capabilities["provider.change.execute"]["requires_confirmation"]
        )


if __name__ == "__main__":
    unittest.main()
