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
    discover_local_media_directory_candidates,
    move_candidate_to_trash,
)
from app.modules.local_media_cleanup import is_probable_sample_video
from app.modules.local_storage import LocalFilesystemAdapter, LocalScanLimitExceeded
from app.modules.special_media import is_sample_media_path


class LocalMediaCandidateTests(unittest.TestCase):
    def test_shared_sample_path_predicate_is_precise_and_local_limit_stays_bounded(self) -> None:
        for value in (
            "Sample.mkv",
            "downloads/Sample/Blue.Streak.1999.mkv",
            "Movie.sample.mkv",
            "Movie.sample.sample.mkv",
            "Movie.proof.mkv",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_sample_media_path(value))

        for value in (
            "Corps Samples (2021).mkv",
            "Corps Samples (2021)/Blue.Streak.1999.mkv",
            "The.Sample.Movie.2021.mkv",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_sample_media_path(value))

        sample_size = 316_410_000
        self.assertFalse(is_probable_sample_video(Path("Sample.mkv"), sample_size))
        self.assertTrue(is_probable_sample_video(Path("Sample.mkv"), 100_000_000))

    def test_browser_first_video_does_not_probe_remote_depth_or_item_limit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="a5-boundary-") as raw:
            root = Path(raw)
            show = root / "Show"
            remote = show / "Remote"
            show.mkdir()
            (show / "00-first.mkv").write_bytes(b"video")
            remote.mkdir()

            deep = remote
            for index in range(4):
                deep = deep / f"d{index}"
                deep.mkdir()
            (deep / "hidden.txt").write_text("remote")
            (remote / "00-tail.txt").write_text("remote")
            (remote / "01-tail.txt").write_text("remote")

            original_init = LocalFilesystemAdapter.__init__
            original_scandir = os.scandir
            remote_scandir: list[Path] = []

            def init(adapter, path, **kwargs):
                kwargs.update(item_limit=1, depth_limit=1)
                original_init(adapter, path, **kwargs)

            def scandir(path):
                candidate = Path(path)
                if candidate == remote or remote in candidate.parents:
                    remote_scandir.append(candidate)
                return original_scandir(path)

            source = SimpleNamespace(local_root=str(root))
            with patch.object(LocalFilesystemAdapter, "__init__", init), patch.object(
                os, "scandir", side_effect=scandir
            ):
                candidates, error, selected = discover_local_media_directory_candidates(source)

            self.assertEqual(candidates, [show])
            self.assertEqual(error, "")
            self.assertEqual(selected, root)
            self.assertEqual(
                remote_scandir,
                [],
                "浏览命中首个视频后不得进入远端深度/数量超限子树",
            )


    def test_discovery_reuses_directory_names_without_rescanning_each_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            directory = root
            for index in range(8):
                directory /= f"Level{index}"
                directory.mkdir()
            videos = [directory / f"Episode{index:02}.mkv" for index in range(40)]
            for video in videos:
                video.write_bytes(b"video")
            calls: dict[Path, int] = {}
            original = os.scandir

            def scandir(path):
                selected = Path(path)
                calls[selected] = calls.get(selected, 0) + 1
                return original(path)

            with patch("os.scandir", side_effect=scandir):
                candidates, error = discover_local_media_candidates(
                    SimpleNamespace(local_root=str(root))
                )
            self.assertEqual(error, "")
            self.assertEqual(candidates, videos)
            self.assertEqual(len(calls), 9)
            self.assertEqual(set(calls.values()), {1})

    def test_directory_listing_preserves_equal_casefold_entry_order(self) -> None:
        from app.modules.local_media_candidates import discover_local_media_directory_candidates

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            directory = root / "Film.MKV"
            directory.mkdir()
            (directory / "Inside.mkv").write_bytes(b"video")
            video = root / "film.mkv"
            video.write_bytes(b"video")
            expected = sorted(root.iterdir(), key=lambda item: item.name.casefold())
            candidates, error, selected = discover_local_media_directory_candidates(
                SimpleNamespace(local_root=str(root))
            )
            self.assertEqual((candidates, error, selected), (expected, "", root))

    def test_discovery_preserves_root_error_summary_before_nested_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for relative in ("A/Good.mkv", "A/Blocked/Hidden.mkv", "B/Hidden.mkv", "C/Hidden.mkv", "D/Hidden.mkv", "Z/Hidden.mkv"):
                video = root / relative
                video.parent.mkdir(parents=True, exist_ok=True)
                video.write_bytes(b"video")
            blocked = {"A/Blocked", "B", "C", "D", "Z"}
            original = os.scandir

            def scandir(path):
                if Path(path).relative_to(root).as_posix() in blocked:
                    raise PermissionError("fixture")
                return original(path)

            with patch("os.scandir", side_effect=scandir):
                candidates, error = discover_local_media_candidates(
                    SimpleNamespace(local_root=str(root))
                )
            self.assertEqual(candidates, [root / "A/Good.mkv"])
            self.assertEqual(error, "目录扫描不完整：目录暂时不可完整读取: B；目录暂时不可完整读取: C；目录暂时不可完整读取: D；目录暂时不可完整读取: Blocked")

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

    def test_discovery_preserves_good_candidates_and_reports_partial_scan(self) -> None:
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            good = root / "Good.mkv"
            good.write_bytes(b"movie")
            blocked = root / "Blocked"
            blocked.mkdir()

            original = LocalFilesystemAdapter.contains_video

            def contains_video(adapter, path):
                if Path(path).name == "Blocked":
                    raise LocalScanLimitExceeded("目录文件数量超过安全上限")
                return original(adapter, path)

            with patch.object(
                LocalFilesystemAdapter,
                "contains_video",
                autospec=True,
                side_effect=contains_video,
            ):
                candidates, error = discover_local_media_candidates(
                    SimpleNamespace(local_root=str(root))
                )

            self.assertEqual(candidates, [good])
            self.assertIn("目录扫描不完整", error)
            self.assertIn("目录文件数量超过安全上限", error)

    def test_discovery_never_reports_empty_success_when_all_probes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            blocked = root / "Blocked"
            blocked.mkdir()

            with patch.object(
                LocalFilesystemAdapter,
                "contains_video",
                side_effect=LocalScanLimitExceeded("目录扫描深度超过安全上限"),
            ):
                candidates, error = discover_local_media_candidates(
                    SimpleNamespace(local_root=str(root))
                )

            self.assertEqual(candidates, [])
            self.assertIn("目录扫描深度超过安全上限", error)

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
