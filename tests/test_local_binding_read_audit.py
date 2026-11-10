"""媒体库控制中心一次读取来源/目标绑定，避免 N+1 和跨版本拼接。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest.mock import patch

from app import database as db
from app.repositories import local_media as local_media_repo
from app.routes import media_libraries_api as api
from tests.support import IsolatedDatabaseTestCase


class LocalBindingReadAuditTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.owner = self._testMethodName

    def _source(self, index, *, enabled=True, owner=None):
        return local_media_repo.create_local_media_source(
            name=f"before-{index}",
            qb_profile="",
            qb_path_prefix="",
            local_root=f"/synthetic/{self.owner}/{index}",
            owner=owner or self.owner,
            enabled=int(enabled),
        )

    def _binding(self, source_id, category="movie", *, owner=None):
        return local_media_repo.upsert_local_library_target(
            source_id,
            category,
            f"/before/{source_id}/{category}",
            provider="jellyfin",
            library_id="movies",
            library_name="电影",
            server_path=f"/server/{source_id}/{category}",
            owner=owner or self.owner,
        )

    def _read(self):
        with patch.object(api, "_OWNER", self.owner):
            return api._local_bindings()

    def test_normal_projection_order_and_disabled_sources_are_preserved(self):
        first = self._source(1, enabled=False)
        second = self._source(2)
        for source_id, category in ((second, "tv"), (first, "tv"), (first, "movie")):
            self._binding(source_id, category)
        rows = self._read()
        self.assertEqual(
            [(r["source_id"], r["category"]) for r in rows],
            [(first, "movie"), (first, "tv"), (second, "tv")],
        )
        self.assertEqual(
            rows[0],
            {
                "source_id": first,
                "source_name": "before-1",
                "category": "movie",
                "category_label": "电影",
                "local_path": f"/before/{first}/movie",
                "provider": "jellyfin",
                "library_id": "movies",
                "library_name": "电影",
                "server_path": f"/server/{first}/movie",
            },
        )

    def test_twenty_sources_use_one_connection_and_one_select(self):
        for index in range(20):
            self._binding(self._source(index))
        original = db.get_conn
        connections = []
        statements = []

        @contextmanager
        def traced():
            with original() as conn:
                connections.append(conn)
                conn.set_trace_callback(statements.append)
                yield conn

        with patch.object(db, "get_conn", traced):
            rows = self._read()
        self.assertEqual(len(rows), 20)
        self.assertEqual(len(connections), 1)
        self.assertEqual(
            len([s for s in statements if s.lstrip().upper().startswith("SELECT")]), 1
        )
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_source_and_target_share_snapshot_during_concurrent_config_change(self):
        for index in range(2):
            self._binding(self._source(index))
        original = db.get_conn
        changed = False

        @contextmanager
        def interleaved():
            nonlocal changed
            with original() as conn:

                def row_factory(cursor, values):
                    nonlocal changed
                    row = sqlite3.Row(cursor, values)
                    if not changed:
                        changed = True
                        with original() as writer:
                            writer.execute(
                                "UPDATE local_media_sources SET name='after-'||id WHERE owner=?",
                                (self.owner,),
                            )
                            writer.execute(
                                "UPDATE local_library_targets SET path='/after' WHERE owner=?",
                                (self.owner,),
                            )
                    return row

                conn.row_factory = row_factory
                yield conn

        with patch.object(db, "get_conn", interleaved):
            snapshot = self._read()
        self.assertTrue(changed)
        self.assertTrue(
            all(row["source_name"].startswith("before-") for row in snapshot)
        )
        self.assertTrue(
            all(row["local_path"].startswith("/before/") for row in snapshot)
        )
        self.assertEqual(
            [(r["source_name"], r["local_path"]) for r in self._read()],
            [(f"after-{r['source_id']}", "/after") for r in snapshot],
        )

    def test_empty_unbound_and_other_owner_records_do_not_leak_into_bindings(self):
        self._source(1)
        self._binding(
            self._source(2, owner=self.owner + "-other"), owner=self.owner + "-other"
        )
        self.assertEqual(self._read(), [])
        source_id = self._source(3)
        self._binding(source_id)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE local_library_targets SET server_path='' WHERE source_id=?",
                (source_id,),
            )
        self.assertEqual(self._read()[0]["server_path"], "")
