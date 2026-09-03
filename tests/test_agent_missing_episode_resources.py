"""Media Agent 已播缺集定向资源搜索测试。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

from app.agent.episode_resource_actions import (
    missing_episode_resource_arguments,
    missing_season_resource_arguments,
    search_missing_episode_resources,
    search_missing_season_resources,
)
from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult


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
            **(
                {"target_missing": target_missing} if target_missing is not None else {}
            ),
            **({"library_name": library_name} if library_name else {}),
            "sources": [{"server_name": "secret-server", "path": "/private/media"}],
        },
        evidence=[
            Evidence("media_servers+tmdb", "safe audit", "2026-08-01T00:00:00+08:00")
        ],
        suggestions=["audit suggestion"],
        error="audit safe error" if not ok else "",
    )


def _search_result(*, ok=True, status="success", query="示例剧 S02E03", items=None):
    if items is None:
        items = (
            [
                {
                    "result_id": "safe-result-id-1234",
                    "site_id": "nyaa",
                    "site_name": "Nyaa",
                    "title": f"{query} 1080p",
                    "download_state": "ready",
                    "download_kinds": ["magnet"],
                }
            ]
            if ok
            else []
        )
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
        evidence=[
            Evidence("indexer_service", "safe search", "2026-08-01T00:00:00+08:00")
        ],
        suggestions=["选择 result_id 后可预检提交。"] if ok else [],
        error="safe indexer error" if not ok else "",
    )


class MissingEpisodeResourceToolTests(unittest.TestCase):
    def setUp(self):
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
        normalized = missing_episode_resource_arguments(
            {
                "query": "  示例劇  ",
                "tmdb_id": " 12345 ",
                "season": 2,
                "episode": 3,
                "sites": ["NYAA", "nyaa"],
                "limit": 10,
            }
        )
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
        with (
            patch("app.agent.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.indexer_actions.get_indexer_service", return_value=service
            ),
            self.assertRaisesRegex(AgentToolError, "站点未启用或不存在"),
        ):
            missing_episode_resource_arguments(
                {"query": "示例剧", "season": 2, "episode": 3, "sites": ["mikan"]}
            )

    def test_season_arguments_normalize_and_reject_unsafe_fields(self):
        normalized = missing_season_resource_arguments(
            {
                "query": "  示例劇  ",
                "tmdb_id": " 12345 ",
                "season": 2,
                "sites": ["NYAA", "nyaa"],
                "max_episodes": 3,
                "limit_per_episode": 10,
            }
        )
        self.assertEqual(
            normalized,
            {
                "query": "示例劇",
                "tmdb_id": "12345",
                "season": 2,
                "as_of": date.today().isoformat(),
                "sites": ["nyaa"],
                "max_episodes": 3,
                "limit_per_episode": 10,
            },
        )
        self.assertEqual(
            missing_season_resource_arguments({"query": "示例剧", "season": 2})[
                "max_episodes"
            ],
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
        with (
            patch("app.agent.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.indexer_actions.get_indexer_service", return_value=service
            ),
            self.assertRaisesRegex(AgentToolError, "站点未启用或不存在"),
        ):
            missing_season_resource_arguments(
                {"query": "示例剧", "season": 2, "sites": ["mikan"]}
            )

    def test_only_searches_after_exact_missing_episode_is_verified(self):
        arguments = missing_episode_resource_arguments(
            {
                "query": "示例剧",
                "season": 2,
                "episode": 3,
                "sites": ["nyaa"],
                "limit": 7,
            }
        )
        searched = _search_result()
        service = Mock(enabled_site_ids=("nyaa",))
        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(missing=[{"season": 2, "episode": 3}]),
            ) as audit,
            patch("app.agent.indexer_actions.config.get_bool", return_value=True),
            patch(
                "app.agent.indexer_actions.get_indexer_service", return_value=service
            ),
            patch(
                "app.agent.episode_resource_actions.search_resources",
                return_value=searched,
            ) as search,
        ):
            result = search_missing_episode_resources(arguments)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "success")
        self.assertTrue(result.data["verification"]["verified_missing"])
        self.assertEqual(result.data["verification"]["tmdb_id"], "12345")
        self.assertEqual(
            result.data["search"]["items"][0]["result_id"], "safe-result-id-1234"
        )
        self.assertEqual(result.data["search"]["items"][0]["quality"]["rank"], 1)
        self.assertEqual(
            result.data["search"]["recommendation"]["status"], "recommended"
        )
        self.assertFalse(result.data["search"]["download_plan"]["auto_submit"])
        self.assertEqual(
            result.data["search"]["download_plan"]["prepare_tool"], "ingest.submit"
        )
        audit.assert_called_once_with(
            {
                "query": "示例剧",
                "tmdb_id": "",
                "season": 2,
                "target_episode": 3,
                "as_of": date.today().isoformat(),
            }
        )
        call = search.call_args.args[0]
        self.assertEqual(call["title"], "示例剧 S02E03")
        self.assertIn("示例剧 2x03", call["aliases"])
        self.assertIn("示例剧 第2季 第3集", call["aliases"])
        serialized = repr(result.to_dict())
        self.assertNotIn("secret-server", serialized)
        self.assertNotIn("/private/media", serialized)

    def test_exact_high_episode_can_be_verified_outside_bounded_sample(self):
        arguments = missing_episode_resource_arguments(
            {"query": "示例剧", "season": 1, "episode": 150, "library_name": "美女库"}
        )
        searched = _search_result(query="示例剧 S01E150")
        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(
                    missing=[
                        {"season": 1, "episode": number} for number in range(1, 101)
                    ],
                    truncated=True,
                    target_missing=True,
                    library_name="美女库",
                ),
            ) as audit,
            patch(
                "app.agent.episode_resource_actions.search_resources",
                return_value=searched,
            ) as search,
        ):
            result = search_missing_episode_resources(arguments)
        self.assertTrue(result.ok)
        self.assertTrue(result.data["verification"]["verified_missing"])
        self.assertEqual(result.data["verification"]["library_name"], "美女库")
        audit.assert_called_once_with(
            {
                "query": "示例剧",
                "tmdb_id": "",
                "season": 1,
                "target_episode": 150,
                "as_of": date.today().isoformat(),
                "library_name": "美女库",
            }
        )
        self.assertEqual(search.call_args.args[0]["title"], "示例剧 S01E150")

    def test_preserves_verified_state_when_indexer_search_is_unavailable(self):
        arguments = missing_episode_resource_arguments(
            {"query": "示例剧", "season": 2, "episode": 3}
        )
        searched = _search_result(ok=False, status="unavailable")
        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(missing=[{"season": 2, "episode": 3}]),
            ),
            patch(
                "app.agent.episode_resource_actions.search_resources",
                return_value=searched,
            ) as search,
        ):
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
        arguments = missing_episode_resource_arguments(
            {"query": "示例剧", "season": 2, "episode": 3}
        )
        cases = (
            (_audit_result(status="up_to_date", missing=[]), "not_missing"),
            (_audit_result(missing=[{"season": 2, "episode": 4}]), "not_missing"),
            (
                _audit_result(missing=[{"season": 2, "episode": 4}], truncated=True),
                "inconclusive",
            ),
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
            with (
                self.subTest(status=status),
                patch(
                    "app.agent.episode_resource_actions.audit_series_episodes",
                    return_value=audit_result,
                ),
                patch("app.agent.episode_resource_actions.search_resources") as search,
            ):
                result = search_missing_episode_resources(arguments)
            self.assertFalse(result.ok)
            self.assertEqual(result.status, status)
            search.assert_not_called()

    def test_season_search_processes_verified_missing_episodes_in_order(self):
        arguments = missing_season_resource_arguments(
            {
                "query": "示例剧",
                "season": 2,
                "sites": ["nyaa"],
                "max_episodes": 3,
                "limit_per_episode": 7,
            }
        )
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
        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(missing=missing),
            ) as audit,
            patch(
                "app.agent.episode_resource_actions.search_resources",
                side_effect=searches,
            ) as search,
        ):
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
        self.assertTrue(
            all(call.args[0]["limit"] == 7 for call in search.call_args_list)
        )
        serialized = repr(result.to_dict())
        self.assertNotIn("secret-server", serialized)
        self.assertNotIn("/private/media", serialized)

    def test_season_search_refuses_unverified_truncated_or_empty_samples(self):
        arguments = missing_season_resource_arguments({"query": "示例剧", "season": 2})
        cases = (
            (_audit_result(status="up_to_date", missing=[]), "not_missing"),
            (_audit_result(status="ambiguous", ok=False), "ambiguous"),
            (
                _audit_result(missing=[{"season": 2, "episode": 3}], truncated=True),
                "inconclusive",
            ),
            (
                _audit_result(missing=[{"season": 1, "episode": 3}, "invalid"]),
                "not_missing",
            ),
        )
        for audit_result, status in cases:
            with (
                self.subTest(status=status),
                patch(
                    "app.agent.episode_resource_actions.audit_series_episodes",
                    return_value=audit_result,
                ),
                patch("app.agent.episode_resource_actions.search_resources") as search,
            ):
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
        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(missing=missing),
            ),
            patch(
                "app.agent.episode_resource_actions.search_resources",
                side_effect=searches,
            ) as search,
        ):
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
        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(missing=missing),
            ),
            patch("app.agent.episode_resource_actions.search_resources") as search,
            patch(
                "app.agent.episode_resource_actions.time.monotonic",
                side_effect=[0.0, 31.0],
            ),
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
        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(missing=missing),
            ),
            patch(
                "app.agent.episode_resource_actions.search_resources",
                return_value=_search_result(query="示例剧 S02E01"),
            ) as search,
            patch(
                "app.agent.episode_resource_actions.time.monotonic",
                side_effect=[0.0, 0.0, 31.0],
            ),
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
