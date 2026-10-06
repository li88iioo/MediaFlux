"""离线解析索引严格校验；仅用合成文件树，禁止真实网络。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.clients.guangya import GuangYaClient


class GuangYaOfflineIndexValidationTests(unittest.TestCase):
    def setUp(self):
        for target in (
            "socket.socket.connect",
            "socket.socket.connect_ex",
            "socket.create_connection",
            "socket.getaddrinfo",
        ):
            guard = patch(target, side_effect=AssertionError("external network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    @staticmethod
    def manifest(items, tree_key="subfiles"):
        return {
            "msg": "success",
            "data": {
                "btResInfo": {
                    "infoHash": "synthetic-index-fixture",
                    "subfilesNum": len(items),
                    tree_key: items,
                },
            },
        }

    def test_explicit_invalid_first_index_cannot_be_recovered_as_zero(self):
        invalid_values = (
            False, True, None, "", " ", -1, "-1", -1.0, -0.5, 1.75,
            "1.75", "not-an-index", [], {}, float("inf"), float("-inf"), float("nan"),
        )
        for value in invalid_values:
            with self.subTest(value=value):
                response = self.manifest([
                    {"fileIndex": value, "fileName": "First.mkv", "fileSize": 100},
                    {"fileIndex": 1, "fileName": "Second.mkv", "fileSize": 200},
                ])
                with self.assertRaisesRegex(ValueError, "解析结果包含无效文件索引"):
                    GuangYaClient.normalize_offline_files(response)

    def test_known_trees_fail_closed_instead_of_returning_only_valid_files(self):
        for tree_key in ("subfiles", "subFiles", "files", "fileList", "file_list"):
            for value in (False, None, -1, -0.5, 1.75, "invalid"):
                with self.subTest(tree_key=tree_key, value=value):
                    response = self.manifest([
                        {"fileIndex": 7, "name": "Valid.mkv", "size": 100},
                        {"fileIndex": value, "name": "Invalid.mkv", "size": 200},
                    ], tree_key)
                    with self.assertRaisesRegex(ValueError, "解析结果包含无效文件索引"):
                        GuangYaClient.normalize_offline_files(response)

    def test_each_recognized_index_alias_rejects_invalid_values(self):
        for key in (
            "fileIndex", "file_index", "fileIdx", "file_idx", "selectIndex", "select_index",
        ):
            for value in (False, None, "", -1, 0.5, "invalid"):
                with self.subTest(key=key, value=value):
                    response = self.manifest([
                        {key: value, "name": "First.mkv", "size": 100},
                        {"fileIndex": 1, "name": "Second.mkv", "size": 200},
                    ])
                    with self.assertRaisesRegex(ValueError, "解析结果包含无效文件索引"):
                        GuangYaClient.normalize_offline_files(response)

    def test_valid_alias_cannot_hide_an_explicit_invalid_alias(self):
        for indexes in (
            {"fileIndex": None, "file_index": 0},
            {"fileIndex": "", "file_index": 0},
            {"fileIndex": 0, "file_index": False},
            {"fileIndex": 0, "select_index": "invalid"},
        ):
            with self.subTest(indexes=indexes):
                response = self.manifest([{"name": "First.mkv", "size": 100, **indexes}])
                with self.assertRaisesRegex(ValueError, "解析结果包含无效文件索引"):
                    GuangYaClient.normalize_offline_files(response)

    def test_invalid_nested_index_rejects_the_whole_manifest(self):
        response = self.manifest([
            {"fileIndex": 0, "name": "Valid.mkv", "size": 100},
            {"name": "Season", "type": "folder", "subfiles": [
                {"fileIndex": -1, "name": "Invalid.mkv", "size": 200},
            ]},
        ], "files")
        with self.assertRaisesRegex(ValueError, "解析结果包含无效文件索引"):
            GuangYaClient.normalize_offline_files(response)

    def test_invalid_index_error_does_not_echo_the_untrusted_value(self):
        response = self.manifest([
            {"fileIndex": "private-fixture-value", "name": "First.mkv", "size": 100},
        ])
        with self.assertRaisesRegex(ValueError, "解析结果包含无效文件索引") as caught:
            GuangYaClient.normalize_offline_files(response)
        self.assertNotIn("private-fixture-value", str(caught.exception))

    def test_valid_integer_values_keep_their_indexes(self):
        for value, expected in ((0, 0), (7, 7), ("0", 0), ("7", 7), ("007", 7), (" 7 ", 7), (0.0, 0), (7.0, 7)):
            with self.subTest(value=value):
                files = GuangYaClient.normalize_offline_files(self.manifest([
                    {"fileIndex": value, "name": "Valid.mkv", "size": 100},
                ], "files"))
                self.assertEqual(files, [{"index": expected, "name": "Valid.mkv", "size": 100, "excluded": False}])
                self.assertIs(type(files[0]["index"]), int)

    def test_each_recognized_index_alias_accepts_integer_strings(self):
        for key in (
            "fileIndex", "file_index", "fileIdx", "file_idx", "selectIndex", "select_index",
        ):
            with self.subTest(key=key):
                files = GuangYaClient.normalize_offline_files(self.manifest([
                    {key: "0", "name": "Valid.mkv", "size": 100},
                ], "files"))
                self.assertEqual([item["index"] for item in files], [0])

    def test_truly_omitted_zero_is_recovered_in_known_bt_manifests(self):
        for tree_key in ("subfiles", "subFiles"):
            for remaining in ([], [{"fileIndex": "1", "name": "Second.mkv", "size": 200}]):
                with self.subTest(tree_key=tree_key, remaining=remaining):
                    files = GuangYaClient.normalize_offline_files(self.manifest([
                        {"name": "First.mkv", "size": 100}, *remaining,
                    ], tree_key))
                    self.assertEqual([item["index"] for item in files], list(range(1 + len(remaining))))

    def test_unverifiable_missing_index_rejects_incomplete_selection(self):
        for items in (
            [{"name": "Unknown.mkv", "size": 100}, {"fileIndex": 7, "name": "Known.mkv", "size": 200}],
            [{"fileIndex": 0, "name": "Known.mkv", "size": 100}, {"name": "Unknown.mkv", "size": 200}],
        ):
            with self.subTest(items=items), self.assertRaisesRegex(ValueError, "文件索引缺失"):
                GuangYaClient.normalize_offline_files(self.manifest(items))

    def test_nested_bt_tree_recovers_zero_not_tree_position(self):
        # 事故响应结构：先列目录，正片位于根数组中部且省略零值索引。
        response = self.manifest([
            {"fileName": "Sample", "isDir": True, "subfiles": [
                {"fileName": "Sample.mkv", "fileIndex": 4, "fileSize": 331776368},
            ]},
            {"fileName": "Screens", "isDir": True, "subfiles": [
                {"fileName": f"screen-{i}.jpg", "fileIndex": i, "fileSize": 100}
                for i in range(5, 10)
            ]},
            {"fileName": "Movie.nfo", "fileIndex": 1, "fileSize": 100},
            {"fileName": "Movie.1999.4k.mkv", "fileSize": 28255299040},
            {"fileName": "folder.jpg", "fileIndex": 2, "fileSize": 100},
            {"fileName": "logo.png", "fileIndex": 3, "fileSize": 100},
        ])
        info = response["data"]["btResInfo"]
        info["subfilesNum"] = 10
        for excluded in ([], [0]):
            info["excludeIndices"] = excluded
            with self.subTest(excluded=excluded):
                files = GuangYaClient.normalize_offline_files(response)
                self.assertEqual(len(files), 10)
                main = next(item for item in files if item["index"] == 0)
                self.assertEqual(main["size"], 28255299040)
                self.assertEqual(main["name"], "Movie.1999.4k.mkv")
                self.assertEqual(main["excluded"], bool(excluded))
        for count in (9, 11, None):
            info["subfilesNum"] = count
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "文件数量.*不一致"):
                GuangYaClient.normalize_offline_files(response)

    def test_declared_bt_count_cannot_silently_drop_an_entire_leaf(self):
        response = self.manifest([
            {"fileIndex": i, "fileName": f"File-{i}.mkv", "fileSize": 100}
            for i in range(1, 10)
        ])
        response["data"]["btResInfo"]["subfilesNum"] = 10
        with self.assertRaisesRegex(ValueError, "文件数量.*不一致"):
            GuangYaClient.normalize_offline_files(response)

    def test_duplicate_real_indexes_still_fail_closed(self):
        response = self.manifest([
            {"fileIndex": 0, "name": "First.mkv", "size": 100},
            {"fileIndex": "0", "name": "Second.mkv", "size": 200},
        ])
        with self.assertRaisesRegex(ValueError, "解析结果包含重复文件索引: 0"):
            GuangYaClient.normalize_offline_files(response)

    def test_unknown_metadata_lists_are_not_treated_as_file_trees(self):
        response = {
            "data": {
                "trackers": [{"fileIndex": False, "name": "NotAFile"}],
                "files": [
                    {"fileIndex": 0, "name": "Valid.mkv", "size": 100},
                ],
            },
        }
        files = GuangYaClient.normalize_offline_files(response)
        self.assertEqual([item["index"] for item in files], [0])

    def test_known_directories_ignore_placeholder_indexes_and_keep_child_files(self):
        for type_key in ("type", "fileType", "kind", "resType"):
            for item_type in ("folder", "dir", "directory", 2, "2"):
                for placeholder in (-1, None):
                    for tree_key in ("files", "subfiles"):
                        with self.subTest(type_key=type_key, item_type=item_type, placeholder=placeholder, tree_key=tree_key):
                            response = self.manifest([
                                {
                                    type_key: item_type,
                                    "name": "Season",
                                    "fileIndex": placeholder,
                                    "excludeIndices": [3],
                                    "subfiles": [{"fileIndex": 3, "name": "Episode.mkv", "size": 100}],
                                },
                            ], tree_key)
                            files = GuangYaClient.normalize_offline_files(response)
                            self.assertEqual(files, [
                                {"index": 3, "name": "Episode.mkv", "size": 100, "excluded": True},
                            ])

    def test_bt_root_with_directory_does_not_infer_zero_from_array_positions(self):
        response = self.manifest([
            {"name": "Unknown.mkv", "size": 100},
            {"type": "folder", "fileIndex": 1, "name": "Season", "subfiles": [
                {"fileIndex": 2, "name": "Episode.mkv", "size": 200},
            ]},
        ])
        with self.assertRaisesRegex(ValueError, "文件索引缺失"):
            GuangYaClient.normalize_offline_files(response)

    def test_invalid_child_file_still_fails_closed_beneath_placeholder_directory(self):
        response = self.manifest([
            {"type": "directory", "fileIndex": None, "name": "Season", "subfiles": [
                {"fileIndex": 0, "name": "Valid.mkv", "size": 100},
                {"fileIndex": False, "name": "Invalid.mkv", "size": 200},
            ]},
        ])
        with self.assertRaisesRegex(ValueError, "解析结果包含无效文件索引"):
            GuangYaClient.normalize_offline_files(response)
