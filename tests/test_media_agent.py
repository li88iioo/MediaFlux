"""Media Agent 只读工具、确定性路由与 API 安全测试。"""
from __future__ import annotations

import re
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app.agent.models import RiskLevel, ToolContext, ToolResult, ToolSpec
from app.agent.orchestrator import (
    AgentInputError,
    AgentOrchestrator,
    is_presentation_feedback_message,
    is_library_series_episode_count_and_audit_message,
    is_library_series_episode_count_message,
    normalize_agent_message,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.tools import (
    build_tool_registry, diagnose_config, diagnose_strm,
    guangya_organize_status, reset_agent_tool_caches_for_tests, search_library,
    strm_runtime_status,
)
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _identity(arguments):
    return dict(arguments)


class AgentCoreTests(unittest.TestCase):
    def test_message_validation_rejects_empty_long_and_control_characters(self):
        for value in ("", "x" * 1001, "hello\nworld", 123, True, {}, []):
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(AgentInputError):
                    normalize_agent_message(value)

    def test_registry_rejects_unknown_tools_and_non_read_tools(self):
        registry = ToolRegistry()
        with self.assertRaises(AgentToolError) as unknown:
            registry.execute("missing", {})
        self.assertEqual(unknown.exception.code, "tool_not_found")

        registry.register(ToolSpec(
            name="write.test",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={},
            handler=lambda _arguments: ToolResult(True, "success", "done"),
            validator=_identity,
            requires_confirmation=True,
        ))
        with self.assertRaises(AgentToolError) as blocked:
            registry.execute("write.test", {})
        self.assertEqual(blocked.exception.code, "confirmation_required")

    def test_orchestrator_routes_supported_intents_and_episode_audit(self):
        calls = []
        registry = ToolRegistry()
        for name in (
            "agent.capabilities", "config.diagnose", "strm.diagnose", "strm.status",
            "guangya.organize.status", "library.search", "library.audit_episodes",
            "library.count_series_episodes",
        ):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: (
                    calls.append((tool, arguments))
                    or ToolResult(True, "success", tool)
                ),
                validator=_identity,
            ))
        service = AgentOrchestrator(registry)

        self.assertEqual(service.query("检查项目配置")["tool_call"]["name"], "config.diagnose")
        self.assertEqual(service.query("检查 STRM 是否健康")["tool_call"]["name"], "strm.diagnose")
        self.assertEqual(service.query("STRM 同步进度")["tool_call"]["name"], "strm.status")
        self.assertEqual(service.query("光鸭整理任务现在进度怎么样")["tool_call"]["name"], "guangya.organize.status")
        search = service.query("帮我找《沙丘2》")
        self.assertEqual(search["tool_call"]["name"], "library.search")
        self.assertEqual(search["tool_call"]["arguments"]["query"], "沙丘2")

        local_count = service.query("查看媒体库中 师兄啊师兄 一共有多少集")
        self.assertEqual(local_count["tool_call"]["name"], "library.count_series_episodes")
        self.assertEqual(local_count["tool_call"]["arguments"], {"query": "师兄啊师兄"})
        quoted_count = service.query("Jellyfin 里《师兄啊师兄》有几集")
        self.assertEqual(quoted_count["tool_call"]["name"], "library.count_series_episodes")
        self.assertEqual(quoted_count["tool_call"]["arguments"], {"query": "师兄啊师兄"})
        custom_library_count = service.query("查看我的美女库中 师兄啊师兄 一共有多少集")
        self.assertEqual(
            custom_library_count["tool_call"]["name"],
            "library.count_series_episodes",
        )
        self.assertEqual(
            custom_library_count["tool_call"]["arguments"],
            {"query": "师兄啊师兄", "library_name": "美女库"},
        )
        scoped_unquoted = service.query("在儿童媒体库里 The Show 有多少集")
        self.assertEqual(
            scoped_unquoted["tool_call"]["arguments"],
            {"query": "The Show", "library_name": "儿童媒体库"},
        )
        combined = service.query("我的美女库中《师兄啊师兄》一共有多少集，有没有缺集")
        self.assertEqual(combined["tool_call"]["name"], "agent.read_plan")
        self.assertEqual(
            [step["tool_name"] for step in combined["result"]["data"]["steps"]],
            ["library.count_series_episodes", "library.audit_episodes"],
        )
        self.assertEqual(
            [step["result"]["summary"] for step in combined["result"]["data"]["steps"]],
            ["library.count_series_episodes", "library.audit_episodes"],
        )

        calls_before_as_of = len(calls)
        combined_as_of = service.query(
            "我的美女库中《师兄啊师兄》截至 2026-01-01 一共有多少集，有没有缺集"
        )
        self.assertEqual(combined_as_of["tool_call"]["name"], "agent.read_plan")
        self.assertEqual(
            calls[calls_before_as_of:],
            [
                (
                    "library.count_series_episodes",
                    {"query": "师兄啊师兄", "library_name": "美女库"},
                ),
                (
                    "library.audit_episodes",
                    {
                        "query": "师兄啊师兄",
                        "as_of": "2026-01-01",
                        "library_name": "美女库",
                    },
                ),
            ],
        )

        self.assertFalse(
            is_library_series_episode_count_message(
                "数据库中《师兄啊师兄》一共有多少集"
            )
        )
        self.assertFalse(
            is_library_series_episode_count_and_audit_message(
                "知识库中《师兄啊师兄》一共有多少集，有没有缺集"
            )
        )

        audit = service.query("检查《某剧》第 2 季有没有缺集，TMDB 12345")
        self.assertEqual(audit["tool_call"]["name"], "library.audit_episodes")
        self.assertEqual(audit["tool_call"]["arguments"], {
            "query": "某剧", "tmdb_id": "12345", "season": 2,
        })
        season_audit = service.query("核对《另一剧》第 3 季")
        self.assertEqual(season_audit["tool_call"]["name"], "library.audit_episodes")
        self.assertEqual(season_audit["tool_call"]["arguments"], {
            "query": "另一剧", "season": 3,
        })
        unsupported = service.query("检查有没有缺集")
        self.assertIsNone(unsupported["tool_call"])
        self.assertEqual(unsupported["result"]["status"], "unsupported")
        self.assertNotIn(("library.search", {"query": "某剧", "limit": 8}), calls)

        self.assertEqual(service.query("能力")["tool_call"]["name"], "agent.capabilities")
        self.assertEqual(service.query("请诊断")["tool_call"]["name"], "config.diagnose")
        self.assertEqual(service.query("检查索引")["tool_call"]["name"], "strm.diagnose")
        self.assertEqual(service.query("片名：沙丘")["tool_call"]["arguments"]["query"], "沙丘")
        self.assertEqual(service.query("剧名：黑镜")["tool_call"]["arguments"]["query"], "黑镜")


    def test_greeting_uses_local_conversation_without_exposing_tools(self):
        service = AgentOrchestrator(ToolRegistry())
        for message in (
            "你好", "在干吗呢", "在干嘛呢", "在干啥呢", "干吗", "干嘛", "干什么", "干啥",
        ):
            with self.subTest(message=message):
                response = service.query(message)
                self.assertEqual(response["mode"], "conversation")
                self.assertIsNone(response["tool_call"])
                self.assertIn("我在", response["result"]["summary"])
                self.assertNotIn(".", response["result"]["summary"])
        for message in ("干嘛检查下载队列", "干什么刷新 RSS 订阅", "干啥找《沙丘2》的资源"):
            with self.subTest(message=message):
                self.assertIsNone(service._local_conversation(message))

    def test_bare_ambiguous_followup_asks_for_plain_language_scope(self):
        response = AgentOrchestrator(ToolRegistry()).query("啥？")

        self.assertEqual(response["mode"], "clarification")
        self.assertIsNone(response["tool_call"])
        self.assertIn("想关注哪一部分", response["result"]["summary"])
        self.assertNotRegex(response["result"]["summary"], r"[a-z_]+\.[a-z_]+")

    def test_presentation_feedback_is_answered_directly_instead_of_being_misrouted(self):
        message = "为什么输出的格式这么紧凑没有排版"

        self.assertTrue(is_presentation_feedback_message(message))
        response = AgentOrchestrator(ToolRegistry()).query(message)

        self.assertEqual(response["mode"], "conversation")
        self.assertIsNone(response["tool_call"])
        self.assertIn("确实是排版问题", response["result"]["summary"])
        self.assertIn("\n\n", response["result"]["summary"])
        self.assertNotIn("检查下载队列里的异常", response["result"]["suggestions"])

        for normal_request in (
            "显示订阅列表",
            "查看内容列表",
            "为什么这个资源没集数",
            "下载页面显示内容太挤了，怎么调整",
            "媒体库的内容没有换行，怎么看",
        ):
            with self.subTest(normal_request=normal_request):
                self.assertFalse(is_presentation_feedback_message(normal_request))

        for feedback in (
            "Telegram 回复挤在一起",
            "Agent 输出没有换行",
        ):
            with self.subTest(feedback=feedback):
                self.assertTrue(is_presentation_feedback_message(feedback))


