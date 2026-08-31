"""项目级 Agent 跨模块执行能力的安全回归。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from app import database as db
from app.agent.llm_router import _native_read_capabilities, is_agent_action_request
from app.agent.local_media_scan_actions import (
    local_media_scan_arguments,
    prepare_scan_local_media_sources,
    scan_local_media_sources_confirmed,
)
from app.agent.media_library_actions import (
    media_library_refresh_arguments,
    prepare_refresh_media_library,
    refresh_media_library_confirmed,
)
from app.agent.media_proxy_actions import (
    media_proxy_restart_arguments,
    prepare_restart_media_proxy_instance,
    restart_media_proxy_instance_confirmed,
)
from app.agent.objective_contract import infer_agent_objective
from app.agent.registry import AgentToolError
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.modules.local_media_scheduler import LocalMediaScheduler
from app.modules.media_server_profiles import MediaServerProfile
from tests.support import IsolatedDatabaseTestCase


class AgentGeneralistOperationTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")
            conn.execute("DELETE FROM media_proxy_instances")
        reset_agent_service_for_tests()

    def tearDown(self) -> None:
        reset_agent_service_for_tests()

    def test_registry_and_objectives_cover_cross_module_operations(self) -> None:
        registry = build_tool_registry()
        capabilities = {
            item["name"]: item for item in registry.capabilities()
        }
        for name in (
            "local_media.scan_sources",
            "library.refresh_library",
            "media_proxy.restart_instance",
        ):
            with self.subTest(name=name):
                self.assertTrue(capabilities[name]["requires_confirmation"])
                self.assertEqual(capabilities[name]["risk"], "low_write")

        cases = {
            "扫描全部本地媒体来源": (
                "local_media_scan", "local_media.scan_sources"
            ),
            "通知 Jellyfin 扫描动漫库": (
                "media_library_refresh", "provider.change.execute"
            ),
            "重启 Jellyfin 反代实例 1": (
                "media_proxy_restart", "media_proxy.restart_instance"
            ),
            "暂停第 1 个下载任务": (
                "download_control", "provider.change.execute"
            ),
            "把 STRM 同步改为每 7 天": (
                "strm_schedule_policy", "strm.set_schedule_policy"
            ),
            "重试 STRM 失败项": (
                "strm_failure_workflow", "strm.retry_failures"
            ),
            "把媒体库巡检改为每 3 天": (
                "library_patrol_control", "library.set_patrol_policy"
            ),
            "立即巡检整个媒体库缺集": (
                "library_wide_episode_audit", "library.start_episode_audit"
            ),
            "启用本地媒体来源 2 的 qB 完成触发": (
                "local_media_source_control",
                "local_media.set_source_trigger_enabled",
            ),
            "停用媒体反代实例 2": (
                "media_proxy_control", "media_proxy.set_instance_enabled"
            ),
            "发送 Telegram 测试通知": (
                "telegram_test_notification", "telegram.send_test_notification"
            ),
            "将第 2 和第 4 条 RSS 提交到 qB": (
                "rss_workflow", "rss.submit_entries_to_qb"
            ),
            "删除 RSS 订阅源 3": (
                "rss_workflow", "rss.delete_subscription"
            ),
            "清理光鸭整理来源里的严格垃圾图片": (
                "guangya_cleanup_workflow",
                "guangya.organize.cleanup.preview",
            ),
            "把光鸭某个目录名称里的垃圾前缀去掉": (
                "guangya_rename_workflow", "guangya.rename.preview"
            ),
        }
        for message, (task_kind, tool_name) in cases.items():
            with self.subTest(message=message):
                objective = infer_agent_objective(message)
                self.assertEqual(objective.task_kind, task_kind)
                self.assertIn(tool_name, objective.allowed_tools)
                self.assertTrue(is_agent_action_request(message))
                names = {
                    registry.native_tool_name(item["name"])
                    for item in _native_read_capabilities(
                        registry, message, include_confirmations=True
                    )
                }
                self.assertIn(tool_name, names)

        for message in ("关闭 Agent", "我想关闭 Agent", "我要开启智能助手"):
            with self.subTest(message=message):
                control = infer_agent_objective(message)
                self.assertEqual(control.task_kind, "agent_control_guidance")
                self.assertEqual(control.allowed_tools, ())

        read_cases = {
            "检查系统运行状态和最近失败任务": (
                "system_status", "workspace.health"
            ),
            "看看索引站是否正常": (
                "indexer_status", "indexer.diagnose_readiness"
            ),
            "测试所有媒体反代实例": (
                "media_proxy_diagnosis", "media_proxy.test_instance"
            ),
            "检查 Jellyfin 反代是不是正常": (
                "media_proxy_diagnosis", "media_proxy.status_summary"
            ),
            "Telegram 测试通知是否开启": (
                "telegram_status", "agent.runtime_status"
            ),
        }
        for message, (task_kind, tool_name) in read_cases.items():
            with self.subTest(message=message):
                objective = infer_agent_objective(message)
                self.assertEqual(objective.task_kind, task_kind)
                self.assertIn(tool_name, objective.allowed_tools)
                self.assertFalse(is_agent_action_request(message))

        combined = infer_agent_objective(
            "检查某剧第二季现在多少集了，有更新吗，推送更新到光鸭"
        )
        self.assertEqual(combined.task_kind, "series_missing_download_plan")
        self.assertEqual(combined.entity_terms, ("某剧",))
        self.assertIn("library.check_updates", combined.allowed_tools)
        self.assertIn("indexer.search_resources", combined.allowed_tools)

    def test_local_media_scan_is_limited_to_confirmed_configured_sources(self) -> None:
        self.assertEqual(
            local_media_scan_arguments({}), {"source_numbers": [], "query": ""}
        )
        self.assertEqual(
            local_media_scan_arguments({"source_numbers": [2, 1, 2]}),
            {"source_numbers": [2, 1], "query": ""},
        )
        with self.assertRaises(AgentToolError):
            local_media_scan_arguments({"source_numbers": [True]})

        with tempfile.TemporaryDirectory() as root_raw, tempfile.TemporaryDirectory() as target_raw:
            root = Path(root_raw)
            source_id = db.create_local_media_source(
                name="下载来源",
                qb_profile="",
                qb_path_prefix="",
                local_root=str(root),
                stable_seconds=0,
                owner="admin",
            )
            db.upsert_local_library_target(
                source_id, "default", target_raw, owner="admin"
            )
            preview, fingerprint = prepare_scan_local_media_sources(
                {"source_numbers": [1]}
            )
            self.assertEqual(preview.status, "confirmation_required")
            self.assertEqual(preview.data["eligible"], 1)

            scheduler = Mock()
            scheduler.status.return_value = {"running": False}
            scheduler.enqueue_manual_scan_candidates.return_value = {
                "scanned_sources": 1,
                "candidate_count": 2,
                "queued_count": 2,
            }
            with patch(
                "app.agent.local_media_scan_actions.get_local_media_scheduler",
                return_value=scheduler,
            ):
                result = scan_local_media_sources_confirmed(
                    {"source_numbers": [1]}, fingerprint
                )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["queued_tasks"], 2)
            scheduler.enqueue_manual_scan_candidates.assert_called_once_with(
                silent=True, source_ids={source_id}, candidate_query=""
            )
            scheduler.start.assert_called_once_with()

    def test_scheduler_filters_manual_scan_by_media_name(self) -> None:
        with tempfile.TemporaryDirectory() as root_raw, tempfile.TemporaryDirectory() as target_raw:
            root = Path(root_raw)
            (root / "Alpha.Show.S01E01.mkv").write_bytes(b"alpha")
            (root / "Beta.Show.S01E01.mkv").write_bytes(b"beta")
            source_id = db.create_local_media_source(
                name="下载来源",
                qb_profile="",
                qb_path_prefix="",
                local_root=str(root),
                stable_seconds=0,
                owner="admin",
            )
            db.upsert_local_library_target(
                source_id, "default", target_raw, owner="admin"
            )
            result = LocalMediaScheduler(service=Mock()).enqueue_manual_scan_candidates(
                silent=True,
                source_ids={source_id},
                candidate_query="Alpha Show",
            )
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["queued_count"], 1)
        task = db.get_local_media_task(result["task_ids"][0], owner="admin")
        self.assertIn("Alpha.Show", task.content_path)

    def test_media_library_refresh_requires_unique_live_match(self) -> None:
        self.assertEqual(
            media_library_refresh_arguments(
                {"provider": "JELLYFIN", "library_name": "动漫"}
            ),
            {"provider": "jellyfin", "library_name": "动漫"},
        )
        profile = MediaServerProfile(
            source="configured:jellyfin",
            server_type="jellyfin",
            label="Jellyfin",
            url="http://jellyfin.invalid:8096",
            credential="secret",
            enabled=True,
        )
        client = Mock()
        client.list_virtual_folders.return_value = [
            {"id": "private-library-id", "name": "动漫"}
        ]
        client.refresh_library.return_value = True
        with patch(
            "app.agent.media_library_actions.list_configured_profiles",
            return_value=[profile],
        ), patch(
            "app.agent.media_library_actions._client_for",
            return_value=client,
        ), patch(
            "app.agent.media_library_actions.get_web_secret",
            return_value="x" * 32,
        ):
            preview, fingerprint = prepare_refresh_media_library(
                {"provider": "jellyfin", "library_name": "动漫"}
            )
            result = refresh_media_library_confirmed(
                {"provider": "jellyfin", "library_name": "动漫"},
                fingerprint,
            )
        self.assertEqual(preview.data["library"], "动漫")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["refreshed"])
        client.refresh_library.assert_called_once_with("private-library-id")

    def test_media_library_auto_falls_back_to_reachable_server(self) -> None:
        jellyfin = MediaServerProfile(
            source="configured:jellyfin",
            server_type="jellyfin",
            label="Jellyfin",
            url="http://jellyfin.invalid:8096",
            credential="secret",
            enabled=True,
        )
        emby = MediaServerProfile(
            source="configured:emby",
            server_type="emby",
            label="Emby",
            url="http://emby.invalid:8096",
            credential="secret",
            enabled=True,
        )
        unavailable = Mock()
        unavailable.list_virtual_folders.side_effect = ConnectionError
        reachable = Mock()
        reachable.list_virtual_folders.return_value = [
            {"id": "emby-library-id", "name": "动漫"}
        ]
        reachable.refresh_library.return_value = True

        def client_for(profile: MediaServerProfile):
            return unavailable if profile.server_type == "jellyfin" else reachable

        with patch(
            "app.agent.media_library_actions.list_configured_profiles",
            return_value=[jellyfin, emby],
        ), patch(
            "app.agent.media_library_actions._client_for",
            side_effect=client_for,
        ), patch(
            "app.agent.media_library_actions.get_web_secret",
            return_value="x" * 32,
        ):
            preview, fingerprint = prepare_refresh_media_library(
                {"provider": "auto", "library_name": "动漫"}
            )
            result = refresh_media_library_confirmed(
                {"provider": "auto", "library_name": "动漫"},
                fingerprint,
            )

        self.assertEqual(preview.data["provider"], "emby")
        self.assertTrue(result.ok)
        reachable.refresh_library.assert_called_once_with("emby-library-id")

    def test_media_proxy_restart_queues_runtime_rebuild_without_config_write(self) -> None:
        self.assertEqual(
            media_proxy_restart_arguments({"instance_number": 1}),
            {"instance_number": 1},
        )
        instance_id = db.add_media_proxy_instance(
            name="Jellyfin 反代",
            server_type="jellyfin",
            upstream_url="http://jellyfin.invalid:8096",
            api_key="secret",
            listen_host="127.0.0.1",
            listen_port=19096,
            local_root="/media",
            enabled=1,
        )
        preview, fingerprint = prepare_restart_media_proxy_instance(
            {"instance_number": 1}
        )
        manager = Mock()
        manager.request_restart.return_value = True
        with patch(
            "app.agent.media_proxy_actions.clear_signed_url_cache",
            return_value=3,
        ), patch(
            "app.agent.media_proxy_actions.get_media_proxy_manager",
            return_value=manager,
        ):
            result = restart_media_proxy_instance_confirmed(
                {"instance_number": 1}, fingerprint
            )
        self.assertEqual(preview.status, "confirmation_required")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["cache_entries_cleared"], 3)
        manager.request_restart.assert_called_once_with(instance_id)


class MediaProxyRuntimeRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_instance_rebuilds_only_selected_runtime(self) -> None:
        from app.modules import media_proxy

        row = {
            "id": 7,
            "enabled": 1,
            "listen_host": "127.0.0.1",
            "listen_port": 18097,
            "upstream_url": "http://127.0.0.1:8096",
        }
        previous = MagicMock()
        replacement = MagicMock()
        manager = media_proxy.MediaProxyManager()
        manager._runtimes[7] = previous
        with patch.object(
            media_proxy.database, "get_media_proxy_instance", return_value=row
        ), patch.object(
            media_proxy.database, "update_media_proxy_instance"
        ), patch.object(
            media_proxy, "resolve_proxy_instance", return_value=row
        ), patch.object(
            manager, "_stop_runtime", new=AsyncMock()
        ) as stop, patch.object(
            manager, "_start_runtime", new=AsyncMock(return_value=replacement)
        ) as start:
            result = await manager.restart_instance(7)

        self.assertEqual(result, {"restarted": True, "reason": ""})
        stop.assert_awaited_once_with(previous)
        start.assert_awaited_once_with(row)
        self.assertIs(manager._runtimes[7], replacement)
