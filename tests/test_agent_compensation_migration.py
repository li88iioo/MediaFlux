"""补偿原表 v38→v39 迁移、旧空载荷失效与新建 schema 对齐。"""
from __future__ import annotations

import sqlite3

import pytest

from app import database as db, database_migrations, sdk
from app.repositories import agent_compensations as receipts
from tests import test_agent_sdk_preferences as preference_tests, test_agent_sdk_activity_undo as undo_tests

OWNER, runtime = preference_tests.OWNER, preference_tests.runtime
organize_backend = undo_tests.organize_backend

LEGACY_SCHEMA = """
CREATE TABLE agent_compensations (
 receipt_id TEXT PRIMARY KEY, owner_digest TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'available' CHECK(state IN ('available','executing','completed','outcome_unknown')),
 updated_at TEXT NOT NULL
);
"""


def test_real_v38_upgrade_preserves_rows_preferences_and_other_table_schema(runtime, organize_backend):
    owner_digest = db.media_owner_digest(OWNER)
    states = ("available", "executing", "completed", "outcome_unknown")
    with sdk.call_owner(OWNER):
        assert sdk.preferences.update(memory_notes=["原库路径 /media/archive；保留原保存值"])["ok"]
        before_preferences = sdk.preferences.get()
    with db.get_conn() as conn:
        conn.execute("DROP TABLE agent_compensations")
        conn.executescript(LEGACY_SCHEMA)
        conn.executemany("INSERT INTO agent_compensations VALUES(?,?,?,?)",
                         [("legacy-" + state, owner_digest, state, "2026-01-01 00:00:00") for state in states])
        conn.execute("PRAGMA user_version=38")
        before_schema = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name<>'agent_compensations' ORDER BY name").fetchall()
    db.init_db()  # 实际 SQLite 文件、原备份门禁和正式版本迁移。
    db.init_db()  # 重启/可重入不补造旧 payload 或刷新旧行时间。
    with db.get_conn() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        after_schema = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name<>'agent_compensations' ORDER BY name").fetchall()
        assert [tuple(row) for row in after_schema] == [tuple(row) for row in before_schema]
        rows = conn.execute("SELECT receipt_id,owner_digest,state,updated_at,payload_json,expires_at FROM agent_compensations ORDER BY receipt_id").fetchall()
        assert [tuple(row) for row in rows] == sorted(
            [("legacy-" + state, owner_digest, state, "2026-01-01 00:00:00", "", 0.0) for state in states])
    with sdk.call_owner(OWNER):
        assert sdk.preferences.get() == before_preferences
        with pytest.raises(ValueError, match="历史回执未保存私有快照"):
            sdk.activity.undo("legacy-available")
        assert not receipts.claim("legacy-available", owner_digest)
        fresh = sdk.activity.inspect_undo(7)["data"]["receipt_id"]
        assert fresh != "legacy-available"
        stored = receipts.get(fresh, owner_digest)
        assert stored is not None and stored["payload"]["log_id"] == 7


def test_migration_is_atomic_idempotent_and_matches_fresh_compensation_schema():
    with sqlite3.connect(":memory:") as migrated, sqlite3.connect(":memory:") as fresh:
        migrated.executescript(LEGACY_SCHEMA)
        migrated.execute("INSERT INTO agent_compensations VALUES('legacy','owner','available','before')")
        migrated.commit()
        migrated.execute("BEGIN IMMEDIATE")
        database_migrations._migrate_agent_compensation_payload_v39(migrated)
        migrated.rollback()
        assert [row[1] for row in migrated.execute("PRAGMA table_info(agent_compensations)")] == [
            "receipt_id", "owner_digest", "state", "updated_at"]
        database_migrations._migrate_agent_compensation_payload_v39(migrated)
        database_migrations._migrate_agent_compensation_payload_v39(migrated)
        fresh.executescript(db._SCHEMA)
        assert list(migrated.execute("PRAGMA table_info(agent_compensations)")) == list(
            fresh.execute("PRAGMA table_info(agent_compensations)"))
        assert migrated.execute("SELECT * FROM agent_compensations").fetchone() == ("legacy", "owner", "available", "before", "", 0.0)
