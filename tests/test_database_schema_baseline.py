"""首个正式 SQLite schema 基线契约。"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from app import database as db
from tests.support import IsolatedDatabaseTestCase


class DatabaseSchemaBaselineTests(IsolatedDatabaseTestCase):
    def test_fresh_database_contains_complete_v2_schema(self) -> None:
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
        self.assertEqual(version, 2)
        self.assertIn("rules_snapshot", task_columns)
        self.assertIn("season_override", task_columns)
        self.assertIn("episode_override", task_columns)
        self.assertIn("session_id", playback_columns)
        self.assertIn("version", mapping_columns)
        self.assertIn("idx_agent_action_history_owner_id", action_indexes)
        self.assertEqual(rss_indexes.get("idx_rss_entries_item_guid"), 1)
        self.assertEqual(rss_indexes.get("idx_rss_entries_failure_retry"), 0)
        self.assertTrue({
            "media_title_aliases",
            "idx_media_title_alias_lookup",
            "recognition_knowledge",
            "idx_recognition_knowledge_lookup",
            "idx_recognition_knowledge_source",
            "idx_media_probe_cache_fingerprint_updated",
            "media_proxy_playback_sessions",
            "idx_media_proxy_records_session_id",
        }.issubset(schema_objects))

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
                self.assertEqual(version, 2)
                self.assertEqual(preserved["context_type"], "patrol")
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

    def test_precreated_empty_database_is_treated_as_fresh_install(self) -> None:
        previous_path = db.DB_PATH
        previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        with tempfile.TemporaryDirectory(prefix="mediaflux-schema-empty-") as root:
            path = Path(root) / "empty.db"
            path.touch()
            db.configure_database(path, test_mode=True)
            try:
                db.init_db()
                with db.get_conn() as conn:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual(version, db.SCHEMA_VERSION)
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
