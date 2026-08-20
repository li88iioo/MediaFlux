from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.scheduler import STRMScheduler, _merge_organize_changes
from app.modules.strm import (
    STRM_SUBDIR,
    _build_video_index_maps,
    _update_video_index_snapshot,
    generate_strm,
    sync_strm,
    sync_strm_incremental,
)
from tests.support import IsolatedDatabaseTestCase


class _IncrementalClient:
    def __init__(self, files: dict[str, GuangYaFile]):
        self.files = files
        self.list_calls = 0

    def file_info(self, file_id: str):
        return self.files.get(file_id)

    def list_dir(self, _file_id: str):
        self.list_calls += 1
        raise AssertionError("精准增量不应递归扫描目录")

    def get_download_url(self, _file_id: str) -> str:
        raise AssertionError("视频增量不应获取元数据直链")


class _TreeClient:
    def __init__(self, tree: dict[str, list[GuangYaFile]]):
        self.tree = tree

    def list_dir(self, file_id: str):
        return list(self.tree.get(file_id, []))


class StrmP2IncrementalTests(IsolatedDatabaseTestCase):
    @staticmethod
    def _fingerprint(path: Path) -> str:
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    @staticmethod
    def _empty_stats(**updates) -> dict:
        stats = {
            "total": 0, "generated": 0, "created": 0, "updated": 0,
            "skipped": 0, "failed": 0,
            "metadata_total": 0, "metadata_generated": 0,
            "metadata_skipped": 0, "metadata_failed": 0,
            "metadata_cleaned": 0, "cleaned": 0, "clean_skipped": False,
            "empty_dirs_cleaned": 0, "directories": 0,
            "scan_elapsed_seconds": 0.0, "metadata_elapsed_seconds": 0.0,
            "error_samples": [], "changes": [], "omitted_count": 0,
        }
        stats.update(updates)
        return stats

    def test_dual_index_snapshot_preserves_unrelated_rows_and_clears_all_conflicts(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "target.strm"
            target.write_text("payload", encoding="utf-8")
            rows = [
                {"file_id": "file", "strm_path": "/old", "etag": "1", "size": 1,
                 "filename": "old.mkv", "content_fingerprint": ""},
                {"file_id": "conflict-a", "strm_path": str(target), "etag": "1", "size": 1,
                 "filename": "a.mkv", "content_fingerprint": ""},
                {"file_id": "conflict-b", "strm_path": str(target), "etag": "1", "size": 1,
                 "filename": "b.mkv", "content_fingerprint": ""},
                {"file_id": "other", "strm_path": "/other", "etag": "1", "size": 1,
                 "filename": "other.mkv", "content_fingerprint": ""},
            ]
            by_id, by_path = _build_video_index_maps(rows)
            _update_video_index_snapshot(
                by_id, by_path,
                GuangYaFile("file", "target.mkv", False, 2, "2"), target,
            )

            self.assertEqual(set(by_id), {"file", "other"})
            self.assertNotIn("/old", by_path)
            self.assertEqual(set(by_path[str(target)]), {"file"})
            self.assertEqual(set(by_path["/other"]), {"other"})

    def test_incremental_upsert_never_scans_and_second_run_is_current(self):
        source_id = "incremental-source"
        video = GuangYaFile(
            "video-1", "Episode.S01E01.mkv", False, 1024, "etag-1", "target-dir"
        )
        client = _IncrementalClient({video.file_id: video})
        change = {
            "source_id": source_id, "kind": "video", "action": "upsert",
            "file_id": video.file_id, "rel_dir": "剧集/示例/Season 01",
            "name": video.name, "etag": video.etag, "size": video.size,
            "parent_id": video.parent_id,
        }
        with tempfile.TemporaryDirectory() as root:
            first = sync_strm_incremental(
                source_id, [change], "http://media.invalid", root, client=client,
            )
            second = sync_strm_incremental(
                source_id, [change], "http://media.invalid", root, client=client,
            )
            updated_video = GuangYaFile(
                video.file_id, video.name, False, video.size, "etag-2", video.parent_id
            )
            client.files[video.file_id] = updated_video
            updated_change = {**change, "etag": updated_video.etag}
            third = sync_strm_incremental(
                source_id, [updated_change], "http://media.invalid", root, client=client,
            )
            generated = list((Path(root) / STRM_SUBDIR).rglob("*.strm"))

        self.assertEqual(first["generated"], 1)
        self.assertEqual(first["created"], 1)
        self.assertEqual(first["updated"], 0)
        self.assertEqual(first["scanned_files"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(third["generated"], 1)
        self.assertEqual(third["created"], 0)
        self.assertEqual(third["updated"], 1)
        self.assertFalse(first["fallback_required"])
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(len(generated), 1)

    def test_incremental_rename_removes_old_index_and_old_local_file(self):
        source_id = "incremental-rename"
        source_key = f"guangya:{source_id}"
        old = GuangYaFile("old", "Old.mkv", False, 100, "old-etag", "target")
        new = GuangYaFile("new", "New.mkv", False, 200, "new-etag", "target")
        client = _IncrementalClient({new.file_id: new})
        with tempfile.TemporaryDirectory() as root:
            old_path = generate_strm(old, "电影/示例", "http://media.invalid", root)
            db.upsert_strm_index(
                source_key, old.file_id, old.etag, old.size, old.name,
                str(old_path), self._fingerprint(old_path),
            )
            stats = sync_strm_incremental(
                source_id,
                [
                    {
                        "source_id": source_id, "kind": "video", "action": "upsert",
                        "file_id": new.file_id, "rel_dir": "电影/示例",
                        "name": new.name, "etag": new.etag, "size": new.size,
                        "parent_id": new.parent_id,
                    },
                    {
                        "source_id": source_id, "kind": "video", "action": "remove",
                        "file_id": old.file_id,
                    },
                ],
                "http://media.invalid", root, client=client,
            )
            rows = db.list_strm_index(source_key)
            new_files = list((Path(root) / STRM_SUBDIR).rglob("New.mkv.strm"))
            old_exists = old_path.exists()

        self.assertFalse(stats["fallback_required"])
        self.assertEqual(stats["generated"], 1)
        self.assertEqual(stats["cleaned"], 1)
        self.assertFalse(old_exists)
        self.assertEqual([row["file_id"] for row in rows], ["new"])
        self.assertEqual(len(new_files), 1)

    def test_incremental_remote_snapshot_change_requests_full_fallback(self):
        source_id = "incremental-stale"
        current = GuangYaFile("video", "Renamed.mkv", False, 100, "new", "target")
        client = _IncrementalClient({current.file_id: current})
        with tempfile.TemporaryDirectory() as root:
            stats = sync_strm_incremental(
                source_id,
                [{
                    "source_id": source_id, "kind": "video", "action": "upsert",
                    "file_id": current.file_id, "rel_dir": "电影",
                    "name": "Original.mkv", "etag": "old", "size": 100,
                    "parent_id": "target",
                }],
                "http://media.invalid", root, client=client,
            )

        self.assertTrue(stats["fallback_required"])
        self.assertEqual(stats["failed"], 1)
        self.assertIn("名称已变化", stats["fallback_reason"])

    def test_full_sync_deletes_every_index_conflict_for_same_target_path(self):
        source_id = "multi-conflict"
        source_key = f"guangya:{source_id}"
        winner = GuangYaFile("winner", "Movie.mkv", False, 300, "winner", source_id)
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / STRM_SUBDIR / "Movie.mkv.strm"
            target.parent.mkdir(parents=True)
            target.write_text("old", encoding="utf-8")
            for file_id in ("old-a", "old-b"):
                db.upsert_strm_index(
                    source_key, file_id, "old", 1, "Movie.mkv", str(target),
                    f"sha256:{hashlib.sha256(b'old').hexdigest()}",
                )
            stats = sync_strm(
                source_id, "http://media.invalid", root,
                client=_TreeClient({source_id: [winner]}),
            )
            rows = db.list_strm_index(source_key)

        self.assertEqual(stats["generated"], 1)
        self.assertEqual([row["file_id"] for row in rows], ["winner"])

    def test_scheduler_routes_trusted_organize_changes_to_incremental(self):
        scheduler = STRMScheduler()
        source = {"id": "source", "name": "来源", "rel_prefix": ""}
        changes = [{
            "source_id": "source", "kind": "video", "action": "upsert",
            "file_id": "video", "name": "Movie.mkv",
        }]
        values = {
            "GY_STRM_BASE_URL": "http://media.invalid",
            "STRM_ROOT": "/tmp/p2-incremental",
        }
        incremental_stats = self._empty_stats(
            total=1, skipped=1, mode="incremental",
            fallback_required=False, fallback_reason="",
        )
        with patch.object(scheduler, "validate_config", return_value=""), patch.object(
            scheduler, "_source_dirs", return_value=[source]
        ), patch.object(scheduler, "_video_exts", return_value={"mkv"}), patch.object(
            scheduler, "_metadata_exts", return_value=set()
        ), patch.object(scheduler, "_refresh_media_servers", return_value={}), patch.object(
            scheduler, "_notify_success"
        ), patch.object(scheduler, "_notify_details"), patch(
            "app.modules.scheduler.sync_strm_incremental", return_value=incremental_stats
        ) as incremental, patch("app.modules.scheduler.sync_strm") as full, patch(
            "app.modules.scheduler.get", side_effect=lambda key, default="": values.get(key, default)
        ), patch("app.modules.scheduler.get_int", return_value=0):
            result = scheduler.run_blocking("organize", organize_changes=changes)

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "incremental")
        self.assertFalse(result["fallback_used"])
        incremental.assert_called_once()
        self.assertTrue(incremental.call_args.kwargs["defer_metadata"])
        full.assert_not_called()

    def test_scheduler_rejects_unconfigured_organize_source_without_full_fallback(self):
        scheduler = STRMScheduler()
        configured_source = {"id": "configured", "name": "来源", "rel_prefix": ""}
        changes = [{
            "source_id": "unconfigured", "kind": "video", "action": "upsert",
            "file_id": "video", "name": "Movie.mkv",
        }]
        values = {
            "GY_STRM_BASE_URL": "http://media.invalid",
            "STRM_ROOT": "/tmp/p2-source-mismatch",
        }
        with patch.object(scheduler, "validate_config", return_value=""), patch.object(
            scheduler, "_source_dirs", return_value=[configured_source]
        ), patch.object(scheduler, "_video_exts", return_value={"mkv"}), patch.object(
            scheduler, "_metadata_exts", return_value=set()
        ), patch.object(scheduler, "_refresh_media_servers", return_value={}), patch.object(
            scheduler, "_notify_success"
        ), patch.object(scheduler, "_notify_details"), patch(
            "app.modules.scheduler.sync_strm_incremental"
        ) as incremental, patch(
            "app.modules.scheduler.sync_strm"
        ) as full, patch(
            "app.modules.scheduler.get", side_effect=lambda key, default="": values.get(key, default)
        ), patch("app.modules.scheduler.get_int", return_value=0):
            result = scheduler.run_blocking("organize", organize_changes=changes)

        self.assertFalse(result["ok"])
        self.assertIn("STRM 整理联动配置错误", result["error"])
        self.assertIn("未唯一匹配", result["error"])
        incremental.assert_not_called()
        full.assert_not_called()
        self.assertEqual(scheduler.status()["source_runtime"][0]["status"], "failed")

    def test_scheduler_falls_back_to_full_in_same_run(self):
        scheduler = STRMScheduler()
        source = {"id": "source", "name": "来源", "rel_prefix": ""}
        changes = [{
            "source_id": "source", "kind": "video", "action": "upsert",
            "file_id": "video", "name": "Movie.mkv",
        }]
        values = {
            "GY_STRM_BASE_URL": "http://media.invalid",
            "STRM_ROOT": "/tmp/p2-fallback",
        }
        incremental_stats = self._empty_stats(
            total=1, generated=1, created=1, failed=1,
            mode="incremental", fallback_required=True,
            fallback_reason="远端快照变化",
        )
        full_stats = self._empty_stats(
            total=1, generated=1, updated=1, scanned_files=7,
        )
        with patch.object(scheduler, "validate_config", return_value=""), patch.object(
            scheduler, "_source_dirs", return_value=[source]
        ), patch(
            "app.modules.scheduler.configured_strm_source_plans",
            return_value=([source], ""),
        ), patch.object(scheduler, "_video_exts", return_value={"mkv"}), patch.object(
            scheduler, "_metadata_exts", return_value=set()
        ), patch.object(scheduler, "_refresh_media_servers", return_value={}), patch.object(
            scheduler, "_notify_success"
        ), patch.object(scheduler, "_notify_details"), patch(
            "app.modules.scheduler.sync_strm_incremental", return_value=incremental_stats
        ) as incremental, patch(
            "app.modules.scheduler.sync_strm", return_value=full_stats
        ) as full, patch(
            "app.modules.scheduler.clean_retired_strm_sources", return_value={}
        ), patch(
            "app.modules.scheduler.get", side_effect=lambda key, default="": values.get(key, default)
        ), patch("app.modules.scheduler.get_int", return_value=0):
            result = scheduler.run_blocking("organize", organize_changes=changes)

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "full_fallback")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_reason"], "远端快照变化")
        self.assertEqual(result["stats"]["created"], 1)
        self.assertEqual(result["stats"]["updated"], 1)
        self.assertEqual(result["stats"]["scanned_files"], 7)
        incremental.assert_called_once()
        full.assert_called_once()
        self.assertEqual(db.get_last_task_run("strm_sync")["status"], "success")

    def test_jellyfin_refresh_skips_when_no_safe_change_target(self):
        values = {
            "JELLYFIN_URL": "http://jellyfin.invalid",
            "JELLYFIN_API_KEY": "secret",
            "STRM_ROOT": "/media/strm",
        }
        with patch(
            "app.modules.scheduler.get_bool",
            side_effect=lambda key, default=False: key == "JELLYFIN_ENABLED",
        ), patch(
            "app.modules.scheduler.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.modules.scheduler.JellyfinClient") as jellyfin_cls, patch(
            "app.services.clear_dashboard_cache"
        ) as clear_cache:
            result = STRMScheduler._refresh_media_servers(
                emby_enabled=False, has_changes=True
            )

        self.assertEqual(result, {})
        jellyfin_cls.assert_not_called()
        clear_cache.assert_not_called()

    def test_queue_change_merge_keeps_latest_snapshot_per_file(self):
        merged = _merge_organize_changes(
            [
                {"source_id": "a", "kind": "video", "file_id": "same", "action": "upsert"},
                {"source_id": "a", "kind": "metadata", "file_id": "meta", "action": "upsert"},
            ],
            [
                {"source_id": "a", "kind": "video", "file_id": "same", "action": "remove"},
                {"source_id": "b", "kind": "video", "file_id": "other", "action": "upsert"},
            ],
        )
        keyed = {(row["source_id"], row["kind"], row["file_id"]): row for row in merged}
        self.assertEqual(len(keyed), 3)
        self.assertEqual(keyed[("a", "video", "same")]["action"], "remove")
        self.assertIn(("a", "metadata", "meta"), keyed)
        self.assertIn(("b", "video", "other"), keyed)

    def test_full_scan_backfills_missing_fingerprints_in_one_batch_without_rehash(self):
        source_id = "fingerprint-backfill"
        source_key = f"guangya:{source_id}"
        video = GuangYaFile(
            "video-1", "Episode.mkv", False, 1024, "etag-1", source_id
        )
        client = _TreeClient({source_id: [video]})
        with tempfile.TemporaryDirectory() as root:
            first = sync_strm(
                source_id, "http://media.invalid", root,
                client=client, clean_invalid=False, scan_workers=1,
            )
            row = db.list_strm_index(source_key)[0]
            db.upsert_strm_index(
                source_key,
                row["file_id"],
                row["etag"],
                row["size"],
                row["filename"],
                row["strm_path"],
                "",
            )
            with patch(
                "app.modules.strm.db.upsert_strm_index_batch",
                wraps=db.upsert_strm_index_batch,
            ) as batch, patch(
                "app.modules.strm._content_fingerprint",
                side_effect=AssertionError("稳定 STRM 不应再次读取计算指纹"),
            ):
                second = sync_strm(
                    source_id, "http://media.invalid", root,
                    client=client, clean_invalid=False, scan_workers=1,
                )

            persisted = db.list_strm_index(source_key)[0]

        self.assertEqual(first["generated"], 1)
        self.assertEqual(first["created"], 1)
        self.assertEqual(first["updated"], 0)
        self.assertEqual(first["scanned_files"], 1)
        self.assertEqual(second["generated"], 0)
        self.assertEqual(second["skipped"], 1)
        batch.assert_called_once()
        self.assertTrue(str(persisted["content_fingerprint"]).startswith("sha256:"))
