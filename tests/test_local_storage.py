"""本地文件快照与安全扫描测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.modules.local_storage import (
    LocalContentChanged,
    LocalFilesystemAdapter,
    LocalScanLimitExceeded,
    LocalStorageError,
    snapshot_digest,
)


class LocalStorageTests(unittest.TestCase):
    def test_snapshot_detects_size_mtime_or_inode_change(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            video = root / "Movie.mkv"
            video.write_bytes(b"before")
            adapter = LocalFilesystemAdapter(root)
            snapshot = adapter.snapshot(video)
            replacement = root / "replacement"
            replacement.write_bytes(b"changed-content")
            os.replace(replacement, video)
            with self.assertRaises(LocalContentChanged):
                adapter.verify_snapshot(snapshot)

    def test_scan_filters_temporary_zero_and_symlink_but_keeps_companions(self):
        with tempfile.TemporaryDirectory() as root_raw, tempfile.TemporaryDirectory() as outside_raw:
            root = Path(root_raw)
            (root / "Show.S01E01.mkv").write_bytes(b"video")
            (root / "Show.S01E01.zh.ass").write_text("subtitle")
            (root / "poster.jpg").write_bytes(b"image")
            (root / "readme.txt").write_text("not media")
            (root / "archive.zip").write_bytes(b"archive")
            (root / "empty.mkv").touch()
            (root / "downloading.mkv.!qB").write_bytes(b"partial")
            outside = Path(outside_raw) / "outside.mkv"
            outside.write_bytes(b"outside")
            try:
                (root / "linked.mkv").symlink_to(outside)
            except (OSError, NotImplementedError):
                pass
            snapshots = LocalFilesystemAdapter(root).scan()
            self.assertEqual(
                [(item.relative_path, item.role) for item in snapshots],
                [
                    ("poster.jpg", "image"),
                    ("Show.S01E01.mkv", "video"),
                    ("Show.S01E01.zh.ass", "subtitle"),
                ],
            )
            self.assertEqual(snapshot_digest(snapshots), snapshot_digest(list(reversed(snapshots))))

    def test_contains_video_filters_non_media_only_directories(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            non_media = root / "Documents"
            nested_media = root / "Show" / "Season 01"
            non_media.mkdir()
            nested_media.mkdir(parents=True)
            (non_media / "readme.txt").write_text("notes")
            (non_media / "archive.zip").write_bytes(b"archive")
            (nested_media / "Show.S01E01.mkv").write_bytes(b"video")

            adapter = LocalFilesystemAdapter(root)

            self.assertFalse(adapter.contains_video(non_media))
            self.assertTrue(adapter.contains_video(nested_media.parent))
            self.assertFalse(adapter.contains_video(non_media / "readme.txt"))
            self.assertTrue(adapter.contains_video(nested_media / "Show.S01E01.mkv"))

    def test_single_video_scan_includes_only_its_uniquely_matched_subtitles(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            episode_one = root / "Show.S01E01.mkv"
            episode_two = root / "Show.S01E02.mkv"
            episode_one.write_bytes(b"episode-one")
            episode_two.write_bytes(b"episode-two")
            (root / "Show.S01E01.zh-Hans.ass").write_bytes(b"subtitle-one")
            (root / "Show.S01E02.en.srt").write_bytes(b"subtitle-two")
            (root / "unmatched.srt").write_bytes(b"unmatched")
            (root / "poster.jpg").write_bytes(b"poster")

            snapshots = LocalFilesystemAdapter(root).scan(episode_one)

            self.assertEqual(
                [(item.path.name, item.role) for item in snapshots],
                [
                    ("Show.S01E01.mkv", "video"),
                    ("Show.S01E01.zh-Hans.ass", "subtitle"),
                ],
            )

    def test_contains_video_distinguishes_missing_and_unreadable_paths(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            blocked = root / "blocked"
            blocked.mkdir()
            adapter = LocalFilesystemAdapter(root)

            with self.assertRaisesRegex(LocalContentChanged, "扫描路径不存在"):
                adapter.contains_video(root / "Movie.mkv")

            def failed_walk(_path, *, followlinks, onerror):
                self.assertFalse(followlinks)
                onerror(PermissionError("denied"))
                return []

            with patch("app.modules.local_storage.os.walk", side_effect=failed_walk):
                with self.assertRaisesRegex(LocalStorageError, "目录暂时不可完整读取"):
                    adapter.contains_video(blocked)

    def test_scan_prunes_system_and_temporary_directories_by_exact_name(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            normal = root / "Tempest.Show"
            normal.mkdir()
            (normal / "Tempest.Show.S01E01.mkv").write_bytes(b"video")
            for name in ("@eaDir", "temp", "TMP", ".mediaflux-trash", "#recycle"):
                ignored = root / name
                ignored.mkdir()
                (ignored / "Ignored.mkv").write_bytes(b"ignored")

            snapshots = LocalFilesystemAdapter(root).scan()

            self.assertEqual(
                [item.relative_path for item in snapshots],
                ["Tempest.Show/Tempest.Show.S01E01.mkv"],
            )
            self.assertEqual(LocalFilesystemAdapter(root).scan(root / "temp"), [])

    def test_item_and_depth_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            for index in range(3):
                (root / f"{index}.mkv").write_bytes(b"x")
            with self.assertRaises(LocalScanLimitExceeded):
                LocalFilesystemAdapter(root, item_limit=2).scan()

            deep = root / "a" / "b" / "c"
            deep.mkdir(parents=True)
            (deep / "A.mkv").write_bytes(b"x")
            with self.assertRaises(LocalScanLimitExceeded):
                LocalFilesystemAdapter(root, depth_limit=1).scan()

    def test_same_filesystem_and_available_space(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source = root / "A.mkv"
            source.write_bytes(b"x")
            self.assertTrue(LocalFilesystemAdapter.same_filesystem(source, root / "library" / "A.mkv"))
            self.assertGreater(LocalFilesystemAdapter.available_space(root / "library"), 0)


if __name__ == "__main__":
    unittest.main()
