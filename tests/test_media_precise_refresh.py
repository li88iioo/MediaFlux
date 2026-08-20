"""Jellyfin / Emby 精准局部刷新（Sprint 4）需求驱动测试。

覆盖场景来自实施计划的验收条件：
- Task 4.1 变化路径聚合出最小刷新目标
- Task 4.2 Jellyfin 最深父 Item 刷新与降级
- Task 4.3 Emby 与 Jellyfin 行为契约一致
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.modules.media_refresh import MAX_REFRESH_TARGETS, plan_refresh_targets

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

    def test_sibling_seasons_collapse_to_the_show_directory(self):
        plan = plan_refresh_targets(
            [
                f"{ROOT}/剧集/作品 A/Season 01/E01.strm",
                f"{ROOT}/剧集/作品 A/Season 02/E01.strm",
            ],
            media_roots=[ROOT],
        )

        self.assertEqual(plan.targets, (f"{ROOT}/剧集/作品 A",))

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
        client.refresh_library = self._refresh_library
        client.refresh_all = self._refresh_all

    def _library_items(self, library_id: str):
        if self.items_error:
            raise RuntimeError("接口失败")
        return self.items.get(str(library_id), [])

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

    def _clients(self):
        return (
            ("Jellyfin", JellyfinClient("http://jf", "token")),
            ("Emby", EmbyClient("http://emby", "token")),
        )

    def test_deepest_existing_item_is_refreshed_for_both_servers(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths([f"{ROOT}/剧集/作品 A/Season 01"])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["season-a1"])
                self.assertEqual(recorder.refresh_all_calls, 0)
                self.assertEqual(result["fallback"], "")

    def test_same_item_is_refreshed_once_per_round(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                client.refresh_for_paths([
                    f"{ROOT}/剧集/作品 A/Season 01",
                    f"{ROOT}/剧集/作品 A/Season 01/Extras",
                ])

                self.assertEqual(recorder.refreshed, ["season-a1"])

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

    def test_item_listing_failure_degrades_to_library_refresh(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(
                    client, _folders(), _items(), items_error=True,
                )

                result = client.refresh_for_paths([f"{ROOT}/剧集/作品 A/Season 01"])

                self.assertEqual(recorder.refreshed, ["lib-tv"])
                self.assertTrue(result["fallback"])

    def test_paths_outside_every_library_fall_back_to_global_refresh(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths(["/somewhere/else/A"])

                self.assertEqual(recorder.refreshed, [])
                self.assertEqual(recorder.refresh_all_calls, 1)
                self.assertTrue(result["fallback"])

    def test_library_listing_failure_falls_back_to_global_refresh(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())
                client.list_virtual_folders = self._raise

                result = client.refresh_for_paths([f"{ROOT}/剧集/作品 A/Season 01"])

                self.assertEqual(recorder.refresh_all_calls, 1)
                self.assertTrue(result["fallback"])

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
    """调度器把本轮真实变化路径交给精准刷新。"""

    def test_changed_paths_drive_precise_refresh(self):
        from app.modules.scheduler import STRMScheduler

        calls: list[list[str]] = []

        class _Client:
            display_name = "Jellyfin"

            def refresh_for_paths(self, paths):
                calls.append(list(paths))
                return {"ok": True, "items": ["season-a1"], "libraries": [], "fallback": ""}

            def refresh_for_path(self, _path):
                raise AssertionError("有可定位目标时不得退回媒体库级刷新")

        settings = {
            "JELLYFIN_ENABLED": True, "EMBY_ENABLED": False,
            "JELLYFIN_URL": "http://jf", "JELLYFIN_API_KEY": "token",
            "STRM_ROOT": "/data/strm",
        }
        with patch("app.modules.scheduler.get", side_effect=lambda key, default="": settings.get(key, default)), \
                patch("app.modules.scheduler.get_bool", side_effect=lambda key, default=False: bool(settings.get(key, default))), \
                patch("app.modules.scheduler.JellyfinClient", return_value=_Client()), \
                patch("app.services.clear_dashboard_cache"):
            results = STRMScheduler._refresh_media_servers(
                has_changes=True,
                changed_paths=[f"{ROOT}/剧集/作品 A/Season 01/E01.strm"],
                changed_dirs=[],
            )

        self.assertEqual(results, {"Jellyfin": True})
        self.assertEqual(calls, [[f"{ROOT}/剧集/作品 A/Season 01"]])

    def test_no_locatable_target_skips_refresh_instead_of_refreshing_everything(self):
        from app.modules.scheduler import STRMScheduler

        used: list[str] = []

        class _Client:
            display_name = "Jellyfin"

            def refresh_for_paths(self, _paths):
                raise AssertionError("没有可定位目标时不得调用精准刷新")

            def refresh_for_path(self, path):
                used.append(str(path))
                raise AssertionError("没有安全目标时不得退回根目录刷新")

        settings = {
            "JELLYFIN_ENABLED": True, "EMBY_ENABLED": False,
            "JELLYFIN_URL": "http://jf", "JELLYFIN_API_KEY": "token",
            "STRM_ROOT": "/data/strm",
        }
        with patch("app.modules.scheduler.get", side_effect=lambda key, default="": settings.get(key, default)), \
                patch("app.modules.scheduler.get_bool", side_effect=lambda key, default=False: bool(settings.get(key, default))), \
                patch("app.modules.scheduler.JellyfinClient", return_value=_Client()), \
                patch("app.services.clear_dashboard_cache"):
            results = STRMScheduler._refresh_media_servers(
                has_changes=True, changed_paths=[], changed_dirs=[],
            )

        self.assertEqual(results, {})
        self.assertEqual(used, [])

    def test_many_targets_are_refreshed_in_complete_batches(self):
        from app.modules.scheduler import STRMScheduler

        calls: list[list[str]] = []

        class _Client:
            display_name = "Jellyfin"

            def refresh_for_paths(self, paths):
                calls.append(list(paths))
                return {"ok": True, "items": [], "libraries": [], "fallback": ""}

        settings = {
            "JELLYFIN_ENABLED": True, "EMBY_ENABLED": False,
            "JELLYFIN_URL": "http://jf", "JELLYFIN_API_KEY": "token",
            "STRM_ROOT": "/data/strm",
        }
        changed_dirs = [
            f"{ROOT}/剧集/作品 {index}/Season 01"
            for index in range(MAX_REFRESH_TARGETS + 5)
        ]
        with patch("app.modules.scheduler.get", side_effect=lambda key, default="": settings.get(key, default)), \
                patch("app.modules.scheduler.get_bool", side_effect=lambda key, default=False: bool(settings.get(key, default))), \
                patch("app.modules.scheduler.JellyfinClient", return_value=_Client()), \
                patch("app.services.clear_dashboard_cache"):
            results = STRMScheduler._refresh_media_servers(
                has_changes=True, changed_paths=[], changed_dirs=changed_dirs,
            )

        self.assertEqual(results, {"Jellyfin": True})
        self.assertEqual([len(batch) for batch in calls], [MAX_REFRESH_TARGETS, 5])
        self.assertEqual(
            sorted(target for batch in calls for target in batch),
            sorted(changed_dirs),
        )

    def test_failure_in_a_later_refresh_batch_is_reported(self):
        from app.modules.scheduler import STRMScheduler

        calls = 0

        class _Client:
            display_name = "Jellyfin"

            def refresh_for_paths(self, _paths):
                nonlocal calls
                calls += 1
                return {"ok": calls == 1, "items": [], "libraries": [], "fallback": ""}

        settings = {
            "JELLYFIN_ENABLED": True, "EMBY_ENABLED": False,
            "JELLYFIN_URL": "http://jf", "JELLYFIN_API_KEY": "token",
            "STRM_ROOT": "/data/strm",
        }
        changed_dirs = [
            f"{ROOT}/剧集/作品 {index}/Season 01"
            for index in range(MAX_REFRESH_TARGETS + 1)
        ]
        with patch("app.modules.scheduler.get", side_effect=lambda key, default="": settings.get(key, default)), \
                patch("app.modules.scheduler.get_bool", side_effect=lambda key, default=False: bool(settings.get(key, default))), \
                patch("app.modules.scheduler.JellyfinClient", return_value=_Client()), \
                patch("app.services.clear_dashboard_cache"):
            results = STRMScheduler._refresh_media_servers(
                has_changes=True, changed_paths=[], changed_dirs=changed_dirs,
            )

        self.assertEqual(calls, 2)
        self.assertEqual(results, {"Jellyfin": False})

    def test_no_changes_skips_refresh_entirely(self):
        from app.modules.scheduler import STRMScheduler

        self.assertEqual(
            STRMScheduler._refresh_media_servers(has_changes=False), {}
        )


if __name__ == "__main__":
    unittest.main()
