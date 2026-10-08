"""本地媒体识别、统一命名和预览测试。"""
from __future__ import annotations

import dataclasses
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.local_media_service import (
    LocalMediaService,
    LocalMediaServiceError,
    _Inspection,
)
from app.modules.organize import OrganizeRules
from app.modules.organize_scan import OrganizeScanResult, ScannedVideo
from app.modules.scraper import MatchResult, TMDBScraper
from tests.support import IsolatedDatabaseTestCase, isolated_test_database, release_parse_result


class FakeScraper:
    supports_parent_path = True

    def __init__(self, match: MatchResult):
        self.result = match
        self.parents: list[str] = []
        self.media_type_hints: list[str] = []

    def match(self, filename: str, parent_path: str = "", *, media_type_hint: str = ""):
        self.parents.append(parent_path)
        self.media_type_hints.append(media_type_hint)
        return self.result

    def match_from_tmdb(self, tmdb_id: str, media_type: str):
        return MatchResult(tmdb_id=str(tmdb_id), title=self.result.title, year=self.result.year,
                           media_type=media_type, confidence=1.0, status="matched")

    def parse_media(self, filename: str, parent_path: str = "", match=None):
        import re
        season = re.search(r"(?i)S(\d{1,2})", filename)
        episode = re.search(r"(?i)E(\d{1,3})", filename)
        return release_parse_result(
            {
                "season": int(season.group(1)) if season else None,
                "episode": int(episode.group(1)) if episode else None,
                "title": "", "year": "", "type": self.result.media_type,
            },
            filename=filename, parent_path=parent_path,
        )

    def get_detail(self, tmdb_id: str, media_type: str, *, force_refresh=False):
        genre = 16 if self.result.title == "攻壳机动队" else 28
        return {"genres": [{"id": genre}], "origin_country": ["JP"],
                "release_date": "2025-01-01", "first_air_date": "2026-01-01"}

    def search_candidates(self, query, year, media_type):
        return []


class SharedPositionFakeScraper(FakeScraper):
    """固定媒体身份，但复用正式共享季集解析器。"""

    def parse_media(self, filename: str, parent_path: str = "", match=None):
        from app.modules.scraper import extract_recognition_context

        context = extract_recognition_context(filename, parent_path)
        return release_parse_result(
            {
                "season": context.season,
                "episode": context.episode,
                "title": context.normalized_title,
                "year": "",
                "type": self.result.media_type,
            },
            filename=filename,
            parent_path=parent_path,
        )

    def get_detail(self, tmdb_id: str, media_type: str, *, force_refresh=False):
        return {
            "genres": [{"id": 16}],
            "origin_country": ["JP"],
            "first_air_date": "2025-01-01",
        }


