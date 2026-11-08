"""Media Agent 工作区下一步的安全投影、路由与 API 回归测试。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.agent.errors import AgentToolError
from app.modules.workspace import (
    action_handoff_arguments,
    next_actions,
    resolve_action_handoff,
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


def _todo(*, ok=True, status="attention", areas=None, **data) -> dict:
    return {
        "ok": ok,
        "status": status,
        "summary": "PRIVATE CHILD SUMMARY",
        "data": {"areas": list(areas or []), **data},
        "evidence": [
            {
                "source": "PRIVATE",
                "description": "https://private.example/token",
                "collected_at": "2026-08-05",
            }
        ],
        "suggestions": ["PRIVATE CHILD SUGGESTION"],
        "error": "PRIVATE CHILD ERROR",
    }


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
    def test_empty_local_snapshot_keeps_health_scope_in_model_dto_without_probing(self):
        from app.modules.workspace import WORKSPACE_BACKLOG_SCOPE, _backlog

        with patch("app.modules.workspace.connection") as probe:
            results = (_backlog(), next_actions())
        probe.assert_not_called()
        for result in results:
            with self.subTest(
                tool=result["data"].get("source_tool", "diagnostics.overview")
            ):
                self.assertEqual(result["status"], "empty")
                self.assertEqual(result["data"]["scope"], WORKSPACE_BACKLOG_SCOPE)
                self.assertFalse(result["data"]["network_accessed"])
                self.assertEqual(result["data"]["attention_total"], 0)
                self.assertIn("本地任务记录", result["summary"])
                self.assertEqual(
                    json.loads(json.dumps(result))["data"]["scope"],
                    WORKSPACE_BACKLOG_SCOPE,
                )
        self.assertEqual(results[1]["data"]["actions"], [])
        self.assertEqual(results[1]["suggestions"], [])

    def test_scope_is_fixed_even_when_child_contains_a_broader_claim(self):
        from app.modules.workspace import WORKSPACE_BACKLOG_SCOPE

        for status in (
            "attention",
            "active",
            "waiting",
            "empty",
            "partial",
            "unavailable",
        ):
            with (
                self.subTest(status=status),
                patch(
                    "app.modules.workspace._backlog",
                    return_value=_todo(
                        status=status,
                        ok=status != "unavailable",
                        scope="PRIVATE: 所有模块正常",
                    ),
                ),
            ):
                result = next_actions()
            self.assertEqual(
                result["status"], "empty" if status == "attention" else status
            )
            self.assertEqual(result["data"]["scope"], WORKSPACE_BACKLOG_SCOPE)
            self.assertNotIn("PRIVATE", json.dumps(result, ensure_ascii=False))

    def test_handoff_arguments_and_fresh_resolution_are_strict(self):
        self.assertEqual(
            action_handoff_arguments({"action_key": " review_rss "}),
            {"action_key": "review_rss"},
        )
        for invalid in (
            {},
            {"action_key": 1},
            {"action_key": "review_rss", "target_tool": "rss.retry_failed"},
            {"action_key": "rss.retry_failed"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                action_handoff_arguments(invalid)
        fresh = {
            "ok": True,
            "status": "attention",
            "summary": "有 1 个下一步",
            "data": {
                "actions": [{"action_key": "review_rss", "target_tool": "PRIVATE"}]
            },
        }
        with patch(
            "app.modules.workspace.next_actions", return_value=fresh
        ) as summarize:
            resolved = resolve_action_handoff({"action_key": "review_rss"})
        summarize.assert_called_once_with()
        self.assertEqual(
            resolved,
            {
                "action_key": "review_rss",
                "label": "检查 RSS 订阅",
                "target_tool": "agent.run_code",
                "arguments": {
                    "code": "import json\nfrom mf import rss\nprint(json.dumps(rss.diagnose(), ensure_ascii=False))"
                },
            },
        )

    def test_handoff_rejects_stale_or_unavailable_snapshot(self):
        stale = {
            "ok": True,
            "status": "empty",
            "summary": "没有下一步",
            "data": {"actions": []},
        }
        unavailable = {
            "ok": False,
            "status": "unavailable",
            "summary": "不可用",
            "data": {"actions": []},
        }
        for snapshot in (stale, unavailable):
            with (
                self.subTest(status=snapshot["status"]),
                patch("app.modules.workspace.next_actions", return_value=snapshot),
                self.assertRaises(AgentToolError) as captured,
            ):
                resolve_action_handoff({"action_key": "review_downloads"})
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
        with patch("app.modules.workspace._backlog", return_value=child) as summarize:
            result = next_actions()
        summarize.assert_called_once_with()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "attention")
        actions = result["data"]["actions"]
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
        self.assertEqual(actions[-1]["target_tool"], "agent.run_code")
        for action in actions:
            self.assertEqual(set(action), _ACTION_KEYS)
            self.assertEqual(action["risk"], "read")
            self.assertFalse(action["requires_confirmation"])
            self.assertEqual(action["status"], "attention")
            self.assertNotIn("PRIVATE_REASON", action["reason_codes"])
        self.assertEqual(result["data"]["unavailable_areas"], ["downloads", "rss"])
        self.assertFalse(result["data"]["network_accessed"])
        self.assertFalse(result["data"]["filesystem_accessed"])
        payload = json.dumps(result, ensure_ascii=False)
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
            "app.modules.workspace._backlog",
            return_value=_todo(status="empty", areas=areas),
        ):
            result = next_actions()
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["data"]["actions"], [])

    def test_partial_and_unavailable_results_do_not_leak_child_errors(self):
        with patch(
            "app.modules.workspace._backlog",
            return_value=_todo(
                status="partial",
                areas=[_area("downloads", "download_needs_review")],
                unavailable_areas=["rss", "PRIVATE-SOURCE"],
            ),
        ):
            partial = next_actions()
        self.assertTrue(partial["ok"])
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["data"]["unavailable_areas"], ["rss"])
        self.assertEqual(len(partial["data"]["actions"]), 1)
        with patch(
            "app.modules.workspace._backlog",
            return_value=_todo(ok=False, status="unavailable", areas=[]),
        ):
            unavailable = next_actions()
        self.assertFalse(unavailable["ok"])
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertNotIn("PRIVATE", json.dumps(unavailable, ensure_ascii=False))

    def test_active_waiting_and_empty_have_no_action_cards(self):
        for status in ("active", "waiting", "empty"):
            with (
                self.subTest(status=status),
                patch(
                    "app.modules.workspace._backlog",
                    return_value=_todo(status=status, areas=[]),
                ),
            ):
                result = next_actions()
                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], status)
                self.assertEqual(result["data"]["actions"], [])
