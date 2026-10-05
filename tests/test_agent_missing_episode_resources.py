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
        enabled_service.site_ids_for_scope.return_value = ("nyaa",)
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
        service.site_ids_for_scope.return_value = ("nyaa",)
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
        service.site_ids_for_scope.return_value = ("nyaa",)
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

    def test_old_sample_cannot_replace_exact_target_verdict(self):
        arguments = missing_episode_resource_arguments(
            {"query": "示例剧", "season": 2, "episode": 3}
        )
        for verdict in (None, "true", 1, [], {}):
            audit = _audit_result(missing=[{"season": 2, "episode": 3}])
            audit.data["target_missing"] = verdict
            with (
                self.subTest(verdict=verdict),
                patch("app.agent.episode_resource_actions.audit_series_episodes", return_value=audit),
                patch("app.agent.episode_resource_actions.search_resources") as search,
            ):
                result = search_missing_episode_resources(arguments)
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "inconclusive")
            search.assert_not_called()

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
        service.site_ids_for_scope.return_value = ("nyaa",)
        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(missing=[{"season": 2, "episode": 3}], target_missing=True),
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
            },
            refresh=True,
        )
        call = search.call_args.args[0]
        self.assertEqual(call["title"], "示例剧")
        self.assertEqual(call["aliases"], [])
        self.assertEqual(call["season"], 2)
        self.assertEqual(call["episode"], 3)
        serialized = repr(result.to_dict())
        self.assertNotIn("secret-server", serialized)
        self.assertNotIn("/private/media", serialized)

    def test_missing_ninth_episode_does_not_promote_old_packs_or_unknown_coverage(self):
        from app.agent.kernel.ux_selection import _recommend, candidate_item

        arguments = missing_episode_resource_arguments({"query": "东大高武学院", "season": 1, "episode": 9})
        old_titles = ["[GM-Team] 东大高武学院 S01E01-E04 4K", "东大高武学院 S01E08"]
        for extra, expected in ((None, []), ("[GM-Team][东大高武学院][01-04][4K]", []), ("东大高武学院 全集", []), ("东大高武学院 S01E09", [1]), ("东大高武学院 S01E08-E09", [1])):
            titles = old_titles + ([extra] if extra else [])
            searched = _search_result(status="partial", items=[{
                "result_id": f"episode-nine-result-{position:03}", "title": title,
                "site_id": "nyaa", "site_name": "Nyaa", "download_state": "ready", "download_kinds": ["magnet"],
            } for position, title in enumerate(titles, 1)])
            searched.data.update({"partial": True, "errors": [{"site_id": "btbtla", "message": "timeout"}]})
            with self.subTest(extra=extra), patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(missing=[{"season": 1, "episode": 9}], target_missing=True),
            ), patch("app.agent.episode_resource_actions.search_resources", return_value=searched):
                result = search_missing_episode_resources(arguments)
            candidates = result.references[0].value["candidates"] if result.references else []
            assert not any(item["title"] in old_titles for item in candidates)
            assert _recommend([candidate_item(item, item["position"]) for item in candidates]) == expected
            assert result.data["search"]["errors"] == searched.data["errors"]
            assert result.data["search"]["partial"] is True
            assert result.data["search"]["download_plan"]["auto_submit"] is False
            if not extra:
                assert result.references == [], "仅命中旧集时，不得生成候选卡引用"

    def test_minimum_2160_filters_1080_and_unknown_without_candidate_reference(self):
        arguments = missing_episode_resource_arguments(
            {
                "query": "示例剧",
                "season": 2,
                "episode": 3,
                "preference_overrides": {"minimum_resolution": "2160p"},
            }
        )
        searched = _search_result(
            items=[
                {
                    "result_id": "resolution-filter-1080-01",
                    "site_id": "nyaa",
                    "site_name": "Nyaa",
                    "title": "示例剧 S02E03 1080p",
                    "download_state": "ready",
                    "download_kinds": ["magnet"],
                },
                {
                    "result_id": "resolution-filter-unknown-01",
                    "site_id": "nyaa",
                    "site_name": "Nyaa",
                    "title": "示例剧 S02E03 WEB-DL",
                    "download_state": "ready",
                    "download_kinds": ["magnet"],
                },
            ]
        )
        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(
                    missing=[{"season": 2, "episode": 3}], target_missing=True
                ),
            ),
            patch(
                "app.agent.episode_resource_actions.search_resources",
                return_value=searched,
            ),
            patch(
                "app.agent.episode_resource_actions.get_indexer_service",
                return_value=Mock(result_store=None),
            ),
        ):
            result = search_missing_episode_resources(arguments)

        search = result.data["search"]
        self.assertEqual(search["recommendation"]["status"], "no_downloadable_candidate")
        self.assertIsNone(search["recommendation"]["selected"])
        self.assertEqual(search["recommendation"]["candidate_count"], 0)
        self.assertEqual(
            [item["result_id"] for item in search["items"]],
            ["resolution-filter-1080-01", "resolution-filter-unknown-01"],
        )
        self.assertTrue(all(not item["quality"]["eligible"] for item in search["items"]))
        self.assertIn("低于偏好中的最低分辨率", search["items"][0]["quality"]["warnings"])
        self.assertIn("分辨率未知，无法核对最低要求", search["items"][1]["quality"]["warnings"])
        self.assertFalse(
            any(reference.kind == "resource_candidates" for reference in result.references)
        )

    def test_minimum_2160_keeps_2160_candidate_reference_without_1080(self):
        arguments = missing_episode_resource_arguments(
            {
                "query": "示例剧",
                "season": 2,
                "episode": 3,
                "preference_overrides": {"minimum_resolution": "2160p"},
            }
        )
        searched = _search_result(
            items=[
                {
                    "result_id": "resolution-filter-1080-02",
                    "site_id": "nyaa",
                    "site_name": "Nyaa",
                    "title": "示例剧 S02E03 1080p",
                    "download_state": "ready",
                    "download_kinds": ["magnet"],
                },
                {
                    "result_id": "resolution-filter-2160-01",
                    "site_id": "nyaa",
                    "site_name": "Nyaa",
                    "title": "示例剧 S02E03 2160p",
                    "download_state": "ready",
                    "download_kinds": ["magnet"],
                },
            ]
        )
        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(
                    missing=[{"season": 2, "episode": 3}], target_missing=True
                ),
            ),
            patch(
                "app.agent.episode_resource_actions.search_resources",
                return_value=searched,
            ),
            patch(
                "app.agent.episode_resource_actions.get_indexer_service",
                return_value=Mock(result_store=None),
            ),
        ):
            result = search_missing_episode_resources(arguments)

        search = result.data["search"]
        self.assertEqual(
            search["recommendation"]["selected"]["result_id"],
            "resolution-filter-2160-01",
        )
        items_by_id = {item["result_id"]: item for item in search["items"]}
        self.assertFalse(items_by_id["resolution-filter-1080-02"]["quality"]["eligible"])
        self.assertTrue(items_by_id["resolution-filter-2160-01"]["quality"]["eligible"])
        references = [
            reference
            for reference in result.references
            if reference.kind == "resource_candidates"
        ]
        self.assertEqual(len(references), 1)
        self.assertEqual(
            [item["result_id"] for item in references[0].value["candidates"]],
            ["resolution-filter-2160-01"],
        )

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
            },
            refresh=True,
        )
        call = search.call_args.args[0]
        self.assertEqual(call["title"], "示例剧")
        self.assertEqual((call["season"], call["episode"]), (1, 150))

    def test_preserves_verified_state_when_indexer_search_is_unavailable(self):
        arguments = missing_episode_resource_arguments(
            {"query": "示例剧", "season": 2, "episode": 3}
        )
        searched = _search_result(ok=False, status="unavailable")
        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=_audit_result(missing=[{"season": 2, "episode": 3}], target_missing=True),
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
            (_audit_result(missing=[{"season": 2, "episode": 4}], target_missing=False), "not_missing"),
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
            [
                (call.args[0]["title"], call.args[0]["season"], call.args[0]["episode"])
                for call in search.call_args_list
            ],
            [("示例剧", 2, 3), ("示例剧", 2, 4), ("示例剧", 2, 5)],
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


