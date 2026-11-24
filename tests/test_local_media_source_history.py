"""来源改根不应改写已提交媒体的历史身份或阻断同作品后续归档。"""
from __future__ import annotations

import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import config, database as db
from app.modules.local_media_service import LocalMediaService, LocalMediaServiceError, save_local_source_config
from app.repositories import local_media as repository
from tests.support import IsolatedDatabaseTestCase


class LocalMediaSourceHistoryTests(IsolatedDatabaseTestCase):
    def test_nested_single_file_history_survives_source_root_change(self):
        self._check_root_change()

    def test_legacy_original_name_keeps_exact_identity_after_root_change(self):
        self._check_root_change(legacy_original_name=True)

    def test_other_media_history_still_blocks_same_archive_after_root_change(self):
        self._check_root_change(historical_identity="other-media")

    def test_completed_step_without_item_still_requires_manual_check(self):
        self._check_root_change(historical_identity="missing-item")

    def test_number_inside_title_keeps_exact_identity_after_source_root_change(self):
        self._check_root_change(metadata_title="B1 Story SSIS-123")

    def test_root_marker_in_directory_merges_the_same_archived_number(self):
        self._check_root_change(source_scope="Foo:__root__")

    def test_ambiguous_scope_without_number_does_not_guess_an_exact_identity(self):
        self._check_root_change(source_scope="Foo:__root__", legacy_original_name=True, historical_identity="ambiguous")

    def _check_root_change(self, *, legacy_original_name=False, historical_identity="matching", metadata_title="History Synthetic", source_scope="Disc:1"):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        endpoint = f"http://metatube-history-{Path(directory).name}.invalid"
        config.get("GY_ORGANIZE_NSFW_ENABLED")
        assert isinstance(config._cache, dict)
        self.enterContext(patch.dict(config._cache, {
            "GY_ORGANIZE_NSFW_ENABLED": "true",
            "GY_ORGANIZE_NSFW_METATUBE_ENDPOINT": endpoint,
            "GY_ORGANIZE_SMALL_FILE_MB": "0", "GY_ORGANIZE_REGION_SPLIT": "false",
            "GY_ORGANIZE_CONFLICT_STRATEGY": "2",
            "GY_ORGANIZE_YEAR_SPLIT": "false", "GY_ORGANIZE_CLEAN_EMPTY": "false",
            "GY_ORGANIZE_EMBY_REFRESH": "false", "GY_ORGANIZE_LINK_STRM": "false",
        }))
        old_root, new_root, library = [Path(directory) / name for name in ("old", "new", "library")]
        for root in (old_root, new_root, library):
            root.mkdir()
        metadata = {"provider": "javbus", "id": "history:fixed:123", "number": "SSIS-123",
                    "title": metadata_title, "release_date": "2020-01-01"}

        def metatube_reply(_session, url, **kwargs):
            self.assertTrue(url.startswith(endpoint + "/v1/movies/"), url)
            data = [metadata] if url.endswith("/search") else metadata
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": data})

        self.enterContext(patch("app.modules.nsfw.requests.Session.get", metatube_reply))
        self.enterContext(patch("app.modules.local_media_service.probe_local_media_profile", return_value=None))
        self.enterContext(patch("app.modules.local_media_scheduler.get_local_media_scheduler",
                                return_value=SimpleNamespace(reload=lambda: None)))
        source_id = save_local_source_config({
            "name": self._testMethodName, "local_root": str(old_root), "media_type": "nsfw", "enabled": False,
            "targets": [{"category": "movie", "path": str(library)}],
        })["source_id"]
        service = LocalMediaService()
        self.addCleanup(service.close)
        first = old_root / source_scope / "SSIS-123.720p.mkv"
        first.parent.mkdir()
        first.write_bytes(b"old synthetic video")
        inspected = service.inspect_source("admin", source_id, first)
        preview = service.preview("admin", inspected["inspection_id"])
        self.assertEqual(preview["status"], "planned")
        self.assertEqual(preview["pending_confirmations"], [])
        self.assertIn(metadata_title, preview["matches"][0]["title"])
        first_task = service.create_manual_task("admin", inspected["inspection_id"], preview_digest=preview["preview_digest"])
        first_result = service.execute_task("admin", first_task)
        self.assertEqual(first_result["status"], "completed")
        archive = Path(first_result["moved"][0])
        self.assertEqual(archive.read_bytes(), b"old synthetic video")
        if metadata_title == "B1 Story SSIS-123":
            self.assertTrue(archive.name.startswith(metadata_title))
        if legacy_original_name:
            # 模拟旧版 rename_enabled=false 已经提交的事实：原名无番号，
            # 归档作品目录仍保存当时确认的番号；只构造夹具，不替产品补前像。
            original_archive = archive
            archive = archive.with_name("original-scene-name.mkv")
            original_archive.rename(archive)
            with db.get_conn() as conn:
                conn.execute("UPDATE local_media_task_items SET target_path=? WHERE task_id=?", (str(archive), first_task))
                conn.execute("UPDATE local_media_operation_steps SET target_path=? WHERE task_id=?", (str(archive), first_task))
                original_task = repository.get_local_media_task(first_task)
                assert original_task is not None
                snapshot = json.loads(original_task.rules_snapshot)
                snapshot["rename_enabled"] = False
                conn.execute("UPDATE local_media_tasks SET rules_snapshot=? WHERE id=?", (json.dumps(snapshot), first_task))
        if historical_identity == "other-media":
            with db.get_conn() as conn:
                conn.execute("UPDATE local_media_task_items SET media_group='movie:tmdb:4242:Disc:1' WHERE task_id=?", (first_task,))
        elif historical_identity == "missing-item":
            with db.get_conn() as conn:
                conn.execute("DELETE FROM local_media_task_items WHERE task_id=?", (first_task,))
        old_items = [dict(row) for row in repository.list_local_media_task_items(first_task)]
        old_steps = [dict(row) for row in repository.list_local_media_operation_steps(first_task)]
        if historical_identity == "matching":
            self.assertTrue(old_items[0]["media_group"].endswith(":" + source_scope))
            self.assertIn("javbus:history:fixed:123", old_items[0]["media_group"])

        save_local_source_config({"local_root": str(new_root)}, source_id=source_id)
        next_file = new_root / "Disc:2" / "SSIS-123.1080p.mkv"
        next_file.parent.mkdir()
        next_file.write_bytes(b"new higher-resolution synthetic video")
        next_inspected = service.inspect_source("admin", source_id, next_file)
        if historical_identity in {"missing-item", "ambiguous"}:
            reason = "身份快照已缺失" if historical_identity == "missing-item" else "缺少可验证的媒体身份"
            with self.assertRaisesRegex(LocalMediaServiceError, reason):
                service.preview("admin", next_inspected["inspection_id"])
            self.assertEqual(next_file.read_bytes(), b"new higher-resolution synthetic video")
            self.assertEqual(archive.read_bytes(), b"old synthetic video")
            self.assertEqual([dict(row) for row in repository.list_local_media_operation_steps(first_task)], old_steps)
            return
        next_preview = service.preview("admin", next_inspected["inspection_id"])
        if historical_identity == "other-media":
            self.assertEqual(next_preview["status"], "requires_manual")
            self.assertIn("其他媒体", next_preview["reason"])
            self.assertEqual(next_file.read_bytes(), b"new higher-resolution synthetic video")
            self.assertEqual(archive.read_bytes(), b"old synthetic video")
            self.assertEqual([dict(row) for row in repository.list_local_media_task_items(first_task)], old_items)
            self.assertEqual([dict(row) for row in repository.list_local_media_operation_steps(first_task)], old_steps)
            return
        self.assertEqual(next_preview["status"], "planned")
        self.assertEqual(next_preview["pending_confirmations"], [])
        next_task = service.create_manual_task("admin", next_inspected["inspection_id"], preview_digest=next_preview["preview_digest"])
        next_result = service.execute_task("admin", next_task)
        self.assertEqual(next_result["status"], "completed")
        self.assertFalse(next_file.exists())
        self.assertEqual(Path(next_result["moved"][0]).read_bytes(), b"new higher-resolution synthetic video")
        self.assertEqual([dict(row) for row in repository.list_local_media_task_items(first_task)], old_items)
        self.assertEqual([dict(row) for row in repository.list_local_media_operation_steps(first_task)], old_steps)
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM local_media_tasks WHERE source_id=?", (source_id,)).fetchone()[0], 2)
