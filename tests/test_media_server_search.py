"""Jellyfin / Emby 本地媒体搜索与最近入库映射。"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient

_ITEM_ID = "a" * 32


def typed_pages(rows: list[dict]):
    """模拟真实服务器：只返回所请求类型，按 StartIndex/Limit 分页；rows 已按入库时间倒序。"""
    def page(_path, *, params):
        typed = [row for row in rows if row["Type"] in params["IncludeItemTypes"].split(",")]
        offset = params.get("StartIndex", 0)
        return {"Items": typed[offset:offset + params["Limit"]]}
    return page


def item_queries(request) -> list:
    return [call for call in request.call_args_list if "IncludeItemTypes" in call.kwargs.get("params", {})]


def type_pages(request) -> list[tuple[str, int, int]]:
    return [(call.kwargs["params"]["IncludeItemTypes"], call.kwargs["params"].get("StartIndex", 0),
             call.kwargs["params"]["Limit"]) for call in item_queries(request)]


class MediaServerSearchTests(unittest.TestCase):
    @staticmethod
    def _payload() -> dict:
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
        assert (method) is not None, "JellyfinClient 缺少 search_media"
        with patch.object(client, "_request", return_value=self._payload()) as request:
            items = method("Dune", limit=8).items

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
        self.assertEqual(item.series_id, series_id)
        self.assertIn(f"details?id={series_id}", item.series_web_url)

    def test_emby_episode_exposes_series_identity_and_link(self):
        client = EmbyClient("http://emby.example", "token")
        series_id = "b" * 32

        item = client._media_item({
            "Id": _ITEM_ID,
            "Name": "Episode",
            "Type": "Episode",
            "SeriesId": series_id,
            "SeriesName": "Example Series",
        })

        self.assertEqual(item.series_id, series_id)
        self.assertIn(f"id={series_id}", item.series_web_url)
        self.assertEqual(item.primary_image, f"/media-image/emby/{series_id}")

    def test_series_items_use_their_own_identity_as_series_identity(self):
        for client in (
            JellyfinClient("http://jellyfin.example", "token"),
            EmbyClient("http://emby.example", "token"),
        ):
            with self.subTest(client=type(client).__name__):
                item = client._media_item({
                    "Id": _ITEM_ID,
                    "Name": "Example Series",
                    "Type": "Series",
                })
                self.assertEqual(item.series_id, _ITEM_ID)
                self.assertIn(f"id={_ITEM_ID}", item.series_web_url)

    def test_emby_search_media_maps_images_and_external_details(self):
        client = EmbyClient("http://emby.example", "token")
        client._cached_user_id = "user-1"
        method = getattr(client, "search_media", None)
        assert (method) is not None, "EmbyClient 缺少 search_media"
        with patch.object(client, "_request", return_value=self._payload()) as request:
            items = method("Dune", limit=6).items

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
                items = client.search_media("Dune", limit=6).items
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
                    items = client.search_media("Dune", limit=6).items
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
                    items = client.search_media("Dune", limit=6).items
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
                items = client.search_media("Dune", limit=6).items
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
            assert (method) is not None, f"{type(client).__name__} 缺少 recent_media"
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

    def test_recent_media_filters_library_and_type_before_fetching(self):
        for client_type in (JellyfinClient, EmbyClient):
            for media_type, item_types in (("all", "Movie,Series,Episode"), ("tv", "Series,Episode"), ("movie", "Movie"), ("episode", "Episode")):
                with self.subTest(client=client_type.__name__, media_type=media_type):
                    client = client_type("http://media.invalid", "token")
                    self.addCleanup(client.close)
                    client._cached_user_id = "user-1"
                    with patch.object(client, "_request", return_value={"Items": []}) as request:
                        self.assertEqual(client.recent_media(limit=5, parent_id="tv-library", media_type=media_type), [])
                    # 多类型合并排序在 Jellyfin 12 上要数秒，必须逐类型查询。
                    self.assertEqual([kind for kind, _start, _limit in type_pages(request)], item_types.split(","))
                    for call in item_queries(request):
                        params = call.kwargs["params"]
                        self.assertEqual(params["ParentId"], "tv-library")
                        self.assertEqual(params["SortBy"], "DateCreated")
                        self.assertEqual(params["Recursive"], "true")
                        self.assertEqual("SeriesPrimaryImageTag" in params["Fields"], client_type is JellyfinClient)

    def test_recent_media_episode_granularity_keeps_consecutive_episodes_by_id(self):
        rows = [
            {"Id": f"episode-{number}", "Name": f"第{number}集", "Type": "Episode",
             "SeriesId": "series-a", "SeriesName": "黑镜", "ParentIndexNumber": 7,
             "IndexNumber": number, "DateCreated": f"2026-10-16T12:00:0{number}Z"}
            for number in (4, 6, 5)
        ]
        rows.append({**rows[1], "DateCreated": "2026-10-16T12:00:03Z"})
        for client_type in (JellyfinClient, EmbyClient):
            with self.subTest(client=client_type.__name__), client_type("http://media.invalid", "token") as client:
                client._cached_user_id = "user-1"
                if isinstance(client, EmbyClient):
                    client._cached_server_info = {"ProductName": "Emby", "Version": "4.9"}
                with patch.object(client, "_request", return_value={"Items": rows}) as request:
                    items = client.recent_media(limit=3, parent_id="tv-library", media_type="episode")
                self.assertEqual([item.id for item in items], ["episode-6", "episode-5", "episode-4"])
                self.assertEqual([item.series_id for item in items], ["series-a"] * 3)
                self.assertEqual([(item.season_number, item.episode_number) for item in items], [(7, 6), (7, 5), (7, 4)])
                self.assertEqual(request.call_count, 1)
                params = request.call_args.kwargs["params"]
                self.assertEqual(params["ParentId"], "tv-library")
                self.assertEqual(params["IncludeItemTypes"], "Episode")
                self.assertEqual(params["SortBy"], "DateCreated")
                self.assertEqual(params["SortOrder"], "Descending")
                with patch.object(client, "_request", return_value={"Items": rows}):
                    items = client.recent_media(limit=4, media_type="episode")
                self.assertEqual([item.id for item in items], ["episode-6", "episode-5", "episode-4"])

    def test_recent_media_fills_distinct_results_across_episode_pages(self):
        rows = [
            {"Id": f"episode-{i}", "Name": f"第{i}集", "Type": "Episode",
             "SeriesId": "series-a", "SeriesName": "测试剧甲",
             "DateCreated": f"2026-10-06T12:00:{59-i:02}Z"}
            for i in range(30)
        ] + [{"Id": "series-b", "Name": "测试剧乙", "Type": "Series",
              "DateCreated": "2026-10-05T12:00:00Z"}]
        for client_type in (JellyfinClient, EmbyClient):
            with self.subTest(client=client_type.__name__), client_type("http://media.invalid", "token") as client:
                client._cached_user_id = "user-1"
                if isinstance(client, EmbyClient):
                    client._cached_server_info = {"ProductName": "Emby", "Version": "4.9"}
                with patch.object(client, "_request", side_effect=typed_pages(rows)) as request:
                    items = client.recent_media(limit=2, parent_id="tv-library", media_type="tv")
                self.assertEqual([item.name for item in items], ["第0集", "测试剧乙"])
                self.assertEqual(type_pages(request), [("Series", 0, 30), ("Episode", 0, 30), ("Episode", 30, 200)])
                for call in request.call_args_list:
                    self.assertEqual(call.kwargs["params"]["ParentId"], "tv-library")

    def test_recent_media_movie_granularity_keeps_distinct_work_ids(self):
        rows = [
            {"Id": movie_id, "Name": "同名电影", "Type": "Movie", "DateCreated": date}
            for movie_id, date in (
                ("movie-a", "2026-10-16T12:00:06Z"),
                ("movie-a", "2026-10-16T12:00:05Z"),
                ("movie-b", "2026-10-16T12:00:04Z"),
            )
        ]
        for client_type in (JellyfinClient, EmbyClient):
            with self.subTest(client=client_type.__name__), client_type("http://media.invalid", "token") as client:
                client._cached_user_id = "user-1"
                if isinstance(client, EmbyClient):
                    client._cached_server_info = {"ProductName": "Emby", "Version": "4.9"}
                with patch.object(client, "_request", return_value={"Items": rows}):
                    items = client.recent_media(limit=3, media_type="movie")
                self.assertEqual([item.id for item in items], ["movie-a", "movie-b"])
                self.assertEqual([item.date_added for item in items], [rows[0]["DateCreated"], rows[2]["DateCreated"]])

    def test_recent_media_merges_series_and_episode_using_newest_item(self):
        payload = {"Items": [
            {"Id": "series-a", "Name": "测试剧甲", "Type": "Series",
             "DateCreated": "2026-10-11T12:00:00Z"},
            {"Id": "episode-b", "Name": "旧单集", "Type": "Episode", "SeriesId": "series-b",
             "SeriesName": "测试剧乙", "DateCreated": "2026-10-10T12:00:00Z"},
            {"Id": "series-b", "Name": "测试剧乙", "Type": "Series",
             "DateCreated": "2026-10-12T12:00:00Z"},
            {"Id": "episode-a", "Name": "新单集", "Type": "Episode", "SeriesId": "series-a",
             "SeriesName": "测试剧甲", "DateCreated": "2026-10-13T12:00:00Z"},
        ]}
        for client_type in (JellyfinClient, EmbyClient):
            for media_type in ("all", "tv"):
                with self.subTest(client=client_type.__name__, media_type=media_type), client_type("http://media.invalid", "token") as client:
                    client._cached_user_id = "user-1"
                    if isinstance(client, EmbyClient):
                        client._cached_server_info = {"ProductName": "Emby", "Version": "4.9"}
                    with patch.object(client, "_request", return_value=payload):
                        items = client.recent_media(limit=5, media_type=media_type)
                    self.assertEqual(
                        [(item.id, item.series_id, item.type, item.date_added) for item in items],
                        [("episode-a", "series-a", "Episode", "2026-10-13T12:00:00Z"),
                         ("series-b", "series-b", "Series", "2026-10-12T12:00:00Z")],
                    )

    def test_recent_media_fills_distinct_results_across_mixed_series_pages(self):
        rows = [
            {"Id": "episode-a", "Name": "单集甲", "Type": "Episode", "SeriesId": "series-a",
             "DateCreated": "2026-10-13T12:00:00Z"},
            {"Id": "series-b", "Name": "测试剧乙", "Type": "Series",
             "DateCreated": "2026-10-12T12:00:00Z"},
            {"Id": "episode-c", "Name": "单集丙", "Type": "Episode", "SeriesId": "series-c",
             "DateCreated": "2026-10-11T12:00:00Z"},
            {"Id": "episode-d", "Name": "单集丁", "Type": "Episode", "SeriesId": "series-d",
             "DateCreated": "2026-10-10T12:00:00Z"},
            {"Id": "series-d", "Name": "测试剧丁", "Type": "Series",
             "DateCreated": "2026-10-09T12:00:00Z"},
        ] + [
            {"Id": f"episode-d-{i}", "Name": f"旧单集{i}", "Type": "Episode", "SeriesId": "series-d",
             "DateCreated": f"2026-10-08T12:00:{59-i:02}Z"}
            for i in range(30)
        ] + [{"Id": "series-e", "Name": "测试剧戊", "Type": "Series",
              "DateCreated": "2026-10-07T12:00:00Z"}]
        for client_type in (JellyfinClient, EmbyClient):
            with self.subTest(client=client_type.__name__), client_type("http://media.invalid", "token") as client:
                client._cached_user_id = "user-1"
                if isinstance(client, EmbyClient):
                    client._cached_server_info = {"ProductName": "Emby", "Version": "4.9"}
                with patch.object(client, "_request", side_effect=typed_pages(rows)) as request:
                    items = client.recent_media(limit=5, parent_id="tv-library", media_type="tv")
                self.assertEqual([item.series_id for item in items], ["series-a", "series-b", "series-c", "series-d", "series-e"])
                self.assertEqual([item.id for item in items], ["episode-a", "series-b", "episode-c", "episode-d", "series-e"])
                self.assertEqual(type_pages(request), [("Series", 0, 30), ("Episode", 0, 30), ("Episode", 30, 200)])
                for call in request.call_args_list:
                    self.assertEqual(call.args[0], "/Users/user-1/Items")
                    self.assertEqual(call.kwargs["params"]["ParentId"], "tv-library")
                    self.assertEqual(call.kwargs["params"]["SortBy"], "DateCreated")
                    self.assertEqual(call.kwargs["params"]["SortOrder"], "Descending")

    def test_recent_media_keeps_same_named_series_with_distinct_ids(self):
        payload = {"Items": [
            {"Id": "series-a", "Name": "同名剧", "Type": "Series",
             "DateCreated": "2026-10-13T12:00:00Z"},
            {"Id": "series-b", "Name": "同名剧", "Type": "Series",
             "DateCreated": "2026-10-12T12:00:00Z"},
            {"Id": "episode-c", "Name": "第1集", "Type": "Episode", "SeriesId": "series-c",
             "SeriesName": "同名剧", "DateCreated": "2026-10-11T12:00:00Z"},
            {"Id": "episode-d", "Name": "第1集", "Type": "Episode", "SeriesId": "series-d",
             "SeriesName": "同名剧", "DateCreated": "2026-10-10T12:00:00Z"},
        ]}
        for client_type in (JellyfinClient, EmbyClient):
            with self.subTest(client=client_type.__name__), client_type("http://media.invalid", "token") as client:
                client._cached_user_id = "user-1"
                if isinstance(client, EmbyClient):
                    client._cached_server_info = {"ProductName": "Emby", "Version": "4.9"}
                with patch.object(client, "_request", return_value=payload):
                    items = client.recent_media(limit=5)
                self.assertEqual([item.id for item in items], ["series-a", "series-b", "episode-c", "episode-d"])
                self.assertEqual([item.series_id for item in items], ["series-a", "series-b", "series-c", "series-d"])

    def test_recent_media_deduplicates_episodes_without_series_id_by_item_id(self):
        payload = {"Items": [
            {"Id": "series-a", "Name": "同名剧", "Type": "Series",
             "DateCreated": "2026-10-13T12:00:00Z"},
            {"Id": "episode-a", "Name": "第1集", "Type": "Episode", "SeriesName": "同名剧",
             "DateCreated": "2026-10-12T12:00:00Z"},
            {"Id": "episode-b", "Name": "第1集", "Type": "Episode", "SeriesName": "同名剧",
             "SeriesId": "", "DateCreated": "2026-10-11T12:00:00Z"},
            {"Id": "episode-c", "Name": "第1集", "Type": "Episode", "SeriesName": "同名剧",
             "SeriesId": None, "DateCreated": "2026-10-10T12:00:00Z"},
            {"Id": "episode-a", "Name": "旧单集", "Type": "Episode", "SeriesName": "同名剧",
             "DateCreated": "2026-10-09T12:00:00Z"},
        ]}
        for client_type in (JellyfinClient, EmbyClient):
            with self.subTest(client=client_type.__name__), client_type("http://media.invalid", "token") as client:
                client._cached_user_id = "user-1"
                if isinstance(client, EmbyClient):
                    client._cached_server_info = {"ProductName": "Emby", "Version": "4.9"}
                with patch.object(client, "_request", return_value=payload):
                    items = client.recent_media(limit=5)
                self.assertEqual([item.id for item in items], ["series-a", "episode-a", "episode-b", "episode-c"])
                self.assertEqual([item.series_id for item in items], ["series-a", "", "", ""])
                self.assertEqual(items[1].date_added, "2026-10-12T12:00:00Z")

    def test_recent_media_has_bounded_scan_and_surfaces_next_page_failure(self):
        for client_type in (JellyfinClient, EmbyClient):
            with self.subTest(client=client_type.__name__), client_type("http://media.invalid", "token") as client:
                client._cached_user_id = "user-1"
                if isinstance(client, EmbyClient):
                    client._cached_server_info = {"ProductName": "Emby", "Version": "4.9"}
                def page(_path, *, params):
                    if params["IncludeItemTypes"] != "Episode":
                        return {"Items": [{"Id": "series-a", "Name": "同一部剧", "Type": "Series"}]
                                if params["IncludeItemTypes"] == "Series" else []}
                    start = params.get("StartIndex", 0)
                    if start and params.get("fail_next"):
                        raise RuntimeError("page failed")
                    return {"Items": [
                        {"Id": str(i), "Name": "同一部剧", "Type": "Episode", "SeriesId": "series-a"}
                        for i in range(start, start + params["Limit"])
                    ]}
                with patch.object(client, "_request", side_effect=page) as request:
                    self.assertEqual(len(client.recent_media(limit=2)), 1)
                episode_pages = [(start, limit) for kind, start, limit in type_pages(request) if kind == "Episode"]
                self.assertLessEqual(len(episode_pages), 11)
                self.assertEqual(sum(limit for _start, limit in episode_pages), 2000)
                self.assertEqual(len(request.call_args_list), len(episode_pages) + 2)
                def failing(path, *, params):
                    return page(path, params={**params, "fail_next": True})
                with patch.object(client, "_request", side_effect=failing):
                    with self.assertRaisesRegex(RuntimeError, "page failed"):
                        client.recent_media(limit=2)

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

            with self.subTest(client=type(client).__name__, movie=True), patch.object(
                client, "_request", return_value=payload
            ) as request:
                client.has_tmdb_media("12345", "movie", parent_id=" library-1 ")
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
