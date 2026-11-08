"""剧集完整性审计的映射、播出日期、截断和脱敏测试。"""
from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, cast
from unittest.mock import patch

from app.sdk import library
from app.clients.base import (
    MediaServerClient,
    SeriesCandidate,
    SeriesEpisodeInventory,
    SeriesSearchResult,
)
from app.discovery.models import (
    ProviderNotConfigured,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.modules.episode_audit import (
    EpisodeAuditResult,
    check_library_updates,
    normalize_episode_audit_arguments,
    audit_series_episodes,
    invalidate_episode_audit_cache,
    missing_episode_verification,
    reset_episode_audit_cache_for_tests,
)
from app.services import _series_source_payload


class EpisodeVerificationProjectionTests(unittest.TestCase):
    def test_projection_uses_exact_verdict_without_guessing_from_bounded_sample(self):
        proof = {"title": "The Show", "tmdb_id": "12345", "season": 2,
                 "episode": 99, "as_of": "2026-08-03", "library_name": "动漫库"}
        data = {**proof, "target_episode": 99, "target_missing": True,
                "missing_sample": [{"season": 2, "episode": 3}], "missing_sample_truncated": True,
                "sources": [{"path": "/private/library"}]}
        audit = EpisodeAuditResult(True, "updates_available", "missing", data)
        self.assertEqual(missing_episode_verification(audit, season=2, episode=99), proof)
        self.assertIsNone(missing_episode_verification(audit, season=2, episode=3))
        for patch_data in ({"target_missing": False}, {"target_missing": "true"}, {"tmdb_id": ""}):
            with self.subTest(patch_data=patch_data):
                audit.data = {**data, **patch_data}
                self.assertIsNone(missing_episode_verification(audit, season=2, episode=99))
        audit.ok, audit.status, audit.data = False, "inconclusive", data
        self.assertIsNone(missing_episode_verification(audit, season=2, episode=99))

    def test_season_projection_requires_complete_missing_list(self):
        data = {"title": "The Show", "tmdb_id": "12345", "as_of": "2026-08-03",
                "missing_sample": [{"season": 2, "episode": 3}], "missing_sample_truncated": False}
        audit = EpisodeAuditResult(True, "updates_available", "missing", data)
        proof = missing_episode_verification(audit, season=2, episode=3)
        assert proof is not None
        self.assertEqual(proof["episode"], 3)
        self.assertIsNone(missing_episode_verification(audit, season=2, episode=4))
        audit.data = {**data, "missing_sample_truncated": True}
        self.assertIsNone(missing_episode_verification(audit, season=2, episode=3))


class _FakeTMDB:
    close: Callable[[], None]
    def __init__(self):
        self.details = {"name": "The Show", "first_air_date": "2026-01-01", "seasons": [{"season_number": 1}, {"season_number": 2}]}
        self.seasons: dict[int, dict[str, Any]] = {
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

    def find_series_candidates_by_tmdb(self, tmdb_id, limit=20, *, parent_id="", identity_batch=None):
        self.parent_ids.append(parent_id)
        return self.provider_search

    def list_series_episode_inventory(
        self, series_id, *, max_episodes=2000, page_size=200, include_specials=False
    ):
        return self.inventories[series_id]


class SeriesSourcePayloadTests(unittest.TestCase):
    def test_incomplete_identity_lookup_cannot_be_erased_by_title_fallback(self):
        from app.modules.media_subscriptions import MediaSubscriptionService

        for title_id, expected in ((None, "not_found"), ("", "unmapped"), ("12345", "ready"), ("2", "conflict")):
            with self.subTest(title_id=title_id):
                candidates = [] if title_id is None else [
                    SeriesCandidate("series-1", "The Show", "2026", title_id)
                ]
                client = _FakeMediaClient(
                    SeriesSearchResult(candidates=candidates, total=len(candidates)),
                    provider_search=SeriesSearchResult(truncated=True),
                    inventories={"series-1": SeriesEpisodeInventory(episodes=[(1, 1)], total=1)},
                )
                result = _series_source_payload(
                    "jellyfin", "Jellyfin", cast(MediaServerClient, client), "The Show", "12345", 2000
                )
                self.assertEqual(result["status"], expected)
                self.assertTrue(result["truncated"])
                self.assertFalse(MediaSubscriptionService._inventory_complete([result])[0])

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
            "jellyfin", "Jellyfin", cast(MediaServerClient, client), "The Show", "", 2000
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
            "jellyfin", "Jellyfin", cast(MediaServerClient, client), "The Show", "12345", 2000
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
            "jellyfin", "Jellyfin", cast(MediaServerClient, client), "The Show", "", 2000,
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
                    "jellyfin", "Jellyfin", cast(MediaServerClient, client), "The Show", "", 2000,
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
            "jellyfin", "Jellyfin", cast(MediaServerClient, client), "The Show", "12345", 2000
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["episodes"], [(1, 1), (1, 2)])



class EpisodeAuditTests(unittest.TestCase):
    def test_plain_service_defaults_date_and_shares_cache_with_normalized_agent_input(self):
        from datetime import datetime

        raw = {"query": "  The Show  ", "tmdb_id": " 12345 ", "season": 1}
        with patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=[_ready([])]) as inspect, \
                patch("app.modules.episode_audit.TMDBClient", return_value=_FakeTMDB()):
            facts = audit_series_episodes(raw)
            self.assertEqual(facts.data["as_of"], datetime.now().astimezone().date().isoformat())
            self.assertEqual(facts.data["query"], "The Show")
            self.assertEqual(asdict(audit_series_episodes(normalize_episode_audit_arguments(raw))), asdict(facts))
            self.assertEqual(asdict(audit_series_episodes({**raw, "as_of": None})), asdict(facts))
            inspect.assert_called_once()
            invalidate_episode_audit_cache(raw)
            audit_series_episodes(raw)
            self.assertEqual(inspect.call_count, 2)

    def test_plain_service_rejects_invalid_inputs_before_reading_inventory(self):
        from datetime import date, timedelta
        bad = ({"query": ""}, {"query": "The Show", "tmdb_id": 12345},
               {"query": "The Show", "season": True}, {"query": "The Show", "target_episode": 2},
               {"query": "The Show", "library_name": " "},
               {"query": "The Show", "as_of": True},
               {"query": "The Show", "as_of": ""},
               {"query": "The Show", "as_of": "2026-02-30"},
               {"query": "The Show", "as_of": (date.today() + timedelta(days=1)).isoformat()})
        with patch("app.modules.episode_audit.inspect_series_episode_sources") as inspect:
            for arguments in bad:
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    audit_series_episodes(arguments)
            with self.assertRaises(ValueError):
                audit_series_episodes({"query": "The Show"}, refresh=cast(Any, "yes"))
            inspect.assert_not_called()

    def setUp(self):
        reset_episode_audit_cache_for_tests()
        self.arguments: dict = {
            "query": "The Show", "tmdb_id": "", "season": None, "as_of": "2026-08-01"
        }

    def _run(self, sources):
        with patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=sources), patch(
            "app.modules.episode_audit.TMDBClient", return_value=_FakeTMDB()
        ):
            return audit_series_episodes(dict(self.arguments))

    def test_shared_audit_facts_and_cache_are_independent_of_native_response(self):
        from datetime import datetime

        arguments: dict[str, Any] = {**self.arguments, "season": 1, "target_episode": 2}
        with (
            patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=[_ready([])]) as inspect,
            patch("app.modules.episode_audit.TMDBClient", return_value=_FakeTMDB()),
        ):
            facts = audit_series_episodes(arguments)
            self.assertIsInstance(facts, EpisodeAuditResult)
            self.assertEqual((facts.status, facts.data["missing_count"]), ("updates_available", 2))
            self.assertTrue(facts.data["target_missing"])
            self.assertEqual(facts.mapping_context["detail"]["name"], "The Show")
            self.assertEqual(facts.mapping_context["as_of"], arguments["as_of"])
            self.assertIsNotNone(datetime.fromisoformat(facts.checked_at).tzinfo)
            self.assertNotIn("resource_followups", facts.data)
            expected = asdict(facts)

            shown = library.audit(**arguments)
            self.assertEqual(shown, expected)
            shown["data"]["sources"][0]["selected"]["name"] = "changed response"
            shown["mapping_context"]["detail"]["name"] = "changed response"
            shown["suggestions"].append("response only")
            self.assertEqual(asdict(facts), expected)
            self.assertEqual(asdict(audit_series_episodes(arguments)), expected)
            inspect.assert_called_once()

    def test_incomplete_empty_source_is_not_proof_of_absence(self):
        source = {**_ready([], truncated=True), "status": "not_found", "selected": None}
        result = self._run([source])
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "inconclusive")

    def test_incomplete_empty_second_server_prevents_definitive_missing_verdict(self):
        sources = [_ready([(1, 1)]), {
            **_ready([], server="Emby", truncated=True), "status": "not_found", "selected": None,
        }]
        result = self._run(sources)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "inconclusive")

    def _large_missing_fixture(self):
        fake = _FakeTMDB()
        # TMDB 本身断号，本地还收录了后面的集；不能用首尾范围或集数猜缺集。
        fake.seasons = {
            1: {"episodes": [
                {"episode_number": number, "air_date": "2026-07-01"}
                for number in range(1, 131) if number not in {2, 17, 88}
            ]},
            2: {"episodes": [
                {"episode_number": number, "air_date": "2026-07-01"}
                for number in (1, 3, 8, 13, 21, 150)
            ]},
        }
        source = _ready([(1, 1), (1, 4), (1, 70), (1, 124), (2, 1), (2, 8), (2, 150)])
        missing = [
            {"season": 1, "episode": number}
            for number in range(1, 131) if number not in {1, 2, 4, 17, 70, 88, 124}
        ] + [{"season": 2, "episode": number} for number in (3, 13, 21)]
        return fake, source, missing

    def test_full_missing_list_survives_real_audit_to_batch_across_sparse_seasons(self):
        fake, source, missing = self._large_missing_fixture()
        with patch(
            "app.modules.episode_audit.inspect_series_episode_sources", return_value=[source],
        ) as inspect, patch("app.modules.episode_audit.TMDBClient", return_value=fake):
            audit = audit_series_episodes(dict(self.arguments))
            # 不 mock audit_series_episodes：批量入口必须再次经过真实审计算法。
            batch = check_library_updates({
                "queries": ["The Show"], "media_type": "tv",
                "as_of": self.arguments["as_of"], "refresh": True,
            })
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(len(missing), 126)
        self.assertTrue(audit.ok)
        self.assertEqual(audit.status, "updates_available")
        self.assertEqual(batch["status"], "success")
        self.assertEqual(batch["data"]["counts"], {
            "requested": 1, "updates_available": 1, "up_to_date": 0, "uncertain": 0,
        })
        for data in (audit.data, batch["data"]["items"][0]):
            self.assertEqual(data["expected_aired"], 133)
            self.assertEqual(data["local_episode_count"], 7)
            self.assertEqual(data["missing_count"], len(missing))
            self.assertEqual(data["missing_sample"], missing)
            self.assertFalse(data["missing_sample_truncated"])
            self.assertEqual(data["latest_local"], {"season": 2, "episode": 150})
            self.assertEqual(data["latest_aired"], {"season": 2, "episode": 150})
        self.assertNotIn("resource_followups", audit.data)
        self.assertEqual(batch["data"]["items"][0]["episodes"], audit.data["episodes"])

    def test_full_missing_list_does_not_make_incomplete_audit_conclusive(self):
        fake, source, missing = self._large_missing_fixture()
        incomplete_sources = {
            "truncated_inventory": [{**source, "truncated": True}],
            "unpositioned_inventory": [{**source, "ignored_unknown": 1}],
            "unavailable_server": [source, {
                **_ready([], server="Emby"), "status": "unavailable", "selected": None,
            }],
        }
        for reason, sources in incomplete_sources.items():
            with self.subTest(reason=reason):
                reset_episode_audit_cache_for_tests()
                with patch(
                    "app.modules.episode_audit.inspect_series_episode_sources", return_value=sources,
                ), patch("app.modules.episode_audit.TMDBClient", return_value=fake):
                    audit = audit_series_episodes(dict(self.arguments))
                    batch = check_library_updates({
                        "queries": ["The Show"], "media_type": "tv",
                        "as_of": self.arguments["as_of"], "refresh": True,
                    })
                self.assertFalse(audit.ok)
                self.assertEqual(audit.status, "inconclusive")
                self.assertNotIn("resource_followups", audit.data)
                self.assertFalse(batch["ok"])
                self.assertEqual(batch["status"], "partial")
                self.assertEqual(batch["data"]["counts"], {
                    "requested": 1, "updates_available": 0, "up_to_date": 0, "uncertain": 1,
                })
                for data in (audit.data, batch["data"]["items"][0]):
                    self.assertEqual(data["missing_count"], len(missing))
                    self.assertEqual(data["missing_sample"], missing)
                    self.assertFalse(data["missing_sample_truncated"])
                self.assertEqual(batch["data"]["items"][0]["status"], "inconclusive")

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
        self.assertEqual(result.data["episodes"], [
            {"season": 1, "episode": 1, "aired": True, "local": True},
            {"season": 1, "episode": 2, "aired": True, "local": False},
            {"season": 1, "episode": 3, "aired": False, "local": False},
            {"season": 1, "episode": 4, "aired": None, "local": False},
            {"season": 2, "episode": 1, "aired": True, "local": False},
        ])
        self.assertEqual(result.data["ignored_specials"], 1)

    def test_unpositioned_local_media_blocks_confirmed_missing_followups(self):
        source = {**_ready([(1, 1)]), "ignored_unknown": 1, "local_total": 2}
        with patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=[source]), patch(
            "app.modules.episode_audit.TMDBClient", return_value=_FakeTMDB()
        ):
            result = audit_series_episodes(dict(self.arguments))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.data["ignored_unknown_local"], 1)
        self.assertNotIn("resource_followups", result.data)

    def test_unpositioned_extra_does_not_hide_known_complete_inventory(self):
        source = {**_ready([(1, 1), (1, 2), (2, 1)]), "ignored_unknown": 1}
        with patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=[source]), patch(
            "app.modules.episode_audit.TMDBClient", return_value=_FakeTMDB()
        ):
            result = audit_series_episodes(dict(self.arguments))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "up_to_date")
        self.assertEqual(result.data["missing_count"], 0)

    def test_latest_episode_numbers_are_not_local_episode_count(self):
        fake = _FakeTMDB()
        fake.details = {
            "name": "The Show",
            "first_air_date": "2026-01-01",
            "seasons": [{"season_number": 1}, {"season_number": 2}],
        }
        fake.seasons = {
            1: {
                "episodes": [
                    {"episode_number": 1, "air_date": "2026-07-01"},
                    {"episode_number": 2, "air_date": "2026-07-08"},
                ]
            },
            2: {
                "episodes": [
                    {"episode_number": 1, "air_date": "2026-07-15"}
                ]
            },
        }
        with patch(
            "app.modules.episode_audit.inspect_series_episode_sources",
            return_value=[_ready([(1, 1), (2, 1)])],
        ), patch("app.modules.episode_audit.TMDBClient", return_value=fake):
            result = audit_series_episodes(dict(self.arguments))

        self.assertEqual(result.status, "updates_available")
        self.assertEqual(result.data["local_episode_count"], 2)
        self.assertEqual(
            result.data["latest_local"], {"season": 2, "episode": 1}
        )
        self.assertEqual(
            result.data["latest_aired"], {"season": 2, "episode": 1}
        )
        self.assertEqual(result.data["missing_count"], 1)
        self.assertEqual(
            result.data["missing_sample"], [{"season": 1, "episode": 2}]
        )

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
        arguments: dict[str, Any] = {
            **self.arguments,
            "season": 1,
            "target_episode": 150,
            "library_name": "美女库",
        }
        source = {**_ready([(1, number) for number in range(1, 150)]), "library_name": "美女库"}
        with patch(
            "app.modules.episode_audit.inspect_series_episode_sources",
            return_value=[source],
        ) as inspect, patch("app.modules.episode_audit.TMDBClient", return_value=fake):
            result = audit_series_episodes(arguments)

        self.assertEqual(result.status, "updates_available")
        self.assertTrue(result.data["missing_sample_truncated"] is False)
        self.assertTrue(result.data["target_aired"] is True)
        self.assertTrue(result.data["target_local"] is False)
        self.assertTrue(result.data["target_missing"] is True)
        inspect.assert_called_once_with(
            "The Show", tmdb_id="", max_episodes=2000, library_name="美女库"
        )

    def test_native_audit_keeps_full_large_missing_list(self):
        fake = _FakeTMDB()
        fake.details = {"name": "The Show", "first_air_date": "2026-01-01", "seasons": [{"season_number": 1}]}
        fake.seasons = {
            1: {
                "episodes": [
                    {"episode_number": number, "air_date": "2026-07-01"}
                    for number in range(1, 151)
                ]
            }
        }
        with patch(
            "app.modules.episode_audit.inspect_series_episode_sources",
            return_value=[_ready([])],
        ), patch("app.modules.episode_audit.TMDBClient", return_value=fake):
            result = audit_series_episodes(dict(self.arguments))

        self.assertEqual(result.status, "updates_available")
        self.assertEqual(result.data["missing_count"], 150)
        self.assertEqual(result.data["missing_sample"], [
            {"season": 1, "episode": number} for number in range(1, 151)
        ])
        self.assertFalse(result.data["missing_sample_truncated"])

    def test_up_to_date_and_explicit_season_scope(self):
        self.arguments["season"] = 1
        result = self._run([_ready([(1, 1), (1, 2)])])
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "up_to_date")
        self.assertEqual(result.data["expected_aired"], 2)
        self.assertEqual(result.data["missing_count"], 0)
        self.assertNotIn("resource_followups", result.data)

        self.assertEqual(result.mapping_context["detail"]["name"], "The Show")
        self.assertEqual(result.mapping_context["as_of"], result.data["as_of"])

        self.arguments["season"] = 3
        absent_metadata = self._run([_ready([(1, 1), (3, 4), (3, 5)])])
        self.assertEqual(absent_metadata.status, "not_found")
        self.assertEqual(absent_metadata.data["local_episode_count"], 2)
        self.assertEqual(absent_metadata.data["latest_local"], {"season": 3, "episode": 5})

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
        fake.search = lambda *_args, **_kwargs: [
            {"id": 1, "name": "The Show", "first_air_date": "2026-01-01"},
            {"id": 2, "name": "The Show", "first_air_date": "2026-05-01"},
        ]
        with patch(
            "app.modules.episode_audit.inspect_series_episode_sources",
            return_value=[source],
        ), patch("app.modules.episode_audit.TMDBClient", return_value=fake):
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
        self.assertNotIn("private", str(asdict(partial)))

    def test_truncated_local_inventory_is_inconclusive(self):
        result = self._run([_ready([(1, 1)], truncated=True)])
        self.assertEqual(result.status, "inconclusive")
        self.assertFalse(result.ok)

    def test_tmdb_configuration_and_upstream_errors_are_safe(self):
        sources = [_ready([(1, 1), (2, 3)]), _ready([(1, 1), (1, 2)], server="Emby")]
        cases = (
            (ProviderNotConfigured("private secret"), "not_configured", "detail", None, 3, {"season": 2, "episode": 3}),
            (ProviderUnavailable("private secret"), "unavailable", "detail", 1, 2, {"season": 1, "episode": 2}),
            (ProviderTimeout("private secret"), "unavailable", "tv_season_detail", 2, 1, {"season": 2, "episode": 3}),
            (ValueError("private secret"), "unavailable", "tv_season_detail", 1, 2, {"season": 1, "episode": 2}),
        )
        for error, status, method, season, count, latest in cases:
            with self.subTest(status=status, method=method, season=season):
                reset_episode_audit_cache_for_tests()
                fake = _FakeTMDB()
                setattr(fake, method, lambda *_args, error=error, **_kwargs: (_ for _ in ()).throw(error))
                arguments: dict[str, Any] = {**self.arguments, "query": "本地别名", "season": season}
                with patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=sources), patch(
                    "app.modules.episode_audit.TMDBClient", return_value=fake
                ):
                    result = audit_series_episodes(arguments)
                    shown = audit_series_episodes(arguments)
                    updated = check_library_updates({**arguments, "media_type": "tv"})
                native = json.loads(json.dumps(updated))
                self.assertNotIn("resource_followups", shown.data)
                for data in (result.data, shown.data, updated["data"], native["data"]):
                    self.assertEqual(data["tmdb_id"], "12345")
                    self.assertEqual(data["title"], "The Show")
                    self.assertEqual(data["local_episode_count"], count)
                    self.assertEqual(data["latest_local"], latest)
                    self.assertTrue(data["local_inventory_complete"])
                    self.assertIsNone(data["expected_aired"])
                    self.assertIsNone(data["latest_aired"])
                    self.assertIsNone(data["missing_count"])
                    self.assertEqual(data["metadata_error"], {"provider": "tmdb", "code": getattr(error, "code", "invalid_data")})
                self.assertIn("TMDB", native["summary"])
                self.assertNotIn("private secret", str(asdict(result)))

    def test_metadata_failure_does_not_claim_unknown_inventory_is_empty_or_complete(self):
        for sources in ([{**_ready([]), "status": "unavailable", "selected": None}],
                        [_ready([(1, 1)], truncated=True)],
                        [_ready([(1, 1)]), {**_ready([], server="Emby"), "status": "unavailable", "selected": None}]):
            with self.subTest(sources=sources):
                reset_episode_audit_cache_for_tests()
                fake = _FakeTMDB()
                fake.detail = lambda *_args, **_kwargs: (_ for _ in ()).throw(ProviderUnavailable("private secret"))
                with patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=sources), patch(
                    "app.modules.episode_audit.TMDBClient", return_value=fake
                ):
                    result = audit_series_episodes(dict(self.arguments))
                self.assertEqual(result.status, "unavailable")
                self.assertIsNone(result.data["missing_count"])
                self.assertFalse(result.data["local_inventory_complete"])
                if sources[0]["status"] == "ready":
                    self.assertEqual(result.data["local_episode_count"], 1)
                else:
                    self.assertIsNone(result.data["local_episode_count"])

    def test_failed_metadata_keeps_known_empty_and_conflicting_inventory_distinct(self):
        fake = _FakeTMDB()
        fake.detail = lambda *_args, **_kwargs: (_ for _ in ()).throw(ProviderUnavailable("private secret"))
        with patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=[_ready([])]), patch(
            "app.modules.episode_audit.TMDBClient", return_value=fake
        ):
            empty = audit_series_episodes(dict(self.arguments))
        self.assertEqual(empty.status, "unavailable")
        self.assertEqual(empty.data["local_episode_count"], 0)
        self.assertTrue(empty.data["local_inventory_complete"])
        self.assertIsNone(empty.data["missing_count"])
        reset_episode_audit_cache_for_tests()
        conflict = self._run([_ready([(1, 1)], tmdb_id="1"), _ready([(1, 2)], server="Emby", tmdb_id="2")])
        self.assertEqual(conflict.status, "conflict")
        self.assertIsNone(conflict.data["local_episode_count"])
        self.assertFalse(conflict.data["local_inventory_complete"])

    def test_invalidate_cache_evicts_only_matching_normalized_key(self):
        other = {**self.arguments, "season": 2}
        with patch(
            "app.modules.episode_audit.inspect_series_episode_sources",
            return_value=[_ready([])],
        ) as inspect, patch("app.modules.episode_audit.TMDBClient", return_value=_FakeTMDB()):
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
            "app.modules.episode_audit.inspect_series_episode_sources",
            return_value=[_ready([])],
        ) as inspect, patch("app.modules.episode_audit.TMDBClient", return_value=_FakeTMDB()):
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
        with patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=[_ready([])]) as inspect, patch(
            "app.modules.episode_audit.TMDBClient", return_value=_FakeTMDB()
        ):
            audit_series_episodes(dict(self.arguments))
            audit_series_episodes(dict(self.arguments))
        inspect.assert_called_once()

    def test_check_updates_refreshes_inventory_but_plain_audit_cache_still_reuses(self):
        inventory = [(1, 1)]

        def inspect_inventory(*_args, **_kwargs):
            return [_ready(list(inventory))]

        with patch(
            "app.modules.episode_audit.inspect_series_episode_sources",
            side_effect=inspect_inventory,
        ) as inspect, patch(
            "app.modules.episode_audit.TMDBClient", return_value=_FakeTMDB()
        ):
            first = audit_series_episodes(dict(self.arguments))
            self.assertEqual(first.status, "updates_available")
            self.assertEqual(first.data["local_episode_count"], 1)

            # 普通 audit 仍应命中旧缓存，而不是因为媒体库存变化自行绕过缓存。
            inventory[:] = [(1, 1), (1, 2), (2, 1)]
            cached = audit_series_episodes(dict(self.arguments))
            self.assertEqual(cached.status, "updates_available")
            self.assertEqual(cached.data["local_episode_count"], 1)
            self.assertEqual(inspect.call_count, 1)

            # check_updates 默认 refresh=True：先失效目标缓存，再读取新库存。
            refreshed = check_library_updates(
                {
                    "query": "The Show",
                    "media_type": "tv",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                }
            )
            self.assertEqual(refreshed["status"], "up_to_date")
            self.assertEqual(refreshed["data"]["local_episode_count"], 3)
            self.assertEqual(inspect.call_count, 2)

            # refresh=False 允许复用刚得到的缓存，即使临时库存再次变化。
            inventory[:] = [(1, 1)]
            reused = check_library_updates(
                {
                    "query": "The Show",
                    "media_type": "tv",
                    "tmdb_id": "",
                    "season": None,
                    "as_of": "2026-08-01",
                    "refresh": False,
                }
            )
            self.assertEqual(reused["status"], "up_to_date")
            self.assertEqual(reused["data"]["local_episode_count"], 3)
            self.assertEqual(inspect.call_count, 2)

    def test_explicit_refresh_replaces_only_target_cache_and_preserves_default_reuse(self):
        other = {**self.arguments, "query": "Other Show"}
        with patch("app.modules.episode_audit._audit_uncached", side_effect=[
            EpisodeAuditResult(True, "updates_available", "old"),
            EpisodeAuditResult(True, "up_to_date", "other"),
            EpisodeAuditResult(True, "up_to_date", "fresh"),
        ]) as read:
            audit_series_episodes(self.arguments)
            audit_series_episodes(other)
            self.assertEqual(audit_series_episodes(self.arguments).summary, "old")
            self.assertEqual(audit_series_episodes(self.arguments, refresh=True).summary, "fresh")
            self.assertEqual(audit_series_episodes(self.arguments).summary, "fresh")
            self.assertEqual(audit_series_episodes(other).summary, "other")
            self.assertEqual(read.call_count, 3)

    def test_explicit_refresh_fences_old_flight_and_keeps_new_readers_singleflight(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from app.modules import episode_audit as module

        for old_refresh, old_first in ((False, True), (False, False), (True, True), (True, False)):
            with self.subTest(old_refresh=old_refresh, old_first=old_first):
                reset_episode_audit_cache_for_tests()
                started = [threading.Event(), threading.Event()]
                release = [threading.Event(), threading.Event()]
                joined = threading.Event()
                calls = []

                def read(_arguments, *, calls=calls, started=started, release=release):
                    number = len(calls)
                    calls.append(number)
                    started[number].set()
                    self.assertTrue(release[number].wait(3))
                    return EpisodeAuditResult(True, "up_to_date", str(number))

                with patch.object(module, "_audit_uncached", side_effect=read), ThreadPoolExecutor(max_workers=3) as pool:
                    try:
                        old = pool.submit(audit_series_episodes, self.arguments, refresh=old_refresh)
                        self.assertTrue(started[0].wait(3))
                        fresh = pool.submit(audit_series_episodes, self.arguments, refresh=True)
                        self.assertTrue(started[1].wait(3))
                        flight = next(iter(module._inflight.values()))
                        wait = flight.wait

                        def wait_for_fresh(*args, joined=joined, wait=wait, **kwargs):
                            joined.set()
                            return wait(*args, **kwargs)

                        with patch.object(flight, "wait", side_effect=wait_for_fresh):
                            reader = pool.submit(audit_series_episodes, self.arguments)
                            self.assertTrue(joined.wait(3))
                            if old_first:
                                release[0].set()
                                self.assertEqual(old.result(3).summary, "0")
                                self.assertEqual(list(module._inflight.values()), [flight])
                            release[1].set()
                            self.assertEqual(fresh.result(3).summary, "1")
                            self.assertEqual(reader.result(3).summary, "1")
                            release[0].set()
                            self.assertEqual(old.result(3).summary, "0")
                        self.assertEqual(audit_series_episodes(self.arguments).summary, "1")
                        self.assertEqual(calls, [0, 1])
                        self.assertFalse(module._inflight)
                    finally:
                        for event in release:
                            event.set()

    def test_unknown_air_dates_are_not_presented_as_definitively_current(self):
        with patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=[_ready([(1, 1), (1, 2), (2, 1)])]), patch(
            "app.modules.episode_audit.TMDBClient", return_value=_FakeTMDB()
        ):
            result = audit_series_episodes(dict(self.arguments))
        self.assertEqual(result.status, "up_to_date")
        self.assertEqual(result.data["unknown_air_date_count"], 1)
        self.assertIn("已知播出日期", result.summary)
        self.assertIn("不能据此判断全部最新", " ".join(result.suggestions))

    def test_group_verification_stays_unknown_after_preflight_metadata_changes(self):
        from app.agent.domains.downloads.recent_submissions import (
            RecentDownloadSubmission, RecentDownloadVerification, build_recent_download_library_verification,
        )
        from app.agent.models import ToolResult

        arguments: dict[str, Any] = {**self.arguments, "season": 1}
        for change in ("unknown_date", "future_date", "removed_episode"):
            fake = _FakeTMDB()
            with self.subTest(change=change), \
                 patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=[_ready([(1, 1)])]), \
                 patch("app.modules.episode_audit.TMDBClient", return_value=fake):
                before = audit_series_episodes({**arguments, "target_episode": 2}, refresh=True)
                proof = missing_episode_verification(before, season=1, episode=2)
                assert proof is not None
                record = RecentDownloadSubmission(
                    request_id=1, target="qb", dispatch_status="submitted", succeeded=("qb",), failed=(),
                    created=True, duplicate=False, result_status="accepted", captured_at=before.checked_at,
                    verification=RecentDownloadVerification(**proof),
                )
                episodes = fake.seasons[1]["episodes"]
                if change == "removed_episode":
                    fake.seasons[1]["episodes"] = [item for item in episodes if item["episode_number"] != 2]
                else:
                    next(item for item in episodes if item["episode_number"] == 2)["air_date"] = (
                        None if change == "unknown_date" else "2026-08-08"
                    )
                after = audit_series_episodes(arguments, refresh=True)
                self.assertEqual((after.status, after.data["missing_count"]), ("up_to_date", 0))
                self.assertNotIn("target_missing", after.data)
                facts = next((item for item in after.data["episodes"] if item["episode"] == 2), None)
                if change == "removed_episode":
                    self.assertIsNone(facts)
                else:
                    assert facts is not None
                    self.assertIs(facts["aired"], None if change == "unknown_date" else False)
                    self.assertFalse(facts["local"])
                projected = build_recent_download_library_verification(
                    record, ToolResult(after.ok, after.status, after.summary, data=after.data), position=1,
                )
                self.assertEqual(projected.data["verification"], "inconclusive")

    def test_episode_facts_include_local_only_positions_without_inventing_air_dates(self):
        self.arguments["season"] = 1
        result = self._run([_ready([(1, 1), (1, 9)])])
        self.assertIn({"season": 1, "episode": 9, "aired": None, "local": True}, result.data["episodes"])

    def test_refresh_replaces_inflight_query_without_stale_cache_publish_or_unlock(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from app.modules import episode_audit as module
        old_started, new_started, old_release, new_release = (threading.Event() for _ in range(4))
        calls = []
        def audit(_arguments):
            calls.append(len(calls) + 1)
            number = calls[-1]
            (old_started if number == 1 else new_started).set()
            self.assertTrue((old_release if number == 1 else new_release).wait(3))
            return EpisodeAuditResult(True, "up_to_date", "模拟库存", data={"local_episode_count": number})
        with patch("app.modules.episode_audit._audit_uncached", side_effect=audit), ThreadPoolExecutor(max_workers=2) as pool:
            try:
                old = pool.submit(audit_series_episodes, dict(self.arguments))
                self.assertTrue(old_started.wait(3))
                fresh = pool.submit(check_library_updates, dict(self.arguments))
                self.assertTrue(new_started.wait(3))
                old_release.set()
                self.assertEqual(old.result(3).data["local_episode_count"], 1)
                self.assertEqual(len(module._inflight), 1)
                new_release.set()
                self.assertEqual(fresh.result(3)["data"]["local_episode_count"], 2)
                self.assertEqual(audit_series_episodes(dict(self.arguments)).data["local_episode_count"], 2)
                self.assertEqual(calls, [1, 2])
            finally:
                old_release.set()
                new_release.set()


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
            "app.modules.episode_audit.inspect_series_episode_sources",
            return_value=[_ready([(1, 1)])],
        ), patch("app.modules.episode_audit.TMDBClient", return_value=client):
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
        client.detail = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderUnavailable("upstream unavailable")
        )
        client.close = lambda: (_ for _ in ()).throw(RuntimeError("close failed"))
        with patch(
            "app.modules.episode_audit.inspect_series_episode_sources",
            return_value=[_ready([])],
        ), patch("app.modules.episode_audit.TMDBClient", return_value=client):
            result = audit_series_episodes({
                "query": "The Show",
                "tmdb_id": "",
                "season": None,
                "as_of": "2026-08-01",
            })

        self.assertEqual(result.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