class MultiWorkResourceTests(unittest.TestCase):
    def test_batch_validation_is_bounded_and_single_contract_is_reused(self):
        args = missing_season_resource_arguments({"items": [{"query": " 示例剧 ", "season": 1}, {"query": "示例剧", "season": 1}]})
        self.assertEqual(len(args["items"]), 1)
        self.assertIn("as_of", args["items"][0])
        for raw in ([], ["bad"], [{"query": "剧", "season": 0}], [{"query": "剧", "season": 1}] * 13):
            with self.subTest(raw=raw), self.assertRaises(AgentToolError):
                missing_season_resource_arguments({"items": raw})
        with self.assertRaises(AgentToolError):
            missing_season_resource_arguments({"items": [{"query": "剧", "season": 1}], "query": "另一部"})

    def test_batch_exposes_unproved_arc_candidate_without_preselecting_it(self):
        from types import SimpleNamespace

        from app.indexers.models import IndexerItem

        result_id = "gm-team-arc-review-001"
        title = "[GM-Team][国漫][凡人修仙传 慕兰之战][2026][16][GB][4K HEVC 10Bit]"
        stored = {
            result_id: IndexerItem(
                "mikan",
                "Mikan",
                title,
                result_id=result_id,
                download_state="ready",
                download_kinds=("magnet",),
                magnet="magnet:?xt=urn:btih:" + "a" * 40,
            )
        }
        service = SimpleNamespace(
            result_store=SimpleNamespace(get=lambda key: stored[key], restore=Mock())
        )
        arguments = missing_season_resource_arguments(
            {
                "items": [{"query": "凡人修仙传", "season": 1}],
                "as_of": "2026-08-01",
            }
        )
        audit = _audit_result(missing=[{"season": 1, "episode": 192}])
        audit.data.update(title="凡人修仙传", tmdb_id="44444")
        searched = _search_result(
            items=[
                {
                    "result_id": result_id,
                    "title": title,
                    "site_id": "mikan",
                    "site_name": "Mikan",
                    "download_state": "ready",
                    "download_kinds": ["magnet"],
                    "seeders": 40,
                }
            ]
        )

        with (
            patch(
                "app.agent.episode_resource_actions.audit_series_episodes",
                return_value=audit,
            ),
            patch(
                "app.agent.episode_resource_actions.search_resources",
                return_value=searched,
            ),
            patch(
                "app.agent.episode_resource_actions.get_indexer_service",
                return_value=service,
            ),
        ):
            result = search_missing_season_resources(arguments)

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["recommended_positions"], [])
        self.assertEqual(result.data["review_positions"], [1])
        self.assertEqual(result.data["groups"][0]["review_positions"], [1])
        self.assertEqual(
            result.data["groups"][0]["uncovered_episodes"][0]["reason"],
            "needs_review",
        )
        self.assertEqual(len(result.data["items"]), 1)
        self.assertEqual(len(result.references), 1)

    def test_cancelled_batch_does_not_start_audits_or_indexers(self):
        from app.agent.models import ToolContext
        arguments = missing_season_resource_arguments({"items": [{"query": f"测试作品{i}", "season": 1} for i in range(7)]})
        with patch("app.agent.episode_resource_actions.audit_series_episodes") as audit, patch(
            "app.agent.episode_resource_actions.search_resources"
        ) as search:
            result = search_missing_season_resources(arguments, context=ToolContext(cancelled=lambda: True))
        audit.assert_not_called()
        search.assert_not_called()
        self.assertEqual(len(result.data["groups"]), 7)
        self.assertTrue(all(group["status"] == "cancelled" for group in result.data["groups"]))
        self.assertFalse(result.references)

    def test_seven_work_batch_keeps_six_private_candidates_and_deduplicates_pack(self):
        from types import SimpleNamespace

        from app.agent.recent_resource_candidates import (
            restore_resource_candidate_reference,
            validate_safe_resource_snapshot,
        )
        from app.indexers.models import IndexerItem
        names = ["光阴之外", "择日飞升", "大主宰", "牧神记", "沧元图", "一斩苍穹", "东大高武学院"]
        stored = {}
        service = SimpleNamespace(result_store=SimpleNamespace(get=lambda key: stored[key], restore=Mock()))
        args = missing_season_resource_arguments({"items": [{"query": name, "season": 1} for name in names], "as_of": "2026-08-01"})
        def audit(arguments, *, refresh=False):
            self.assertTrue(refresh)
            result = _audit_result(missing=[{"season": 1, "episode": e} for e in ((8, 9) if arguments["query"] == names[0] else (8,))])
            result.data.update(title=arguments["query"], tmdb_id=str(1000 + names.index(arguments["query"])))
            return result
        def search(arguments, **_kwargs):
            name = next(name for name in names if arguments["title"].startswith(name))
            index = names.index(name)
            if index == 6:
                return _search_result(items=[])
            result_id = f"batch-resource-{index:02d}-0000"
            title = name + (".S01E08-09.1080p" if index == 0 else ".S01E08.1080p")
            stored[result_id] = IndexerItem("nyaa", "测试站点", title, result_id=result_id, download_state="ready", download_kinds=("magnet",), magnet="magnet:?xt=urn:btih:" + "a" * 40)
            return _search_result(items=[{"result_id": result_id, "title": title, "site_id": "nyaa", "site_name": "测试站点", "download_state": "ready", "download_kinds": ["magnet"]}])
        with patch("app.agent.episode_resource_actions.audit_series_episodes", side_effect=audit), patch(
            "app.agent.episode_resource_actions.search_resources", side_effect=search
        ), patch("app.agent.episode_resource_actions.get_indexer_service", return_value=service):
            result = search_missing_season_resources(args)
        self.assertEqual(len(result.data["groups"]), 7)
        self.assertEqual(result.data["recommended_positions"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(result.data["groups"][0]["positions"], [1])
        self.assertFalse(result.data["groups"][0]["uncovered_episodes"])
        self.assertEqual(result.data["groups"][-1]["positions"], [])
        self.assertEqual(result.data["groups"][-1]["uncovered_episodes"][0]["reason"], "no_match")
        reference = result.references[0]
        self.assertEqual(len(reference.value["_private_items"]), 6)
        with patch("app.indexers.runtime.get_indexer_service", return_value=service):
            restored = restore_resource_candidate_reference(reference.value)
        self.assertIsNotNone(validate_safe_resource_snapshot(restored))
        self.assertEqual(service.result_store.restore.call_count, 6)
        self.assertEqual([c["_verification_context"]["title"] for c in restored["candidates"]], names[:6])
        self.assertNotIn("magnet:", str(result.to_dict()))
        self.assertNotIn("_verification_context", str(result.to_dict()))


class MissingResourceInventoryFreshnessTests(unittest.TestCase):
    """资源入口运行真实审计/缓存/媒体清单链；仅外部客户端为替身。"""

    def setUp(self):
        from app.agent import episode_audit
        from app.clients.base import (
            SeriesCandidate,
            SeriesEpisodeInventory,
            SeriesSearchResult,
        )

        self.audit = episode_audit
        self.audit.reset_episode_audit_cache_for_tests()
        self.addCleanup(self.audit.reset_episode_audit_cache_for_tests)
        self.inventory = []
        self.identity = {"query": "Fixture Show", "tmdb_id": "12345", "season": 1}
        self.as_of = "2020-02-01"
        self.media = Mock()
        self.media.search_series_candidates.return_value = SeriesSearchResult(
            candidates=[SeriesCandidate(id="fixture", name="Fixture Show", year="2020", tmdb_id="12345")], total=1,
        )
        self.media.find_series_candidates_by_tmdb.return_value = self.media.search_series_candidates.return_value
        self.media.list_series_episode_inventory.side_effect = lambda *_a, **_kw: SeriesEpisodeInventory(
            episodes=list(self.inventory), total=len(self.inventory),
        )
        self.sources = [("jellyfin", "Jellyfin", "https://fixture.invalid", self.media)]
        tmdb = Mock()
        tmdb.detail.return_value = {
            "name": "Fixture Show", "first_air_date": "2020-01-01",
            "seasons": [{"season_number": 1, "episode_count": 3}],
        }
        tmdb.tv_season_detail.return_value = {
            "season_number": 1,
            "episodes": [{"episode_number": episode, "air_date": "2020-01-01"} for episode in range(1, 4)],
        }
        for target, options in (
            ("app.services._configured_media_sources", {"side_effect": lambda: list(self.sources)}),
            ("app.agent.episode_audit.TMDBClient", {"return_value": tmdb}),
            ("app.agent.episode_resource_actions.get_indexer_service", {"return_value": Mock()}),
        ):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch(
            "app.agent.episode_resource_actions.search_resources",
            side_effect=lambda *_a, **_kw: _search_result(status="empty", items=[]),
        )
        self.search = patcher.start()
        self.addCleanup(patcher.stop)

    def _warm_cache(self, mode, inventory):
        self.audit.reset_episode_audit_cache_for_tests()
        self.inventory[:] = inventory
        self.search.reset_mock()
        self.media.list_series_episode_inventory.reset_mock()
        arguments = {**self.identity, "as_of": self.as_of}
        if mode == "single":
            arguments["target_episode"] = 2
        return self.audit.audit_series_episodes(arguments)

    def _search(self, mode):
        if mode == "single":
            return search_missing_episode_resources(missing_episode_resource_arguments(
                {**self.identity, "as_of": self.as_of, "episode": 2}
            ))
        arguments = (
            # 重复的同剧同季输入仍只核对一次，三个缺集不各自重新扫描。
            {"items": [dict(self.identity), dict(self.identity)], "as_of": self.as_of}
            if mode == "batch" else {**self.identity, "as_of": self.as_of}
        )
        return search_missing_season_resources(missing_season_resource_arguments(arguments))

    def _assert_freshness(self, mode):
        complete = [(1, episode) for episode in range(1, 4)]
        for before, after in (([], complete), (complete, [])):
            with self.subTest(mode=mode, before=before):
                cached = self._warm_cache(mode, before)
                self.assertEqual(cached.status, "up_to_date" if before else "updates_available")
                self.inventory[:] = after
                result = self._search(mode)
                self.assertEqual(self.media.list_series_episode_inventory.call_count, 2)
                self.assertEqual(self.search.call_count, 0 if after else (1 if mode == "single" else 3))
                if mode == "batch":
                    self.assertEqual(len(result.data["groups"]), 1)
                    group = result.data["groups"][0]
                    self.assertEqual(group["status"], "not_missing" if after else "needs_review")
                    if not after:
                        self.assertEqual(group["missing_total"], 3)
                        self.assertEqual(group["remaining"], 0)
                elif after:
                    self.assertEqual(result.status, "not_missing")
                    self.assertFalse(result.data["verification"]["verified_missing"])
                else:
                    self.assertTrue(result.data["verification"]["verified_missing"])
                    if mode == "season":
                        self.assertEqual((result.data["missing_total"], result.data["processed"]), (3, 3))

    def test_single_search_refreshes_both_cached_inventory_verdicts(self):
        self._assert_freshness("single")

    def test_season_search_refreshes_once_for_all_missing_episodes(self):
        self._assert_freshness("season")

    def test_batch_search_refreshes_once_for_duplicate_series_and_all_episodes(self):
        self._assert_freshness("batch")

    def test_partial_media_source_failure_cannot_reuse_cached_up_to_date(self):
        for mode in ("single", "season", "batch"):
            with self.subTest(mode=mode):
                self.sources[:] = self.sources[:1]
                self._warm_cache(mode, [(1, episode) for episode in range(1, 4)])
                self.inventory.clear()
                failed = Mock()
                failed.search_series_candidates.side_effect = OSError("fixture unavailable")
                self.sources.append(("emby", "Emby", "https://failed.invalid", failed))
                result = self._search(mode)
                status = result.data["groups"][0]["status"] if mode == "batch" else result.status
                self.assertEqual(status, "inconclusive")
                self.assertFalse(result.ok)
                self.search.assert_not_called()
                self.assertEqual(self.media.list_series_episode_inventory.call_count, 2)
