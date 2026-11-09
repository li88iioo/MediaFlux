"""共用库存统计与原生 SDK 数据契约：桩替换来源读取，不访问真实服务。"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from app.sdk import library
from app.clients.base import SeriesCandidate, SeriesEpisodeInventory, SeriesSearchResult
from app.services import count_series_episodes as count_series_episode_inventory


class LibraryEpisodeCountArgumentsTests(unittest.TestCase):
    @patch("app.services.inspect_series_episode_sources", return_value=[])
    def test_normalizes_supported_arguments(self, inspect_sources):
        library.episodes("  ＴＶ师兄啊师兄？  ", tmdb_id=" 12345 ")
        inspect_sources.assert_called_once_with(
            "TV师兄啊师兄", tmdb_id="12345", max_episodes=2000, include_specials=False,
        )

    @patch("app.services.inspect_series_episode_sources", return_value=[])
    def test_accepts_named_library_scope(self, inspect_sources):
        library.episodes("师兄啊师兄", library_name="  美女库  ")
        inspect_sources.assert_called_once_with(
            "师兄啊师兄", tmdb_id="", library_name="美女库",
            max_episodes=2000, include_specials=False,
        )

    def test_rejects_invalid_arguments_before_reading_inventory(self):
        invalid: tuple[dict[str, Any], ...] = (
            {}, {"query": 1}, {"query": ""}, {"query": "剧名\n注入"},
            {"query": "剧" * 121}, {"query": "剧名", "tmdb_id": "abc"},
            {"query": "剧名", "tmdb_id": 123},
            {"query": "剧名", "tmdb_id": "１２３"},
            {"query": "剧名", "tmdb_id": "1" * 11},
            {"query": "剧名", "library_name": 123},
            {"query": "剧名", "library_name": " "},
            {"query": "剧名", "library_name": "库" * 81},
            {"query": "剧名", "library_name": "库\x00名"},
        )
        with patch("app.services.inspect_series_episode_sources") as inspect_sources:
            for arguments in invalid:
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    library.episodes(**arguments)
            invalid_signature: tuple[dict[str, Any], ...] = ({"query": "剧名", "extra": True},)
            for arguments in invalid_signature:
                with self.subTest(arguments=arguments), self.assertRaises(TypeError):
                    library.episodes(**arguments)
        inspect_sources.assert_not_called()


@pytest.mark.parametrize("query", ["", "同名别剧"])
def test_tmdb_inventory_uses_identity_without_requiring_title_search(query):
    client = Mock()
    client.find_series_candidates_by_tmdb.return_value = SeriesSearchResult(
        candidates=[SeriesCandidate("series-id", "目标剧", "2026", "123")], total=1,
    )
    client.search_series_candidates.side_effect = AssertionError("精确身份命中无需标题搜索")
    client.list_series_episode_inventory.return_value = SeriesEpisodeInventory(episodes=[(1, 1), (1, 3)], total=2)
    with patch("app.services._configured_media_sources", return_value=[("jellyfin", "Jellyfin", "", client)]):
        result = library.episodes(query=query, tmdb_id="123")
    assert result["status"] == "success"
    assert result["data"]["title"] == "目标剧"
    assert result["data"]["local_episode_count"] == 2
    assert result["data"]["seasons"][0]["episode_ranges"] == [[1, 1], [3, 3]]
    client.search_series_candidates.assert_not_called()
    client.close.assert_called_once_with()


@pytest.mark.parametrize("truncated", [False, True])
def test_tmdb_only_inventory_without_match_preserves_lookup_completeness(truncated):
    client = Mock()
    client.find_series_candidates_by_tmdb.return_value = SeriesSearchResult(truncated=truncated)
    with patch("app.services._configured_media_sources", return_value=[("jellyfin", "Jellyfin", "", client)]):
        result = library.episodes(tmdb_id="123")
    assert result["status"] == "not_found"
    assert result["data"]["sources"][0]["truncated"] is truncated
    assert "123" in result["summary"]
    client.search_series_candidates.assert_not_called()
    client.list_series_episode_inventory.assert_not_called()


class LibraryEpisodeCountSDKTests(unittest.TestCase):
    @patch("app.services.inspect_series_episode_sources")
    def test_counts_unique_local_episode_coordinates_and_seasons(self, inspect_sources):
        inspect_sources.return_value = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "status": "unmapped",
                "selected": {"name": "师兄啊师兄", "year": "2023"},
                "episodes": [(1, 1), (1, 2), (2, 1)],
                "ignored_specials": 2,
                "ignored_unknown": 0,
                "truncated": False,
            },
            {
                "server_type": "emby",
                "server_name": "Emby",
                "status": "ready",
                "selected": {"name": "师兄啊师兄", "year": "2023"},
                "episodes": [(1, 1), (2, 1), (2, 2)],
                "ignored_specials": 0,
                "ignored_unknown": 0,
                "truncated": False,
            },
        ]

        result = library.episodes(query="师兄啊师兄", tmdb_id="")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["local_episode_count"], 4)
        self.assertEqual(result["data"]["season_count"], 2)
        self.assertEqual(
            result["data"]["seasons"],
            [
                {
                    "season": 1,
                    "count": 2,
                    "first_episode": 1,
                    "last_episode": 2,
                    "episode_ranges": [[1, 2]],
                },
                {
                    "season": 2,
                    "count": 2,
                    "first_episode": 1,
                    "last_episode": 2,
                    "episode_ranges": [[1, 2]],
                },
            ],
        )
        self.assertEqual(result["data"]["matched_source_count"], 2)
        self.assertIs(result["data"]["inventory_complete"], True)
        self.assertIn("本地收录 4 集", result["summary"])
        inspect_sources.assert_called_once_with(
            "师兄啊师兄",
            tmdb_id="",
            max_episodes=2000,
            include_specials=False,
        )

    @patch("app.services.inspect_series_episode_sources")
    def test_limits_count_to_named_library_without_exposing_internal_id(
        self, inspect_sources
    ):
        inspect_sources.return_value = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "library_name": "美女库",
                "status": "ready",
                "selected": {"name": "师兄啊师兄", "year": "2023"},
                "episodes": [(1, 1), (1, 2)],
                "truncated": False,
            }
        ]

        result = library.episodes(query="师兄啊师兄", tmdb_id="", library_name="美女库")

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["library_name"], "美女库")
        self.assertIs(result["data"]["inventory_complete"], True)
        inspect_sources.assert_called_once_with(
            "师兄啊师兄",
            tmdb_id="",
            max_episodes=2000,
            include_specials=False,
            library_name="美女库",
        )
        self.assertNotIn("library-id", repr(result))

    @patch("app.services.inspect_series_episode_sources")
    def test_does_not_report_zero_when_media_server_is_not_configured(
        self, inspect_sources
    ):
        inspect_sources.return_value = []

        result = library.episodes(query="师兄啊师兄", tmdb_id="")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_configured")
        self.assertIsNone(result["data"]["local_episode_count"])
        self.assertIs(result["data"]["inventory_complete"], False)
        self.assertNotIn("0 集", result["summary"])

    @patch("app.services.inspect_series_episode_sources")
    def test_not_found_returns_candidates_without_false_count(self, inspect_sources):
        inspect_sources.return_value = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "status": "not_found",
                "candidates": [
                    {"name": "师兄啊师兄 年番", "year": "2024", "id": "private"}
                ],
                "episodes": [],
            }
        ]

        result = library.episodes(query="师兄啊师兄", tmdb_id="")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_found")
        self.assertIsNone(result["data"]["local_episode_count"])
        self.assertIs(result["data"]["inventory_complete"], False)
        self.assertEqual(
            result["data"]["candidates"], [{"name": "师兄啊师兄 年番", "year": "2024"}]
        )
        self.assertNotIn("id", result["data"]["candidates"][0])

    @patch("app.services.inspect_series_episode_sources")
    def test_marks_unnumbered_or_truncated_inventory_as_partial(self, inspect_sources):
        inspect_sources.return_value = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "status": "ready",
                "selected": {"name": "测试剧", "year": "2026"},
                "episodes": [(1, 1), (1, 2)],
                "ignored_unknown": 1,
                "truncated": True,
            }
        ]

        result = library.episodes(query="测试剧", tmdb_id="")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["local_episode_count"], 2)
        self.assertIs(result["data"]["inventory_complete"], False)
        self.assertEqual(
            result["data"]["seasons"],
            [
                {
                    "season": 1,
                    "count": 2,
                    "first_episode": 1,
                    "last_episode": 2,
                    "episode_ranges": [[1, 2]],
                }
            ],
        )
        self.assertIn("下限", result["suggestions"][0])

    @patch("app.services.inspect_series_episode_sources")
    def test_preserves_normalized_input_and_public_inventory_facts(
        self, inspect_sources
    ):
        inspect_sources.return_value = [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "library_name": "剧集库",
                "status": "ready",
                "selected": {"name": "师兄啊师兄", "year": "2023", "id": "private"},
                "episodes": [(1, 1), (1, 2)],
                "ignored_specials": 3,
                "ignored_unknown": 0,
                "truncated": False,
            }
        ]
        result = library.episodes(
            "  师兄啊师兄  ", tmdb_id=" 12345 ", library_name=" 剧集库 ",
        )
        payload = json.loads(json.dumps(result))

        inspect_sources.assert_called_once_with(
            "师兄啊师兄",
            tmdb_id="12345",
            max_episodes=2000,
            include_specials=False,
            library_name="剧集库",
        )
        legacy_data = {
            "query": "师兄啊师兄",
            "library_name": "剧集库",
            "title": "师兄啊师兄",
            "year": "2023",
            "local_episode_count": 2,
            "season_count": 1,
            "matched_source_count": 1,
            "source_count": 1,
            "ignored_specials": 3,
            "ignored_unknown": 0,
            "truncated": False,
            "count_definition": "Jellyfin / Emby 中已识别季号和集号的本地普通集；跨媒体服务器重复季集号只计一次。",
        }
        self.assertEqual(
            {key: payload["data"][key] for key in legacy_data}, legacy_data
        )
        self.assertEqual(
            set(payload),
            {"ok", "status", "summary", "data", "suggestions", "error"},
        )
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["error"], "")
        self.assertIn("本地收录 2 集", payload["summary"])
        self.assertIn("特别篇", payload["suggestions"][0])
        self.assertIs(payload["data"]["inventory_complete"], True)
        self.assertEqual(
            payload["data"]["seasons"],
            [
                {
                    "season": 1,
                    "count": 2,
                    "first_episode": 1,
                    "last_episode": 2,
                    "episode_ranges": [[1, 2]],
                }
            ],
        )
        legacy_source = {
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "library_name": "剧集库",
            "status": "ready",
            "title": "师兄啊师兄",
            "year": "2023",
            "local_episode_count": 2,
            "season_count": 1,
            "truncated": False,
            "ignored_specials": 3,
            "ignored_unknown": 0,
        }
        self.assertEqual(len(payload["data"]["sources"]), 1)
        self.assertEqual(
            {key: payload["data"]["sources"][0][key] for key in legacy_source},
            legacy_source,
        )
        self.assertNotIn("private", repr(payload))


# 以下为相同公共契约的参数化边界用例；共享输入构造，不重复生产计算逻辑。
def inventory_source(episodes=(), **fields):
    return {
        "server_type": "jellyfin", "server_name": "Jellyfin", "status": "ready",
        "selected": {"name": "测试剧", "year": "2026"}, "episodes": list(episodes),
        **fields,
    }


def test_shared_inventory_count_runs_without_agent_runtime():
    script = textwrap.dedent('''
        import json
        import sys
        import tests
        from unittest.mock import patch

        blocked = ("app.agent.domains", "app.agent.kernel")
        with patch.dict(sys.modules, dict.fromkeys(blocked)), patch(
            "socket.socket.connect", side_effect=AssertionError("禁止真实外联"),
        ):
            from app.services import count_series_episodes

            with patch("app.services.inspect_series_episode_sources", return_value=json.load(sys.stdin)):
                result = count_series_episodes("测试剧", tmdb_id="123", library_name="剧集库")
            assert not any(module is not None and (name == prefix or name.startswith(prefix + "."))
                           for name, module in sys.modules.items() for prefix in blocked)
            print(json.dumps(result))
    ''')
    sources = [
        inventory_source([(1, 1), (1, 3), (2, 1)], status="unmapped"),
        inventory_source([(1, 1), (1, 2), (2, 1)], server_type="emby", server_name="Emby"),
    ]
    completed = subprocess.run(
        [sys.executable, "-c", script], input=json.dumps(sources),
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["ok"] and result["status"] == "success"
    assert result["data"]["inventory_complete"] is True
    assert result["data"]["local_episode_count"] == 4
    assert result["data"]["library_name"] == "剧集库"
    assert [season["episode_ranges"] for season in result["data"]["seasons"]] == [[[1, 3]], [[1, 1]]]
    assert "evidence" not in result
    assert "library.audit_episodes" not in completed.stdout


@pytest.fixture
def inspect_inventory(monkeypatch):
    def inspect(sources):
        monkeypatch.setattr("app.services.inspect_series_episode_sources", lambda *a, **kw: sources)
        return count_series_episode_inventory("测试剧")
    return inspect


@pytest.mark.parametrize("first_id, first_year, second_id, second_year", [
    ("100", "2005", "200", "2001"),
    ("100", "2005", "200", "2005"),
    ("100", "", "200", ""),
    ("", "2005", "", "2001"),
    ("100", "2005", "", "2001"),
])
def test_cross_source_inventory_identity_conflict_is_ambiguous(first_id, first_year, second_id, second_year):
    sources = []
    for server, tmdb_id, year, episodes in (
        ("jellyfin", first_id, first_year, [(1, 1), (1, 2)]),
        ("emby", second_id, second_year, [(1, 1), (1, 3)]),
    ):
        client = Mock()
        client.search_series_candidates.return_value = SeriesSearchResult(
            candidates=[SeriesCandidate(f"private-{server}-series", "测试剧", year, tmdb_id)], total=1,
        )
        client.list_series_episode_inventory.return_value = SeriesEpisodeInventory(episodes=episodes, total=2)
        sources.append((server, server, "", client))
    with patch("app.services._configured_media_sources", return_value=sources):
        result = library.episodes("测试剧")

    assert not result["ok"] and result["status"] == result["error"] == "ambiguous"
    data = result["data"]
    assert data["local_episode_count"] is None and data["inventory_complete"] is False
    assert data["matched_source_count"] == data["source_count"] == 2
    assert data["candidates"] == [
        {"name": "测试剧", "year": year, **({"tmdb_id": tmdb_id} if tmdb_id else {})}
        for tmdb_id, year in ((first_id, first_year), (second_id, second_year))
    ]
    assert [(source["server_type"], source["tmdb_id"], source["year"], source["local_episode_count"])
            for source in data["sources"]] == [
        ("jellyfin", first_id, first_year, 2), ("emby", second_id, second_year, 2),
    ]
    assert "seasons" not in data and "本地收录" not in result["summary"]
    assert "TMDB ID" in result["suggestions"][0] and "private-" not in repr(result)


@pytest.mark.parametrize("first, second, expected_status", [
    ({"name": "测试剧", "year": "2026", "tmdb_id": "123"},
     {"name": "测试剧", "year": "2025", "tmdb_id": "123"}, "success"),
    ({"name": "测试剧", "year": "2026", "tmdb_id": "123"},
     {"name": "Another Title", "year": "", "tmdb_id": "123"}, "success"),
    ({"name": "测试剧", "year": "2026", "tmdb_id": "123"},
     {"name": "测试剧", "year": "2026"}, "success"),
    ({"name": "测试剧", "year": "2026", "tmdb_id": "123"},
     {"name": "测试剧", "year": ""}, "success"),
    ({"name": "Ｔｅｓｔ·剧", "year": "2026"},
     {"name": "test 剧", "year": ""}, "success"),
    ({"name": "测试剧", "year": ""}, {"name": "测试剧", "year": None}, "success"),
    ({"name": "测试剧", "year": "2026"},
     {"name": "另一部剧", "year": "2026"}, "ambiguous"),
])
def test_cross_source_inventory_uses_provider_id_then_title_year(inspect_inventory, first, second, expected_status):
    result = inspect_inventory([
        inventory_source([(1, 1), (1, 2)], selected=first),
        inventory_source([(1, 1), (1, 3)], selected=second, server_type="emby", server_name="Emby"),
    ])
    assert result["status"] == expected_status
    if expected_status == "ambiguous":
        assert not result["ok"] and result["data"]["local_episode_count"] is None
        assert result["data"]["inventory_complete"] is False
        assert [candidate["name"] for candidate in result["data"]["candidates"]] == [first["name"], second["name"]]
    else:
        assert result["ok"] and result["data"]["inventory_complete"] is True
        assert result["data"]["local_episode_count"] == 3
        assert result["data"]["seasons"][0]["episode_ranges"] == [[1, 3]]


@pytest.mark.parametrize("inventories, expected", [
    ([[(1, 12), (1, 4), (1, 1), (1, 2), (1, 5), (1, 12), (1, 9), (1, 11)]],
     [(1, 7, [[1, 2], [4, 5], [9, 9], [11, 12]])]),
    ([[(2, 4), (1, 5), (1, 1), (1, 2), (1, 5), (2, 1)], [(1, 2), (1, 3), (1, 5), (2, 1), (2, 3), (2, 3)]],
     [(1, 4, [[1, 3], [5, 5]]), (2, 3, [[1, 1], [3, 4]])]),
])
def test_observed_ranges_preserve_gaps_seasons_and_deduplicate(inspect_inventory, inventories, expected):
    result = inspect_inventory([inventory_source(episodes, server_name=f"source-{i}") for i, episodes in enumerate(inventories)])
    assert result["ok"] and result["status"] == "success"
    assert result["data"]["inventory_complete"] is True
    assert result["data"]["local_episode_count"] == 7
    assert result["data"]["season_count"] == len(expected)
    assert result["data"]["matched_source_count"] == len(inventories)
    assert result["data"]["seasons"] == [
        {"season": season, "count": count, "first_episode": ranges[0][0], "last_episode": ranges[-1][1], "episode_ranges": ranges}
        for season, count, ranges in expected
    ]
    assert [s["local_episode_count"] for s in result["data"]["sources"]] == ([7] if len(inventories) == 1 else [5, 5])


@pytest.mark.parametrize("status", ["ready", "unmapped"])
@pytest.mark.parametrize("defect", [{"truncated": True}, {"ignored_unknown": 1}])
@pytest.mark.parametrize("other_complete", [False, True])
def test_incomplete_inventory_keeps_ranges_and_lower_bound(inspect_inventory, status, defect, other_complete):
    sources = [inventory_source([(1, 4), (1, 1), (1, 2), (1, 4)], status=status, **defect)]
    if other_complete:
        sources.append(inventory_source([(1, 1)]))
    result = inspect_inventory(sources)
    assert result["ok"] and result["status"] == "partial"
    assert result["data"]["inventory_complete"] is False
    assert result["data"]["local_episode_count"] == 3
    assert result["data"]["matched_source_count"] == len(sources)
    assert result["data"]["truncated"] == defect.get("truncated", False)
    assert result["data"]["ignored_unknown"] == defect.get("ignored_unknown", 0)
    assert result["data"]["seasons"][0]["episode_ranges"] == [[1, 2], [4, 4]]
    assert "下限" in result["suggestions"][0]


@pytest.mark.parametrize("unresolved", [
    {"status": status} for status in ("unavailable", "ambiguous", "conflict", "library_ambiguous", "library_not_found")
] + [{"status": "not_found", "truncated": True}])
@pytest.mark.parametrize("unresolved_first", [False, True])
def test_unresolved_source_cannot_prove_absence(inspect_inventory, unresolved, unresolved_first):
    sources = [inventory_source([(1, 1), (1, 3), (2, 2)]), inventory_source(**unresolved)]
    result = inspect_inventory(list(reversed(sources)) if unresolved_first else sources)
    assert result["ok"] and result["status"] == "partial"
    assert result["data"]["inventory_complete"] is False
    assert result["data"]["local_episode_count"] == 3
    assert result["data"]["matched_source_count"] == 1
    assert result["data"]["source_count"] == 2
    assert [(s["season"], s["episode_ranges"]) for s in result["data"]["seasons"]] == [(1, [[1, 1], [3, 3]]), (2, [[2, 2]])]


@pytest.mark.parametrize("status", ["ready", "unmapped"])
@pytest.mark.parametrize("not_found_first", [False, True])
def test_not_found_other_source_does_not_hide_complete_inventory(inspect_inventory, status, not_found_first):
    sources = [inventory_source([(1, 1), (1, 3)], status=status, ignored_specials=2), inventory_source(status="not_found")]
    result = inspect_inventory(list(reversed(sources)) if not_found_first else sources)
    assert result["ok"] and result["status"] == "success"
    assert result["data"]["inventory_complete"] is True
    assert result["data"]["local_episode_count"] == 2
    assert result["data"]["matched_source_count"] == 1
    assert result["data"]["source_count"] == 2
    assert result["data"]["ignored_specials"] == 2
    assert result["data"]["seasons"][0]["episode_ranges"] == [[1, 1], [3, 3]]


@pytest.mark.parametrize("status", ["ready", "unmapped"])
def test_selected_empty_inventory_is_not_unmatched(inspect_inventory, status):
    result = inspect_inventory([inventory_source(status=status)])
    assert result["ok"] and result["status"] == "success"
    assert result["data"]["inventory_complete"] is True
    assert result["data"]["title"] == "测试剧"
    assert result["data"]["local_episode_count"] == result["data"]["season_count"] == 0
    assert result["data"]["seasons"] == []
    assert result["data"]["matched_source_count"] == 1


@pytest.mark.parametrize("status, expected_status", [
    ("unavailable", "unavailable"), ("ambiguous", "ambiguous"), ("conflict", "ambiguous"),
    ("library_not_found", "library_not_found"), ("library_ambiguous", "library_ambiguous"),
])
def test_unmatched_inventory_is_never_complete_or_zero(inspect_inventory, status, expected_status):
    result = inspect_inventory([inventory_source(status=status)])
    assert not result["ok"] and result["status"] == expected_status
    assert result["data"]["inventory_complete"] is False
    assert result["data"]["local_episode_count"] is None
    assert result["data"]["matched_source_count"] == 0
    assert result["data"]["source_count"] == 1
