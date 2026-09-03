"""Media Agent 媒体系统健康总检的聚合、路由与 API 安全测试。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.agent.media_health_actions import diagnose_workspace_health
from app.agent.models import ToolResult
from tests.support import IsolatedDatabaseTestCase


def _workspace_result(*, status: str = "healthy", attention: int = 0) -> ToolResult:
    return ToolResult(
        ok=status != "unavailable",
        status=status,
        summary="workspace private summary",
        data={
            "attention_total": attention,
            "active_total": 2,
            "waiting_total": 3,
            "coverage": {"unavailable": ["rss"] if status == "partial" else []},
            "secret": "PRIVATE-WORKSPACE",
        },
        error="PRIVATE-WORKSPACE-ERROR",
    )


def _config_result(*, errors: int = 0, warnings: int = 0) -> ToolResult:
    status = (
        "healthy"
        if not errors and (not warnings)
        else "attention"
        if errors
        else "degraded"
    )
    return ToolResult(
        ok=errors == 0,
        status=status,
        summary="config private summary",
        data={
            "counts": {"errors": errors, "warnings": warnings},
            "components": [
                {"name": "jellyfin", "status": "ready"},
                {"name": "tmdb", "status": "not_configured" if warnings else "ready"},
            ],
            "issues": [{"message": "PRIVATE-CONFIG"}],
        },
        error="PRIVATE-CONFIG-ERROR",
    )


def _media_result(
    *,
    status: str = "healthy",
    enabled: int = 1,
    configured: int = 1,
    online: int = 1,
    compatible: int = 1,
    attention: int = 0,
    network_accessed: bool = True,
    nodes: list[dict] | None = None,
) -> ToolResult:
    if nodes is None:
        nodes = [{"slot": "jellyfin", "secret": "PRIVATE-NODE"}]
    return ToolResult(
        ok=status not in {"unavailable"},
        status=status,
        summary="media private summary",
        data={
            "network_accessed": network_accessed,
            "counts": {
                "enabled": enabled,
                "configured": configured,
                "online": online,
                "compatible": compatible,
                "attention": attention,
            },
            "nodes": nodes,
            "url": "https://private.internal",
        },
        error="PRIVATE-MEDIA-ERROR",
    )


class WorkspaceHealthUnitTests(IsolatedDatabaseTestCase):
    def test_aggregates_fixed_safe_schema_and_real_network_flag(self):
        with (
            patch(
                "app.agent.media_health_actions.summarize_workspace_briefing",
                return_value=_workspace_result(),
            ),
            patch(
                "app.agent.media_health_actions.diagnose_config",
                return_value=_config_result(),
            ),
            patch(
                "app.agent.media_health_actions.diagnose_media_servers",
                return_value=_media_result(),
            ),
        ):
            result = diagnose_workspace_health({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "healthy")
        self.assertEqual(result.data["probe_mode"], "local_and_media_endpoints")
        self.assertTrue(result.data["network_accessed"])
        self.assertFalse(result.data["content_filesystem_scanned"])
        self.assertEqual(result.data["attention_total"], 0)
        self.assertEqual(
            result.data["coverage"]["requested"],
            ["workspace", "configuration", "media_servers"],
        )
        self.assertEqual(
            result.data["coverage"]["available"],
            ["workspace", "configuration", "media_servers"],
        )
        self.assertEqual(result.data["coverage"]["unavailable"], [])
        self.assertEqual(
            result.data["coverage"]["not_probed"],
            [
                "cloud_directory_content_scan",
                "per_title_episode_audit",
                "per_title_update_check",
                "indexer_network_search",
                "download_submission",
            ],
        )
        self.assertEqual(
            [item["source"] for item in result.data["areas"]],
            ["workspace", "configuration", "media_servers"],
        )
        self.assertEqual(result.data["areas"][0]["active_count"], 2)
        self.assertEqual(result.data["areas"][1]["ready_component_count"], 2)
        self.assertEqual(result.data["areas"][2]["online_count"], 1)
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "PRIVATE-WORKSPACE",
            "PRIVATE-CONFIG",
            "PRIVATE-NODE",
            "private.internal",
            "private summary",
            "PRIVATE-MEDIA-ERROR",
        ):
            self.assertNotIn(secret, serialized)

    def test_partial_child_failure_is_explicit_and_other_areas_survive(self):
        with (
            patch(
                "app.agent.media_health_actions.summarize_workspace_briefing",
                side_effect=RuntimeError("PRIVATE database failure"),
            ),
            patch(
                "app.agent.media_health_actions.diagnose_config",
                return_value=_config_result(warnings=1),
            ),
            patch(
                "app.agent.media_health_actions.diagnose_media_servers",
                return_value=_media_result(
                    network_accessed=False,
                    enabled=0,
                    configured=0,
                    online=0,
                    compatible=0,
                ),
            ),
        ):
            result = diagnose_workspace_health({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertFalse(result.data["network_accessed"])
        self.assertEqual(result.data["coverage"]["unavailable"], ["workspace"])
        self.assertEqual(
            result.data["areas"][0]["reason_codes"], ["workspace_snapshot_unavailable"]
        )
        self.assertEqual(
            result.data["areas"][1]["reason_codes"], ["configuration_warnings"]
        )
        self.assertEqual(
            result.data["areas"][2]["reason_codes"], ["media_server_not_configured"]
        )
        self.assertGreaterEqual(result.data["attention_total"], 2)
        self.assertNotIn(
            "PRIVATE database failure", json.dumps(result.to_dict(), ensure_ascii=False)
        )

    def test_structured_offline_media_diagnosis_is_attention_not_missing_coverage(self):
        with (
            patch(
                "app.agent.media_health_actions.summarize_workspace_briefing",
                return_value=_workspace_result(),
            ),
            patch(
                "app.agent.media_health_actions.diagnose_config",
                return_value=_config_result(),
            ),
            patch(
                "app.agent.media_health_actions.diagnose_media_servers",
                return_value=_media_result(
                    status="unavailable",
                    online=0,
                    compatible=0,
                    attention=1,
                    nodes=[{"slot": "jellyfin", "connection_status": "unavailable"}],
                ),
            ),
        ):
            result = diagnose_workspace_health({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["coverage"]["unavailable"], [])
        media = result.data["areas"][2]
        self.assertEqual(media["status"], "attention")
        self.assertIn("media_server_offline", media["reason_codes"])
        self.assertIn("media_server_compatibility_attention", media["reason_codes"])

    def test_all_child_failures_return_safe_unavailable(self):
        with (
            patch(
                "app.agent.media_health_actions.summarize_workspace_briefing",
                side_effect=RuntimeError("PRIVATE-1"),
            ),
            patch(
                "app.agent.media_health_actions.diagnose_config",
                side_effect=RuntimeError("PRIVATE-2"),
            ),
            patch(
                "app.agent.media_health_actions.diagnose_media_servers",
                side_effect=RuntimeError("PRIVATE-3"),
            ),
        ):
            result = diagnose_workspace_health({})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(
            result.data["coverage"]["unavailable"],
            ["workspace", "configuration", "media_servers"],
        )
        self.assertFalse(result.data["network_accessed"])
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("PRIVATE-", serialized)
