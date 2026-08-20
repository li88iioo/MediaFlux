"""本地文件快照与安全扫描测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.modules.local_storage import (
    LocalContentChanged,
    LocalFilesystemAdapter,
    LocalScanLimitExceeded,
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
