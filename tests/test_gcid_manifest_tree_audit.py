"""GCID 导出、校验和预览必须共享能落为文件树的路径合同。"""

from __future__ import annotations

from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock, call, patch

from app.clients.guangya import GuangYaClient, GuangYaFile
from app.modules import gcid_import
from app.modules.gcid_manifest import (
    ManifestValidationError,
    export_manifest,
    normalize_manifest_v2,
)
from tests.test_gcid_import import _manifest


class GCIDManifestTreeAuditTests(TestCase):
    def test_file_directory_prefix_collisions_are_rejected_in_any_order(self):
        for parent, child in (
            ("Film", "Film/a.mkv"),
            ("Film", "film/sub/a.mkv"),
            ("Film/Part", "Film/Part/a.mkv"),
        ):
            rows = [
                {"path": parent, "size": 1, "gcid": "a"},
                {"path": child, "size": 2, "gcid": "b"},
            ]
            for items in (rows, list(reversed(rows))):
                with (
                    self.subTest(paths=[r["path"] for r in items]),
                    self.assertRaisesRegex(ManifestValidationError, "文件.*目录.*冲突"),
                ):
                    normalize_manifest_v2(
                        {
                            **_manifest(
                                sorted(rows, key=lambda r: r["path"].casefold())
                            ),
                            "files": items,
                        }
                    )

    def test_invalid_tree_is_rejected_before_preview_is_retained(self):
        payload = _manifest(
            [
                {"path": "Film", "size": 1, "gcid": "a"},
                {"path": "Film/a.mkv", "size": 2, "gcid": "b"},
            ]
        )
        with patch.object(
            gcid_import._preview_store,
            "create",
            wraps=gcid_import._preview_store.create,
        ) as create:
            with self.assertRaisesRegex(ManifestValidationError, "文件.*目录.*冲突"):
                gcid_import.create_preview(
                    payload, target_dir_id="target", owner_id="audit"
                )
        create.assert_not_called()

    def test_export_cannot_emit_unimportable_sanitized_path_collisions(self):
        for names in (("A/B.mkv", "A\\B.mkv"), ("A.mkv", "a.mkv")):
            client = Mock()
            client.list_dir.return_value = [
                GuangYaFile(str(i), name, False, i + 1, f"gcid-{i}", "root", md5="a" * 32)
                for i, name in enumerate(names)
            ]
            with (
                self.subTest(names=names),
                self.assertRaisesRegex(ManifestValidationError, "重复路径"),
            ):
                export_manifest(client, "root")
            client.file_info.assert_not_called()

    def test_export_retains_verified_files_without_md5(self):
        no_md5 = GuangYaFile("b", "A.mkv", False, 2, "b" * 32, "folder")
        tree = {
            "root": [
                GuangYaFile("a", "B.mkv", False, 1, "gcid-a", "root", md5="a" * 32),
                GuangYaFile("folder", "影片", True),
            ],
            "folder": [no_md5],
        }
        for md5 in ("", "invalid-md5"):
            with self.subTest(md5=md5):
                client = Mock()
                client.list_dir.side_effect = tree.__getitem__
                client.file_info.return_value = replace(no_md5, md5=md5)

                payload = export_manifest(client, "root")

                client.file_info.assert_called_once_with("b")
                self.assertEqual(payload["files"], [
                    {"path": "B.mkv", "size": 1, "gcid": "gcid-a", "md5": "a" * 32},
                    {"path": "影片/A.mkv", "size": 2, "gcid": "b" * 32},
                ])
                self.assertEqual(normalize_manifest_v2(payload).to_dict(), payload)

    def test_export_rejects_detail_with_mismatched_file_identity(self):
        listed = GuangYaFile("a", "A.mkv", False, 1, "gcid-a", "root")
        detail = replace(listed, md5="a" * 32)
        client = Mock()
        client.list_dir.return_value = [listed]
        for invalid_detail in (
            replace(detail, file_id="b"),
            replace(detail, is_dir=True),
            replace(detail, size=2),
            replace(detail, etag="gcid-b"),
        ):
            with self.subTest(detail=invalid_detail), self.assertRaisesRegex(ValueError, r"身份不一致.*A\.mkv"):
                client.file_info.return_value = invalid_detail
                export_manifest(client, "root")

    def test_export_fills_missing_gcid_using_verified_detail(self):
        listed = GuangYaFile("a", "A.mkv", False, 1, parent_id="root", md5="a" * 32)
        detail = replace(listed, etag="gcid-a")
        client = Mock()
        client.list_dir.return_value = [listed]
        client.file_info.return_value = detail

        payload = export_manifest(client, "root")

        client.file_info.assert_called_once_with("a")
        self.assertEqual(payload["files"], [
            {"path": "A.mkv", "size": 1, "gcid": "gcid-a", "md5": "a" * 32},
        ])

        client.file_info.return_value = replace(detail, md5="b" * 32)
        with self.assertRaisesRegex(ValueError, r"身份不一致.*A\.mkv"):
            export_manifest(client, "root")

    def test_export_propagates_detail_request_failure(self):
        client = Mock()
        client.list_dir.return_value = [GuangYaFile("a", "A.mkv", False, 1, "gcid-a", "root")]
        client.file_info.side_effect = RuntimeError("光鸭详情请求失败")
        progress = Mock()

        with self.assertRaisesRegex(RuntimeError, "光鸭详情请求失败"):
            export_manifest(client, "root", on_progress=progress)
        progress.assert_called_once_with(1, 0)

    def test_valid_prefix_siblings_empty_and_normal_export_roundtrip(self):
        client = Mock()
        client.list_dir.side_effect = lambda directory: {
            "root": [
                GuangYaFile("a", "Film", False, 1, "a", "root", md5="a" * 32),
                GuangYaFile("folder", "Film-more", True, 0, "", "root"),
            ],
            "folder": [GuangYaFile("b", "a.mkv", False, 2, "b", "folder", md5="b" * 32)],
        }[directory]
        progress = Mock()
        payload = GuangYaClient.generate_gcid_json(client, "root", "历史导出", on_progress=progress)
        self.assertEqual(progress.call_args_list, [call(1, 0), call(1, 1), call(2, 1), call(2, 2)])
        self.assertEqual(normalize_manifest_v2(payload).to_dict(), payload)
        with patch.object(gcid_import, "_preview_account_key", return_value="test-account"):
            preview = gcid_import.create_preview(
                payload, target_dir_id="target", owner_id="audit"
            )
        self.assertEqual((preview["file_count"], preview["total_size"]), (2, 3))
        self.assertEqual(len(preview["tree"]), 2)
        client.list_dir.side_effect = None
        client.list_dir.return_value = []
        progress.reset_mock()
        empty = export_manifest(client, "root", on_progress=progress)
        self.assertEqual(normalize_manifest_v2(empty).files, ())
        progress.assert_called_once_with(1, 0)
