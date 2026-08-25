"""明确垃圾直删和未知文件保留测试。"""
from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from app.modules.local_media_cleanup import (
    classify_cleanup_items,
    delete_cleanup_items,
    discover_cleanup_candidates,
)
from app.modules.local_storage import LocalFilesystemAdapter


class LocalMediaCleanupTests(unittest.TestCase):
    def test_quarantine_rollback_never_overwrites_recreated_file(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); group = root / "Movie"; group.mkdir()
            junk = group / "广告说明.txt"; junk.write_bytes(b"old")
            cleanup, _ = classify_cleanup_items([
                LocalFilesystemAdapter(root).snapshot(junk)
            ])
            original_replace = os.replace
            moved_once = False

            def recreate_after_move(src, dst, *args, **kwargs):
                nonlocal moved_once
                result = original_replace(src, dst, *args, **kwargs)
                if not moved_once and kwargs.get("dst_dir_fd") is not None:
                    moved_once = True
                    junk.write_bytes(b"new producer data")
                    trash = root / ".mediaflux-trash"
                    run_dir = next(trash.iterdir())
                    (run_dir / str(dst)).write_bytes(b"changed quarantine data")
                return result

            with patch(
                "app.modules.local_media_cleanup.os.replace",
                side_effect=recreate_after_move,
            ):
                result = delete_cleanup_items(
                    cleanup, allowed_root=root, selected_path=group,
                )

            self.assertEqual(junk.read_bytes(), b"new producer data")
            self.assertEqual(result.deleted, [])
            self.assertTrue(any(".mediaflux-trash" in item for item in result.retained))
            self.assertTrue(result.warnings)

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
                {"下载必看.txt", "site.url", "download.mkv.!qB", "Thumbs.db", "empty.dat"},
            )
            self.assertEqual(
                {item.path.name for item in retained},
                {"Movie.sample.mkv", "fonts.zip", "chapters.xml", "extra.bin"},
            )

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

    def test_delete_quarantines_and_restores_file_replaced_after_snapshot_check(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); group = root / "Movie"; group.mkdir()
            junk = group / "广告说明.txt"; junk.write_bytes(b"old")
            adapter = LocalFilesystemAdapter(root)
            cleanup, _ = classify_cleanup_items([adapter.snapshot(junk)])

            original_verify = LocalFilesystemAdapter.verify_snapshot

            def replace_after_verify(current_adapter, snapshot):
                verified = original_verify(current_adapter, snapshot)
                replacement = group / "replacement.tmp"
                replacement.write_bytes(b"new payload")
                os.replace(replacement, junk)
                return verified

            with patch.object(
                LocalFilesystemAdapter,
                "verify_snapshot",
                autospec=True,
                side_effect=replace_after_verify,
            ):
                result = delete_cleanup_items(cleanup, allowed_root=root, selected_path=group)

            self.assertTrue(junk.exists())
            self.assertEqual(junk.read_bytes(), b"new payload")
            self.assertEqual(result.deleted, [])
            self.assertEqual(len(result.warnings), 1)

    def test_source_parent_swap_cannot_delete_file_outside_root(self):
        with tempfile.TemporaryDirectory() as root_raw, tempfile.TemporaryDirectory() as outside_raw:
            root = Path(root_raw); group = root / "Movie"; group.mkdir()
            outside = Path(outside_raw)
            junk = group / "广告说明.txt"; junk.write_bytes(b"ad")
            cleanup, _ = classify_cleanup_items([
                LocalFilesystemAdapter(root).snapshot(junk)
            ])
            original_verify = LocalFilesystemAdapter.verify_snapshot
            relocated = outside / "Movie"

            def swap_source_parent(current_adapter, snapshot):
                verified = original_verify(current_adapter, snapshot)
                group.rename(relocated)
                group.symlink_to(relocated, target_is_directory=True)
                return verified

            with patch.object(
                LocalFilesystemAdapter,
                "verify_snapshot",
                autospec=True,
                side_effect=swap_source_parent,
            ):
                result = delete_cleanup_items(
                    cleanup, allowed_root=root, selected_path=group,
                )

            self.assertTrue((relocated / junk.name).exists())
            self.assertEqual(result.deleted, [])
            self.assertTrue(result.warnings)
            self.assertTrue(any("跳过空目录清理" in item for item in result.warnings))

    def test_quarantine_directory_swap_cannot_redirect_deletion_outside_root(self):
        with tempfile.TemporaryDirectory() as root_raw, tempfile.TemporaryDirectory() as outside_raw:
            root = Path(root_raw); group = root / "Movie"; group.mkdir()
            outside = Path(outside_raw)
            junk = group / "广告说明.txt"; junk.write_bytes(b"ad")
            cleanup, _ = classify_cleanup_items([
                LocalFilesystemAdapter(root).snapshot(junk)
            ])
            original_replace = os.replace
            swapped = False

            def swap_quarantine_parent(src, dst, *args, **kwargs):
                nonlocal swapped
                if not swapped and kwargs.get("dst_dir_fd") is not None:
                    swapped = True
                    trash = root / ".mediaflux-trash"
                    trash.rename(root / ".mediaflux-trash-original")
                    trash.symlink_to(outside, target_is_directory=True)
                return original_replace(src, dst, *args, **kwargs)

            with patch(
                "app.modules.local_media_cleanup.os.replace",
                side_effect=swap_quarantine_parent,
            ):
                result = delete_cleanup_items(
                    cleanup, allowed_root=root, selected_path=group,
                )

            self.assertTrue(swapped)
            self.assertFalse(junk.exists())
            self.assertEqual(result.deleted, [str(junk)])
            self.assertEqual(list(outside.iterdir()), [])

    def test_cleanup_discovery_prunes_ignored_tree_before_item_limit(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); group = root / "Movie"; group.mkdir()
            ignored = group / ".mediaflux-trash"; ignored.mkdir()
            for index in range(20):
                (ignored / f"old-{index}.tmp").write_bytes(b"x")
            junk = group / "广告说明.txt"; junk.write_bytes(b"ad")

            cleanup = discover_cleanup_candidates(
                root,
                group,
                item_limit=2,
            )

            self.assertEqual([item.snapshot.path for item in cleanup], [junk])

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

    def test_empty_directory_parent_swap_cannot_remove_outside_directory(self):
        with tempfile.TemporaryDirectory() as root_raw, tempfile.TemporaryDirectory() as outside_raw:
            root = Path(root_raw)
            container = root / "Container"
            selected = container / "Empty"
            selected.mkdir(parents=True)
            outside = Path(outside_raw)
            outside_empty = outside / "Empty"
            outside_empty.mkdir()
            displaced = root / "Container-pinned"
            original_rmdir = os.rmdir
            swapped = False

            def swap_parent_then_remove(path, *args, **kwargs):
                nonlocal swapped
                if not swapped and str(path) == "Empty" and kwargs.get("dir_fd") is not None:
                    swapped = True
                    container.rename(displaced)
                    container.symlink_to(outside, target_is_directory=True)
                    try:
                        return original_rmdir(path, *args, **kwargs)
                    finally:
                        container.unlink()
                        displaced.rename(container)
                return original_rmdir(path, *args, **kwargs)

            with patch(
                "app.modules.local_media_cleanup.os.rmdir",
                side_effect=swap_parent_then_remove,
            ):
                result = delete_cleanup_items(
                    [], allowed_root=root, selected_path=selected, remove_empty_dirs=True,
                )

            self.assertTrue(swapped)
            self.assertFalse(selected.exists())
            self.assertTrue(outside_empty.exists())
            self.assertIn(str(selected), result.removed_dirs)

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
