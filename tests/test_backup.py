from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import database
from app.modules.backup import (
    BackupError,
    create_backup,
    recover_pending_restore,
    restore_backup,
    runtime_lifecycle_guard,
    verify_backup,
)
from app.modules.process_lock import CrossProcessLock
from app.runtime_paths import RuntimePaths


def make_paths(root: Path) -> RuntimePaths:
    return RuntimePaths(
        program_dir=root / "program",
        data_dir=root / "data",
        config_dir=root / "config",
        cache_dir=root / "cache",
        log_dir=root / "logs",
        strm_dir=root / "strm",
        trash_dir=root / "trash",
    )


class BackupTests(unittest.TestCase):
    def test_runtime_lifecycle_is_reentrant_within_process_but_keeps_file_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_paths(Path(directory))
            paths.ensure_writable_dirs()

            with runtime_lifecycle_guard(paths):
                with runtime_lifecycle_guard(paths):
                    contender = CrossProcessLock(
                        "runtime-lifecycle", directory=paths.data_dir
                    )
                    self.assertFalse(contender.acquire(blocking=False))
                contender = CrossProcessLock(
                    "runtime-lifecycle", directory=paths.data_dir
                )
                self.assertFalse(contender.acquire(blocking=False))

            contender = CrossProcessLock(
                "runtime-lifecycle", directory=paths.data_dir
            )
            self.assertTrue(contender.acquire(blocking=False))
            contender.release()

    def test_restore_refuses_while_runtime_lifecycle_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_paths(Path(directory))
            paths.ensure_writable_dirs()
            sqlite3.connect(paths.database_path).close()
            archive = create_backup(paths)

            with runtime_lifecycle_guard(paths):
                with self.assertRaisesRegex(BackupError, "服务正在运行"):
                    restore_backup(paths, archive)

    def test_create_backup_waits_for_shared_backup_restore_operation_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_paths(Path(directory))
            paths.ensure_writable_dirs()
            sqlite3.connect(paths.database_path).close()
            held = CrossProcessLock("backup-restore", directory=paths.data_dir)
            self.assertTrue(held.acquire(blocking=False))
            finished = threading.Event()
            result: list[Path] = []

            def run() -> None:
                result.append(create_backup(paths))
                finished.set()

            thread = threading.Thread(target=run)
            thread.start()
            try:
                time.sleep(0.05)
                self.assertFalse(finished.is_set())
            finally:
                held.release()
            thread.join(2)

            self.assertFalse(thread.is_alive())
            self.assertTrue(finished.is_set())
            self.assertTrue(result[0].is_file())

    def test_create_verify_and_restore_preserves_sqlite_env_and_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_paths(root)
            paths.ensure_writable_dirs()
            connection = sqlite3.connect(paths.database_path)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(f"PRAGMA user_version={database.SCHEMA_VERSION}")
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('before')")
            connection.commit()
            paths.env_file.write_text("WEB_PORT=12370\n", encoding="utf-8")
            paths.token_file.write_text('{"token":"secret"}', encoding="utf-8")

            archive = create_backup(paths, reason="unit", source_connection=connection)
            manifest = verify_backup(archive)
            self.assertEqual(
                manifest.payload["database_schema_version"],
                database.SCHEMA_VERSION,
            )
            self.assertEqual(len(manifest.entries), 3)

            connection.close()
            sqlite3.connect(paths.database_path).execute("DROP TABLE sample").connection.close()
            paths.env_file.write_text("WEB_PORT=9999\n", encoding="utf-8")
            paths.token_file.unlink()

            restored = restore_backup(paths, archive)
            self.assertEqual(restored.payload["format_version"], 1)
            self.assertEqual(paths.env_file.read_text(encoding="utf-8"), "WEB_PORT=12370\n")
            self.assertEqual(json.loads(paths.token_file.read_text(encoding="utf-8"))["token"], "secret")
            check = sqlite3.connect(paths.database_path)
            try:
                self.assertEqual(check.execute("SELECT value FROM sample").fetchone()[0], "before")
            finally:
                check.close()

    def test_restore_rejects_future_schema_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_paths(root / "source")
            source.ensure_writable_dirs()
            source_db = sqlite3.connect(source.database_path)
            source_db.execute(
                f"PRAGMA user_version={database.SCHEMA_VERSION + 1}"
            )
            source_db.execute("CREATE TABLE future_data(value TEXT)")
            source_db.execute("INSERT INTO future_data VALUES ('future')")
            source_db.commit()
            source_db.close()
            archive = create_backup(source)

            target = make_paths(root / "target")
            target.ensure_writable_dirs()
            target_db = sqlite3.connect(target.database_path)
            target_db.execute("CREATE TABLE current_data(value TEXT)")
            target_db.execute("INSERT INTO current_data VALUES ('keep')")
            target_db.commit()
            target_db.close()
            target.env_file.write_text("WEB_PORT=1258\n", encoding="utf-8")

            with self.assertRaisesRegex(BackupError, "已拒绝降级恢复"):
                restore_backup(target, archive)

            check = sqlite3.connect(target.database_path)
            try:
                self.assertEqual(
                    check.execute("SELECT value FROM current_data").fetchone()[0],
                    "keep",
                )
                self.assertIsNone(
                    check.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='future_data'"
                    ).fetchone()
                )
            finally:
                check.close()
            self.assertEqual(
                target.env_file.read_text(encoding="utf-8"),
                "WEB_PORT=1258\n",
            )

    def test_restore_rejects_partial_archive_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_paths(root / "source")
            source.ensure_writable_dirs()
            source.env_file.write_text("WEB_PORT=9999\n", encoding="utf-8")
            archive = create_backup(source)
            self.assertEqual(
                [entry["name"] for entry in verify_backup(archive).entries],
                ["config/user.env"],
            )

            target = make_paths(root / "target")
            target.ensure_writable_dirs()
            target_db = sqlite3.connect(target.database_path)
            target_db.execute("CREATE TABLE current_data(value TEXT)")
            target_db.execute("INSERT INTO current_data VALUES ('keep')")
            target_db.commit()
            target_db.close()
            target.env_file.write_text("WEB_PORT=1258\n", encoding="utf-8")

            with self.assertRaisesRegex(BackupError, "完整恢复要求备份包含数据库"):
                restore_backup(target, archive)

            check = sqlite3.connect(target.database_path)
            try:
                self.assertEqual(
                    check.execute("SELECT value FROM current_data").fetchone()[0],
                    "keep",
                )
            finally:
                check.close()
            self.assertEqual(
                target.env_file.read_text(encoding="utf-8"),
                "WEB_PORT=1258\n",
            )

    def test_verify_rejects_hash_tampering_and_extra_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_paths(root)
            paths.ensure_writable_dirs()
            sqlite3.connect(paths.database_path).close()
            archive = create_backup(paths)
            with zipfile.ZipFile(archive, "a") as handle:
                handle.writestr("unexpected.txt", b"bad")
            with self.assertRaises(BackupError):
                verify_backup(archive)

    def test_restore_rejects_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_paths = make_paths(root / "source")
            source_paths.ensure_writable_dirs()
            sqlite3.connect(source_paths.database_path).close()
            source_paths.env_file.write_text("WEB_PORT=12370\n", encoding="utf-8")
            archive = create_backup(source_paths)

            target_paths = make_paths(root / "target")
            target_paths.ensure_writable_dirs()
            real = root / "outside.env"
            real.write_text("safe", encoding="utf-8")
            try:
                target_paths.env_file.symlink_to(real)
            except OSError:
                self.skipTest("环境不支持创建符号链接")
            if not target_paths.env_file.is_symlink():
                self.skipTest("环境未生成真实符号链接")
            with self.assertRaises(BackupError):
                restore_backup(target_paths, archive)
            self.assertEqual(real.read_text(encoding="utf-8"), "safe")

    def test_restore_reads_verified_archive_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = make_paths(root / "trusted")
            trusted.ensure_writable_dirs()
            sqlite3.connect(trusted.database_path).close()
            trusted.env_file.write_text("WEB_PORT=12370\n", encoding="utf-8")
            trusted_archive = create_backup(trusted)

            replacement = make_paths(root / "replacement")
            replacement.ensure_writable_dirs()
            sqlite3.connect(replacement.database_path).close()
            replacement.env_file.write_text("WEB_PORT=9999\n", encoding="utf-8")
            replacement_archive = create_backup(replacement)

            target = make_paths(root / "target")
            target.ensure_writable_dirs()
            target.env_file.write_text("WEB_PORT=1\n", encoding="utf-8")

            real_zip_file = zipfile.ZipFile
            trusted_reader = real_zip_file(trusted_archive)
            replacement_reader = real_zip_file(replacement_archive)
            try:
                with patch(
                    "app.modules.backup.zipfile.ZipFile",
                    side_effect=(trusted_reader, replacement_reader),
                ) as archive_open:
                    restore_backup(target, trusted_archive)
                self.assertEqual(archive_open.call_count, 1)
                self.assertEqual(
                    target.env_file.read_text(encoding="utf-8"),
                    "WEB_PORT=12370\n",
                )
            finally:
                trusted_reader.close()
                replacement_reader.close()

    def test_interrupted_restore_rolls_back_all_files_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_paths(root / "source")
            source.ensure_writable_dirs()
            source_db = sqlite3.connect(source.database_path)
            source_db.execute("CREATE TABLE sample(value TEXT)")
            source_db.execute("INSERT INTO sample VALUES ('new')")
            source_db.commit()
            source_db.close()
            source.env_file.write_text("WEB_PORT=12370\n", encoding="utf-8")
            source.token_file.write_text('{"token":"new"}', encoding="utf-8")
            archive = create_backup(source)

            target = make_paths(root / "target")
            target.ensure_writable_dirs()
            target_db = sqlite3.connect(target.database_path)
            target_db.execute("CREATE TABLE sample(value TEXT)")
            target_db.execute("INSERT INTO sample VALUES ('old')")
            target_db.commit()
            target_db.close()
            target.env_file.write_text("WEB_PORT=1\n", encoding="utf-8")
            target.token_file.write_text('{"token":"old"}', encoding="utf-8")

            import app.modules.backup as backup_module

            real_replace = backup_module.os.replace
            interrupted = False

            def replace_then_interrupt(source_path, destination_path):
                nonlocal interrupted
                result = real_replace(source_path, destination_path)
                source_name = Path(source_path).name
                if (
                    not interrupted
                    and Path(destination_path) == target.database_path
                    and source_name.endswith(".tmp")
                ):
                    interrupted = True
                    raise KeyboardInterrupt("simulated power loss")
                return result

            with patch("app.modules.backup.os.replace", side_effect=replace_then_interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    restore_backup(target, archive)

            self.assertTrue((target.data_dir / ".mediaflux-restore.journal.json").exists())
            self.assertTrue(recover_pending_restore(target))
            self.assertEqual(target.env_file.read_text(encoding="utf-8"), "WEB_PORT=1\n")
            self.assertEqual(target.token_file.read_text(encoding="utf-8"), '{"token":"old"}')
            check = sqlite3.connect(target.database_path)
            try:
                self.assertEqual(check.execute("SELECT value FROM sample").fetchone()[0], "old")
            finally:
                check.close()
            self.assertFalse((target.data_dir / ".mediaflux-restore.journal.json").exists())

    def test_committed_restore_finishes_cleanup_after_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_paths(root / "source")
            source.ensure_writable_dirs()
            sqlite3.connect(source.database_path).close()
            source.env_file.write_text("WEB_PORT=12370\n", encoding="utf-8")
            archive = create_backup(source)

            target = make_paths(root / "target")
            target.ensure_writable_dirs()
            sqlite3.connect(target.database_path).close()
            target.env_file.write_text("WEB_PORT=1\n", encoding="utf-8")

            with patch(
                "app.modules.backup._recover_committed",
                side_effect=KeyboardInterrupt("simulated cleanup interruption"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    restore_backup(target, archive)

            self.assertEqual(target.env_file.read_text(encoding="utf-8"), "WEB_PORT=12370\n")
            self.assertTrue((target.data_dir / ".mediaflux-restore.journal.json").exists())
            self.assertTrue(recover_pending_restore(target))
            self.assertEqual(target.env_file.read_text(encoding="utf-8"), "WEB_PORT=12370\n")
            self.assertFalse((target.data_dir / ".mediaflux-restore.journal.json").exists())
    def test_create_backup_fsyncs_archive_and_destination_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_paths(Path(directory))
            paths.ensure_writable_dirs()
            sqlite3.connect(paths.database_path).close()

            from app.modules import backup as backup_module

            with patch(
                "app.modules.backup.os.fsync", wraps=os.fsync
            ) as file_fsync, patch(
                "app.modules.backup._fsync_directory",
                wraps=backup_module._fsync_directory,
            ) as directory_fsync:
                archive = create_backup(paths)

            self.assertTrue(archive.is_file())
            self.assertTrue(file_fsync.called)
            directory_fsync.assert_called_with(archive.parent)



if __name__ == "__main__":
    unittest.main()
