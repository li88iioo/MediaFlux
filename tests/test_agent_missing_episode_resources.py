"""Media Agent 已播缺集定向资源搜索测试。"""
from __future__ import annotations

from datetime import date, timedelta
import re
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.episode_resource_actions import (
    missing_episode_resource_arguments,
    missing_season_resource_arguments,
    search_missing_episode_resources,
    search_missing_season_resources,
)
from app.agent.models import Evidence, RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_missing_season_resource_search_message,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


def _identity(arguments):
    return dict(arguments)


def _audit_result(
    *,
    status="updates_available",
    ok=True,
    missing=None,
    truncated=False,
    target_missing=None,
    library_name="",
):
    return ToolResult(
        ok,
        status,
        "audit",
        data={
            "title": "示例剧",
            "tmdb_id": "12345",
            "missing_count": len(missing or []),
            "missing_sample": list(missing or []),
            "missing_sample_truncated": truncated,
            **({"target_missing": target_missing} if target_missing is not None else {}),
            **({"library_name": library_name} if library_name else {}),
            "sources": [{"server_name": "secret-server", "path": "/private/media"}],
        },
        evidence=[Evidence("media_servers+tmdb", "safe audit", "2026-08-01T00:00:00+08:00")],
        suggestions=["audit suggestion"],
        error="audit safe error" if not ok else "",
    )


def _search_result(*, ok=True, status="success", query="示例剧 S02E03", items=None):
    if items is None:
        items = [{
            "result_id": "safe-result-id-1234",
            "site_id": "nyaa",
            "site_name": "Nyaa",
            "title": f"{query} 1080p",
            "download_state": "ready",
            "download_kinds": ["magnet"],
        }] if ok else []
    return ToolResult(
        ok,
        status,
        "找到 1 项可查看资源" if ok else "资源搜索不可用",
        data={
            "query": query,
            "page": 1,
            "items": items,
            "sites_attempted": ["nyaa"],
            "sites_succeeded": ["nyaa"],
            "errors": [],
            "partial": False,
            "cached": False,
            "has_more": False,
        },
        evidence=[Evidence("indexer_service", "safe search", "2026-08-01T00:00:00+08:00")],
        suggestions=["选择 result_id 后可预检提交。"] if ok else [],
        error="safe indexer error" if not ok else "",
    )


