"""自动路径刷新：剧集汇总日期、电影精准定位、删除上溯与请求范围契约。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app.clients.base import MediaLibraryEnumerationTooLarge, media_refresh_parameters
from app.clients.emby import EmbyClient
from app.clients.jellyfin import JellyfinClient
from app.modules.media_refresh import MAX_REFRESH_TARGETS, plan_refresh_targets
from app.modules.media_server_path_mapping import MediaServerPathMapping
from app.repositories.strm import StrmRefreshPolicy

ROOT = "/data/strm/光鸭云盘"


class MediaLibraryRefreshRequestTests(unittest.TestCase):
    """刷新预检与真实客户端共用参数契约，所有 HTTP 写入均由桩接管。"""

    _CLIENT_TYPES = {"jellyfin": JellyfinClient, "emby": EmbyClient}

    def _assert_request(self, server, expected, **options):
        self.assertEqual(media_refresh_parameters(
            mode=options.get("mode", "scan"), replace_images=options.get("replace_images", False),
            regenerate_trickplay=options.get("regenerate_trickplay", False),
            server=server,
        ), expected)
        response = Mock()
        headers = {"Authorization": 'MediaBrowser Token="token"', "Accept": "application/json"}
        if server == "emby":
            headers["X-Emby-Token"] = "token"
        with self._CLIENT_TYPES[server]("http://media.invalid/", "token") as client:
            with patch.object(client._session, "post", return_value=response) as post:
                self.assertTrue(client.refresh_library("library-id", **options))
                post.assert_called_once_with(
                    "http://media.invalid/Items/library-id/Refresh",
                    headers=headers, params=expected, timeout=15,
                )
        response.raise_for_status.assert_called_once_with()

    def _assert_rejected(self, server, options, message):
        with self.assertRaisesRegex(ValueError, message):
            media_refresh_parameters(
                mode=options.get("mode", "scan"), replace_images=options.get("replace_images", False),
                regenerate_trickplay=options.get("regenerate_trickplay", False),
                server=server,
            )
        with self._CLIENT_TYPES[server]("http://media.invalid", "token") as client:
            with patch.object(client, "_headers") as headers, patch.object(client._session, "post") as post:
                with self.assertRaisesRegex(ValueError, message):
                    client.refresh_library("library-id", **options)
                headers.assert_not_called()
                post.assert_not_called()

    def test_default_scan_fills_missing_metadata_and_images_without_replacing(self):
        expected = {
            "jellyfin": {
                "metadataRefreshMode": "Default", "imageRefreshMode": "Default",
                "replaceAllMetadata": "false", "replaceAllImages": "false",
                "regenerateTrickplay": "false",
            },
            "emby": {
                "Recursive": "true", "MetadataRefreshMode": "Default",
                "ImageRefreshMode": "Default", "ReplaceAllMetadata": "false",
                "ReplaceAllImages": "false",
            },
        }
        for server, params in expected.items():
            for options in ({}, {"mode": "scan"}):
                with self.subTest(server=server, options=options):
                    self._assert_request(server, params, **options)

    def test_jellyfin_metadata_and_image_options_are_independent(self):
        for mode, replace_metadata in (("missing", "false"), ("replace", "true")):
            for images, trickplay in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(mode=mode, images=images, trickplay=trickplay):
                    self._assert_request("jellyfin", {
                        "metadataRefreshMode": "FullRefresh", "imageRefreshMode": "FullRefresh",
                        "replaceAllMetadata": replace_metadata,
                        "replaceAllImages": "true" if images else "false",
                        "regenerateTrickplay": "true" if trickplay else "false",
                    }, mode=mode, replace_images=images, regenerate_trickplay=trickplay)

    def test_emby_metadata_and_image_options_preserve_recursive_refresh(self):
        for mode, replace_metadata in (("missing", "false"), ("replace", "true")):
            for images in (False, True):
                with self.subTest(mode=mode, images=images):
                    self._assert_request("emby", {
                        "Recursive": "true", "MetadataRefreshMode": "FullRefresh",
                        "ImageRefreshMode": "FullRefresh", "ReplaceAllMetadata": replace_metadata,
                        "ReplaceAllImages": "true" if images else "false",
                    }, mode=mode, replace_images=images)

    def test_invalid_modes_and_non_boolean_options_raise_before_network(self):
        cases = (
            ({"mode": "all"}, "mode"),
            ({"mode": None}, "mode"),
            ({"mode": []}, "mode"),
            ({"replace_images": "false"}, "bool"),
            ({"replace_images": 1}, "bool"),
            ({"regenerate_trickplay": "true"}, "bool"),
            ({"regenerate_trickplay": 0}, "bool"),
        )
        for server in self._CLIENT_TYPES:
            for options, message in cases:
                with self.subTest(server=server, options=options):
                    self._assert_rejected(server, options, message)

    def test_scan_rejects_image_and_trickplay_replacement(self):
        for server in self._CLIENT_TYPES:
            for options in (
                {"replace_images": True},
                {"regenerate_trickplay": True},
                {"replace_images": True, "regenerate_trickplay": True},
            ):
                with self.subTest(server=server, options=options):
                    self._assert_rejected(server, options, "scan|Emby")

    def test_emby_rejects_trickplay_for_metadata_refresh(self):
        for mode in ("missing", "replace"):
            with self.subTest(mode=mode):
                self._assert_rejected("emby", {
                    "mode": mode, "regenerate_trickplay": True,
                }, "Emby.*regenerate_trickplay")

    def test_parameter_helper_defaults_to_jellyfin_and_rejects_unknown_server(self):
        self.assertEqual(media_refresh_parameters("scan", False, False), {
            "metadataRefreshMode": "Default", "imageRefreshMode": "Default",
            "replaceAllMetadata": "false", "replaceAllImages": "false",
            "regenerateTrickplay": "false",
        })
        with self.assertRaisesRegex(ValueError, "server"):
            media_refresh_parameters("scan", False, False, server="unknown")

    def test_network_and_http_failures_keep_false_result(self):
        for server, client_type in self._CLIENT_TYPES.items():
            for failure in ("network", "http"):
                with self.subTest(server=server, failure=failure), client_type("http://media.invalid", "token") as client:
                    response = Mock()
                    with patch.object(client._session, "post", return_value=response) as post:
                        if failure == "network":
                            post.side_effect = requests.Timeout("offline")
                        else:
                            response.raise_for_status.side_effect = requests.HTTPError("server error")
                        self.assertFalse(client.refresh_library("library-id"))
                        post.assert_called_once()
                        if failure == "network":
                            response.raise_for_status.assert_not_called()
                        else:
                            response.raise_for_status.assert_called_once_with()


class RefreshTargetPlanningTests(unittest.TestCase):
    """Task 4.1：变化路径压缩为最小刷新目标。"""

    def test_lone_file_stays_precise_and_same_directory_files_collapse(self):
        episodes = [f"{ROOT}/剧集/作品 A/Season 01/E{index:02d}.strm" for index in range(1, 4)]
        lone = f"{ROOT}/剧集/作品 A/Season 02/E01.strm"
        movies = [f"{ROOT}/电影/作品 A/A.strm", f"{ROOT}/电影/作品 B/B.strm"]

        plan = plan_refresh_targets([*episodes, lone, *movies, lone], media_roots=[ROOT])

        # 规划器保留精确变化路径或同目录聚合；客户端按作品合并实际刷新。
        self.assertEqual(plan.targets, tuple(sorted([
            f"{ROOT}/剧集/作品 A/Season 01", lone, *movies,
        ])))

    def test_files_directly_in_media_root_never_collapse_to_the_root(self):
        files = [f"{ROOT}/A.strm", f"{ROOT}/B.strm"]

        plan = plan_refresh_targets(files, media_roots=[ROOT])

        self.assertEqual(plan.targets, tuple(files))

    def test_overflow_directory_absorbs_files_beneath_it(self):
        inside = f"{ROOT}/剧集/作品 A/Season 01/E01.strm"
        outside = f"{ROOT}/剧集/作品 B/Season 01/E01.strm"

        plan = plan_refresh_targets(
            [inside, outside], [f"{ROOT}/剧集/作品 A/Season 01"], media_roots=[ROOT],
        )

        self.assertEqual(plan.targets, (f"{ROOT}/剧集/作品 A/Season 01", outside))

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

        self.assertEqual(plan.targets, (f"{ROOT}/E01.strm",))
        self.assertNotIn(ROOT, plan.targets)
        self.assertTrue(plan.has_targets)

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

        self.assertEqual(plan.targets, ("D:/strm/光鸭云盘/剧集/作品 A/Season 01/E01.strm",))


class _RefreshRecorder:
    """记录刷新调用的媒体服务器桩。"""

    def __init__(self, client, folders, items, *, items_error: bool = False):
        self.client = client
        self.folders = folders
        self.items = items
        self.items_error = items_error
        self.refreshed: list[str] = []
        # 合法刷新都经 refresh_library 记录；任何额外 POST（如全库扫描）都是缺陷。
        self.posts = Mock(side_effect=AssertionError("不得发起额外刷新请求"))
        self.unexpected_request = Mock(side_effect=AssertionError("不得逐季查询单集"))
        client.list_virtual_folders = lambda: self.folders
        client._request = self.unexpected_request
        client._library_items_with_paths = self._library_items
        client._library_root_items_with_paths = self._library_roots
        client.refresh_library = self._refresh_library
        client._session.post = self.posts

    def _library_items(self, library_id: str):
        if self.items_error:
            raise RuntimeError("接口失败")
        return self.items.get(library_id, [])

    def _library_roots(
        self, library_id: str, *, locations: tuple[str, ...] = (),
    ) -> list[dict]:
        wanted = {self.client._media_path_key(item) for item in locations}
        return [
            item for item in self.items.get(library_id, [])
            if str(item.get("Type") or "").casefold() == "folder"
            and (
                not wanted
                or self.client._media_path_key(item.get("Path")) in wanted
            )
        ]

    def _refresh_library(self, library_id: str) -> bool:
        self.refreshed.append(library_id)
        return True


def _make_tree(root: str, entries) -> None:
    """建立本机文件树；媒体库根总是存在，以 / 结尾的条目是目录。"""
    Path(root).mkdir(parents=True, exist_ok=True)
    for name in entries:
        path = Path(root, name)
        if name.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("http://play.invalid/x")


def _folders() -> list[dict]:
    return [
        {"id": "lib-tv", "name": "剧集", "locations": [f"{ROOT}/剧集"]},
        {"id": "lib-movie", "name": "电影", "locations": [f"{ROOT}/电影"]},
    ]


def _items() -> dict[str, list[dict]]:
    return {
        "lib-tv": [
            {"Id": "series-a", "Type": "Series", "Path": f"{ROOT}/剧集/作品 A"},
            {"Id": "season-a1", "Type": "Season", "SeriesId": "series-a", "Path": f"{ROOT}/剧集/作品 A/Season 01"},
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

    def test_season_change_refreshes_parent_series_for_both_servers(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths([f"{ROOT}/剧集/作品 A/Season 01"])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["series-a"])
                recorder.posts.assert_not_called()
                self.assertEqual(result["fallback"], "")
                self.assertEqual(result["scope"], "item")
                self.assertEqual(result["endpoints"], ["/Items/series-a/Refresh"])

    def test_episode_and_season_changes_refresh_series_summary_once(self):
        # 单集/季的刷新不会更新整剧最新添加日期；新增、更新与删除统一落到受影响的作品。
        s1, s2, s3 = "作品 A/Season 01", "作品 A/Season 02", "作品 A/Season 03"
        e11, e12, e21, e22 = f"{s1}/E01.strm", f"{s1}/E02.strm", f"{s2}/E01.strm", f"{s2}/E02.strm"
        cases = (
            ("单集更新", [e11], [e11], ["series-a"]),
            ("单集字幕", [e11, f"{s1}/E01.zh.srt"], [f"{s1}/E01.zh.srt"], ["series-a"]),
            ("新增单集", [f"{s1}/E03.strm"], [f"{s1}/E03.strm"], ["series-a"]),
            ("删除单集", [f"{s1}/"], [e11], ["series-a"]),
            ("同季多集", [e11, e12], [e11, e12], ["series-a"]),
            ("跨季单集同剧合并", [e11, e21], [e11, e21], ["series-a"]),
            ("同剧多季整季", [e11, e12, e21, e22], [e11, e12, e21, e22], ["series-a"]),
            ("季目录", [f"{s1}/"], [s1], ["series-a"]),
            ("多个季目录", [f"{s1}/", f"{s2}/"], [s1, s2], ["series-a"]),
            ("整剧目录吸收其下单集", [e11], ["作品 A", e11], ["series-a"]),
            ("新季", [f"{s3}/E01.strm"], [f"{s3}/E01.strm"], ["series-a"]),
            ("新剧", ["全新作品/Season 01/E01.strm"], ["全新作品/Season 01/E01.strm"], ["root-tv"]),
            # Jellyfin 只在父级校验子项时移除条目：季目录已删就刷整剧，剧目录已删就刷物理根。
            ("删除整季目录", ["作品 A/"], [s1], ["series-a"]),
            ("删除整季后的单集变化", ["作品 A/"], [e11], ["series-a"]),
            ("删除整剧目录", [], ["作品 A"], ["root-tv"]),
        )
        for label, client in self._clients():
            for case, existing, changed, expected in cases:
                with self.subTest(server=label, case=case), tempfile.TemporaryDirectory() as root:
                    show_root = f"{root}/剧集"
                    _make_tree(show_root, existing)
                    recorder = _RefreshRecorder(client, [
                        {"id": "lib-tv", "name": "剧集", "locations": [show_root]},
                    ], {"lib-tv": [
                        {"Id": "root-tv", "Type": "Folder", "Path": show_root},
                        {"Id": "series-a", "Type": "Series", "Path": f"{show_root}/作品 A"},
                        {"Id": "season-1", "Type": "Season", "SeriesId": "series-a", "Path": f"{show_root}/{s1}"},
                        {"Id": "season-2", "Type": "Season", "SeriesId": "series-a", "Path": f"{show_root}/{s2}"},
                    ]})
                    paths = [f"{show_root}/{name}" for name in changed]

                    result = client.refresh_for_paths(paths)

                    self.assertTrue(result["ok"])
                    self.assertEqual(sorted(recorder.refreshed), expected)
                    recorder.unexpected_request.assert_not_called()
                    self.assertEqual(sorted(result["succeeded_paths"]), sorted(paths))
                    recorder.posts.assert_not_called()

    def test_movie_refresh_stays_on_the_movie_unless_files_appear_or_vanish(self):
        b, other = "作品 B/B.strm", "作品 B/B.part2.strm"
        cases = (
            ("电影更新", [b], b, [b], ["movie-b"]),
            ("电影字幕", [b, "作品 B/B.zh.srt"], "作品 B/B.zh.srt", [b], ["movie-b"]),
            ("同名新视频不算字幕", [b, "作品 B/B.mkv"], "作品 B/B.mkv", [b], ["root-movies"]),
            ("电影删除", [], b, [b], ["root-movies"]),
            ("删除视频但目录仍在", ["作品 B/"], b, [b], ["root-movies"]),
            # 附属文件取主文件名最长的视频；主文件名相同无法区分时交给父目录。
            ("字幕归属更具体的分段", [b, other, "作品 B/B.part2.zh.srt"], "作品 B/B.part2.zh.srt", [b, other],
             ["movie-part2"]),
            ("同名视频歧义", [b, "作品 B/B.mkv", "作品 B/B.zh.srt"], "作品 B/B.zh.srt", [b, "作品 B/B.mkv"],
             ["root-movies"]),
        )
        ids = {b: "movie-b", other: "movie-part2", "作品 B/B.mkv": "movie-mkv"}
        for label, client in self._clients():
            for case, existing, changed, listed, expected in cases:
                with self.subTest(server=label, case=case), tempfile.TemporaryDirectory() as root:
                    movie_root = f"{root}/电影"
                    _make_tree(movie_root, existing)
                    recorder = _RefreshRecorder(client, [
                        {"id": "lib-movie", "name": "电影", "locations": [movie_root]},
                    ], {"lib-movie": [
                        {"Id": "root-movies", "Type": "Folder", "Path": movie_root},
                        *({"Id": ids[name], "Type": "Movie", "Path": f"{movie_root}/{name}"} for name in listed),
                    ]})

                    result = client.refresh_for_paths([f"{movie_root}/{changed}"])

                    self.assertTrue(result["ok"])
                    self.assertEqual(recorder.refreshed, expected)
                    recorder.unexpected_request.assert_not_called()

    def test_deletion_is_only_judged_where_the_library_root_is_visible_locally(self):
        # 映射命中时按本地前缀判断；同路径空间按媒体库目录判断；只在服务端可见的路径保持精确。
        for label, client_type in (("Jellyfin", JellyfinClient), ("Emby", EmbyClient)):
            for case, mapped, server_only_root, create, expected in (
                ("映射路径已删除", True, False, False, ["root-movies"]),
                ("映射路径仍在", True, False, True, ["movie-b"]),
                ("服务端独有目录恰好与本机上级同名", False, True, False, ["movie-b"]),
                ("配置了映射但路径未命中映射", "other", False, False, ["movie-b"]),
            ):
                with self.subTest(server=label, case=case), tempfile.TemporaryDirectory() as root:
                    local_root = f"{root}/local/电影"
                    _make_tree(local_root, ["作品 B/B.strm"] if create else [])
                    if mapped is True:
                        server_root = "//nas/share/电影"
                        mappings = (MediaServerPathMapping(local_root, server_root),)
                        source_root = local_root
                    else:
                        # 本机有 {root}，媒体库目录却只在服务端存在；或映射只覆盖别的目录。
                        server_root = f"{root}/server-only/电影" if server_only_root else local_root
                        mappings = (
                            (MediaServerPathMapping(f"{root}/elsewhere", "//nas/share/其他"),)
                            if mapped == "other" else ()
                        )
                        source_root = server_root
                    client = client_type("http://media.invalid", "token", path_mappings=mappings)
                    recorder = _RefreshRecorder(client, [
                        {"id": "lib-movie", "name": "电影", "locations": [server_root]},
                    ], {"lib-movie": [
                        {"Id": "root-movies", "Type": "Folder", "Path": server_root},
                        {"Id": "movie-b", "Type": "Movie", "Path": f"{server_root}/作品 B/B.strm"},
                    ]})

                    result = client.refresh_for_paths([f"{source_root}/作品 B/B.strm"])

                    self.assertTrue(result["ok"])
                    self.assertEqual(recorder.refreshed, expected)

    def test_missing_series_keeps_known_season_without_guessing_another_show(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                directory = f"{ROOT}/剧集/作品 A/Season 01"
                recorder = _RefreshRecorder(client, _folders(), {"lib-tv": [
                    {"Id": "root-tv", "Type": "Folder", "Path": f"{ROOT}/剧集"},
                    {"Id": "series-b", "Type": "Series", "Path": f"{ROOT}/剧集/作品 B"},
                    # SeriesId 不能将路径范围扩大到另一部作品。
                    {"Id": "season-1", "Type": "Season", "SeriesId": "series-b", "Path": directory},
                ]})

                result = client.refresh_for_paths([f"{directory}/E01.strm"])

                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["season-1"])
                recorder.posts.assert_not_called()
                recorder.unexpected_request.assert_not_called()

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
                self.assertEqual(result["succeeded_paths"], [
                    f"{ROOT}/剧集/作品 A/Season 01",
                    f"{ROOT}/剧集/全新作品/Season 01",
                ])

    def test_movie_parent_refresh_covers_unlisted_siblings(self):
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
                self.assertEqual(recorder.refreshed, ["root-movies"])
                self.assertEqual(result["scope"], "folder")
                recorder.posts.assert_not_called()

    def test_flat_movie_directory_uses_covering_folder_but_file_target_stays_precise(self):
        directory = f"{ROOT}/电影/合集"
        for folder in (True, False):
            items = {"lib-movie": [
                {"Id": "old", "Type": "Movie", "Path": f"{directory}/Long-existing-title.strm"},
                {"Id": "changed", "Type": "Movie", "Path": f"{directory}/B.strm"},
            ]}
            if folder:
                items["lib-movie"].append({"Id": "collection", "Type": "Folder", "Path": directory})
            for label, client in self._clients():
                with self.subTest(server=label, folder=folder):
                    recorder = _RefreshRecorder(client, _folders(), items)
                    result = client.refresh_for_paths([directory])
                    self.assertTrue(result["ok"])
                    self.assertEqual(recorder.refreshed, ["collection" if folder else "lib-movie"])
                    recorder.refreshed.clear()
                    result = client.refresh_for_paths([f"{directory}/B.strm"])
                    self.assertTrue(result["ok"])
                    self.assertEqual(recorder.refreshed, ["changed"])
                    recorder.posts.assert_not_called()

    def test_strict_file_batch_keeps_series_deduplication_without_broad_scan(self):
        directory = f"{ROOT}/剧集/作品 A"
        items = {"lib-tv": [{"Id": "series", "Type": "Series", "Path": directory}]}
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), items)
                result = client.refresh_for_paths(
                    [f"{directory}/Season 1/E{i}.mkv" for i in range(128)],
                    allowed_library_ids=("lib-tv",), allow_library_fallback=False,
                )
                self.assertTrue(result["ok"])
                self.assertEqual(result["scope"], "item")
                self.assertEqual(recorder.refreshed, ["series"])
                recorder.posts.assert_not_called()

    def test_recent_target_id_is_resolved_but_not_refreshed_again(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths(
                    [f"{ROOT}/剧集/作品 A/Season 01"],
                    skip_item_ids=("series-a",),
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

                self.assertEqual(recorder.refreshed, ["series-a"])

    def test_many_existing_targets_stay_precise_with_one_library_enumeration(self):
        items = [
            {"Id": f"series-{index}", "Type": "Series", "Path": f"{ROOT}/剧集/作品 {index}"}
            for index in range(128)
        ]
        paths = [f"{item['Path']}/Season 01/E01.strm" for item in items]
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), {"lib-tv": items})
                client._library_items_with_paths = Mock(
                    wraps=client._library_items_with_paths,
                )

                result = client.refresh_for_paths(paths)

                client._library_items_with_paths.assert_called_once_with("lib-tv")
                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, [item["Id"] for item in items])
                self.assertEqual(result["scope"], "item")
                self.assertEqual(result["succeeded_paths"], paths)
                self.assertEqual(result["folders"], [])
                self.assertEqual(result["libraries"], [])
                recorder.posts.assert_not_called()

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
        paths = [
            f"{ROOT}/剧集/作品 A/Season 01/E01.strm",
            f"{ROOT}/剧集/作品 A/Season 02/E02.strm",
            f"{ROOT}/剧集/作品 B/Season 01/E01.strm",
            f"{ROOT}/电影/作品 B",
        ]
        for label, client in self._clients():
            with self.subTest(server=label):
                items = _items()
                items["lib-tv"].extend([
                    {"Id": "season-a2", "Type": "Season", "Path": f"{ROOT}/剧集/作品 A/Season 02"},
                    {"Id": "series-b", "Type": "Series", "Path": f"{ROOT}/剧集/作品 B"},
                ])
                recorder = _RefreshRecorder(client, _folders(), items)
                result = client.refresh_for_paths(paths)

                self.assertTrue(result["ok"])
                self.assertEqual(sorted(recorder.refreshed), ["movie-b", "series-a", "series-b"])
                self.assertEqual(result["succeeded_paths"], paths)
                self.assertEqual(result["folders"], [])
                self.assertEqual(result["libraries"], [])
                recorder.posts.assert_not_called()
                recorder.unexpected_request.assert_not_called()

                recorder.refreshed.clear()
                deferred = client.refresh_for_paths(paths, skip_item_ids=("series-a",))
                self.assertEqual(deferred["deferred_paths"], paths[:2])
                self.assertEqual(deferred["deferred_targets"], dict.fromkeys(paths[:2], "series-a"))
                self.assertEqual(deferred["succeeded_paths"], paths[2:])
                self.assertEqual(sorted(recorder.refreshed), ["movie-b", "series-b"])

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
                recorder.posts.assert_not_called()
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
                recorder.posts.assert_not_called()

    def test_item_listing_failure_is_safely_skipped(self):
        clients = (
            JellyfinClient("http://jf", "token"),
            EmbyClient("http://emby", "token"),
        )
        for client in clients:
            with self.subTest(server=client.display_name):
                recorder = _RefreshRecorder(
                    client, _folders(), _items(), items_error=True,
                )

                result = client.refresh_for_paths([f"{ROOT}/剧集/作品 A/Season 01"])

                self.assertEqual(recorder.refreshed, [])
                recorder.posts.assert_not_called()
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
                recorder.posts.assert_not_called()

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
                recorder.posts.assert_not_called()
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

                recorder.posts.assert_not_called()
                self.assertFalse(result["ok"])
                self.assertTrue(result["skipped"])
                self.assertEqual(result["scope"], "skipped")
                self.assertTrue(result["fallback"])

    def test_bound_library_constraint_rejects_other_library_without_global_fallback(self):
        clients = (
            JellyfinClient("http://jf", "token"),
            EmbyClient("http://emby", "token"),
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
                recorder.posts.assert_not_called()

    def test_posix_root_library_does_not_capture_unc_target(self):
        folders = [{"id": "posix-root", "name": "根", "locations": ["/"]}]
        clients = (
            JellyfinClient("http://jf", "token"),
            EmbyClient("http://emby", "token"),
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
                recorder.posts.assert_not_called()

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
                recorder.posts.assert_not_called()

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
                recorder.posts.assert_not_called()

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
                recorder.posts.assert_not_called()

    def test_item_index_preserves_path_style_case_and_component_boundaries(self):
        cases = (
            # 根目录取本机不存在的路径，避免宿主机恰好有 /media 时被当作本机已删除。
            ("/mf-absent-root", "/mf-absent-root/Film", "/mf-absent-root/Film/E01.strm", True),
            ("/mf-absent-root", "/mf-absent-root/Film", "/mf-absent-root/film/E01.strm", False),
            ("/mf-absent-root", "/mf-absent-root/Film", "/mf-absent-root/Film-extra/E01.strm", False),
            ("C:/", "C:/Media/Straße", r"c:\media\STRASSE\E01.strm", True),
            ("//NAS/Share", "//NAS/Share/Film", "//nas/share/FILM/E01.strm", True),
        )
        for label, client in self._clients():
            for root, item_path, target, matched in cases:
                with self.subTest(server=label, target=target):
                    recorder = _RefreshRecorder(client, [
                        {"id": "library", "name": "作品", "locations": [root]},
                    ], {"library": [{"Id": "series", "Type": "Series", "Path": item_path}]})
                    result = client.refresh_for_paths([target], allow_library_fallback=False)
                    self.assertEqual(result["ok"], matched)
                    self.assertEqual(recorder.refreshed, ["series"] if matched else [])
                    self.assertEqual(result["succeeded_paths"], [target] if matched else [])
                    recorder.posts.assert_not_called()

    def test_same_canonical_item_path_keeps_existing_candidate_priority(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, [
                    {"id": "library", "name": "作品", "locations": ["D:/Media"]},
                ], {"library": [
                    {"Id": "short", "Type": "Series", "Path": "D:/Media/Straße"},
                    {"Id": "preferred", "Type": "Series", "Path": "D:/Media/STRASSE"},
                    {"Id": "tie", "Type": "Series", "Path": "D:/Media/strasse"},
                ]})
                result = client.refresh_for_paths(["D:/Media/STRASSE/S01"], allow_library_fallback=False)
                self.assertTrue(result["ok"])
                self.assertEqual(recorder.refreshed, ["preferred"])

    def test_duplicate_direct_virtual_folder_location_is_safely_rejected(self):
        for target, locations in (
            ("/media/movies/Film", ["/media/movies", "/media/movies"]),
            ("//NAS/Media/Film", [r"\\NAS\Media", "//nas/media/"]),
            ("D:/STRASSE/Film", ["d:/Straße", "D:/STRASSE"]),
        ):
            folders = [
                {"id": library_id, "locations": [location]}
                for library_id, location in zip(("lib-a", "lib-b"), locations)
            ]
            for label, client in self._clients():
                with self.subTest(server=label, target=target):
                    recorder = _RefreshRecorder(client, folders, {})

                    result = client.refresh_for_paths([target])

                    self.assertFalse(result["ok"])
                    self.assertEqual(result["scope"], "skipped")
                    self.assertEqual(result["unmatched"], 1)
                    self.assertEqual(result["ambiguous"], 1)
                    self.assertEqual(result["unmatched_paths"], [target])
                    self.assertIn("匹配多个媒体库", result["fallback"])
                    self.assertEqual(recorder.refreshed, [])
                    recorder.posts.assert_not_called()
                    recorder.unexpected_request.assert_not_called()

    def test_canonical_locations_in_same_library_do_not_block_mapped_refresh(self):
        target = "/data/strm/Film.strm"
        for label, client in self._clients(path_mappings=(MediaServerPathMapping("/data/strm", "//NAS/Media"),)):
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, [
                    {"id": "movies", "locations": [r"\\NAS\Media", "//nas/media/"]},
                ], {"movies": [{"Id": "movie", "Type": "Movie", "Path": "//nas/media/Film.strm"}]})
                result = client.refresh_for_paths([target], allow_library_fallback=False)
                self.assertTrue(result["ok"])
                self.assertEqual(result["ambiguous"], 0)
                self.assertEqual(result["succeeded_paths"], [target])
                self.assertEqual(recorder.refreshed, ["movie"])
                recorder.posts.assert_not_called()

    def test_bound_local_library_can_refresh_duplicate_root_while_unbound_strm_is_ambiguous(self):
        from app.modules.media_refresh_coordinator import MediaRefreshCoordinator

        target = "/media/movies/Film.strm"
        folders = [{"id": key, "name": f"电影 {key.upper()}", "locations": ["/media/movies"]} for key in ("a", "b")]
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, folders, {"a": [{"Id": "movie", "Type": "Movie", "Path": target}]})
                allowed = MediaRefreshCoordinator._resolve_library_binding(client, {
                    "library_binding": {"id": "a", "name": "电影 A"},
                })
                local = client.refresh_for_paths([target], allowed_library_ids=allowed)
                self.assertTrue(local["ok"])
                self.assertEqual(local["allowed_libraries"], ["a"])
                self.assertEqual(local["ambiguous"], 0)
                self.assertEqual(local["succeeded_paths"], [target])
                self.assertEqual(recorder.refreshed, ["movie"])
                recorder.refreshed.clear()
                strm = client.refresh_for_paths([target])
                self.assertFalse(strm["ok"])
                self.assertEqual(strm["allowed_libraries"], [])
                self.assertEqual(strm["ambiguous"], 1)
                self.assertEqual(strm["unmatched_paths"], [target])
                self.assertEqual(recorder.refreshed, [])
                recorder.posts.assert_not_called()

    def test_empty_and_dot_library_locations_cannot_trigger_posix_refresh(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, [
                    {"id": "bad", "locations": [None, "", " ", ".", "./"]},
                ], {})
                result = client.refresh_for_paths(["/media/Film.strm"])
                self.assertFalse(result["ok"])
                self.assertEqual(result["scope"], "skipped")
                self.assertEqual(result["matched"], 0)
                self.assertEqual(recorder.refreshed, [])
                recorder.posts.assert_not_called()

    def test_ambiguous_and_unique_paths_only_refresh_the_unique_target(self):
        ambiguous = "/media/movies/Film.strm"
        unique = "/media/movies/anime/Film.strm"
        folders = [
            {"id": "a", "locations": ["/media/movies"]},
            {"id": "b", "locations": ["/media/movies"]},
            {"id": "anime", "locations": ["/media/movies/anime"]},
        ]
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, folders, {"anime": [
                    {"Id": "movie", "Type": "Movie", "Path": unique},
                ]})
                result = client.refresh_for_paths([ambiguous, unique], allow_library_fallback=False)
                self.assertEqual(result["matched"], 1)
                self.assertEqual(result["ambiguous"], 1)
                self.assertEqual(result["succeeded_paths"], [unique])
                self.assertEqual(result["unmatched_paths"], [ambiguous])
                self.assertEqual(recorder.refreshed, ["movie"])
                self.assertEqual(result["libraries"], [])
                recorder.posts.assert_not_called()

    def test_ambiguous_target_never_uses_opt_in_global_fallback(self):
        folders = [
            {"id": "lib-a", "name": "电影 A", "locations": ["/media/movies"]},
            {"id": "lib-b", "name": "电影 B", "locations": ["/media/movies"]},
        ]
        clients = (
            JellyfinClient("http://jf", "token"),
            EmbyClient("http://emby", "token"),
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
                recorder.posts.assert_not_called()

    def test_partial_match_refreshes_safe_target_without_global_scan(self):
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths([
                    f"{ROOT}/剧集/作品 A/Season 01",
                    "/somewhere/else/A",
                ])

                self.assertEqual(recorder.refreshed, ["series-a"])
                recorder.posts.assert_not_called()
                self.assertFalse(result["ok"])
                self.assertEqual(result["scope"], "item")
                self.assertEqual(result["matched"], 1)
                self.assertEqual(result["unmatched"], 1)
                self.assertIn("安全跳过", result["fallback"])
                self.assertEqual(result["succeeded_paths"], [
                    f"{ROOT}/剧集/作品 A/Season 01",
                ])

    def test_successful_paths_preserve_input_spelling_and_exclude_unsubmitted_targets(self):
        paths = [
            r"D:\STRM\剧集\作品 A\Season 01",
            "D:/STRM/剧集/作品 A/Season 01",
            "D:/STRM/电影/作品 B",
            "D:/outside/unmapped",
        ]
        for label, client in self._clients():
            for deferred in (False, True):
                with self.subTest(server=label, deferred=deferred):
                    client.path_mappings = (MediaServerPathMapping("D:/STRM", ROOT),)
                    _RefreshRecorder(client, _folders(), _items())
                    client.refresh_library = Mock(side_effect=lambda item: item != "movie-b")
                    result = client.refresh_for_paths(
                        paths, skip_item_ids=("movie-b",) if deferred else (),
                    )

                    self.assertFalse(result["ok"])
                    self.assertEqual(result["succeeded_paths"], paths[:2])
                    self.assertEqual(result["succeeded_target_ids"], ["series-a"])
                    self.assertEqual(result["retryable"], not deferred)
                    self.assertEqual(result["deduplicated"], int(deferred))
                    self.assertEqual(result["deferred_paths"], paths[2:3] if deferred else [])
                    self.assertEqual(result["deferred_targets"], dict.fromkeys(paths[2:3], "movie-b") if deferred else {})

    def test_dedupe_receipt_survives_other_library_read_failure(self):
        paths = [f"{ROOT}/剧集/作品 A/Season 01", f"{ROOT}/电影/作品 B", "/outside/unmapped"]
        for label, client in self._clients():
            with self.subTest(server=label):
                recorder = _RefreshRecorder(client, _folders(), _items())

                def listing(library_id):
                    if library_id == "lib-movie":
                        raise TimeoutError("movie listing unavailable")
                    return _items()[library_id]

                client._library_items_with_paths = listing
                result = client.refresh_for_paths(paths, skip_item_ids=("series-a",))
                self.assertFalse(result["ok"])
                self.assertTrue(result["retryable"])
                self.assertEqual(result["deferred_paths"], paths[:1])
                self.assertEqual(result["deferred_targets"], {paths[0]: "series-a"})
                self.assertEqual(result["unmatched_paths"], paths[2:])
                self.assertEqual(recorder.refreshed, [])
                recorder.posts.assert_not_called()

    def test_partial_match_never_uses_opt_in_global_fallback(self):
        clients = (
            JellyfinClient("http://jf", "token"),
            EmbyClient("http://emby", "token"),
        )
        for client in clients:
            with self.subTest(server=client.display_name):
                recorder = _RefreshRecorder(client, _folders(), _items())

                result = client.refresh_for_paths([
                    f"{ROOT}/剧集/作品 A/Season 01",
                    "/somewhere/else/A",
                ])

                self.assertEqual(recorder.refreshed, ["series-a"])
                recorder.posts.assert_not_called()
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

                recorder.posts.assert_not_called()
                self.assertTrue(result["ok"])
                self.assertTrue(result["skipped"])
                self.assertTrue(result["fallback"])

    @staticmethod
    def _raise():
        raise RuntimeError("接口失败")


class SchedulerRefreshWiringTests(unittest.TestCase):
    """调度器只规划变化路径并提交统一持久刷新队列。"""

    def setUp(self):
        self.enqueue_outbox = patch(
            "app.repositories.strm.enqueue_strm_refresh_paths"
        ).start()
        self.ack_outbox = patch(
            "app.repositories.strm.acknowledge_strm_refresh_paths"
        ).start()
        self.register_handoff = patch(
            "app.repositories.media_refresh_queue.register_media_refresh_handoff"
        ).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def _settings():
        return {"STRM_ROOT": "/data/strm"}

    def test_changed_paths_are_queued_after_precise_planning(self):
        from app.modules.scheduler import STRMScheduler

        entries = [{
            "path": f"{ROOT}/剧集/作品 A/Season 01/E01.strm",
            "allow_emby": True,
            "event_token": "a" * 32,
        }]
        self.enqueue_outbox.return_value = entries
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
        self.enqueue_outbox.assert_called_once()
        self.assertEqual(self.enqueue_outbox.call_args.args[0], [f"{ROOT}/剧集/作品 A/Season 01/E01.strm"])
        self.assertTrue(self.enqueue_outbox.call_args.kwargs["policy"].allow_emby)
        self.register_handoff.assert_called_once()
        self.assertEqual(self.register_handoff.call_args.args, (entries,))
        self.assertIsNone(self.register_handoff.call_args.kwargs["run_id"])
        self.ack_outbox.assert_called_once_with(entries)
        enqueue.assert_called_once_with(
            [f"{ROOT}/剧集/作品 A/Season 01/E01.strm"],
            immediate=False,
            allow_emby=True,
            source_entries=entries,
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

    def test_frozen_provider_boundary_only_disables_emby(self):
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
                refresh_policy=StrmRefreshPolicy(allow_emby=False),
                has_changes=True,
                changed_dirs=[f"{ROOT}/剧集/作品 A"],
            )

        self.assertEqual(results, {"Jellyfin": "queued"})
        self.enqueue_outbox.assert_called_once()
        self.assertEqual(self.enqueue_outbox.call_args.args[0], [f"{ROOT}/剧集/作品 A"])
        self.assertFalse(self.enqueue_outbox.call_args.kwargs["policy"].allow_emby)
        self.ack_outbox.assert_called_once_with(self.enqueue_outbox.return_value)
        self.assertFalse(enqueue.call_args.kwargs["allow_emby"])

    def test_unified_refresh_override_disables_queueing(self):
        from app.modules.scheduler import STRMScheduler

        with patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
        ) as enqueue:
            result = STRMScheduler._refresh_media_servers(
                refresh_policy=StrmRefreshPolicy(enabled=False),
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
        self.enqueue_outbox.assert_called_once()
        self.ack_outbox.assert_not_called()

    def test_queue_exception_is_reported_and_keeps_durable_outbox(self):
        from app.modules.scheduler import STRMScheduler

        settings = self._settings()
        with patch(
            "app.modules.scheduler.get",
            side_effect=lambda key, default="": settings.get(key, default),
        ), patch(
            "app.modules.media_refresh_coordinator.enqueue_media_refresh_paths",
            side_effect=RuntimeError("queue unavailable"),
        ):
            results = STRMScheduler._refresh_media_servers(
                has_changes=True, changed_dirs=[f"{ROOT}/剧集/作品 A"],
            )

        self.assertEqual(results, {"媒体库": "failed"})
        self.enqueue_outbox.assert_called_once()
        self.ack_outbox.assert_not_called()

    def test_no_changes_skips_refresh_entirely(self):
        from app.modules.scheduler import STRMScheduler

        self.assertEqual(STRMScheduler._refresh_media_servers(has_changes=False), {})


if __name__ == "__main__":
    unittest.main()
