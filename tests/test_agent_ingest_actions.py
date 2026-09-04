"""Agent 统一资源接入链路与传统 Telegram 隔离测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent.errors import AgentToolError
from app.agent.ingest_actions import (
    AgentIngestSessionStore,
    IngestActions,
    ingest_inspect_arguments,
    ingest_status_arguments,
    ingest_submit_arguments,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.recent_resource_candidates import (
    RecentResourceCandidateStore,
    new_resource_search_id,
    safe_resource_snapshot,
)
from app.agent.state_commit import (
    AgentStateCommitBuffer,
    defer_agent_state_commits,
    stage_agent_resource_candidates,
)

_MAGNET = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Demo"
_SHARE = "https://www.guangyapan.com/s/1938060615538466851_aecd7hu726g3whGl#/share"


def _resource_result(result_id: str, title: str) -> ToolResult:
    return ToolResult(
        True,
        "success",
        "searched",
        data={
            "items": [
                {
                    "result_id": result_id,
                    "title": title,
                    "site_id": "mikan",
                    "site_name": "Mikan",
                    "size_text": "1.2 GiB",
                    "download_state": "ready",
                    "download_kinds": ["magnet"],
                }
            ]
        },
    )


class AgentIngestArgumentTests(unittest.TestCase):
    def test_submit_arguments_are_public_coordinates_only(self):
        self.assertEqual(
            ingest_submit_arguments(
                {
                    "source_type": "resource_candidates",
                    "positions": [2, 1, 2],
                    "target": "both",
                }
            ),
            {
                "source_type": "resource_candidates",
                "positions": [2, 1],
                "target": "both",
            },
        )
        with self.assertRaises(AgentToolError):
            ingest_submit_arguments(
                {"source_type": "direct_url", "target": "qb", "input": _MAGNET}
            )

    def test_resource_search_id_is_optional_but_strict_when_present(self):
        search_id = "rs_1234567890abcdef"
        self.assertEqual(
            ingest_submit_arguments(
                {
                    "source_type": "resource_candidates",
                    "positions": [1],
                    "target": "qb",
                    "search_id": search_id,
                }
            )["search_id"],
            search_id,
        )
        with self.assertRaises(AgentToolError):
            ingest_submit_arguments(
                {
                    "source_type": "resource_candidates",
                    "positions": [1],
                    "target": "qb",
                    "search_id": "latest",
                }
            )
        with self.assertRaises(AgentToolError):
            ingest_submit_arguments(
                {"source_type": "direct_url", "target": "qb", "search_id": search_id}
            )

    def test_resource_candidate_reference_is_private_coordinate_only(self):
        reference = "ref_1234567890abcdefghijkl"
        self.assertEqual(
            ingest_submit_arguments(
                {
                    "source_type": "resource_candidates",
                    "positions": [1],
                    "target": "guangya",
                    "resource_candidates_ref": reference,
                }
            )["resource_candidates_ref"],
            reference,
        )
        self.assertEqual(
            ingest_inspect_arguments(
                {
                    "source_type": "resource_candidates",
                    "resource_candidates_ref": reference,
                }
            )["resource_candidates_ref"],
            reference,
        )
        with self.assertRaises(AgentToolError):
            ingest_submit_arguments(
                {
                    "source_type": "resource_candidates",
                    "positions": [1],
                    "target": "guangya",
                    "resource_candidates_ref": "latest",
                }
            )

    def test_direct_and_share_submit_accept_only_typed_ingest_snapshot_reference(self):
        reference = "ref_1234567890abcdefghijkl"
        direct = ingest_submit_arguments(
            {
                "source_type": "direct_url",
                "target": "qb",
                "ingest_snapshot_ref": reference,
            }
        )
        share = ingest_submit_arguments(
            {
                "source_type": "guangya_share",
                "ingest_snapshot_ref": reference,
            }
        )
        self.assertEqual(direct["ingest_snapshot_ref"], reference)
        self.assertEqual(share["ingest_snapshot_ref"], reference)
        with self.assertRaises(AgentToolError):
            ingest_submit_arguments(
                {
                    "source_type": "direct_url",
                    "target": "qb",
                    "ingest_snapshot_ref": "latest",
                }
            )
        with self.assertRaises(AgentToolError):
            ingest_submit_arguments(
                {
                    "source_type": "direct_url",
                    "target": "qb",
                    "resource_candidates_ref": reference,
                }
            )

    def test_share_defaults_to_guangya_and_all_items(self):
        self.assertEqual(
            ingest_submit_arguments({"source_type": "guangya_share"}),
            {"source_type": "guangya_share", "target": "guangya", "positions": []},
        )
        self.assertEqual(
            ingest_submit_arguments({"source_type": "guangya_share", "positions": []}),
            {"source_type": "guangya_share", "target": "guangya", "positions": []},
        )

    def test_resource_inspection_rejects_input(self):
        with self.assertRaises(AgentToolError):
            ingest_inspect_arguments(
                {"source_type": "resource_candidates", "input": "secret"}
            )

    def test_status_requires_public_positive_request_number(self):
        self.assertEqual(
            ingest_status_arguments({"request_number": 12}), {"request_number": 12}
        )
        with self.assertRaises(AgentToolError):
            ingest_status_arguments({"request_id": 12})


class AgentIngestActionTests(unittest.TestCase):
    def setUp(self):
        self.store = AgentIngestSessionStore()
        self.resources = RecentResourceCandidateStore()
        self.actions = IngestActions(
            store=self.store, recent_resource_store=self.resources
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

    def test_ingest_memory_snapshots_are_isolated_by_conversation_session(self):
        first_context = ToolContext(owner="owner-a", session_id="session-a")
        second_context = ToolContext(owner="owner-a", session_id="session-b")
        self.actions.inspect(
            {"source_type": "direct_url", "input": _MAGNET}, first_context
        )
        self.actions.inspect(
            {"source_type": "direct_url", "input": _MAGNET}, second_context
        )

        first = self.store.get(
            owner="owner-a",
            source_type="direct_url",
            conversation_session_id="session-a",
        )
        second = self.store.get(
            owner="owner-a",
            source_type="direct_url",
            conversation_session_id="session-b",
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.session_id, second.session_id)

    @patch(
        "app.agent.ingest_actions.download_target_readiness",
        return_value={"qb": True},
    )
    def test_direct_reference_rebuilds_preview_after_memory_store_restart(
        self, _readiness
    ):
        context = ToolContext(owner="owner-a", session_id="session-a")
        inspected = self.actions.inspect(
            {"source_type": "direct_url", "input": _MAGNET}, context
        )
        reference_value = inspected.references[0].value
        restarted = IngestActions(
            store=AgentIngestSessionStore(),
            recent_resource_store=RecentResourceCandidateStore(),
        )
        resolved_arguments = {
            "source_type": "direct_url",
            "target": "qb",
            "positions": [],
            "ingest_snapshot": reference_value,
        }

        preview, fingerprint = restarted.prepare_submit(resolved_arguments, context)

        self.assertTrue(preview.ok)
        self.assertTrue(fingerprint)
        self.assertEqual(preview.data["source_type"], "direct_url")
        self.assertIsNone(
            restarted.store.get(
                owner="owner-a",
                source_type="direct_url",
                conversation_session_id="session-a",
            )
        )

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
        "app.agent.ingest_actions.download_target_readiness", return_value={"qb": True}
    )
    def test_direct_link_requires_confirmation_and_executes_via_dispatcher(
        self, _readiness, create_request_mock, dispatch_mock, _get_request
    ):
        self.actions.inspect(
            {"source_type": "direct_url", "input": _MAGNET}, self.context
        )
        arguments = {"source_type": "direct_url", "target": "qb", "positions": []}
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
        "app.agent.ingest_actions.download_target_readiness",
        return_value={"guangya": True},
    )
    @patch(
        "app.agent.ingest_actions.inspect_share_for_transfer",
        return_value={
            "preview_id": "preview-secret",
            "share_id": "share-secret",
            "files": [
                {
                    "id": "file-secret-1",
                    "name": "Episode 1.mkv",
                    "is_dir": False,
                    "size": 10,
                },
                {
                    "id": "file-secret-2",
                    "name": "Episode 2.mkv",
                    "is_dir": False,
                    "size": 20,
                },
            ],
            "selected_ids": ["file-secret-1", "file-secret-2"],
            "target_id": "target-secret",
            "target_name": "默认目录",
            "expires_in": 900,
        },
    )
    def test_share_reference_revalidates_after_memory_store_restart(
        self, inspect_share, _readiness
    ):
        context = ToolContext(owner="owner-a", session_id="session-a")
        inspected = self.actions.inspect(
            {"source_type": "guangya_share", "input": _SHARE}, context
        )
        reference_value = inspected.references[0].value
        restarted = IngestActions(
            store=AgentIngestSessionStore(),
            recent_resource_store=RecentResourceCandidateStore(),
        )

        preview, fingerprint = restarted.prepare_submit(
            {
                "source_type": "guangya_share",
                "target": "guangya",
                "positions": [1, 2],
                "ingest_snapshot": reference_value,
            },
            context,
        )

        self.assertTrue(preview.ok)
        self.assertTrue(fingerprint)
        self.assertEqual(preview.data["count"], 2)
        self.assertEqual(inspect_share.call_count, 2)

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

    @patch("app.agent.indexer_candidate_actions.prepare_submit_resource")
    def test_staged_candidates_override_older_persisted_latest(self, prepare_resource):
        prepare_resource.side_effect = lambda arguments: (
            ToolResult(True, "confirmation_required", "preview", data={"resource": {}}),
            f"{arguments['result_id']}:{arguments['target']}",
        )
        self.resources.capture(
            owner="owner-a", result=_resource_result("resource-old-0001", "Old")
        )
        staged_search_id = new_resource_search_id()
        staged_snapshot = safe_resource_snapshot(
            _resource_result("resource-new-0001", "New"), search_id=staged_search_id
        )
        buffer = AgentStateCommitBuffer(owner="owner-a")
        arguments = ingest_submit_arguments(
            {"source_type": "resource_candidates", "positions": [1], "target": "qb"}
        )
        with defer_agent_state_commits(buffer):
            self.assertTrue(
                stage_agent_resource_candidates(
                    owner="owner-a", snapshot=staged_snapshot
                )
            )
            self.actions.prepare_submit(arguments, self.context)
        self.assertEqual(arguments["search_id"], staged_search_id)
        prepare_resource.assert_called_once_with(
            {"result_id": "resource-new-0001", "target": "qb"}
        )
        buffer.discard()

    def test_kernel_session_does_not_fall_back_to_another_sessions_latest_candidates(
        self,
    ):
        self.resources.capture(
            owner="owner-a", result=_resource_result("resource-other-001", "Other")
        )
        with self.assertRaises(AgentToolError) as raised:
            self.actions.prepare_submit(
                ingest_submit_arguments(
                    {
                        "source_type": "resource_candidates",
                        "positions": [1],
                        "target": "qb",
                    }
                ),
                ToolContext(owner="owner-a", session_id="session-a"),
            )
        self.assertEqual(raised.exception.code, "precondition_failed")
        self.assertIn("资源候选引用", raised.exception.safe_message)

    @patch("app.agent.indexer_candidate_actions.submit_resource_confirmed")
    @patch("app.agent.indexer_candidate_actions.prepare_submit_resource")
    def test_confirm_uses_frozen_snapshot_after_a_newer_search(
        self, prepare_resource, submit_resource
    ):
        prepare_resource.side_effect = lambda arguments: (
            ToolResult(True, "confirmation_required", "preview", data={"resource": {}}),
            f"{arguments['result_id']}:{arguments['target']}",
        )
        submit_resource.side_effect = lambda arguments, _expected: ToolResult(
            True, "accepted", "submitted", data={"result_id": arguments["result_id"]}
        )
        first_search_id = self.resources.capture(
            owner="owner-a", result=_resource_result("resource-first-001", "First")
        )
        arguments = ingest_submit_arguments(
            {"source_type": "resource_candidates", "positions": [1], "target": "qb"}
        )
        _preview, expected_context = self.actions.prepare_submit(
            arguments, self.context
        )
        self.assertEqual(arguments["search_id"], first_search_id)
        self.resources.capture(
            owner="owner-a", result=_resource_result("resource-second-01", "Second")
        )
        result = self.actions.execute_submit(arguments, expected_context, self.context)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["result_id"], "resource-first-001")
        self.assertEqual(
            [call.args[0]["result_id"] for call in prepare_resource.call_args_list],
            ["resource-first-001", "resource-first-001"],
        )
        submit_resource.assert_called_once_with(
            {"result_id": "resource-first-001", "target": "qb"}, "resource-first-001:qb"
        )

    @patch("app.agent.indexer_candidate_actions.submit_resource_confirmed")
    @patch("app.agent.indexer_candidate_actions.prepare_submit_resource")
    def test_kernel_reference_snapshot_survives_preview_and_confirmation(
        self, prepare_resource, submit_resource
    ):
        prepare_resource.side_effect = lambda arguments: (
            ToolResult(True, "confirmation_required", "preview", data={"resource": {}}),
            f"{arguments['result_id']}:{arguments['target']}",
        )
        submit_resource.side_effect = lambda arguments, _expected: ToolResult(
            True, "accepted", "submitted", data={"result_id": arguments["result_id"]}
        )
        search_id = new_resource_search_id()
        snapshot = safe_resource_snapshot(
            _resource_result("resource-kernel-0001", "Kernel"),
            search_id=search_id,
        )
        arguments = {
            "source_type": "resource_candidates",
            "positions": [1],
            "target": "guangya",
            "resource_candidates": snapshot,
        }

        preview, expected_context = self.actions.prepare_submit(arguments, self.context)
        self.assertTrue(preview.ok)
        self.assertEqual(arguments["search_id"], search_id)
        self.assertIsNone(self.resources.get(owner="owner-a"))

        result = self.actions.execute_submit(arguments, expected_context, self.context)
        self.assertTrue(result.ok)
        submit_resource.assert_called_once_with(
            {"result_id": "resource-kernel-0001", "target": "guangya"},
            "resource-kernel-0001:guangya",
        )

    @patch("app.agent.indexer_candidate_actions.submit_resource_confirmed")
    @patch("app.agent.indexer_candidate_actions.prepare_submit_resource")
    def test_confirm_fails_closed_when_frozen_snapshot_was_evicted(
        self, prepare_resource, submit_resource
    ):
        resources = RecentResourceCandidateStore(max_snapshots_per_owner=1)
        actions = IngestActions(store=self.store, recent_resource_store=resources)
        prepare_resource.side_effect = lambda arguments: (
            ToolResult(True, "confirmation_required", "preview", data={"resource": {}}),
            f"{arguments['result_id']}:{arguments['target']}",
        )
        resources.capture(
            owner="owner-a", result=_resource_result("resource-first-001", "First")
        )
        arguments = ingest_submit_arguments(
            {"source_type": "resource_candidates", "positions": [1], "target": "qb"}
        )
        _preview, expected_context = actions.prepare_submit(arguments, self.context)
        resources.capture(
            owner="owner-a", result=_resource_result("resource-second-01", "Second")
        )
        with self.assertRaises(AgentToolError) as raised:
            actions.execute_submit(arguments, expected_context, self.context)
        self.assertEqual(raised.exception.code, "confirmation_stale")
        submit_resource.assert_not_called()

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
