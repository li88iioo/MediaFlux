"""测试运行时的 SQLite 数据库隔离契约。"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database as db


class DatabaseIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.original_test_mode = bool(getattr(db, "_configured_test_mode", False))
        self.original_environment = {
            key: os.environ.get(key)
            for key in ("MEDIAFLUX_TEST_MODE", "MEDIAFLUX_TEST_DB_PATH")
        }

    def tearDown(self) -> None:
        db.configure_database(
            self.original_db_path,
            test_mode=self.original_test_mode,
        )
        for key, value in self.original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _fingerprint(path: Path) -> tuple[bool, int | None, int | None]:
        if not path.exists():
            return False, None, None
        stat = path.stat()
        return True, stat.st_size, stat.st_mtime_ns

    def test_default_suite_database_is_isolated_from_runtime_database(self):
        self.assertEqual(os.environ.get("MEDIAFLUX_TEST_MODE"), "1")
        resolved = db.resolve_db_path()
        self.assertEqual(
            resolved,
            Path(os.environ["MEDIAFLUX_TEST_DB_PATH"]).resolve(),
        )
        self.assertNotEqual(resolved, db.production_db_path())
        self.assertTrue(
            resolved.is_relative_to(Path(os.environ["MEDIAFLUX_DATA_DIR"]).resolve())
        )

    def test_bootstrap_overrides_ambient_runtime_paths_in_fresh_process(self):
        project_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as root:
            ambient_root = Path(root)
            ambient = {
                "MEDIAFLUX_TEST_MODE": "0",
                "MEDIAFLUX_TEST_DB_PATH": str(ambient_root / "ambient.db"),
                "MEDIAFLUX_DATA_DIR": str(ambient_root / "data"),
                "MEDIAFLUX_CONFIG_DIR": str(ambient_root / "config"),
                "MEDIAFLUX_CACHE_DIR": str(ambient_root / "cache"),
                "MEDIAFLUX_LOG_DIR": str(ambient_root / "logs"),
                "MEDIAFLUX_STRM_DIR": str(ambient_root / "strm"),
                "MEDIAFLUX_DISABLE_FILE_LOGGING": "0",
            }
            environment = dict(os.environ)
            environment.update(ambient)
            probe = textwrap.dedent(
                """
                import json
                import os
                import tests
                from app import config, database

                print(json.dumps({
                    "test_mode": os.environ.get("MEDIAFLUX_TEST_MODE"),
                    "db": str(database.resolve_db_path()),
                    "data": str(config.PATHS.data_dir),
                    "config": str(config.PATHS.config_dir),
                    "cache": str(config.PATHS.cache_dir),
                    "logs": str(config.PATHS.log_dir),
                    "strm": str(config.PATHS.strm_dir),
                    "test_root": str(tests._TEST_ROOT),
                }, sort_keys=True))
                """
            )
            completed = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            observed = json.loads(completed.stdout.strip().splitlines()[-1])

        test_root = Path(observed.pop("test_root")).resolve()
        self.assertEqual(observed.pop("test_mode"), "1")
        ambient_keys = {
            "db": "MEDIAFLUX_TEST_DB_PATH",
            "data": "MEDIAFLUX_DATA_DIR",
            "config": "MEDIAFLUX_CONFIG_DIR",
            "cache": "MEDIAFLUX_CACHE_DIR",
            "logs": "MEDIAFLUX_LOG_DIR",
            "strm": "MEDIAFLUX_STRM_DIR",
        }
        for key, value in observed.items():
            with self.subTest(key=key):
                resolved = Path(value).resolve()
                self.assertTrue(resolved.is_relative_to(test_root))
                self.assertNotEqual(resolved, Path(ambient[ambient_keys[key]]).resolve())

    def test_test_mode_rejects_production_database(self):
        with self.assertRaisesRegex(RuntimeError, "生产数据库"):
            db.configure_database(db.production_db_path(), test_mode=True)

    def test_environment_test_path_is_only_used_in_test_mode(self):
        production = db.production_db_path()
        with tempfile.TemporaryDirectory() as root:
            test_path = Path(root) / "env-test.db"
            with patch.dict(
                os.environ,
                {
                    "MEDIAFLUX_TEST_MODE": "0",
                    "MEDIAFLUX_TEST_DB_PATH": str(test_path),
                },
                clear=False,
            ):
                db.configure_database(production, test_mode=False)
                self.assertEqual(db.resolve_db_path(), production)
                with patch.dict(os.environ, {"MEDIAFLUX_TEST_MODE": "1"}, clear=False):
                    self.assertEqual(db.resolve_db_path(), test_path.resolve())

    def test_environment_test_mode_rejects_production_database(self):
        production = db.production_db_path()
        with patch.dict(
            os.environ,
            {
                "MEDIAFLUX_TEST_MODE": "1",
                "MEDIAFLUX_TEST_DB_PATH": str(production),
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "生产数据库"):
                db.resolve_db_path()

    def test_temp_database_does_not_touch_production_file(self):
        production = db.production_db_path()
        before = self._fingerprint(production)
        with tempfile.TemporaryDirectory() as root:
            expected = (Path(root) / "test.db").resolve()
            path = db.configure_database(expected, test_mode=True)
            db.init_db()
            self.assertEqual(path, expected)
            self.assertTrue(expected.is_file())
        self.assertEqual(self._fingerprint(production), before)

    def test_patching_db_path_remains_compatible(self):
        with tempfile.TemporaryDirectory() as root:
            test_path = Path(root) / "patched.db"
            with patch("app.database.DB_PATH", test_path):
                self.assertEqual(db.resolve_db_path(), test_path.resolve())
                db.init_db()
            self.assertTrue(test_path.is_file())

    @unittest.skipUnless(os.name == "posix", "POSIX 文件模式测试")
    def test_database_file_is_private_without_changing_parent_directory(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            root_path.chmod(0o755)
            test_path = root_path / "private.db"
            test_path.touch(mode=0o644)
            db.configure_database(test_path, test_mode=True)

            db.init_db()
            with db.get_conn() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS private_mode_probe (id INTEGER)"
                )
                connection.execute("INSERT INTO private_mode_probe(id) VALUES (1)")
                for candidate in (
                    test_path,
                    Path(f"{test_path}-wal"),
                    Path(f"{test_path}-shm"),
                ):
                    if candidate.exists():
                        self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o600)

            self.assertEqual(stat.S_IMODE(test_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(root_path.stat().st_mode), 0o755)

    def test_scraper_organizer_audit_regression_never_connects_production_database(self):
        import sqlite3
        from tests.test_production import ScraperAndOrganizerTests

        checkout_database = (Path(__file__).resolve().parent.parent / "db" / "mediaflux.db").resolve()
        real_connect = sqlite3.connect

        def guarded_connect(path, *args, **kwargs):
            if Path(path).expanduser().resolve() == checkout_database:
                raise AssertionError("测试禁止连接源码工作区数据库")
            return real_connect(path, *args, **kwargs)

        case = ScraperAndOrganizerTests(
            "test_safe_replacement_keeps_existing_until_incoming_succeeds"
        )
        result = unittest.TestResult()
        suite = unittest.TestSuite([case])
        with tempfile.TemporaryDirectory() as root:
            isolated = Path(root) / "audit-regression.db"
            db.configure_database(isolated, test_mode=True)
            db.init_db()
            with patch("app.database.sqlite3.connect", side_effect=guarded_connect):
                suite.run(result)

        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)

    def test_get_conn_logs_rollback_and_close_failures_without_masking_original_error(self):
        import sqlite3

        class BrokenConnection:
            def rollback(self):
                raise sqlite3.Error("rollback failed")

            def close(self):
                raise sqlite3.Error("close failed")

        with patch("app.database._connect", return_value=BrokenConnection()), patch.object(
            db.logger, "warning"
        ) as warning:
            with self.assertRaisesRegex(RuntimeError, "original failure"):
                with db.get_conn():
                    raise RuntimeError("original failure")

        self.assertEqual(warning.call_count, 2)
        self.assertIn("数据库回滚失败", warning.call_args_list[0].args[0])
        self.assertIn("数据库连接关闭失败", warning.call_args_list[1].args[0])

    def test_shared_test_database_initializes_schema_and_restores_path(self):
        from tests.support import isolated_test_database

        with tempfile.TemporaryDirectory() as root:
            previous = db.configure_database(Path(root) / "previous.db")
            with isolated_test_database("shared.db") as isolated:
                self.assertEqual(db.resolve_db_path(), isolated)
                with db.get_conn() as conn:
                    row = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='strm_index'"
                    ).fetchone()
                self.assertIsNotNone(row)
            self.assertEqual(db.resolve_db_path(), previous)


if __name__ == "__main__":
    unittest.main()


class SQLiteContentionObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        db._reset_sqlite_contention_metrics_for_tests()

    def test_operation_contention_is_counted_without_masking_original_error(self):
        import sqlite3

        class Connection:
            def rollback(self):
                pass

            def close(self):
                pass

        error = sqlite3.OperationalError("database is locked; /private/path secret")
        with patch("app.database._connect", return_value=Connection()), patch.object(
            db.logger, "warning"
        ) as warning:
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                with db.get_conn():
                    raise error

        self.assertIs(ctx.exception, error)
        metrics = db.get_sqlite_contention_metrics()
        self.assertEqual(metrics["total"], 1)
        self.assertEqual(metrics["locked"], 1)
        self.assertEqual(metrics["operation"], 1)
        rendered = " ".join(str(value) for value in warning.call_args.args)
        self.assertNotIn("/private/path", rendered)
        self.assertNotIn("secret", rendered)

    def test_commit_contention_uses_commit_phase_and_keeps_rollback(self):
        import sqlite3

        class Connection:
            rolled_back = False

            def commit(self):
                raise sqlite3.OperationalError("database is busy")

            def rollback(self):
                self.rolled_back = True

            def close(self):
                pass

        conn = Connection()
        with patch("app.database._connect", return_value=conn):
            with self.assertRaises(sqlite3.OperationalError):
                with db.get_conn():
                    pass
        self.assertTrue(conn.rolled_back)
        metrics = db.get_sqlite_contention_metrics()
        self.assertEqual(metrics["busy"], 1)
        self.assertEqual(metrics["commit"], 1)

    def test_non_contention_error_does_not_change_metrics_or_emit_warning(self):
        import sqlite3

        with patch.object(db.logger, "warning") as warning:
            db._observe_sqlite_contention(
                sqlite3.OperationalError("no such table: missing"),
                phase="operation",
            )
        self.assertEqual(db.get_sqlite_contention_metrics()["total"], 0)
        warning.assert_not_called()

    def test_metrics_snapshot_is_a_copy(self):
        snapshot = db.get_sqlite_contention_metrics()
        snapshot["total"] = 99
        self.assertEqual(db.get_sqlite_contention_metrics()["total"], 0)


class SQLiteConnectCleanupTests(unittest.TestCase):
    def test_connect_closes_partially_initialized_connection_on_pragma_failure(self):
        import sqlite3

        error = sqlite3.OperationalError("database is locked")

        class Connection:
            row_factory = None
            closed = False

            def execute(self, _statement):
                raise error

            def close(self):
                self.closed = True

        conn = Connection()
        with (
            patch("app.database.resolve_db_path", return_value=Path("/tmp/mediaflux-test.db")),
            patch("app.database.sqlite3.connect", return_value=conn),
            patch("app.database._protect_database_files"),
        ):
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                db._connect()

        self.assertIs(ctx.exception, error)
        self.assertTrue(conn.closed)
