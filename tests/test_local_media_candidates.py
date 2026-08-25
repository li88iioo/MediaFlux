"""本地媒体候选发现与回收目录句柄边界测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.modules.local_media_candidates import (
    discover_local_media_candidates,
    move_candidate_to_trash,
)


class LocalMediaCandidateTests(unittest.TestCase):
    def test_discovery_filters_sample_only_beside_primary_video(self) -> None:
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            movie = root / "Movie"
            movie.mkdir()
            primary = movie / "Movie.mkv"
            sample = movie / "Movie.sample.mkv"
            primary.write_bytes(b"primary")
            sample.write_bytes(b"sample")

            candidates, error = discover_local_media_candidates(
                SimpleNamespace(local_root=str(root))
            )

            self.assertEqual(error, "")
            self.assertEqual(candidates, [primary])

    def test_discovery_keeps_standalone_proof_video(self) -> None:
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            proof = root / "Proof.mkv"
            proof.write_bytes(b"standalone proof")

            candidates, error = discover_local_media_candidates(
                SimpleNamespace(local_root=str(root))
            )

            self.assertEqual(error, "")
            self.assertEqual(candidates, [proof])

    def test_rollback_never_overwrites_recreated_root_file(self) -> None:
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            candidate = root / "Movie.mkv"
            candidate.write_bytes(b"old")
            info = candidate.lstat()
            identity = {
                "size": int(info.st_size),
                "mtime_ns": int(info.st_mtime_ns),
                "device": int(info.st_dev),
                "inode": int(info.st_ino),
            }
            original_replace = os.replace
            moved_once = False

            def recreate_after_move(src, dst, *args, **kwargs):
                nonlocal moved_once
                result = original_replace(src, dst, *args, **kwargs)
                if not moved_once and kwargs.get("dst_dir_fd") is not None:
                    moved_once = True
                    candidate.write_bytes(b"new producer data")
                    (root / ".mediaflux-trash" / str(dst)).write_bytes(b"changed trash data")
                return result

            with patch(
                "app.modules.local_media_candidates.os.replace",
                side_effect=recreate_after_move,
            ), self.assertRaises(Exception):
                move_candidate_to_trash(
                    SimpleNamespace(local_root=str(root)), candidate, identity,
                )

            self.assertEqual(candidate.read_bytes(), b"new producer data")
            retained = list((root / ".mediaflux-trash").iterdir())
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), b"changed trash data")

    def test_transient_trash_path_swap_cannot_redirect_media_outside_source(self) -> None:
        with tempfile.TemporaryDirectory() as root_raw, tempfile.TemporaryDirectory() as outside_raw:
            root = Path(root_raw)
            outside = Path(outside_raw)
            candidate = root / "Movie"
            candidate.mkdir()
            (candidate / "Movie.mkv").write_bytes(b"movie")
            info = candidate.lstat()
            identity = {
                "size": int(info.st_size),
                "mtime_ns": int(info.st_mtime_ns),
                "device": int(info.st_dev),
                "inode": int(info.st_ino),
            }
            trash = root / ".mediaflux-trash"
            trash.mkdir()
            displaced = root / ".mediaflux-trash-pinned"
            original_replace = os.replace
            swapped = False

            def swap_trash_then_move(src, dst, *args, **kwargs):
                nonlocal swapped
                if not swapped and kwargs.get("dst_dir_fd") is not None:
                    swapped = True
                    trash.rename(displaced)
                    trash.symlink_to(outside, target_is_directory=True)
                    try:
                        return original_replace(src, dst, *args, **kwargs)
                    finally:
                        trash.unlink()
                        displaced.rename(trash)
                return original_replace(src, dst, *args, **kwargs)

            with patch(
                "app.modules.local_media_candidates.os.replace",
                side_effect=swap_trash_then_move,
            ):
                destination = move_candidate_to_trash(
                    SimpleNamespace(local_root=str(root)), candidate, identity,
                )

            self.assertTrue(swapped)
            self.assertFalse(candidate.exists())
            self.assertTrue(destination.exists())
            self.assertTrue((destination / "Movie.mkv").exists())
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
