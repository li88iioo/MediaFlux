"""Agent 统一资源接入链路与传统 Telegram 隔离测试。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent.confirmation import ConfirmationStore
from app.agent.ingest_actions import (
    AgentIngestSessionStore,
    IngestActions,
    ingest_inspect_arguments,
    ingest_status_arguments,
    ingest_submit_arguments,
)
from app.agent.models import ToolContext
from app.agent.orchestrator import AgentOrchestrator, recent_ingest_submit_request
from app.agent.recent_resource_candidates import RecentResourceCandidateStore
from app.agent.registry import AgentToolError
from app.agent.tools import build_tool_registry

_MAGNET = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Demo"
_SHARE = "https://www.guangyapan.com/s/1938060615538466851_aecd7hu726g3whGl#/share"


class AgentIngestArgumentTests(unittest.TestCase):
    def test_submit_arguments_are_public_coordinates_only(self):
        self.assertEqual(
            ingest_submit_arguments({
                "source_type": "resource_candidates",
                "positions": [2, 1, 2],
                "target": "both",
            }),
            {
                "source_type": "resource_candidates",
                "positions": [2, 1],
                "target": "both",
            },
        )
        with self.assertRaises(AgentToolError):
            ingest_submit_arguments({
                "source_type": "direct_url",
                "target": "qb",
                "input": _MAGNET,
            })

    def test_share_defaults_to_guangya_and_all_items(self):
        self.assertEqual(
            ingest_submit_arguments({"source_type": "guangya_share"}),
            {
                "source_type": "guangya_share",
                "target": "guangya",
                "positions": [],
            },
        )

    def test_resource_inspection_rejects_input(self):
        with self.assertRaises(AgentToolError):
            ingest_inspect_arguments({
                "source_type": "resource_candidates",
                "input": "secret",
            })

    def test_status_requires_public_positive_request_number(self):
        self.assertEqual(
            ingest_status_arguments({"request_number": 12}),
            {"request_number": 12},
        )
        with self.assertRaises(AgentToolError):
            ingest_status_arguments({"request_id": 12})

    def test_direct_followup_accepts_target_only_but_rejects_negation(self):
        self.assertEqual(
            recent_ingest_submit_request("qB", source_type="direct_url"),
            {"source_type": "direct_url", "target": "qb"},
        )
        self.assertIsNone(
            recent_ingest_submit_request("不要下载到 qB", source_type="direct_url")
        )
        self.assertIsNone(
            recent_ingest_submit_request("qB 还是光鸭？", source_type="direct_url")
        )


class AgentIngestActionTests(unittest.TestCase):
    def setUp(self):
        self.store = AgentIngestSessionStore()
        self.resources = RecentResourceCandidateStore()
        self.actions = IngestActions(
            store=self.store,
            recent_resource_store=self.resources,
        )
        self.context = ToolContext(owner="owner-a")

    def test_direct_link_snapshot_never_returns_link_or_hash(self):
        result = self.actions.inspect(
            {"source_type": "direct_url", "input": _MAGNET}, self.context
        )
        self.assertTrue(result.ok)
        rendered = repr(result.to_dict())
        self.assertNotIn("magnet:?", rendered)
        self.assertNotIn("a" * 40, rendered)
        self.assertEqual(result.data["source_type"], "direct_url")
        self.assertIsNone(self.store.get(owner="owner-b"))

    def test_ordinary_web_page_is_not_ingested(self):
        with self.assertRaises(AgentToolError) as raised:
            self.actions.inspect(
                {
                    "source_type": "auto",
                    "input": "http://192.168.0.195:1258/guangya/offline",
                },
                self.context,
            )
        self.assertEqual(raised.exception.code, "precondition_failed")
        self.assertIsNone(self.store.get(owner="owner-a"))

    def test_malformed_download_link_returns_agent_input_error(self):
        with self.assertRaises(AgentToolError) as raised:
            self.actions.inspect(
                {"source_type": "direct_url", "input": "magnet:?dn=missing-hash"},
                self.context,
            )
        self.assertEqual(raised.exception.code, "precondition_failed")
        self.assertIsNone(self.store.get(owner="owner-a"))

    @patch("app.agent.ingest_actions.db.get_download_request", return_value=None)
    @patch(
        "app.agent.ingest_actions.dispatch_request",
        return_value={
            "ok": True,
            "status": "submitted",
            "succeeded": ["qb"],
            "failed": [],
        },
    )
    @patch(
        "app.agent.ingest_actions.create_request",
        return_value={"id": 42, "created": True, "status": "pending"},
    )
    @patch(
        "app.agent.ingest_actions.download_target_readiness",
        return_value={"qb": True},
    )
    def test_direct_link_requires_confirmation_and_executes_via_dispatcher(
        self, _readiness, create_request_mock, dispatch_mock, _get_request
    ):
        self.actions.inspect(
            {"source_type": "direct_url", "input": _MAGNET}, self.context
        )
        arguments = {
            "source_type": "direct_url",
            "target": "qb",
            "positions": [],
        }
        preview, fingerprint = self.actions.prepare_submit(arguments, self.context)
        self.assertTrue(preview.ok)
        self.assertNotIn("magnet:?", repr(preview.to_dict()))
        result = self.actions.execute_submit(arguments, fingerprint, self.context)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.data["request_number"], 42)
        create_request_mock.assert_called_once()
        dispatch_mock.assert_called_once_with(42, "qb")

    @patch(
        "app.agent.ingest_actions.inspect_share_for_transfer",
        return_value={
            "preview_id": "preview-secret",
            "share_id": "share-secret",
            "files": [
                {"id": "file-secret-1", "name": "Episode 1.mkv", "size": 10},
                {"id": "file-secret-2", "name": "Episode 2.mkv", "size": 20},
            ],
            "selected_ids": ["file-secret-1", "file-secret-2"],
            "target_id": "target-secret",
            "target_name": "默认目录",
            "expires_in": 900,
        },
    )
    def test_share_inspection_projects_positions_only(self, _inspect):
        result = self.actions.inspect(
            {"source_type": "guangya_share", "input": _SHARE}, self.context
        )
        rendered = repr(result.to_dict())
        self.assertTrue(result.ok)
        self.assertNotIn(_SHARE, rendered)
        self.assertNotIn("preview-secret", rendered)
        self.assertNotIn("file-secret", rendered)
        self.assertEqual([item["position"] for item in result.data["items"]], [1, 2])

    @patch(
        "app.agent.ingest_actions.inspect_share_for_transfer",
        return_value={
            "preview_id": "preview-secret",
            "share_id": "share-secret",
            "files": [
                {"name": "invalid", "size": "not-a-number"},
                {"id": "file-secret-2", "name": "Episode 2.mkv", "size": "bad"},
            ],
            "target_id": "0",
            "target_name": "根目录",
            "expires_in": "invalid",
        },
    )
    def test_share_projection_keeps_positions_aligned_with_private_ids(self, _inspect):
        result = self.actions.inspect(
            {"source_type": "guangya_share", "input": _SHARE}, self.context
        )
        self.assertEqual(result.data["count"], 1)
        self.assertEqual(result.data["items"][0]["position"], 1)
        self.assertEqual(result.data["items"][0]["size_bytes"], 0)
        snapshot = self.store.get(owner="owner-a", source_type="guangya_share")
        self.assertEqual(snapshot.private["file_ids"], ["file-secret-2"])

    def test_registry_has_only_unified_ingest_submission(self):
        registry = build_tool_registry(self.resources, self.store)
        self.assertTrue(registry.has("ingest.inspect"))
        self.assertTrue(registry.has("ingest.submit"))
        self.assertTrue(registry.has("ingest.status"))
        self.assertFalse(registry.has("indexer.submit_candidate"))
        self.assertFalse(registry.has("indexer.submit_candidates"))

    @patch(
        "app.agent.ingest_actions.download_target_readiness",
        return_value={"qb": True},
    )
    def test_orchestrator_routes_link_and_followup_through_confirmation(self, _readiness):
        registry = build_tool_registry(self.resources, self.store)
        service = AgentOrchestrator(
            registry,
            confirmation_store=ConfirmationStore(),
            recent_resource_store=self.resources,
            recent_ingest_store=self.store,
        )
        response = service.query(_MAGNET, owner="owner-a")
        self.assertEqual(response["tool_call"]["name"], "ingest.inspect")
        self.assertNotIn("magnet:?", repr(response))

        prepared = service.query("下载到 qB", owner="owner-a", present=False)
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "ingest.submit")
        self.assertNotIn("magnet:?", repr(prepared))

        service.reset_session(owner="owner-a")
        inline = service.query(
            f"把这个链接下载到 qB：{_MAGNET}",
            owner="owner-a",
            present=False,
        )
        self.assertEqual(inline["mode"], "confirmation_required")
        self.assertEqual(inline["tool_call"]["name"], "ingest.submit")
        self.assertNotIn("magnet:?", repr(inline))

    @patch("app.agent.ingest_actions.db.get_download_request")
    def test_status_returns_only_public_request_projection(self, get_request):
        get_request.return_value = {
            "id": 7,
            "title": "Safe title",
            "kind": "magnet",
            "targets": "qb",
            "status": "downloading",
            "qb_status": "downloading",
            "gy_status": "not_started",
            "organize_status": "pending",
            "strm_status": "pending",
            "updated_at": "2026-09-01 12:00:00",
            "source_value": _MAGNET,
        }
        result = self.actions.status({"request_number": 7}, self.context)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["request"]["request_number"], 7)
        self.assertNotIn("source_value", result.data["request"])
        self.assertNotIn("magnet:?", repr(result.to_dict()))


if __name__ == "__main__":
    unittest.main()
