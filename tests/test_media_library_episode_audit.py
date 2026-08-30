"""Media Agent 全库剧集缺集巡检。"""
from __future__ import annotations

import time
import unittest
from unittest.mock import ANY, MagicMock, patch

from app.clients.base import SeriesCandidate, SeriesEpisodeInventory, SeriesSearchResult


class LibrarySeriesSourceTests(unittest.TestCase):
    def test_service_snapshot_uses_internal_ids_only_for_inventory_calls(self):
        from app.services import inspect_library_series_sources

        client = MagicMock()
        client.list_library_series.return_value = SeriesSearchResult(
            candidates=[
                SeriesCandidate("internal-secret-id", "Mapped Show", "2026", "12345"),
                SeriesCandidate("internal-unmapped-id", "Unmapped Show", "2025", ""),
            ],
            total=2,
            truncated=False,
        )
        client.list_series_episode_inventory.return_value = SeriesEpisodeInventory(
            episodes=[(1, 1)],
            total=1,
            truncated=False,
        )
        with patch(
            "app.services._configured_media_sources",
            return_value=[("jellyfin", "Jellyfin", "http://secret.local", client)],
        ):
            sources = inspect_library_series_sources(
                max_series=10,
                deadline_at=time.monotonic() + 30,
            )

        self.assertEqual(
            [call.args[0] for call in client.list_series_episode_inventory.call_args_list],
            ["internal-secret-id", "internal-unmapped-id"],
        )
        for call in client.list_series_episode_inventory.call_args_list:
            self.assertEqual(call.kwargs["max_episodes"], 2000)
            self.assertEqual(call.kwargs["page_size"], 200)
            self.assertIsNotNone(call.kwargs["deadline_at"])
        client.list_library_series.assert_called_once_with(
            max_series=10,
            page_size=100,
            deadline_at=ANY,
        )
        self.assertEqual(sources[0]["unmapped_count"], 1)
        self.assertEqual(
            [item["tmdb_id"] for item in sources[0]["series"]],
            ["12345", ""],
        )
        serialized = repr(sources)
        self.assertNotIn("internal-secret-id", serialized)
        self.assertNotIn("internal-unmapped-id", serialized)
        self.assertNotIn("secret.local", serialized)

    def test_scan_all_pages_catalog_and_selects_after_cursor_in_tmdb_order(self):
        from app.services import inspect_library_series_sources

        client = MagicMock()
        client.list_library_series.return_value = SeriesSearchResult(
            candidates=[
                SeriesCandidate("id-30", "Thirty", "2026", "30"),
                SeriesCandidate("id-10", "Ten", "2026", "10"),
                SeriesCandidate("id-unmapped", "Unmapped", "2026", ""),
                SeriesCandidate("id-20", "Twenty", "2026", "20"),
                SeriesCandidate("id-40", "Forty", "2026", "40"),
            ],
            total=5,
            truncated=False,
        )
        client.list_series_episode_inventory.return_value = SeriesEpisodeInventory(
            episodes=[(1, 1)],
            total=1,
            truncated=False,
        )
        with patch(
            "app.services._configured_media_sources",
            return_value=[("jellyfin", "Jellyfin", "http://secret.local", client)],
        ):
            sources = inspect_library_series_sources(
                max_series=2,
                deadline_at=time.monotonic() + 30,
                after_tmdb_id="10",
                scan_all=True,
            )

        client.list_library_series.assert_called_once_with(
            max_series=5000,
            page_size=100,
            deadline_at=ANY,
        )
        self.assertEqual(
            [call.args[0] for call in client.list_series_episode_inventory.call_args_list],
            ["id-20", "id-30"],
        )
        self.assertEqual([item["tmdb_id"] for item in sources[0]["series"]], ["20", "30"])
        self.assertTrue(sources[0]["batch_remaining"])
        self.assertEqual(sources[0]["next_tmdb_id"], "40")
        self.assertEqual(sources[0]["unmapped_count"], 1)
        self.assertFalse(sources[0]["catalog_truncated"])

    def test_scan_all_exposes_first_unselected_tmdb_id_with_duplicate_candidates(self):
        from app.services import inspect_library_series_sources

        client = MagicMock()
        client.list_library_series.return_value = SeriesSearchResult(
            candidates=[
                SeriesCandidate("id-10-a", "Ten A", "2026", "10"),
                SeriesCandidate("id-10-b", "Ten B", "2026", "10"),
                SeriesCandidate("id-20", "Twenty", "2026", "20"),
            ],
            total=3,
            truncated=False,
        )
        client.list_series_episode_inventory.return_value = SeriesEpisodeInventory(
            episodes=[(1, 1)],
            total=1,
            truncated=False,
        )
        with patch(
            "app.services._configured_media_sources",
            return_value=[("jellyfin", "Jellyfin", "http://secret.local", client)],
        ):
            sources = inspect_library_series_sources(
                max_series=1,
                deadline_at=time.monotonic() + 30,
                scan_all=True,
            )

        self.assertEqual(
            [item["tmdb_id"] for item in sources[0]["series"]],
            ["10", "10"],
        )
        self.assertTrue(sources[0]["batch_remaining"])
        self.assertEqual(sources[0]["next_tmdb_id"], "20")

    def test_scan_all_inventory_deadline_exposes_retry_cursor(self):
        from app.services import inspect_library_series_sources

        client = MagicMock()
        client.list_library_series.return_value = SeriesSearchResult(
            candidates=[SeriesCandidate("id-20", "Twenty", "2026", "20")],
            total=1,
            truncated=False,
        )
        client.list_series_episode_inventory.side_effect = TimeoutError("deadline")
        with patch(
            "app.services._configured_media_sources",
            return_value=[("jellyfin", "Jellyfin", "http://secret.local", client)],
        ):
            sources = inspect_library_series_sources(
                max_series=10,
                deadline_at=time.monotonic() + 30,
                scan_all=True,
            )

        self.assertEqual(sources[0]["status"], "incomplete")
        self.assertTrue(sources[0]["batch_remaining"])
        self.assertEqual(sources[0]["next_tmdb_id"], "20")
        self.assertTrue(sources[0]["deadline_exhausted"])
        self.assertFalse(sources[0]["catalog_truncated"])

    def test_source_timeout_is_reported_as_incomplete_deadline(self):
        from app.services import inspect_library_series_sources

        client = MagicMock()
        client.list_library_series.side_effect = TimeoutError("deadline")
        with patch(
            "app.services._configured_media_sources",
            return_value=[("jellyfin", "Jellyfin", "http://secret.local", client)],
        ):
            sources = inspect_library_series_sources(
                max_series=10,
                deadline_at=time.monotonic() + 30,
            )

        self.assertEqual(sources[0]["status"], "incomplete")
        self.assertTrue(sources[0]["truncated"])
        self.assertTrue(sources[0]["deadline_exhausted"])
        self.assertEqual(sources[0]["series"], [])



