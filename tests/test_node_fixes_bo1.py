"""BO1：逐视频检查复用目录名称快照，但不复用文件身份或字幕归属。"""

from __future__ import annotations

from typing import cast
from app.modules.scraper import TMDBScraper

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.repositories import local_media as local_media_repo
from app.modules import local_storage
from app.modules.local_media_candidates import discover_local_media_candidates
from app.modules.local_media_service import LocalMediaService, LocalMediaServiceError
from app.modules.scraper import MatchResult
from app.modules.subtitle_identity import plan_subtitle_companions
from tests.support import IsolatedDatabaseTestCase
from tests.test_local_media_service import FakeScraper


class SiblingBatchTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="node-fixes-bo1-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source_id = local_media_repo.create_local_media_source(
            name=self.root.name,
            qb_profile="",
            qb_path_prefix="",
            local_root=str(self.root),
            owner="admin",
        )
        self.service = LocalMediaService(
            scraper=cast(TMDBScraper, FakeScraper(MatchResult(title="Show", media_type="tv")))
        )
        self.addCleanup(self.service.close)

    def write(self, name, data=b"media"):
        path = self.root / name
        path.write_bytes(data)
        return path

    def inspect(self, path):
        return self.service.inspect_source("admin", self.source_id, path)

    def subtitles(self, path):
        result = self.inspect(path)
        self.service.inspections.discard("admin", result["inspection_id"])
        return {row["name"] for row in result["files"] if row["role"] == "subtitle"}

    def test_same_directory_batch_has_linear_traversal_and_matching_work(self):
        measurements = []
        for count in (80, 160):
            with (
                self.subTest(count=count),
                tempfile.TemporaryDirectory(dir=self.root) as raw,
            ):
                directory = Path(raw)
                for index in range(count):
                    (directory / f"Show.S01E{index + 1:03d}.mkv").write_bytes(b"media")
                    (directory / f"Show.S01E{index + 1:03d}.en.srt").write_bytes(
                        b"subtitle"
                    )
                # 使用正式发现入口，统计仅涵盖随后的逐候选检查。
                source = local_media_repo.get_local_media_source(self.source_id, owner="admin")
                candidates, error = discover_local_media_candidates(source)
                self.assertEqual(error, "")
                self.assertEqual(len(candidates), count)
                stats: dict[str, int | float] = {
                    "count": count,
                    "directory_walks": 0,
                    "video_rows": 0,
                    "subtitle_rows": 0,
                    "lstat": 0,
                }
                original_iterdir = Path.iterdir
                original_lstat = Path.lstat

                def iterdir(
                    path,
                    directory=directory,
                    stats=stats,
                    original_iterdir=original_iterdir,
                ):
                    if path == directory:
                        stats["directory_walks"] += 1
                    return original_iterdir(path)

                def lstat(
                    path,
                    *args,
                    directory=directory,
                    stats=stats,
                    original_lstat=original_lstat,
                    **kwargs,
                ):
                    if path.parent == directory:
                        stats["lstat"] += 1
                    return original_lstat(path, *args, **kwargs)

                def matching(videos, subtitles, stats=stats):
                    stats["video_rows"] += len(videos)
                    stats["subtitle_rows"] += len(subtitles)
                    return plan_subtitle_companions(videos, subtitles)

                start = time.monotonic()
                with (
                    patch.object(Path, "iterdir", iterdir),
                    patch.object(Path, "lstat", lstat),
                    patch.object(
                        local_storage,
                        "plan_subtitle_companions",
                        side_effect=matching,
                    ),
                ):
                    for candidate in candidates:
                        self.assertEqual(
                            self.subtitles(candidate), {candidate.stem + ".en.srt"}
                        )
                stats["seconds"] = round(time.monotonic() - start, 4)
                measurements.append(stats)
                print("BO1_PERFORMANCE", stats, flush=True)
                self.assertEqual(stats["directory_walks"], 1)
                self.assertEqual(stats["video_rows"], count)
                self.assertEqual(stats["subtitle_rows"], count)
                self.assertLessEqual(stats["lstat"], 30 * count)
        if len(measurements) == 2:
            self.assertEqual(
                measurements[1]["video_rows"], 2 * measurements[0]["video_rows"]
            )

    def test_matching_keeps_full_directory_algorithm_semantics(self):
        videos = [
            "Show.S01E01.mkv",
            "Show.S01E01.en.mkv",
            "Show.S01E02.mkv",
            "Show.S01E02.mp4",
            "Show.S01E03.mkv",
        ]
        subtitles = [
            "Show.S01E01.en.srt",
            "Show.S01E01.chs.srt",
            "Show.S01E01.zh-Hans.srt",
            "Show.S01E01.en.forced.ass",
            "Show.S01E02.en.srt",
            "Show.S01E03.cht.default.srt",
            "unmatched.srt",
        ]
        for name in videos + subtitles:
            self.write(name)

        def files(names):
            return [
                SimpleNamespace(name=name, file_id=str(self.root / name))
                for name in names
            ]

        expected = plan_subtitle_companions(files(videos), files(subtitles))
        for video in videos:
            with self.subTest(video=video):
                self.assertEqual(
                    self.subtitles(self.root / video),
                    {
                        item.file.name
                        for item in expected.plans
                        if item.video_file_id == str(self.root / video)
                    },
                )

    def test_directory_add_rename_delete_and_symlink_invalidate_names(self):
        one = self.write("Show.S01E01.mkv")
        two = self.write("Show.S01E02.mkv")
        three = self.write("Show.S01E03.mkv")
        four = self.write("Show.S01E04.mkv")
        self.assertEqual(self.subtitles(one), set())
        sub = self.write("Show.S01E02.en.srt", b"subtitle")
        self.assertEqual(self.subtitles(two), {sub.name})
        sub = sub.rename(self.root / "Show.S01E03.en.srt")
        self.assertEqual(self.subtitles(three), {sub.name})
        sub.unlink()
        (self.root / "Show.S01E04.en.srt").symlink_to(one)
        self.assertEqual(self.subtitles(four), set())

    def test_in_place_eligibility_changes_without_directory_mtime_change_are_fresh(
        self,
    ):
        one = self.write("Show.S01E01.mkv")
        two = self.write("Show.S01E02.mkv")
        three = self.write("Show.S01E03.mkv")
        four = self.write("Show.S01E04.mkv")
        duplicate = self.write("Show.S01E02.mp4", b"")
        self.write("Show.S01E02.en.srt", b"subtitle")
        sub_three = self.write("Show.S01E03.en.srt", b"")
        sub_four = self.write("Show.S01E04.en.srt", b"subtitle")
        self.subtitles(one)
        before = self.root.stat().st_mtime_ns
        duplicate.write_bytes(b"now ambiguous")
        sub_three.write_bytes(b"now valid")
        sub_four.write_bytes(b"")
        self.assertEqual(self.root.stat().st_mtime_ns, before)
        self.assertEqual(self.subtitles(two), set())
        self.assertEqual(self.subtitles(three), {sub_three.name})
        self.assertEqual(self.subtitles(four), set())

    def test_preview_keeps_final_identity_and_membership_recheck(self):
        for mutation in (
            "subtitle-content",
            "subtitle-replace",
            "video-content",
            "new-subtitle",
            "new-ambiguous-video",
        ):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory(dir=self.root) as raw,
            ):
                directory = Path(raw)
                video = directory / "Show.S01E01.mkv"
                video.write_bytes(b"media")
                sub = directory / "Show.S01E01.en.srt"
                sub.write_bytes(b"subtitle")
                inspection = self.inspect(video)
                if mutation == "subtitle-content":
                    sub.write_bytes(b"changed subtitle bytes")
                elif mutation == "subtitle-replace":
                    replacement = directory / "replacement"
                    replacement.write_bytes(b"subtitle")
                    replacement.replace(sub)
                elif mutation == "video-content":
                    video.write_bytes(b"changed video bytes")
                elif mutation == "new-subtitle":
                    (directory / "Show.S01E01.chs.srt").write_bytes(b"new subtitle")
                else:
                    (directory / "Show.S01E01.mp4").write_bytes(b"ambiguous video")
                with self.assertRaisesRegex(
                    LocalMediaServiceError, "源文件在检查后发生变化"
                ):
                    self.service.preview("admin", inspection["inspection_id"])

    def test_batch_repeat_expiry_and_close_discard_name_snapshot(self):
        one = self.write("Show.S01E01.mkv")
        two = self.write("Show.S01E02.mkv")
        original = Path.iterdir
        walks = []

        def iterdir(path):
            walks.append(path)
            return original(path)

        with patch.object(Path, "iterdir", iterdir):
            self.subtitles(one)
            self.subtitles(two)
            self.assertEqual(walks, [self.root])
            self.subtitles(one)  # 重复选择即新一批，不长期保留历史索引。
            self.assertEqual(len(walks), 2)
            with patch.object(
                local_storage.time, "monotonic", return_value=time.monotonic() + 31
            ):
                self.subtitles(two)
            self.assertEqual(len(walks), 3)
        self.assertTrue(self.service.close())
        self.assertIsNone(self.service._sibling_scan_batch._scope)
        self.assertFalse(self.service._sibling_scan_batch._videos)

    def test_unreadable_and_changing_directory_fail_closed(self):
        one = self.write("Show.S01E01.mkv")
        self.write("Show.S01E01.en.srt", b"subtitle")
        original = Path.iterdir

        def changing(path):
            entries = list(original(path))
            self.write("Show.S01E01.mp4", b"new ambiguity")
            return iter(entries)

        with (
            patch.object(Path, "iterdir", changing),
            self.assertRaisesRegex(
                local_storage.LocalContentChanged, "目录在扫描期间发生变化"
            ),
        ):
            self.inspect(one)
        with (
            patch.object(Path, "iterdir", side_effect=PermissionError("isolated test")),
            self.assertRaisesRegex(
                local_storage.LocalStorageError, "字幕目录暂时不可完整读取"
            ),
        ):
            self.inspect(one)

    def test_sibling_name_snapshot_enforces_item_limit(self):
        one = self.write("Show.S01E01.mkv")
        self.write("Show.S01E01.en.srt", b"subtitle")
        adapter = local_storage.LocalFilesystemAdapter(self.root, item_limit=1)
        with self.assertRaises(local_storage.LocalScanLimitExceeded):
            adapter.scan(one)

    def test_preview_and_planning_workers_share_only_the_name_batch(self):
        one = self.write("Show.S01E01.mkv")
        two = self.write("Show.S01E02.mkv")
        self.write("Show.S01E01.en.srt", b"subtitle")
        self.write("Show.S01E02.en.srt", b"subtitle")
        with patch.object(self.service, "parallel_planning_safe", return_value=True):
            workers = [self.service.create_planning_worker() for _ in range(2)]
        for worker in workers:
            self.addCleanup(worker.close)
            self.assertIsNot(worker.inspections, self.service.inspections)
        original = Path.iterdir
        walks = []

        def iterdir(path):
            walks.append(path)
            return original(path)

        with patch.object(Path, "iterdir", iterdir):
            for worker, video in zip(workers, (one, two), strict=True):
                inspection = worker.inspect_source("admin", self.source_id, video)
                # 无目标配置会在新鲜 scan/digest 复核之后停止，不调用元数据服务。
                with self.assertRaisesRegex(LocalMediaServiceError, "尚未配置归档目标"):
                    worker.preview("admin", inspection["inspection_id"])
        self.assertEqual(walks, [self.root])
