"""发布格式教学 schema31：DDL、迁移原子性与备份恢复边界。"""
from __future__ import annotations

import builtins
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import database as db
from app import database_migrations, database_schema
from app.modules import backup
from app.runtime_paths import RuntimePaths
from tests.support import isolated_test_database

_RULE_TABLE = "recognition_format_rules"
_RULE_TEMPLATE = "[Group] {title} S{season}E{episode} [{resolution}].mkv"
_RULE_EXAMPLES = (
    {
        "filename": "[Group] 星海航行 S1E2 [1080p].mkv",
        "title": "星海航行",
        "season": 1,
        "episode": 2,
    },
    {
        "filename": "[Group] 星海航行 S1E3 [1080p].mkv",
        "title": "星海航行",
        "season": 1,
        "episode": 3,
    },
)
_RULE_EXAMPLES_JSON = json.dumps(_RULE_EXAMPLES, ensure_ascii=False)
_RULE_COLUMNS = (
    "id",
    "signature",
    "name",
    "template",
    "scope",
    "parent_path",
    "examples_json",
    "disabled",
    "revision",
    "created_at",
    "updated_at",
)


class ReleaseFormatMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database_path = self.enterContext(
            isolated_test_database("mediaflux.db")
        )

    @staticmethod
    def _table_schema(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE tbl_name=? ORDER BY type,name",
                (_RULE_TABLE,),
            )
        ]

    @staticmethod
    def _set_up_v30(conn: sqlite3.Connection) -> None:
        conn.execute(f"DROP TABLE {_RULE_TABLE}")
        conn.execute("PRAGMA user_version=30")
        conn.execute(
            "INSERT OR REPLACE INTO settings_kv(key,value) "
            "VALUES('release-format-migration-sentinel','keep')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO episode_research_cache "
            "(cache_key,policy_version,status,payload,expires_at,updated_at) "
            "VALUES(?,1,'proposal','{}',1000,900)",
            ("a" * 64,),
        )

    @staticmethod
    def _paths(root: Path) -> RuntimePaths:
        return RuntimePaths(
            program_dir=root / "program",
            data_dir=root / "data",
            config_dir=root / "config",
            cache_dir=root / "cache",
            log_dir=root / "logs",
            strm_dir=root / "strm",
            trash_dir=root / "trash",
        )

    def _current_runtime_paths(self) -> RuntimePaths:
        root = self.database_path.parent
        return RuntimePaths(
            program_dir=root / "program",
            data_dir=root,
            config_dir=root,
            cache_dir=root / "cache",
            log_dir=root / "logs",
            strm_dir=root / "strm",
            trash_dir=root / "trash",
        )

    @staticmethod
    def _seed_database(path: Path, *, value: str) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.executescript(database_schema._SCHEMA)
            connection.execute("PRAGMA user_version=31")
            connection.execute(
                "INSERT INTO recognition_format_rules "
                "(signature,name,template,scope,parent_path,examples_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "sig-release-1080",
                    "季度番剧",
                    _RULE_TEMPLATE,
                    "directory",
                    "/library/anime",
                    _RULE_EXAMPLES_JSON,
                    "2026-09-13 10:00:00",
                    "2026-09-13 10:00:00",
                ),
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS backup_sentinel(value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO backup_sentinel(value) VALUES(?)", (value,))
            connection.commit()
        finally:
            connection.close()

    def test_schema31_ddl_has_exact_columns_defaults_and_constraints(self) -> None:
        self.assertEqual(db.SCHEMA_VERSION, 32)
        self.assertEqual(sorted(database_migrations._SCHEMA_MIGRATIONS), list(range(1, 32)))
        migration = database_migrations._SCHEMA_MIGRATIONS[30]
        self.assertFalse(hasattr(db, migration.__name__))
        self.assertEqual(migration.__module__, database_migrations.__name__)
        self.assertEqual(len(database_schema._RECOGNITION_FORMAT_RULE_STATEMENTS), 1)

        with db.get_conn() as conn:
            info = [
                tuple(row)
                for row in conn.execute(f"PRAGMA table_info({_RULE_TABLE})")
            ]
            self.assertEqual([row[1] for row in info], list(_RULE_COLUMNS))
            self.assertEqual(
                info,
                [
                    (0, "id", "INTEGER", 0, None, 1),
                    (1, "signature", "TEXT", 1, None, 0),
                    (2, "name", "TEXT", 1, None, 0),
                    (3, "template", "TEXT", 1, None, 0),
                    (4, "scope", "TEXT", 1, None, 0),
                    (5, "parent_path", "TEXT", 1, "''", 0),
                    (6, "examples_json", "TEXT", 1, "'[]'", 0),
                    (7, "disabled", "INTEGER", 1, "0", 0),
                    (8, "revision", "INTEGER", 1, "1", 0),
                    (9, "created_at", "TEXT", 1, None, 0),
                    (10, "updated_at", "TEXT", 1, None, 0),
                ],
            )

            conn.execute(
                "INSERT INTO recognition_format_rules "
                "(signature,name,template,scope,created_at,updated_at) "
                "VALUES('defaults','默认','{title} - {episode}.mkv','release','now','now')"
            )
            row = conn.execute(
                "SELECT parent_path,examples_json,disabled,revision "
                "FROM recognition_format_rules WHERE signature='defaults'"
            ).fetchone()
            self.assertEqual(tuple(row), ("", "[]", 0, 1))

            invalid_rows = (
                ("bad-scope", "other", 0, 1),
                ("bad-disabled", "directory", 2, 1),
                ("bad-revision", "directory", 0, 0),
            )
            for signature, scope, disabled, revision in invalid_rows:
                with self.subTest(signature=signature), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    conn.execute(
                        "INSERT INTO recognition_format_rules "
                        "(signature,name,template,scope,disabled,revision,"
                        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            signature,
                            "规则",
                            "{title} - {episode}.mkv",
                            scope,
                            disabled,
                            revision,
                            "now",
                            "now",
                        ),
                    )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO recognition_format_rules "
                    "(signature,name,template,scope,created_at,updated_at) "
                    "VALUES('defaults','重复','{title} - {episode}.mkv','directory','now','now')"
                )

    def test_v30_migration_reuses_fresh_ddl_is_idempotent_and_keeps_data(self) -> None:
        migration = database_migrations._SCHEMA_MIGRATIONS[30]
        with db.get_conn() as conn:
            fresh_schema = self._table_schema(conn)
            self._set_up_v30(conn)
            migration(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 30)
            self.assertEqual(self._table_schema(conn), fresh_schema)
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM settings_kv "
                    "WHERE key='release-format-migration-sentinel'"
                ).fetchone()[0],
                "keep",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT payload FROM episode_research_cache WHERE cache_key=?",
                    ("a" * 64,),
                ).fetchone()[0],
                "{}",
            )
            migration(conn)
            self.assertEqual(self._table_schema(conn), fresh_schema)

            conn.execute(
                "INSERT INTO recognition_format_rules "
                "(signature,name,template,scope,created_at,updated_at) "
                "VALUES('migrated','迁移规则','{title} - {episode}.mkv','release','now','now')"
            )
            self.assertEqual(
                conn.execute(
                    "SELECT name FROM recognition_format_rules WHERE signature='migrated'"
                ).fetchone()[0],
                "迁移规则",
            )

        with sqlite3.connect(":memory:") as fresh_conn:
            fresh_conn.executescript(database_schema._SCHEMA)
            self.assertEqual(self._table_schema(fresh_conn), fresh_schema)

    def test_init_db_30_to_31_runs_backup_gate_before_ddl(self) -> None:
        with db.get_conn() as conn:
            self._set_up_v30(conn)
        visited: list[int] = []

        def backup_gate(connection: sqlite3.Connection, *, current_version: int) -> None:
            visited.append(current_version)
            self.assertEqual(current_version, 30)
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (_RULE_TABLE,),
                ).fetchone()
            )
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 30)

        with patch.object(db, "_create_pre_migration_backup", side_effect=backup_gate):
            db.init_db()

        self.assertEqual(visited, [30])
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (_RULE_TABLE,),
                ).fetchone()
            )
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM settings_kv "
                    "WHERE key='release-format-migration-sentinel'"
                ).fetchone()[0],
                "keep",
            )

    def test_failed_30_to_31_migration_rolls_back_table_and_version(self) -> None:
        with db.get_conn() as conn:
            self._set_up_v30(conn)
        migration = database_migrations._SCHEMA_MIGRATIONS[30]

        def failing(connection: sqlite3.Connection) -> None:
            migration(connection)
            raise RuntimeError("release format migration interrupted")

        with (
            patch.dict(database_migrations._SCHEMA_MIGRATIONS, {30: failing}),
            self.assertRaisesRegex(RuntimeError, "release format migration interrupted"),
        ):
            db.init_db()

        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 30)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (_RULE_TABLE,),
                ).fetchone()
            )
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM settings_kv "
                    "WHERE key='release-format-migration-sentinel'"
                ).fetchone()[0],
                "keep",
            )
        db.init_db()
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (_RULE_TABLE,),
                ).fetchone()
            )

    def test_backup_restore_includes_release_format_rules_and_invalidates_loaded_cache(self) -> None:
        from app.modules.recognition import formats

        compiled = formats.compile_template(_RULE_TEMPLATE)
        match = compiled.regex.fullmatch(_RULE_EXAMPLES[0]["filename"])
        self.assertIsNotNone(match)
        self.assertEqual(
            match.groupdict(),
            {
                "title": "星海航行",
                "season": "1",
                "episode": "2",
                "resolution": "1080p",
            },
        )

        with tempfile.TemporaryDirectory(prefix="mediaflux-release-format-backup-") as directory:
            root = Path(directory)
            source = self._paths(root / "source")
            target = self._paths(root / "target")
            source.ensure_writable_dirs()
            target.ensure_writable_dirs()
            self._seed_database(source.database_path, value="source")
            target_connection = sqlite3.connect(target.database_path)
            try:
                target_connection.execute(
                    "CREATE TABLE target_only(value TEXT NOT NULL)"
                )
                target_connection.execute("INSERT INTO target_only(value) VALUES('old')")
                target_connection.commit()
            finally:
                target_connection.close()

            archive = backup.create_backup(
                source,
                output=root / "release-formats.zip",
                include_settings=False,
            )
            invalidate_cache = Mock()
            formats_module = types.ModuleType("app.modules.recognition.formats")
            formats_module.invalidate_cache = invalidate_cache
            with patch.dict(
                sys.modules,
                {"app.modules.recognition.formats": formats_module},
            ):
                manifest = backup.restore_backup(target, archive)

            self.assertEqual(manifest.payload["database_schema_version"], 31)
            invalidate_cache.assert_called_once_with()
            restored = sqlite3.connect(target.database_path)
            try:
                self.assertEqual(
                    restored.execute(
                        "SELECT name,template,scope,parent_path,examples_json,disabled,revision "
                        "FROM recognition_format_rules WHERE signature='sig-release-1080'"
                    ).fetchone(),
                    (
                        "季度番剧",
                        _RULE_TEMPLATE,
                        "directory",
                        "/library/anime",
                        _RULE_EXAMPLES_JSON,
                        0,
                        1,
                    ),
                )
                self.assertEqual(
                    restored.execute("PRAGMA user_version").fetchone()[0], 31
                )
                self.assertEqual(
                    restored.execute(
                        "SELECT value FROM backup_sentinel"
                    ).fetchone()[0],
                    "source",
                )
                self.assertIsNone(
                    restored.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='target_only'"
                    ).fetchone()
                )
            finally:
                restored.close()

    def test_saved_rule_roundtrips_through_backup_and_unified_core(self) -> None:
        from app.modules.recognition import formats
        from app.modules.scraper import _parse_release_core
        from tests.test_release_formats import PARENT, filename, save_teaching, teaching

        item, _payload = save_teaching()
        teaching_template = teaching()["draft"]["template"]
        self.assertEqual(item["template"], teaching_template)
        self.assertEqual(
            formats.active_rules()[0]["template"],
            teaching_template,
        )
        before = _parse_release_core(filename(15), PARENT).context
        self.assertEqual((before.normalized_title, before.episode), ("星海航行", 15))

        runtime_paths = self._current_runtime_paths()
        runtime_paths.ensure_writable_dirs()
        archive = runtime_paths.backup_dir / "release-format-roundtrip.zip"
        with db.get_conn() as connection:
            backup.create_backup(
                runtime_paths,
                output=archive,
                source_connection=connection,
                include_settings=False,
            )

        formats.change(item["id"], {"revision": item["revision"]}, delete=True)
        self.assertEqual(formats.active_rules(), ())
        self.assertIsNone(_parse_release_core(filename(15), PARENT).context.episode)

        backup.restore_backup(runtime_paths, archive)

        restored_rules = formats.active_rules()
        self.assertEqual(len(restored_rules), 1)
        self.assertEqual(restored_rules[0]["template"], item["template"])
        self.assertEqual(restored_rules[0]["examples"], item["examples"])
        with db.get_conn() as connection:
            restored_row = connection.execute(
                "SELECT template,examples_json FROM recognition_format_rules "
                "WHERE id=?",
                (item["id"],),
            ).fetchone()
        self.assertEqual(restored_row[0], item["template"])
        self.assertEqual(json.loads(restored_row[1]), item["examples"])

        after = _parse_release_core(filename(15), PARENT).context
        self.assertEqual((after.normalized_title, after.episode), ("星海航行", 15))

    def test_restore_does_not_import_formats_when_cache_module_is_unloaded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mediaflux-release-format-no-import-") as directory:
            root = Path(directory)
            source = self._paths(root / "source")
            target = self._paths(root / "target")
            source.ensure_writable_dirs()
            target.ensure_writable_dirs()
            self._seed_database(source.database_path, value="source")
            archive = backup.create_backup(
                source,
                output=root / "release-formats.zip",
                include_settings=False,
            )

            module_name = "app.modules.recognition.formats"
            previous_module = sys.modules.pop(module_name, None)
            real_import = builtins.__import__

            def guarded_import(name, *args, **kwargs):
                if name in {"app.modules.recognition", module_name}:
                    raise AssertionError("纯备份恢复不得导入发布格式解析器")
                return real_import(name, *args, **kwargs)

            try:
                with patch("builtins.__import__", side_effect=guarded_import):
                    backup.restore_backup(target, archive)
            finally:
                if previous_module is not None:
                    sys.modules[module_name] = previous_module

            restored = sqlite3.connect(target.database_path)
            try:
                self.assertEqual(
                    restored.execute("PRAGMA user_version").fetchone()[0], 31
                )
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
