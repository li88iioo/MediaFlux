"""本地媒体成功移动后的来源空目录收尾回归测试。"""
from __future__ import annotations

from typing import cast
from app.modules.scraper import TMDBScraper

import tempfile
from pathlib import Path
from unittest.mock import patch

from app.repositories import local_media as local_media_repo
from app.modules.local_media_cleanup import delete_cleanup_items
from app.modules.local_media_service import (
    LocalMediaPostMoveError,
    LocalMediaService,
    LocalMovePlan,
)
from app.modules.local_move_transaction import LocalMoveError, LocalMoveTransaction
from app.modules.local_storage import LocalFilesystemAdapter
from app.modules.organize import OrganizeRules
from app.modules.scraper import MatchResult
from tests.support import IsolatedDatabaseTestCase
from tests.test_local_media_service import FakeScraper
from tests.test_local_qb_lifecycle import FakeQB


class LocalMediaSourceCleanupTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.source = base / "source"
        self.target = base / "library"
        self.source.mkdir()
        self.target.mkdir()
        self.selected = self.source / "Show"
        self.nested = self.selected / "Disc 1" / "Season 01"
        self.nested.mkdir(parents=True)
        self.video = self.nested / "Show.S01E01.mkv"
        self.video.write_bytes(b"episode-one")
        self.source_id = local_media_repo.create_local_media_source(
            name=f"source-cleanup-{base.name}", qb_profile="", qb_path_prefix="",
            local_root=str(self.source), stable_seconds=0, owner="admin",
        )
        local_media_repo.upsert_local_library_target(self.source_id, "tv", str(self.target), owner="admin")
        self.service = LocalMediaService(scraper=cast(TMDBScraper, FakeScraper(MatchResult(
            tmdb_id="1", title="Show", year="2026", media_type="tv", confidence=1.0,
        ))))
        self.addCleanup(self.service.close)
        self.rules = OrganizeRules(
            region_split=False, year_split=False, small_file_mb=0,
            clean_empty=True, media_probe_enabled=False, emby_refresh=False,
        )

    def _create_task(self, *, selected_path=None, qb_hash=""):
        task_id = local_media_repo.create_local_media_task(
            self.source_id, qb_hash, str(selected_path or self.selected), owner="admin",
            trigger="qb_completed" if qb_hash else "scan",
        )
        local_media_repo.update_local_media_task(
            task_id, owner="admin",
            rules_snapshot=self.service._serialize_rules_snapshot(self.rules),
        )
        self.assertTrue(local_media_repo.claim_local_media_task(task_id, owner="admin"))
        return task_id

    def test_successful_move_removes_nested_source_directories_without_junk(self):
        task_id = self._create_task()

        result = self.service.execute_task("admin", task_id)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["moved"]), 1)
        self.assertEqual(Path(result["moved"][0]).read_bytes(), b"episode-one")
        self.assertEqual(result["deleted_junk"], [])
        self.assertFalse(self.nested.exists())
        self.assertFalse(self.selected.exists())
        self.assertTrue(self.source.is_dir())

    def test_clean_empty_disabled_never_cleans_moved_source_directories(self):
        self.rules.clean_empty = False
        task_id = self._create_task()

        with patch("app.modules.local_media_service.delete_cleanup_items", wraps=delete_cleanup_items) as cleanup:
            result = self.service.execute_task("admin", task_id)

        cleanup.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.video.exists())
        self.assertTrue(self.nested.is_dir())
        self.assertTrue(self.selected.is_dir())

    def test_clean_empty_disabled_keeps_directories_even_when_junk_is_deleted(self):
        self.rules.clean_empty = False
        junk = self.nested / "site.url"
        junk.write_bytes(b"junk")
        task_id = self._create_task()

        result = self.service.execute_task("admin", task_id)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["deleted_junk"], [str(junk)])
        self.assertFalse(self.video.exists())
        self.assertTrue(self.nested.is_dir())
        self.assertTrue(self.selected.is_dir())

    def test_source_root_selection_is_preserved_after_nested_move(self):
        task_id = self._create_task(selected_path=self.source)

        result = self.service.execute_task("admin", task_id)

        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.selected.exists())
        self.assertTrue(self.source.is_dir())
        self.assertEqual(list(self.source.iterdir()), [])

    def test_empty_siblings_outside_moved_ancestry_are_not_scanned_or_removed(self):
        untouched = self.selected / "Unrelated" / "Empty"
        untouched.mkdir(parents=True)
        task_id = self._create_task()

        result = self.service.execute_task("admin", task_id)

        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.nested.exists())
        self.assertTrue(untouched.is_dir())
        self.assertTrue(self.selected.is_dir())

    def test_nonempty_moved_source_parent_retains_unknown_files(self):
        unknown = self.nested / "notes.txt"
        unknown.write_bytes(b"keep")
        task_id = self._create_task()

        result = self.service.execute_task("admin", task_id)

        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.video.exists())
        self.assertEqual(unknown.read_bytes(), b"keep")
        self.assertTrue(self.nested.is_dir())
        self.assertTrue(self.selected.is_dir())

    def test_cleanup_collects_path_parents_of_moved_video_and_subtitle_but_not_skip(self):
        subtitle = self.selected / "Subtitles" / "Show.S01E01.zh.srt"
        subtitle.parent.mkdir()
        subtitle.write_bytes(b"subtitle")
        skipped = self.selected / "Skipped" / "Show.S01E02.mkv"
        skipped.parent.mkdir()
        skipped.write_bytes(b"skip")
        adapter = LocalFilesystemAdapter(self.source)
        plans = [
            LocalMovePlan(adapter.snapshot(self.video), self.target / "episode.mkv", "video", "tv:1"),
            LocalMovePlan(adapter.snapshot(subtitle), self.target / "episode.zh.srt", "subtitle", "tv:1"),
            LocalMovePlan(
                adapter.snapshot(skipped), self.target / "skipped.mkv", "video", "tv:1", action="skip",
            ),
        ]
        task_id = self._create_task()
        preview = {"status": "planned", "digest": "mixed-source-cleanup", "_move_plans": plans}

        with patch.object(self.service, "preview", return_value=preview), patch(
            "app.modules.local_media_service.delete_cleanup_items", wraps=delete_cleanup_items,
        ) as cleanup:
            result = self.service.execute_task("admin", task_id)

        cleanup.assert_called_once()
        source_parents = cleanup.call_args.kwargs["source_parent_dirs"]
        self.assertEqual(source_parents, {self.video.parent, subtitle.parent})
        self.assertTrue(all(isinstance(directory, Path) for directory in source_parents))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["moved"]), 2)
        self.assertEqual((self.target / "episode.mkv").read_bytes(), b"episode-one")
        self.assertEqual((self.target / "episode.zh.srt").read_bytes(), b"subtitle")
        self.assertFalse(self.nested.exists())
        self.assertFalse(subtitle.parent.exists())
        self.assertEqual(skipped.read_bytes(), b"skip")
        self.assertTrue(skipped.parent.is_dir())

    def test_qb_release_failure_does_not_clean_empty_moved_source_directories(self):
        qb = FakeQB(delete_error=OSError("qB delete failed"))
        task_id = self._create_task(qb_hash="hash-1")

        with patch("app.modules.local_media_service.delete_cleanup_items", wraps=delete_cleanup_items) as cleanup:
            result = self.service.execute_task("admin", task_id, qb_client=qb)

        cleanup.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["qb_cleanup_pending"])
        self.assertEqual(qb.calls, [("pause", "hash-1"), ("delete", "hash-1", False)])
        self.assertFalse(self.video.exists())
        self.assertTrue(self.nested.is_dir())
        self.assertTrue(self.selected.is_dir())
        self.assertEqual(Path(result["moved"][0]).read_bytes(), b"episode-one")

    def test_qb_source_directories_are_cleaned_only_after_task_release(self):
        qb = FakeQB()
        task_id = self._create_task(qb_hash="hash-1")

        def cleanup_after_release(*args, **kwargs):
            self.assertEqual(qb.calls, [("pause", "hash-1"), ("delete", "hash-1", False)])
            self.assertEqual(kwargs["source_parent_dirs"], {self.video.parent})
            return delete_cleanup_items(*args, **kwargs)

        with patch(
            "app.modules.local_media_service.delete_cleanup_items", side_effect=cleanup_after_release,
        ) as cleanup:
            result = self.service.execute_task("admin", task_id, qb_client=qb)

        cleanup.assert_called_once()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["qb_cleanup_pending"])
        self.assertFalse(self.selected.exists())
        self.assertTrue(self.source.is_dir())

    def test_rolled_back_move_never_cleans_sources_or_deletes_qb_task(self):
        qb = FakeQB()
        task_id = self._create_task(qb_hash="hash-1")
        original_move = LocalMoveTransaction._move_one

        def move_then_fail(transaction, plan, index):
            original_move(transaction, plan, index)
            raise OSError("injected failure after move")

        with patch.object(
            LocalMoveTransaction, "_move_one", autospec=True, side_effect=move_then_fail,
        ), patch("app.modules.local_media_service.delete_cleanup_items") as cleanup, self.assertRaisesRegex(
            LocalMoveError, "injected failure after move",
        ):
            self.service.execute_task("admin", task_id, qb_client=qb)

        cleanup.assert_not_called()
        self.assertEqual(self.video.read_bytes(), b"episode-one")
        self.assertTrue(self.nested.is_dir())
        self.assertTrue(self.selected.is_dir())
        self.assertEqual(list(self.target.rglob("*.mkv")), [])
        self.assertEqual(qb.calls, [("pause", "hash-1"), ("resume", "hash-1")])
        get_local_media_task_result = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert get_local_media_task_result is not None
        self.assertEqual(get_local_media_task_result.status, "failed")

    def test_incomplete_rollback_keeps_empty_sources_and_qb_paused(self):
        qb = FakeQB()
        task_id = self._create_task(qb_hash="hash-1")
        original_move = LocalMoveTransaction._move_one

        def move_then_fail(transaction, plan, index):
            original_move(transaction, plan, index)
            raise OSError("injected failure after move")

        with patch.object(
            LocalMoveTransaction, "_move_one", autospec=True, side_effect=move_then_fail,
        ), patch.object(LocalMoveTransaction, "rollback", return_value=["injected rollback failure"]), patch(
            "app.modules.local_media_service.delete_cleanup_items",
        ) as cleanup, self.assertRaisesRegex(LocalMediaPostMoveError, "回滚失败"):
            self.service.execute_task("admin", task_id, qb_client=qb)

        cleanup.assert_not_called()
        self.assertFalse(self.video.exists())
        self.assertTrue(self.nested.is_dir())
        self.assertTrue(self.selected.is_dir())
        self.assertEqual(list(self.nested.iterdir()), [])
        self.assertEqual(len(list(self.target.rglob("*.mkv"))), 1)
        self.assertEqual(qb.calls, [("pause", "hash-1")])
        get_local_media_task_result_2 = local_media_repo.get_local_media_task(task_id, owner="admin")
        assert get_local_media_task_result_2 is not None
        self.assertEqual(get_local_media_task_result_2.status, "requires_manual")
