"""Media Agent 工作区下一步的安全投影、路由与 API 回归测试。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.agent.workspace_next_actions import (
    resolve_workspace_action_handoff,
    summarize_workspace_next_actions,
    workspace_action_handoff_arguments,
)
from tests.support import IsolatedDatabaseTestCase

_ACTION_KEYS = {
    "action_key",
    "source",
    "status",
    "attention_count",
    "reason_codes",
    "label",
    "why",
    "target_tool",
    "prompt",
    "risk",
    "requires_confirmation",
    "precondition",
    "staleness",
}


def _todo(*, ok=True, status="attention", areas=None, **data) -> ToolResult:
    return ToolResult(
        ok=ok,
        status=status,
        summary="PRIVATE CHILD SUMMARY",
        data={"areas": list(areas or []), **data},
        evidence=[Evidence("PRIVATE", "https://private.example/token", "2026-08-05")],
        suggestions=["PRIVATE CHILD SUGGESTION"],
        error="PRIVATE CHILD ERROR",
    )


def _area(source: str, reason: str, count: object = 1, **extra):
    return {
        "source": source,
        "status": "attention",
        "attention_count": count,
        "active_count": 0,
        "waiting_count": 0,
        "reason_codes": [reason, "PRIVATE_REASON"],
        "title": "PRIVATE-TITLE",
        "path": "/private/path",
        "url": "https://private.example/?token=SECRET",
        **extra,
    }


class WorkspaceNextActionsUnitTests(IsolatedDatabaseTestCase):
    def test_handoff_arguments_and_fresh_resolution_are_strict(self):
        self.assertEqual(
            workspace_action_handoff_arguments({"action_key": " review_rss "}),
            {"action_key": "review_rss"},
        )
        for invalid in (
            {},
            {"action_key": 1},
            {"action_key": "review_rss", "target_tool": "rss.retry_failed_to_qb"},
            {"action_key": "rss.retry_failed_to_qb"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                workspace_action_handoff_arguments(invalid)
        fresh = ToolResult(
            True,
            "attention",
            "有 1 个下一步",
            data={"actions": [{"action_key": "review_rss", "target_tool": "PRIVATE"}]},
        )
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_next_actions",
            return_value=fresh,
        ) as summarize:
            resolved = resolve_workspace_action_handoff({"action_key": "review_rss"})
        summarize.assert_called_once_with({})
        self.assertEqual(
            resolved,
            {
                "action_key": "review_rss",
                "label": "检查 RSS 订阅",
                "target_tool": "rss.diagnose",
                "arguments": {},
            },
        )

    def test_handoff_rejects_stale_or_unavailable_snapshot(self):
        stale = ToolResult(True, "empty", "没有下一步", data={"actions": []})
        unavailable = ToolResult(False, "unavailable", "不可用", data={"actions": []})
        for snapshot in (stale, unavailable):
            with (
                self.subTest(status=snapshot.status),
                patch(
                    "app.agent.workspace_next_actions.summarize_workspace_next_actions",
                    return_value=snapshot,
                ),
                self.assertRaises(AgentToolError) as captured,
            ):
                resolve_workspace_action_handoff({"action_key": "review_downloads"})
            self.assertEqual(captured.exception.code, "precondition_failed")

    def test_projection_is_allowlisted_deduplicated_and_stably_ordered(self):
        areas = [
            _area("library_patrol", "library_patrol_updates_available", 7),
            _area("rss", "rss_failed", 3),
            _area("downloads", "download_needs_review", 2),
            _area("rss", "rss_failed", 99),
            _area("organize", "organize_issue", 4),
            _area("strm", "strm_open_failure", 5),
            _area("local_media", "local_media_failed", 6),
            _area("download_verification", "download_verification_attention", 8),
            _area("unknown", "unknown_reason", 9),
        ]
        child = _todo(
            areas=areas,
            attention_total=999,
            active_total=2,
            waiting_total=3,
            unavailable_areas=["unknown", "rss", "downloads", "rss"],
            token="PRIVATE-DATA",
        )
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_todo",
            return_value=child,
        ) as summarize:
            result = summarize_workspace_next_actions({})
        summarize.assert_called_once_with({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        actions = result.data["actions"]
        self.assertEqual(
            [item["source"] for item in actions],
            [
                "downloads",
                "rss",
                "organize",
                "strm",
                "local_media",
                "download_verification",
                "library_patrol",
            ],
        )
        self.assertEqual(actions[1]["attention_count"], 3)
        for action in actions:
            self.assertEqual(set(action), _ACTION_KEYS)
            self.assertEqual(action["risk"], "read")
            self.assertFalse(action["requires_confirmation"])
            self.assertEqual(action["status"], "attention")
            self.assertNotIn("PRIVATE_REASON", action["reason_codes"])
        self.assertEqual(result.data["unavailable_areas"], ["downloads", "rss"])
        self.assertFalse(result.data["network_accessed"])
        self.assertFalse(result.data["filesystem_accessed"])
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "PRIVATE-TITLE",
            "/private/path",
            "private.example",
            "SECRET",
            "PRIVATE-DATA",
            "PRIVATE CHILD",
            "PRIVATE_REASON",
        ):
            self.assertNotIn(secret, payload)

    def test_unknown_reason_non_attention_and_invalid_counts_do_not_create_actions(
        self,
    ):
        areas = [
            _area("downloads", "future_reason", 1),
            _area("rss", "rss_failed", 0),
            _area("organize", "organize_issue", -2),
            _area("strm", "strm_open_failure", "bad"),
            _area("local_media", "local_media_failed", 3, status="active"),
        ]
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_todo",
            return_value=_todo(status="empty", areas=areas),
        ):
            result = summarize_workspace_next_actions({})
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.data["actions"], [])

    def test_partial_and_unavailable_results_do_not_leak_child_errors(self):
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_todo",
            return_value=_todo(
                status="partial",
                areas=[_area("downloads", "download_needs_review")],
                unavailable_areas=["rss", "PRIVATE-SOURCE"],
            ),
        ):
            partial = summarize_workspace_next_actions({})
        self.assertTrue(partial.ok)
        self.assertEqual(partial.status, "partial")
        self.assertEqual(partial.data["unavailable_areas"], ["rss"])
        self.assertEqual(len(partial.data["actions"]), 1)
        with patch(
            "app.agent.workspace_next_actions.summarize_workspace_todo",
            return_value=_todo(ok=False, status="unavailable", areas=[]),
        ):
            unavailable = summarize_workspace_next_actions({})
        self.assertFalse(unavailable.ok)
        self.assertEqual(unavailable.status, "unavailable")
        self.assertNotIn(
            "PRIVATE", json.dumps(unavailable.to_dict(), ensure_ascii=False)
        )

    def test_active_waiting_and_empty_have_no_action_cards(self):
        for status in ("active", "waiting", "empty"):
            with (
                self.subTest(status=status),
                patch(
                    "app.agent.workspace_next_actions.summarize_workspace_todo",
                    return_value=_todo(status=status, areas=[]),
                ),
            ):
                result = summarize_workspace_next_actions({})
                self.assertTrue(result.ok)
                self.assertEqual(result.status, status)
                self.assertEqual(result.data["actions"], [])
