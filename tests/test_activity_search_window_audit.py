"""活动搜索应选全域最近更新记录，不能先按各表ID截断再补排序。"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from unittest.mock import patch
from typing import Any

from app import database as db
from app import sdk
from app.repositories import activity
from tests.support import IsolatedDatabaseTestCase


class ActivitySearchWindowAuditTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            for table in (
                "download_request_keys",
                "download_log",
                "download_requests",
                "local_media_task_items",
                "local_media_tasks",
                "local_media_sources",
                "organize_log",
            ):
                conn.execute(f"DELETE FROM {table}")
            self.source = conn.execute(
                "INSERT INTO local_media_sources(name,local_root,created_at,updated_at) "
                "VALUES('audit','/fixture','2026-01-01 00:00:00','2026-01-01 00:00:00')"
            ).lastrowid

    def _add(self, kind, title, updated, *, created="2026-01-01 00:00:00"):
        key = uuid.uuid4().hex
        with db.get_conn() as conn:
            if kind == "download":
                return conn.execute(
                    "INSERT INTO download_requests(request_key,kind,title,status,created_at,updated_at) "
                    "VALUES(?,'magnet',?,'downloading',?,?)",
                    (key, title, created, updated),
                ).lastrowid
            if kind == "organize":
                return conn.execute(
                    "INSERT INTO organize_log(source,original_path,new_path,title,status,created_at,updated_at) "
                    "VALUES('guangya','/fixture/source','/fixture/target',?,'success',?,?)",
                    (title, created, updated),
                ).lastrowid
            return conn.execute(
                "INSERT INTO local_media_tasks(source_id,content_path,operation_token,title,status,created_at,updated_at) "
                "VALUES(?,'/fixture',?,?,'waiting_stable',?,?)",
                (self.source, key, title, created, updated),
            ).lastrowid

    def test_old_ids_updated_latest_are_not_hidden_by_newer_ids(self):
        for kind in ("download", "organize", "local_media"):
            with self.subTest(kind=kind):
                wanted = self._add(kind, f"{kind}-wanted", "2026-01-09 00:00:00")
                for index in range(30):
                    self._add(kind, f"{kind}-old-{index}", "2026-01-02 00:00:00")
                page = activity.search_page(query=kind, limit=1)
                rows = page["items"]
                self.assertEqual((rows[0]["kind"], rows[0]["id"]), (kind, wanted))
                self.assertEqual((page["total"], page["returned"]), (31, 1))
                self.assertTrue(page["has_more"])
                self.assertEqual(page["next_offset"], 1)

    def test_three_domains_share_one_statement_and_bounded_global_window(self):
        for kind in ("download", "organize", "local_media"):
            for index in range(25):
                self._add(kind, f"fixture-{index}", f"2026-01-{index + 1:02} 00:00:00")
        original = db.get_conn
        statements = []

        @contextmanager
        def traced():
            with original() as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        with patch.object(db, "get_conn", traced):
            page = activity.search_page(query="fixture")
        rows = page["items"]
        self.assertEqual((len(rows), page["total"], page["returned"]), (50, 75, 50))
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 50)
        self.assertEqual(
            sum(sql.startswith(("SELECT", "WITH")) for sql in statements), 1
        )
        self.assertEqual(
            set(rows[0]), {"kind", "id", "title", "status", "created_at", "updated_at"}
        )
        last = activity.search_page(query="fixture", offset=page["next_offset"])
        self.assertEqual((last["total"], last["returned"]), (75, 25))
        self.assertFalse(last["has_more"])
        self.assertIsNone(last["next_offset"])
        all_items = rows + last["items"]
        self.assertEqual(len({(row["kind"], row["id"]) for row in all_items}), 75)
        wide = activity.search_page(query="fixture", limit=75)
        self.assertEqual(wide["items"], all_items)  # 50 是默认页大小，不是业务上限。
        self.assertEqual(
            (wide["total"], wide["returned"], wide["has_more"]), (75, 75, False)
        )

    def test_source_reads_cannot_mix_a_later_cross_domain_insert(self):
        first = self._add("download", "fixture-first", "2026-01-02 00:00:00")
        original = db.get_conn
        fired = False

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchall(cursor_self):
                nonlocal fired
                rows = cursor_self.cursor.fetchall()
                if not fired:
                    fired = True
                    self._add("local_media", "fixture-later", "2026-01-09 00:00:00")
                return rows

        class Connection:
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def execute(self, sql, *args):
                cursor = self.conn.execute(sql, *args)
                return (
                    Cursor(cursor)
                    if sql.startswith(("SELECT", "WITH")) and not fired
                    else cursor
                )

        @contextmanager
        def hooked():
            with original() as conn:
                yield Connection(conn)

        with patch.object(db, "get_conn", hooked):
            page = activity.search_page(query="fixture", limit=20)
        self.assertTrue(fired)
        self.assertEqual(
            [(row["kind"], row["id"]) for row in page["items"]], [("download", first)]
        )
        self.assertEqual(
            (page["total"], page["returned"], page["has_more"]), (1, 1, False)
        )
        later = activity.search_page(query="fixture", limit=20)
        self.assertEqual((later["total"], later["returned"]), (2, 2))

    def test_public_positions_reference_actual_newest_identity_and_has_more(self):
        wanted = self._add("download", "fixture-newest", "2026-01-09 00:00:00")
        for index in range(25):
            self._add("download", f"fixture-{index}", "2026-01-02 00:00:00")
        result = sdk.activity.search(query="fixture", limit=1)
        self.assertEqual(result["items"][0]["title"], "fixture-newest")
        self.assertTrue(result["has_more"])
        self.assertEqual(
            [{"kind": row["kind"], "id": row["id"]} for row in result["items"]], [{"kind": "download", "id": wanted}]
        )
        first_twenty = sdk.activity.search(query="fixture", limit=20)
        self.assertEqual(len(first_twenty["items"]), 20)
        self.assertTrue(first_twenty["has_more"])
        self.assertEqual(first_twenty["total"], 26)
        self.assertEqual(first_twenty["items"][0]["id"], wanted)

    def test_literal_query_empty_timestamp_and_empty_results_keep_contract(self):
        wanted = self._add(
            "organize", "100%_fixture", "", created="2026-01-09 00:00:00"
        )
        self._add("download", "other fixture", "2026-01-02 00:00:00")
        matched = activity.search_page(query="%_", limit=10)
        self.assertEqual(matched["items"][0]["id"], wanted)
        self.assertEqual(matched["items"][0]["updated_at"], "2026-01-09T00:00:00")
        self.assertEqual(matched["total"], 1)
        self.assertEqual(
            activity.search_page(query="missing", limit=10),
            {
                "items": [],
                "total": 0,
                "returned": 0,
                "has_more": False,
                "next_offset": None,
            },
        )
        page = activity.search_page(query="", limit=1)
        self.assertEqual(
            (page["total"], page["returned"], page["next_offset"]), (2, 1, 1)
        )
        self.assertEqual(page["items"][0]["id"], wanted)
        last = activity.search_page(limit=1, offset=1)
        self.assertEqual(last["items"][0]["kind"], "download")
        self.assertEqual(
            (last["total"], last["returned"], last["has_more"]), (2, 1, False)
        )
        for offset in (2, 999):
            with self.subTest(offset=offset):
                self.assertEqual(
                    activity.search_page(offset=offset),
                    {
                        "items": [],
                        "total": 2,
                        "returned": 0,
                        "has_more": False,
                        "next_offset": None,
                    },
                )

    def test_equal_time_and_id_keep_stable_kind_order_on_repeated_reads(self):
        for kind in ("download", "organize", "local_media"):
            identifier = self._add(kind, "tie-fixture", "2026-01-09 00:00:00")
            table = {
                "download": "download_requests",
                "organize": "organize_log",
                "local_media": "local_media_tasks",
            }[kind]
            with db.get_conn() as conn:
                conn.execute(f"UPDATE {table} SET id=999999 WHERE id=?", (identifier,))
        expected = ["download", "organize", "local_media"]
        for _ in range(3):
            self.assertEqual(
                [
                    row["kind"]
                    for row in activity.search_page(query="tie-fixture", limit=3)[
                        "items"
                    ]
                ],
                expected,
            )
        pages = [
            activity.search_page(query="tie-fixture", limit=1, offset=index)
            for index in range(3)
        ]
        self.assertEqual([page["items"][0]["kind"] for page in pages], expected)
        self.assertEqual([page["total"] for page in pages], [3, 3, 3])

    def test_shared_page_keeps_identity_but_projects_private_text_and_fields(self):
        identifier = self._add(
            "download", "token=search-secret /fixture/private", "invalid-date"
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE download_requests SET source_value=?,torrent_data=?,chat_id=?,status=? WHERE id=?",
                (
                    "https://source.invalid/private?token=secret",
                    b"private-torrent",
                    "private-chat",
                    "/private-status",
                    identifier,
                ),
            )
        page = activity.search_page(query="token=")
        self.assertEqual((page["total"], page["returned"]), (1, 1))
        self.assertEqual(
            page["items"],
            [
                {
                    "kind": "download",
                    "id": identifier,
                    "title": "",
                    "status": "unknown",
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "",
                }
            ],
        )
        public = json.dumps(page, allow_nan=False)
        self.assertNotIn("secret", public)
        self.assertNotIn("private", public)
        adapted = sdk.activity.search(query="token=", limit=1)
        self.assertEqual(adapted["items"][0]["title"], "")
        self.assertEqual(adapted["items"][0]["status"], "unknown")
        self.assertEqual(adapted["items"][0]["updated_at"], "")
        self.assertEqual(adapted["items"][0]["id"], identifier)

    def test_shared_page_rejects_invalid_pagination_before_reading_database(self):
        with patch.object(db, "get_conn") as read:
            arguments: dict[str, Any]
            for arguments in (
                {"query": None},
                {"limit": 0},
                {"limit": -1},
                {"limit": True},
                {"limit": 1.5},
                {"offset": -1},
                {"offset": True},
                {"offset": "1"},
            ):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    activity.search_page(**arguments)
        read.assert_not_called()
