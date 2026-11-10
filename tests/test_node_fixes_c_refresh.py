"""C01/C02：真实临时 STRM、来源范围与 durable outbox 回归。"""

from typing import cast
from app.clients.guangya import GuangYaClient
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.repositories import strm as strm_repo
from app.clients.guangya import GuangYaFile
from app.modules import strm
from app.modules.scheduler import STRMScheduler
from tests.support import isolated_test_database
from tests.support import PagedDirectoryTestMixin


class Cloud(PagedDirectoryTestMixin):
    def __init__(self, parent="OldSeries", count=1):
        self.parent = parent
        self.count = count

    def list_dir(self, fid):
        if fid == "source":
            return [GuangYaFile(self.parent, self.parent, True)]
        return [self.file_info(str(i)) for i in range(self.count)]

    def file_info(self, fid):
        return GuangYaFile(fid, f"E{fid}.mkv", False, 100, "e", self.parent)


def change(fid="0", parent="NewSeries"):
    return {"source_id": "source", "kind": "video", "action": "upsert",
            "file_id": fid, "parent_id": parent, "rel_dir": parent,
            "name": f"E{fid}.mkv", "etag": "e", "size": 100}


class RefreshFixesTests(unittest.TestCase):
    def test_C01_full_and_incremental_moves_refresh_both_directories(self):
        for incremental in (False, True):
            with self.subTest(incremental=incremental), isolated_test_database(), tempfile.TemporaryDirectory() as root:
                first = strm.sync_strm("source", "http://play.invalid", root,
                                       client=cast(GuangYaClient, Cloud()), clean_empty_dirs=False)
                old = Path(first["changed_strm_paths"][0])
                if incremental:
                    stats = strm.sync_strm_incremental("source", [change()], "http://play.invalid", root,
                                                       client=cast(GuangYaClient, Cloud("NewSeries")))
                else:
                    stats = strm.sync_strm("source", "http://play.invalid", root,
                                           client=cast(GuangYaClient, Cloud("NewSeries")), clean_empty_dirs=False)
                strm.finalize_changed_paths(stats)
                new = Path(strm_repo.list_strm_index("guangya:source")[0]["strm_path"])
                self.assertFalse(old.exists())
                self.assertTrue(new.is_file())
                self.assertEqual(stats["cleaned"], 1)
                self.assertEqual(set(stats["changed_dirs"]), {str(old.parent), str(new.parent)})
                with patch("app.modules.scheduler.get", side_effect=lambda k, d="": root if k == "STRM_ROOT" else d):
                    STRMScheduler._refresh_media_servers(changed_paths=stats["changed_strm_paths"],
                        changed_dirs=stats["changed_dirs"], persist_only=True)
                self.assertEqual({e["path"] for e in strm_repo.list_strm_refresh_entries()},
                                 {str(old.parent), str(new.parent)})

    def test_C01_failed_move_does_not_report_deleted_old_directory(self):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            initial = strm.sync_strm("source", "http://play.invalid", root,
                                     client=cast(GuangYaClient, Cloud()), clean_empty_dirs=False)
            old = Path(initial["changed_strm_paths"][0])
            with patch.object(strm, "_delete_owned_file", side_effect=OSError("injected")):
                stats = strm.sync_strm_incremental("source", [change()], "http://play.invalid", root,
                                                   client=cast(GuangYaClient, Cloud("NewSeries")))
            self.assertTrue(old.is_file())
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(stats["changed_strm_paths"], [])
            self.assertEqual(strm_repo.list_strm_index("guangya:source")[0]["strm_path"], str(old))

    def test_C02_saturation_persists_exact_batches_and_bounds_in_memory_samples(self):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            source_root = Path(root) / strm.STRM_SUBDIR / "SourceA"
            stats = {}
            for i in range(5300):
                strm._record_changed_path(stats, source_root / f"Library{i}" / "E.strm",
                                          on_refresh_paths=strm_repo.enqueue_strm_refresh_paths)
            strm.finalize_changed_paths(stats)
            self.assertLessEqual(len(stats["changed_strm_paths"]), 5000)
            self.assertLessEqual(len(stats["changed_overflow_dirs"]), 256)
            self.assertEqual(stats["changed_paths_omitted"], 300)
            self.assertNotIn(str(source_root), stats["changed_dirs"])
            self.assertNotIn(str(source_root.parent), stats["changed_dirs"])
            persisted = {entry["path"] for entry in strm_repo.list_strm_refresh_entries()}
            self.assertEqual(len(persisted), 257)
            self.assertEqual(persisted | set(stats["changed_dirs"]),
                             {str(source_root / f"Library{i}") for i in range(5300)})

    def test_C02_unprefixed_source_overflow_reaches_durable_outbox(self):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            stats = {}
            changed = [Path(root) / strm.STRM_SUBDIR / f"Library{i}" / "E.strm" for i in range(6)]
            with patch.object(strm, "_MAX_TRACKED_CHANGED_PATHS", 1), patch.object(strm, "_MAX_TRACKED_OVERFLOW_DIRS", 1):
                for path in changed:
                    strm._record_changed_path(stats, path, on_refresh_paths=strm_repo.enqueue_strm_refresh_paths)
            strm.finalize_changed_paths(stats)
            with patch("app.modules.scheduler.get", side_effect=lambda k, d="": root if k == "STRM_ROOT" else d):
                STRMScheduler._refresh_media_servers(changed_paths=stats["changed_strm_paths"],
                    changed_dirs=stats["changed_dirs"], persist_only=True)
            queued = {entry["path"] for entry in strm_repo.list_strm_refresh_entries()}
            self.assertTrue(all(any(str(path.parent) == q or str(path.parent).startswith(q + "/") for q in queued)
                                for path in changed), f"漏刷新：{queued}")

    def test_C01_remove_only_has_no_stale_upsert_path(self):
        with isolated_test_database(), tempfile.TemporaryDirectory() as root:
            initial = strm.sync_strm("source", "http://play.invalid", root,
                                     client=cast(GuangYaClient, Cloud()), clean_empty_dirs=False)
            old = Path(initial["changed_strm_paths"][0])
            stats = strm.sync_strm_incremental("source", [{"source_id": "source", "kind": "video",
                "action": "remove", "file_id": "0"}], "http://play.invalid", root, client=cast(GuangYaClient, Cloud()))
            self.assertFalse(stats["fallback_required"])
            self.assertEqual(stats["changed_strm_paths"], [str(old)])
            self.assertFalse(old.exists())
