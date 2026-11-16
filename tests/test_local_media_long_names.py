"""合法长 basename 的跨盘移动、补偿、回收和垃圾清理。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.modules.local_media_candidates import move_candidate_to_trash
from app.modules.local_media_cleanup import delete_cleanup_items, discover_cleanup_candidates
from app.modules.local_media_service import LocalMovePlan
from app.modules.local_move_transaction import LocalMoveError, LocalMoveTransaction
from app.modules.local_storage import LocalFilesystemAdapter


LONG_NAME = "长" * 75 + ".mkv"


class LocalMediaLongNameTests(unittest.TestCase):
    def roots(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        source, target = Path(directory) / "source", Path(directory) / "target"
        source.mkdir()
        target.mkdir()
        return source, target

    def test_cross_filesystem_move_and_replace_accept_long_source_and_target_names(self):
        for action in ("move", "replace"):
            with self.subTest(action=action):
                source_root, target_root = self.roots()
                source, target = source_root / LONG_NAME, target_root / LONG_NAME
                source.write_bytes(b"new long-name video")
                if action == "replace":
                    target.write_bytes(b"old video")
                plan = LocalMovePlan(LocalFilesystemAdapter(source_root).snapshot(source), target, "video", "group", action=action)
                with patch.object(LocalFilesystemAdapter, "same_filesystem", return_value=False):
                    result = LocalMoveTransaction([source_root], [target_root]).execute([plan])
                self.assertEqual(result.status, "completed")
                self.assertTrue(result.moved[0].cross_filesystem)
                self.assertFalse(source.exists())
                self.assertEqual(target.read_bytes(), b"new long-name video")
                self.assertEqual(list(source_root.iterdir()), [])
                self.assertEqual(list(target_root.iterdir()), [target])

    def test_later_failure_restores_long_source_and_replaced_target(self):
        source_root, target_root = self.roots()
        source, target = source_root / LONG_NAME, target_root / LONG_NAME
        source.write_bytes(b"new long-name video")
        target.write_bytes(b"old long-name video")
        second = source_root / "Second.mkv"
        second.write_bytes(b"second video")
        occupied = target_root / second.name
        occupied.write_bytes(b"external occupied target")
        adapter = LocalFilesystemAdapter(source_root)
        plans = [LocalMovePlan(adapter.snapshot(source), target, "video", "group", action="replace"),
                 LocalMovePlan(adapter.snapshot(second), occupied, "video", "group")]
        with patch.object(LocalFilesystemAdapter, "same_filesystem", return_value=False), self.assertRaises(LocalMoveError) as failure:
            LocalMoveTransaction([source_root], [target_root]).execute(plans)
        self.assertIn("已存在目标文件", str(failure.exception))
        self.assertEqual(failure.exception.rollback_errors, [])
        self.assertEqual(source.read_bytes(), b"new long-name video")
        self.assertEqual(target.read_bytes(), b"old long-name video")
        self.assertEqual(second.read_bytes(), b"second video")
        self.assertEqual(occupied.read_bytes(), b"external occupied target")
        self.assertEqual(set(source_root.iterdir()), {source, second})
        self.assertEqual(set(target_root.iterdir()), {target, occupied})

    def test_recycling_long_file_or_directory_preserves_original_basename(self):
        for kind in ("file", "directory"):
            with self.subTest(kind=kind):
                root, _ = self.roots()
                selected = root / LONG_NAME
                if kind == "directory":
                    selected.mkdir()
                    (selected / "Movie.mkv").write_bytes(b"movie")
                else:
                    selected.write_bytes(b"movie")
                info = selected.stat()
                identity = {"size": str(info.st_size), "mtime_ns": str(info.st_mtime_ns),
                            "device": str(info.st_dev), "inode": str(info.st_ino)}
                destination = move_candidate_to_trash(SimpleNamespace(local_root=str(root)), selected, identity)
                self.assertFalse(selected.exists())
                self.assertEqual(destination.name, LONG_NAME)
                archived = destination / "Movie.mkv" if kind == "directory" else destination
                self.assertEqual(archived.read_bytes(), b"movie")
                self.assertTrue(destination.is_relative_to(root / ".mediaflux-trash"))

    def test_cleanup_of_long_junk_name_preserves_unknown_companions(self):
        root, _ = self.roots()
        junk = root / ("长" * 75 + ".tmp")
        junk.write_bytes(b"confirmed temporary junk")
        unknown = root / "notes.txt"
        unknown.write_bytes(b"personal note")
        candidates = discover_cleanup_candidates(root, root)
        result = delete_cleanup_items(candidates, allowed_root=root)
        self.assertEqual(result.deleted, [str(junk)])
        self.assertEqual(result.retained, [])
        self.assertEqual(result.warnings, [])
        self.assertFalse(junk.exists())
        self.assertEqual(unknown.read_bytes(), b"personal note")
