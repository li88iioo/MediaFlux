"""播放记录列表、会话指标及故障摘要在真实并发写入时仍属于同一快照。"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from app import database as db
from app.repositories import media_proxy as repository
from tests.support import IsolatedDatabaseTestCase


class MediaProxySnapshotAuditTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_proxy_playback_records")
            conn.execute("DELETE FROM media_proxy_playback_sessions")
        # 先完成维护事务，覆盖日常读取不触发清理 DML 的真实路径。
        repository.list_media_proxy_playback_records()

    def _record(self, *, session="", status=302, stage="", route="guangya_direct"):
        return repository.record_media_proxy_playback_attempt(
            instance_id=1,
            playback_session_key=session,
            media_item_id="fixture-item",
            route_class=route,
            method="GET",
            status_code=status,
            source="guangya",
            total_latency_ms=20,
            upstream_latency_ms=5,
            failure_stage=stage,
        )

    @contextmanager
    def _after_read(self, sql_prefix, callback):
        original = db.get_conn
        state = {"fired": False}

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def _fire(self):
                if not state["fired"]:
                    state["fired"] = True
                    callback()

            def fetchone(self):
                row = self.cursor.fetchone()
                self._fire()
                return row

            def fetchall(self):
                rows = self.cursor.fetchall()
                self._fire()
                return rows

        class Connection:
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def execute(self, sql, *args, **kwargs):
                cursor = self.conn.execute(sql, *args, **kwargs)
                if sql.startswith(sql_prefix) and not state["fired"]:
                    return Cursor(cursor)
                return cursor

        @contextmanager
        def hooked():
            with original() as conn:
                yield Connection(conn)

        with patch.object(db, "get_conn", hooked):
            yield
        self.assertTrue(state["fired"])

    def test_record_count_and_page_remain_same_snapshot_during_insert(self):
        first = self._record()
        with self._after_read(
            "SELECT COUNT(*) AS count FROM media_proxy_playback_records",
            self._record,
        ):
            page = db.list_media_proxy_playback_records(instance_id=1)
        self.assertEqual(page["total"], 1)
        self.assertEqual([row["id"] for row in page["items"]], [first])
        self.assertEqual(page["items"][0]["internal_latency_ms"], 15)
        self.assertEqual(repository.list_media_proxy_playback_records()["total"], 2)

    def test_session_rows_and_stage_metrics_do_not_mix_later_requests(self):
        self._record(session="fixture-session")
        with self._after_read(
            "SELECT id,instance_id,media_item_id,media_source_id,media_name,guangya_file_id,",
            lambda: self._record(session="fixture-session"),
        ):
            page = db.list_media_proxy_playback_sessions(instance_id=1)
        item = page["items"][0]
        self.assertEqual(item["request_count"], 1)
        self.assertEqual(item["redirect_request_count"], 1)
        self.assertEqual(item["average_redirect_latency_ms"], 20)
        updated = repository.list_media_proxy_playback_sessions()["items"][0]
        self.assertEqual(updated["request_count"], 2)
        self.assertEqual(updated["redirect_request_count"], 2)

    def test_failure_summary_totals_and_groups_share_one_snapshot(self):
        self._record(status=502, stage="signed_url")
        with self._after_read(
            "SELECT COUNT(*) AS total,",
            lambda: self._record(status=504, stage="upstream_timeout"),
        ):
            summary = db.get_media_proxy_playback_failure_summary(instance_id=1)
        self.assertEqual(summary["total_recorded"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(
            summary["failure_stages"], [{"stage": "signed_url", "count": 1}]
        )
        self.assertEqual(sum(row["count"] for row in summary["route_classes"]), 1)
        self.assertEqual(
            repository.get_media_proxy_playback_failure_summary()["failed"], 2
        )

    def test_native_failure_rows_and_summary_share_one_snapshot(self):
        first = self._record(status=502, stage="signed_url")
        with self._after_read("SELECT COUNT(*) AS total,", lambda: self._record(status=504, stage="timeout")):
            facts = repository.media_proxy_failure_facts(instance_id=1)
        self.assertEqual(facts["total"], facts["failed"])
        self.assertEqual(facts["total"], 1)
        self.assertEqual([row["id"] for row in facts["items"]], [first])
        self.assertEqual(repository.media_proxy_failure_facts(instance_id=1)["total"], 2)

    def test_batch_session_metrics_keep_one_group_query_and_normal_pagination(self):
        for index in range(105):
            self._record(session=f"fixture-{index}")
        statements = []
        original = db.get_conn

        @contextmanager
        def traced():
            with original() as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        with patch.object(db, "get_conn", traced):
            page = repository.list_media_proxy_playback_sessions(page=2, page_size=100)
        self.assertEqual(
            (page["total"], page["page"], page["page_size"]), (105, 2, 100)
        )
        self.assertEqual(len(page["items"]), 5)
        self.assertEqual(sum("GROUP BY session_id" in sql for sql in statements), 1)
        self.assertEqual(sum(sql.startswith("SELECT") for sql in statements), 4)
        self.assertEqual(page["unlinked_total"], 0)