class MediaLibraryEpisodeAuditTests(unittest.TestCase):
    def _sources(self):
        return [
            {
                "server_type": "jellyfin",
                "server_name": "Jellyfin",
                "status": "ready",
                "series_total": 2,
                "series_enumerated": 2,
                "truncated": False,
                "deadline_exhausted": False,
                "unmapped_count": 0,
                "series": [
                    {
                        "name": "示例剧",
                        "year": "2026",
                        "tmdb_id": "012345",
                        "episodes": [(1, 1)],
                        "local_total": 1,
                        "truncated": False,
                        "ignored_specials": 1,
                        "ignored_unknown": 0,
                    },
                    {
                        "name": "已完整剧",
                        "year": "2025",
                        "tmdb_id": "67890",
                        "episodes": [(1, 1)],
                        "local_total": 1,
                        "truncated": False,
                        "ignored_specials": 0,
                        "ignored_unknown": 0,
                    },
                ],
            },
            {
                "server_type": "emby",
                "server_name": "Emby",
                "status": "ready",
                "series_total": 1,
                "series_enumerated": 1,
                "truncated": False,
                "deadline_exhausted": False,
                "unmapped_count": 0,
                "series": [
                    {
                        "name": "Example Show",
                        "year": "2026",
                        "tmdb_id": "12345",
                        "episodes": [(1, 2)],
                        "local_total": 1,
                        "truncated": False,
                        "ignored_specials": 0,
                        "ignored_unknown": 0,
                    }
                ],
            },
        ]

    @staticmethod
    def _tmdb_client():
        client = MagicMock()
        client.detail.side_effect = lambda tmdb_id, media_type, **_kwargs: {
            "name": "示例剧" if tmdb_id == "12345" else "已完整剧",
            "seasons": [{"season_number": 1}],
        }
        client.tv_season_detail.side_effect = lambda tmdb_id, season, **_kwargs: {
            "episodes": (
                [
                    {"episode_number": 1, "air_date": "2026-01-01"},
                    {"episode_number": 2, "air_date": "2026-01-08"},
                    {"episode_number": 3, "air_date": "2026-01-15"},
                    {"episode_number": 4, "air_date": "2027-01-01"},
                ]
                if tmdb_id == "12345"
                else [{"episode_number": 1, "air_date": "2025-01-01"}]
            )
        }
        return client

    def test_cross_server_series_are_grouped_by_tmdb_id_and_missing_is_bounded(self):
        from app.agent.library_episode_audit import audit_library_episodes

        client = self._tmdb_client()
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=self._sources(),
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
            result = audit_library_episodes({"as_of": "2026-08-03", "max_series": 50})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "updates_available")
        self.assertEqual(result.data["checked_series_count"], 2)
        self.assertEqual(result.data["updates_available_count"], 1)
        self.assertEqual(result.data["up_to_date_count"], 1)
        self.assertEqual(result.data["missing_episode_count"], 1)
        finding = result.data["findings"][0]
        self.assertEqual(finding["tmdb_id"], "12345")
        self.assertEqual(finding["source_count"], 2)
        self.assertEqual(finding["local_episode_count"], 2)
        self.assertEqual(finding["missing_sample"], [{"season": 1, "episode": 3}])
        serialized = repr(result.data)
        self.assertNotIn("http://", serialized)
        self.assertNotIn("token", serialized.casefold())
        self.assertNotIn("series-internal", serialized)
        self.assertEqual(client.detail.call_args.kwargs["retries"], 0)
        self.assertIn("deadline_at", client.detail.call_args.kwargs)

    def test_manual_patrol_strictly_maps_unmapped_series_by_title_and_year(self):
        from app.agent.library_episode_audit import audit_library_episodes

        sources = [{
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "status": "ready",
            "series_total": 1,
            "series_enumerated": 1,
            "truncated": False,
            "deadline_exhausted": False,
            "unmapped_count": 1,
            "series": [{
                "name": "示例剧",
                "year": "2026",
                "tmdb_id": "",
                "episodes": [(1, 1)],
                "local_total": 1,
                "truncated": False,
                "ignored_specials": 0,
                "ignored_unknown": 0,
            }],
        }]
        client = MagicMock()
        client.search.return_value = [{
            "id": 12345,
            "name": "示例剧",
            "original_name": "Example Show",
            "first_air_date": "2026-01-02",
        }]
        client.detail.return_value = {
            "id": 12345,
            "name": "示例剧",
            "original_name": "Example Show",
            "first_air_date": "2026-01-02",
            "seasons": [{"season_number": 1}],
        }
        client.tv_season_detail.return_value = {"episodes": [
            {"episode_number": 1, "air_date": "2026-01-02"},
            {"episode_number": 2, "air_date": "2026-01-09"},
        ]}
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=sources,
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
            result = audit_library_episodes({"as_of": "2026-08-11", "max_series": 50})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "updates_available")
        self.assertEqual(result.data["mapping_fallback"], {
            "attempted": 1,
            "resolved": 1,
            "ambiguous": 0,
            "unmatched": 0,
        })
        self.assertEqual(result.data["unmapped_series_count"], 0)
        self.assertEqual(result.data["mapped_series_count"], 1)
        self.assertEqual(result.data["missing_episode_count"], 1)
        self.assertEqual(
            result.data["findings"][0]["missing_sample"],
            [{"season": 1, "episode": 2}],
        )
        client.search.assert_called_once_with(
            "示例剧", "2026", "tv", deadline_at=ANY, retries=0
        )
        client.detail.assert_called_once_with(
            "12345", "tv", deadline_at=ANY, retries=0
        )

    def test_manual_patrol_rejects_ambiguous_title_year_mapping(self):
        from app.agent.library_episode_audit import audit_library_episodes

        sources = [{
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "status": "ready",
            "series_total": 1,
            "series_enumerated": 1,
            "truncated": False,
            "deadline_exhausted": False,
            "unmapped_count": 1,
            "series": [{
                "name": "同名剧",
                "year": "2026",
                "tmdb_id": "",
                "episodes": [(1, 1)],
                "local_total": 1,
                "truncated": False,
            }],
        }]
        client = MagicMock()
        client.search.return_value = [
            {"id": 1, "name": "同名剧", "first_air_date": "2026-01-01"},
            {"id": 2, "name": "同名剧", "first_air_date": "2026-02-01"},
        ]
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=sources,
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
            result = audit_library_episodes({"as_of": "2026-08-11", "max_series": 50})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.data["mapping_fallback"]["ambiguous"], 1)
        self.assertEqual(result.data["local_series_count"], 1)
        self.assertEqual(result.data["local_episode_count"], 1)
        self.assertEqual(result.data["comparison_eligible_count"], 0)
        self.assertEqual(result.data["unmapped_series_count"], 1)
        self.assertIn("已从 Jellyfin / Emby 读取 1 部本地剧集", result.summary)
        self.assertIn("缺少可靠 TMDB 映射", result.summary)
        client.detail.assert_not_called()
        client.tv_season_detail.assert_not_called()

    def test_manual_patrol_rejects_title_match_with_wrong_year(self):
        from app.agent.library_episode_audit import audit_library_episodes

        sources = [{
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "status": "ready",
            "series_total": 1,
            "series_enumerated": 1,
            "truncated": False,
            "deadline_exhausted": False,
            "unmapped_count": 1,
            "series": [{
                "name": "示例剧",
                "year": "2026",
                "tmdb_id": "",
                "episodes": [(1, 1)],
                "local_total": 1,
                "truncated": False,
            }],
        }]
        client = MagicMock()
        client.search.return_value = [{
            "id": 12345,
            "name": "示例剧",
            "first_air_date": "2025-01-01",
        }]
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=sources,
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
            result = audit_library_episodes({"as_of": "2026-08-11", "max_series": 50})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.data["mapping_fallback"]["unmatched"], 1)
        self.assertEqual(result.data["local_series_count"], 1)
        self.assertEqual(result.data["comparison_eligible_count"], 0)
        self.assertEqual(result.data["unmapped_series_count"], 1)
        self.assertNotIn("媒体库中没有", result.summary)
        client.detail.assert_not_called()
        client.tv_season_detail.assert_not_called()

    def test_ready_media_server_without_series_reports_empty_inventory(self):
        from app.agent.library_episode_audit import audit_library_episodes

        sources = [{
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "status": "ready",
            "series_total": 0,
            "series_enumerated": 0,
            "truncated": False,
            "deadline_exhausted": False,
            "unmapped_count": 0,
            "series": [],
        }]
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=sources,
        ), patch(
            "app.agent.library_episode_audit.TMDBClient", return_value=MagicMock()
        ):
            result = audit_library_episodes({"as_of": "2026-08-11", "max_series": 50})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.data["local_series_count"], 0)
        self.assertEqual(result.data["comparison_eligible_count"], 0)
        self.assertIn("没有读取到剧集条目", result.summary)

    def test_unknown_air_dates_make_the_series_inconclusive(self):
        from app.agent.library_episode_audit import audit_library_episodes

        client = self._tmdb_client()
        client.tv_season_detail.side_effect = lambda _tmdb_id, _season, **_kwargs: {
            "episodes": [
                {"episode_number": 1, "air_date": "2026-01-01"},
                {"episode_number": 2, "air_date": None},
            ]
        }
        sources = self._sources()[:1]
        sources[0]["series"] = [sources[0]["series"][1]]
        sources[0]["series_total"] = 1
        sources[0]["series_enumerated"] = 1
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=sources,
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
            result = audit_library_episodes({"as_of": "2026-08-03", "max_series": 50})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.data["unknown_air_date_count"], 1)
        self.assertEqual(result.data["inconclusive_count"], 1)
        self.assertEqual(result.data["findings"][0]["status"], "inconclusive")

    def test_unmapped_or_truncated_sources_make_report_inconclusive(self):
        from app.agent.library_episode_audit import audit_library_episodes

        sources = self._sources()[:1]
        sources[0]["unmapped_count"] = 1
        sources[0]["truncated"] = True
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=sources,
        ), patch(
            "app.agent.library_episode_audit.TMDBClient", return_value=self._tmdb_client()
        ):
            result = audit_library_episodes({"as_of": "2026-08-03", "max_series": 50})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.data["unmapped_series_count"], 1)
        self.assertTrue(result.data["sources"][0]["truncated"])
        self.assertEqual(result.data["updates_available_count"], 1)

    def test_missing_tmdb_seasons_or_episodes_is_inconclusive(self):
        from app.agent.library_episode_audit import audit_library_episodes

        sources = self._sources()[:1]
        sources[0]["series"] = [sources[0]["series"][1]]
        sources[0]["series_total"] = 1
        sources[0]["series_enumerated"] = 1

        for detail_payload, season_payload in (
            ({"name": "已完整剧"}, {"episodes": [{"episode_number": 1, "air_date": "2025-01-01"}]}),
            ({"name": "已完整剧", "seasons": [{"season_number": 1}]}, {}),
        ):
            with self.subTest(detail=detail_payload, season=season_payload):
                client = MagicMock()
                client.detail.return_value = detail_payload
                client.tv_season_detail.return_value = season_payload
                with patch(
                    "app.agent.library_episode_audit.inspect_library_series_sources",
                    return_value=sources,
                ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
                    result = audit_library_episodes({"as_of": "2026-08-03", "max_series": 50})

                self.assertFalse(result.ok)
                self.assertEqual(result.status, "inconclusive")
                self.assertEqual(result.data["up_to_date_count"], 0)
                self.assertEqual(result.data["inconclusive_count"], 1)

    def test_tmdb_request_budget_stops_patrol(self):
        from app.agent.library_episode_audit import audit_library_episodes

        sources = self._sources()[:1]
        sources[0]["series"] = [sources[0]["series"][1]]
        sources[0]["series_total"] = 1
        sources[0]["series_enumerated"] = 1
        client = self._tmdb_client()
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=sources,
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client), patch(
            "app.agent.library_episode_audit._MAX_TMDB_REQUESTS", 1
        ):
            result = audit_library_episodes({"as_of": "2026-08-03", "max_series": 50})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "inconclusive")
        self.assertTrue(result.data["request_budget_exhausted"])
        self.assertEqual(result.data["tmdb_request_budget"], 1)
        self.assertEqual(result.data["tmdb_requests_used"], 1)
        self.assertEqual(client.detail.call_count, 1)
        client.tv_season_detail.assert_not_called()

    def test_max_series_is_a_global_cross_server_cap(self):
        from app.agent.library_episode_audit import audit_library_episodes

        client = self._tmdb_client()
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=self._sources(),
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
            result = audit_library_episodes({"as_of": "2026-08-03", "max_series": 1})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.data["checked_series_count"], 1)
        self.assertEqual(client.detail.call_count, 1)

    def test_background_batch_processes_duplicate_tmdb_group_before_next_boundary(self):
        from app.agent.library_episode_audit import audit_library_episodes_batch

        media_client = MagicMock()
        media_client.list_library_series.return_value = SeriesSearchResult(
            candidates=[
                SeriesCandidate("id-10-a", "Ten A", "2026", "10"),
                SeriesCandidate("id-10-b", "Ten B", "2026", "10"),
                SeriesCandidate("id-20", "Twenty", "2026", "20"),
            ],
            total=3,
            truncated=False,
        )
        media_client.list_series_episode_inventory.return_value = (
            SeriesEpisodeInventory(
                episodes=[(1, 1)],
                total=1,
                truncated=False,
            )
        )
        tmdb_client = MagicMock()
        tmdb_client.detail.return_value = {
            "name": "Ten",
            "seasons": [{"season_number": 1}],
        }
        tmdb_client.tv_season_detail.return_value = {
            "episodes": [{"episode_number": 1, "air_date": "2026-01-01"}],
        }

        with patch(
            "app.services._configured_media_sources",
            return_value=[
                ("jellyfin", "Jellyfin", "http://secret.local", media_client)
            ],
        ), patch(
            "app.agent.library_episode_audit.TMDBClient",
            return_value=tmdb_client,
        ):
            result = audit_library_episodes_batch({
                "as_of": "2026-08-06",
                "max_series": 1,
            })

        self.assertTrue(result.ok)
        self.assertTrue(result.data["continuation_pending"])
        self.assertEqual(result.data["checked_series_count"], 1)
        self.assertEqual(result.data["last_processed_tmdb_id"], "10")
        self.assertEqual(result.data["stalled_tmdb_id"], "20")
        tmdb_client.detail.assert_called_once()
        self.assertEqual(tmdb_client.detail.call_args.args[0], "10")
        self.assertEqual(
            [call.args[0] for call in media_client.list_series_episode_inventory.call_args_list],
            ["id-10-a", "id-10-b"],
        )

    def test_background_batch_returns_cursor_and_resumes_after_request_budget(self):
        from app.agent.library_episode_audit import audit_library_episodes_batch

        client = self._tmdb_client()
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=self._sources(),
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client), patch(
            "app.agent.library_episode_audit._MAX_TMDB_REQUESTS", 2
        ):
            first = audit_library_episodes_batch({
                "as_of": "2026-08-03",
                "max_series": 50,
            })

        self.assertTrue(first.ok)
        self.assertTrue(first.data["continuation_pending"])
        self.assertEqual(first.data["checked_series_count"], 1)
        self.assertEqual(first.data["last_processed_tmdb_id"], "12345")
        self.assertEqual(first.data["stalled_tmdb_id"], "67890")
        self.assertEqual(first.data["inconclusive_count"], 0)

        client = self._tmdb_client()
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=self._sources(),
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
            second = audit_library_episodes_batch({
                "as_of": "2026-08-03",
                "max_series": 50,
                "after_tmdb_id": first.data["last_processed_tmdb_id"],
            })

        self.assertFalse(second.data["continuation_pending"])
        self.assertEqual(second.data["checked_series_count"], 1)
        self.assertEqual(second.data["last_processed_tmdb_id"], "67890")
        client.detail.assert_called_once()
        self.assertEqual(client.detail.call_args.args[0], "67890")

    def test_background_batch_retries_first_series_when_inventory_times_out(self):
        from app.agent.library_episode_audit import audit_library_episodes_batch

        sources = [{
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "status": "incomplete",
            "series_total": 1,
            "series_enumerated": 1,
            "truncated": True,
            "catalog_truncated": False,
            "batch_remaining": True,
            "next_tmdb_id": "20",
            "deadline_exhausted": True,
            "unmapped_count": 0,
            "series": [],
        }]
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=sources,
        ):
            result = audit_library_episodes_batch({
                "as_of": "2026-08-06",
                "max_series": 50,
            })

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "inconclusive")
        self.assertTrue(result.data["continuation_pending"])
        self.assertEqual(result.data["last_processed_tmdb_id"], "")
        self.assertEqual(result.data["stalled_tmdb_id"], "20")

    def test_background_batch_respects_ready_source_batch_boundary(self):
        from app.agent.library_episode_audit import audit_library_episodes_batch

        lower_source = {
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "status": "ready",
            "series_total": 3,
            "series_enumerated": 3,
            "truncated": False,
            "catalog_truncated": False,
            "batch_remaining": True,
            "next_tmdb_id": "20",
            "deadline_exhausted": False,
            "unmapped_count": 0,
            "series": [
                {
                    "name": "Ten A",
                    "year": "2026",
                    "tmdb_id": "10",
                    "episodes": [(1, 1)],
                    "local_total": 1,
                    "truncated": False,
                    "ignored_specials": 0,
                    "ignored_unknown": 0,
                },
                {
                    "name": "Ten B",
                    "year": "2026",
                    "tmdb_id": "10",
                    "episodes": [(1, 2)],
                    "local_total": 1,
                    "truncated": False,
                    "ignored_specials": 0,
                    "ignored_unknown": 0,
                },
            ],
        }
        higher_source = {
            "server_type": "emby",
            "server_name": "Emby",
            "status": "ready",
            "series_total": 2,
            "series_enumerated": 2,
            "truncated": False,
            "catalog_truncated": False,
            "batch_remaining": False,
            "next_tmdb_id": "",
            "deadline_exhausted": False,
            "unmapped_count": 0,
            "series": [
                {
                    "name": "One Hundred",
                    "year": "2026",
                    "tmdb_id": "100",
                    "episodes": [(1, 1)],
                    "local_total": 1,
                    "truncated": False,
                    "ignored_specials": 0,
                    "ignored_unknown": 0,
                },
                {
                    "name": "One Hundred One",
                    "year": "2026",
                    "tmdb_id": "101",
                    "episodes": [(1, 1)],
                    "local_total": 1,
                    "truncated": False,
                    "ignored_specials": 0,
                    "ignored_unknown": 0,
                },
            ],
        }
        client = self._tmdb_client()
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=[lower_source, higher_source],
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
            result = audit_library_episodes_batch({
                "as_of": "2026-08-06",
                "max_series": 2,
            })

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "inconclusive")
        self.assertTrue(result.data["continuation_pending"])
        self.assertEqual(result.data["checked_series_count"], 1)
        self.assertEqual(result.data["last_processed_tmdb_id"], "10")
        self.assertEqual(result.data["stalled_tmdb_id"], "20")
        client.detail.assert_called_once()
        self.assertEqual(client.detail.call_args.args[0], "10")

    def test_background_batch_does_not_advance_past_lower_cross_source_stall(self):
        from app.agent.library_episode_audit import audit_library_episodes_batch

        stalled_source = {
            "server_type": "jellyfin",
            "server_name": "Jellyfin",
            "status": "incomplete",
            "series_total": 1,
            "series_enumerated": 1,
            "truncated": True,
            "catalog_truncated": False,
            "batch_remaining": True,
            "next_tmdb_id": "10",
            "deadline_exhausted": True,
            "unmapped_count": 0,
            "series": [],
        }
        successful_source = {
            "server_type": "emby",
            "server_name": "Emby",
            "status": "ready",
            "series_total": 1,
            "series_enumerated": 1,
            "truncated": False,
            "catalog_truncated": False,
            "batch_remaining": False,
            "next_tmdb_id": "",
            "deadline_exhausted": False,
            "unmapped_count": 0,
            "series": [{
                "name": "Higher ID",
                "year": "2026",
                "tmdb_id": "20",
                "episodes": [(1, 1)],
                "local_total": 1,
                "truncated": False,
                "ignored_specials": 0,
                "ignored_unknown": 0,
            }],
        }
        client = self._tmdb_client()
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=[stalled_source, successful_source],
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
            result = audit_library_episodes_batch({
                "as_of": "2026-08-06",
                "max_series": 50,
            })

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "inconclusive")
        self.assertTrue(result.data["continuation_pending"])
        self.assertEqual(result.data["checked_series_count"], 0)
        self.assertEqual(result.data["last_processed_tmdb_id"], "")
        self.assertEqual(result.data["stalled_tmdb_id"], "10")
        client.detail.assert_not_called()

    def test_background_batch_processes_only_ids_below_cross_source_stall(self):
        from app.agent.library_episode_audit import audit_library_episodes_batch

        sources = self._sources()
        sources[0].update({
            "status": "incomplete",
            "truncated": True,
            "catalog_truncated": False,
            "batch_remaining": True,
            "next_tmdb_id": "67890",
            "deadline_exhausted": True,
        })
        client = self._tmdb_client()
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=sources,
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
            result = audit_library_episodes_batch({
                "as_of": "2026-08-06",
                "max_series": 50,
            })

        self.assertTrue(result.ok)
        self.assertTrue(result.data["continuation_pending"])
        self.assertEqual(result.data["checked_series_count"], 1)
        self.assertEqual(result.data["last_processed_tmdb_id"], "12345")
        self.assertEqual(result.data["stalled_tmdb_id"], "67890")
        client.detail.assert_called_once()
        self.assertEqual(client.detail.call_args.args[0], "12345")

    def test_deadline_exhaustion_stops_before_tmdb_requests(self):
        from app.agent.library_episode_audit import audit_library_episodes

        client = self._tmdb_client()
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=self._sources(),
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client), patch(
            "app.agent.library_episode_audit.time.monotonic", side_effect=[0.0, 31.0]
        ):
            result = audit_library_episodes({"as_of": "2026-08-03", "max_series": 50})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "inconclusive")
        self.assertTrue(result.data["deadline_exhausted"])
        client.detail.assert_not_called()




class MediaLibraryEpisodeAuditClientLifecycleTests(unittest.TestCase):
    def test_owned_tmdb_client_is_closed_after_audit(self) -> None:
        from app.agent.library_episode_audit import audit_library_episodes

        owner = MediaLibraryEpisodeAuditTests()
        client = owner._tmdb_client()
        with patch(
            "app.agent.library_episode_audit.inspect_library_series_sources",
            return_value=owner._sources(),
        ), patch("app.agent.library_episode_audit.TMDBClient", return_value=client):
            result = audit_library_episodes({
                "as_of": "2026-08-03",
                "max_series": 50,
            })

        self.assertTrue(result.ok)
        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
