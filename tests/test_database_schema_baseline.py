"""首个正式 SQLite schema 基线契约。"""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from app import database as db
from app import runtime_paths as runtime_paths_module
from app.modules.backup import BackupError, restore_backup, verify_backup
from app.runtime_paths import RuntimePaths
from tests.support import IsolatedDatabaseTestCase


class DatabaseSchemaBaselineTests(IsolatedDatabaseTestCase):
    def test_fresh_database_contains_complete_v6_schema(self) -> None:
        with db.get_conn() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            task_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(local_media_tasks)")
            }
            playback_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(media_proxy_playback_records)")
            }
            mapping_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(media_external_ids)")
            }
            action_indexes = {
                str(row["name"])
                for row in conn.execute("PRAGMA index_list(agent_action_history)")
            }
            action_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(agent_action_history)")
            }
            rss_indexes = {
                str(row["name"]): int(row["unique"])
                for row in conn.execute("PRAGMA index_list(rss_entries)")
            }
            schema_objects = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                )
            }

        self.assertEqual(version, db.SCHEMA_VERSION)
        self.assertEqual(version, 6)
        self.assertIn("rules_snapshot", task_columns)
        self.assertIn("season_override", task_columns)
        self.assertIn("episode_override", task_columns)
        self.assertIn("session_id", playback_columns)
        self.assertIn("version", mapping_columns)
        self.assertIn("idx_agent_action_history_owner_id", action_indexes)
        self.assertIn("idx_agent_action_history_confirmation", action_indexes)
        self.assertIn("confirmation_id", action_columns)
        self.assertEqual(rss_indexes.get("idx_rss_entries_item_guid"), 1)
        self.assertEqual(rss_indexes.get("idx_rss_entries_failure_retry"), 0)
        self.assertTrue({
            "media_title_aliases",
            "idx_media_title_alias_lookup",
            "recognition_knowledge",
            "idx_recognition_knowledge_lookup",
            "idx_recognition_knowledge_source",
            "idx_media_probe_cache_fingerprint_updated",
            "agent_session_context_epochs",
            "agent_session_context_generation_sequence",
            "organize_operation_jobs",
            "idx_organize_operation_jobs_active_dedupe",
            "media_proxy_playback_sessions",
            "idx_media_proxy_records_session_id",
        }.issubset(schema_objects))

    def test_v014_release_schema_prebackup_restore_and_upgrade(self) -> None:
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "database"
            / "v0.1.4-schema.sql"
        )
        fixture_bytes = fixture.read_bytes()
        self.assertEqual(
            hashlib.sha256(fixture_bytes).hexdigest(),
            "025aaf682dc1ccb1cfe9733e928057727c76e54ce61898c89ed7b0c3577350e3",
        )

        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        previous_runtime_paths = runtime_paths_module._configured_paths

        def make_paths(root: Path) -> RuntimePaths:
            return RuntimePaths(
                program_dir=root / "program",
                data_dir=root / "data",
                config_dir=root / "data",
                cache_dir=root / "data" / "cache",
                log_dir=root / "data" / "logs",
                strm_dir=root / "strm",
                trash_dir=root / "data" / "trash",
            )

        def assert_upgraded(path: Path) -> None:
            connection = sqlite3.connect(path)
            try:
                connection.row_factory = sqlite3.Row
                self.assertEqual(
                    int(connection.execute("PRAGMA user_version").fetchone()[0]),
                    db.SCHEMA_VERSION,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM settings_kv WHERE key='release_fixture_sentinel'"
                    ).fetchone()[0],
                    "preserved",
                )
                self.assertEqual(
                    tuple(connection.execute(
                        "SELECT context_type,context_generation "
                        "FROM agent_session_context WHERE owner_digest='release-owner'"
                    ).fetchone()),
                    ("patrol", 0),
                )
                self.assertEqual(
                    tuple(connection.execute(
                        "SELECT summary,confirmation_id FROM agent_action_history "
                        "WHERE owner_digest=?",
                        ("a" * 64,),
                    ).fetchone()),
                    ("v0.1.4 sentinel", ""),
                )
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='idx_agent_action_history_confirmation'"
                ).fetchone())
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                connection.close()

        with tempfile.TemporaryDirectory(prefix="mediaflux-v014-upgrade-") as root:
            root_path = Path(root)
            source_paths = make_paths(root_path / "source")
            source_paths.ensure_writable_dirs()
            connection = sqlite3.connect(source_paths.database_path)
            try:
                connection.executescript(fixture_bytes.decode("utf-8"))
                connection.execute(
                    "INSERT INTO settings_kv(key,value,updated_at) VALUES(?,?,?)",
                    ("release_fixture_sentinel", "preserved", "2026-08-24"),
                )
                connection.execute(
                    "INSERT INTO agent_session_context("
                    "owner_digest,context_type,payload,expires_at,created_at"
                    ") VALUES(?,?,?,?,?)",
                    ("release-owner", "patrol", "{}", 9_999_999_999, "2026-08-24"),
                )
                connection.execute(
                    "INSERT INTO agent_action_history("
                    "owner_digest,tool_name,risk,status,ok,summary,started_at,finished_at"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "a" * 64,
                        "rss.refresh_subscription",
                        "write",
                        "completed",
                        1,
                        "v0.1.4 sentinel",
                        "2026-08-24",
                        "2026-08-24",
                    ),
                )
                connection.execute("PRAGMA user_version=1")
                connection.commit()
            finally:
                connection.close()

            runtime_paths_module.configure_runtime_paths(source_paths)
            db.configure_database(source_paths.database_path, test_mode=False)
            try:
                with mock.patch.object(db, "_test_mode_enabled", return_value=False):
                    db.init_db()
                assert_upgraded(source_paths.database_path)

                backups = list(source_paths.backup_dir.glob(
                    "mediaflux-*-pre-migration-1-to-6-*.zip"
                ))
                self.assertEqual(len(backups), 1)
                manifest = verify_backup(backups[0])
                self.assertEqual(manifest.payload["database_schema_version"], 1)

                restored_paths = make_paths(root_path / "restored")
                restored_paths.ensure_writable_dirs()
                restore_backup(restored_paths, backups[0])
                restored = sqlite3.connect(restored_paths.database_path)
                try:
                    self.assertEqual(
                        int(restored.execute("PRAGMA user_version").fetchone()[0]),
                        1,
                    )
                    self.assertEqual(
                        restored.execute(
                            "SELECT value FROM settings_kv "
                            "WHERE key='release_fixture_sentinel'"
                        ).fetchone()[0],
                        "preserved",
                    )
                finally:
                    restored.close()

                runtime_paths_module.configure_runtime_paths(restored_paths)
                db.configure_database(restored_paths.database_path, test_mode=False)
                with mock.patch.object(db, "_test_mode_enabled", return_value=False):
                    db.init_db()
                assert_upgraded(restored_paths.database_path)
            finally:
                runtime_paths_module.configure_runtime_paths(previous_runtime_paths)
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_v1_agent_session_context_migrates_without_losing_rows(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-agent-v2-") as root:
            path = Path(root) / "v1.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    "CREATE TABLE agent_session_context ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "owner_digest TEXT NOT NULL,"
                    "context_type TEXT NOT NULL "
                    "CHECK(context_type IN ('patrol','download_submission')),"
                    "payload TEXT NOT NULL,"
                    "expires_at REAL NOT NULL,"
                    "created_at TEXT NOT NULL"
                    ");"
                    "CREATE INDEX idx_agent_session_context_lookup "
                    "ON agent_session_context(owner_digest,context_type,expires_at,id DESC);"
                    "CREATE INDEX idx_agent_session_context_expiry "
                    "ON agent_session_context(expires_at);"
                )
                conn.execute(
                    "INSERT INTO agent_session_context("
                    "owner_digest,context_type,payload,expires_at,created_at"
                    ") VALUES('digest','patrol','{}',9999999999,'2026-08-01')"
                )
                conn.execute("PRAGMA user_version=1")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                db.init_db()
                with db.get_conn() as migrated:
                    version = int(migrated.execute("PRAGMA user_version").fetchone()[0])
                    preserved = migrated.execute(
                        "SELECT context_type FROM agent_session_context "
                        "WHERE owner_digest='digest'"
                    ).fetchone()
                    migrated.execute(
                        "INSERT INTO agent_session_context("
                        "owner_digest,context_type,payload,expires_at,created_at"
                        ") VALUES('digest','resource_candidates','{}',9999999999,'2026-08-01')"
                    )
                self.assertEqual(version, 6)
                self.assertEqual(preserved["context_type"], "patrol")
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_pre_migration_backup_failure_leaves_v1_database_untouched(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-backup-failure-") as root:
            path = Path(root) / "v1.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    "CREATE TABLE agent_session_context ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "owner_digest TEXT NOT NULL,"
                    "context_type TEXT NOT NULL "
                    "CHECK(context_type IN ('patrol','download_submission')),"
                    "payload TEXT NOT NULL,"
                    "expires_at REAL NOT NULL,"
                    "created_at TEXT NOT NULL"
                    ");"
                    "CREATE INDEX idx_agent_session_context_lookup "
                    "ON agent_session_context(owner_digest,context_type,expires_at,id DESC);"
                    "CREATE INDEX idx_agent_session_context_expiry "
                    "ON agent_session_context(expires_at);"
                )
                conn.execute(
                    "INSERT INTO agent_session_context("
                    "owner_digest,context_type,payload,expires_at,created_at"
                    ") VALUES('preserved','patrol','{}',9999999999,'2026-08-01')"
                )
                conn.execute("PRAGMA user_version=1")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            backup = mock.Mock(side_effect=BackupError("injected backup failure"))
            try:
                with (
                    mock.patch.object(db, "_test_mode_enabled", return_value=False),
                    mock.patch("app.modules.backup.create_backup", backup),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "数据库迁移前备份失败",
                    ):
                        db.init_db()
                conn = sqlite3.connect(path)
                try:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    table_sql = str(
                        conn.execute(
                            "SELECT sql FROM sqlite_master WHERE type='table' "
                            "AND name='agent_session_context'"
                        ).fetchone()[0]
                    ).casefold()
                    preserved = conn.execute(
                        "SELECT owner_digest FROM agent_session_context"
                    ).fetchall()
                    legacy_table = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='agent_session_context_v1'"
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual(version, 1)
                self.assertIn("check", table_sql)
                self.assertEqual(preserved, [("preserved",)])
                self.assertIsNone(legacy_table)
                backup.assert_called_once()
                kwargs = backup.call_args.kwargs
                self.assertEqual(kwargs["reason"], "pre-migration-1-to-6")
                self.assertFalse(kwargs["include_settings"])
                self.assertEqual(Path(kwargs["output"]).parent, path.parent / "backups")
                self.assertIsInstance(kwargs["source_connection"], sqlite3.Connection)
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_v2_generation_conflict_is_rejected_without_resetting_fence(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-agent-v2-generation-") as root:
            path = Path(root) / "v1-generation-conflict.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    "CREATE TABLE agent_session_context ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "owner_digest TEXT NOT NULL,"
                    "context_type TEXT NOT NULL "
                    "CHECK(context_type IN ('patrol','download_submission')),"
                    "payload TEXT NOT NULL,"
                    "expires_at REAL NOT NULL,"
                    "created_at TEXT NOT NULL"
                    ");"
                    "CREATE INDEX idx_agent_session_context_lookup "
                    "ON agent_session_context(owner_digest,context_type,expires_at,id DESC);"
                    "CREATE INDEX idx_agent_session_context_expiry "
                    "ON agent_session_context(expires_at);"
                )
                conn.execute(
                    "INSERT INTO agent_session_context("
                    "owner_digest,context_type,payload,expires_at,created_at"
                    ") VALUES('same-row','patrol','{}',9999999999,'2026-08-01')"
                )
                conn.execute("PRAGMA user_version=1")
                conn.commit()
                conn.execute(
                    "ALTER TABLE agent_session_context "
                    "RENAME TO agent_session_context_v1"
                )
                conn.execute(
                    "CREATE TABLE agent_session_context ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "owner_digest TEXT NOT NULL,"
                    "context_type TEXT NOT NULL,"
                    "payload TEXT NOT NULL,"
                    "expires_at REAL NOT NULL,"
                    "context_generation INTEGER NOT NULL DEFAULT 0,"
                    "created_at TEXT NOT NULL"
                    ")"
                )
                conn.execute(
                    "INSERT INTO agent_session_context("
                    "id,owner_digest,context_type,payload,expires_at,"
                    "context_generation,created_at"
                    ") VALUES(1,'same-row','patrol','{}',9999999999,9,'2026-08-01')"
                )
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                with self.assertRaisesRegex(RuntimeError, "拒绝自动覆盖"):
                    db.init_db()
                conn = sqlite3.connect(path)
                try:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    generation = int(
                        conn.execute(
                            "SELECT context_generation FROM agent_session_context WHERE id=1"
                        ).fetchone()[0]
                    )
                    legacy_owner = str(
                        conn.execute(
                            "SELECT owner_digest FROM agent_session_context_v1 WHERE id=1"
                        ).fetchone()[0]
                    )
                finally:
                    conn.close()
                self.assertEqual(version, 1)
                self.assertEqual(generation, 9)
                self.assertEqual(legacy_owner, "same-row")
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_interrupted_v2_rename_is_recovered_without_losing_rows(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-agent-v2-recover-") as root:
            path = Path(root) / "v1-interrupted.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    "CREATE TABLE agent_session_context ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "owner_digest TEXT NOT NULL,"
                    "context_type TEXT NOT NULL "
                    "CHECK(context_type IN ('patrol','download_submission')),"
                    "payload TEXT NOT NULL,"
                    "expires_at REAL NOT NULL,"
                    "created_at TEXT NOT NULL"
                    ");"
                    "CREATE INDEX idx_agent_session_context_lookup "
                    "ON agent_session_context(owner_digest,context_type,expires_at,id DESC);"
                    "CREATE INDEX idx_agent_session_context_expiry "
                    "ON agent_session_context(expires_at);"
                )
                conn.execute(
                    "INSERT INTO agent_session_context("
                    "owner_digest,context_type,payload,expires_at,created_at"
                    ") VALUES('preserved','patrol','{}',9999999999,'2026-08-01')"
                )
                conn.execute("PRAGMA user_version=1")
                conn.commit()
                # 模拟旧版迁移在首条非事务 DDL 后退出。
                conn.execute(
                    "ALTER TABLE agent_session_context "
                    "RENAME TO agent_session_context_v1"
                )
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                db.init_db()
                with db.get_conn() as migrated:
                    version = int(migrated.execute("PRAGMA user_version").fetchone()[0])
                    preserved = migrated.execute(
                        "SELECT context_type,context_generation "
                        "FROM agent_session_context WHERE owner_digest='preserved'"
                    ).fetchone()
                    legacy_table = migrated.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='agent_session_context_v1'"
                    ).fetchone()
                self.assertEqual(version, db.SCHEMA_VERSION)
                self.assertEqual(preserved["context_type"], "patrol")
                self.assertEqual(int(preserved["context_generation"]), 0)
                self.assertIsNone(legacy_table)
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_interrupted_v2_copy_restarts_from_complete_legacy_table(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-agent-v2-partial-") as root:
            path = Path(root) / "v1-partial-copy.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    "CREATE TABLE agent_session_context ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "owner_digest TEXT NOT NULL,"
                    "context_type TEXT NOT NULL "
                    "CHECK(context_type IN ('patrol','download_submission')),"
                    "payload TEXT NOT NULL,"
                    "expires_at REAL NOT NULL,"
                    "created_at TEXT NOT NULL"
                    ");"
                    "CREATE INDEX idx_agent_session_context_lookup "
                    "ON agent_session_context(owner_digest,context_type,expires_at,id DESC);"
                    "CREATE INDEX idx_agent_session_context_expiry "
                    "ON agent_session_context(expires_at);"
                )
                conn.execute(
                    "INSERT INTO agent_session_context("
                    "owner_digest,context_type,payload,expires_at,created_at"
                    ") VALUES('authoritative','patrol','{}',9999999999,'2026-08-01')"
                )
                conn.execute("PRAGMA user_version=1")
                conn.commit()
                conn.execute(
                    "ALTER TABLE agent_session_context "
                    "RENAME TO agent_session_context_v1"
                )
                conn.execute(
                    "CREATE TABLE agent_session_context ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "owner_digest TEXT NOT NULL,"
                    "context_type TEXT NOT NULL,"
                    "payload TEXT NOT NULL,"
                    "expires_at REAL NOT NULL,"
                    "context_generation INTEGER NOT NULL DEFAULT 0,"
                    "created_at TEXT NOT NULL"
                    ")"
                )
                conn.execute(
                    "INSERT INTO agent_session_context("
                    "id,owner_digest,context_type,payload,expires_at,created_at"
                    ") VALUES(1,'authoritative','patrol','{}',9999999999,'2026-08-01')"
                )
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                db.init_db()
                with db.get_conn() as migrated:
                    owners = {
                        str(row["owner_digest"])
                        for row in migrated.execute(
                            "SELECT owner_digest FROM agent_session_context"
                        )
                    }
                    legacy_table = migrated.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='agent_session_context_v1'"
                    ).fetchone()
                self.assertEqual(owners, {"authoritative"})
                self.assertIsNone(legacy_table)
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_ambiguous_v2_double_table_state_is_rejected_without_data_loss(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-agent-v2-ambiguous-") as root:
            path = Path(root) / "v1-ambiguous.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    "CREATE TABLE agent_session_context ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "owner_digest TEXT NOT NULL,"
                    "context_type TEXT NOT NULL "
                    "CHECK(context_type IN ('patrol','download_submission')),"
                    "payload TEXT NOT NULL,"
                    "expires_at REAL NOT NULL,"
                    "created_at TEXT NOT NULL"
                    ");"
                    "CREATE INDEX idx_agent_session_context_lookup "
                    "ON agent_session_context(owner_digest,context_type,expires_at,id DESC);"
                    "CREATE INDEX idx_agent_session_context_expiry "
                    "ON agent_session_context(expires_at);"
                )
                conn.execute(
                    "INSERT INTO agent_session_context("
                    "owner_digest,context_type,payload,expires_at,created_at"
                    ") VALUES('legacy-row','patrol','{}',9999999999,'2026-08-01')"
                )
                conn.execute("PRAGMA user_version=1")
                conn.commit()
                conn.execute(
                    "ALTER TABLE agent_session_context "
                    "RENAME TO agent_session_context_v1"
                )
                conn.execute(
                    "CREATE TABLE agent_session_context ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "owner_digest TEXT NOT NULL,"
                    "context_type TEXT NOT NULL,"
                    "payload TEXT NOT NULL,"
                    "expires_at REAL NOT NULL,"
                    "context_generation INTEGER NOT NULL DEFAULT 0,"
                    "created_at TEXT NOT NULL"
                    ")"
                )
                conn.execute(
                    "INSERT INTO agent_session_context("
                    "owner_digest,context_type,payload,expires_at,created_at"
                    ") VALUES('new-row','patrol','{}',9999999999,'2026-08-02')"
                )
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                with self.assertRaisesRegex(RuntimeError, "拒绝自动覆盖"):
                    db.init_db()
                conn = sqlite3.connect(path)
                try:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    legacy_owners = conn.execute(
                        "SELECT owner_digest FROM agent_session_context_v1"
                    ).fetchall()
                    current_owners = conn.execute(
                        "SELECT owner_digest FROM agent_session_context"
                    ).fetchall()
                finally:
                    conn.close()
                self.assertEqual(version, 1)
                self.assertEqual(legacy_owners, [("legacy-row",)])
                self.assertEqual(current_owners, [("new-row",)])
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_v2_database_migrates_context_generation_and_epochs(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-agent-v3-") as root:
            path = Path(root) / "v2.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    "CREATE TABLE agent_session_context ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "owner_digest TEXT NOT NULL,"
                    "context_type TEXT NOT NULL,"
                    "payload TEXT NOT NULL,"
                    "expires_at REAL NOT NULL,"
                    "created_at TEXT NOT NULL"
                    ");"
                    "CREATE INDEX idx_agent_session_context_lookup "
                    "ON agent_session_context(owner_digest,context_type,expires_at,id DESC);"
                    "CREATE INDEX idx_agent_session_context_expiry "
                    "ON agent_session_context(expires_at);"
                )
                conn.execute(
                    "INSERT INTO agent_session_context("
                    "owner_digest,context_type,payload,expires_at,created_at"
                    ") VALUES('digest','patrol','{}',9999999999,'2026-08-01')"
                )
                conn.execute("PRAGMA user_version=2")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                db.init_db()
                with db.get_conn() as migrated:
                    version = int(migrated.execute("PRAGMA user_version").fetchone()[0])
                    columns = {
                        str(row["name"])
                        for row in migrated.execute(
                            "PRAGMA table_info(agent_session_context)"
                        )
                    }
                    preserved = migrated.execute(
                        "SELECT context_type,context_generation "
                        "FROM agent_session_context WHERE owner_digest='digest'"
                    ).fetchone()
                    epoch_table = migrated.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='agent_session_context_epochs'"
                    ).fetchone()
                    generation_sequence = migrated.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='agent_session_context_generation_sequence'"
                    ).fetchone()
                self.assertEqual(version, 6)
                self.assertIn("context_generation", columns)
                self.assertEqual(preserved["context_type"], "patrol")
                self.assertEqual(int(preserved["context_generation"]), 0)
                self.assertIsNotNone(epoch_table)
                self.assertIsNotNone(generation_sequence)
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_schema_initialization_is_idempotent(self) -> None:
        db.init_db()
        db.init_db()
        with db.get_conn() as conn:
            self.assertEqual(
                int(conn.execute("PRAGMA user_version").fetchone()[0]),
                db.SCHEMA_VERSION,
            )

    def test_builtin_recognition_lookup_does_not_lazy_create_schema(self) -> None:
        from app.modules import recognition_knowledge as knowledge

        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-recognition-readonly-") as root:
            path = Path(root) / "uninitialized.db"
            db.configure_database(path, test_mode=True)
            knowledge.reset_runtime_state_for_tests()
            try:
                self.assertTrue(knowledge.is_known("ANi", "release_group"))
                self.assertEqual(
                    knowledge.lookup_any("Nekomoe", "release_group")["source"],
                    "builtin",
                )
                self.assertFalse(knowledge.is_known("NotAReleaseGroup", "release_group"))
                conn = sqlite3.connect(path)
                try:
                    tables = {
                        str(row[0])
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                finally:
                    conn.close()
                self.assertNotIn("recognition_knowledge", tables)
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)
                knowledge.reset_runtime_state_for_tests()

    def test_v3_database_migrates_hardened_durable_organize_operation_queue(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-organize-v4-") as root:
            path = Path(root) / "v3.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE task_runs(id INTEGER PRIMARY KEY)")
                conn.execute("PRAGMA user_version=3")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                db.init_db()
                with db.get_conn() as migrated:
                    version = int(migrated.execute("PRAGMA user_version").fetchone()[0])
                    table = migrated.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type='table' AND name='organize_operation_jobs'"
                    ).fetchone()
                    indexes = {
                        str(row["name"])
                        for row in migrated.execute(
                            "PRAGMA index_list(organize_operation_jobs)"
                        )
                    }
                    columns = {
                        str(row["name"])
                        for row in migrated.execute(
                            "PRAGMA table_info(organize_operation_jobs)"
                        )
                    }
                self.assertEqual(version, 6)
                self.assertIsNotNone(table)
                self.assertIn("idx_organize_operation_jobs_active_dedupe", indexes)
                self.assertTrue({
                    "payload_auth", "cancel_requested", "expires_at", "purged_at"
                }.issubset(columns))
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_v4_queue_migrates_existing_rows_to_v5_safely(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-organize-v5-") as root:
            path = Path(root) / "v4.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    "CREATE TABLE organize_operation_jobs ("
                    "job_id TEXT PRIMARY KEY,job_kind TEXT NOT NULL,owner_digest TEXT NOT NULL,"
                    "operation TEXT NOT NULL,reference TEXT NOT NULL DEFAULT '',"
                    "payload_json TEXT NOT NULL DEFAULT '{}',dedupe_digest TEXT NOT NULL,"
                    "status TEXT NOT NULL,lease_generation INTEGER NOT NULL DEFAULT 0,"
                    "result_json TEXT NOT NULL DEFAULT '{}',error_code TEXT NOT NULL DEFAULT '',"
                    "error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
                    "started_at TEXT,finished_at TEXT);"
                    "CREATE UNIQUE INDEX idx_organize_operation_jobs_active_dedupe "
                    "ON organize_operation_jobs(dedupe_digest) "
                    "WHERE status IN ('pending','running');"
                )
                rows = [
                    ("a" * 32, "pending", "payload-pending", "1" * 64),
                    ("b" * 32, "running", "payload-running", "2" * 64),
                    ("c" * 32, "completed", "payload-completed", "3" * 64),
                ]
                for job_id, status, payload, digest in rows:
                    conn.execute(
                        "INSERT INTO organize_operation_jobs("
                        "job_id,job_kind,owner_digest,operation,reference,payload_json,"
                        "dedupe_digest,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (job_id, "agent_directory_scrape", "d" * 64, "目录刮削", "ref", payload,
                         digest, status, "2026-08-01", "2026-08-01"),
                    )
                conn.execute("PRAGMA user_version=4")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                db.init_db()
                with db.get_conn() as migrated:
                    version = int(migrated.execute("PRAGMA user_version").fetchone()[0])
                    result = {
                        str(row["job_id"]): dict(row)
                        for row in migrated.execute(
                            "SELECT * FROM organize_operation_jobs ORDER BY job_id"
                        )
                    }
                    columns = {
                        str(row["name"])
                        for row in migrated.execute("PRAGMA table_info(organize_operation_jobs)")
                    }
                self.assertEqual(version, 6)
                self.assertTrue({"payload_auth", "cancel_requested", "expires_at", "purged_at"}.issubset(columns))
                self.assertEqual(result["a" * 32]["status"], "cancelled")
                self.assertEqual(result["a" * 32]["payload_json"], "{}")
                self.assertEqual(result["b" * 32]["status"], "running")
                self.assertEqual(result["c" * 32]["status"], "completed")
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_v5_action_history_gains_confirmation_execution_identity(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-agent-history-v6-") as root:
            path = Path(root) / "v5.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    "CREATE TABLE agent_action_history ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "owner_digest TEXT NOT NULL DEFAULT '',"
                    "tool_name TEXT NOT NULL,risk TEXT NOT NULL,status TEXT NOT NULL,"
                    "ok INTEGER NOT NULL DEFAULT 0,"
                    "mode TEXT NOT NULL DEFAULT 'confirmed_action',"
                    "summary TEXT NOT NULL,safe_details TEXT NOT NULL DEFAULT '{}',"
                    "error_code TEXT NOT NULL DEFAULT '',elapsed_ms INTEGER NOT NULL DEFAULT 0,"
                    "started_at TEXT NOT NULL,finished_at TEXT NOT NULL"
                    ");"
                )
                conn.execute(
                    "INSERT INTO agent_action_history("
                    "owner_digest,tool_name,risk,status,summary,started_at,finished_at"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        "a" * 64,
                        "rss.refresh_subscription",
                        "write",
                        "completed",
                        "历史记录",
                        "2026-08-01",
                        "2026-08-01",
                    ),
                )
                conn.execute("PRAGMA user_version=5")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                db.init_db()
                with db.get_conn() as migrated:
                    version = int(migrated.execute("PRAGMA user_version").fetchone()[0])
                    columns = {
                        str(row["name"])
                        for row in migrated.execute(
                            "PRAGMA table_info(agent_action_history)"
                        )
                    }
                    indexes = {
                        str(row["name"])
                        for row in migrated.execute(
                            "PRAGMA index_list(agent_action_history)"
                        )
                    }
                    preserved = migrated.execute(
                        "SELECT summary,confirmation_id FROM agent_action_history"
                    ).fetchone()
                self.assertEqual(version, 6)
                self.assertIn("confirmation_id", columns)
                self.assertIn("idx_agent_action_history_confirmation", indexes)
                self.assertEqual(tuple(preserved), ("历史记录", ""))
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_precreated_empty_database_is_treated_as_fresh_install(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-empty-") as root:
            path = Path(root) / "empty.db"
            path.touch()
            db.configure_database(path, test_mode=True)
            try:
                with (
                    mock.patch.object(db, "_test_mode_enabled", return_value=False),
                    mock.patch("app.modules.backup.create_backup") as backup,
                ):
                    db.init_db()
                with db.get_conn() as conn:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual(version, db.SCHEMA_VERSION)
                backup.assert_not_called()
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_unversioned_legacy_backup_failure_stops_before_baseline_changes(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-v0-backup-failure-") as root:
            path = Path(root) / "legacy.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE legacy_data(id INTEGER PRIMARY KEY,value TEXT NOT NULL)"
                )
                conn.execute("INSERT INTO legacy_data(id,value) VALUES(1,'preserved')")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            backup = mock.Mock(side_effect=BackupError("injected backup failure"))
            try:
                with (
                    mock.patch.object(db, "_test_mode_enabled", return_value=False),
                    mock.patch("app.modules.backup.create_backup", backup),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "数据库迁移前备份失败",
                    ):
                        db.init_db()
                conn = sqlite3.connect(path)
                try:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    tables = {
                        str(row[0])
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    value = str(
                        conn.execute("SELECT value FROM legacy_data WHERE id=1").fetchone()[0]
                    )
                finally:
                    conn.close()
                self.assertEqual(version, 0)
                self.assertEqual(tables, {"legacy_data"})
                self.assertEqual(value, "preserved")
                backup.assert_called_once()
                self.assertEqual(
                    backup.call_args.kwargs["reason"],
                    "pre-migration-0-to-6",
                )
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_unversioned_legacy_database_is_safely_baselined_to_v1(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-v1-") as root:
            path = Path(root) / "legacy.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE legacy_data(id INTEGER PRIMARY KEY)")
                # 模拟历史开发库中存在缺少部分字段的表
                conn.execute(
                    "CREATE TABLE agent_action_history ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "tool_name TEXT NOT NULL, "
                    "risk TEXT NOT NULL, "
                    "status TEXT NOT NULL, "
                    "ok INTEGER NOT NULL DEFAULT 1, "
                    "mode TEXT NOT NULL, "
                    "summary TEXT NOT NULL, "
                    "safe_details TEXT NOT NULL DEFAULT '', "
                    "error_code TEXT NOT NULL DEFAULT '', "
                    "elapsed_ms INTEGER NOT NULL DEFAULT 0, "
                    "started_at TEXT NOT NULL, "
                    "finished_at TEXT NOT NULL"
                    ")"
                )
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                db.init_db()
                with db.get_conn() as conn:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    cols = {
                        str(r["name"])
                        for r in conn.execute("PRAGMA table_info(agent_action_history)")
                    }
                self.assertEqual(version, db.SCHEMA_VERSION)
                self.assertIn("owner_digest", cols)
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_existing_v1_playback_records_gain_nullable_session_link_without_data_loss(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-playback-session-schema-") as root:
            path = Path(root) / "v1-playback.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE media_proxy_playback_records ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "instance_id INTEGER NOT NULL,"
                    "route_class TEXT NOT NULL,"
                    "method TEXT NOT NULL,"
                    "status_code INTEGER NOT NULL DEFAULT 0,"
                    "source TEXT NOT NULL DEFAULT 'upstream',"
                    "cache_hit INTEGER NOT NULL DEFAULT 0,"
                    "upstream_latency_ms INTEGER NOT NULL DEFAULT 0,"
                    "total_latency_ms INTEGER NOT NULL DEFAULT 0,"
                    "failure_stage TEXT DEFAULT '',"
                    "error TEXT DEFAULT '',"
                    "created_at TEXT NOT NULL"
                    ")"
                )
                conn.execute(
                    "INSERT INTO media_proxy_playback_records("
                    "instance_id,route_class,method,status_code,source,created_at"
                    ") VALUES(7,'stream','GET',206,'upstream',datetime('now','localtime'))"
                )
                conn.execute("PRAGMA user_version=1")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                db.init_db()
                with db.get_conn() as conn:
                    columns = {
                        str(row["name"])
                        for row in conn.execute(
                            "PRAGMA table_info(media_proxy_playback_records)"
                        )
                    }
                    row = conn.execute(
                        "SELECT session_id FROM media_proxy_playback_records WHERE id=1"
                    ).fetchone()
                    session_table = conn.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='media_proxy_playback_sessions'"
                    ).fetchone()
                self.assertIn("session_id", columns)
                self.assertIsNone(row["session_id"])
                self.assertIsNotNone(session_table)
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_existing_v1_playback_sessions_gain_media_name_without_data_loss(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-playback-name-schema-") as root:
            path = Path(root) / "v1-playback-name.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE media_proxy_playback_sessions ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "instance_id INTEGER NOT NULL,"
                    "session_key TEXT NOT NULL,"
                    "media_item_id TEXT NOT NULL DEFAULT '',"
                    "media_source_id TEXT NOT NULL DEFAULT '',"
                    "guangya_file_id TEXT NOT NULL DEFAULT '',"
                    "request_count INTEGER NOT NULL DEFAULT 0,"
                    "success_count INTEGER NOT NULL DEFAULT 0,"
                    "error_count INTEGER NOT NULL DEFAULT 0,"
                    "cache_hit_count INTEGER NOT NULL DEFAULT 0,"
                    "cache_miss_count INTEGER NOT NULL DEFAULT 0,"
                    "upstream_latency_ms_total INTEGER NOT NULL DEFAULT 0,"
                    "total_latency_ms_total INTEGER NOT NULL DEFAULT 0,"
                    "max_total_latency_ms INTEGER NOT NULL DEFAULT 0,"
                    "last_route_class TEXT NOT NULL DEFAULT '',"
                    "last_source TEXT NOT NULL DEFAULT 'unknown',"
                    "last_status_code INTEGER NOT NULL DEFAULT 0,"
                    "last_failure_stage TEXT NOT NULL DEFAULT '',"
                    "last_error TEXT NOT NULL DEFAULT '',"
                    "started_at TEXT NOT NULL,"
                    "last_request_at TEXT NOT NULL,"
                    "UNIQUE(instance_id, session_key)"
                    ")"
                )
                conn.execute(
                    "CREATE TABLE media_proxy_playback_records ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "instance_id INTEGER NOT NULL,"
                    "session_id INTEGER,"
                    "route_class TEXT NOT NULL,"
                    "method TEXT NOT NULL,"
                    "status_code INTEGER NOT NULL DEFAULT 0,"
                    "source TEXT NOT NULL DEFAULT 'upstream',"
                    "cache_hit INTEGER NOT NULL DEFAULT 0,"
                    "upstream_latency_ms INTEGER NOT NULL DEFAULT 0,"
                    "total_latency_ms INTEGER NOT NULL DEFAULT 0,"
                    "failure_stage TEXT DEFAULT '',"
                    "error TEXT DEFAULT '',"
                    "created_at TEXT NOT NULL"
                    ")"
                )
                conn.execute(
                    "INSERT INTO media_proxy_playback_sessions("
                    "instance_id,session_key,media_item_id,request_count,"
                    "started_at,last_request_at"
                    ") VALUES(7,'legacy-session','legacy-item',1,"
                    "datetime('now','localtime'),datetime('now','localtime'))"
                )
                conn.execute(
                    "INSERT INTO media_proxy_playback_records("
                    "instance_id,session_id,route_class,method,status_code,source,created_at"
                    ") VALUES(7,1,'stream','GET',206,'upstream',datetime('now','localtime'))"
                )
                conn.execute("PRAGMA user_version=1")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                db.init_db()
                with db.get_conn() as conn:
                    columns = {
                        str(row["name"])
                        for row in conn.execute(
                            "PRAGMA table_info(media_proxy_playback_sessions)"
                        )
                    }
                    media_name = conn.execute(
                        "SELECT media_name FROM media_proxy_playback_sessions WHERE id=1"
                    ).fetchone()["media_name"]
                sessions = db.list_media_proxy_playback_sessions(instance_id=7)
                self.assertIn("media_name", columns)
                self.assertEqual(media_name, "")
                self.assertEqual(sessions["total"], 1)
                self.assertEqual(sessions["items"][0]["media_item_id"], "legacy-item")
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_registered_future_migration_advances_schema_version(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        previous_migrations = dict(db._SCHEMA_MIGRATIONS)
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-upgrade-") as root:
            path = Path(root) / "v1.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE v1_data(id INTEGER PRIMARY KEY)")
                conn.execute("PRAGMA user_version=1")
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)

            def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
                conn.execute("ALTER TABLE v1_data ADD COLUMN label TEXT NOT NULL DEFAULT ''")

            try:
                db._SCHEMA_MIGRATIONS.clear()
                db._SCHEMA_MIGRATIONS[1] = migrate_v1_to_v2
                with mock.patch.object(db, "SCHEMA_VERSION", 2):
                    db.init_db()
                with db.get_conn() as conn:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    columns = {
                        str(row["name"])
                        for row in conn.execute("PRAGMA table_info(v1_data)")
                    }
                self.assertEqual(version, 2)
                self.assertIn("label", columns)
            finally:
                db._SCHEMA_MIGRATIONS.clear()
                db._SCHEMA_MIGRATIONS.update(previous_migrations)
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_failed_future_migration_rolls_back_schema_data_and_version(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        previous_migrations = dict(db._SCHEMA_MIGRATIONS)
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-upgrade-rollback-") as root:
            path = Path(root) / "v1.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE v1_data(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute("INSERT INTO v1_data(id,value) VALUES(1,'before')")
                conn.execute("PRAGMA user_version=1")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)

            def fail_mid_migration(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "ALTER TABLE v1_data ADD COLUMN label TEXT NOT NULL DEFAULT ''"
                )
                conn.execute("UPDATE v1_data SET value='after',label='partial'")
                raise sqlite3.OperationalError("injected migration failure")

            try:
                db._SCHEMA_MIGRATIONS.clear()
                db._SCHEMA_MIGRATIONS[1] = fail_mid_migration
                with mock.patch.object(db, "SCHEMA_VERSION", 2):
                    with self.assertRaisesRegex(
                        sqlite3.OperationalError,
                        "injected migration failure",
                    ):
                        db.init_db()
                conn = sqlite3.connect(path)
                try:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    columns = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(v1_data)")
                    }
                    value = str(
                        conn.execute("SELECT value FROM v1_data WHERE id=1").fetchone()[0]
                    )
                finally:
                    conn.close()
                self.assertEqual(version, 1)
                self.assertEqual(columns, {"id", "value"})
                self.assertEqual(value, "before")
            finally:
                db._SCHEMA_MIGRATIONS.clear()
                db._SCHEMA_MIGRATIONS.update(previous_migrations)
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_failed_later_migration_rolls_back_entire_upgrade_chain(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        previous_migrations = dict(db._SCHEMA_MIGRATIONS)
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-chain-rollback-") as root:
            path = Path(root) / "v1.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE v1_data(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute("INSERT INTO v1_data(id,value) VALUES(1,'before')")
                conn.execute("PRAGMA user_version=1")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)

            def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "ALTER TABLE v1_data ADD COLUMN v2_label TEXT NOT NULL DEFAULT ''"
                )

            def fail_v2_to_v3(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "ALTER TABLE v1_data ADD COLUMN v3_label TEXT NOT NULL DEFAULT ''"
                )
                conn.execute("UPDATE v1_data SET value='partial-v3'")
                raise sqlite3.OperationalError("injected second migration failure")

            try:
                db._SCHEMA_MIGRATIONS.clear()
                db._SCHEMA_MIGRATIONS.update({1: migrate_v1_to_v2, 2: fail_v2_to_v3})
                with mock.patch.object(db, "SCHEMA_VERSION", 3):
                    with self.assertRaisesRegex(
                        sqlite3.OperationalError,
                        "injected second migration failure",
                    ):
                        db.init_db()
                conn = sqlite3.connect(path)
                try:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    columns = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(v1_data)")
                    }
                    value = str(
                        conn.execute("SELECT value FROM v1_data WHERE id=1").fetchone()[0]
                    )
                finally:
                    conn.close()
                self.assertEqual(version, 1)
                self.assertEqual(columns, {"id", "value"})
                self.assertEqual(value, "before")
            finally:
                db._SCHEMA_MIGRATIONS.clear()
                db._SCHEMA_MIGRATIONS.update(previous_migrations)
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_keyboard_interrupt_rolls_back_schema_step(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        previous_migrations = dict(db._SCHEMA_MIGRATIONS)
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-interrupt-") as root:
            path = Path(root) / "v1.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE v1_data(id INTEGER PRIMARY KEY)")
                conn.execute("PRAGMA user_version=1")
                conn.commit()
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)

            def interrupt_migration(conn: sqlite3.Connection) -> None:
                conn.execute("ALTER TABLE v1_data ADD COLUMN partial TEXT")
                raise KeyboardInterrupt()

            try:
                db._SCHEMA_MIGRATIONS.clear()
                db._SCHEMA_MIGRATIONS[1] = interrupt_migration
                with mock.patch.object(db, "SCHEMA_VERSION", 2):
                    with self.assertRaises(KeyboardInterrupt):
                        db.init_db()
                conn = sqlite3.connect(path)
                try:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    columns = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(v1_data)")
                    }
                finally:
                    conn.close()
                self.assertEqual(version, 1)
                self.assertEqual(columns, {"id"})
            finally:
                db._SCHEMA_MIGRATIONS.clear()
                db._SCHEMA_MIGRATIONS.update(previous_migrations)
                db.configure_database(previous_path, test_mode=previous_test_mode)

    def test_newer_database_is_rejected(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-newer-") as root:
            path = Path(root) / "newer.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
            finally:
                conn.close()
            db.configure_database(path, test_mode=True)
            try:
                with self.assertRaisesRegex(RuntimeError, "拒绝降级启动"):
                    db.init_db()
            finally:
                db.configure_database(previous_path, test_mode=previous_test_mode)
