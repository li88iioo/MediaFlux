"""v25→v26 只增加 STRM 迁移凭据；索引与凭据必须同事务提交/回滚。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from app import database as db
from app.repositories import strm as strm_repo
from app import database_migrations
from tests.support import isolated_test_database


def put(
    path="/root/光鸭云盘/Old.strm", file_id="video", fingerprint="sha256:" + "1" * 64
):
    strm_repo.upsert_strm_index(
        "guangya:source", file_id, "e", 100, "E.mkv", path, fingerprint
    )


class StrmPathCleanupMigrationTests(unittest.TestCase):
    def test_v25_upgrade_preserves_original_index_and_repeated_init_preserves_receipt(
        self,
    ):
        with isolated_test_database():
            put()
            original = dict(strm_repo.list_strm_index("guangya:source")[0])
            with db.get_conn() as conn:
                conn.execute("DROP TABLE strm_path_cleanup")
                conn.execute("PRAGMA user_version=25")
            db.init_db()
            self.assertEqual(dict(strm_repo.list_strm_index("guangya:source")[0]), original)
            self.assertEqual(strm_repo.list_strm_path_cleanup("guangya:source"), [])
            put("/root/光鸭云盘/New.strm")
            receipt = [dict(row) for row in strm_repo.list_strm_path_cleanup("guangya:source")]
            db.init_db()
            self.assertEqual(
                [dict(row) for row in strm_repo.list_strm_path_cleanup("guangya:source")],
                receipt,
            )
            with db.get_conn() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(
                    conn.execute("PRAGMA foreign_key_check").fetchall(), []
                )

    def test_failed_migration_rolls_back_new_table_and_version(self):
        with isolated_test_database():
            put()
            with db.get_conn() as conn:
                conn.execute("DROP TABLE strm_path_cleanup")
                conn.execute("PRAGMA user_version=25")
            real_migrate = database_migrations._SCHEMA_MIGRATIONS[25]

            def failing(conn):
                real_migrate(conn)
                raise RuntimeError("isolated migration failure")

            with (
                patch.dict(database_migrations._SCHEMA_MIGRATIONS, {25: failing}),
                self.assertRaises(RuntimeError),
            ):
                db.init_db()
            with db.get_conn() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 25)
                self.assertIsNone(
                    conn.execute(
                        "SELECT name FROM sqlite_master WHERE name='strm_path_cleanup'"
                    ).fetchone()
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM strm_index").fetchone()[0], 1
                )
            db.init_db()
            self.assertEqual(strm_repo.list_strm_path_cleanup("guangya:source"), [])

    def test_failed_index_upsert_also_rolls_back_cleanup_receipt(self):
        with isolated_test_database():
            put()
            with db.get_conn() as conn:
                conn.execute(
                    "CREATE TRIGGER reject_move BEFORE UPDATE ON strm_index BEGIN SELECT RAISE(ABORT, 'isolated'); END"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                put("/root/光鸭云盘/New.strm")
            self.assertEqual(
                strm_repo.list_strm_index("guangya:source")[0]["strm_path"],
                "/root/光鸭云盘/Old.strm",
            )
            self.assertEqual(strm_repo.list_strm_path_cleanup("guangya:source"), [])

    def test_batch_receipts_only_capture_displaced_known_paths(self):
        with isolated_test_database():
            put(file_id="moved")
            put(file_id="unchanged", path="/root/光鸭云盘/Keep.strm")
            put(file_id="unknown", path="/root/光鸭云盘/Unknown.strm", fingerprint="")
            strm_repo.upsert_strm_index_batch(
                "guangya:source",
                [
                    {"file_id": "moved", "strm_path": "/root/光鸭云盘/New.strm"},
                    {"file_id": "unchanged", "strm_path": "/root/光鸭云盘/Keep.strm"},
                    {
                        "file_id": "unknown",
                        "strm_path": "/root/光鸭云盘/UnknownNew.strm",
                    },
                ],
            )
            rows = strm_repo.list_strm_path_cleanup("guangya:source")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["file_id"], "moved")
            self.assertEqual(rows[0]["strm_path"], "/root/光鸭云盘/Old.strm")

    def test_multiple_moves_keep_all_distinct_old_paths_until_ack(self):
        with isolated_test_database():
            put()
            put("/root/光鸭云盘/Middle.strm")
            put("/root/光鸭云盘/New.strm")
            rows = strm_repo.list_strm_path_cleanup("guangya:source")
            self.assertEqual(
                {row["strm_path"] for row in rows},
                {"/root/光鸭云盘/Old.strm", "/root/光鸭云盘/Middle.strm"},
            )
            page = strm_repo.list_strm_path_cleanup("guangya:source", limit=1)
            following = strm_repo.list_strm_path_cleanup(
                "guangya:source", after_id=page[0]["id"]
            )
            self.assertEqual(len(following), 1)
            strm_repo.delete_strm_path_cleanup([page[0]["id"]])
            self.assertEqual(len(strm_repo.list_strm_path_cleanup("guangya:source")), 1)

    def test_duplicate_id_in_batch_preserves_intermediate_path_and_fingerprint(self):
        with isolated_test_database():
            put()
            middle_fp = "sha256:" + "2" * 64
            strm_repo.upsert_strm_index_batch(
                "guangya:source",
                [
                    {
                        "file_id": "video",
                        "strm_path": "/root/光鸭云盘/Middle.strm",
                        "content_fingerprint": middle_fp,
                    },
                    {
                        "file_id": "video",
                        "strm_path": "/root/光鸭云盘/New.strm",
                        "content_fingerprint": "sha256:" + "3" * 64,
                    },
                ],
            )
            rows = strm_repo.list_strm_path_cleanup("guangya:source")
            self.assertEqual(
                {(r["strm_path"], r["content_fingerprint"]) for r in rows},
                {
                    ("/root/光鸭云盘/Old.strm", "sha256:" + "1" * 64),
                    ("/root/光鸭云盘/Middle.strm", middle_fp),
                },
            )

    def test_conflict_replacement_preserves_victim_receipt_single_and_batch(self):
        for batch in (False, True):
            with self.subTest(batch=batch), isolated_test_database():
                put(file_id="old")
                if batch:
                    strm_repo.upsert_strm_index_batch(
                        "guangya:source",
                        [
                            {
                                "file_id": "new",
                                "strm_path": "/root/光鸭云盘/New.strm",
                                "conflicting_file_ids": ["old"],
                            }
                        ],
                    )
                else:
                    strm_repo.upsert_strm_index(
                        "guangya:source",
                        "new",
                        "e",
                        100,
                        "E.mkv",
                        "/root/光鸭云盘/New.strm",
                        conflicting_file_ids=["old"],
                    )
                rows = strm_repo.list_strm_path_cleanup("guangya:source")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["file_id"], "old")
                self.assertEqual(rows[0]["strm_path"], "/root/光鸭云盘/Old.strm")

    def test_conflict_delete_failure_rolls_back_index_and_all_receipts(self):
        for batch in (False, True):
            with self.subTest(batch=batch), isolated_test_database():
                put(file_id="old")
                with db.get_conn() as conn:
                    conn.execute(
                        "CREATE TRIGGER reject_delete BEFORE DELETE ON strm_index BEGIN SELECT RAISE(ABORT, 'isolated delete failure'); END"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    if batch:
                        strm_repo.upsert_strm_index_batch(
                            "guangya:source",
                            [
                                {
                                    "file_id": "new",
                                    "strm_path": "/root/光鸭云盘/New.strm",
                                    "conflicting_file_ids": ["old"],
                                }
                            ],
                        )
                    else:
                        strm_repo.upsert_strm_index(
                            "guangya:source",
                            "new",
                            "e",
                            100,
                            "E.mkv",
                            "/root/光鸭云盘/New.strm",
                            conflicting_file_ids=["old"],
                        )
                self.assertEqual(strm_repo.list_strm_path_cleanup("guangya:source"), [])
                self.assertEqual(
                    [row["file_id"] for row in strm_repo.list_strm_index("guangya:source")],
                    ["old"],
                )
