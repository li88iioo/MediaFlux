"""数据层拆分后仍使用单一连接入口、迁移登记表和跨表事务。"""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from app import database as db
from app import database_migrations, database_schema
from app.repositories import local_media
from tests.support import IsolatedDatabaseTestCase, isolated_test_database


class DatabaseModuleImportTests(unittest.TestCase):
    def test_facade_exports_the_single_local_media_implementation(self):
        connection_helpers = {
            "_recover_after_restart",  # 启动 hook 接收既有事务，不属于 CRUD 门面API。
        }
        for name, function in inspect.getmembers(local_media, inspect.isfunction):
            if function.__module__ != local_media.__name__ or name in connection_helpers:
                continue
            with self.subTest(name=name):
                self.assertIs(getattr(db, name), function)
        self.assertIs(
            db._LOCAL_MEDIA_TERMINAL_TASK_STATUSES,
            local_media._LOCAL_MEDIA_TERMINAL_TASK_STATUSES,
        )

    def test_unused_single_target_delete_is_not_exported(self):
        self.assertFalse(hasattr(local_media, "delete_local_library_target"))
        self.assertFalse(hasattr(db, "delete_local_library_target"))

    def test_schema_and_migration_registry_have_one_owner(self):
        self.assertIs(db._SCHEMA, database_schema._SCHEMA)
        migrations = database_migrations._SCHEMA_MIGRATIONS
        self.assertEqual(sorted(migrations), list(range(1, db.SCHEMA_VERSION)))
        removed_exports = {
            "_SCHEMA_MIGRATIONS",
            "_restore_interrupted_agent_session_context_v2",
            "_LegacyTelegramHTMLTextExtractor",
            "_legacy_organize_notification_event",
            "_legacy_rss_download_identity",
            *(function.__name__ for function in migrations.values()),
        }
        for name in removed_exports:
            with self.subTest(name=name):
                self.assertFalse(hasattr(db, name))
        for function in migrations.values():
            with self.subTest(migration=function.__name__):
                self.assertEqual(function.__module__, database_migrations.__name__)

    def test_coordinator_uses_canonical_registry_and_rolls_back_migration_failure(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA user_version=1")

            def fail_migration(connection):
                connection.execute("CREATE TABLE rollback_probe(id INTEGER)")
                raise RuntimeError("canonical migration failure")

            canonical_registry = {1: fail_migration}
            with patch.object(
                database_migrations,
                "_SCHEMA_MIGRATIONS",
                canonical_registry,
            ), patch.object(db, "SCHEMA_VERSION", 2), patch.object(
                db, "_create_pre_migration_backup"
            ):
                with self.assertRaisesRegex(RuntimeError, "canonical migration failure"):
                    db._prepare_schema_migration(conn, database_existed=True)

            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                1,
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='rollback_probe'"
                ).fetchone()
            )
        finally:
            conn.close()

    def test_import_order_never_opens_a_database_or_creates_a_cycle(self):
        orders = (
            ("app.database", "app.database_migrations", "app.repositories.local_media"),
            ("app.repositories.local_media", "app.database_migrations", "app.database"),
            ("app.database_migrations", "app.database_schema", "app.database"),
        )
        orders += tuple(
            (f"app.repositories.{name}", "app.database")
            for name in ("agent_download_verification", "agent_jobs", "agent_library_patrol",
                         "agent_provider_plans", "download_requests", "media_proxy",
                         "organize_history", "rss", "strm", "telegram_notifications")
        )
        for order in orders:
            with self.subTest(order=order):
                code = f'''
from importlib import import_module
from unittest.mock import patch
with patch("sqlite3.connect", side_effect=AssertionError("import opened database")):
    for name in {order!r}:
        import_module(name)
from app import database
from app.repositories import local_media
assert database.create_local_media_task is local_media.create_local_media_task
'''
                completed = subprocess.run(
                    [sys.executable, "-c", code],
                    cwd=Path(__file__).resolve().parents[1],
                    env={**os.environ, "MEDIAFLUX_DISABLE_FILE_LOGGING": "1"},
                    capture_output=True, text=True, timeout=30, check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)


class LocalMediaRepositoryBoundaryTests(IsolatedDatabaseTestCase):
    def test_bundle_uses_facade_connection_and_clock_once(self):
        timestamp = "2001-02-03 04:05:06"
        with patch.object(db, "get_conn", wraps=db.get_conn) as connection, patch.object(
            db, "now", return_value=timestamp,
        ) as clock:
            source_id = local_media.save_local_media_source_bundle(
                name="single-transaction", qb_profile="", qb_path_prefix="",
                local_root="/downloads/single-transaction", enabled=True,
                media_type="auto", mode="move", owner="boundary-connection",
                targets=[{"category": "movie", "path": "/library/movies"}],
            )
        self.assertEqual(connection.call_count, 1)
        self.assertEqual(clock.call_count, 1)
        with db.get_conn() as conn:
            source = conn.execute(
                "SELECT created_at,updated_at FROM local_media_sources WHERE id=?",
                (source_id,),
            ).fetchone()
            target = conn.execute(
                "SELECT created_at,updated_at FROM local_library_targets WHERE source_id=?",
                (source_id,),
            ).fetchone()
        self.assertEqual(tuple(source), (timestamp, timestamp))
        self.assertEqual(tuple(target), (timestamp, timestamp))
        with patch.object(db, "now", return_value=timestamp):
            self.assertEqual(database_migrations.now(), timestamp)

    def test_target_insert_failure_rolls_back_source_and_prior_targets(self):
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER boundary_reject_tv BEFORE INSERT ON local_library_targets "
                "WHEN NEW.category='tv' BEGIN "
                "SELECT RAISE(ABORT, 'boundary target failure'); END"
            )
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "boundary target failure"):
                local_media.save_local_media_source_bundle(
                    name="rollback-source", qb_profile="", qb_path_prefix="",
                    local_root="/downloads/rollback-source", enabled=True,
                    media_type="auto", mode="move", owner="boundary-rollback",
                    targets=[
                        {"category": "movie", "path": "/library/movies"},
                        {"category": "tv", "path": "/library/tv"},
                    ],
                )
            with db.get_conn() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM local_media_sources WHERE owner='boundary-rollback'",
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM local_library_targets WHERE owner='boundary-rollback'",
                ).fetchone()[0], 0)
        finally:
            with db.get_conn() as conn:
                conn.execute("DROP TRIGGER boundary_reject_tv")

    def test_repository_follows_database_reconfiguration_without_cached_connections(self):
        previous_path = db.resolve_db_path()
        with isolated_test_database("repository-switch.db") as path:
            self.assertNotEqual(path, previous_path)
            local_media.create_local_media_source(
                name="only-in-switched-database", qb_profile="", qb_path_prefix="",
                local_root="/downloads/switched", owner="boundary-switch",
            )
            self.assertEqual(len(db.list_local_media_sources(owner="boundary-switch")), 1)
        self.assertEqual(db.resolve_db_path(), previous_path)
        self.assertEqual(local_media.list_local_media_sources(owner="boundary-switch"), [])

    def test_repository_uses_the_canonical_owner_without_connection_shims(self):
        self.assertIs(local_media.db, db)
        for name in ("_database", "get_conn", "now", "is_interrupted_local_media_write_error"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(local_media, name))
