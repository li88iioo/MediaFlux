"""qB 控制只允许通过统一 Provider 写计划链。"""

from __future__ import annotations

import unittest

from app.agent.provider_models import ProviderPayload, ProviderProfileView
from app.agent.provider_operations import build_provider_catalog


class _QBControlTransport:
    provider = "qbittorrent"

    def __init__(self, task_names: list[str]) -> None:
        self.task_names = list(task_names)
        self.previews: list[tuple[str, dict, dict]] = []
        self.executions: list[tuple[str, dict]] = []

    def profiles(self) -> list[ProviderProfileView]:
        return [
            ProviderProfileView(
                "configured:qbittorrent", "qbittorrent", "qBittorrent", "online"
            )
        ]

    def profile_revision(self, _profile_ref: str) -> str:
        return "qb-profile-revision-1"

    def execute_read(
        self, _profile_ref: str, operation: str, _arguments: dict
    ) -> ProviderPayload:
        if operation != "qb.torrents.info":
            raise AssertionError(f"unexpected read operation: {operation}")
        return ProviderPayload(
            summary=f"qBittorrent 返回 {len(self.task_names)} 个下载任务",
            data={
                "torrents": [
                    {
                        "__object_id": f"{index:040x}",
                        "__object_kind": "qb_torrent",
                        "name": name,
                        "state": "downloading",
                        "progress": 0.5,
                        "size": 1024,
                    }
                    for index, name in enumerate(self.task_names, start=1)
                ],
                "count": len(self.task_names),
                "total": len(self.task_names),
                "truncated": False,
            },
            source="qbittorrent_api",
        )

    def preview_write(
        self, _profile_ref: str, operation: str, arguments: dict, target_snapshot: dict
    ) -> ProviderPayload:
        self.previews.append((operation, dict(arguments), dict(target_snapshot)))
        return ProviderPayload(
            summary="将精准控制 1 个 qBittorrent 任务",
            data={
                "targets": list(target_snapshot.get("torrent_refs") or []),
                "target_count": 1,
                "delete_files": False,
            },
            source="qbittorrent_api",
        )

    def execute_write(
        self,
        _profile_ref: str,
        operation: str,
        arguments: dict,
        *,
        expected_profile_revision: str,
    ) -> ProviderPayload:
        assert expected_profile_revision == "qb-profile-revision-1"
        self.executions.append((operation, dict(arguments)))
        return ProviderPayload(
            summary="qBittorrent 已接受任务控制操作",
            data={
                "affected": 1,
                "accepted": True,
                "delete_files": False,
                "verification": "verified",
            },
            source="qbittorrent_api",
            status="accepted",
        )


class DownloadControlTests(unittest.TestCase):
    def test_provider_capability_intent_keeps_relevance_order(self) -> None:
        catalog = build_provider_catalog()
        cases = (
            ("qbittorrent", "暂停刚才这些下载任务", "qb.torrents.pause"),
            ("qbittorrent", "恢复刚才这些下载任务", "qb.torrents.resume"),
            ("qbittorrent", "移除 qB 任务保留文件", "qb.torrents.delete_task"),
            ("media", "刷新媒体库", "media.library.refresh"),
        )
        for provider, intent, expected in cases:
            with self.subTest(intent=intent):
                operations = catalog.list(provider=provider, intent=intent, limit=1)
                self.assertEqual([item.operation_id for item in operations], [expected])