class MissingEpisodeResourceToolTests(unittest.TestCase):
    def setUp(self):
        # 参数规范化测试不应依赖开发机 user.env 当前启用了哪些站点。
        # 保留真实的“仅允许已启用站点”校验，只固定其运行时依赖。
        enabled_service = Mock(enabled_site_ids=("nyaa",))
        get_bool_patch = patch(
            "app.agent.indexer_actions.config.get_bool", return_value=True
        )
        service_patch = patch(
            "app.agent.indexer_actions.get_indexer_service",
            return_value=enabled_service,
        )
        get_bool_patch.start()
        service_patch.start()
        self.addCleanup(service_patch.stop)
        self.addCleanup(get_bool_patch.stop)

    def test_arguments_normalize_and_reject_unsafe_fields(self):
        normalized = missing_episode_resource_arguments({
            "query": "  示例劇  ",
            "tmdb_id": " 12345 ",
            "season": 2,
            "episode": 3,
            "sites": ["NYAA", "nyaa"],
            "limit": 10,
        })
        self.assertEqual(normalized["query"], "示例劇")
        self.assertEqual(normalized["tmdb_id"], "12345")
        self.assertEqual(normalized["season"], 2)
        self.assertEqual(normalized["episode"], 3)
        self.assertEqual(normalized["sites"], ["nyaa"])
        self.assertEqual(normalized["limit"], 10)
        self.assertEqual(normalized["as_of"], date.today().isoformat())

        invalid = (
            {},
            {"query": "x", "season": 1, "episode": 1, "url": "https://secret"},
            {"query": "x", "season": True, "episode": 1},
            {"query": "x", "season": 1, "episode": 0},
            {"query": "x", "season": 1, "episode": 1, "tmdb_id": "../1"},
            {"query": "x", "season": 1, "episode": 1, "sites": ["bad/site"]},
            {"query": "x", "season": 1, "episode": 1, "limit": 51},
            {
                "query": "x",
                "season": 1,
                "episode": 1,
                "as_of": (date.today() + timedelta(days=1)).isoformat(),
            },
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                missing_episode_resource_arguments(arguments)

        service = Mock(enabled_site_ids=("nyaa",))
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), self.assertRaisesRegex(AgentToolError, "站点未启用或不存在"):
            missing_episode_resource_arguments({
                "query": "示例剧",
                "season": 2,
                "episode": 3,
                "sites": ["mikan"],
            })


    def test_season_arguments_normalize_and_reject_unsafe_fields(self):
        normalized = missing_season_resource_arguments({
            "query": "  示例劇  ",
            "tmdb_id": " 12345 ",
            "season": 2,
            "sites": ["NYAA", "nyaa"],
            "max_episodes": 3,
            "limit_per_episode": 10,
        })
        self.assertEqual(normalized, {
            "query": "示例劇",
            "tmdb_id": "12345",
            "season": 2,
            "as_of": date.today().isoformat(),
            "sites": ["nyaa"],
            "max_episodes": 3,
            "limit_per_episode": 10,
        })
        self.assertEqual(
            missing_season_resource_arguments({"query": "示例剧", "season": 2})["max_episodes"],
            3,
        )

        invalid = (
            {},
            {"query": "x", "season": 1, "episode": 1},
            {"query": "x", "season": True},
            {"query": "x", "season": 0},
            {"query": "x", "season": 1, "tmdb_id": ""},
            {"query": "x", "season": 1, "tmdb_id": "../1"},
            {"query": "x", "season": 1, "sites": ["bad/site"]},
            {"query": "x", "season": 1, "max_episodes": 4},
            {"query": "x", "season": 1, "limit_per_episode": 11},
            {
                "query": "x",
                "season": 1,
                "as_of": (date.today() + timedelta(days=1)).isoformat(),
            },
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                missing_season_resource_arguments(arguments)

        service = Mock(enabled_site_ids=("nyaa",))
        with patch("app.agent.indexer_actions.config.get_bool", return_value=True), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), self.assertRaisesRegex(AgentToolError, "站点未启用或不存在"):
            missing_season_resource_arguments({
                "query": "示例剧", "season": 2, "sites": ["mikan"]
            })

    def test_only_searches_after_exact_missing_episode_is_verified(self):
        arguments = missing_episode_resource_arguments({
            "query": "示例剧",
            "season": 2,
            "episode": 3,
            "sites": ["nyaa"],
            "limit": 7,
        })
        searched = _search_result()
        service = Mock(enabled_site_ids=("nyaa",))
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes",
            return_value=_audit_result(missing=[{"season": 2, "episode": 3}]),
        ) as audit, patch(
            "app.agent.indexer_actions.config.get_bool", return_value=True
        ), patch(
            "app.agent.indexer_actions.get_indexer_service", return_value=service
        ), patch(
            "app.agent.episode_resource_actions.search_resources", return_value=searched
        ) as search:
            result = search_missing_episode_resources(arguments)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")
        self.assertTrue(result.data["verification"]["verified_missing"])
        self.assertEqual(result.data["verification"]["tmdb_id"], "12345")
        self.assertEqual(result.data["search"]["items"][0]["result_id"], "safe-result-id-1234")
        self.assertEqual(result.data["search"]["items"][0]["quality"]["rank"], 1)
        self.assertEqual(result.data["search"]["recommendation"]["status"], "recommended")
        self.assertFalse(result.data["search"]["download_plan"]["auto_submit"])
        self.assertEqual(
            result.data["search"]["download_plan"]["prepare_tool"],
            "indexer.submit_candidate",
        )
        audit.assert_called_once_with({
            "query": "示例剧",
            "tmdb_id": "",
            "season": 2,
            "target_episode": 3,
            "as_of": date.today().isoformat(),
        })
        call = search.call_args.args[0]
        self.assertEqual(call["title"], "示例剧 S02E03")
        self.assertIn("示例剧 2x03", call["aliases"])
        self.assertIn("示例剧 第2季 第3集", call["aliases"])
        serialized = repr(result.to_dict())
        self.assertNotIn("secret-server", serialized)
        self.assertNotIn("/private/media", serialized)

    def test_exact_high_episode_can_be_verified_outside_bounded_sample(self):
        arguments = missing_episode_resource_arguments({
            "query": "示例剧",
            "season": 1,
            "episode": 150,
            "library_name": "美女库",
        })
        searched = _search_result(query="示例剧 S01E150")
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes",
            return_value=_audit_result(
                missing=[{"season": 1, "episode": number} for number in range(1, 101)],
                truncated=True,
                target_missing=True,
                library_name="美女库",
            ),
        ) as audit, patch(
            "app.agent.episode_resource_actions.search_resources", return_value=searched
        ) as search:
            result = search_missing_episode_resources(arguments)

        self.assertTrue(result.ok)
        self.assertTrue(result.data["verification"]["verified_missing"])
        self.assertEqual(result.data["verification"]["library_name"], "美女库")
        audit.assert_called_once_with({
            "query": "示例剧",
            "tmdb_id": "",
            "season": 1,
            "target_episode": 150,
            "as_of": date.today().isoformat(),
            "library_name": "美女库",
        })
        self.assertEqual(search.call_args.args[0]["title"], "示例剧 S01E150")

    def test_preserves_verified_state_when_indexer_search_is_unavailable(self):
        arguments = missing_episode_resource_arguments({
            "query": "示例剧",
            "season": 2,
            "episode": 3,
        })
        searched = _search_result(ok=False, status="unavailable")
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes",
            return_value=_audit_result(missing=[{"season": 2, "episode": 3}]),
        ), patch(
            "app.agent.episode_resource_actions.search_resources", return_value=searched
        ) as search:
            result = search_missing_episode_resources(arguments)

        search.assert_called_once()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.error, "safe indexer error")
        self.assertTrue(result.data["verification"]["verified_missing"])
        self.assertEqual(result.data["search"]["query"], "示例剧 S02E03")
        self.assertIn("agent_verification", [item.source for item in result.evidence])
        serialized = repr(result.to_dict())
        self.assertNotIn("secret-server", serialized)
        self.assertNotIn("/private/media", serialized)

    def test_does_not_search_when_target_is_not_missing_or_audit_is_inconclusive(self):
        arguments = missing_episode_resource_arguments({"query": "示例剧", "season": 2, "episode": 3})
        cases = (
            (_audit_result(status="up_to_date", missing=[]), "not_missing"),
            (_audit_result(missing=[{"season": 2, "episode": 4}]), "not_missing"),
            (_audit_result(missing=[{"season": 2, "episode": 4}], truncated=True), "inconclusive"),
            (_audit_result(status="ambiguous", ok=False), "ambiguous"),
            (
                _audit_result(
                    status="updates_available",
                    ok=False,
                    missing=[{"season": 2, "episode": 3}],
                ),
                "updates_available",
            ),
        )
        for audit_result, status in cases:
            with self.subTest(status=status), patch(
                "app.agent.episode_resource_actions.audit_series_episodes", return_value=audit_result
            ), patch("app.agent.episode_resource_actions.search_resources") as search:
                result = search_missing_episode_resources(arguments)
            self.assertFalse(result.ok)
            self.assertEqual(result.status, status)
            search.assert_not_called()

    def test_season_search_processes_verified_missing_episodes_in_order(self):
        arguments = missing_season_resource_arguments({
            "query": "示例剧",
            "season": 2,
            "sites": ["nyaa"],
            "max_episodes": 3,
            "limit_per_episode": 7,
        })
        missing = [
            {"season": 2, "episode": 5},
            {"season": 1, "episode": 1},
            {"season": 2, "episode": 3},
            {"season": 2, "episode": 4},
            {"season": 2, "episode": 3},
            {"season": 2, "episode": True},
            "invalid",
        ]
        searches = [
            _search_result(query="示例剧 S02E03"),
            _search_result(query="示例剧 S02E04"),
            _search_result(query="示例剧 S02E05"),
        ]
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes",
            return_value=_audit_result(missing=missing),
        ) as audit, patch(
            "app.agent.episode_resource_actions.search_resources", side_effect=searches
        ) as search:
            result = search_missing_season_resources(arguments)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["missing_total"], 3)
        self.assertEqual(result.data["processed"], 3)
        self.assertEqual(result.data["remaining"], 0)
        self.assertEqual(result.data["failed"], 0)
        self.assertTrue(result.data["verification"]["verified_missing"])
        self.assertEqual(result.data["verification"]["missing_count"], 3)
        self.assertEqual(
            [item["episode_label"] for item in result.data["episodes"]],
            ["S02E03", "S02E04", "S02E05"],
        )
        audit.assert_called_once()
        self.assertEqual(search.call_count, 3)
        self.assertEqual(
            [call.args[0]["title"] for call in search.call_args_list],
            ["示例剧 S02E03", "示例剧 S02E04", "示例剧 S02E05"],
        )
        self.assertTrue(all(call.args[0]["limit"] == 7 for call in search.call_args_list))
        serialized = repr(result.to_dict())
        self.assertNotIn("secret-server", serialized)
        self.assertNotIn("/private/media", serialized)

    def test_season_search_refuses_unverified_truncated_or_empty_samples(self):
        arguments = missing_season_resource_arguments({"query": "示例剧", "season": 2})
        cases = (
            (_audit_result(status="up_to_date", missing=[]), "not_missing"),
            (_audit_result(status="ambiguous", ok=False), "ambiguous"),
            (_audit_result(missing=[{"season": 2, "episode": 3}], truncated=True), "inconclusive"),
            (_audit_result(missing=[{"season": 1, "episode": 3}, "invalid"]), "not_missing"),
        )
        for audit_result, status in cases:
            with self.subTest(status=status), patch(
                "app.agent.episode_resource_actions.audit_series_episodes", return_value=audit_result
            ), patch("app.agent.episode_resource_actions.search_resources") as search:
                result = search_missing_season_resources(arguments)
            self.assertFalse(result.ok)
            self.assertEqual(result.status, status)
            search.assert_not_called()

    def test_season_search_preserves_partial_results_and_remaining_count(self):
        arguments = missing_season_resource_arguments({"query": "示例剧", "season": 2})
        missing = [{"season": 2, "episode": episode} for episode in (1, 2, 3, 4)]
        searches = [
            _search_result(query="示例剧 S02E01"),
            _search_result(ok=False, status="unavailable", query="示例剧 S02E02"),
            _search_result(query="示例剧 S02E03"),
        ]
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes",
            return_value=_audit_result(missing=missing),
        ), patch(
            "app.agent.episode_resource_actions.search_resources", side_effect=searches
        ) as search:
            result = search_missing_season_resources(arguments)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(search.call_count, 3)
        self.assertEqual(result.data["processed"], 3)
        self.assertEqual(result.data["failed"], 1)
        self.assertEqual(result.data["remaining"], 1)
        self.assertEqual(len(result.data["episodes"]), 3)
        self.assertTrue(any("部分集" in item for item in result.suggestions))
        self.assertTrue(any("剩余" in item for item in result.suggestions))

    def test_season_search_does_not_start_indexers_after_audit_consumes_deadline(self):
        arguments = missing_season_resource_arguments({"query": "示例剧", "season": 2})
        missing = [{"season": 2, "episode": episode} for episode in (1, 2)]
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes",
            return_value=_audit_result(missing=missing),
        ), patch(
            "app.agent.episode_resource_actions.search_resources",
        ) as search, patch(
            "app.agent.episode_resource_actions.time.monotonic",
            side_effect=[0.0, 31.0],
        ):
            result = search_missing_season_resources(arguments)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "partial")
        search.assert_not_called()
        self.assertEqual(result.data["processed"], 0)
        self.assertEqual(result.data["remaining"], 2)
        self.assertTrue(any("耗时上限" in item for item in result.suggestions))

    def test_season_search_stops_at_absolute_deadline(self):
        arguments = missing_season_resource_arguments({"query": "示例剧", "season": 2})
        missing = [{"season": 2, "episode": episode} for episode in (1, 2, 3)]
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes",
            return_value=_audit_result(missing=missing),
        ), patch(
            "app.agent.episode_resource_actions.search_resources",
            return_value=_search_result(query="示例剧 S02E01"),
        ) as search, patch(
            "app.agent.episode_resource_actions.time.monotonic",
            side_effect=[0.0, 0.0, 31.0],
        ):
            result = search_missing_season_resources(arguments)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(search.call_count, 1)
        self.assertEqual(search.call_args.kwargs, {"timeout_seconds": 30.0})
        self.assertEqual(result.data["processed"], 1)
        self.assertEqual(result.data["remaining"], 2)
        self.assertTrue(result.data["truncated"])
        self.assertTrue(any("耗时上限" in item for item in result.suggestions))

    def test_season_resource_questions_do_not_trigger_batch_search(self):
        for message in (
            "《示例剧》第 2 季缺集资源要不要找？",
            "《示例剧》第 2 季缺集资源有没有？",
            "是否搜索《示例剧》第 2 季缺集资源",
            "别搜《示例剧》第 2 季缺集资源",
        ):
            with self.subTest(message=message):
                self.assertFalse(is_missing_season_resource_search_message(message))

    def test_registry_and_natural_language_routing_preserve_existing_boundaries(self):
        capabilities = {item["name"]: item for item in build_tool_registry().capabilities()}
        tool = capabilities["library.search_missing_episode_resources"]
        self.assertEqual(tool["risk"], "read")
        self.assertFalse(tool["requires_confirmation"])
        parameters = tool["parameters"]
        self.assertEqual(parameters["required"], ["query", "season", "episode"])
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["properties"]["season"]["maximum"], 100)
        self.assertEqual(parameters["properties"]["episode"]["maximum"], 1000)
        self.assertEqual(parameters["properties"]["limit"]["maximum"], 50)
        self.assertEqual(parameters["properties"]["tmdb_id"]["pattern"], "^[0-9]{1,10}$")
        season_tool = capabilities["library.search_missing_season_resources"]
        self.assertEqual(season_tool["risk"], "read")
        self.assertFalse(season_tool["requires_confirmation"])
        season_parameters = season_tool["parameters"]
        self.assertEqual(season_parameters["required"], ["query", "season"])
        self.assertFalse(season_parameters["additionalProperties"])
        self.assertEqual(season_parameters["properties"]["max_episodes"]["maximum"], 3)
        self.assertEqual(season_parameters["properties"]["limit_per_episode"]["maximum"], 10)

        calls: list[tuple[str, dict]] = []
        registry = ToolRegistry()
        for name in (
            "library.search_missing_season_resources",
            "library.search_missing_episode_resources",
            "library.audit_episodes",
            "indexer.search_resources",
            "library.search",
        ):
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk=RiskLevel.READ,
                parameters={},
                handler=lambda arguments, tool_name=name: (
                    calls.append((tool_name, dict(arguments))) or ToolResult(True, "success", tool_name)
                ),
                validator=_identity,
            ))
        agent = AgentOrchestrator(registry)

        season_scoped = agent.query("给《示例剧》第 2 季所有缺集找资源")
        self.assertEqual(season_scoped["tool_call"]["name"], "library.search_missing_season_resources")
        self.assertEqual(season_scoped["tool_call"]["arguments"], {"query": "示例剧", "season": 2})
        season_coded = agent.query("搜索《示例剧》S2 这季缺的资源")
        self.assertEqual(season_coded["tool_call"]["name"], "library.search_missing_season_resources")
        self.assertEqual(season_coded["tool_call"]["arguments"], {"query": "示例剧", "season": 2})

        for unquoted_season in (
            "给 示例剧 第2季所有缺集找资源",
            "给示例剧 S2 缺集找资源",
            "帮示例剧批量找第 2 季漏集资源",
            "帮我示例剧批量找第 2 季漏集资源",
        ):
            with self.subTest(unquoted_season=unquoted_season):
                routed = agent.query(unquoted_season)
                self.assertEqual(
                    routed["tool_call"]["name"],
                    "library.search_missing_season_resources",
                )
                self.assertEqual(
                    routed["tool_call"]["arguments"],
                    {"query": "示例剧", "season": 2},
                )

        library_scoped = agent.query("给我的美女库中《示例剧》第 1 季第 150 集找缺集资源")
        self.assertEqual(
            library_scoped["tool_call"]["name"],
            "library.search_missing_episode_resources",
        )
        self.assertEqual(
            library_scoped["tool_call"]["arguments"],
            {"query": "示例剧", "season": 1, "episode": 150, "library_name": "美女库"},
        )

        scoped = agent.query("给《示例剧》第 2 季第 3 集找缺集资源")
        self.assertEqual(scoped["tool_call"]["name"], "library.search_missing_episode_resources")
        self.assertEqual(scoped["tool_call"]["arguments"], {"query": "示例剧", "season": 2, "episode": 3})
        coded = agent.query("搜索《示例剧》S02E03 的缺集资源，TMDB 12345")
        self.assertEqual(coded["tool_call"]["name"], "library.search_missing_episode_resources")
        self.assertEqual(coded["tool_call"]["arguments"]["tmdb_id"], "12345")
        adjacent = agent.query("搜索示例剧S02E03缺集资源")
        self.assertEqual(adjacent["tool_call"]["name"], "library.search_missing_episode_resources")
        self.assertEqual(
            adjacent["tool_call"]["arguments"],
            {"query": "示例剧", "season": 2, "episode": 3},
        )
        unquoted = agent.query("给示例剧第2季第3集找缺集资源")
        self.assertEqual(
            unquoted["tool_call"]["arguments"],
            {"query": "示例剧", "season": 2, "episode": 3},
        )
        crossed = agent.query("搜索示例剧2x03缺集资源")
        self.assertEqual(
            crossed["tool_call"]["arguments"],
            {"query": "示例剧", "season": 2, "episode": 3},
        )
        chinese_episode = agent.query("给《示例剧》第二季第三十四集找缺集资源")
        self.assertEqual(
            chinese_episode["tool_call"]["arguments"],
            {"query": "示例剧", "season": 2, "episode": 34},
        )
        chinese_season = agent.query("给《示例剧》第二季所有缺集找资源")
        self.assertEqual(
            chinese_season["tool_call"]["arguments"],
            {"query": "示例剧", "season": 2},
        )

        scoped_audit = agent.query("检查我的美女库中《示例剧》第 1 季第 150 集有没有缺集")
        self.assertEqual(scoped_audit["tool_call"]["name"], "library.audit_episodes")
        self.assertEqual(
            scoped_audit["tool_call"]["arguments"],
            {"query": "示例剧", "season": 1, "target_episode": 150, "library_name": "美女库"},
        )

        audit = agent.query("检查《示例剧》第 2 季有没有缺集")
        self.assertEqual(audit["tool_call"]["name"], "library.audit_episodes")
        chinese_audit = agent.query("核对《示例剧》第二季")
        self.assertEqual(chinese_audit["tool_call"]["name"], "library.audit_episodes")
        self.assertEqual(
            chinese_audit["tool_call"]["arguments"],
            {"query": "示例剧", "season": 2},
        )
        resource = agent.query("搜索《示例剧》的资源")
        self.assertEqual(resource["tool_call"]["name"], "indexer.search_resources")
        episode_resource = agent.query("搜索《示例剧》S02E03 的资源")
        self.assertEqual(
            episode_resource["tool_call"]["name"],
            "library.search_missing_episode_resources",
        )
        self.assertEqual(
            episode_resource["tool_call"]["arguments"],
            {"query": "示例剧", "season": 2, "episode": 3},
        )
        contextual_episode = agent.query(
            "给这部剧第2季第3集找资源",
            conversation_context=[{
                "role": "assistant",
                "text": "《示例剧》当前共有 24 集。",
                "tool_name": "library.count_series_episodes",
                "media_context": {"title": "示例剧", "media_type": "tv"},
            }],
        )
        self.assertEqual(
            contextual_episode["tool_call"]["name"],
            "library.search_missing_episode_resources",
        )
        self.assertEqual(
            contextual_episode["tool_call"]["arguments"],
            {"query": "示例剧", "season": 2, "episode": 3},
        )

        # 真实用户旅程：先询问本地集数，再沿用已核验剧集身份做缺集核对，
        # 最后按明确季集搜索资源。各轮只依赖公开会话投影，不要求用户记工具名。
        journey_calls: list[tuple[str, dict]] = []
        journey_registry = ToolRegistry()
        journey_registry.register(ToolSpec(
            name="library.count_series_episodes",
            description="count",
            risk=RiskLevel.READ,
            parameters={},
            validator=_identity,
            handler=lambda arguments: (
                journey_calls.append(("library.count_series_episodes", dict(arguments)))
                or ToolResult(
                    True,
                    "success",
                    "《九门》共 22 集。",
                    data={
                        "query": "九门",
                        "title": "九门",
                        "year": "2026",
                        "media_type": "tv",
                        "local_episode_count": 22,
                    },
                )
            ),
        ))
        journey_registry.register(ToolSpec(
            name="library.audit_episodes",
            description="audit",
            risk=RiskLevel.READ,
            parameters={},
            validator=_identity,
            handler=lambda arguments: (
                journey_calls.append(("library.audit_episodes", dict(arguments)))
                or _audit_result(missing=[{"season": 2, "episode": 3}])
            ),
        ))
        journey_registry.register(ToolSpec(
            name="library.search_missing_episode_resources",
            description="search missing",
            risk=RiskLevel.READ,
            parameters={},
            validator=_identity,
            handler=lambda arguments: (
                journey_calls.append((
                    "library.search_missing_episode_resources",
                    dict(arguments),
                ))
                or _search_result(query="九门 S02E03")
            ),
        ))
        journey_agent = AgentOrchestrator(journey_registry)

        counted = journey_agent.query("媒体库中九门有多少集", present=False)
        counted_data = counted["result"]["data"]
        conversation_context = [{
            "role": "assistant",
            "text": counted["result"]["summary"],
            "tool_name": counted["tool_call"]["name"],
            "status": counted["result"]["status"],
            "media_context": {
                "title": counted_data["title"],
                "year": counted_data["year"],
                "media_type": counted_data["media_type"],
            },
        }]
        audited = journey_agent.query(
            "这部剧有没有缺集",
            conversation_context=conversation_context,
            present=False,
        )
        targeted = journey_agent.query(
            "给这部剧第2季第3集找资源",
            conversation_context=conversation_context,
            present=False,
        )

        self.assertEqual(audited["tool_call"]["name"], "library.audit_episodes")
        self.assertEqual(
            targeted["tool_call"]["name"],
            "library.search_missing_episode_resources",
        )
        self.assertEqual(journey_calls, [
            ("library.count_series_episodes", {"query": "九门"}),
            ("library.audit_episodes", {"query": "九门"}),
            (
                "library.search_missing_episode_resources",
                {"query": "九门", "season": 2, "episode": 3},
            ),
        ])

        switched_context = [
            {
                "role": "assistant",
                "text": "《示例剧》当前共有 24 集。",
                "tool_name": "library.count_series_episodes",
                "media_context": {"title": "示例剧", "media_type": "tv"},
            },
            {
                "role": "assistant",
                "text": "下载队列状态正常。",
                "tool_name": "downloads.diagnose_queue",
            },
        ]
        switched = agent.query(
            "搜索这部剧的资源",
            conversation_context=switched_context,
        )
        self.assertIsNone(switched.get("tool_call"))

        type_conflict = agent.query(
            "搜索这部电影的资源",
            conversation_context=[{
                "role": "assistant",
                "text": "《示例剧》当前共有 24 集。",
                "tool_name": "library.count_series_episodes",
                "media_context": {"title": "示例剧", "media_type": "tv"},
            }],
        )
        self.assertIsNone(type_conflict.get("tool_call"))


