"""明确垃圾直删和未知文件保留测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.modules.local_media_cleanup import (
    classify_cleanup_items,
    delete_cleanup_items,
    discover_cleanup_candidates,
)
from app.modules.local_storage import LocalFilesystemAdapter


class LocalMediaCleanupTests(unittest.TestCase):
    def test_cleanup_discovery_ignores_system_and_temporary_directories(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            group = root / "Movie"; group.mkdir()
            normal = group / "广告说明.txt"; normal.write_bytes(b"ad")
            for name in ("@eaDir", "temp"):
                ignored = group / name
                ignored.mkdir()
                (ignored / "广告说明.txt").write_bytes(b"ad")

            candidates = discover_cleanup_candidates(root, group)

            self.assertEqual([item.snapshot.path for item in candidates], [normal])
            self.assertEqual(discover_cleanup_candidates(root, group / "temp"), [])

    def test_only_confident_junk_is_classified_and_unknown_is_retained(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            files = {
                "下载必看.txt": b"ad",
                "site.url": b"url",
                "Movie.sample.mkv": b"sample",
                "download.mkv.!qB": b"partial",
                "Thumbs.db": b"cache",
                "empty.dat": b"",
                "fonts.zip": b"font",
                "chapters.xml": b"chapter",
                "extra.bin": b"unknown",
            }
            for name, data in files.items():
                (root / name).write_bytes(data)
            snapshots = [LocalFilesystemAdapter(root).snapshot(root / name) for name in files]
            cleanup, retained = classify_cleanup_items(snapshots, primary_video_count=1)
            self.assertEqual(
                {item.snapshot.path.name for item in cleanup},
                {"下载必看.txt", "site.url", "Movie.sample.mkv", "download.mkv.!qB", "Thumbs.db", "empty.dat"},
            )
            self.assertEqual({item.path.name for item in retained}, {"fonts.zip", "chapters.xml", "extra.bin"})

    def test_delete_rechecks_snapshot_and_removes_only_empty_subdirectories(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); group = root / "Movie"; group.mkdir()
            junk = group / "广告说明.txt"; junk.write_bytes(b"ad")
            changed = group / "site.url"; changed.write_bytes(b"before")
            adapter = LocalFilesystemAdapter(root)
            cleanup, _ = classify_cleanup_items([adapter.snapshot(junk), adapter.snapshot(changed)])
            changed.write_bytes(b"changed")
            result = delete_cleanup_items(cleanup, allowed_root=root, selected_path=group)
            self.assertFalse(junk.exists())
            self.assertTrue(changed.exists())
            self.assertTrue(group.exists())
            self.assertEqual(len(result.deleted), 1)
            self.assertEqual(len(result.warnings), 1)

    def test_empty_source_directory_is_removed_but_root_is_never_removed(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); group = root / "Movie"; group.mkdir()
            junk = group / ".DS_Store"; junk.write_bytes(b"x")
            cleanup, _ = classify_cleanup_items([LocalFilesystemAdapter(root).snapshot(junk)])
            result = delete_cleanup_items(cleanup, allowed_root=root, selected_path=group)
            self.assertFalse(group.exists())
            self.assertTrue(root.exists())
            self.assertIn(str(group), result.removed_dirs)

    def test_junk_cleanup_can_keep_empty_directory_when_switch_is_disabled(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); group = root / "Movie"; group.mkdir()
            junk = group / ".DS_Store"; junk.write_bytes(b"x")
            cleanup, _ = classify_cleanup_items([LocalFilesystemAdapter(root).snapshot(junk)])
            result = delete_cleanup_items(
                cleanup,
                allowed_root=root,
                selected_path=group,
                remove_empty_dirs=False,
            )
            self.assertFalse(junk.exists())
            self.assertTrue(group.exists())
            self.assertEqual(result.removed_dirs, [])

    def test_empty_selected_directory_is_removed_even_without_junk_candidates(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); group = root / "Show"; group.mkdir()
            result = delete_cleanup_items(
                [], allowed_root=root, selected_path=group, remove_empty_dirs=True,
            )
            self.assertFalse(group.exists())
            self.assertTrue(root.exists())
            self.assertIn(str(group), result.removed_dirs)

    def test_parent_of_moved_single_file_is_removed_when_empty(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); group = root / "Show"; group.mkdir()
            moved_file = group / "Show.S01E01.mkv"
            result = delete_cleanup_items(
                [], allowed_root=root, selected_path=moved_file, remove_empty_dirs=True,
            )
            self.assertFalse(group.exists())
            self.assertTrue(root.exists())
            self.assertIn(str(group), result.removed_dirs)


if __name__ == "__main__":
    unittest.main()
