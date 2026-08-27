"""Jellyfin / Emby 精准局部刷新（Sprint 4）需求驱动测试。

覆盖场景来自实施计划的验收条件：
- Task 4.1 变化路径聚合出最小刷新目标
- Task 4.2 Jellyfin 最深父 Item 刷新与降级
- Task 4.3 Emby 与 Jellyfin 行为契约一致
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.clients.base import MediaLibraryEnumerationTooLarge
from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.modules.media_refresh import MAX_REFRESH_TARGETS, plan_refresh_targets
from app.modules.media_server_path_mapping import MediaServerPathMapping

ROOT = "/data/strm/光鸭云盘"


class RefreshTargetPlanningTests(unittest.TestCase):
    """Task 4.1：变化路径压缩为最小刷新目标。"""

    def test_multiple_episodes_in_one_season_produce_one_target(self):
        plan = plan_refresh_targets(
            [f"{ROOT}/剧集/作品 A/Season 01/E{index:02d}.strm" for index in range(1, 13)],
            media_roots=[ROOT],
        )

        self.assertEqual(plan.targets, (f"{ROOT}/剧集/作品 A/Season 01",))

    def test_different_libraries_are_refreshed_separately(self):
        plan = plan_refresh_targets(
            [
                f"{ROOT}/剧集/作品 A/Season 01/E01.strm",
                f"{ROOT}/电影/作品 B/B.strm",
            ],
            media_roots=[ROOT],
        )

        self.assertEqual(sorted(plan.targets), sorted([
            f"{ROOT}/电影/作品 B", f"{ROOT}/剧集/作品 A/Season 01",
        ]))

    def test_sibling_seasons_remain_precise_until_item_id_dedupe(self):
        plan = plan_refresh_targets(
            [
                f"{ROOT}/剧集/作品 A/Season 01/E01.strm",
                f"{ROOT}/剧集/作品 A/Season 02/E01.strm",
            ],
            media_roots=[ROOT],
        )

        self.assertEqual(plan.targets, (
            f"{ROOT}/剧集/作品 A/Season 01",
            f"{ROOT}/剧集/作品 A/Season 02",
        ))

    def test_sibling_movies_never_collapse_to_library_root(self):
        plan = plan_refresh_targets(
            [
                f"{ROOT}/电影/作品 A/A.strm",
                f"{ROOT}/电影/作品 B/B.strm",
            ],
            media_roots=[ROOT],
        )

        self.assertEqual(plan.targets, (
            f"{ROOT}/电影/作品 A",
            f"{ROOT}/电影/作品 B",
        ))

    def test_descendants_of_a_selected_ancestor_are_dropped(self):
        plan = plan_refresh_targets(
            changed_dirs=[
                f"{ROOT}/剧集/作品 A",
                f"{ROOT}/剧集/作品 A/Season 01",
                f"{ROOT}/剧集/作品 A/Season 01/Extras",
            ],
            media_roots=[ROOT],
        )

        self.assertEqual(plan.targets, (f"{ROOT}/剧集/作品 A",))

    def test_media_root_itself_is_never_a_refresh_target(self):
        plan = plan_refresh_targets(
            [f"{ROOT}/E01.strm"], media_roots=[ROOT],
        )

        self.assertEqual(plan.targets, ())
        self.assertTrue(plan.reason)

    def test_empty_change_set_reports_a_reason_instead_of_silent_success(self):
        plan = plan_refresh_targets([], [], media_roots=[ROOT])

        self.assertFalse(plan.has_targets)
        self.assertTrue(plan.reason)

    def test_target_overflow_is_batched_without_dropping_targets(self):
        paths = [
            f"{ROOT}/剧集/作品 {index}/Season 01/E01.strm"
            for index in range(MAX_REFRESH_TARGETS + 5)
        ]

        plan = plan_refresh_targets(paths, media_roots=[ROOT])

        self.assertEqual(len(plan.targets), MAX_REFRESH_TARGETS + 5)
        self.assertEqual(plan.omitted, 0)
        self.assertEqual([len(batch) for batch in plan.batches], [MAX_REFRESH_TARGETS, 5])
        self.assertEqual(
            [target for batch in plan.batches for target in batch],
            list(plan.targets),
        )
        self.assertTrue(plan.reason)

    def test_windows_style_separators_are_normalized(self):
        plan = plan_refresh_targets(
            [r"D:\strm\光鸭云盘\剧集\作品 A\Season 01\E01.strm"],
            media_roots=[r"D:\strm\光鸭云盘"],
        )

        self.assertEqual(plan.targets, ("D:/strm/光鸭云盘/剧集/作品 A/Season 01",))


class _RefreshRecorder:
    """记录刷新调用的媒体服务器桩。"""

    def __init__(self, client, folders, items, *, items_error: bool = False):
        self.client = client
        self.folders = folders
        self.items = items
        self.items_error = items_error
        self.refreshed: list[str] = []
        self.refresh_all_calls = 0
        client.list_virtual_folders = lambda: self.folders
        client._library_items_with_paths = self._library_items
        client._library_root_items_with_paths = self._library_roots
        client.refresh_library = self._refresh_library
        client.refresh_all = self._refresh_all

    def _library_items(self, library_id: str):
        if self.items_error:
            raise RuntimeError("接口失败")
        return self.items.get(str(library_id), [])

    def _library_roots(
        self, library_id: str, *, locations: tuple[str, ...] = (),
    ) -> list[dict]:
        wanted = {self.client._media_path_key(item) for item in locations}
        return [
            item for item in self.items.get(str(library_id), [])
            if str(item.get("Type") or "").casefold() == "folder"
            and (
                not wanted
                or self.client._media_path_key(item.get("Path")) in wanted
            )
        ]

    def _refresh_library(self, library_id: str) -> bool:
        self.refreshed.append(str(library_id))
        return True

    def _refresh_all(self) -> bool:
        self.refresh_all_calls += 1
        return True


def _folders() -> list[dict]:
    return [
        {"id": "lib-tv", "name": "剧集", "locations": [f"{ROOT}/剧集"]},
        {"id": "lib-movie", "name": "电影", "locations": [f"{ROOT}/电影"]},
    ]


def _items() -> dict[str, list[dict]]:
    return {
        "lib-tv": [
            {"Id": "series-a", "Path": f"{ROOT}/剧集/作品 A"},
            {"Id": "season-a1", "Path": f"{ROOT}/剧集/作品 A/Season 01"},
        ],
        "lib-movie": [{"Id": "movie-b", "Path": f"{ROOT}/电影/作品 B"}],
    }


class MediaServerPreciseRefreshTests(unittest.TestCase):
    """Task 4.2 / 4.3：最深父 Item 刷新与降级策略。"""

    def _clients(self, **kwargs):
        return (
            ("Jellyfin", JellyfinClient("http://jf", "token", **kwargs)),
            ("Emby", EmbyClient("http://emby", "token", **kwargs)),
        )

    @staticmethod
    def _real_item_listing(client, library_id: str):
        from app.clients.base import MediaServerClient

        return MediaServerClient._library_items_with_paths(client, library_id)

    def test_library_item_lookup_paginates_until_target_page(self):
        client = JellyfinClient("http://jf", "token")
        responses = [
            {"Items": [
                {"Id": "series-a", "Path": f"{ROOT}/剧集/作品 A"},
                {"Id": "season-a1", "Path": f"{ROOT}/剧集/作品 A/Season 01"},
            ], "TotalRecordCount": 3},
            {"Items": [
                {"Id": "season-b1", "Path": f"{ROOT}/剧集/作品 B/Season 01"},
            ], "TotalRecordCount": 3},
        ]
        with patch("app.clients.base._MEDIA_ITEM_PAGE_SIZE", 2), patch.object(
            client, "_request", side_effect=responses,
        ) as request:
            items = client._library_items_with_paths("lib-tv")

        self.assertEqual([item["Id"] for item in items], [
            "series-a", "season-a1", "season-b1",
        ])
        self.assertEqual(request.call_args_list[0].kwargs["params"]["StartIndex"], 0)
        self.assertEqual(request.call_args_list[1].kwargs["params"]["StartIndex"], 2)

    def test_library_item_lookup_rejects_repeated_page(self):
        client = JellyfinClient("http://jf", "token")
        repeated = {
            "Items": [{"Id": "series-a", "Path": f"{ROOT}/剧集/作品 A"}],
            "TotalRecordCount": 3,
        }
        with patch("app.clients.base._MEDIA_ITEM_PAGE_SIZE", 1), patch.object(
            client, "_request", side_effect=[repeated, repeated],
        ):
            with self.assertRaisesRegex(RuntimeError, "分页未前进"):
                client._library_items_with_paths("lib-tv")

    def test_library_item_lookup_rejects_malformed_or_truncated_pages(self):
        malformed = (
            {},
            {"Items": "invalid", "TotalRecordCount": 0},
            {"Items": [], "TotalRecordCount": 1},
            {
                "Items": [{"Id": "series-a", "Path": f"{ROOT}/剧集/作品 A"}],
                "TotalRecordCount": 2,
            },
        )
        for response in malformed:
            with self.subTest(response=response):
                client = JellyfinClient("http://jf", "token")
                with patch.object(client, "_request", return_value=response):
                    with self.assertRaises(RuntimeError):
                        client._library_items_with_paths("lib-tv")

    def test_library_item_lookup_rejects_total_changes_between_pages(self):
        client = JellyfinClient("http://jf", "token")
        responses = [
            {
                "Items": [{"Id": "a", "Path": f"{ROOT}/剧集/A"}],
                "TotalRecordCount": 3,
            },
            {
                "Items": [{"Id": "b", "Path": f"{ROOT}/剧集/B"}],
                "TotalRecordCount": 2,
            },
        ]
        with patch("app.clients.base._MEDIA_ITEM_PAGE_SIZE", 1), patch.object(
            client, "_request", side_effect=responses,
        ):
            with self.assertRaisesRegex(RuntimeError, "总数发生变化"):
                client._library_items_with_paths("lib-tv")

    def test_library_item_lookup_rejects_page_larger_than_requested_limit(self):
        client = JellyfinClient("http://jf", "token")
        response = {
            "Items": [
                {"Id": "a", "Path": f"{ROOT}/剧集/A"},
                {"Id": "b", "Path": f"{ROOT}/剧集/B"},
            ],
            "TotalRecordCount": 2,
        }
        with patch("app.clients.base._MEDIA_ITEM_PAGE_SIZE", 1), patch.object(
            client, "_request", return_value=response,
        ):
            with self.assertRaisesRegex(RuntimeError, "超过请求上限"):
                client._library_items_with_paths("lib-tv")

    def test_library_item_lookup_rejects_partially_overlapping_pages(self):
        client = JellyfinClient("http://jf", "token")
        responses = [
            {"Items": [
                {"Id": "a", "Path": f"{ROOT}/剧集/A"},
                {"Id": "b", "Path": f"{ROOT}/剧集/B"},
            ], "TotalRecordCount": 4},
            {"Items": [
                {"Id": "b", "Path": f"{ROOT}/剧集/B"},
                {"Id": "c", "Path": f"{ROOT}/剧集/C"},
            ], "TotalRecordCount": 4},
        ]
        with patch("app.clients.base._MEDIA_ITEM_PAGE_SIZE", 2), patch.object(
            client, "_request", side_effect=responses,
        ):
            with self.assertRaisesRegex(RuntimeError, "重复或缺失"):
                client._library_items_with_paths("lib-tv")

    def test_deepest_existing_item_is_refreshed_for_both_servers(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths([f"{ROOT}/剧集/作品 A/Season 01"])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["season-a1"])
                self.assertEqual(recorder.refresh_all_calls, 0)
                self.assertEqual(result["fallback"], "")
                self.assertEqual(result["scope"], "item")
                self.assertEqual(result["endpoints"], ["/Items/season-a1/Refresh"])

    def test_existing_season_is_promoted_to_series_refresh(self):
        items = {"lib-tv": [
            {"Id": "root-tv", "Type": "Folder", "Path": f"{ROOT}/剧集"},
            {"Id": "series-a", "Type": "Series", "Path": f"{ROOT}/剧集/作品 A"},
            {
                "Id": "season-a1", "Type": "Season", "SeriesId": "series-a",
                "Path": f"{ROOT}/剧集/作品 A/Season 01",
            },
        ]}
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), items)

                result = client.refresh_for_paths([
                    f"{ROOT}/剧集/作品 A/Season 01",
                ])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["series-a"])
                self.assertEqual(result["items"], ["series-a"])

    def test_existing_series_wins_over_folder_at_same_season_path(self):
        items = {"lib-tv": [
            {"Id": "root-tv", "Type": "Folder", "Path": f"{ROOT}/剧集"},
            {"Id": "series-a", "Type": "Series", "Path": f"{ROOT}/剧集/作品 A"},
            {
                "Id": "season-folder", "Type": "Folder",
                "Path": f"{ROOT}/剧集/作品 A/Season 01",
            },
        ]}
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), items)

                result = client.refresh_for_paths([
                    f"{ROOT}/剧集/作品 A/Season 01",
                ])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["series-a"])

    def test_new_show_root_scan_suppresses_existing_series_in_same_root(self):
        items = {"lib-tv": [
            {"Id": "root-tv", "Type": "Folder", "Path": f"{ROOT}/剧集"},
            {"Id": "series-a", "Type": "Series", "Path": f"{ROOT}/剧集/作品 A"},
            {
                "Id": "season-a1", "Type": "Season", "SeriesId": "series-a",
                "Path": f"{ROOT}/剧集/作品 A/Season 01",
            },
        ]}
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), items)

                result = client.refresh_for_paths([
                    f"{ROOT}/剧集/作品 A/Season 01",
                    f"{ROOT}/剧集/全新作品/Season 01",
                ])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["root-tv"])
                self.assertEqual(result["items"], [])
                self.assertEqual(result["folders"], ["root-tv"])
                self.assertEqual(result["scope"], "folder")

    def test_existing_movie_file_is_matched_from_changed_parent_directory(self):
        items = {"lib-movie": [
            {"Id": "root-movies", "Type": "Folder", "Path": f"{ROOT}/电影"},
            {
                "Id": "movie-a", "Type": "Movie",
                "Path": f"{ROOT}/电影/作品 A/作品 A.strm",
            },
        ]}
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), items)

                result = client.refresh_for_paths([f"{ROOT}/电影/作品 A"])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["movie-a"])
                self.assertEqual(result["scope"], "item")

    def test_automatic_caller_can_forbid_configured_global_fallback(self):
        client = JellyfinClient(
            "http://jf", "token", allow_global_refresh_fallback=True,
        )
        recorder = _RefreshRecorder(client, _folders(), _items())

        result = client.refresh_for_paths(
            ["/outside/library"], allow_global_fallback=False,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["scope"], "skipped")
        self.assertEqual(recorder.refresh_all_calls, 0)

    def test_recent_target_id_is_resolved_but_not_refreshed_again(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths(
                    [f"{ROOT}/剧集/作品 A/Season 01"],
                    skip_item_ids=("season-a1",),
                )

                self.assertTrue(result["ok"])
                self.assertEqual(result["scope"], "deduplicated")
                self.assertEqual(result["deduplicated"], 1)
                self.assertEqual(recorder.refreshed, [])

    def test_same_item_is_refreshed_once_per_round(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                client.refresh_for_paths([
                    f"{ROOT}/剧集/作品 A/Season 01",
                    f"{ROOT}/剧集/作品 A/Season 01/Extras",
                ])

                self.assertEqual(recorder.refreshed, ["season-a1"])

    def test_many_targets_in_one_library_coalesce_before_item_enumeration(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())
                client._library_items_with_paths = Mock(
                    side_effect=AssertionError("dense target set must not enumerate items")
                )

                result = client.refresh_for_paths([
                    f"{ROOT}/剧集/作品 {index}/Season 01"
                    for index in range(65)
                ])

                client._library_items_with_paths.assert_not_called()
                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["lib-tv"])
                self.assertEqual(result["scope"], "library")
                self.assertIn("多变更媒体库", result["fallback"])

    def test_many_targets_strict_mode_skips_instead_of_library_refresh(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths(
                    [
                        f"{ROOT}/剧集/作品 {index}/Season 01"
                        for index in range(65)
                    ],
                    allowed_library_ids=("lib-tv",),
                    allow_library_fallback=False,
                )

                self.assertFalse(result["ok"])
                self.assertTrue(result["skipped"])
                self.assertEqual(result["scope"], "skipped")
                self.assertEqual(recorder.refreshed, [])

    def test_changes_in_different_libraries_refresh_their_own_items(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                client.refresh_for_paths([
                    f"{ROOT}/剧集/作品 A/Season 01", f"{ROOT}/电影/作品 B",
                ])

                self.assertEqual(sorted(recorder.refreshed), ["movie-b", "season-a1"])
                self.assertEqual(recorder.refresh_all_calls, 0)

    def test_nested_library_uses_the_longest_matching_location(self):
        folders = [
            {"id": "lib-root", "name": "全部剧集", "locations": [f"{ROOT}/剧集"]},
            {
                "id": "lib-anime", "name": "动漫",
                "locations": [f"{ROOT}/剧集/动漫"],
            },
        ]
        items = {
            "lib-root": [{"Id": "root-item", "Path": f"{ROOT}/剧集"}],
            "lib-anime": [{
                "Id": "anime-season",
                "Path": f"{ROOT}/剧集/动漫/作品 A/Season 01",
            }],
        }
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, folders, items)

                result = client.refresh_for_paths([
                    f"{ROOT}/剧集/动漫/作品 A/Season 01",
                ])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["anime-season"])
                self.assertEqual(result["libraries"], [])

    def test_unlocatable_item_degrades_to_library_refresh_with_reason(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), {"lib-tv": []})

                result = client.refresh_for_paths([f"{ROOT}/剧集/新作品/Season 01"])

                self.assertEqual(recorder.refreshed, ["lib-tv"])
                self.assertEqual(recorder.refresh_all_calls, 0)
                self.assertTrue(result["fallback"])
                self.assertEqual(result["scope"], "library")

    def test_strict_mode_never_degrades_to_library_refresh(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), {"lib-tv": []})

                result = client.refresh_for_paths(
                    [f"{ROOT}/剧集/新作品/Season 01"],
                    allowed_library_ids=("lib-tv",),
                    allow_library_fallback=False,
                )

                self.assertFalse(result["ok"])
                self.assertTrue(result["skipped"])
                self.assertEqual(result["scope"], "skipped")
                self.assertEqual(recorder.refreshed, [])
                self.assertEqual(recorder.refresh_all_calls, 0)

    def test_item_listing_failure_is_safely_skipped(self):
        clients = (
            JellyfinClient("http://jf", "token", allow_global_refresh_fallback=True),
            EmbyClient("http://emby", "token", allow_global_refresh_fallback=True),
        )
        for client in clients:
            with self.subTest(server=client.display_name):
                recorder = _RefreshRecorder(
                    client, _folders(), _items(), items_error=True,
                )

                result = client.refresh_for_paths([f"{ROOT}/剧集/作品 A/Season 01"])

                self.assertEqual(recorder.refreshed, [])
                self.assertEqual(recorder.refresh_all_calls, 0)
                self.assertFalse(result["ok"])
                self.assertTrue(result["skipped"])
                self.assertIn("条目读取失败", result["fallback"])
                self.assertEqual(result["scope"], "skipped")

    def test_item_listing_safety_limit_degrades_to_target_library_refresh(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())
                client._library_items_with_paths = (
                    MediaServerPreciseRefreshTests._real_item_listing.__get__(client)
                )
                client._request = lambda *_args, **_kwargs: {
                    "Items": [{"Id": "series-a", "Path": f"{ROOT}/剧集/作品 A"}],
                    "TotalRecordCount": 2,
                }
                with patch("app.clients.base._MEDIA_ITEM_PAGE_SIZE", 1), patch(
                    "app.clients.base._MAX_MEDIA_ITEMS_FOR_PRECISE_REFRESH", 1,
                ):
                    result = client.refresh_for_paths([f"{ROOT}/剧集/作品 A/Season 01"])

                self.assertTrue(result["ok"])
                self.assertEqual(result["scope"], "library")
                self.assertEqual(recorder.refreshed, ["lib-tv"])
                self.assertEqual(recorder.refresh_all_calls, 0)

    def test_oversized_library_prefers_physical_root_over_virtual_library(self):
        items = {"lib-tv": [{
            "Id": "root-tv", "Type": "Folder", "Path": f"{ROOT}/剧集",
        }]}
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), items)
                client._library_items_with_paths = Mock(
                    side_effect=MediaLibraryEnumerationTooLarge("too large")
                )

                result = client.refresh_for_paths([
                    f"{ROOT}/剧集/全新作品/Season 01",
                ])

                self.assertTrue(result["ok"])
                self.assertEqual(result["scope"], "folder")
                self.assertEqual(recorder.refreshed, ["root-tv"])
                self.assertEqual(result["libraries"], [])

    def test_paths_outside_every_library_are_safely_skipped(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths(["/somewhere/else/A"])

                self.assertEqual(recorder.refreshed, [])
                self.assertEqual(recorder.refresh_all_calls, 0)
                self.assertFalse(result["ok"])
                self.assertTrue(result["skipped"])
                self.assertEqual(result["scope"], "skipped")
                self.assertTrue(result["fallback"])

    def test_library_listing_failure_is_safely_skipped(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())
                client.list_virtual_folders = self._raise

                result = client.refresh_for_paths([f"{ROOT}/剧集/作品 A/Season 01"])

                self.assertEqual(recorder.refresh_all_calls, 0)
                self.assertFalse(result["ok"])
                self.assertTrue(result["skipped"])
                self.assertEqual(result["scope"], "skipped")
                self.assertTrue(result["fallback"])

    def test_bound_library_constraint_rejects_other_library_without_global_fallback(self):
        clients = (
            JellyfinClient("http://jf", "token", allow_global_refresh_fallback=True),
            EmbyClient("http://emby", "token", allow_global_refresh_fallback=True),
        )
        for client in clients:
            with self.subTest(server=client.display_name):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths(
                    [f"{ROOT}/剧集/作品 A/Season 01"],
                    allowed_library_ids=("lib-movie",),
                )

                self.assertFalse(result["ok"])
                self.assertEqual(result["scope"], "skipped")
                self.assertEqual(result["allowed_libraries"], ["lib-movie"])
                self.assertEqual(recorder.refreshed, [])
                self.assertEqual(recorder.refresh_all_calls, 0)

    def test_posix_root_library_does_not_capture_unc_target(self):
        folders = [{"id": "posix-root", "name": "根", "locations": ["/"]}]
        clients = (
            JellyfinClient("http://jf", "token", allow_global_refresh_fallback=True),
            EmbyClient("http://emby", "token", allow_global_refresh_fallback=True),
        )
        for client in clients:
            with self.subTest(server=client.display_name):
                recorder = _RefreshRecorder(client, folders, {})

                result = client.refresh_for_paths(
                    ["//NAS/Share/Film"], allowed_library_ids=("posix-root",),
                )

                self.assertFalse(result["ok"])
                self.assertEqual(result["scope"], "skipped")
                self.assertEqual(result["matched"], 0)
                self.assertEqual(recorder.refreshed, [])
                self.assertEqual(recorder.refresh_all_calls, 0)

    def test_global_refresh_fallback_requires_explicit_opt_in(self):
        clients = (
            JellyfinClient("http://jf", "token", allow_global_refresh_fallback=True),
            EmbyClient("http://emby", "token", allow_global_refresh_fallback=True),
        )
        for client in clients:
            with self.subTest(server=client.display_name):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths(["/somewhere/else/A"])

                self.assertEqual(recorder.refresh_all_calls, 1)
                self.assertTrue(result["ok"])
                self.assertEqual(result["scope"], "global")
                self.assertEqual(result["endpoints"], ["/Library/Refresh"])

    def test_explicit_path_mapping_matches_unc_library(self):
        mapping = MediaServerPathMapping(
            f"{ROOT}/电影",
            r"\\Nas\固态\MediaFlux\STRM\光鸭云盘\整理\电影",
        )
        folders = [{
            "id": "lib-movie", "name": "电影",
            "locations": [r"\\Nas\固态\MediaFlux\STRM\光鸭云盘\整理\电影"],
        }]
        items = {"lib-movie": [{
            "Id": "movie-a",
            "Path": r"\\Nas\固态\MediaFlux\STRM\光鸭云盘\整理\电影\作品 A",
        }]}
        for client in (
            JellyfinClient("http://jf", "token", path_mappings=(mapping,)),
            EmbyClient("http://emby", "token", path_mappings=(mapping,)),
        ):
            with self.subTest(server=client.display_name):
                recorder = _RefreshRecorder(client, folders, items)

                result = client.refresh_for_paths([f"{ROOT}/电影/作品 A"])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["movie-a"])
                self.assertEqual(result["mapped"], 1)
                self.assertEqual(result["matched"], 1)
                self.assertEqual(result["scope"], "item")

    def test_same_virtual_folder_path_requires_no_mapping(self):
        folders = [{
            "id": "lib-movie", "name": "电影",
            "locations": [f"{ROOT}/电影"],
        }]
        items = {"lib-movie": [{
            "Id": "movie-a", "Path": f"{ROOT}/电影/作品 A",
        }]}
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, folders, items)

                result = client.refresh_for_paths([f"{ROOT}/电影/作品 A"])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["movie-a"])
                self.assertEqual(result["mapped"], 0)
                self.assertEqual(result["path_mappings"][0]["mode"], "none")
                self.assertEqual(result["scope"], "item")

    def test_different_absolute_path_space_requires_explicit_mapping(self):
        folders = [{
            "id": "lib-movie", "name": "电影",
            "locations": [r"\\Nas\固态\MediaFlux\STRM\光鸭云盘\整理\电影"],
        }]
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, folders, {})

                result = client.refresh_for_paths([f"{ROOT}/电影/作品 A"])

                self.assertFalse(result["ok"])
                self.assertEqual(result["scope"], "skipped")
                self.assertEqual(result["mapped"], 0)
                self.assertEqual(result["matched"], 0)
                self.assertEqual(recorder.refreshed, [])
                self.assertEqual(recorder.refresh_all_calls, 0)

    def test_unc_virtual_folder_matching_is_case_insensitive(self):
        folders = [{
            "id": "lib-movie", "name": "Movies",
            "locations": ["//NAS/Media/Movies"],
        }]
        items = {"lib-movie": [{
            "Id": "movie-item", "Path": "//Nas/Media/Movies/Film",
        }]}
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, folders, items)

                result = client.refresh_for_paths(["//nas/media/movies/film"])

                self.assertTrue(result["ok"])
                self.assertEqual(result["scope"], "item")
                self.assertEqual(recorder.refreshed, ["movie-item"])
                self.assertEqual(recorder.refresh_all_calls, 0)

    def test_posix_virtual_folder_matching_is_case_sensitive(self):
        folders = [{
            "id": "lib-upper", "name": "Movies",
            "locations": ["/media/Movies"],
        }]
        items = {"lib-upper": [{
            "Id": "upper-film", "Path": "/media/Movies/Film",
        }]}
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, folders, items)

                result = client.refresh_for_paths(["/media/movies/Film"])

                self.assertFalse(result["ok"])
                self.assertEqual(result["scope"], "skipped")
                self.assertEqual(result["matched"], 0)
                self.assertEqual(recorder.refreshed, [])
                self.assertEqual(recorder.refresh_all_calls, 0)

    def test_duplicate_direct_virtual_folder_location_is_safely_rejected(self):
        folders = [
            {"id": "lib-a", "name": "电影 A", "locations": ["/media/movies"]},
            {"id": "lib-b", "name": "电影 B", "locations": ["/media/movies"]},
        ]
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, folders, {})

                result = client.refresh_for_paths(["/media/movies/Film"])

                self.assertFalse(result["ok"])
                self.assertEqual(result["scope"], "skipped")
                self.assertEqual(result["unmatched"], 1)
                self.assertEqual(result["ambiguous"], 1)
                self.assertIn("匹配多个媒体库", result["fallback"])
                self.assertEqual(recorder.refreshed, [])
                self.assertEqual(recorder.refresh_all_calls, 0)

    def test_ambiguous_target_never_uses_opt_in_global_fallback(self):
        folders = [
            {"id": "lib-a", "name": "电影 A", "locations": ["/media/movies"]},
            {"id": "lib-b", "name": "电影 B", "locations": ["/media/movies"]},
        ]
        clients = (
            JellyfinClient("http://jf", "token", allow_global_refresh_fallback=True),
            EmbyClient("http://emby", "token", allow_global_refresh_fallback=True),
        )
        for client in clients:
            with self.subTest(server=client.display_name):
                recorder = _RefreshRecorder(client, folders, {})

                result = client.refresh_for_paths(["/media/movies/Film"])

                self.assertFalse(result["ok"])
                self.assertTrue(result["skipped"])
                self.assertEqual(result["scope"], "skipped")
                self.assertEqual(result["ambiguous"], 1)
                self.assertEqual(recorder.refreshed, [])
                self.assertEqual(recorder.refresh_all_calls, 0)

    def test_partial_match_refreshes_safe_target_without_global_scan(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths([
                    f"{ROOT}/剧集/作品 A/Season 01",
                    "/somewhere/else/A",
                ])

                self.assertEqual(recorder.refreshed, ["season-a1"])
                self.assertEqual(recorder.refresh_all_calls, 0)
                self.assertFalse(result["ok"])
                self.assertEqual(result["scope"], "item")
                self.assertEqual(result["matched"], 1)
                self.assertEqual(result["unmatched"], 1)
                self.assertIn("安全跳过", result["fallback"])

    def test_partial_match_never_uses_opt_in_global_fallback(self):
        clients = (
            JellyfinClient("http://jf", "token", allow_global_refresh_fallback=True),
            EmbyClient("http://emby", "token", allow_global_refresh_fallback=True),
        )
        for client in clients:
            with self.subTest(server=client.display_name):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths([
                    f"{ROOT}/剧集/作品 A/Season 01",
                    "/somewhere/else/A",
                ])

                self.assertEqual(recorder.refreshed, ["season-a1"])
                self.assertEqual(recorder.refresh_all_calls, 0)
                self.assertFalse(result["ok"])
                self.assertEqual(result["scope"], "item")
                self.assertEqual(result["matched"], 1)
                self.assertEqual(result["unmatched"], 1)

    def test_library_root_folder_is_reported_as_folder_scope(self):
        items = {"lib-movie": [{
            "Id": "physical-root", "Type": "Folder",
            "Path": f"{ROOT}/电影",
        }]}
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), items)

                result = client.refresh_for_paths([f"{ROOT}/电影/新作品"])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["physical-root"])
                self.assertEqual(result["items"], [])
                self.assertEqual(result["folders"], ["physical-root"])
                self.assertEqual(result["libraries"], [])
                self.assertEqual(result["scope"], "folder")

    def test_empty_path_list_is_a_safe_noop(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths([])

                self.assertEqual(recorder.refresh_all_calls, 0)
                self.assertTrue(result["ok"])
                self.assertTrue(result["skipped"])
                self.assertTrue(result["fallback"])

    @staticmethod
    def _raise():
        raise RuntimeError("接口失败")


class SchedulerRefreshWiringTests(unittest.TestCase):
    """调度器只规划变化路径并提交统一持久刷新队列。"""

    @staticmethod
    def _settings():
        return {"STRM_ROOT": "/data/strm"}

    def test_changed_paths_are_queued_after_precise_planning(self):
        from app.modules.scheduler import STRMScheduler

        settings = self._settings()
        with patch(
            "app.modules.scheduler.get",
            side_effect=lambda key, default="": settings.get(key, default),
        ), patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
            return_value={"Jellyfin": "queued"},
        ) as enqueue:
            results = STRMScheduler._refresh_media_servers(
                has_changes=True,
                changed_paths=[f"{ROOT}/剧集/作品 A/Season 01/E01.strm"],
                changed_dirs=[],
            )

        self.assertEqual(results, {"Jellyfin": "queued"})
        enqueue.assert_called_once_with(
            [f"{ROOT}/剧集/作品 A/Season 01"],
            immediate=False,
            allow_emby=True,
        )

    def test_manual_refresh_is_queued_without_quiet_window(self):
        from app.modules.scheduler import STRMScheduler

        settings = self._settings()
        with patch(
            "app.modules.scheduler.get",
            side_effect=lambda key, default="": settings.get(key, default),
        ), patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
            return_value={"Jellyfin": "queued"},
        ) as enqueue:
            STRMScheduler._refresh_media_servers(
                has_changes=True,
                changed_dirs=[f"{ROOT}/剧集/作品 A"],
                immediate=True,
            )

        self.assertTrue(enqueue.call_args.kwargs["immediate"])

    def test_legacy_emby_override_only_disables_emby(self):
        from app.modules.scheduler import STRMScheduler

        settings = self._settings()
        with patch(
            "app.modules.scheduler.get",
            side_effect=lambda key, default="": settings.get(key, default),
        ), patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
            return_value={"Jellyfin": "queued"},
        ) as enqueue:
            results = STRMScheduler._refresh_media_servers(
                emby_enabled=False,
                has_changes=True,
                changed_dirs=[f"{ROOT}/剧集/作品 A"],
            )

        self.assertEqual(results, {"Jellyfin": "queued"})
        self.assertFalse(enqueue.call_args.kwargs["allow_emby"])

    def test_unified_refresh_override_disables_queueing(self):
        from app.modules.scheduler import STRMScheduler

        with patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
        ) as enqueue:
            result = STRMScheduler._refresh_media_servers(
                media_server_refresh_enabled=False,
                has_changes=True,
                changed_paths=[f"{ROOT}/剧集/作品 A/Season 01/E01.strm"],
            )

        self.assertEqual(result, {})
        enqueue.assert_not_called()

    def test_no_locatable_target_skips_queueing(self):
        from app.modules.scheduler import STRMScheduler

        settings = self._settings()
        with patch(
            "app.modules.scheduler.get",
            side_effect=lambda key, default="": settings.get(key, default),
        ), patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
        ) as enqueue:
            results = STRMScheduler._refresh_media_servers(
                has_changes=True, changed_paths=[], changed_dirs=[],
            )

        self.assertEqual(results, {})
        enqueue.assert_not_called()

    def test_many_targets_are_submitted_in_one_queue_operation(self):
        from app.modules.scheduler import STRMScheduler

        settings = self._settings()
        changed_dirs = [
            f"{ROOT}/剧集/作品 {index}/Season 01"
            for index in range(MAX_REFRESH_TARGETS + 5)
        ]
        with patch(
            "app.modules.scheduler.get",
            side_effect=lambda key, default="": settings.get(key, default),
        ), patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
            return_value={"Jellyfin": "queued"},
        ) as enqueue:
            results = STRMScheduler._refresh_media_servers(
                has_changes=True, changed_paths=[], changed_dirs=changed_dirs,
            )

        self.assertEqual(results, {"Jellyfin": "queued"})
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(sorted(enqueue.call_args.args[0]), sorted(changed_dirs))

    def test_queue_failure_is_reported(self):
        from app.modules.scheduler import STRMScheduler

        settings = self._settings()
        with patch(
            "app.modules.scheduler.get",
            side_effect=lambda key, default="": settings.get(key, default),
        ), patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
            return_value={"Jellyfin": "failed"},
        ):
            results = STRMScheduler._refresh_media_servers(
                has_changes=True, changed_dirs=[f"{ROOT}/剧集/作品 A"],
            )

        self.assertEqual(results, {"Jellyfin": "failed"})

    def test_no_changes_skips_refresh_entirely(self):
        from app.modules.scheduler import STRMScheduler

        self.assertEqual(STRMScheduler._refresh_media_servers(has_changes=False), {})


if __name__ == "__main__":
    unittest.main()
