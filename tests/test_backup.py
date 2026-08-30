from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import contextmanager
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
    def test_create_app_recovers_pending_restore_before_reading_session_secret(self) -> None:
        from app import main

        events: list[str] = []

        @contextmanager
        def lifecycle_guard(_paths):
            events.append("lock")
            yield

        with patch(
            "app.modules.backup.runtime_lifecycle_guard",
            side_effect=lifecycle_guard,
        ), patch(
            "app.modules.backup.recover_pending_restore",
            side_effect=lambda *_args, **_kwargs: events.append("recover") or True,
        ), patch.object(
            main,
            "_secret_key",
            side_effect=lambda: events.append("secret") or "test-secret",
        ):
            main.create_app(start_background=False)

        self.assertLess(events.index("recover"), events.index("secret"))

    def test_background_app_holds_startup_lifecycle_until_lifespan_or_launcher_releases(self) -> None:
        from app import main

        events: list[str] = []

        @contextmanager
        def lifecycle_guard(_paths):
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        with patch(
            "app.modules.backup.runtime_lifecycle_guard",
            side_effect=lifecycle_guard,
        ), patch(
            "app.modules.backup.recover_pending_restore",
            return_value=False,
        ), patch.object(main, "_secret_key", return_value="test-secret"):
            app = main.create_app(start_background=True)
            self.assertEqual(events, ["enter"])
            app.state.release_startup_lifecycle_guard()
            app.state.release_startup_lifecycle_guard()

        self.assertEqual(events, ["enter", "exit"])

    def test_background_app_releases_startup_guard_when_late_construction_fails(self) -> None:
        from app import main

        events: list[str] = []

        @contextmanager
        def lifecycle_guard(_paths):
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        with patch(
            "app.modules.backup.runtime_lifecycle_guard",
            side_effect=lifecycle_guard,
        ), patch(
            "app.modules.backup.recover_pending_restore",
            return_value=False,
        ), patch.object(
            main, "_secret_key", return_value="test-secret"
        ), patch.object(
            main,
            "FastAPI",
            side_effect=RuntimeError("late app construction failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "late app construction failure"):
                main.create_app(start_background=True)

        self.assertEqual(events, ["enter", "exit"])

    def test_normal_startup_guard_unregisters_atexit_callback_when_released(self) -> None:
        from app import main

        @contextmanager
        def lifecycle_guard(_paths):
            yield

        with patch(
            "app.modules.backup.runtime_lifecycle_guard",
            side_effect=lifecycle_guard,
        ), patch(
            "app.modules.backup.recover_pending_restore",
            return_value=False,
        ), patch.object(
            main, "_secret_key", return_value="test-secret"
        ), patch.object(
            main.atexit, "register"
        ) as register, patch.object(
            main.atexit, "unregister"
        ) as unregister:
            app = main.create_app(start_background=True)
            callback = app.state.release_startup_lifecycle_guard
            register.assert_called_once_with(callback)
            callback()
            callback()

        unregister.assert_called_once_with(callback)

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

    def test_pending_restore_uses_fixed_lifecycle_config_backup_lock_order(self) -> None:
        from app.modules import backup as backup_module

        with tempfile.TemporaryDirectory() as directory:
            paths = make_paths(Path(directory))
            events: list[str] = []

            @contextmanager
            def guard(label: str):
                events.append(f"{label}-enter")
                try:
                    yield
                finally:
                    events.append(f"{label}-exit")

            with patch.object(
                backup_module,
                "_exclusive_runtime_lifecycle_guard",
                side_effect=lambda _paths: guard("lifecycle"),
            ), patch.object(
                backup_module,
                "config_snapshot_guard",
                side_effect=lambda _paths: guard("config"),
            ), patch.object(
                backup_module,
                "_backup_operation_guard",
                side_effect=lambda _paths: guard("backup"),
            ):
                self.assertFalse(recover_pending_restore(paths))

        self.assertEqual(
            events,
            [
                "lifecycle-enter",
                "config-enter",
                "backup-enter",
                "backup-exit",
                "config-exit",
                "lifecycle-exit",
            ],
        )

    def test_config_publish_waits_until_database_and_env_backup_snapshot_complete(self) -> None:
        from app import config as app_config
        from app.modules import backup as backup_module

        with tempfile.TemporaryDirectory() as directory:
            paths = make_paths(Path(directory))
            paths.ensure_writable_dirs()
            connection = sqlite3.connect(paths.database_path)
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('old')")
            connection.commit()
            connection.close()
            paths.env_file.write_text("BACKUP_LOCK_TEST='old' # mediaflux-literal\n", encoding="utf-8")
            expected = paths.env_file.read_bytes()
            snapshot_started = threading.Event()
            release_snapshot = threading.Event()
            database_write_started = threading.Event()
            config_finished = threading.Event()
            archive_paths: list[Path] = []
            original_snapshot = backup_module._sqlite_backup_bytes

            def slow_snapshot(*args, **kwargs):
                snapshot_started.set()
                self.assertTrue(release_snapshot.wait(2))
                return original_snapshot(*args, **kwargs)

            def run_backup() -> None:
                archive_paths.append(create_backup(paths, reason="consistent"))

            def publish_config() -> None:
                writer = sqlite3.connect(paths.database_path, timeout=2)
                try:
                    writer.execute("UPDATE sample SET value='new'")
                    database_write_started.set()
                    app_config.update_runtime_env_file(
                        paths.env_file,
                        {"BACKUP_LOCK_TEST": "new"},
                        expected=expected,
                    )
                    writer.commit()
                    config_finished.set()
                finally:
                    writer.close()

            previous = os.environ.pop("BACKUP_LOCK_TEST", None)
            try:
                with patch.object(app_config, "PATHS", paths), patch.object(
                    app_config, "ENV_FILE", paths.env_file
                ), patch.object(
                    app_config, "_STARTUP_ENV_OVERRIDES", frozenset()
                ), patch.object(
                    backup_module, "_sqlite_backup_bytes", side_effect=slow_snapshot
                ):
                    backup_thread = threading.Thread(target=run_backup)
                    backup_thread.start()
                    self.assertTrue(snapshot_started.wait(2))
                    config_thread = threading.Thread(target=publish_config)
                    config_thread.start()
                    self.assertTrue(database_write_started.wait(2))
                    time.sleep(0.05)
                    self.assertFalse(config_finished.is_set())
                    release_snapshot.set()
                    backup_thread.join(2)
                    config_thread.join(2)
            finally:
                if previous is None:
                    os.environ.pop("BACKUP_LOCK_TEST", None)
                else:
                    os.environ["BACKUP_LOCK_TEST"] = previous

            self.assertFalse(backup_thread.is_alive())
            self.assertFalse(config_thread.is_alive())
            self.assertTrue(config_finished.is_set())
            with zipfile.ZipFile(archive_paths[0]) as archive:
                self.assertEqual(archive.read("config/user.env"), expected)
                archived_database = Path(directory) / "archived.db"
                archived_database.write_bytes(archive.read("database/mediaflux.db"))
            archived = sqlite3.connect(archived_database)
            try:
                self.assertEqual(
                    archived.execute("SELECT value FROM sample").fetchone()[0],
                    "old",
                )
            finally:
                archived.close()
            self.assertIn("BACKUP_LOCK_TEST='new'", paths.env_file.read_text(encoding="utf-8"))

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
