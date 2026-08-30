"""剧集完整性审计的映射、播出日期、截断和脱敏测试。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent.episode_audit import (
    audit_series_episodes,
    invalidate_episode_audit_cache,
    reset_episode_audit_cache_for_tests,
)
from app.clients.base import SeriesCandidate, SeriesEpisodeInventory, SeriesSearchResult
from app.discovery.models import ProviderNotConfigured, ProviderUnavailable
from app.services import _series_source_payload


class _FakeTMDB:
    details = {"name": "The Show", "first_air_date": "2026-01-01", "seasons": [{"season_number": 1}, {"season_number": 2}]}
    seasons = {
        1: {"episodes": [
            {"episode_number": 1, "air_date": "2026-07-01"},
            {"episode_number": 2, "air_date": "2026-07-08"},
            {"episode_number": 3, "air_date": "2026-08-08"},
            {"episode_number": 4, "air_date": None},
        ]},
        2: {"episodes": [{"episode_number": 1, "air_date": "2026-07-15"}]},
    }

    def search(self, title, year, media_type):
        return [{
            "id": 12345,
            "name": "The Show",
            "original_name": "The Show",
            "first_air_date": "2026-01-01",
        }]

    def detail(self, tmdb_id, media_type):
        return self.details

    def tv_season_detail(self, tmdb_id, season_number):
        return self.seasons[season_number]


def _ready(episodes, *, server="Jellyfin", tmdb_id="12345", truncated=False):
    return {
        "server_type": server.casefold(),
        "server_name": server,
        "status": "ready",
        "candidates": [{"name": "The Show", "year": "2026", "tmdb_id": tmdb_id}],
        "selected": {"name": "The Show", "year": "2026", "tmdb_id": tmdb_id},
        "episodes": episodes,
        "local_total": len(episodes),
        "truncated": truncated,
        "ignored_specials": 1,
        "ignored_unknown": 0,
        "error": "",
    }


class _FakeMediaClient:
    def __init__(self, title_search, *, provider_search=None, inventories=None, folders=None):
        self.title_search = title_search
        self.provider_search = provider_search or SeriesSearchResult()
        self.inventories = inventories or {}
        self.folders = list(folders or [])
        self.parent_ids: list[str] = []

    def list_virtual_folders(self):
        return list(self.folders)

    def search_series_candidates(self, query, limit=6, *, parent_id=""):
        self.parent_ids.append(parent_id)
        return self.title_search

    def find_series_candidates_by_tmdb(self, tmdb_id, limit=20, *, parent_id=""):
        self.parent_ids.append(parent_id)
        return self.provider_search

    def list_series_episode_inventory(
        self, series_id, *, max_episodes=2000, page_size=200, include_specials=False
    ):
        return self.inventories[series_id]


class SeriesSourcePayloadTests(unittest.TestCase):
    def test_unmapped_unique_local_series_still_reads_jellyfin_inventory(self):
        candidate = SeriesCandidate(id="series-1", name="The Show", year="2026")
        client = _FakeMediaClient(
            SeriesSearchResult(candidates=[candidate], total=1),
            inventories={
                "series-1": SeriesEpisodeInventory(
                    episodes=[(1, 1), (1, 2)], total=2
                )
            },
        )
        result = _series_source_payload(
            "jellyfin", "Jellyfin", client, "The Show", "", 2000
        )
        self.assertEqual(result["status"], "unmapped")
        self.assertEqual(result["episodes"], [(1, 1), (1, 2)])
        self.assertEqual(result["mapping"]["status"], "unmapped")

    def test_explicit_tmdb_id_can_use_title_inventory_when_provider_id_is_missing(self):
        candidate = SeriesCandidate(id="series-1", name="The Show", year="2026")
        client = _FakeMediaClient(
            SeriesSearchResult(candidates=[candidate], total=1),
            inventories={
                "series-1": SeriesEpisodeInventory(episodes=[(1, 1)], total=1)
            },
        )
        result = _series_source_payload(
            "jellyfin", "Jellyfin", client, "The Show", "12345", 2000
        )
        self.assertEqual(result["status"], "unmapped")
        self.assertEqual(result["episodes"], [(1, 1)])

    def test_named_library_scope_uses_parent_id_but_only_returns_public_name(self):
        candidate = SeriesCandidate(id="series-1", name="The Show", year="2026")
        client = _FakeMediaClient(
            SeriesSearchResult(candidates=[candidate], total=1),
            inventories={
                "series-1": SeriesEpisodeInventory(episodes=[(1, 1)], total=1)
            },
            folders=[
                {"id": "private-library-id", "name": "美女库"},
                {"id": "other-library-id", "name": "电视剧"},
            ],
        )

        result = _series_source_payload(
            "jellyfin", "Jellyfin", client, "The Show", "", 2000,
            library_name="美女库",
        )

        self.assertEqual(result["status"], "unmapped")
        self.assertEqual(result["library_name"], "美女库")
        self.assertEqual(client.parent_ids, ["private-library-id"])
        self.assertNotIn("private-library-id", repr(result))

    def test_named_library_scope_fails_closed_without_searching_series(self):
        for folders, requested, expected in (
            ([], "动漫", "library_not_found"),
            ([
                {"id": "library-1", "name": "动画电影"},
                {"id": "library-2", "name": "动画剧集"},
            ], "动画", "library_ambiguous"),
        ):
            client = _FakeMediaClient(SeriesSearchResult(), folders=folders)
            with self.subTest(expected=expected):
                result = _series_source_payload(
                    "jellyfin", "Jellyfin", client, "The Show", "", 2000,
                    library_name=requested,
                )
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["library_name"], requested)
            self.assertEqual(client.parent_ids, [])
            self.assertNotIn("library-1", repr(result))
            self.assertNotIn("library-2", repr(result))

    def test_provider_mapped_duplicates_are_merged(self):
        first = SeriesCandidate(
            id="series-1", name="The Show", year="2026", tmdb_id="12345"
        )
        second = SeriesCandidate(
            id="series-2", name="The Show", year="2026", tmdb_id="12345"
        )
        client = _FakeMediaClient(
            SeriesSearchResult(candidates=[first], total=1),
            provider_search=SeriesSearchResult(
                candidates=[first, second], total=2, truncated=False
            ),
            inventories={
                "series-1": SeriesEpisodeInventory(episodes=[(1, 1)], total=1),
                "series-2": SeriesEpisodeInventory(episodes=[(1, 2)], total=1),
            },
        )
        result = _series_source_payload(
            "jellyfin", "Jellyfin", client, "The Show", "12345", 2000
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["episodes"], [(1, 1), (1, 2)])



class EpisodeAuditTests(unittest.TestCase):
    def setUp(self):
        reset_episode_audit_cache_for_tests()
        self.arguments = {
            "query": "The Show", "tmdb_id": "", "season": None, "as_of": "2026-08-01"
        }

    def _run(self, sources):
        with patch("app.agent.episode_audit.inspect_series_episode_sources", return_value=sources), patch(
            "app.agent.episode_audit.TMDBClient", return_value=_FakeTMDB()
        ):
            return audit_series_episodes(dict(self.arguments))

    def test_reports_aired_missing_episodes_and_ignores_future_unknown_and_specials(self):
        result = self._run([_ready([(1, 1)])])
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "updates_available")
        self.assertEqual(result.data["expected_aired"], 3)
        self.assertEqual(result.data["missing_count"], 2)
        self.assertEqual(result.data["missing_sample"], [
            {"season": 1, "episode": 2}, {"season": 2, "episode": 1}
        ])
        self.assertEqual(result.data["future_episode_count"], 1)
        self.assertEqual(result.data["unknown_air_date_count"], 1)
        self.assertEqual(result.data["ignored_specials"], 1)
        self.assertEqual(result.data["resource_followups"], [
            {
                "tool": "library.search_missing_episode_resources",
                "label": "搜索 S01E02 资源",
                "episode_label": "S01E02",
                "arguments": {
                    "query": "The Show",
                    "tmdb_id": "12345",
                    "season": 1,
                    "episode": 2,
                    "as_of": "2026-08-01",
                },
            },
            {
                "tool": "library.search_missing_episode_resources",
                "label": "搜索 S02E01 资源",
                "episode_label": "S02E01",
                "arguments": {
                    "query": "The Show",
                    "tmdb_id": "12345",
                    "season": 2,
                    "episode": 1,
                    "as_of": "2026-08-01",
                },
            },
        ])
        self.assertFalse(result.data["resource_followups_truncated"])

    def test_exact_target_beyond_missing_sample_is_still_verified(self):
        fake = _FakeTMDB()
        fake.details = {
            "name": "The Show",
            "first_air_date": "2026-01-01",
            "seasons": [{"season_number": 1}],
        }
        fake.seasons = {
            1: {
                "episodes": [
                    {"episode_number": number, "air_date": "2026-07-01"}
                    for number in range(1, 151)
                ]
            }
        }
        arguments = {
            **self.arguments,
            "season": 1,
            "target_episode": 150,
            "library_name": "美女库",
        }
        source = {**_ready([(1, number) for number in range(1, 150)]), "library_name": "美女库"}
        with patch(
            "app.agent.episode_audit.inspect_series_episode_sources",
            return_value=[source],
        ) as inspect, patch("app.agent.episode_audit.TMDBClient", return_value=fake):
            result = audit_series_episodes(arguments)

        self.assertEqual(result.status, "updates_available")
        self.assertTrue(result.data["missing_sample_truncated"] is False)
        self.assertTrue(result.data["target_aired"] is True)
        self.assertTrue(result.data["target_local"] is False)
        self.assertTrue(result.data["target_missing"] is True)
        self.assertEqual(
            result.data["resource_followups"][0]["episode_label"], "S01E150"
        )
        self.assertEqual(
            result.data["resource_followups"][0]["arguments"]["library_name"],
            "美女库",
        )
        inspect.assert_called_once_with(
            "The Show", tmdb_id="", max_episodes=2000, library_name="美女库"
        )

    def test_resource_followups_are_bounded_and_only_cover_verified_sample(self):
        fake = _FakeTMDB()
        fake.details = {"name": "The Show", "first_air_date": "2026-01-01", "seasons": [{"season_number": 1}]}
        fake.seasons = {
            1: {
                "episodes": [
                    {"episode_number": number, "air_date": "2026-07-01"}
                    for number in range(1, 16)
                ]
            }
        }
        with patch(
            "app.agent.episode_audit.inspect_series_episode_sources",
            return_value=[_ready([])],
        ), patch("app.agent.episode_audit.TMDBClient", return_value=fake):
            result = audit_series_episodes(dict(self.arguments))

        self.assertEqual(result.status, "updates_available")
        self.assertEqual(len(result.data["missing_sample"]), 15)
        self.assertEqual(len(result.data["resource_followups"]), 12)
        self.assertTrue(result.data["resource_followups_truncated"])
        self.assertEqual(
            [item["episode_label"] for item in result.data["resource_followups"]],
            [f"S01E{number:02d}" for number in range(1, 13)],
        )

    def test_up_to_date_and_explicit_season_scope(self):
        self.arguments["season"] = 1
        result = self._run([_ready([(1, 1), (1, 2)])])
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "up_to_date")
        self.assertEqual(result.data["expected_aired"], 2)
        self.assertEqual(result.data["missing_count"], 0)
        self.assertNotIn("resource_followups", result.data)

    def test_unmapped_local_inventory_uses_strict_title_year_tmdb_fallback(self):
        source = {
            **_ready([(1, 1)]),
            "status": "unmapped",
            "selected": {"name": "The Show", "year": "2026", "tmdb_id": ""},
            "mapping": {"status": "unmapped", "tmdb_id": ""},
        }
        result = self._run([source])
        self.assertEqual(result.status, "updates_available")
        self.assertEqual(result.data["tmdb_id"], "12345")
        self.assertEqual(result.data["mapping_status"], "title_year_fallback")
        self.assertEqual(
            result.data["sources"][0]["mapping"]["status"],
            "title_year_fallback",
        )
        self.assertEqual(result.data["local_episode_count"], 1)

    def test_unmapped_title_year_fallback_refuses_ambiguous_tmdb_results(self):
        source = {
            **_ready([(1, 1)]),
            "status": "unmapped",
            "selected": {"name": "The Show", "year": "2026", "tmdb_id": ""},
        }
        fake = _FakeTMDB()
        fake.search = lambda *_args: [
            {"id": 1, "name": "The Show", "first_air_date": "2026-01-01"},
            {"id": 2, "name": "The Show", "first_air_date": "2026-05-01"},
        ]
        with patch(
            "app.agent.episode_audit.inspect_series_episode_sources",
            return_value=[source],
        ), patch("app.agent.episode_audit.TMDBClient", return_value=fake):
            result = audit_series_episodes(dict(self.arguments))
        self.assertEqual(result.status, "ambiguous")

    def test_explicit_tmdb_id_binds_unmapped_inventory_only_when_identity_matches(self):
        self.arguments["tmdb_id"] = "12345"
        source = {
            **_ready([(1, 1)]),
            "status": "unmapped",
            "selected": {"name": "The Show", "year": "2026", "tmdb_id": ""},
        }
        result = self._run([source])
        self.assertEqual(result.status, "updates_available")
        self.assertEqual(result.data["local_episode_count"], 1)
        self.assertEqual(result.data["mapping_status"], "explicit_tmdb_id")

        reset_episode_audit_cache_for_tests()
        source["status"] = "unmapped"
        source["mapping"] = {"status": "unmapped", "tmdb_id": ""}
        source["selected"] = {"name": "Another Show", "year": "2026", "tmdb_id": ""}
        mismatch = self._run([source])
        self.assertEqual(mismatch.status, "not_found")

    def test_matching_unmapped_duplicate_is_merged_with_provider_mapped_source(self):
        mapped = _ready([(1, 1)])
        unmapped = {
            **_ready([(1, 2)], server="Emby"),
            "status": "unmapped",
            "selected": {"name": "The Show", "year": "2026", "tmdb_id": ""},
        }
        result = self._run([mapped, unmapped])
        self.assertEqual(result.data["local_episode_count"], 2)
        self.assertEqual(result.data["sources"][1]["status"], "ready")

    def test_ambiguous_unmapped_conflict_and_partial_are_not_claimed_complete(self):
        ambiguous = {**_ready([]), "status": "ambiguous", "selected": None}
        self.assertEqual(self._run([ambiguous]).status, "ambiguous")
        reset_episode_audit_cache_for_tests()
        unmapped = {**_ready([]), "status": "unmapped", "selected": {"name": "The Show", "year": "", "tmdb_id": ""}}
        self.assertEqual(self._run([unmapped]).status, "unmapped")
        reset_episode_audit_cache_for_tests()
        self.assertEqual(self._run([_ready([], tmdb_id="1"), _ready([], server="Emby", tmdb_id="2")]).status, "conflict")
        reset_episode_audit_cache_for_tests()
        unavailable = {**_ready([]), "status": "unavailable", "selected": None, "error": "secret http://private"}
        partial = self._run([_ready([(1, 1), (1, 2), (2, 1)]), unavailable])
        self.assertEqual(partial.status, "inconclusive")
        self.assertNotIn("resource_followups", partial.data)
        self.assertNotIn("private", str(partial.to_dict()))

    def test_truncated_local_inventory_is_inconclusive(self):
        result = self._run([_ready([(1, 1)], truncated=True)])
        self.assertEqual(result.status, "inconclusive")
        self.assertFalse(result.ok)

    def test_tmdb_configuration_and_upstream_errors_are_safe(self):
        for error, status in ((ProviderNotConfigured("private secret"), "not_configured"),
                              (ProviderUnavailable("private secret"), "unavailable")):
            reset_episode_audit_cache_for_tests()
            fake = _FakeTMDB()
            fake.detail = lambda *_args, error=error: (_ for _ in ()).throw(error)
            with patch("app.agent.episode_audit.inspect_series_episode_sources", return_value=[_ready([])]), patch(
                "app.agent.episode_audit.TMDBClient", return_value=fake
            ):
                result = audit_series_episodes(dict(self.arguments))
            self.assertEqual(result.status, status)
            self.assertNotIn("private secret", str(result.to_dict()))

    def test_invalidate_cache_evicts_only_matching_normalized_key(self):
        other = {**self.arguments, "season": 2}
        with patch(
            "app.agent.episode_audit.inspect_series_episode_sources",
            return_value=[_ready([])],
        ) as inspect, patch("app.agent.episode_audit.TMDBClient", return_value=_FakeTMDB()):
            audit_series_episodes(dict(self.arguments))
            audit_series_episodes(dict(other))
            audit_series_episodes(dict(self.arguments))
            self.assertEqual(inspect.call_count, 2)

            invalidate_episode_audit_cache({**self.arguments, "query": "the show"})
            audit_series_episodes(dict(self.arguments))
            self.assertEqual(inspect.call_count, 3)

            audit_series_episodes(dict(other))
            self.assertEqual(inspect.call_count, 3)

    def test_cache_dimensions_include_library_and_exact_target_episode(self):
        scoped = {**self.arguments, "library_name": "动漫库", "season": 1, "target_episode": 1}
        other_episode = {**scoped, "target_episode": 2}
        other_library = {**scoped, "library_name": "美女库"}
        with patch(
            "app.agent.episode_audit.inspect_series_episode_sources",
            return_value=[_ready([])],
        ) as inspect, patch("app.agent.episode_audit.TMDBClient", return_value=_FakeTMDB()):
            audit_series_episodes(dict(scoped))
            audit_series_episodes(dict(other_episode))
            audit_series_episodes(dict(other_library))
            audit_series_episodes(dict(scoped))
            self.assertEqual(inspect.call_count, 3)

            invalidate_episode_audit_cache(dict(scoped))
            audit_series_episodes(dict(scoped))
            audit_series_episodes(dict(other_episode))
            audit_series_episodes(dict(other_library))
            self.assertEqual(inspect.call_count, 4)

    def test_cache_deduplicates_same_audit(self):
        with patch("app.agent.episode_audit.inspect_series_episode_sources", return_value=[_ready([])]) as inspect, patch(
            "app.agent.episode_audit.TMDBClient", return_value=_FakeTMDB()
        ):
            audit_series_episodes(dict(self.arguments))
            audit_series_episodes(dict(self.arguments))
        inspect.assert_called_once()


class TMDBSeasonPathTests(unittest.TestCase):
    def test_tv_season_detail_uses_bounded_relative_path(self):
        from app.clients.tmdb import TMDBClient

        client = TMDBClient(api_key="key")
        with patch.object(client, "get", return_value={"episodes": []}) as get:
            client.tv_season_detail("12345", 2)
        get.assert_called_once_with("/tv/12345/season/2")
        with self.assertRaises(ValueError):
            client.tv_season_detail("../secret", 2)
        with self.assertRaises(ValueError):
            client.tv_season_detail("12345", True)




class EpisodeAuditClientLifecycleTests(unittest.TestCase):
    def test_owned_tmdb_client_is_closed_after_success(self) -> None:
        reset_episode_audit_cache_for_tests()
        client = _FakeTMDB()
        closed = False

        def close() -> None:
            nonlocal closed
            closed = True

        client.close = close
        with patch(
            "app.agent.episode_audit.inspect_series_episode_sources",
            return_value=[_ready([(1, 1)])],
        ), patch("app.agent.episode_audit.TMDBClient", return_value=client):
            result = audit_series_episodes({
                "query": "The Show",
                "tmdb_id": "",
                "season": None,
                "as_of": "2026-08-01",
            })

        self.assertTrue(result.ok)
        self.assertTrue(closed)

    def test_close_failure_does_not_replace_tmdb_error(self) -> None:
        reset_episode_audit_cache_for_tests()
        client = _FakeTMDB()
        client.detail = lambda *_args: (_ for _ in ()).throw(
            ProviderUnavailable("upstream unavailable")
        )
        client.close = lambda: (_ for _ in ()).throw(RuntimeError("close failed"))
        with patch(
            "app.agent.episode_audit.inspect_series_episode_sources",
            return_value=[_ready([])],
        ), patch("app.agent.episode_audit.TMDBClient", return_value=client):
            result = audit_series_episodes({
                "query": "The Show",
                "tmdb_id": "",
                "season": None,
                "as_of": "2026-08-01",
            })

        self.assertEqual(result.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