class LocalMediaServiceTests(IsolatedDatabaseTestCase):
    def test_directory_inspection_reuses_release_source_position_once(self):
        from app.modules import scraper as scraper_module

        source_root = Path(tempfile.mkdtemp(prefix="mediaflux-recognition-source-"))
        self.addCleanup(lambda: source_root.rmdir())
        scraper = TMDBScraper("offline-fixture")
        self.addCleanup(scraper.close)
        service = LocalMediaService(scraper=scraper)
        self.addCleanup(service.close)
        video = GuangYaFile(
            "video-1", "Example.Show.S01E03.mkv", False,
            1024, "etag-1", str(source_root),
        )
        scan_result = OrganizeScanResult(
            scanned_videos=[ScannedVideo(
                file=video,
                relative_dir="",
                recognition_parent_path="",
            )],
            scanned_dirs=[],
            companion_files={},
            video_files_by_path={"": [video]},
            protected_sources=set(),
            source_root_name="Example Show",
        )
        inspection = _Inspection(
            owner="admin", source_id=1, root=source_root,
            selected_path=source_root, snapshots=[], digest="digest",
            created_at=time.time(), media_type="tv",
        )

        with patch.object(
            scraper_module,
            "_parse_release_core",
            wraps=scraper_module._parse_release_core,
        ) as parse_core:
            directory_inspection, source_positions = (
                service._build_local_directory_inspection(
                    inspection, scan_result, media_type="tv",
                )
            )

        self.assertEqual(parse_core.call_count, 1)
        self.assertEqual(source_positions["video-1"], (1, 3))
        self.assertEqual(directory_inspection.videos[0].season, 1)
        self.assertEqual(directory_inspection.videos[0].episode, 3)

    def test_directory_inspection_keeps_source_and_effective_positions_distinct(self):
        class SourceEffectiveScraper(FakeScraper):
            def parse_media(self, filename, parent_path="", match=None):
                del match
                return release_parse_result(
                    {
                        "season": 1,
                        "episode": 3,
                        "title": "Example Show",
                        "type": self.result.media_type,
                    },
                    filename=filename,
                    parent_path=parent_path,
                    source_season=2,
                    source_episode=7,
                )

        source_root = Path(tempfile.mkdtemp(prefix="mediaflux-recognition-source-effective-"))
        self.addCleanup(lambda: source_root.rmdir())
        service = LocalMediaService(
            scraper=SourceEffectiveScraper(MatchResult(media_type="tv")),
        )
        self.addCleanup(service.close)
        video = GuangYaFile(
            "video-source-effective", "Example.Show.mkv", False,
            1024, "etag-source-effective", str(source_root),
        )
        scan_result = OrganizeScanResult(
            scanned_videos=[ScannedVideo(
                file=video,
                relative_dir="",
                recognition_parent_path="",
            )],
            scanned_dirs=[],
            companion_files={},
            video_files_by_path={"": [video]},
            protected_sources=set(),
            source_root_name="Example Show",
        )
        inspection = _Inspection(
            owner="admin", source_id=1, root=source_root,
            selected_path=source_root, snapshots=[], digest="digest",
            created_at=time.time(), media_type="tv",
        )

        directory_inspection, source_positions = (
            service._build_local_directory_inspection(
                inspection, scan_result, media_type="tv",
            )
        )

        self.assertEqual(source_positions["video-source-effective"], (2, 7))
        self.assertEqual(
            (directory_inspection.videos[0].season, directory_inspection.videos[0].episode),
            (2, 7),
        )

    def test_planner_reuses_passed_source_position_result(self):
        from app.modules.organize import OrganizeRules, Organizer

        class CountingPositionScraper(FakeScraper):
            def __init__(self, match):
                super().__init__(match)
                self.source_position_calls = 0

            def parse_source_position(self, filename, parent_path=""):
                del filename, parent_path
                self.source_position_calls += 1
                raise AssertionError("trusted parsed result should avoid reparsing position")

        scraper = CountingPositionScraper(MatchResult(
            tmdb_id="1", title="Movie", year="2026", media_type="movie",
            confidence=1.0, status="matched",
        ))
        planner = Organizer(client=object(), scraper=scraper)
        self.addCleanup(planner.close)
        video = GuangYaFile("planner-video", "Movie.2026.mkv", False, 1024, "etag", "source")
        scan_result = OrganizeScanResult(
            scanned_videos=[ScannedVideo(
                file=video, relative_dir="", recognition_parent_path="",
            )],
            scanned_dirs=[], companion_files={}, video_files_by_path={"": [video]},
            protected_sources=set(), source_root_name="Source",
        )
        rules = OrganizeRules(
            target_dir_id="0", region_split=False, year_split=False,
            clean_empty=False, link_strm=False, notify_enabled=False,
            media_info_enabled=False, media_probe_enabled=False,
        )

        for partial_position in ((None, None), (2, None)):
            with self.subTest(partial_position=partial_position):
                planning_result, _ = planner.plan_scan_result(
                    scan_result,
                    rules,
                    source_positions_by_file_id={
                        video.file_id: partial_position,
                    },
                    target_inventory_loader=lambda _plan: (None, [], {}),
                )

                self.assertTrue(planning_result.plans)
                self.assertIsNone(planning_result.plans[0].source_season)
                self.assertIsNone(planning_result.plans[0].source_episode)

        self.assertEqual(scraper.source_position_calls, 0)

    def test_directory_inspection_keeps_source_position_when_parse_media_fails(self):
        class FallbackScraper(FakeScraper):
            def parse_media(self, filename, parent_path="", match=None):
                del filename, parent_path, match
                raise RuntimeError("parse_media fixture failure")

            def parse_source_position(self, filename, parent_path=""):
                del filename, parent_path
                return 2, 7

        source_root = Path(tempfile.mkdtemp(prefix="mediaflux-recognition-fallback-"))
        self.addCleanup(lambda: source_root.rmdir())
        service = LocalMediaService(scraper=FallbackScraper(MatchResult(media_type="tv")))
        self.addCleanup(service.close)
        video = GuangYaFile(
            "video-fallback", "Example.Show.mkv", False,
            1024, "etag-fallback", str(source_root),
        )
        scan_result = OrganizeScanResult(
            scanned_videos=[ScannedVideo(
                file=video,
                relative_dir="",
                recognition_parent_path="",
            )],
            scanned_dirs=[],
            companion_files={},
            video_files_by_path={"": [video]},
            protected_sources=set(),
            source_root_name="Example Show",
        )
        inspection = _Inspection(
            owner="admin", source_id=1, root=source_root,
            selected_path=source_root, snapshots=[], digest="digest",
            created_at=time.time(), media_type="tv",
        )

        directory_inspection, source_positions = (
            service._build_local_directory_inspection(
                inspection, scan_result, media_type="tv",
            )
        )

        self.assertEqual(source_positions, {"video-fallback": (2, 7)})
        self.assertEqual(directory_inspection.videos[0].season, 2)
        self.assertEqual(directory_inspection.videos[0].episode, 7)

    def test_directory_inspection_does_not_fabricate_position_after_both_parsers_fail(self):
        class NoPositionScraper(FakeScraper):
            def parse_media(self, filename, parent_path="", match=None):
                del filename, parent_path, match
                raise RuntimeError("parse_media fixture failure")

            def parse_source_position(self, filename, parent_path=""):
                del filename, parent_path
                raise RuntimeError("fallback fixture failure")

        source_root = Path(tempfile.mkdtemp(prefix="mediaflux-recognition-no-position-"))
        self.addCleanup(lambda: source_root.rmdir())
        service = LocalMediaService(
            scraper=NoPositionScraper(MatchResult(media_type="tv")),
        )
        self.addCleanup(service.close)
        video = GuangYaFile(
            "video-no-position", "Example.Show.mkv", False,
            1024, "etag-no-position", str(source_root),
        )
        scan_result = OrganizeScanResult(
            scanned_videos=[ScannedVideo(
                file=video,
                relative_dir="",
                recognition_parent_path="",
            )],
            scanned_dirs=[],
            companion_files={},
            video_files_by_path={"": [video]},
            protected_sources=set(),
            source_root_name="Example Show",
        )
        inspection = _Inspection(
            owner="admin", source_id=1, root=source_root,
            selected_path=source_root, snapshots=[], digest="digest",
            created_at=time.time(), media_type="tv",
        )

        directory_inspection, source_positions = (
            service._build_local_directory_inspection(
                inspection, scan_result, media_type="tv",
            )
        )

        self.assertEqual(source_positions, {})
        self.assertIsNone(directory_inspection.videos[0].season)
        self.assertIsNone(directory_inspection.videos[0].episode)

    def test_execution_uses_persisted_tasks_without_an_in_memory_write_entry(self):
        self.assertFalse(hasattr(LocalMediaService, "execute_preview"))
        self.assertFalse(hasattr(LocalMediaService, "_execute_preview_under_writer"))
        self.assertTrue(callable(LocalMediaService.create_manual_task))
        self.assertTrue(callable(LocalMediaService.execute_task))

    def setUp(self):
        super().setUp()
        probe = patch(
            "app.modules.local_media_service.probe_local_media_profile", return_value=None,
        )
        self.probe = probe.start()
        self.addCleanup(probe.stop)

    def _source(self, source_root: Path, target_root: Path, category: str) -> int:
        source_id = db.create_local_media_source(
            name=f"source-{source_root.name}-{category}", qb_profile="", qb_path_prefix="",
            local_root=str(source_root), owner="admin",
        )
        db.upsert_local_library_target(source_id, category, str(target_root), owner="admin")
        return source_id

    def test_execute_task_uses_one_cross_service_writer(self):
        state_lock = threading.Lock()
        first_entered = threading.Event()
        release = threading.Event()
        active = 0
        peak = 0
        entries = 0
        errors: list[BaseException] = []

        class WriterService(LocalMediaService):
            def _execute_task_under_writer(
                self, owner: str, task_id: int, *, qb_client=None,
            ):
                nonlocal active, peak, entries
                del self, owner, qb_client
                with state_lock:
                    active += 1
                    entries += 1
                    peak = max(peak, active)
                    first_entered.set()
                try:
                    release.wait(timeout=2)
                    return {"status": "completed", "task_id": task_id}
                finally:
                    with state_lock:
                        active -= 1

        match = MatchResult(
            tmdb_id="1", title="Writer", year="2026",
            media_type="movie", confidence=1.0,
        )
        services = [
            WriterService(scraper=FakeScraper(match)),
            WriterService(scraper=FakeScraper(match)),
        ]

        def execute(service: WriterService, task_id: int) -> None:
            try:
                service.execute_task("admin", task_id)
            except BaseException as exc:  # pragma: no cover - 主线程统一断言
                errors.append(exc)

        threads = [
            threading.Thread(target=execute, args=(service, index + 1))
            for index, service in enumerate(services)
        ]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(first_entered.wait(timeout=1))
            time.sleep(0.05)
            with state_lock:
                self.assertEqual(entries, 1)
                self.assertEqual(peak, 1)
            release.set()
            for thread in threads:
                thread.join(timeout=2)
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=2)
            for service in services:
                service.close()

        self.assertEqual(errors, [])
        self.assertEqual(entries, 2)
        self.assertEqual(peak, 1)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_qb_state_transitions_use_shared_qb_writer_lease(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "qb-downloads"
            target_root = root / "movies"
            source_root.mkdir()
            target_root.mkdir()
            movie = source_root / "Movie.2026.mkv"
            movie.write_bytes(b"video")
            source_id = self._source(source_root, target_root, "movie")
            task_id = db.create_local_media_task(
                source_id, "f" * 40, str(movie), owner="admin", trigger="qb_completed",
            )
            self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026",
                media_type="movie", confidence=1.0,
            )))
            state = {"leased": False, "entries": 0}

            @contextmanager
            def lease():
                self.assertFalse(state["leased"])
                state["leased"] = True
                state["entries"] += 1
                try:
                    yield
                finally:
                    state["leased"] = False

            class QBClient:
                def pause_torrents(inner_self, torrent_hash):
                    del inner_self
                    self.assertTrue(state["leased"])
                    self.assertEqual(torrent_hash, "f" * 40)
                    self.assertEqual(
                        db.get_local_media_task(task_id, owner="admin").status,
                        "moving",
                    )

                def delete_torrents(inner_self, torrent_hash, *, delete_files):
                    del inner_self
                    self.assertTrue(state["leased"])
                    self.assertEqual((torrent_hash, delete_files), ("f" * 40, False))
                    self.assertEqual(
                        db.get_local_media_task(task_id, owner="admin").status,
                        "verifying",
                    )

            rules = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both",
                clean_empty=False, conflict_strategy=1, emby_refresh=False,
            )
            try:
                with patch(
                    "app.modules.local_media_service.OrganizeRules.from_config",
                    return_value=rules,
                ), patch(
                    "app.modules.local_media_service.qb_control_write_lease",
                    side_effect=lease,
                ):
                    result = service.execute_task(
                        "admin", task_id, qb_client=QBClient(),
                    )
            finally:
                service.close()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(state["entries"], 2)
        self.assertFalse(state["leased"])

    def test_qb_resume_after_precommit_failure_uses_shared_writer_lease(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "qb-failed-downloads"
            target_root = root / "movies"
            source_root.mkdir()
            target_root.mkdir()
            movie = source_root / "Movie.2026.mkv"
            movie.write_bytes(b"video")
            source_id = self._source(source_root, target_root, "movie")
            task_id = db.create_local_media_task(
                source_id, "9" * 40, str(movie), owner="admin", trigger="qb_completed",
            )
            self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026",
                media_type="movie", confidence=1.0,
            )))
            state = {"leased": False, "calls": []}

            @contextmanager
            def lease():
                self.assertFalse(state["leased"])
                state["leased"] = True
                try:
                    yield
                finally:
                    state["leased"] = False

            class QBClient:
                def pause_torrents(inner_self, torrent_hash):
                    del inner_self
                    self.assertTrue(state["leased"])
                    state["calls"].append(("pause", torrent_hash))

                def resume_torrents(inner_self, torrent_hash):
                    del inner_self
                    self.assertTrue(state["leased"])
                    state["calls"].append(("resume", torrent_hash))

            rules = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both",
                clean_empty=False, conflict_strategy=1, emby_refresh=False,
            )
            try:
                with patch(
                    "app.modules.local_media_service.OrganizeRules.from_config",
                    return_value=rules,
                ), patch(
                    "app.modules.local_media_service.qb_control_write_lease",
                    side_effect=lease,
                ), patch(
                    "app.modules.local_media_service.LocalMoveTransaction.execute",
                    side_effect=RuntimeError("move failed"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "move failed"):
                        service.execute_task("admin", task_id, qb_client=QBClient())
            finally:
                service.close()

        self.assertEqual(state["calls"], [
            ("pause", "9" * 40),
            ("resume", "9" * 40),
        ])
        self.assertFalse(state["leased"])
        self.assertEqual(
            db.get_local_media_task(task_id, owner="admin").status,
            "failed",
        )

    def test_move_media_item_to_trash_waits_for_pipeline_writer(self):
        state_lock = threading.Lock()
        writer_entered = threading.Event()
        release_writer = threading.Event()
        trash_finished = threading.Event()
        errors: list[BaseException] = []

        class HoldingWriterService(LocalMediaService):
            def _execute_task_under_writer(
                self, owner: str, task_id: int, *, qb_client=None,
            ):
                del self, owner, qb_client
                writer_entered.set()
                release_writer.wait(timeout=2)
                return {"status": "completed", "task_id": task_id}

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "downloads"
            source_root.mkdir()
            media_file = source_root / "Movie.2026.mkv"
            media_file.write_bytes(b"movie")
            info = media_file.lstat()
            identity = {
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "device": info.st_dev,
                "inode": info.st_ino,
            }
            source_id = db.create_local_media_source(
                name="trash-writer-source", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), owner="admin",
            )
            holder = HoldingWriterService(
                scraper=FakeScraper(MatchResult(
                    tmdb_id="1", title="Movie", year="2026",
                    media_type="movie", confidence=1.0,
                ))
            )
            mover = LocalMediaService(
                scraper=FakeScraper(MatchResult(
                    tmdb_id="1", title="Movie", year="2026",
                    media_type="movie", confidence=1.0,
                ))
            )

            def hold_writer() -> None:
                try:
                    holder.execute_task("admin", 1)
                except BaseException as exc:  # pragma: no cover - asserted below
                    with state_lock:
                        errors.append(exc)

            def move_to_trash() -> None:
                try:
                    mover.move_media_item_to_trash(
                        "admin", source_id, media_file, identity,
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    with state_lock:
                        errors.append(exc)
                finally:
                    trash_finished.set()

            holder_thread = threading.Thread(target=hold_writer)
            trash_thread = threading.Thread(target=move_to_trash)
            try:
                holder_thread.start()
                self.assertTrue(writer_entered.wait(timeout=1))
                trash_thread.start()
                self.assertFalse(trash_finished.wait(timeout=0.05))
                self.assertTrue(media_file.exists())
                release_writer.set()
                holder_thread.join(timeout=2)
                trash_thread.join(timeout=2)
            finally:
                release_writer.set()
                holder_thread.join(timeout=2)
                trash_thread.join(timeout=2)
                holder.close()
                mover.close()

            self.assertEqual(errors, [])
            self.assertFalse(media_file.exists())
            self.assertEqual(len(list((source_root / ".mediaflux-trash").iterdir())), 1)
            self.assertTrue(all(not thread.is_alive() for thread in (holder_thread, trash_thread)))

    def test_move_media_item_to_trash_rejects_all_active_path_overlaps(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "downloads"
            source_root.mkdir()
            source_id = db.create_local_media_source(
                name="trash-overlap-source", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), owner="admin",
            )
            service = LocalMediaService(
                scraper=FakeScraper(MatchResult(
                    tmdb_id="1", title="Movie", year="2026",
                    media_type="movie", confidence=1.0,
                ))
            )
            try:
                for relation in ("equal", "ancestor", "descendant"):
                    with self.subTest(relation=relation):
                        selected = source_root / f"Show-{relation}"
                        selected.mkdir()
                        episode = selected / "S01E01.mkv"
                        episode.write_bytes(b"episode")
                        info = selected.lstat()
                        identity = {
                            "size": info.st_size,
                            "mtime_ns": info.st_mtime_ns,
                            "device": info.st_dev,
                            "inode": info.st_ino,
                        }
                        task_path = {
                            "equal": selected,
                            "ancestor": source_root,
                            "descendant": episode,
                        }[relation]
                        task_id = db.create_local_media_task(
                            source_id, "", str(task_path), owner="admin", trigger="manual",
                        )
                        db.update_local_media_task(
                            task_id, owner="admin", status="requires_manual",
                        )
                        with self.assertRaisesRegex(
                            LocalMediaServiceError, "未完成的本地媒体任务路径重叠",
                        ):
                            service.move_media_item_to_trash(
                                "admin", source_id, selected, identity,
                            )
                        self.assertTrue(selected.exists())
                        db.update_local_media_task(
                            task_id, owner="admin", status="failed",
                        )

                selected = source_root / "Independent"
                selected.mkdir()
                (selected / "Movie.mkv").write_bytes(b"movie")
                sibling = source_root / "Other.mkv"
                sibling.write_bytes(b"other")
                sibling_task = db.create_local_media_task(
                    source_id, "", str(sibling), owner="admin", trigger="manual",
                )
                info = selected.lstat()
                destination = service.move_media_item_to_trash(
                    "admin",
                    source_id,
                    selected,
                    {
                        "size": info.st_size,
                        "mtime_ns": info.st_mtime_ns,
                        "device": info.st_dev,
                        "inode": info.st_ino,
                    },
                )
                self.assertFalse(selected.exists())
                self.assertTrue(destination.exists())
                self.assertEqual(
                    db.get_local_media_task(sibling_task, owner="admin").status,
                    "waiting_stable",
                )
            finally:
                service.close()

    def test_warmed_tasks_recheck_latest_target_inventory_before_each_commit(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "parallel-writer-downloads"
            target_root = root / "parallel-writer-movies"
            first_dir = source_root / "first"
            second_dir = source_root / "second"
            first_dir.mkdir(parents=True)
            second_dir.mkdir(parents=True)
            target_root.mkdir()
            first_file = first_dir / "Movie.2026.mkv"
            second_file = second_dir / "Movie.2026.mkv"
            first_file.write_bytes(b"first-version")
            second_file.write_bytes(b"second-version")
            source_id = self._source(source_root, target_root, "movie")
            task_ids = [
                db.create_local_media_task(
                    source_id, "", str(path), owner="admin", trigger="scan",
                )
                for path in (first_file, second_file)
            ]
            self.assertTrue(all(
                db.claim_local_media_task(task_id, owner="admin")
                for task_id in task_ids
            ))
            match = MatchResult(
                tmdb_id="1", title="Movie", year="2026",
                media_type="movie", confidence=1.0,
            )
            services = [
                LocalMediaService(scraper=FakeScraper(match)),
                LocalMediaService(scraper=FakeScraper(match)),
            ]
            rules = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both",
                conflict_strategy=1, emby_refresh=False,
            )
            try:
                with patch(
                    "app.modules.local_media_service.OrganizeRules.from_config",
                    return_value=rules,
                ):
                    warmed = [
                        service.prepare_task("admin", task_id)
                        for service, task_id in zip(services, task_ids, strict=True)
                    ]
                    results = [
                        service.execute_task("admin", task_id)
                        for service, task_id in zip(services, task_ids, strict=True)
                    ]
            finally:
                for service in services:
                    service.close()

            targets = list(target_root.rglob("*.mkv"))
            second_retained = second_file.exists()

        self.assertEqual(
            [item["status"] for item in warmed], ["planned", "planned"],
        )
        self.assertEqual(len(results[0]["moved"]), 1)
        self.assertEqual(results[1]["moved"], [])
        self.assertTrue(any("跳过 1 项" in item for item in results[1]["warnings"]))
        self.assertEqual(len(targets), 1)
        self.assertTrue(second_retained)
        self.assertEqual(
            [db.get_local_media_task(task_id, owner="admin").status for task_id in task_ids],
            ["completed", "completed"],
        )

    def test_dynamis_b_global_filename_uses_shared_clean_title_in_local_inspection(self):
        from app.modules.scraper import TMDBScraper

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "dynamis-downloads"
            target_root = root / "dynamis-anime"
            source_root.mkdir()
            target_root.mkdir()
            filename = (
                "[Dynamis One] Ever Night - 06 "
                "(B-Global Donghua 1920x832 HEVC AAC MKV) [B2088D0F].mkv"
            )
            (source_root / filename).write_bytes(b"video")
            source_id = self._source(source_root, target_root, "anime")

            inspection = LocalMediaService(scraper=TMDBScraper()).inspect_source(
                "admin", source_id, source_root,
            )

        self.assertEqual(inspection["suggested_query"], "Ever Night")
        self.assertEqual(inspection["media_type"], "tv")
        self.assertEqual(inspection["parsed_episode"], 6)

    def test_movie_preview_uses_independent_media_directory_and_organizer_naming(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            movie = source_root / "Creation.of.the.Gods.2.2025.1080p.H265.mkv"
            movie.write_bytes(b"movie")
            source_id = self._source(source_root, target_root, "movie")
            scraper = FakeScraper(MatchResult(tmdb_id="1155281", title="封神第二部：战火西岐",
                                              year="2025", media_type="movie", confidence=1.0))
            service = LocalMediaService(scraper=scraper)
            inspection = service.inspect_source("admin", source_id, source_root)
            with patch("app.modules.local_media_service.OrganizeRules.from_config") as rules_factory:
                from app.modules.organize import OrganizeRules
                rules_factory.return_value = OrganizeRules(region_split=False, year_split=False, naming_scope="both")
                preview = service.preview("admin", inspection["inspection_id"])
            self.assertEqual(preview["status"], "planned")
            target = Path(preview["plans"][0]["target_path"])
            self.assertIn("封神第二部：战火西岐 (2025) {tmdb-1155281}", target.parent.name)
            self.assertIn("封神第二部：战火西岐.2025", target.name)

    def test_auto_multi_video_directory_prefers_episode_evidence_over_generic_folder_name(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "auto-detect-downloads"
            target_root = root / "auto-detect-library"
            source_root.mkdir()
            target_root.mkdir()
            (source_root / "Example.Show.S01E01.mkv").write_bytes(b"episode-1")
            (source_root / "Example.Show.S01E02.mkv").write_bytes(b"episode-2")
            source_id = self._source(source_root, target_root, "tv")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Example Show", year="2026",
                media_type="movie", confidence=1.0,
            )))

            inspection = service.inspect_source("admin", source_id, source_root)

            self.assertEqual(inspection["media_type"], "tv")

    def test_manual_directory_rejects_two_distinct_media_titles(self):
        class MixedTitleScraper(FakeScraper):
            def parse_media(self, filename: str, parent_path: str = "", match=None):
                title = "Alpha Show" if "Alpha" in filename else "Beta Show"
                return release_parse_result(
                    {
                        "season": 1,
                        "episode": 1 if "Alpha" in filename else 2,
                        "title": title,
                        "year": "2026",
                        "type": "tv",
                    },
                    filename=filename, parent_path=parent_path,
                )

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "mixed-downloads"
            target_root = root / "tv"
            source_root.mkdir()
            target_root.mkdir()
            (source_root / "Alpha.Show.S01E01.mkv").write_bytes(b"alpha")
            (source_root / "Beta.Show.S01E02.mkv").write_bytes(b"beta")
            source_id = self._source(source_root, target_root, "tv")
            service = LocalMediaService(scraper=MixedTitleScraper(MatchResult(
                tmdb_id="1", title="Alpha Show", year="2026",
                media_type="tv", confidence=1.0,
            )))
            inspection = service.inspect_source("admin", source_id, source_root)

            with self.assertRaisesRegex(
                LocalMediaServiceError, "目录包含多个不同媒体",
            ):
                service.preview(
                    "admin", inspection["inspection_id"],
                    tmdb_id="1", media_type="tv",
                )

    def test_batch_version_winner_never_retires_another_source_file(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "batch-downloads"
            target_root = root / "movies"
            source_root.mkdir()
            target_root.mkdir()
            (source_root / "Movie.2026.1080p-SMALL.mkv").write_bytes(b"small")
            (source_root / "Movie.2026.1080p-LARGE.mkv").write_bytes(b"larger-version")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026",
                media_type="movie", confidence=1.0,
            )))
            inspection = service.inspect_source("admin", source_id, source_root)
            rules = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both",
                conflict_strategy=2,
            )
            with patch(
                "app.modules.local_media_service.OrganizeRules.from_config",
                return_value=rules,
            ):
                preview = service.preview("admin", inspection["inspection_id"])

            actions = [item.action for item in preview["_move_plans"] if item.role == "video"]
            self.assertEqual(sorted(actions), ["move", "skip"])
            winner = next(
                item for item in preview["_move_plans"]
                if item.role == "video" and item.action == "move"
            )
            self.assertIsNone(winner.retire_target)
            self.assertIsNone(winner.expected_retire_identity)

    def test_local_preview_reuses_one_bounded_probe_budget_for_all_videos(self):
        from app.modules.media_probe import ProbeBudget
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "anime-downloads"
            target_root = root / "anime-library"
            show = source_root / "The Ghost in the Shell"
            show.mkdir(parents=True)
            target_root.mkdir()
            (show / "The Ghost in the Shell.S01E01.mkv").write_bytes(b"episode-1")
            (show / "The Ghost in the Shell.S01E02.mkv").write_bytes(b"episode-2")
            source_id = self._source(source_root, target_root, "anime")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="255358", title="攻壳机动队", year="2026",
                media_type="tv", confidence=1.0,
            )))
            inspection = service.inspect_source("admin", source_id, show)
            with patch(
                "app.modules.local_media_service.OrganizeRules.from_config",
                return_value=OrganizeRules(region_split=False, year_split=False),
            ):
                preview = service.preview("admin", inspection["inspection_id"])

        self.assertEqual(preview["status"], "planned")
        self.assertEqual(len(preview["plans"]), 2)
        budgets = [call.kwargs.get("budget") for call in self.probe.call_args_list]
        self.assertEqual(len(budgets), 2)
        self.assertIsInstance(budgets[0], ProbeBudget)
        self.assertIs(budgets[0], budgets[1])
        self.assertIsNotNone(budgets[0].remaining_seconds())

    def test_sample_video_is_retained_without_being_deleted_or_archived(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "sample-downloads"
            target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            (source_root / "Movie.2026.mkv").write_bytes(b"main-video")
            (source_root / "Movie.2026.sample.mkv").write_bytes(b"sample-video")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026",
                media_type="movie", confidence=1.0,
            )))
            inspection = service.inspect_source("admin", source_id, source_root)
            with patch(
                "app.modules.local_media_service.OrganizeRules.from_config",
                return_value=OrganizeRules(region_split=False, year_split=False),
            ):
                preview = service.preview("admin", inspection["inspection_id"])

            self.assertEqual(preview["status"], "planned")
            self.assertEqual(len(preview["plans"]), 1)
            self.assertEqual(preview["plans"][0]["source_path"], "Movie.2026.mkv")
            self.assertEqual(preview["cleanup"], [])
            self.assertEqual(
                preview["retained"],
                [{
                    "name": "Movie.2026.sample.mkv",
                    "reason": "疑似 sample/proof 视频，已保留且不自动归档",
                    "reason_code": "sample-review",
                }],
            )

    def test_rules_snapshot_keeps_manual_execution_consistent_with_preview(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "snapshot-downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            (source_root / "Movie.2026.mkv").write_bytes(b"movie")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, source_root)
            original = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both",
                movie_dir_template="ORIGINAL-${showTitle}",
                movie_template="ORIGINAL-${showTitle}.${ext}",
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=original):
                preview = service.preview("admin", inspection["inspection_id"])
            changed = OrganizeRules(
                region_split=True, year_split=True, naming_scope="both",
                movie_dir_template="CHANGED-${showTitle}",
                movie_template="CHANGED-${showTitle}.${ext}",
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=changed):
                replay = service.preview(
                    "admin", inspection["inspection_id"],
                    rules_snapshot=preview["rules_snapshot"],
                )
            self.assertEqual(replay["plans"][0]["target_path"], preview["plans"][0]["target_path"])
            self.assertIn("Movie (2026) {tmdb-1}", replay["plans"][0]["target_path"])
            self.assertNotIn("ORIGINAL-", replay["plans"][0]["target_path"])
            self.assertNotIn("CHANGED-", replay["plans"][0]["target_path"])

    def test_rules_snapshot_excludes_token_and_ignores_client_runtime_endpoint(self):
        from app.modules.organize import OrganizeRules

        configured = OrganizeRules(
            nsfw_enabled=True,
            nsfw_metatube_endpoint="http://127.0.0.1:8080",
            nsfw_metatube_token="server-secret",
            nsfw_timeout_seconds=8,
        )
        snapshot = LocalMediaService._serialize_rules_snapshot(configured)
        self.assertNotIn("server-secret", snapshot)
        self.assertNotIn("nsfw_metatube_token", snapshot)
        self.assertNotIn("nsfw_metatube_endpoint", snapshot)

        tampered = json.loads(snapshot)
        tampered.update({
            "nsfw_enabled": True,
            "nsfw_metatube_endpoint": "http://169.254.169.254",
            "nsfw_metatube_token": "attacker",
            "nsfw_timeout_seconds": 30,
        })
        with patch(
            "app.modules.local_media_service.OrganizeRules.from_config",
            return_value=configured,
        ):
            restored = LocalMediaService._restore_rules_snapshot(json.dumps(tampered))
        self.assertEqual(restored.nsfw_metatube_endpoint, "http://127.0.0.1:8080")
        self.assertEqual(restored.nsfw_metatube_token, "server-secret")
        self.assertEqual(restored.nsfw_timeout_seconds, 8)

    def test_local_preview_applies_shared_large_file_conflict_strategy(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "conflict-downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            incoming = source_root / "Movie.2026.mkv"
            incoming.write_bytes(b"new-version-is-larger")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, source_root)
            rules = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both", conflict_strategy=2
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=rules):
                first = service.preview("admin", inspection["inspection_id"])
                target = Path(first["plans"][0]["target_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"old")
                second = service.preview("admin", inspection["inspection_id"])
            self.assertEqual(second["plans"][0]["action"], "replace")
            self.assertIn("替换", second["plans"][0]["note"])

    def test_manual_and_automatic_conflicts_share_organizer_strategy(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "manual-conflict-downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            incoming = source_root / "Movie.2026.mkv"
            incoming.write_bytes(b"incoming-version")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, incoming)
            rules = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both", conflict_strategy=1
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=rules):
                initial = service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie"
                )
                target = Path(initial["plans"][0]["target_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"existing-library-version")
                manual = service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie"
                )
                automatic = service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie",
                    automatic=True,
                )
            self.assertEqual(manual["plans"][0]["action"], "skip")
            self.assertIn("保留现有版本", manual["plans"][0]["note"])
            self.assertEqual(Path(manual["plans"][0]["target_path"]), target)
            self.assertEqual(automatic["plans"][0]["action"], "skip")

    def test_manual_execution_replaces_existing_target_without_numbered_duplicate(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "manual-execute-downloads"
            target_root = root / "manual-execute-library"
            source_root.mkdir(); target_root.mkdir()
            incoming = source_root / "Movie.2026.mkv"
            incoming.write_bytes(b"incoming-version")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, incoming)
            rules = OrganizeRules(
                region_split=False, year_split=False, naming_scope="both",
                conflict_strategy=3, emby_refresh=False,
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=rules):
                preview = service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie"
                )
                target = Path(preview["plans"][0]["target_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"existing-library-version")
                preview = service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="1", media_type="movie"
                )
                task_id = service.create_manual_task(
                    "admin", inspection["inspection_id"],
                    preview_digest=preview["preview_digest"],
                )
                with self.assertRaisesRegex(
                    LocalMediaServiceError, "检查记录不存在或已过期"
                ):
                    service.preview("admin", inspection["inspection_id"])
                self.assertTrue(db.claim_local_media_task(
                    task_id, expected="waiting_stable", owner="admin"
                ))
                result = service.execute_task("admin", task_id)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(target.read_bytes(), b"incoming-version")
            self.assertFalse(incoming.exists())
            self.assertEqual(list(target_root.rglob("*.mkv")), [target])
            self.assertEqual(list(target.parent.glob(".*.mediaflux-replaced-*")), [])

    def test_tv_episode_and_language_subtitle_are_planned_together(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "anime"
            show = source_root / "The Ghost in the Shell"
            show.mkdir(parents=True); target_root.mkdir()
            video = show / "[LoliHouse] The Ghost in the Shell - S01E03.mkv"
            subtitle = show / "[LoliHouse] The Ghost in the Shell - S01E03.zh.ass"
            video.write_bytes(b"video"); subtitle.write_text("subtitle")
            source_id = self._source(source_root, target_root, "anime")
            scraper = FakeScraper(MatchResult(tmdb_id="255358", title="攻壳机动队", year="2026",
                                              media_type="tv", confidence=1.0))
            service = LocalMediaService(scraper=scraper)
            inspection = service.inspect_source("admin", source_id, show)
            with patch("app.modules.local_media_service.OrganizeRules.from_config") as rules_factory:
                from app.modules.organize import OrganizeRules
                rules_factory.return_value = OrganizeRules(region_split=False, year_split=False, naming_scope="both")
                preview = service.preview("admin", inspection["inspection_id"])
            self.assertEqual(preview["status"], "planned")
            self.assertEqual([item["role"] for item in preview["plans"]], ["video", "subtitle"])
            video_target = Path(preview["plans"][0]["target_path"])
            subtitle_target = Path(preview["plans"][1]["target_path"])
            self.assertIn("S01E03", preview["plans"][0]["target_name"])
            self.assertTrue(preview["plans"][1]["target_name"].endswith(".zh.ass"))
            self.assertEqual(video_target.parent.name, "Season 1")
            self.assertEqual(video_target.parent.parent.name, "攻壳机动队 (2026) {tmdb-255358}")
            self.assertEqual(subtitle_target.parent, video_target.parent)
            self.assertEqual(scraper.parents, ["The Ghost in the Shell"])


    def test_inspection_filters_non_media_files_from_snapshot(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "downloads-filter"
            target_root = root / "library"
            source_root.mkdir()
            target_root.mkdir()
            (source_root / "Movie.2026.mkv").write_bytes(b"movie")
            (source_root / "Movie.2026.zh.ass").write_text("subtitle")
            (source_root / "readme.txt").write_text("ignored")
            (source_root / "archive.zip").write_bytes(b"ignored")
            source_id = self._source(source_root, target_root, "movie")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Movie", year="2026", media_type="movie", confidence=1.0,
            )))

            inspection = service.inspect_source("admin", source_id, source_root)

            self.assertEqual(inspection["file_count"], 2)
            self.assertEqual(inspection["video_count"], 1)
            self.assertEqual(
                {item["name"] for item in inspection["files"]},
                {"Movie.2026.mkv", "Movie.2026.zh.ass"},
            )

    def test_single_tv_file_can_override_season_and_keep_parsed_episode(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads-season-override"; target_root = root / "tv"
            source_root.mkdir(); target_root.mkdir()
            episode = source_root / "Show.S01E07.mkv"
            episode.write_bytes(b"video")
            source_id = self._source(source_root, target_root, "tv")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Show", year="2026", media_type="tv", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, episode)
            self.assertEqual(inspection["selected_kind"], "file")
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv",
                season_override=2,
            )
            self.assertEqual(preview["status"], "planned")
            self.assertEqual(preview["position_overrides"], {"season": 2, "episode": None})
            self.assertEqual(preview["matches"][0]["season"], 2)
            self.assertEqual(preview["matches"][0]["episode"], 7)
            self.assertIn("S02E07", preview["plans"][0]["target_name"])
            self.assertEqual(Path(preview["plans"][0]["target_path"]).parent.name, "Season 2")

    def test_local_preview_uses_shared_unicode_roman_season_parser(self):
        from app.modules.organize import OrganizeRules

        filename = (
            "[ANi] Clevatess Ⅱ－魔獸之王與虛假的勇者傳承－ - 08 "
            "[1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        )
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "unicode-roman-downloads"
            target_root = root / "unicode-roman-anime"
            source_root.mkdir()
            target_root.mkdir()
            episode = source_root / filename
            episode.write_bytes(b"video")
            source_id = self._source(source_root, target_root, "anime")
            service = LocalMediaService(scraper=SharedPositionFakeScraper(MatchResult(
                tmdb_id="258348",
                title="克雷瓦提斯-魔兽之王与婴儿与尸之勇者-",
                year="2025",
                media_type="tv",
                confidence=1.0,
                status="matched",
            )))

            inspection = service.inspect_source("admin", source_id, episode)
            rules = OrganizeRules(
                region_split=False,
                year_split=False,
                naming_scope="both",
            )
            with patch(
                "app.modules.local_media_service.OrganizeRules.from_config",
                return_value=rules,
            ):
                preview = service.preview("admin", inspection["inspection_id"])

        self.assertEqual(
            (inspection["parsed_season"], inspection["parsed_episode"]),
            (2, 8),
        )
        self.assertEqual(preview["status"], "planned")
        target = Path(preview["plans"][0]["target_path"])
        self.assertEqual(target.parent.name, "Season 2")
        self.assertIn("S02E08", target.name)

    def test_single_tv_file_episode_override_defaults_to_season_one(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads-episode-override"; target_root = root / "tv"
            source_root.mkdir(); target_root.mkdir()
            episode = source_root / "Show.mkv"
            episode.write_bytes(b"video")
            source_id = self._source(source_root, target_root, "tv")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Show", year="2026", media_type="tv", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, episode)
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv",
                episode_override=9,
            )
            self.assertEqual(preview["status"], "planned")
            self.assertEqual(preview["position_overrides"], {"season": 1, "episode": 9})
            self.assertIn("S01E09", preview["plans"][0]["target_name"])

    def test_manual_task_execution_reuses_persisted_position_override(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads-persisted-position"; target_root = root / "tv"
            source_root.mkdir(); target_root.mkdir()
            episode = source_root / "Show.S01E07.mkv"
            episode.write_bytes(b"video")
            source_id = db.create_local_media_source(
                name="persisted-position-source", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), media_type="tv", mode="preview_only",
                stable_seconds=0, owner="admin",
            )
            db.upsert_local_library_target(source_id, "tv", str(target_root), owner="admin")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Show", year="2026", media_type="tv", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, episode)
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv",
                season_override=2,
            )
            task_id = service.create_manual_task(
                "admin", inspection["inspection_id"],
                preview_digest=preview["preview_digest"],
            )
            self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
            result = service.execute_task("admin", task_id)
            self.assertEqual(result["status"], "completed")
            self.assertIn("S02E07", result["preview"]["plans"][0]["target_name"])
            stored = db.get_local_media_task(task_id, owner="admin")
            summary = json.loads(stored.recognition_summary)
            self.assertEqual(summary["media"][0]["seasons"], [{
                "episodes": [7], "file_count": 1, "season": 2,
            }])
            self.assertEqual((stored.season_override, stored.episode_override), (2, None))
            self.assertTrue(episode.exists())

    def test_manual_task_execution_remaps_absolute_release_to_explicit_season_episode(self):
        filename = (
            "[ANi] 地獄模式 ～喜歡挑戰特殊成就的玩家在廢設定的異世界成為無雙～ "
            "2nd Season - 24 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        )
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "downloads-explicit-remap"
            target_root = root / "tv"
            source_root.mkdir()
            target_root.mkdir()
            episode = source_root / filename
            episode.write_bytes(b"video")
            source_id = db.create_local_media_source(
                name="explicit-remap-source", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), media_type="tv", mode="move",
                stable_seconds=0, owner="admin",
            )
            db.upsert_local_library_target(
                source_id, "tv", str(target_root), owner="admin"
            )
            service = LocalMediaService(scraper=SharedPositionFakeScraper(MatchResult(
                tmdb_id="261403",
                title="地狱模式 ～喜欢速通游戏的玩家在废设定异世界无双～",
                year="2025",
                media_type="tv",
                confidence=1.0,
            )))
            inspection = service.inspect_source("admin", source_id, episode)
            self.assertEqual(
                (inspection["parsed_season"], inspection["parsed_episode"]), (2, 24)
            )
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="261403",
                media_type="tv", season_override=2, episode_override=12,
            )
            self.assertEqual(preview["status"], "planned")
            self.assertIn("S02E12", preview["plans"][0]["target_name"])
            self.assertNotIn("S02E24", preview["plans"][0]["target_name"])
            task_id = service.create_manual_task(
                "admin", inspection["inspection_id"],
                preview_digest=preview["preview_digest"],
            )
            self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
            expected_target = Path(preview["plans"][0]["target_path"])
            result = service.execute_task("admin", task_id)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["moved"], [str(expected_target)])
            target = Path(result["moved"][0])
            self.assertIn("S02E12", target.name)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"video")
            self.assertEqual(target.parent.name, "Season 2")
            self.assertFalse(episode.exists())
            stored = db.get_local_media_task(task_id, owner="admin")
            self.assertEqual((stored.season_override, stored.episode_override), (2, 12))

    def test_single_video_directory_rejects_episode_override_like_guangya(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads-directory-override"; target_root = root / "tv"
            show = source_root / "Show"
            show.mkdir(parents=True); target_root.mkdir()
            (show / "Show.S01E01.mkv").write_bytes(b"video")
            source_id = self._source(source_root, target_root, "tv")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Show", year="2026", media_type="tv", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, show)
            self.assertEqual(inspection["selected_kind"], "directory")
            self.assertTrue(inspection["single_video"])
            self.assertEqual(inspection["primary_video_name"], "Show.S01E01.mkv")
            self.assertEqual(inspection["parsed_season"], 1)
            self.assertEqual(inspection["parsed_episode"], 1)
            with self.assertRaisesRegex(Exception, "目录刮削只能统一指定归档季"):
                service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv",
                    episode_override=2,
                )

    def test_multi_video_directory_rejects_one_episode_override(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads-multi-override"; target_root = root / "tv"
            show = source_root / "Show"
            show.mkdir(parents=True); target_root.mkdir()
            (show / "Show.S01E01.mkv").write_bytes(b"video-one")
            (show / "Show.S01E02.mkv").write_bytes(b"video-two")
            source_id = self._source(source_root, target_root, "tv")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="42", title="Show", year="2026", media_type="tv", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, show)
            self.assertFalse(inspection["single_video"])
            with self.assertRaisesRegex(Exception, "目录刮削只能统一指定归档季"):
                service.preview(
                    "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv",
                    episode_override=2,
                )

    def test_manual_tmdb_selection_does_not_apply_preprocess_position_offsets(self):
        class PositionAwareScraper(FakeScraper):
            def parse_media(self, filename, parent_path="", match=None):
                parsed = super().parse_media(filename, parent_path, match)
                return dataclasses.replace(
                    parsed,
                    source_season=1,
                    source_episode=2,
                    effective_season=9,
                    effective_episode=99,
                )

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "manual-downloads"; target_root = root / "manual-tv"
            source_root.mkdir(); target_root.mkdir()
            (source_root / "Show.S01E02.mkv").write_bytes(b"video")
            source_id = self._source(source_root, target_root, "tv")
            scraper = PositionAwareScraper(MatchResult(
                tmdb_id="42", title="Show", year="2026", media_type="tv", confidence=1.0
            ))
            service = LocalMediaService(scraper=scraper)
            inspection = service.inspect_source("admin", source_id, source_root)
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv"
            )
            self.assertEqual(preview["status"], "planned")
            self.assertIn("S01E02", preview["plans"][0]["target_name"])

    def test_directory_numbering_mode_reuses_shared_season_continuous_mapping(self):
        class SeasonDetailScraper(FakeScraper):
            def get_detail(self, tmdb_id: str, media_type: str, *, force_refresh=False):
                return {
                    "id": int(tmdb_id),
                    "genres": [{"id": 16}],
                    "origin_country": ["JP"],
                    "first_air_date": "2026-01-01",
                    "seasons": [
                        {"season_number": 1, "episode_count": 12},
                        {"season_number": 2, "episode_count": 12},
                    ],
                }

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "numbering-downloads"
            target_root = root / "numbering-library"
            show = source_root / "Example Show Second Season"
            show.mkdir(parents=True)
            target_root.mkdir()
            (show / "Example.Show.S02E13.mkv").write_bytes(b"episode-13")
            (show / "Example.Show.S02E14.mkv").write_bytes(b"episode-14")
            source_id = self._source(source_root, target_root, "tv")
            service = LocalMediaService(scraper=SeasonDetailScraper(MatchResult(
                tmdb_id="42", title="Example Show", year="2026",
                media_type="tv", confidence=1.0,
            )))
            inspection = service.inspect_source("admin", source_id, show)

            preview = service.preview(
                "admin",
                inspection["inspection_id"],
                tmdb_id="42",
                media_type="tv",
                numbering_mode="season_continuous",
            )

            self.assertEqual(preview["numbering_mode"], "season_continuous")
            self.assertEqual(preview["numbering"]["changed"], 2)
            target_names = [item["target_name"] for item in preview["plans"]]
            self.assertTrue(any("S02E01" in name for name in target_names))
            self.assertTrue(any("S02E02" in name for name in target_names))

    def test_manual_task_execution_reuses_persisted_numbering_mode(self):
        class SeasonDetailScraper(FakeScraper):
            def get_detail(self, tmdb_id: str, media_type: str, *, force_refresh=False):
                return {
                    "id": int(tmdb_id),
                    "genres": [{"id": 16}],
                    "origin_country": ["JP"],
                    "first_air_date": "2026-01-01",
                    "seasons": [
                        {"season_number": 1, "episode_count": 12},
                        {"season_number": 2, "episode_count": 12},
                    ],
                }

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw)
            source_root = root / "persisted-numbering-downloads"
            target_root = root / "persisted-numbering-library"
            source_root.mkdir()
            target_root.mkdir()
            episode = source_root / "Example.Show.S02E13.mkv"
            episode.write_bytes(b"episode")
            source_id = db.create_local_media_source(
                name="persisted-numbering-source",
                qb_profile="",
                qb_path_prefix="",
                local_root=str(source_root),
                media_type="tv",
                mode="preview_only",
                stable_seconds=0,
                owner="admin",
            )
            db.upsert_local_library_target(source_id, "tv", str(target_root), owner="admin")
            service = LocalMediaService(scraper=SeasonDetailScraper(MatchResult(
                tmdb_id="42", title="Example Show", year="2026",
                media_type="tv", confidence=1.0,
            )))
            inspection = service.inspect_source("admin", source_id, episode)
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv",
                numbering_mode="season_continuous",
            )
            task_id = service.create_manual_task(
                "admin", inspection["inspection_id"],
                preview_digest=preview["preview_digest"],
            )
            self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))

            result = service.execute_task("admin", task_id)

            self.assertEqual(result["status"], "completed")
            self.assertIn("S02E01", result["preview"]["plans"][0]["target_name"])
            stored = db.get_local_media_task(task_id, owner="admin")
            self.assertEqual(stored.numbering_mode, "season_continuous")
            self.assertNotIn("S09E99", preview["plans"][0]["target_name"])

    def test_source_media_type_is_forwarded_to_automatic_recognition(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "tv"
            source_root.mkdir(); target_root.mkdir()
            (source_root / "Show.S01E01.mkv").write_bytes(b"video")
            source_id = db.create_local_media_source(
                name="tv-source", qb_profile="", qb_path_prefix="", local_root=str(source_root),
                media_type="tv", owner="admin",
            )
            db.upsert_local_library_target(source_id, "tv", str(target_root), owner="admin")
            scraper = FakeScraper(MatchResult(
                tmdb_id="1", title="Show", year="2026", media_type="tv", confidence=1.0
            ))
            service = LocalMediaService(scraper=scraper)
            inspection = service.inspect_source("admin", source_id, source_root)
            preview = service.preview("admin", inspection["inspection_id"])
            self.assertEqual(preview["status"], "planned")
            self.assertEqual(scraper.media_type_hints, ["tv"])

    def test_preview_only_task_finishes_without_moving_files(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            movie = source_root / "Movie.2026.mkv"; movie.write_bytes(b"video")
            source_id = db.create_local_media_source(
                name="preview-source", qb_profile="", qb_path_prefix="", local_root=str(source_root),
                stable_seconds=0, mode="preview_only", owner="admin",
            )
            db.upsert_local_library_target(source_id, "movie", str(target_root), owner="admin")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, movie)
            preview = service.preview("admin", inspection["inspection_id"], tmdb_id="1", media_type="movie")
            task_id = service.create_manual_task(
                "admin", inspection["inspection_id"], preview_digest=preview["preview_digest"],
            )
            self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
            with patch("app.modules.local_media_service.LocalMoveTransaction.execute") as execute:
                result = service.execute_task("admin", task_id)
            execute.assert_not_called()
            self.assertEqual(result["status"], "completed")
            self.assertTrue(movie.exists())
            task = db.get_local_media_task(task_id, owner="admin")
            self.assertEqual(task.status, "completed")
            self.assertEqual((task.title, task.year, task.tmdb_id, task.media_type), (
                "Movie", "2026", "1", "movie",
            ))
            summary = json.loads(task.recognition_summary)
            self.assertEqual(summary["status"], "resolved")
            self.assertEqual(summary["media"][0]["title"], "Movie")
            self.assertIn("仅预览模式", task.warning)

    def test_tv_without_episode_requires_manual_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "tv"
            source_root.mkdir(); target_root.mkdir()
            (source_root / "Show.mkv").write_bytes(b"video")
            source_id = self._source(source_root, target_root, "tv")
            scraper = FakeScraper(MatchResult(tmdb_id="1", title="Show", year="2026",
                                              media_type="tv", confidence=1.0))
            service = LocalMediaService(scraper=scraper)
            inspection = service.inspect_source("admin", source_id, source_root)
            preview = service.preview("admin", inspection["inspection_id"])
            self.assertEqual(preview["status"], "requires_manual")
            self.assertEqual(preview["candidates"][0]["tmdb_id"], "1")
            self.assertEqual(preview["files"], [{"name": "Show.mkv"}])
            self.assertTrue(preview["snapshot_digest"])
            self.assertEqual(list(target_root.rglob("*")), [])

    def test_position_blocked_task_preserves_only_explicit_single_file_identity(self):
        class SeasonScraper(FakeScraper):
            supports_tmdb_position_validation = True
            validate_position = staticmethod(TMDBScraper.validate_position)
            position_validation_error = staticmethod(TMDBScraper.position_validation_error)

            def get_detail(self, tmdb_id, media_type, *, force_refresh=False):
                return {"id": 1, "name": "Show", "first_air_date": "2026-01-01",
                        "seasons": [{"season_number": 2, "episode_count": 10}]}

        for locked, directory in ((True, False), (False, False), (True, True)):
            with self.subTest(locked=locked, directory=directory), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source_root, target_root = root / f"source-{locked}-{directory}", root / "target"
                source_root.mkdir(); target_root.mkdir()
                episode = source_root / "Show.S02E999.mkv"
                episode.write_bytes(b"video")
                source_id = self._source(source_root, target_root, "tv")
                task_id = db.create_local_media_task(
                    source_id, "", str(source_root if directory else episode),
                    owner="admin", trigger="scan",
                )
                self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
                service = LocalMediaService(scraper=SeasonScraper(MatchResult(
                    tmdb_id="1", title="Show", year="2026", media_type="tv",
                    confidence=1.0, status="matched", provider="tmdb", external_id="1",
                    matched_by="tmdb_id" if locked else "search", locked=locked,
                )))
                self.addCleanup(service.close)
                result = service.execute_task("admin", task_id)
                task = db.get_local_media_task(task_id, owner="admin")
                self.assertEqual(result["status"], "requires_manual")
                self.assertIn("文件集号超出", task.error)
                self.assertEqual(task.status, "requires_manual")
                self.assertEqual((task.tmdb_id, task.title, task.year, task.media_type),
                                 ("1", "Show", "2026", "tv") if locked and not directory
                                 else ("", "", "", ""))
                self.assertEqual(episode.read_bytes(), b"video")
                self.assertEqual(list(target_root.rglob("*")), [])
                self.assertEqual(db.list_local_media_task_items(task_id, owner="admin"), [])

    def test_requires_manual_task_review_targets_episode_file(self):
        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "review-downloads"; target_root = root / "tv"
            source_root.mkdir(); target_root.mkdir()
            episode = source_root / "Show.mkv"
            episode.write_bytes(b"video")
            source_id = self._source(source_root, target_root, "tv")
            task_id = db.create_local_media_task(
                source_id, "", str(episode), owner="admin", trigger="scan",
            )
            self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Show", year="2026", media_type="tv", confidence=1.0
            )))

            result = service.execute_task("admin", task_id)

            self.assertEqual(result["status"], "requires_manual")
            task = db.get_local_media_task(task_id, owner="admin")
            self.assertEqual(task.status, "requires_manual")
            self.assertEqual(task.content_path, str(episode))
            self.assertEqual(task.tmdb_id, "")
            inspection = service.inspect_task("admin", task_id)
            self.assertEqual(inspection["primary_video_name"], "Show.mkv")
            self.assertEqual(inspection["suggested_query"], "Show")
            self.assertIn("缺少集数", inspection["task_error"])


    def test_local_media_ignores_legacy_scope_and_template_overrides(self):
        from app.modules.organize import OrganizeRules

        with tempfile.TemporaryDirectory() as root_raw:
            root = Path(root_raw); source_root = root / "downloads"; target_root = root / "movies"
            source_root.mkdir(); target_root.mkdir()
            movie = source_root / "Movie.2026.mkv"; movie.write_bytes(b"video")
            source_id = db.create_local_media_source(
                name="scope-source", qb_profile="", qb_path_prefix="",
                local_root=str(source_root), owner="admin",
            )
            db.upsert_local_library_target(source_id, "movie", str(target_root), owner="admin")
            service = LocalMediaService(scraper=FakeScraper(MatchResult(
                tmdb_id="1", title="Movie", year="2026", media_type="movie", confidence=1.0
            )))
            inspection = service.inspect_source("admin", source_id, source_root)
            legacy = OrganizeRules(
                region_split=False, year_split=False, naming_scope="local",
                movie_dir_template="LOCAL-{title}", movie_template="LOCAL-{title}.{ext}",
            )
            with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=legacy):
                preview = service.preview("admin", inspection["inspection_id"])
            target_path = preview["plans"][0]["target_path"]
            self.assertIn("Movie (2026) {tmdb-1}", target_path)
            self.assertNotIn("LOCAL-", target_path)

    def _prepare_notification_local_task(
        self,
        root: Path,
        *,
        name: str,
        filenames: tuple[str, ...],
        media_type: str,
        mode: str = "move",
        conflict_indexes: tuple[int, ...] = (),
        execute: bool = True,
    ):
        source_root, target_root = root / "downloads", root / "library"
        source_root.mkdir(parents=True)
        target_root.mkdir()
        for filename in filenames:
            (source_root / filename).write_bytes(b"incoming video")
        source_id = db.create_local_media_source(
            name=name, qb_profile="", qb_path_prefix="", local_root=str(source_root),
            stable_seconds=0, mode=mode, owner="admin",
        )
        db.upsert_local_library_target(
            source_id, media_type, str(target_root), owner="admin",
        )
        service = LocalMediaService(scraper=FakeScraper(MatchResult(
            tmdb_id="1", title="Show" if media_type == "tv" else "Movie",
            year="2026", media_type=media_type, confidence=1.0,
        )))
        inspection = service.inspect_source("admin", source_id, source_root)
        rules = OrganizeRules(
            region_split=False, year_split=False, naming_scope="both",
            conflict_strategy=1, emby_refresh=False, clean_empty=False,
            media_probe_enabled=False,
        )
        with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=rules):
            preview = service.preview("admin", inspection["inspection_id"])
            for index in conflict_indexes:
                target = Path(preview["plans"][index]["target_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"existing library version is longer")
            if conflict_indexes:
                preview = service.preview("admin", inspection["inspection_id"])
            task_id = service.create_manual_task(
                "admin", inspection["inspection_id"],
                preview_digest=preview["preview_digest"],
            )
            if execute:
                self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
                service.execute_task("admin", task_id)
        return service, task_id

    def test_download_notification_uses_real_local_media_file_outcome(self):
        from app.modules.local_media_outcomes import local_media_task_outcome
        from app.modules.telegram_notification_center import (
            deserialize_notification_event,
            serialize_notification_event,
        )
        from app.modules.telegram_download_lifecycle import publish_download_lifecycle
        from app.modules import telegram_notification_center as center
        from app.repositories.local_media import reconcile_local_media_downloads
        from app.repositories.telegram_notifications import get_notification
        from app.notifier import render_event

        cases = (
            ("preview", ("Movie.2026.mkv",), "movie", "preview_only", (), "preview_only"),
            ("all-skip", ("Movie.2026.mkv",), "movie", "move", (0,), "conflict_skipped"),
            (
                "partial", ("Show.S01E01.mkv", "Show.S01E02.mkv"),
                "tv", "move", (0,), "partial",
            ),
            ("archive", ("Movie.2026.mkv",), "movie", "move", (), "archived"),
        )
        expected_fields = {
            "preview_only": "未移动",
            "conflict_skipped": "冲突跳过",
            "partial": "已归档",
            "archived": "已归档",
        }
        with (
            patch("app.modules.telegram_notification_policy.notifications_enabled", return_value=True),
            patch("app.modules.telegram_notification_policy.notification_level", return_value="standard"),
            patch("app.modules.telegram_download_lifecycle.config.get_bool", return_value=True),
            patch.object(center, "wake_telegram_notification_dispatcher"),
            patch.object(center, "send_event_result", side_effect=AssertionError("Telegram transport is disabled")),
            patch.object(center, "edit_event_result", side_effect=AssertionError("Telegram transport is disabled")),
            tempfile.TemporaryDirectory() as temporary_root,
        ):
            root = Path(temporary_root)
            for name, filenames, media_type, mode, conflicts, expected_outcome in cases:
                with self.subTest(outcome=expected_outcome):
                    service, task_id = self._prepare_notification_local_task(
                        root / name, name=f"notification-{name}", filenames=filenames,
                        media_type=media_type, mode=mode, conflict_indexes=conflicts,
                    )
                    try:
                        task = db.get_local_media_task(task_id, owner="admin")
                        outcome = local_media_task_outcome(
                            task, db.list_local_media_task_items(task_id, owner="admin"),
                        )
                        self.assertEqual(outcome["file_outcome"], expected_outcome)

                        request_id, _ = db.create_download_request(
                            f"notification-local-{name}", "magnet", title=name, chat_id="100",
                        )
                        db.update_download_request(
                            request_id, status="completed", qb_status="completed",
                            local_import_status="pending",
                            local_import_target=f"local-media-task:{task_id}",
                        )
                        with db.get_conn() as conn:
                            reconcile_local_media_downloads(conn, task_id=task_id)

                        files_before = {str(path): path.read_bytes() for path in (root / name).rglob("*.mkv")}
                        with patch.object(db, "get_local_media_task", wraps=db.get_local_media_task) as read_task, patch.object(
                            db, "list_local_media_task_items", wraps=db.list_local_media_task_items,
                        ) as read_items:
                            published = publish_download_lifecycle(request_id, deliver_now=False)
                        self.assertEqual(read_task.call_count, 1)
                        self.assertEqual(read_items.call_count, 1)
                        self.assertTrue(published.accepted, published)
                        row = get_notification(published.event_key)
                        event = deserialize_notification_event(row["event_json"])
                        local_value = dict(event.fields)["本地整理"]
                        self.assertIn(expected_fields[expected_outcome], local_value)
                        if expected_outcome in {"preview_only", "conflict_skipped"}:
                            self.assertEqual(event.title, "✅ 下载完成（自动入库已跳过）")
                            self.assertEqual(event.state, "downloaded")
                        else:
                            self.assertEqual(event.title, "✅ 下载与入库完成")
                            self.assertEqual(event.state, "completed")
                        self.assertEqual(db.get_download_request(request_id)["local_import_status"], "completed")
                        self.assertEqual(row["event_json"], serialize_notification_event(event))
                        self.assertIn(expected_fields[expected_outcome], render_event(event))
                        self.assertEqual(row["revision"], 1)

                        publish_download_lifecycle(request_id, deliver_now=False)
                        repeated = get_notification(published.event_key)
                        self.assertEqual(repeated["revision"], 1)
                        self.assertEqual(repeated["event_json"], row["event_json"])
                        self.assertEqual({str(path): path.read_bytes() for path in (root / name).rglob("*.mkv")}, files_before)
                    finally:
                        service.close()

            history_id, _ = db.create_download_request(
                "notification-local-history", "magnet", title="history", chat_id="100",
            )
            db.update_download_request(
                history_id, status="completed", qb_status="completed", local_import_status="completed",
                local_import_target="/library/history",
            )
            history = publish_download_lifecycle(history_id, deliver_now=False)
            history_row = get_notification(history.event_key)
            history_event = deserialize_notification_event(history_row["event_json"])
            self.assertEqual(history_event.title, "✅ 下载与入库完成")
            self.assertEqual(dict(history_event.fields)["本地整理"], "完成")

    def test_download_notification_revision_tracks_local_task_completion(self):
        from app.modules.telegram_notification_policy import NotificationTopic
        from app.modules.telegram_notification_center import (
            deserialize_notification_event,
            get_notification_thread_snapshot,
        )
        from app.modules.telegram_download_lifecycle import publish_download_lifecycle
        from app.modules import telegram_notification_center as center
        from app.repositories.local_media import reconcile_local_media_downloads
        from app.repositories.telegram_notifications import get_notification

        with (
            patch("app.modules.telegram_notification_policy.notifications_enabled", return_value=True),
            patch("app.modules.telegram_notification_policy.notification_level", return_value="standard"),
            patch("app.modules.telegram_download_lifecycle.config.get_bool", return_value=True),
            patch.object(center, "wake_telegram_notification_dispatcher"),
            patch.object(center, "send_event_result", side_effect=AssertionError("Telegram transport is disabled")),
            patch.object(center, "edit_event_result", side_effect=AssertionError("Telegram transport is disabled")),
            tempfile.TemporaryDirectory() as temporary_root,
        ):
            service, task_id = self._prepare_notification_local_task(
                Path(temporary_root), name="notification-revision",
                filenames=("Movie.2026.mkv",), media_type="movie", execute=False,
            )
            try:
                request_id, _ = db.create_download_request(
                    "notification-local-revision", "magnet", title="revision", chat_id="100",
                )
                db.update_download_request(
                    request_id, status="completed", qb_status="completed",
                    local_import_status="pending",
                    local_import_target=f"local-media-task:{task_id}",
                )
                first = publish_download_lifecycle(request_id, deliver_now=False)
                first_row = get_notification(first.event_key)
                self.assertEqual(first_row["revision"], 1)
                self.assertNotIn("已归档", first_row["event_json"])

                self.assertTrue(db.claim_local_media_task(task_id, owner="admin"))
                service.execute_task("admin", task_id)
                with db.get_conn() as conn:
                    reconcile_local_media_downloads(conn, task_id=task_id)

                latest = publish_download_lifecycle(request_id, deliver_now=False)
                latest_row = get_notification(latest.event_key)
                self.assertEqual(latest_row["revision"], 2)
                self.assertNotEqual(latest_row["event_json"], first_row["event_json"])
                event = deserialize_notification_event(latest_row["event_json"])
                self.assertIn("已归档", dict(event.fields)["本地整理"])
                snapshot = get_notification_thread_snapshot(
                    f"download:{request_id}", topic=NotificationTopic.DOWNLOAD,
                    chat_id="100",
                )
                self.assertEqual(snapshot.revision, 2)
                self.assertFalse(snapshot.current_revision_delivered)

                publish_download_lifecycle(request_id, deliver_now=False)
                self.assertEqual(get_notification(latest.event_key)["revision"], 2)
            finally:
                service.close()



@pytest.fixture
def local_preview(tmp_path):
    with isolated_test_database(), patch(
        "socket.socket.connect", side_effect=AssertionError("network forbidden")
    ):
        source = tmp_path / "incoming"
        target = tmp_path / "library"
        source.mkdir()
        target.mkdir()
        video = source / "Example.Show.S01E01.mkv"
        subtitle = source / "Example.Show.S01E01.en.srt"
        junk = source / "desktop.ini"
        video.write_bytes(b"episode-one")
        subtitle.write_text("subtitle", encoding="utf-8")
        junk.write_bytes(b"junk")
        source_id = db.create_local_media_source(
            name="isolated-web", qb_profile="", qb_path_prefix="",
            local_root=str(source), media_type="tv", owner="admin",
        )
        db.upsert_local_library_target(source_id, "tv", str(target), owner="admin")
        scraper = FakeScraper(MatchResult(
            tmdb_id="42", title="Example Show", year="2026",
            media_type="tv", confidence=1.0,
        ))
        rules = OrganizeRules(
            region_split=False, year_split=False, naming_scope="both",
            small_file_mb=0, clean_empty=False, media_probe_enabled=False,
            emby_refresh=False,
        )
        service = LocalMediaService(scraper=scraper)
        with patch("app.modules.local_media_service.OrganizeRules.from_config", return_value=rules), patch(
            "app.modules.local_media_service.probe_local_media_profile", return_value=None,
        ):
            inspection = service.inspect_source("admin", source_id, source)
            preview = service.preview(
                "admin", inspection["inspection_id"], tmdb_id="42", media_type="tv"
            )
            assert preview["status"] == "planned"
            case = SimpleNamespace(
                service=service, source_id=source_id, source=source, target=target,
                video=video, subtitle=subtitle, junk=junk, scraper=scraper,
                rules=rules, inspection=inspection, preview=preview,
            )
            try:
                yield case
            finally:
                case.service.close()


def create_web_task(case):
    return case.service.create_manual_task(
        "admin", case.inspection["inspection_id"],
        preview_digest=case.preview["preview_digest"],
    )


@pytest.mark.parametrize("phase", ["before_create", "queued", "restart"])
def test_web_new_sibling_requires_repreview_without_file_writes(local_preview, phase):
    case = local_preview
    if phase != "before_create":
        task_id = create_web_task(case)
    added = case.source / "Example.Show.S01E02.mkv"
    added.write_bytes(b"unconfirmed episode")
    if phase == "before_create":
        task_id = create_web_task(case)
    if phase == "restart":
        case.service.close()
        case.service = LocalMediaService(scraper=case.scraper)
    assert db.claim_local_media_task(task_id, owner="admin")
    result = case.service.execute_task("admin", task_id)
    assert result["status"] == "requires_manual", result
    assert result["repreview_required"] is True
    assert "重新" in result["preview"]["reason"]
    assert case.video.read_bytes() == b"episode-one"
    assert case.subtitle.read_text(encoding="utf-8") == "subtitle"
    assert added.read_bytes() == b"unconfirmed episode"
    assert case.junk.read_bytes() == b"junk"
    assert not list(case.target.rglob("*"))
    assert db.list_local_media_task_items(task_id, owner="admin") == []
    assert db.list_local_media_operation_steps(task_id, owner="admin") == []


@pytest.mark.parametrize("change", ["subtitle", "target", "missing_source", "task_rules"])
def test_web_source_target_companion_and_rules_changes_do_not_write(local_preview, change):
    import json
    case = local_preview
    task_id = create_web_task(case)
    target = Path(case.preview["plans"][0]["target_path"])
    if change == "subtitle":
        case.subtitle.write_text("new subtitle", encoding="utf-8")
    elif change == "target":
        target.parent.mkdir(parents=True)
        target.write_bytes(b"unconfirmed existing target")
    elif change == "missing_source":
        case.video.unlink()
    else:
        rules = json.loads(case.preview["rules_snapshot"])
        rules["clean_empty"] = True
        db.update_local_media_task(task_id, rules_snapshot=json.dumps(rules))
    assert db.claim_local_media_task(task_id, owner="admin")
    result = case.service.execute_task("admin", task_id)
    assert result["status"] == "requires_manual", result
    assert result["repreview_required"]
    assert case.junk.exists() and case.subtitle.exists()
    assert not db.list_local_media_operation_steps(task_id)
    if change == "target":
        assert target.read_bytes() == b"unconfirmed existing target"
    else:
        assert not list(case.target.rglob("*"))
    if change == "missing_source":
        assert "没有可整理的视频" in result["preview"]["reason"]
    else:
        assert case.video.read_bytes() == b"episode-one"


def test_web_restart_preserves_plan_and_frozen_rules(local_preview):
    case = local_preview
    task_id = create_web_task(case)
    assert db.get_local_media_task(task_id).snapshot_digest == case.preview["preview_digest"]
    case.service.close()
    case.service = LocalMediaService(scraper=case.scraper)
    case.rules.naming_scope = "file"
    case.rules.year_split = True
    assert db.claim_local_media_task(task_id, owner="admin")
    result = case.service.execute_task("admin", task_id)
    assert result["status"] == "completed", result
    assert set(result["moved"]) == {p["target_path"] for p in case.preview["plans"]}
    assert not case.video.exists() and not case.subtitle.exists()


def test_web_retry_keeps_binding_until_new_preview_confirmation(local_preview):
    case = local_preview
    task_id = create_web_task(case)
    added = case.source / "Example.Show.S01E02.mkv"
    added.write_bytes(b"new episode")
    assert db.claim_local_media_task(task_id)
    assert case.service.execute_task("admin", task_id)["repreview_required"]
    assert db.reset_local_media_task(task_id)
    assert db.get_local_media_task(task_id).snapshot_digest == case.preview["preview_digest"]
    assert db.claim_local_media_task(task_id)
    assert case.service.execute_task("admin", task_id)["repreview_required"]
    case.inspection = case.service.inspect_source("admin", case.source_id, case.source)
    case.preview = case.service.preview(
        "admin", case.inspection["inspection_id"], tmdb_id="42", media_type="tv"
    )
    assert create_web_task(case) == task_id
    assert db.claim_local_media_task(task_id)
    result = case.service.execute_task("admin", task_id)
    assert result["status"] == "completed", result
    assert len(result["moved"]) == 3  # 两个视频及已确认的字幕。


def test_legacy_manual_task_keeps_history_and_requires_new_preview(local_preview):
    case = local_preview
    old_id = db.create_local_media_task(case.source_id, "", str(case.source / "old"), trigger="manual")
    db.update_local_media_task(old_id, status="completed", title="历史", completed_at=db.now())
    old = db.get_local_media_task(old_id)
    task_id = db.create_local_media_task(case.source_id, "", str(case.source), trigger="manual")
    db.update_local_media_task(task_id, snapshot_digest=case.inspection["digest"])
    db.add_local_media_task_item(task_id, str(case.video), "/historical-target", role="video")
    items = db.list_local_media_task_items(task_id)
    assert db.claim_local_media_task(task_id)
    result = case.service.execute_task("admin", task_id)
    assert result["status"] == "requires_manual" and result["repreview_required"]
    assert case.video.exists() and case.subtitle.exists() and case.junk.exists()
    assert db.list_local_media_task_items(task_id) == items
    assert db.get_local_media_task(old_id) == old
    assert not list(case.target.rglob("*"))
    # writer 的检查刷新了 inspection，用户显式获取新预览后才允许恢复。
    case.inspection = case.service.inspect_source("admin", case.source_id, case.source)
    case.preview = case.service.preview("admin", case.inspection["inspection_id"], "42", "tv")
    assert create_web_task(case) == task_id
    assert db.claim_local_media_task(task_id)
    assert case.service.execute_task("admin", task_id)["status"] == "completed"
    assert db.get_local_media_task(old_id) == old


@pytest.mark.parametrize("trigger", ["scan", "qb_completed"])
def test_automatic_tasks_still_plan_latest_directory(local_preview, trigger):
    case = local_preview
    task_id = db.create_local_media_task(case.source_id, "", str(case.source), trigger=trigger)
    db.update_local_media_task(task_id, snapshot_digest=case.inspection["digest"])
    (case.source / "Example.Show.S01E02.mkv").write_bytes(b"new episode")
    assert db.claim_local_media_task(task_id)
    result = case.service.execute_task("admin", task_id)
    assert result["status"] == "completed", result
    assert len(result["moved"]) == 3


@pytest.mark.parametrize("trigger", ["scan", "qb_completed"])
@pytest.mark.parametrize("fixed_candidate", [False, True])
def test_web_reuses_failed_automatic_task_with_manual_preview_semantics(local_preview, trigger, fixed_candidate):
    from unittest.mock import Mock
    case = local_preview
    qb_hash = "fake-qb-hash" if trigger == "qb_completed" else ""
    task_id = db.create_local_media_task(case.source_id, qb_hash, str(case.source), trigger=trigger)
    db.update_local_media_task(task_id, status="failed", error="prior failure")
    case.scraper.result.confidence = 0.82
    case.preview = case.service.preview(
        "admin", case.inspection["inspection_id"],
        tmdb_id="42" if fixed_candidate else "", media_type="tv",
        season_override=2 if fixed_candidate else None,
    )
    assert case.preview["status"] == "planned", case.preview
    assert create_web_task(case) == task_id
    stored = db.get_local_media_task(task_id)
    assert stored.trigger == trigger and stored.qb_hash == qb_hash
    assert stored.season_override == (2 if fixed_candidate else None)
    assert db.claim_local_media_task(task_id)
    qb = Mock()
    with patch.object(case.service, "preview", wraps=case.service.preview) as replan:
        result = case.service.execute_task("admin", task_id, qb_client=qb if qb_hash else None)
    assert replan.call_args.kwargs["automatic"] is False
    assert result["status"] == "completed", result
    assert set(result["moved"]) == {p["target_path"] for p in case.preview["plans"]}
    if qb_hash:
        qb.delete_torrents.assert_called_once_with(qb_hash, delete_files=False)


@pytest.mark.parametrize("actor", ["human", "agent"])
def test_actor_flag_cannot_bypass_existing_web_ticket(local_preview, actor):
    case = local_preview
    task_id = create_web_task(case)
    db.update_local_media_task(task_id, status="recognizing", confirmation_actor=actor)
    (case.source / "Example.Show.S01E02.mkv").write_bytes(b"unconfirmed")
    result = case.service.execute_task("admin", task_id)
    assert result["repreview_required"]
    assert case.video.exists() and not list(case.target.rglob("*"))


def test_web_io_failure_preserves_diagnostic_instead_of_claiming_plan_changed(local_preview):
    case = local_preview
    task_id = create_web_task(case)
    assert db.claim_local_media_task(task_id)
    with patch.object(case.service, "inspect_source", side_effect=OSError("isolated disk I/O error")):
        with pytest.raises(OSError, match="isolated disk I/O error"):
            case.service.execute_task("admin", task_id)
    stored = db.get_local_media_task(task_id)
    assert stored.status == "failed"
    assert stored.error == "isolated disk I/O error"
    assert case.video.exists() and not list(case.target.rglob("*"))


@pytest.mark.parametrize("change_after_claim", [False, True])
def test_telegram_replaces_web_ticket_and_writer_checks_new_source_digest(local_preview, change_after_claim):
    from app.modules.organize_confirmations import create_local_media_confirmation_actions, start_confirmation
    case = local_preview
    task_id = create_web_task(case)
    db.update_local_media_task(task_id, status="requires_manual")
    # TG 展示的是新的文件集合，不再沿用此前 Web 确认。
    (case.source / "Example.Show.S01E02.mkv").write_bytes(b"newly confirmed episode")
    current = case.service.inspect_source("admin", case.source_id, case.source)
    candidate = {"tmdb_id": "42", "media_type": "tv", "title": "Example Show", "year": "2026"}
    actions = create_local_media_confirmation_actions(
        db.get_local_media_task(task_id), db.get_local_media_source(case.source_id),
        {"snapshot_digest": current["digest"], "rules_snapshot": case.preview["rules_snapshot"],
         "candidate": candidate, "candidates": [candidate], "reason": "needs confirmation"},
        owner="admin", chat_id="100",
    )
    token = actions[0].callback_data.split(":")[1]
    callbacks = []
    original_claim = db.claim_local_media_confirmation_task

    def claim(*args, **kwargs):
        assert kwargs["expected_snapshot_digest"] == case.preview["preview_digest"]
        assert kwargs["confirmed_snapshot_digest"] == current["digest"]
        claimed = original_claim(*args, **kwargs)
        assert claimed
        if change_after_claim:
            (case.source / "Example.Show.S01E03.mkv").write_bytes(b"unconfirmed after claim")
        return claimed

    manager = SimpleNamespace(start_operation=lambda _name, _reference, callback: (
        callbacks.append(callback) or {"ok": True, "task_id": "isolated-worker"}
    ))
    scheduler = SimpleNamespace(service=case.service, qb_factory=lambda: pytest.fail("qB forbidden"))
    with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager), patch(
        "app.modules.local_media_scheduler.get_local_media_scheduler", return_value=scheduler
    ), patch("app.modules.organize_confirmations.publish_confirmation_event", return_value=True), patch(
        "app.modules.organize_confirmations._dispatch_due_confirmation_delivery", return_value=True
    ), patch.object(db, "claim_local_media_confirmation_task", side_effect=claim):
        start_confirmation(token, 0, chat_id="100")
        if change_after_claim:
            with pytest.raises(ValueError, match="重新"):
                callbacks[0]()
            assert db.get_local_media_task(task_id).status == "requires_manual"
            assert case.video.exists() and not list(case.target.rglob("*"))
        else:
            assert callbacks[0]()["local_task_id"] == task_id
            assert db.get_local_media_task(task_id).status == "completed"
            assert len(list(case.target.rglob("*.mkv"))) == 2
    assert db.get_local_media_task(task_id).snapshot_digest == current["digest"]



@pytest.mark.parametrize("confirmation_entry", ["web", "tg"])
@pytest.mark.parametrize("change_at", [None, "before_prepare", "before_confirm", "queued", "retry"])
def test_agent_explicit_remap_rebinds_confirmed_plan_through_writer(local_preview, confirmation_entry, change_at):
    from app.agent.errors import AgentToolError
    from unittest.mock import Mock
    from tests.agent_kernel_test_harness import get_kernel_test_service, reset_kernel_test_service
    case = local_preview
    case.inspection = case.service.inspect_source("admin", case.source_id, case.video)
    case.preview = case.service.preview(
        "admin", case.inspection["inspection_id"], "42", "tv", season_override=2, episode_override=24,
    )
    task_id = create_web_task(case)
    db.update_local_media_task(task_id, status="requires_manual", error="集数超出记录范围")
    if confirmation_entry == "tg":
        task = db.get_local_media_task(task_id)
        assert db.claim_local_media_confirmation_task(
            task_id, expected_version=task.version, expected_snapshot_digest=task.snapshot_digest,
            confirmed_snapshot_digest=case.inspection["digest"], tmdb_id="42", media_type="tv",
            rules_snapshot=case.preview["rules_snapshot"], season_override=2, episode_override=24,
        )
        db.update_local_media_task(task_id, status="requires_manual", error="集数超出记录范围")
    reset_kernel_test_service()
    try:
        agent = get_kernel_test_service()
        agent.invoke("local_media.task_summaries", {"scope": "attention", "limit": 12}, owner="remap-owner")
        with patch("app.agent.local_media_task_actions.get_local_media_service", return_value=case.service), patch(
            "app.agent.local_media_task_actions.get_local_media_scheduler", return_value=Mock()
        ):
            added = case.source / "Example.Show.S01E01.zh.srt"
            if change_at == "before_prepare":
                added.write_bytes(b"unconfirmed companion")
                with pytest.raises(AgentToolError, match="源文件范围已变化"):
                    agent.prepare(
                        "local_media.retry_task", {"task_number": 1, "season": 2, "episode": 12}, owner="remap-owner",
                    )
                assert db.get_local_media_task(task_id).status == "requires_manual"
                assert case.video.exists() and not list(case.target.rglob("*"))
                return
            prepared = agent.prepare(
                "local_media.retry_task",
                {"task_number": 1} if change_at == "retry" else {"task_number": 1, "season": 2, "episode": 12},
                owner="remap-owner",
            )
            if change_at == "before_confirm":
                added.write_bytes(b"unconfirmed companion")
                with pytest.raises(AgentToolError) as stale:
                    agent.confirm(prepared["action_plan"]["plan_id"], owner="remap-owner")
                assert stale.value.code == "confirmation_stale"
                assert db.get_local_media_task(task_id).status == "requires_manual"
                assert case.video.exists() and not list(case.target.rglob("*"))
                return
            agent.confirm(prepared["action_plan"]["plan_id"], owner="remap-owner")
        if change_at == "queued":
            added.write_bytes(b"unconfirmed companion")
        assert db.claim_local_media_task(task_id)
        result = case.service.execute_task("admin", task_id)
        if change_at == "queued":
            assert result["repreview_required"]
            assert case.video.exists() and not list(case.target.rglob("*"))
            return
        assert result["status"] == "completed", result
        expected_position = "S02E24" if change_at == "retry" else "S02E12"
        assert all(expected_position in Path(path).name for path in result["moved"])
        assert not case.video.exists() and not case.subtitle.exists()
    finally:
        reset_kernel_test_service()


if __name__ == "__main__":
    unittest.main()
