"""项目自有数据库与日志文件权限收紧测试。"""
from __future__ import annotations

import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.logger import _WindowsSafeTimedRotatingFileHandler
from app import private_files
from app.private_files import protect_private_file, protect_sqlite_files


@unittest.skipUnless(os.name == "posix", "POSIX 文件模式测试")
class PrivateFilePermissionTests(unittest.TestCase):
    @staticmethod
    def _mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def test_regular_private_file_is_restricted_to_owner(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "secret.db"
            path.write_text("private", encoding="utf-8")
            path.chmod(0o644)

            self.assertTrue(protect_private_file(path))
            self.assertEqual(self._mode(path), 0o600)

    def test_already_private_file_does_not_reopen_or_fchmod(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "private.db"
            path.write_text("private", encoding="utf-8")
            path.chmod(0o600)

            with patch.object(private_files.os, "open") as open_file, patch.object(
                private_files.os, "fchmod"
            ) as fchmod:
                self.assertTrue(protect_private_file(path))

            open_file.assert_not_called()
            fchmod.assert_not_called()

    def test_symlink_and_directory_are_not_modified(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "external"
            directory.mkdir(mode=0o755)
            target = directory / "target.log"
            target.write_text("external", encoding="utf-8")
            target.chmod(0o644)
            link = Path(root) / "app.log"
            link.symlink_to(target)

            directory_mode = self._mode(directory)
            self.assertFalse(protect_private_file(link))
            self.assertFalse(protect_private_file(directory))
            self.assertEqual(self._mode(target), 0o644)
            self.assertEqual(self._mode(directory), directory_mode)

    def test_sqlite_main_and_sidecars_are_all_restricted(self):
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "mediaflux.db"
            candidates = (
                database,
                Path(f"{database}-wal"),
                Path(f"{database}-shm"),
            )
            for candidate in candidates:
                candidate.write_text("private", encoding="utf-8")
                candidate.chmod(0o666)

            self.assertTrue(protect_sqlite_files(database))
            self.assertEqual([self._mode(path) for path in candidates], [0o600] * 3)

    def test_log_open_and_rollover_keep_private_mode(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "app.log"
            handler = _WindowsSafeTimedRotatingFileHandler(
                path,
                when="S",
                interval=1,
                backupCount=2,
                encoding="utf-8",
            )
            try:
                record = logging.LogRecord(
                    "private-test", logging.INFO, __file__, 1, "first", (), None
                )
                handler.emit(record)
                handler.flush()
                self.assertEqual(self._mode(path), 0o600)

                handler.doRollover()
                handler.emit(record)
                handler.flush()
                self.assertEqual(self._mode(path), 0o600)
                rotated = [item for item in Path(root).iterdir() if item != path]
                self.assertTrue(rotated)
                self.assertTrue(all(self._mode(item) == 0o600 for item in rotated))
            finally:
                handler.close()


if __name__ == "__main__":
    unittest.main()