class MissingEpisodeResourceAPITests(IsolatedDatabaseTestCase):
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

    def test_direct_tool_and_query_are_authenticated_csrf_protected_and_rate_limited(self):
        unauthenticated = self.client.post(
            "/api/agent/tools/library.search_missing_episode_resources",
            json={"session_id": "test_session_identifier_0001", "arguments": {"query": "示例剧", "season": 2, "episode": 3}},
        )
        self.assertEqual(unauthenticated.status_code, 401)

        csrf = self.login()
        payload = {"session_id": "test_session_identifier_0001", "arguments": {"query": "示例剧", "season": 2, "episode": 3}}
        missing_csrf = self.client.post(
            "/api/agent/tools/library.search_missing_episode_resources", json=payload
        )
        self.assertEqual(missing_csrf.status_code, 403)

        headers = {"X-CSRF-Token": csrf}
        audit = _audit_result(missing=[{"season": 2, "episode": 3}])
        searched = _search_result()
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes", return_value=audit
        ), patch(
            "app.agent.episode_resource_actions.search_resources", return_value=searched
        ):
            for _ in range(4):
                response = self.client.post(
                    "/api/agent/tools/library.search_missing_episode_resources",
                    headers=headers,
                    json=payload,
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["result"]["data"]["verification"]["verified_missing"])
            limited = self.client.post(
                "/api/agent/tools/library.search_missing_episode_resources",
                headers=headers,
                json=payload,
            )
        self.assertEqual(limited.status_code, 429, limited.text)

        agent_rate_limiter.reset()
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes", return_value=audit
        ), patch(
            "app.agent.episode_resource_actions.search_resources", return_value=searched
        ):
            query = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "给《示例剧》第 2 季第 3 集找缺集资源"},
            )
        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(query.json()["tool_call"]["name"], "library.search_missing_episode_resources")

        agent_rate_limiter.reset()
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes", return_value=audit
        ), patch(
            "app.agent.episode_resource_actions.search_resources", return_value=searched
        ):
            for _ in range(3):
                direct = self.client.post(
                    "/api/agent/tools/library.search_missing_episode_resources",
                    headers=headers,
                    json=payload,
                )
                self.assertEqual(direct.status_code, 200, direct.text)
            fourth = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "搜索示例剧Ｓ０２Ｅ０３缺集资源"},
            )
            fifth = self.client.post(
                "/api/agent/tools/library.search_missing_episode_resources",
                headers=headers,
                json=payload,
            )
        self.assertEqual(fourth.status_code, 200, fourth.text)
        self.assertEqual(fifth.status_code, 429, fifth.text)


    def test_season_direct_tool_and_query_share_weighted_rate_limit(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        payload = {"session_id": "test_session_identifier_0001", "arguments": {"query": "示例剧", "season": 2}}
        audit = _audit_result(missing=[{"season": 2, "episode": 1}])
        searched = _search_result(query="示例剧 S02E01")
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes", return_value=audit
        ), patch(
            "app.agent.episode_resource_actions.search_resources", return_value=searched
        ):
            first = self.client.post(
                "/api/agent/tools/library.search_missing_season_resources",
                headers=headers,
                json=payload,
            )
            second = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "给《示例剧》第 2 季所有缺集找资源"},
            )
            blocked = self.client.post(
                "/api/agent/tools/library.search_missing_episode_resources",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"query": "示例剧", "season": 2, "episode": 1}},
            )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(
            second.json()["tool_call"]["name"], "library.search_missing_season_resources"
        )
        self.assertEqual(blocked.status_code, 429, blocked.text)

        agent_rate_limiter.reset()
        with patch(
            "app.agent.episode_resource_actions.audit_series_episodes", return_value=audit
        ), patch(
            "app.agent.episode_resource_actions.search_resources", return_value=searched
        ):
            for _ in range(2):
                response = self.client.post(
                    "/api/agent/tools/library.search_missing_season_resources",
                    headers=headers,
                    json=payload,
                )
                self.assertEqual(response.status_code, 200, response.text)
            limited = self.client.post(
                "/api/agent/tools/library.search_missing_season_resources",
                headers=headers,
                json=payload,
            )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    unittest.main()
