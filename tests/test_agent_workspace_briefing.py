"""Media Agent 本地系统简报的聚合、路由与 API 安全测试。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.agent.models import Evidence, ToolResult
from app.agent.workspace_briefing_actions import summarize_workspace_briefing
from tests.support import IsolatedDatabaseTestCase


def _todo_result(*, status: str = "attention") -> ToolResult:
    areas = [
        {
            "source": "downloads",
            "status": "attention",
            "attention_count": 2,
            "active_count": 1,
            "waiting_count": 0,
            "reason_codes": ["download_needs_review"],
            "next_tool": "downloads.diagnose_queue",
        },
        {
            "source": "rss",
            "status": "waiting",
            "attention_count": 0,
            "active_count": 0,
            "waiting_count": 3,
            "reason_codes": ["rss_pending"],
            "next_tool": "rss.diagnose",
        },
    ]
    if status == "unavailable":
        areas = []
    return ToolResult(
        ok=status != "unavailable",
        status=status,
        summary="workspace",
        data={"areas": areas},
        evidence=[Evidence("workspace", "local", "2026-08-01T10:00:00+08:00")],
    )


def _indexer_result(*, status: str = "ready", attention: int = 0) -> ToolResult:
    return ToolResult(
        ok=status != "unavailable",
        status=status,
        summary="indexer",
        data={
            "counts": {
                "enabled": 4,
                "searchable": 4,
                "downloadable": 3,
                "attention": attention,
            }
        },
    )


class WorkspaceBriefingUnitTests(IsolatedDatabaseTestCase):
    def test_aggregates_local_snapshot_without_sensitive_values(self):
        config_values = {
            "JELLYFIN_ENABLED": "1",
            "JELLYFIN_URL": "https://private.internal",
            "JELLYFIN_API_KEY": "PRIVATE-TOKEN",
            "EMBY_ENABLED": "0",
        }
        with (
            patch(
                "app.agent.workspace_briefing_actions.summarize_workspace_todo",
                return_value=_todo_result(),
            ),
            patch(
                "app.agent.workspace_briefing_actions.diagnose_indexer_readiness",
                return_value=_indexer_result(),
            ),
            patch(
                "app.agent.workspace_briefing_actions.config.get",
                side_effect=lambda key, default="": config_values.get(key, default),
            ),
        ):
            result = summarize_workspace_briefing({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["probe_mode"], "local_snapshot")
        self.assertFalse(result.data["network_accessed"])
        self.assertFalse(result.data["content_filesystem_scanned"])
        self.assertEqual(result.data["attention_total"], 2)
        self.assertEqual(result.data["active_total"], 1)
        self.assertEqual(result.data["waiting_total"], 3)
        self.assertEqual(
            [item["source"] for item in result.data["areas"]],
            ["downloads", "rss", "indexers", "media_servers"],
        )
        media = result.data["areas"][-1]
        self.assertEqual(media["status"], "ready")
        self.assertEqual(media["ready_count"], 1)
        self.assertEqual(media["connectivity"], "not_probed")
        self.assertEqual(
            result.data["coverage"]["not_probed"],
            ["media_server_connectivity", "cloud_directory_pending_scan"],
        )
        self.assertEqual(result.suggestions, ["检查下载队列里的异常"])
        self.assertNotIn("downloads.diagnose_queue", " ".join(result.suggestions))
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("private.internal", serialized)
        self.assertNotIn("PRIVATE-TOKEN", serialized)

    def test_partial_failures_are_not_reported_as_zero_or_healthy(self):
        with (
            patch(
                "app.agent.workspace_briefing_actions.summarize_workspace_todo",
                side_effect=RuntimeError("PRIVATE database error"),
            ),
            patch(
                "app.agent.workspace_briefing_actions.diagnose_indexer_readiness",
                return_value=_indexer_result(),
            ),
            patch(
                "app.agent.workspace_briefing_actions.config.get",
                side_effect=RuntimeError("PRIVATE config error"),
            ),
        ):
            result = summarize_workspace_briefing({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(
            set(result.data["coverage"]["unavailable"]),
            {
                "downloads",
                "rss",
                "organize",
                "strm",
                "local_media",
                "download_verification",
                "library_patrol",
                "media_servers",
            },
        )
        self.assertIn("indexers", result.data["coverage"]["available"])
        self.assertNotEqual(result.summary, "系统本地状态未发现待处理事项")
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("PRIVATE", serialized)

    def test_unavailable_todo_result_uses_attempt_evidence(self):
        unavailable_areas = [
            {
                "source": source,
                "status": "unavailable",
                "attention_count": 0,
                "active_count": 0,
                "waiting_count": 0,
                "reason_codes": ["local_snapshot_unavailable"],
                "next_tool": next_tool,
            }
            for source, next_tool in (
                ("downloads", "downloads.diagnose_queue"),
                ("rss", "rss.diagnose"),
                ("organize", "guangya.organize.status"),
                ("strm", "strm.triage_failures"),
                ("local_media", "local_media.diagnose"),
                ("download_verification", "downloads.diagnose_queue"),
                ("library_patrol", "library.patrol_status"),
            )
        ]
        todo = ToolResult(
            False, "unavailable", "unavailable", data={"areas": unavailable_areas}
        )
        with (
            patch(
                "app.agent.workspace_briefing_actions.summarize_workspace_todo",
                return_value=todo,
            ),
            patch(
                "app.agent.workspace_briefing_actions.diagnose_indexer_readiness",
                return_value=_indexer_result(),
            ),
            patch("app.agent.workspace_briefing_actions.config.get", return_value="0"),
        ):
            result = summarize_workspace_briefing({})
        workspace_evidence = next(

                item
                for item in result.evidence
                if item.source == "workspace_local_snapshot"

        )
        self.assertIn("尝试读取", workspace_evidence.description)
        self.assertNotIn("读取下载", workspace_evidence.description)

    def test_disabled_and_not_configured_are_distinct_from_unavailable(self):
        with (
            patch(
                "app.agent.workspace_briefing_actions.summarize_workspace_todo",
                return_value=ToolResult(
                    True,
                    "empty",
                    "empty",
                    data={
                        "areas": [
                            {
                                "source": "downloads",
                                "status": "idle",
                                "attention_count": 0,
                                "active_count": 0,
                                "waiting_count": 0,
                                "reason_codes": [],
                                "next_tool": "downloads.diagnose_queue",
                            }
                        ]
                    },
                ),
            ),
            patch(
                "app.agent.workspace_briefing_actions.diagnose_indexer_readiness",
                return_value=_indexer_result(status="disabled"),
            ),
            patch("app.agent.workspace_briefing_actions.config.get", return_value="0"),
        ):
            result = summarize_workspace_briefing({})
        self.assertEqual(result.status, "healthy")
        self.assertEqual(result.data["coverage"]["disabled"], ["indexers"])
        self.assertEqual(result.data["coverage"]["not_configured"], ["media_servers"])
        self.assertEqual(result.data["coverage"]["unavailable"], [])
