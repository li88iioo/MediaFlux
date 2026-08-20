"""Media Agent 媒体更新核对的语义、路由与 API 安全测试。"""
from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.models import Evidence, RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import AgentOrchestrator, is_library_update_check_message
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.agent.update_actions import check_library_updates
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _identity(arguments):
    return dict(arguments)


def _audit_result(*, status: str = "updates_available") -> ToolResult:
    return ToolResult(
        ok=status in {"updates_available", "up_to_date"},
        status=status,
        summary="发现 1 集已播但本地尚未收录" if status == "updates_available" else "未找到",
        data={"query": "黑镜", "missing_count": 1},
        evidence=[Evidence("media_servers+tmdb", "安全审计", "2026-08-01T10:00:00+08:00")],
    )


class LibraryUpdateActionTests(unittest.TestCase):
    def test_movie_checks_exact_library_presence_and_offers_safe_resource_followup(self):
        arguments = {
            "query": "沙丘2",
            "media_type": "movie",
            "tmdb_id": "693134",
            "season": None,
            "as_of": "2026-08-01",
        }
        sources = [{
            "server_type": "jellyfin",
            "server_name": "客厅媒体库",
            "web_url": "http://private.invalid",
            "items": [
                SimpleNamespace(type="Movie", name="沙丘 2", display_name="沙丘 2", year="2024"),
                SimpleNamespace(type="Episode", name="沙丘2", display_name="沙丘2", year="2024"),
                SimpleNamespace(type="Movie", name="沙丘：第二部", display_name="沙丘：第二部", year="2024"),
            ],
            "error": "",
        }]
        with patch("app.agent.update_actions.audit_series_episodes") as audit, patch(
            "app.agent.update_actions.search_media_servers", return_value=sources
        ) as search:
            result = check_library_updates(arguments)
        audit.assert_not_called()
        search.assert_called_once_with("沙丘2", limit=50)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "comparison_unavailable")
        self.assertEqual(result.data["media_type"], "movie")
        self.assertEqual(result.data["local_match_status"], "found")
        self.assertEqual(result.data["exact_match_count"], 1)
        self.assertEqual(result.data["possible_match_count"], 1)
        self.assertNotIn("web_url", result.data["sources"][0])
        self.assertEqual(result.data["resource_followups"], [{
            "tool": "indexer.search_resources",
            "label": "搜索《沙丘2》资源候选",
            "arguments": {"title": "沙丘2", "media_type": "movie", "year": "2024"},
        }])
        self.assertFalse(result.data["comparison"]["available"])
        self.assertNotIn("693134", result.summary)

    def test_movie_presence_is_honest_for_possible_missing_and_unavailable_sources(self):
        possible = [{
            "server_type": "emby",
            "server_name": "Emby",
            "items": [SimpleNamespace(type="Movie", name="Dune Part Two", display_name="Dune Part Two", year="2024")],
            "error": "",
        }]
        with patch("app.agent.update_actions.search_media_servers", return_value=possible):
            result = check_library_updates({
                "query": "沙丘2", "media_type": "movie", "tmdb_id": "", "season": None,
                "as_of": "2026-08-01",
            })
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.data["local_match_status"], "possible")
        self.assertEqual(result.data["exact_match_count"], 0)

        with patch("app.agent.update_actions.search_media_servers", return_value=[{
            "server_type": "jellyfin", "server_name": "Jellyfin", "items": [], "error": "",
        }]):
            result = check_library_updates({
                "query": "沙丘2", "media_type": "movie", "tmdb_id": "", "season": None,
                "as_of": "2026-08-01",
            })
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.data["local_match_status"], "not_found")

        with patch("app.agent.update_actions.search_media_servers", side_effect=RuntimeError("secret")):
            result = check_library_updates({
                "query": "沙丘2", "media_type": "movie", "tmdb_id": "", "season": None,
                "as_of": "2026-08-01",
            })
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn("secret", result.summary)

    def test_movie_presence_caps_public_match_rows_across_all_servers(self):
        sources = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "items": [
                    SimpleNamespace(type="Movie", name=f"候选 {index}", display_name=f"候选 {index}", year="2024")
                    for index in range(8)
                ],
                "error": "",
            },
            {
                "server_type": "emby",
                "server_name": "Emby",
                "items": [SimpleNamespace(type="Movie", name="额外候选", display_name="额外候选", year="2025")],
                "error": "",
            },
        ]
        with patch("app.agent.update_actions.search_media_servers", return_value=sources):
            result = check_library_updates({
                "query": "不存在的精确片名", "media_type": "movie", "tmdb_id": "", "season": None,
                "as_of": "2026-08-01",
            })

        self.assertEqual(result.data["possible_match_count"], 9)
        self.assertEqual(result.data["matches_truncated"], 1)
        self.assertEqual(sum(source["returned"] for source in result.data["sources"]), 8)
        self.assertEqual(result.data["sources"][1]["returned"], 0)

    def test_movie_presence_does_not_claim_not_found_when_source_hits_search_cap(self):
        sources = [{
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "items": [
                SimpleNamespace(type="Episode", name=f"同名剧集 {index}", display_name=f"同名剧集 {index}", year="2024")
                for index in range(50)
            ],
            "error": "",
        }]
        with patch("app.agent.update_actions.search_media_servers", return_value=sources):
            result = check_library_updates({
                "query": "目标电影", "media_type": "movie", "tmdb_id": "", "season": None,
                "as_of": "2026-08-01",
            })

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.data["local_match_status"], "indeterminate")
        self.assertEqual(result.data["search_truncated_server_count"], 1)
        self.assertNotIn("未找到同名电影", result.summary)

    def test_movie_presence_scans_later_servers_before_capping_public_rows(self):
        sources = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "items": [
                    SimpleNamespace(type="Movie", name=f"候选 {index}", display_name=f"候选 {index}", year="2024")
                    for index in range(8)
                ],
                "error": "",
            },
            {
                "server_type": "emby",
                "server_name": "Emby",
                "items": [SimpleNamespace(type="Movie", name="沙丘 2", display_name="沙丘 2", year="2024")],
                "error": "",
            },
        ]
        with patch("app.agent.update_actions.search_media_servers", return_value=sources):
            result = check_library_updates({
                "query": "沙丘2", "media_type": "movie", "tmdb_id": "", "season": None,
                "as_of": "2026-08-01",
            })

        self.assertEqual(result.status, "comparison_unavailable")
        self.assertEqual(result.data["local_match_status"], "found")
        self.assertEqual(result.data["exact_match_count"], 1)
        self.assertEqual(result.data["possible_match_count"], 8)
        self.assertEqual(sum(source["returned"] for source in result.data["sources"]), 8)
        self.assertEqual(result.data["sources"][1]["items"][0]["match"], "exact_title")

    def test_tv_delegates_to_episode_audit_and_labels_definition(self):
        with patch("app.agent.update_actions.audit_series_episodes", return_value=_audit_result()) as audit:
            result = check_library_updates({
                "query": "黑镜",
                "media_type": "tv",
                "tmdb_id": "42009",
                "season": 7,
                "as_of": "2026-08-01",
            })
        audit.assert_called_once_with({
            "query": "黑镜",
            "tmdb_id": "42009",
            "season": 7,
            "as_of": "2026-08-01",
        })
        self.assertEqual(result.status, "updates_available")
        self.assertEqual(result.data["media_type"], "tv")
        self.assertEqual(
            result.data["check_definition"],
            "aired_normal_episodes_missing_from_enabled_media_servers",
        )

    def test_auto_not_found_does_not_claim_movie_or_series_is_current(self):
        with patch(
            "app.agent.update_actions.audit_series_episodes",
            return_value=_audit_result(status="not_found"),
        ):
            result = check_library_updates({
                "query": "未知标题",
                "media_type": "auto",
                "tmdb_id": "",
                "season": None,
                "as_of": "2026-08-01",
            })
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "cannot_determine")
        self.assertIn("无法可靠判断", result.summary)

    def test_registry_contract_rejects_future_dates_and_movie_seasons(self):
        registry = build_tool_registry()
        capability = {item["name"]: item for item in registry.capabilities()}["library.check_updates"]
        self.assertEqual(capability["risk"], "read")
        self.assertFalse(capability["requires_confirmation"])
        self.assertFalse(capability["parameters"]["additionalProperties"])
        self.assertEqual(capability["parameters"]["properties"]["media_type"]["enum"], ["auto", "tv", "movie"])

        future_date = "9999-12-31"
        invalid = (
            {"query": "黑镜", "as_of": future_date},
            {"query": "黑镜", "media_type": "documentary"},
            {"query": "沙丘2", "media_type": "movie", "season": 1},
            {"query": "黑镜", "token": "PRIVATE"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                registry.execute("library.check_updates", arguments)
        with self.assertRaises(AgentToolError):
            registry.execute("library.audit_episodes", {"query": "黑镜", "as_of": future_date})


class LibraryUpdateRoutingTests(unittest.TestCase):
    def _agent(self) -> AgentOrchestrator:
        registry = ToolRegistry()
        for name in (
            "library.check_updates",
            "library.audit_episodes",
            "library.search_missing_episode_resources",
            "workspace.search",
            "indexer.search_resources",
            "discovery.search",
            "config.diagnose",
            "library.search",
        ):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool=name: ToolResult(True, "success", tool, data=dict(arguments)),
                validator=_identity,
            ))
        return AgentOrchestrator(registry)

    def test_update_intent_extracts_media_type_and_scope(self):
        agent = self._agent()
        generic = agent.query("《黑镜》有没有更新")
        self.assertEqual(generic["tool_call"]["name"], "library.check_updates")
        self.assertEqual(generic["tool_call"]["arguments"], {"query": "黑镜", "media_type": "auto"})

        series = agent.query("检查电视剧《黑镜》第 7 季有没有更新，TMDB 42009")
        self.assertEqual(series["tool_call"]["name"], "library.check_updates")
        self.assertEqual(series["tool_call"]["arguments"], {
            "query": "黑镜", "tmdb_id": "42009", "season": 7, "media_type": "tv",
        })

        movie = agent.query("检查电影《沙丘2》是否有更新")
        self.assertEqual(movie["tool_call"]["name"], "library.check_updates")
        self.assertEqual(movie["tool_call"]["arguments"], {"query": "沙丘2", "media_type": "movie"})

    def test_update_intent_extracts_unquoted_titles(self):
        agent = self._agent()
        cases = (
            ("黑镜是否有更新", {"query": "黑镜", "media_type": "auto"}),
            ("检查黑镜有更新吗", {"query": "黑镜", "media_type": "auto"}),
            ("检查电视剧黑镜有无更新", {"query": "黑镜", "media_type": "tv"}),
            ("检查电影沙丘2是否有更新", {"query": "沙丘2", "media_type": "movie"}),
            ("黑镜有新一集吗", {"query": "黑镜", "media_type": "tv"}),
            ("黑镜有新一季吗", {"query": "黑镜", "media_type": "tv"}),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                response = agent.query(message)
                self.assertEqual(response["tool_call"]["name"], "library.check_updates")
                self.assertEqual(response["tool_call"]["arguments"], expected)

    def test_contextual_update_followup_inherits_verified_media_identity(self):
        agent = self._agent()
        context = [{
            "role": "assistant",
            "text": "《九门》目前共 22 集。",
            "tool_name": "library.count_series_episodes",
            "status": "success",
            "media_context": {"title": "九门", "year": "2026", "media_type": "tv"},
        }]

        response = agent.query("这部剧有更新吗", conversation_context=context)

        self.assertEqual(response["tool_call"]["name"], "library.check_updates")
        self.assertEqual(response["tool_call"]["arguments"], {
            "query": "九门",
            "media_type": "tv",
        })

    def test_explicit_update_title_overrides_previous_media_context(self):
        agent = self._agent()
        context = [{
            "role": "assistant",
            "text": "《九门》目前共 22 集。",
            "media_context": {"title": "九门", "year": "2026", "media_type": "tv"},
        }]

        response = agent.query("电影《沙丘2》有没有更新", conversation_context=context)

        self.assertEqual(response["tool_call"]["name"], "library.check_updates")
        self.assertEqual(response["tool_call"]["arguments"], {
            "query": "沙丘2",
            "media_type": "movie",
        })

    def test_update_intent_does_not_steal_other_scopes(self):
        agent = self._agent()
        cases = (
            ("检查《示例剧》第 2 季有没有缺集", "library.audit_episodes"),
            ("给《示例剧》第 2 季第 3 集找缺集资源", "library.search_missing_episode_resources"),
            ("搜索《示例剧》更新资源", "indexer.search_resources"),
            ("全局搜索《黑镜》有没有更新", "workspace.search"),
            ("在网上找《黑镜》有没有更新", "discovery.search"),
            ("查询 TMDB《黑镜》有没有更新", "discovery.search"),
            ("检查项目配置有没有更新", "config.diagnose"),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(agent.query(message)["tool_call"]["name"], expected)
        self.assertFalse(is_library_update_check_message("RSS 订阅有没有更新"))
        self.assertFalse(is_library_update_check_message("下载任务有没有更新"))
        self.assertFalse(is_library_update_check_message("刷新媒体库里的《黑镜》"))
        self.assertFalse(is_library_update_check_message("我本周读书进度有没有更新"))


class LibraryUpdateAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        matched = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not matched:
            matched = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not matched:
            raise AssertionError("CSRF token missing")
        return matched.group(1)

    def _login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_auth_csrf_and_shared_direct_query_rate_limit(self):
        path = "/api/agent/tools/library.check_updates"
        payload = {"session_id": "test_session_identifier_0001", "arguments": {"query": "黑镜", "media_type": "tv"}}
        self.assertEqual(self.client.post(path, json=payload).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json=payload).status_code, 403)
        headers = {"X-CSRF-Token": csrf}
        with patch("app.agent.update_actions.audit_series_episodes", return_value=_audit_result()):
            for _ in range(5):
                response = self.client.post(path, headers=headers, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
            sixth = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "检查电视剧《黑镜》有没有更新"},
            )
            self.assertEqual(sixth.status_code, 200, sixth.text)
            limited = self.client.post(
                "/api/agent/tools/library.audit_episodes",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"query": "黑镜"}},
            )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_invalid_arguments_are_rejected_before_audit(self):
        csrf = self._login()
        with patch("app.agent.update_actions.audit_series_episodes") as audit:
            response = self.client.post(
                "/api/agent/tools/library.check_updates",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "arguments": {
                    "query": "黑镜",
                    "as_of": "9999-12-31",
                }},
            )
        self.assertEqual(response.status_code, 400, response.text)
        audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
