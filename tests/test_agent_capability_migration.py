"""v24 可重入迁移保留已有用户数据，且不在读请求中执行 DDL。"""

import sqlite3
import unittest

from app import database as db


class CapabilityMigrationTests(unittest.TestCase):
    def test_existing_preferences_survive_idempotent_migration(self):
        with sqlite3.connect(":memory:") as conn:
            conn.executescript(
                "CREATE TABLE agent_media_preferences (owner_digest TEXT PRIMARY KEY,preferred_server TEXT,preferred_download_target TEXT,updated_at TEXT); INSERT INTO agent_media_preferences VALUES('owner','emby','qb','before');"
            )
            db._migrate_agent_capability_closure_v24(conn)
            db._migrate_agent_capability_closure_v24(conn)
            self.assertEqual(
                conn.execute("SELECT * FROM agent_media_preferences").fetchone(),
                ("owner", "emby", "qb", "before", "{}", ""),
            )
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue({"agent_compensations", "media_automation_rules"} <= tables)
            conn.execute(
                "INSERT INTO agent_compensations VALUES('receipt','owner','available','now')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE agent_compensations SET state='pretend_completed'")

    def test_migration_participates_in_transaction(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("BEGIN IMMEDIATE")
            db._migrate_agent_capability_closure_v24(conn)
            conn.rollback()
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='media_automation_rules'"
                ).fetchone()
            )

    def test_fresh_schema_contains_same_automation_columns(self):
        from app.repositories.media_automation_rules import SCHEMA

        with (
            sqlite3.connect(":memory:") as fresh,
            sqlite3.connect(":memory:") as migrated,
        ):
            fresh.executescript(db._SCHEMA)
            migrated.executescript(SCHEMA)
            expected = list(
                migrated.execute("PRAGMA table_info(media_automation_rules)")
            )
            self.assertEqual(
                list(fresh.execute("PRAGMA table_info(media_automation_rules)")),
                expected,
            )
