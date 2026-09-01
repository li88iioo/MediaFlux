"""qB 控制只允许通过统一 Provider 写计划链。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent import provider_actions
from app.agent.confirmation import ConfirmationStore
from app.agent.orchestrator import AgentOrchestrator, download_task_control_request
from app.agent.provider_actions import _LOCK as PROVIDER_GATEWAY_LOCK
from app.agent.provider_gateway import ProviderGateway
from app.agent.provider_models import ProviderPayload, ProviderProfileView
from app.agent.provider_operations import build_provider_catalog
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.tools import build_tool_registry
from tests.support import isolated_test_database


class _QBControlTransport:
    provider = "qbittorrent"

    def __init__(self, task_names: list[str]) -> None:
        self.task_names = list(task_names)
        self.previews: list[tuple[str, dict, dict]] = []
        self.executions: list[tuple[str, dict]] = []

    def profiles(self) -> list[ProviderProfileView]:
        return [
            ProviderProfileView(
                "configured:qbittorrent",
                "qbittorrent",
                "qBittorrent",
                "online",
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
        self,
        _profile_ref: str,
        operation: str,
        arguments: dict,
        target_snapshot: dict,
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
    def test_natural_language_maps_to_provider_operations(self) -> None:
        self.assertEqual(
            download_task_control_request("暂停下载任务《Example.Show.S01E01》"),
            ("qb.torrents.pause", "Example.Show.S01E01"),
        )
        self.assertEqual(
            download_task_control_request(
                "恢复 qBittorrent 任务『Example.Show.S01E01』"
            ),
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
                operations = catalog.list(
                    provider=provider,
                    intent=intent,
                    limit=1,
                )
                self.assertEqual([item.operation_id for item in operations], [expected])


class ProviderDownloadControlIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database_context = isolated_test_database("provider-control.db")
        self.database_context.__enter__()
        with PROVIDER_GATEWAY_LOCK:
            self.previous_gateway = provider_actions._GATEWAY

    def tearDown(self) -> None:
        try:
            with PROVIDER_GATEWAY_LOCK:
                provider_actions._GATEWAY = self.previous_gateway
        finally:
            self.database_context.__exit__(None, None, None)

    @staticmethod
    def _service(
        task_names: list[str],
    ) -> tuple[
        AgentOrchestrator,
        ToolRegistry,
        _QBControlTransport,
    ]:
        transport = _QBControlTransport(task_names)
        gateway = ProviderGateway(
            catalog=build_provider_catalog(),
            transports=[transport],
        )
        with PROVIDER_GATEWAY_LOCK:
            provider_actions._GATEWAY = gateway
        registry = build_tool_registry()
        service = AgentOrchestrator(
            registry,
            ConfirmationStore(token_factory=lambda: "provider-confirmation-token-0001"),
        )
        return service, registry, transport

    def test_deterministic_control_uses_registry_session_and_confirms_once(
        self,
    ) -> None:
        service, registry, transport = self._service(["Example Show S01E01"])

        with (
            patch.object(service, "_query_with_model_tools", return_value=None),
            patch.object(registry, "execute", wraps=registry.execute) as execute,
        ):
            prepared = service.query(
                "暂停下载任务《Example Show S01E01》",
                owner="owner-a",
                session_id="session-real",
                request_id="request-real",
            )

        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(
            prepared["tool_call"]["name"],
            "provider.change.execute",
        )
        provider_calls = [
            call
            for call in execute.call_args_list
            if call.args and str(call.args[0]).startswith("provider.")
        ]
        self.assertEqual(
            [call.args[0] for call in provider_calls],
            [
                "provider.capabilities",
                "provider.query",
                "provider.change.preview",
            ],
        )
        self.assertTrue(provider_calls)
        contexts = [call.kwargs["context"] for call in provider_calls]
        self.assertEqual({context.owner for context in contexts}, {"owner-a"})
        self.assertEqual({context.session_id for context in contexts}, {"session-real"})
        self.assertEqual({context.request_id for context in contexts}, {"request-real"})
        self.assertEqual(len(transport.previews), 1)

        plan_id = prepared["action_plan"]["plan_id"]
        confirmed = service.confirm(
            plan_id,
            owner="owner-a",
            session_id="session-real",
            request_id="confirm-real",
        )
        self.assertEqual(confirmed["mode"], "confirmed_action")
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(
            transport.executions,
            [("qb.torrents.pause", {"torrent_refs": [f"{1:040x}"]})],
        )
        with self.assertRaises(AgentToolError) as repeated:
            service.confirm(
                plan_id,
                owner="owner-a",
                session_id="session-real",
            )
        self.assertEqual(repeated.exception.code, "confirmation_invalid")
        self.assertEqual(len(transport.executions), 1)

    def test_duplicate_exact_names_never_create_provider_write_plan(self) -> None:
        service, _registry, transport = self._service(["Same Name", "Same Name"])

        with patch.object(service, "_query_with_model_tools", return_value=None):
            response = service.query(
                "暂停下载任务《Same Name》",
                owner="owner-a",
                session_id="session-real",
            )

        self.assertEqual(response["result"]["status"], "selection_required")
        self.assertFalse(response["result"]["ok"])
        self.assertEqual(transport.previews, [])
        self.assertEqual(transport.executions, [])


if __name__ == "__main__":
    unittest.main()