class AgentToolTests(unittest.TestCase):
    @staticmethod
    def _config_get(values):
        return lambda key, default="": values.get(key, default)

    def test_config_diagnosis_reports_completeness_without_values(self):
        secret = "do-not-leak-agent-secret"
        values = {
            "JELLYFIN_ENABLED": "1",
            "JELLYFIN_URL": "http://private-server:8096",
            "JELLYFIN_API_KEY": secret,
            "TMDB_API_KEY": "",
            "QB_URL": "http://qb:8080",
            "QB_USERNAME": "",
            "QB_PASSWORD": "",
            "QB_API_KEY": "",
            "STRM_SCHEDULE_ENABLED": "1",
            "GY_STRM_SOURCE_DIRS": "",
            "GY_STRM_BASE_URL": "",
            "STRM_ROOT": "/private/strm",
            "AI_RECOGNITION_ENABLED": "0",
            "EMBY_ENABLED": "0",
        }
        with patch("app.agent.tools.config.all_items", return_value=dict(values)), patch(
            "app.agent.tools.config.get", side_effect=self._config_get(values)
        ):
            result = diagnose_config({})

        payload = str(result.to_dict())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertNotIn(secret, payload)
        self.assertNotIn("private-server", payload)
        self.assertNotIn("/private/strm", payload)
        codes = {item["code"] for item in result.data["issues"]}
        self.assertIn("tmdb_not_configured", codes)
        self.assertIn("qbittorrent_incomplete", codes)
        self.assertIn("strm_schedule_incomplete", codes)

    def test_strm_diagnosis_returns_counts_without_raw_result_or_error(self):
        secret = "signed-private-path"
        rows = [{
            "status": "failed",
            "trigger_type": "manual",
            "started_at": "2026-08-01 10:00:00",
            "finished_at": "2026-08-01 10:00:01",
            "result": secret,
            "error": secret,
        }]
        with patch("app.agent.tools.config.get", return_value="/private/strm"), patch(
            "app.agent.tools.db.list_strm_index_diagnostics",
            return_value={"total": 4, "existing": 3, "missing": 1, "real_source": 4, "confirmed_test_artifact": 0},
        ), patch(
            "app.agent.tools.db.summarize_strm_failures",
            return_value={
                "open": 2,
                "resolved": 1,
                "sources": [{"id": f"secret-{secret}", "name": f"/private/{secret}", "open": 2}],
            },
        ), patch("app.agent.tools.db.list_task_runs", return_value=rows):
            result = diagnose_strm({})

        self.assertFalse(result.ok)
        self.assertEqual(result.data["index"]["missing"], 1)
        self.assertEqual(result.data["failures"]["open"], 2)
        self.assertNotIn(secret, str(result.to_dict()))
        self.assertEqual(result.data["failures"]["by_source"], [{"label": "来源 1", "open": 2}])

    def test_strm_runtime_status_returns_sanitized_progress(self):
        secret = "private-source-and-error"
        scheduler = Mock()
        scheduler.status.return_value = {
            "enabled": True,
            "cron": "0 4 * * *",
            "sources": [{"id": secret, "name": f"/{secret}"}],
            "cron_valid": True,
            "config_error": "",
            "running": True,
            "current_trigger": "manual",
            "progress": {"stage": "generate", "completed": 3, "total": 10, "percent": 30, "detail": secret},
            "source_runtime": [
                {"id": secret, "name": secret, "status": "completed", "completed": 3, "total": 3},
                {"id": secret, "name": secret, "status": "running", "completed": 0, "total": 7},
            ],
            "next_run": "2026-08-02 04:00:00",
            "last_run": {
                "id": secret,
                "status": "success",
                "trigger_type": "cron",
                "started_at": "2026-08-01 04:00:00",
                "finished_at": "2026-08-01 04:01:00",
                "result": secret,
                "error": secret,
            },
        }
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
            result = strm_runtime_status({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "running")
        self.assertEqual(result.data["progress"], {
            "stage": "generate", "completed": 3, "total": 10, "percent": 30,
        })
        self.assertEqual(result.data["sources"], {
            "total": 2, "by_status": {"completed": 1, "running": 1},
        })
        self.assertNotIn(secret, str(result.to_dict()))

    def test_guangya_organize_status_returns_sanitized_task_summary(self):
        secret = "private-task-source-error"
        manager = Mock()
        manager.status.return_value = {
            "id": secret,
            "status": "running",
            "message": secret,
            "stoppable": True,
            "sources": [{"id": secret, "name": secret}],
            "current_source": secret,
            "target_dir_id": secret,
            "started_at": "2026-08-01 12:00:00",
            "finished_at": "",
            "trigger_type": "cron",
            "stats": {"total": 12, "moved": 7, "failed": 1, "unsafe_detail": secret},
            "error": secret,
            "source_results": [{"name": secret}],
            "schedule": {
                "enabled": True,
                "cron": "0 5 * * *",
                "cron_valid": True,
                "config_error": "",
                "next_run": "2026-08-02 05:00:00",
                "last_result": {"error": secret, "sources": [secret]},
            },
        }
        with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager):
            result = guangya_organize_status({})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "running")
        self.assertEqual(result.data["task"]["stats"], {"total": 12, "moved": 7, "failed": 1})
        self.assertEqual(result.data["schedule"], {
            "enabled": True,
            "configured": True,
            "cron_valid": True,
            "next_run": "2026-08-02 05:00:00",
        })
        self.assertNotIn(secret, str(result.to_dict()))

    def test_guangya_organize_status_queries_durable_operation_by_public_reference(self):
        operation_ref = "GY-ABCD-EF01-2345-6789-ABCD-EF01-2345-6789"
        manager = Mock()
        manager.status.return_value = {
            "status": "idle",
            "operation_queue": {"total": 0, "items": []},
            "schedule": {},
        }
        manager.task_result.return_value = {
            "status": "manual_review",
            "started_at": "",
            "finished_at": "",
            "result": {},
        }
        with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager):
            result = guangya_organize_status(
                {"operation_ref": operation_ref}, ToolContext(owner="owner-status")
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["task"]["operation_ref"], operation_ref)
        manager.task_result.assert_called_once_with(operation_ref, owner="owner-status")

    def test_guangya_organize_status_preserves_partial_state_and_safe_cleanup_counts(self):
        secret = "private-cleanup-detail"
        manager = Mock()
        manager.status.return_value = {
            "status": "partial",
            "message": secret,
            "stats": {
                "total": 4,
                "moved": 3,
                "source_dir_cleanup_failed": 1,
                "empty_dir_cleanup_failed": 2,
                "audit_failures": 1,
                "unsafe_detail": secret,
            },
            "schedule": {},
        }
        with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager):
            result = guangya_organize_status({})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["task"]["status"], "partial")
        self.assertEqual(result.data["task"]["stats"], {
            "total": 4,
            "moved": 3,
            "source_dir_cleanup_failed": 1,
            "empty_dir_cleanup_failed": 2,
            "audit_failures": 1,
        })
        self.assertNotIn(secret, str(result.to_dict()))

    def test_library_search_serializes_safe_media_summary(self):
        item = Mock()
        item.name = "Episode One"
        item.display_name = "Show"
        item.type = "Episode"
        item.year = "2026"
        item.series_name = "Show"
        item.season_number = 1
        item.episode_number = 1
        item.overview = "Overview"
        item.runtime = 48
        item.progress = 42.25
        item.web_url = "http://server/private/item"
        item.id = "secret-item-id"
        sources = [{
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "web_url": "http://server:8096",
            "items": [item],
            "error": "",
        }]
        reset_agent_tool_caches_for_tests()
        with patch("app.agent.tools.search_media_servers", return_value=sources):
            result = search_library({"query": "Show", "limit": 8})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["total"], 1)
        payload = str(result.to_dict())
        self.assertNotIn("server:8096", payload)
        self.assertNotIn("secret-item-id", payload)
        self.assertEqual(result.data["match_status"], "found")
        self.assertEqual(result.data["sources"][0]["match_status"], "found")
        projected = result.data["sources"][0]["items"][0]
        self.assertEqual(projected["episode"], 1)
        self.assertEqual(projected["runtime_minutes"], 48)
        self.assertEqual(projected["playback_progress_percent"], 42.25)

    def test_library_search_distinguishes_unavailable_sources_from_empty_results(self):
        reset_agent_tool_caches_for_tests()
        sources = [{
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "items": [],
            "error": "private upstream detail",
        }]
        with patch("app.agent.tools.search_media_servers", return_value=sources):
            result = search_library({"query": "Show", "limit": 8})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.data["match_status"], "indeterminate")
        self.assertEqual(result.data["sources"][0]["match_status"], "unknown")
        self.assertIn("暂时不可用", result.summary)
        self.assertNotIn("没有找到", result.summary)
        self.assertNotIn("private upstream detail", str(result.to_dict()))

    def test_library_search_marks_all_available_empty_sources_as_not_found(self):
        reset_agent_tool_caches_for_tests()
        sources = [
            {"server_type": "jellyfin", "server_name": "主库", "items": [], "error": ""},
            {"server_type": "emby", "server_name": "备用库", "items": [], "error": ""},
        ]
        with patch("app.agent.tools.search_media_servers", return_value=sources):
            result = search_library({"query": "Show", "limit": 8})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.data["match_status"], "not_found")
        self.assertTrue(all(
            source["status"] == "ready" and source["match_status"] == "not_found"
            for source in result.data["sources"]
        ))

    def test_library_search_marks_partial_results_as_found(self):
        item = Mock()
        item.name = "Show"
        item.display_name = "Show"
        item.type = "Series"
        item.year = "2026"
        item.series_name = ""
        item.season_number = None
        item.episode_number = None
        item.runtime = 45
        item.progress = 5
        item.overview = ""
        reset_agent_tool_caches_for_tests()
        sources = [
            {"server_type": "jellyfin", "server_name": "主库", "items": [item], "error": ""},
            {"server_type": "emby", "server_name": "备用库", "items": [], "error": "timeout"},
        ]
        with patch("app.agent.tools.search_media_servers", return_value=sources):
            result = search_library({"query": "Show", "limit": 8})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["match_status"], "found")
        self.assertEqual(result.data["total"], 1)
        self.assertEqual(result.data["sources"][0]["match_status"], "found")
        self.assertEqual(result.data["sources"][1]["match_status"], "unknown")

    def test_library_search_marks_partial_zero_results_as_indeterminate(self):
        reset_agent_tool_caches_for_tests()
        sources = [
            {"server_type": "jellyfin", "server_name": "主库", "items": [], "error": ""},
            {"server_type": "emby", "server_name": "备用库", "items": [], "error": "timeout"},
        ]
        with patch("app.agent.tools.search_media_servers", return_value=sources):
            result = search_library({"query": "Show", "limit": 8})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["match_status"], "indeterminate")
        self.assertIn("可用来源未找到", result.summary)

    def test_library_search_redacts_sensitive_upstream_text_and_normalizes_numbers(self):
        item = Mock()
        item.name = "https://private.example/item"
        item.display_name = "Safe title"
        item.type = "UnexpectedType"
        item.year = "9999"
        item.series_name = "token=private-secret"
        item.season_number = True
        item.episode_number = -1
        item.runtime = 999999999
        item.progress = float("inf")
        item.overview = "/srv/media/private.mkv"
        item.id = "secret-id"
        item.web_url = "http://server/private/item"
        sources = [{
            "server_type": "custom",
            "server_name": "node.private.co.uk/library",
            "items": [item],
            "error": "",
        }]
        reset_agent_tool_caches_for_tests()
        with patch("app.agent.tools.search_media_servers", return_value=sources):
            result = search_library({"query": "Safe title", "limit": 8})

        projected = result.data["sources"][0]["items"][0]
        self.assertEqual(projected["title"], "媒体条目")
        self.assertEqual(projected["display_name"], "Safe title")
        self.assertEqual(projected["media_type"], "unknown")
        self.assertEqual(projected["year"], "")
        self.assertEqual(projected["series_name"], "")
        self.assertIsNone(projected["season"])
        self.assertIsNone(projected["episode"])
        self.assertEqual(projected["runtime_minutes"], 0)
        self.assertEqual(projected["playback_progress_percent"], 0.0)
        self.assertEqual(projected["overview"], "")
        payload = str(result.to_dict())
        self.assertNotIn("private.example", payload)
        self.assertNotIn("node.private.co.uk", payload)
        self.assertNotIn("private-secret", payload)
        self.assertNotIn("/srv/media", payload)
        self.assertNotIn("secret-id", payload)

    def test_library_search_cache_deduplicates_same_short_lived_query(self):
        reset_agent_tool_caches_for_tests()
        with patch("app.agent.tools.search_media_servers", return_value=[]) as search:
            search_library({"query": "Show", "limit": 8})
            search_library({"query": "Show", "limit": 8})
        search.assert_called_once_with("Show", limit=8)

    def test_library_search_cache_has_a_hard_entry_limit(self):
        reset_agent_tool_caches_for_tests()
        with patch("app.agent.tools._SEARCH_CACHE_MAX_ENTRIES", 2), patch(
            "app.agent.tools.search_media_servers", return_value=[]
        ) as search:
            search_library({"query": "Show One", "limit": 8})
            search_library({"query": "Show Two", "limit": 8})
            search_library({"query": "Show Three", "limit": 8})
            search_library({"query": "Show One", "limit": 8})
        self.assertEqual(search.call_count, 4)

    def test_registry_argument_validation_is_strict(self):
        registry = build_tool_registry()
        with self.assertRaises(AgentToolError):
            registry.execute("config.diagnose", {"unexpected": True})
        with self.assertRaises(AgentToolError):
            registry.execute("strm.status", {"unexpected": True})
        with self.assertRaises(AgentToolError):
            registry.execute("guangya.organize.status", {"unexpected": True})
        with self.assertRaises(AgentToolError):
            registry.execute("library.search", {"query": "Show", "limit": True})
        with self.assertRaises(AgentToolError):
            registry.execute("library.search", {"query": "", "limit": 8})
        for sensitive_query in (
            "https://private.example/item",
            "node.private.co.uk/library",
            "vault.company.info/token",
            "host.unknown-tld/library",
            "/srv/media/private.mkv",
            "token=private-secret",
            "a" * 32,
        ):
            with self.subTest(sensitive_query=sensitive_query), patch(
                "app.agent.tools.search_media_servers"
            ) as search, self.assertRaises(AgentToolError):
                registry.execute("library.search", {"query": sensitive_query, "limit": 8})
            search.assert_not_called()
        for title in (
            "D.P.",
            "M.A.S.H.",
            "U.S. Marshals",
            "S.W.A.T.",
            "The O.C.",
            "A.I.",
            "Mr.Robot",
            "Dune.Part.Two.2024",
        ):
            with self.subTest(title=title), patch(
                "app.agent.tools.search_media_servers", return_value=[]
            ) as search:
                result, _elapsed = registry.execute("library.search", {"query": title, "limit": 8})
            self.assertEqual(result.status, "not_configured")
            search.assert_called_once_with(title, limit=8)
        for query in (123, True, [], {}):
            with self.subTest(query=query):
                with self.assertRaises(AgentToolError):
                    registry.execute("library.search", {"query": query, "limit": 8})
        for arguments in (
            {"query": "Show", "tmdb_id": "12x"},
            {"query": "Show", "season": True},
            {"query": "Show", "season": 0},
            {"query": "Show", "target_episode": 3},
            {"query": "Show", "season": 1, "target_episode": True},
            {"query": "Show", "season": 1, "target_episode": 0},
            {"query": "Show", "season": 1, "target_episode": 1001},
            {"query": "Show", "as_of": "2026/08/01"},
            {"query": "Show", "unexpected": True},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(AgentToolError):
                    registry.execute("library.audit_episodes", arguments)


class _FakeAgentService:
    def capabilities(self):
        return {"tools": [{"name": "config.diagnose"}], "mode": "read_only"}

    def query(
        self, message, *, owner="", llm_rate_owner="", query_tool_rate_identity="",
        llm_tool_rate_identity="", **_kwargs
    ):
        if message == "结构化结果":
            return {
                "mode": "read_only",
                "request_id": "request-1",
                "tool_call": {"name": "workspace.todo", "elapsed_ms": 7},
                "result": {
                    "ok": True,
                    "status": "attention",
                    "summary": "工作区有待处理事项",
                    "data": {"attention_total": 2, "areas": []},
                    "evidence": [{"source": "workspace_database", "description": "安全聚合"}],
                    "suggestions": ["检查下载队列状态"],
                    "error": "",
                },
            }
        return {"message": message, "owner": bool(owner), "mode": "read_only"}

    def has_tool(self, tool_name):
        return tool_name != "missing" and not str(tool_name).startswith("missing-")

    def invoke(self, tool_name, arguments, *, owner="", **_kwargs):
        if not self.has_tool(tool_name):
            raise AgentToolError("未知 Agent 工具", code="tool_not_found")
        return {
            "tool": tool_name,
            "arguments": arguments,
            "owner": bool(owner),
            "mode": "read_only",
        }

    def prepare(self, tool_name, arguments, *, owner, **_kwargs):
        if tool_name == "missing":
            raise AgentToolError("未知 Agent 工具", code="tool_not_found")
        return {
            "mode": "confirmation_required",
            "tool": tool_name,
            "arguments": arguments,
            "owner": bool(owner),
            "confirmation": {"confirmation_id": "x" * 24},
        }

    def confirm(self, confirmation_id, *, owner, **_kwargs):
        if confirmation_id == "z" * 24:
            raise AgentToolError("确认请求无效或已过期", code="confirmation_invalid")
        return {
            "mode": "confirmed_action",
            "owner": bool(owner),
            "result": {"ok": True, "status": "accepted"},
        }


class AgentAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()
        self.service_patch = patch("app.routes.agent_api.get_agent_service", return_value=_FakeAgentService())
        self.service_patch.start()

    def tearDown(self):
        self.service_patch.stop()
        self.client.__exit__(None, None, None)
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_agent_api_requires_login_and_csrf(self):
        response = self.client.get("/api/agent/capabilities")
        self.assertEqual(response.status_code, 401)
        self.login()
        response = self.client.post("/api/agent/query", json={"session_id": "test_session_identifier_0001", "message": "检查配置"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "CSRF token invalid")

    def test_agent_query_and_explicit_tool_api(self):
        csrf = self.login()
        response = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "message": "检查配置"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["message"], "检查配置")

        response = self.client.post(
            "/api/agent/tools/config.diagnose",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "arguments": {}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tool"], "config.diagnose")

    def test_agent_query_preserves_complete_structured_result_payload(self):
        csrf = self.login()
        response = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "message": "结构化结果"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["mode"], "read_only")
        self.assertEqual(payload["request_id"], "request-1")
        self.assertEqual(payload["tool_call"], {"name": "workspace.todo", "elapsed_ms": 7})
        self.assertTrue(payload["result"]["ok"])
        self.assertEqual(payload["result"]["status"], "attention")
        self.assertEqual(payload["result"]["summary"], "工作区有待处理事项")
        self.assertEqual(payload["result"]["data"], {"attention_total": 2, "areas": []})
        self.assertEqual(
            payload["result"]["evidence"][0],
            {"source": "workspace_database", "description": "安全聚合"},
        )
        self.assertEqual(payload["result"]["suggestions"], ["检查下载队列状态"])
        self.assertEqual(payload["result"]["error"], "")

    def test_agent_api_rejects_invalid_shapes_and_unknown_tool(self):
        csrf = self.login()
        invalid = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "message": "x", "extra": True},
        )
        self.assertEqual(invalid.status_code, 400)
        for message in (123, True, {}, [], None):
            with self.subTest(message=message):
                response = self.client.post(
                    "/api/agent/query",
                    headers={"X-CSRF-Token": csrf},
                    json={"session_id": "test_session_identifier_0001", "message": message},
                )
                self.assertEqual(response.status_code, 400, response.text)

        bad_arguments = self.client.post(
            "/api/agent/tools/config.diagnose",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "arguments": []},
        )
        self.assertEqual(bad_arguments.status_code, 400)

        tracked_before = agent_rate_limiter.tracked_keys()
        for index in range(24):
            unknown = self.client.post(
                f"/api/agent/tools/missing-{index}",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "arguments": {}},
            )
            self.assertEqual(unknown.status_code, 404)
            self.assertEqual(unknown.json()["error"], "未知 Agent 工具")
        self.assertEqual(agent_rate_limiter.tracked_keys(), tracked_before)

        for index in range(24):
            unknown = self.client.post(
                f"/api/agent/actions/missing-action-{index}/prepare",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "arguments": {}},
            )
            self.assertEqual(unknown.status_code, 404)
            self.assertEqual(unknown.json()["error"], "未知 Agent 工具")
        self.assertEqual(agent_rate_limiter.tracked_keys(), tracked_before)

    def test_agent_api_normalizes_and_rejects_invalid_messages_before_dispatch(self):
        csrf = self.login()
        normalized = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "message": "  Ａ  "},
        )
        self.assertEqual(normalized.status_code, 200, normalized.text)
        self.assertEqual(normalized.json()["message"], "A")

        for message in (" " * 4, "x" * 1001, "bad\x00message"):
            with self.subTest(message=message[:20]):
                response = self.client.post(
                    "/api/agent/query",
                    headers={"X-CSRF-Token": csrf},
                    json={"session_id": "test_session_identifier_0001", "message": message},
                )
                self.assertEqual(response.status_code, 400, response.text)

    def test_agent_api_rejects_oversized_declared_body(self):
        csrf = self.login()
        response = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf, "Content-Length": str(64 * 1024 + 1)},
            content=b'{"message":"x"}',
        )
        self.assertEqual(response.status_code, 413)


class AgentAPIRealServiceTests(IsolatedDatabaseTestCase):
    def setUp(self):
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_real_agent_service_runs_config_tool_without_exposing_values(self):
        csrf = self.login()
        secret = "real-agent-api-secret"
        values = {
            "JELLYFIN_ENABLED": "1",
            "JELLYFIN_URL": "http://private-jellyfin:8096",
            "JELLYFIN_API_KEY": secret,
            "TMDB_API_KEY": "",
        }
        with patch("app.routes.agent_api.is_agent_enabled", return_value=True), patch(
            "app.agent.tools.config.all_items", return_value=dict(values)
        ), patch(
            "app.agent.tools.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "message": "检查项目配置"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tool_call"]["name"], "config.diagnose")
        self.assertNotIn(secret, response.text)
        self.assertNotIn("private-jellyfin", response.text)


if __name__ == "__main__":
    unittest.main()
