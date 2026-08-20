"""Jellyfin / Emby 本地媒体搜索与最近入库映射。"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient


_ITEM_ID = "a" * 32


class MediaServerSearchTests(unittest.TestCase):
    @staticmethod
    def _payload():
        return {
            "Items": [{
                "Id": _ITEM_ID,
                "Name": "Dune: Part Two",
                "Type": "Movie",
                "ProductionYear": 2024,
                "DateCreated": "2026-07-29T10:00:00Z",
                "Overview": "overview",
                "ImageTags": {"Primary": "tag"},
                "RunTimeTicks": 7_200_000_000,
                "UserData": {"PlayedPercentage": 42.25},
            }],
            "TotalRecordCount": 1,
        }

    def test_jellyfin_search_media_uses_search_term_and_maps_links(self):
        client = JellyfinClient("http://jellyfin.example", "token")
        client._cached_user_id = "user-1"
        method = getattr(client, "search_media", None)
        self.assertIsNotNone(method, "JellyfinClient 缺少 search_media")
        with patch.object(client, "_request", return_value=self._payload()) as request:
            items = method("Dune", limit=8)

        self.assertEqual(items[0].name, "Dune: Part Two")
        self.assertEqual(
            items[0].primary_image,
            f"/media-image/jellyfin/{_ITEM_ID}?tag=tag",
        )
        self.assertIn(f"details?id={_ITEM_ID}", items[0].web_url)
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["SearchTerm"], "Dune")
        self.assertEqual(params["Limit"], 8)
        self.assertIn("RunTimeTicks", params["Fields"])
        self.assertIn("SeriesPrimaryImageTag", params["Fields"])
        self.assertEqual(items[0].runtime, 12)
        self.assertEqual(items[0].progress, 42.25)

    def test_jellyfin_omits_proxy_url_when_primary_image_is_not_ready(self):
        client = JellyfinClient("http://jellyfin.example", "token")
        item = client._media_item({
            "Id": _ITEM_ID,
            "Name": "Poster Pending",
            "Type": "Movie",
            "ImageTags": {"Thumb": "thumb-only"},
        })

        self.assertEqual(item.primary_image, "")

    def test_jellyfin_episode_prefers_tagged_series_poster(self):
        client = JellyfinClient("http://jellyfin.example", "token")
        series_id = "b" * 32
        item = client._media_item({
            "Id": _ITEM_ID,
            "Name": "Episode",
            "Type": "Episode",
            "SeriesId": series_id,
            "SeriesPrimaryImageTag": "series/tag",
            "ImageTags": {"Primary": "episode-tag"},
        })

        self.assertEqual(
            item.primary_image,
            f"/media-image/jellyfin/{series_id}?tag=series%2Ftag",
        )

    def test_emby_search_media_maps_images_and_external_details(self):
        client = EmbyClient("http://emby.example", "token")
        client._cached_user_id = "user-1"
        method = getattr(client, "search_media", None)
        self.assertIsNotNone(method, "EmbyClient 缺少 search_media")
        with patch.object(client, "_request", return_value=self._payload()) as request:
            items = method("Dune", limit=6)

        self.assertEqual(items[0].primary_image, f"/media-image/emby/{_ITEM_ID}")
        self.assertIn(f"id={_ITEM_ID}", items[0].web_url)
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["SearchTerm"], "Dune")
        self.assertEqual(params["Limit"], 6)
        self.assertIn("RunTimeTicks", params["Fields"])
        self.assertEqual(items[0].runtime, 12)
        self.assertEqual(items[0].progress, 42.25)

    def test_search_media_accepts_legacy_list_payloads(self):
        payload = self._payload()["Items"]
        for client in (
            JellyfinClient("http://jellyfin.example", "token"),
            EmbyClient("http://emby.example", "token"),
        ):
            client._cached_user_id = "user-1"
            with self.subTest(client=type(client).__name__), patch.object(
                client, "_request", return_value=payload
            ):
                items = client.search_media("Dune", limit=6)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].name, "Dune: Part Two")
            self.assertEqual(items[0].runtime, 12)
            self.assertEqual(items[0].progress, 42.25)

    def test_search_media_normalizes_invalid_playback_percentages(self):
        for value, expected in (
            ("invalid", 0.0),
            (float("nan"), 0.0),
            (float("inf"), 0.0),
            (float("-inf"), 0.0),
            (True, 0.0),
            (None, 0.0),
            (-5, 0.0),
            (120, 100.0),
            (42.256, 42.26),
        ):
            payload = self._payload()
            payload["Items"][0]["UserData"]["PlayedPercentage"] = value
            for client in (
                JellyfinClient("http://jellyfin.example", "token"),
                EmbyClient("http://emby.example", "token"),
            ):
                client._cached_user_id = "user-1"
                with self.subTest(value=value, client=type(client).__name__), patch.object(
                    client, "_request", return_value=payload
                ):
                    items = client.search_media("Dune", limit=6)
                self.assertEqual(items[0].progress, expected)

    def test_search_media_normalizes_invalid_runtime_ticks(self):
        for value, expected in (
            ("invalid", 0),
            (float("inf"), 0),
            (float("-inf"), 0),
            (True, 0),
            (None, 0),
            (-5, 0),
            (10**100, 0),
            (1_500_000_000, 3),
        ):
            payload = self._payload()
            payload["Items"][0]["RunTimeTicks"] = value
            for client in (
                JellyfinClient("http://jellyfin.example", "token"),
                EmbyClient("http://emby.example", "token"),
            ):
                client._cached_user_id = "user-1"
                with self.subTest(value=value, client=type(client).__name__), patch.object(
                    client, "_request", return_value=payload
                ):
                    items = client.search_media("Dune", limit=6)
                self.assertEqual(items[0].runtime, expected)

    def test_search_media_tolerates_non_dict_user_data(self):
        payload = self._payload()
        payload["Items"][0]["UserData"] = ["unexpected"]
        for client in (
            JellyfinClient("http://jellyfin.example", "token"),
            EmbyClient("http://emby.example", "token"),
        ):
            client._cached_user_id = "user-1"
            with self.subTest(client=type(client).__name__), patch.object(
                client, "_request", return_value=payload
            ):
                items = client.search_media("Dune", limit=6)
            self.assertEqual(items[0].progress, 0.0)

    def test_search_media_rejects_malformed_top_level_payload(self):
        for client in (
            JellyfinClient("http://jellyfin.example", "token"),
            EmbyClient("http://emby.example", "token"),
        ):
            client._cached_user_id = "user-1"
            for payload in (None, "bad", {"Items": "bad"}):
                with self.subTest(client=type(client).__name__, payload=payload), patch.object(
                    client, "_request", return_value=payload
                ), self.assertRaises(ValueError):
                    client.search_media("Dune", limit=6)

    def test_recent_media_queries_all_accessible_libraries_in_date_order(self):
        for client in (
            JellyfinClient("http://jellyfin.example", "token"),
            EmbyClient("http://emby.example", "token"),
        ):
            client._cached_user_id = "user-1"
            method = getattr(client, "recent_media", None)
            self.assertIsNotNone(method, f"{type(client).__name__} 缺少 recent_media")
            with patch.object(client, "_request", return_value=self._payload()) as request:
                items = method(limit=24)
            self.assertEqual(len(items), 1)
            self.assertEqual(request.call_args.args[0], "/Users/user-1/Items")
            params = request.call_args.kwargs["params"]
            self.assertGreaterEqual(params["Limit"], 24)
            self.assertEqual(params["Recursive"], "true")
            self.assertEqual(params["SortBy"], "DateCreated")
            self.assertEqual(params["SortOrder"], "Descending")
            self.assertNotIn("GroupItems", params)

    def test_recent_media_keeps_newest_episode_per_series(self):
        payload = {"Items": [
            {
                "Id": "1" * 32, "Name": "旧单集", "Type": "Episode",
                "SeriesId": "f" * 32, "SeriesName": "测试剧集",
                "DateCreated": "2026-07-20T10:00:00Z", "IndexNumber": 1,
            },
            {
                "Id": "2" * 32, "Name": "新单集", "Type": "Episode",
                "SeriesId": "f" * 32, "SeriesName": "测试剧集",
                "DateCreated": "2026-07-29T10:00:00Z", "IndexNumber": 2,
            },
            {
                "Id": "3" * 32, "Name": "新电影", "Type": "Movie",
                "DateCreated": "2026-07-28T10:00:00Z",
            },
        ]}
        for client in (
            JellyfinClient("http://jellyfin.example", "token"),
            EmbyClient("http://emby.example", "token"),
        ):
            client._cached_user_id = "user-1"
            with patch.object(client, "_request", return_value=payload):
                items = client.recent_media(limit=8)
            self.assertEqual([item.name for item in items], ["新单集", "新电影"])


    def test_series_candidates_and_episode_inventory_are_compatible_across_clients(self):
        series_payload = {
            "Items": [{
                "Id": "series-internal-id",
                "Name": "The Show",
                "ProductionYear": 2026,
                "ProviderIds": {"Tmdb": "12345"},
            }],
            "TotalRecordCount": 1,
        }
        episode_pages = [
            {
                "Items": [
                    {"ParentIndexNumber": 0, "IndexNumber": 1},
                    {"ParentIndexNumber": 1, "IndexNumber": 1},
                    {"ParentIndexNumber": 1, "IndexNumber": 2},
                    {"ParentIndexNumber": None, "IndexNumber": 3},
                ],
                "TotalRecordCount": 5,
            },
            {
                "Items": [{"ParentIndexNumber": 2, "IndexNumber": 1}],
                "TotalRecordCount": 5,
            },
        ]
        for client in (
            JellyfinClient("http://jellyfin.example", "token"),
            EmbyClient("http://emby.example", "token"),
        ):
            client._cached_user_id = "user-1"
            with self.subTest(client=type(client).__name__):
                with patch.object(client, "_request", return_value=series_payload) as request:
                    result = client.search_series_candidates("The Show", limit=6)
                self.assertEqual(result.total, 1)
                self.assertEqual(result.candidates[0].tmdb_id, "12345")
                self.assertNotIn("token", repr(result))
                params = request.call_args.kwargs["params"]
                self.assertEqual(params["IncludeItemTypes"], "Series")
                self.assertIn("ProviderIds", params["Fields"])

                with patch.object(client, "_request", side_effect=episode_pages) as request:
                    inventory = client.list_series_episode_inventory(
                        "series-internal-id", max_episodes=10, page_size=4
                    )
                self.assertEqual(inventory.episodes, [(1, 1), (1, 2), (2, 1)])
                self.assertEqual(inventory.ignored_specials, 1)
                self.assertEqual(inventory.ignored_unknown, 1)
                self.assertFalse(inventory.truncated)
                self.assertEqual(request.call_count, 2)
                self.assertEqual(request.call_args_list[1].kwargs["params"]["StartIndex"], 4)

    def test_series_search_scopes_requests_with_trimmed_parent_id(self):
        payload = {
            "Items": [{
                "Id": "series-1",
                "Name": "The Show",
                "ProductionYear": 2026,
                "ProviderIds": {"Tmdb": "12345"},
            }],
            "TotalRecordCount": 1,
        }
        for client in (
            JellyfinClient("http://jellyfin.example", "token"),
            EmbyClient("http://emby.example", "token"),
        ):
            client._cached_user_id = "user-1"
            with self.subTest(client=type(client).__name__), patch.object(
                client, "_request", return_value=payload
            ) as request:
                client.search_series_candidates(
                    "The Show", limit=6, parent_id="  library-1  "
                )
            self.assertEqual(
                request.call_args.kwargs["params"]["ParentId"], "library-1"
            )

            with self.subTest(client=type(client).__name__, provider=True), patch.object(
                client, "_request", return_value=payload
            ) as request:
                client.find_series_candidates_by_tmdb(
                    "12345", limit=20, parent_id=" library-1 "
                )
            self.assertEqual(
                request.call_args.kwargs["params"]["ParentId"], "library-1"
            )

            with self.subTest(client=type(client).__name__, unscoped=True), patch.object(
                client, "_request", return_value=payload
            ) as request:
                client.search_series_candidates("The Show", limit=6)
            self.assertNotIn("ParentId", request.call_args.kwargs["params"])

    def test_library_series_enumeration_paginates_and_marks_hard_cap(self):
        client = JellyfinClient("http://jellyfin.example", "token")
        client._cached_user_id = "user-1"
        pages = [
            {
                "Items": [
                    {"Id": "series-1", "Name": "Show One", "ProviderIds": {"Tmdb": "101"}},
                    {"Id": "series-2", "Name": "Show Two", "ProviderIds": {}},
                ],
                "TotalRecordCount": 4,
            },
            {
                "Items": [
                    {"Id": "series-3", "Name": "Show Three", "ProviderIds": {"Tmdb": "303"}},
                ],
                "TotalRecordCount": 4,
            },
        ]
        with patch.object(client, "_request", side_effect=pages) as request:
            result = client.list_library_series(max_series=3, page_size=2)

        self.assertEqual([item.name for item in result.candidates], ["Show One", "Show Two", "Show Three"])
        self.assertEqual(result.total, 4)
        self.assertTrue(result.truncated)
        self.assertEqual(result.candidates[0].tmdb_id, "101")
        self.assertEqual(result.candidates[1].tmdb_id, "")
        self.assertEqual(request.call_args_list[0].kwargs["params"]["StartIndex"], 0)
        self.assertEqual(request.call_args_list[1].kwargs["params"]["StartIndex"], 2)
        self.assertEqual(request.call_args_list[1].kwargs["params"]["Limit"], 1)
        self.assertNotIn("SearchTerm", request.call_args_list[0].kwargs["params"])

    def test_library_series_normalizes_tmdb_id_and_bounds_request_timeout(self):
        client = JellyfinClient("http://jellyfin.example", "token", timeout=10)
        client._cached_user_id = "user-1"
        payload = {
            "Items": [{
                "Id": "series-1",
                "Name": "Show One",
                "ProviderIds": {"Tmdb": "000101"},
            }],
            "TotalRecordCount": 1,
        }
        with patch.object(client, "_request", return_value=payload) as request:
            result = client.list_library_series(
                max_series=1,
                page_size=1,
                deadline_at=time.monotonic() + 1,
            )

        self.assertEqual(result.candidates[0].tmdb_id, "101")
        self.assertGreater(request.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(request.call_args.kwargs["timeout"], 1)

    def test_episode_inventory_marks_hard_cap_as_truncated(self):
        client = JellyfinClient("http://jellyfin.example", "token")
        client._cached_user_id = "user-1"
        payload = {
            "Items": [
                {"ParentIndexNumber": 1, "IndexNumber": 1},
                {"ParentIndexNumber": 1, "IndexNumber": 2},
            ],
            "TotalRecordCount": 10,
        }
        with patch.object(client, "_request", return_value=payload):
            inventory = client.list_series_episode_inventory(
                "series-internal-id", max_episodes=2, page_size=2
            )
        self.assertTrue(inventory.truncated)
        self.assertEqual(inventory.episodes, [(1, 1), (1, 2)])



if __name__ == "__main__":
    unittest.main()
