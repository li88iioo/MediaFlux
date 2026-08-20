"""qB 任务与本地媒体移动的安全调用顺序。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.modules.local_media_service import LocalMediaService
from app.modules.scraper import MatchResult
from tests.support import IsolatedDatabaseTestCase
from tests.test_local_media_service import FakeScraper


class FakeQB:
    def __init__(self, delete_error=None):
        self.calls = []
        self.delete_error = delete_error

    def pause_torrents(self, value): self.calls.append(("pause", value))
    def resume_torrents(self, value): self.calls.append(("resume", value))
    def delete_torrents(self, value, delete_files=False):
        self.calls.append(("delete", value, delete_files))
        if self.delete_error:
            raise self.delete_error


class LocalQBLifecycleTests(IsolatedDatabaseTestCase):
    def _fixture(self):
        root = tempfile.TemporaryDirectory()
        base = Path(root.name); source = base / "source"; target = base / "target"
        source.mkdir(); target.mkdir(); (source / "Movie.2025.mkv").write_bytes(b"movie")
        (source / "下载必看.txt").write_bytes(b"ad")
        (source / "fonts.zip").write_bytes(b"font")
        source_id = db.create_local_media_source(
            name=f"qb-{base.name}", qb_profile="configured:qb", qb_path_prefix="/downloads",
            local_root=str(source), stable_seconds=0, owner="admin",
        )
        db.upsert_local_library_target(source_id, "movie", str(target), owner="admin")
        task_id = db.create_local_media_task(source_id, "hash-1", str(source), owner="admin")
        db.claim_local_media_task(task_id, expected="waiting_stable", owner="admin")
        scraper = FakeScraper(MatchResult(tmdb_id="1", title="Movie", year="2025", media_type="movie", confidence=1))
        return root, source, target, task_id, LocalMediaService(scraper=scraper)

    def test_qb_task_is_removed_only_after_verified_move(self):
        root, source, target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        qb = FakeQB()
        with patch("app.modules.local_media_service.LocalMediaService._refresh_paths", return_value=[]):
            result = service.execute_task("admin", task_id, qb_client=qb)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(qb.calls, [("pause", "hash-1"), ("delete", "hash-1", False)])
        self.assertFalse((source / "Movie.2025.mkv").exists())
        self.assertFalse((source / "下载必看.txt").exists())
        self.assertTrue((source / "fonts.zip").exists())
        self.assertTrue(any(target.rglob("*.mkv")))

    def test_move_failure_resumes_and_never_deletes_qb(self):
        root, _source, _target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        qb = FakeQB()
        with patch("app.modules.local_media_service.LocalMoveTransaction.execute", side_effect=OSError("move failed")):
            with self.assertRaisesRegex(OSError, "move failed"):
                service.execute_task("admin", task_id, qb_client=qb)
        self.assertEqual(qb.calls, [("pause", "hash-1"), ("resume", "hash-1")])
        self.assertEqual(db.get_local_media_task(task_id, owner="admin").status, "failed")

    def test_qb_delete_failure_keeps_junk_files(self):
        root, source, _target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        qb = FakeQB(delete_error=OSError("delete failed"))
        with patch("app.modules.local_media_service.LocalMediaService._refresh_paths", return_value=[]):
            result = service.execute_task("admin", task_id, qb_client=qb)
        self.assertEqual(result["status"], "completed")
        self.assertTrue((source / "下载必看.txt").exists())
        self.assertTrue(result["qb_cleanup_pending"])
        self.assertEqual(qb.calls, [("pause", "hash-1"), ("delete", "hash-1", False)])
        self.assertTrue(any("任务保持暂停" in item for item in result["warnings"]))

    def test_qb_task_without_client_fails_before_any_move(self):
        root, source, _target, task_id, service = self._fixture()
        self.addCleanup(root.cleanup)
        with self.assertRaisesRegex(Exception, "缺少可用客户端"):
            service.execute_task("admin", task_id, qb_client=None)
        self.assertTrue((source / "Movie.2025.mkv").exists())
        self.assertEqual(db.get_local_media_task(task_id, owner="admin").status, "failed")


if __name__ == "__main__":
    unittest.main()
