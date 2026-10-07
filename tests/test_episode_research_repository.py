"""季集证据缓存：只使用临时 SQLite，禁止真实配置及网络访问。"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import config, database_migrations, database_schema
from app import database as db
from app.repositories import episode_research as repository
from tests.support import isolated_test_database

KEY = "a" * 64
STAMP = 2_000_000_000.0
PAYLOAD_LIMIT = 256 * 1024


class EpisodeResearchCacheTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(
            config, "_ensure_loaded", side_effect=AssertionError("配置不得读取"),
        ))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("禁止联网")))
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("禁止联网")))
        self.database_path = self.enterContext(isolated_test_database())
        self.enterContext(patch.object(repository.time, "time", return_value=STAMP))

    def put(self, key=KEY, payload=None, **kwargs):
        return repository.put_episode_research_cache(
            key, {} if payload is None else payload,
            **{"status": "verified", "ttl_seconds": 60, **kwargs},
        )

    def rows(self):
        with db.get_conn() as conn:
            return [tuple(row) for row in conn.execute(
                "SELECT * FROM episode_research_cache ORDER BY cache_key"
            )]

    def seed(self, count=512, *, start=0, stamp=STAMP - 100, ttl=1000):
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO episode_research_cache"
                "(cache_key,policy_version,status,payload,expires_at,updated_at) "
                "VALUES(?,1,'proposal','{}',?,?)",
                [(f"{i:064x}", stamp + ttl, stamp) for i in range(start, start + count)],
            )

    def test_roundtrip_statuses_versions_and_detached_payload(self):
        payload = {"version": 1, "evidence": [{"provider": "tmdb", "title": "季集"}],
                   "empty": None, "flag": True, "ratio": 0.25}
        for status in ("verified", "proposal", "negative"):
            with self.subTest(status=status):
                self.assertIsNone(self.put(payload=payload, status=status, policy_version=2))
                self.assertIsNone(repository.get_episode_research_cache(KEY))
                actual = repository.get_episode_research_cache(KEY.upper(), policy_version=2)
                self.assertEqual(actual, {
                    "status": status, "payload": payload,
                    "expires_at": STAMP + 60, "policy_version": 2,
                })
                actual["payload"]["evidence"].clear()
                self.assertEqual(repository.get_episode_research_cache(
                    KEY, policy_version=2,
                )["payload"], payload)
        self.assertEqual(len(self.rows()), 1)

    def test_expiration_is_exact_and_get_is_read_only(self):
        self.put()
        before = self.rows()
        for offset, hit in ((59.999, True), (60, False), (61, False)):
            with self.subTest(offset=offset), patch.object(repository.time, "time", return_value=STAMP + offset):
                self.assertEqual(repository.get_episode_research_cache(KEY) is not None, hit)
        self.assertIsNone(repository.get_episode_research_cache("b" * 64))
        self.assertEqual(self.rows(), before)

    def test_ttl_boundaries(self):
        for ttl in (60, 2_592_000):
            with self.subTest(ttl=ttl):
                self.put(ttl_seconds=ttl)
                self.assertEqual(repository.get_episode_research_cache(KEY)["expires_at"], STAMP + ttl)
        for ttl in (None, False, True, 0, 59, 2_592_001, -1, "60", 60.0, float("inf"), float("nan")):
            with self.subTest(ttl=ttl), patch.object(db, "get_conn") as connection:
                with self.assertRaises(ValueError):
                    self.put(ttl_seconds=ttl)
                connection.assert_not_called()

    def test_invalid_keys_rejected_by_all_entrypoints_before_database_access(self):
        for key in (None, 7, True, b"a" * 64, "", "a" * 63, "a" * 65,
                    "g" * 64, " " + KEY, KEY + "\n", "Ａ" * 64, "' OR 1=1 --"):
            for operation in (self.put, repository.get_episode_research_cache,
                              repository.invalidate_episode_research_cache):
                with self.subTest(key=key, operation=operation), patch.object(db, "get_conn") as connection:
                    with self.assertRaises(ValueError):
                        operation(key)
                    connection.assert_not_called()

    def test_invalid_status_and_policy_versions_rejected_before_database_access(self):
        for status in (None, True, 1, "", "VERIFIED", "verified ", "failed", [], {}):
            with self.subTest(status=status), patch.object(db, "get_conn") as connection:
                with self.assertRaises(ValueError):
                    self.put(status=status)
                connection.assert_not_called()
        for version in (None, True, False, 0, -1, "1", 1.0, 2**63, float("nan")):
            for operation in (self.put, repository.get_episode_research_cache):
                with self.subTest(version=version, operation=operation), patch.object(db, "get_conn") as connection:
                    with self.assertRaises(ValueError):
                        operation(KEY, policy_version=version)
                    connection.assert_not_called()
        self.put(policy_version=2**63 - 1)
        self.assertIsNotNone(repository.get_episode_research_cache(KEY, policy_version=2**63 - 1))

    def test_bad_json_objects_and_nonfinite_values_are_rejected_without_writes(self):
        cyclic = {}
        cyclic["cycle"] = cyclic
        deep = {}
        for _ in range(100):
            deep = {"nested": deep}
        invalid = [None, [], "{}", 1, True, {1: "key"}, {"nested": {None: "key"}},
                   {"bytes": b"x"}, {"tuple": (1, 2)}, {"set": {1}}, {"object": object()},
                   {"text": "\ud800"}, {"\udfff": "key"}, cyclic, deep]
        invalid.extend({"nested": [value]} for value in (float("nan"), float("inf"), -float("inf")))
        self.put(payload={"preserve": True})
        before = self.rows()
        for payload in invalid:
            with self.subTest(kind=type(payload)), patch.object(db, "get_conn") as connection:
                with self.assertRaises(ValueError):
                    repository.put_episode_research_cache(KEY, payload, status="proposal", ttl_seconds=60)
                connection.assert_not_called()
        self.assertEqual(self.rows(), before)

    def test_shared_subtree_expansion_is_bounded_but_small_aliases_are_valid(self):
        subtree = ["value"]
        self.put(payload={"values": [subtree, subtree]})
        self.assertEqual(repository.get_episode_research_cache(KEY)["payload"],
                         {"values": [["value"], ["value"]]})
        for _ in range(48):
            subtree = [subtree, subtree]
        with patch.object(db, "get_conn") as connection:
            with self.assertRaises(ValueError):
                self.put(payload={"values": subtree})
            connection.assert_not_called()

    def test_payload_limit_counts_compact_utf8_bytes(self):
        payload = {"x": "a" * (PAYLOAD_LIMIT - 8)}
        self.put(payload=payload)
        self.assertEqual(repository.get_episode_research_cache(KEY)["payload"], payload)
        for oversized in ({"x": payload["x"] + "a"}, {"x": "季" * (PAYLOAD_LIMIT // 3)}):
            with self.subTest(unicode=oversized["x"][0]), self.assertRaises(ValueError):
                self.put(payload=oversized)
        self.assertEqual(repository.get_episode_research_cache(KEY)["payload"], payload)

    def test_malformed_persisted_payload_is_miss_and_never_deleted_on_read(self):
        self.put()
        malformed = ["{", "[]", "null", "true", '{"x":NaN}', '{"x":Infinity}',
                     '{"x":-Infinity}', '{"x":1e999}', '{"x":1,"x":2}',
                     '{"x":{"a":1,"a":2}}', '{"x":"\\ud800"}', b"\xff",
                     '{"x":' + '[' * 1100 + '0' + ']' * 1100 + '}',
                     json.dumps({"x": "x" * PAYLOAD_LIMIT})]
        for payload in malformed:
            with self.subTest(payload=str(payload)[:35]):
                with db.get_conn() as conn:
                    conn.execute("PRAGMA ignore_check_constraints=ON")
                    conn.execute("UPDATE episode_research_cache SET payload=?", (payload,))
                before = self.rows()
                self.assertIsNone(repository.get_episode_research_cache(KEY))
                self.assertEqual(self.rows(), before)

    def test_invalid_utf8_sqlite_text_is_miss_not_a_decode_exception(self):
        self.put()
        with db.get_conn() as conn:
            conn.execute("UPDATE episode_research_cache SET payload=CAST(X'7B2278223A22FF227D' AS TEXT)")
        self.assertIsNone(repository.get_episode_research_cache(KEY))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM episode_research_cache").fetchone()[0], 1)

    def test_corrupt_row_metadata_is_miss(self):
        for column, value in (("status", "approved"), ("policy_version", 0),
                              ("policy_version", 1.5), ("expires_at", "bad"),
                              ("expires_at", float("inf")), ("updated_at", "bad"),
                              ("updated_at", -1), ("updated_at", STAMP + 61),
                              ("updated_at", float("inf"))):
            with self.subTest(column=column, value=value):
                self.put()
                with db.get_conn() as conn:
                    conn.execute("PRAGMA ignore_check_constraints=ON")
                    conn.execute(f"UPDATE episode_research_cache SET {column}=?", (value,))
                self.assertIsNone(repository.get_episode_research_cache(KEY))

    def test_invalidate_is_idempotent_and_does_not_touch_other_keys(self):
        self.put()
        self.put("b" * 64)
        self.assertIsNone(repository.invalidate_episode_research_cache(KEY.upper()))
        repository.invalidate_episode_research_cache(KEY)
        self.assertIsNone(repository.get_episode_research_cache(KEY))
        self.assertIsNotNone(repository.get_episode_research_cache("b" * 64))

    def test_expired_entries_are_evicted_before_oldest_live_entries(self):
        self.seed(511)
        self.seed(1, start=511, stamp=STAMP - 60, ttl=60)
        self.put()
        self.assertEqual(len(self.rows()), 512)
        self.assertIsNotNone(repository.get_episode_research_cache("0" * 64))
        self.assertIsNone(repository.get_episode_research_cache(f"{511:064x}"))
        self.put("b" * 64)
        self.assertEqual(len(self.rows()), 512)
        self.assertIsNone(repository.get_episode_research_cache("0" * 64))
        self.assertIsNotNone(repository.get_episode_research_cache(f"{1:064x}"))

    def test_upsert_refreshes_age_without_evicting_unrelated_rows(self):
        self.seed()
        self.put("0" * 64, payload={"new": 1}, status="negative", policy_version=2)
        self.assertEqual(len(self.rows()), 512)
        self.put()
        self.assertIsNone(repository.get_episode_research_cache(f"{1:064x}"))
        self.assertEqual(repository.get_episode_research_cache(
            "0" * 64, policy_version=2,
        )["payload"], {"new": 1})

    def test_failed_insert_rolls_back_expiration_cleanup(self):
        self.seed(1, ttl=60)
        before = self.rows()
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_cache_insert BEFORE INSERT ON episode_research_cache "
                         "BEGIN SELECT RAISE(ABORT,'test insert failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test insert failure"):
            self.put()
        self.assertEqual(self.rows(), before)

    def test_failed_oldest_eviction_rolls_back_upsert(self):
        self.seed()
        before = self.rows()
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_cache_delete BEFORE DELETE ON episode_research_cache "
                         "BEGIN SELECT RAISE(ABORT,'test eviction failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test eviction failure"):
            self.put()
        self.assertEqual(self.rows(), before)

    def test_concurrent_writers_keep_capacity_and_reader_sees_whole_payload(self):
        self.seed(508)
        barrier = threading.Barrier(8)

        def write(worker):
            barrier.wait(timeout=10)
            for index in range(6):
                key = f"{1000 + worker * 10 + index:064x}"
                payload = {"worker": worker, "values": [worker] * 30}
                self.put(key, payload=payload)
                actual = repository.get_episode_research_cache(key)
                if actual is not None:
                    self.assertEqual(actual["payload"], payload)
                with db.get_conn() as conn:
                    self.assertLessEqual(conn.execute(
                        "SELECT COUNT(*) FROM episode_research_cache"
                    ).fetchone()[0], 512)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(8)))
        self.assertEqual(len(self.rows()), 512)

    def test_concurrent_same_key_upserts_remain_coherent(self):
        barrier = threading.Barrier(8)

        def write(worker):
            barrier.wait(timeout=10)
            self.put(payload={"worker": worker, "values": [worker] * 100},
                     status="proposal", ttl_seconds=60 + worker)
            actual = repository.get_episode_research_cache(KEY)
            winner = actual["payload"]["worker"]
            self.assertEqual(actual["payload"]["values"], [winner] * 100)
            self.assertEqual(actual["expires_at"], STAMP + 60 + winner)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(8)))
        self.assertEqual(len(self.rows()), 1)

    def test_calls_use_the_current_facade_connection_not_a_captured_path(self):
        with patch.object(db, "get_conn", wraps=db.get_conn) as connection:
            self.put()
            repository.get_episode_research_cache(KEY)
            repository.invalidate_episode_research_cache(KEY)
        self.assertEqual(connection.call_count, 3)
        self.put(payload={"database": "outer"})
        with isolated_test_database("other.db"):
            self.assertIsNone(repository.get_episode_research_cache(KEY))
            self.put(payload={"database": "inner"})
        self.assertEqual(repository.get_episode_research_cache(KEY)["payload"], {"database": "outer"})

    def cache_schema(self, conn):
        return [tuple(row) for row in conn.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE tbl_name='episode_research_cache' ORDER BY type,name"
        )]

    def legacy_29(self, conn):
        conn.execute("DROP TABLE episode_research_cache")
        conn.execute("PRAGMA user_version=29")
        conn.execute("INSERT INTO settings_kv(key,value) VALUES('cache-migration-sentinel','keep')")

    def test_v29_migration_alone_matches_fresh_schema_and_preserves_legacy_database(self):
        self.assertEqual(db.SCHEMA_VERSION, 33)
        self.assertEqual(sorted(database_migrations._SCHEMA_MIGRATIONS), list(range(1, 33)))
        migration = database_migrations._SCHEMA_MIGRATIONS[29]
        self.assertFalse(hasattr(db, migration.__name__))
        self.assertEqual(migration.__module__, database_migrations.__name__)
        with db.get_conn() as conn:
            fresh = self.cache_schema(conn)
            self.legacy_29(conn)
            before_schema = [tuple(row) for row in conn.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            )]
            before_dump = list(conn.iterdump())
            migration(conn)
            self.assertEqual(self.cache_schema(conn), fresh)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 29)
            migration(conn)  # 可重复运行，无隐式提交或自推版本。
            conn.execute("DROP TABLE episode_research_cache")
            self.assertEqual(list(conn.iterdump()), before_dump)
            self.assertEqual([tuple(row) for row in conn.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            )], before_schema)
            migration(conn)
        with sqlite3.connect(":memory:") as fresh_conn:
            fresh_conn.executescript(database_schema._SCHEMA)
            self.assertEqual(self.cache_schema(fresh_conn), fresh)

    def test_registered_upgrade_runs_backup_gate_before_ddl_then_advances_to_31(self):
        with db.get_conn() as conn:
            fresh = self.cache_schema(conn)
            self.legacy_29(conn)
        visited = []

        def backup(connection, *, current_version):
            visited.append(current_version)
            self.assertEqual(current_version, 29)
            self.assertEqual(self.cache_schema(connection), [])
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 29)

        with patch.object(db, "_create_pre_migration_backup", side_effect=backup), db.get_conn() as conn:
            self.assertEqual(db._prepare_schema_migration(conn, database_existed=True), 29)
            self.assertEqual(self.cache_schema(conn), fresh)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(visited, [29])
        db.init_db()
        self.put()
        self.assertIsNotNone(repository.get_episode_research_cache(KEY))

    def test_init_db_29_to_31_uses_registered_migration(self):
        with db.get_conn() as conn:
            self.legacy_29(conn)
        migration = database_migrations._SCHEMA_MIGRATIONS[29]
        with patch.dict(database_migrations._SCHEMA_MIGRATIONS, {29: unittest.mock.Mock(wraps=migration)}):
            observed = database_migrations._SCHEMA_MIGRATIONS[29]
            db.init_db()
            self.assertEqual(observed.call_count, 1)
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertEqual(conn.execute(
                "SELECT value FROM settings_kv WHERE key='cache-migration-sentinel'"
            ).fetchone()[0], "keep")
        self.put()

    def test_missing_migration_or_backup_failure_does_not_modify_v29(self):
        with db.get_conn() as conn:
            self.legacy_29(conn)
        with patch.dict(database_migrations._SCHEMA_MIGRATIONS, {}, clear=True), self.assertRaisesRegex(RuntimeError, "缺少"):
            db.init_db()
        with (
            patch.object(db, "_create_pre_migration_backup", side_effect=RuntimeError("backup blocked")),
            self.assertRaisesRegex(RuntimeError, "backup blocked"),
        ):
            db.init_db()
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 29)
            self.assertEqual(self.cache_schema(conn), [])

    def test_migration_failure_rolls_back_table_indexes_and_version(self):
        with db.get_conn() as conn:
            self.legacy_29(conn)
        migration = database_migrations._SCHEMA_MIGRATIONS[29]

        def failing(connection):
            migration(connection)
            raise RuntimeError("migration interrupted")

        with (
            patch.dict(database_migrations._SCHEMA_MIGRATIONS, {29: failing}),
            self.assertRaisesRegex(RuntimeError, "migration interrupted"),
        ):
            db.init_db()
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 29)
            self.assertEqual(self.cache_schema(conn), [])
        db.init_db()
        self.put()

    def test_new_table_constraints(self):
        base = [KEY, 1, "verified", "{}", STAMP + 60, STAMP]
        for index, invalid in ((0, "bad"), (0, "g" * 64), (0, KEY + "\0extra"), (1, 0), (1, 1.5),
                               (2, "approved"), (3, b"{}"), (3, "x" * (PAYLOAD_LIMIT + 1)),
                               (4, "bad"), (5, -1)):
            values = base.copy()
            values[index] = invalid
            with self.subTest(index=index), self.assertRaises(sqlite3.IntegrityError), db.get_conn() as conn:
                conn.execute("INSERT INTO episode_research_cache VALUES(?,?,?,?,?,?)", values)


class EpisodeResearchRepositoryImportTests(unittest.TestCase):
    def test_import_does_not_import_database_or_config_or_open_connections(self):
        code = '''
import importlib.abc
import sys
from unittest.mock import patch
class ForbiddenImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in {"app.database", "app.config"}:
            raise AssertionError("eager database/config import")
sys.meta_path.insert(0, ForbiddenImports())
with patch("sqlite3.connect", side_effect=AssertionError("database opened")):
    from app.repositories import episode_research
assert "app.database" not in sys.modules
assert "app.config" not in sys.modules
'''
        completed = subprocess.run(
            [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "MEDIAFLUX_DISABLE_FILE_LOGGING": "1"},
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
