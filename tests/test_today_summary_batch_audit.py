"""今日业务总量不受标题展示窗口限制，跨模块读取同一个快照。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from app import database as db
from app.repositories import rss as rss_repo
from app.repositories import media_subscriptions as media_subscriptions_repo
from app.repositories import media_experience as repository
from tests.support import IsolatedDatabaseTestCase


class TodaySummaryBatchAuditTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        self.day = datetime.now().astimezone().strftime("%Y-%m-%d")
        self.stamp = f"{self.day} 12:00:00"
        with db.get_conn() as conn:
            for table in (
                "media_subscription_runs",
                "local_media_tasks",
                "rss_entries",
                "download_log",
                "media_subscriptions",
                "local_media_sources",
                "rss_items",
            ):
                conn.execute(f"DELETE FROM {table}")
            self.source = conn.execute(
                "INSERT INTO local_media_sources(name,local_root,created_at,updated_at) VALUES('audit','/fixture',?,?)",
                (self.stamp, self.stamp),
            ).lastrowid
        self.sid = media_subscriptions_repo.add_media_subscription(
            provider="tmdb",
            external_id="909",
            tmdb_id="909",
            media_type="tv",
            title="Subscription",
            action="confirm",
            download_target="guangya",
            sites=("mikan",),
        )
        self.rss_id = rss_repo.add_rss_subscription("audit", "https://fixture.invalid/rss")

    def _seed(self, count=120):
        with db.get_conn() as conn:
            for index in range(count):
                conn.execute(
                    "INSERT INTO media_subscription_runs(subscription_id,status,started_at,finished_at) VALUES(?,?,?,?)",
                    (
                        self.sid,
                        "missing" if index % 2 else "satisfied",
                        self.stamp,
                        self.stamp,
                    ),
                )
                conn.execute(
                    "INSERT INTO local_media_tasks(source_id,content_path,status,operation_token,title,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        self.source,
                        f"/fixture/{index}",
                        "completed" if index % 2 else "failed",
                        str(index),
                        f"Local {index}",
                        self.stamp,
                        self.stamp,
                        self.stamp,
                    ),
                )
                conn.execute(
                    "INSERT INTO rss_entries(rss_item_id,title,status,created_at,processed_at) VALUES(?,?,?,?,?)",
                    (
                        self.rss_id,
                        f"RSS {index}",
                        "downloaded" if index % 2 else "pending",
                        self.stamp,
                        self.stamp,
                    ),
                )
                conn.execute(
                    "INSERT INTO download_log(source,title,status,created_at,updated_at,completed_at) VALUES('qb',?,?,?,?,?)",
                    (
                        f"Download {index}",
                        "success" if index % 2 else "failed",
                        self.stamp,
                        self.stamp,
                        self.stamp,
                    ),
                )

    def test_all_four_sources_count_complete_batch_with_bounded_titles(self) -> None:
        self._seed()
        result = repository.today_content_summary()
        self.assertEqual(result["event_count"], 480)
        for key in (
            "subscription_runs",
            "local_media_tasks",
            "rss_entries",
            "downloads",
        ):
            self.assertEqual(sum(result[key].values()), 120, key)
            self.assertEqual(sorted(result[key].values()), [60, 60], key)
        self.assertEqual(
            result["content_titles"],
            ["Subscription", *[f"Local {i}" for i in range(119, 112, -1)]],
        )
        self.assertEqual(result["local_date"], self.day)

    def test_empty_legacy_timestamps_fall_back_but_real_terminal_dates_win(
        self,
    ) -> None:
        previous = (datetime.now().astimezone() - timedelta(days=1)).strftime(
            "%Y-%m-%d 12:00:00"
        )
        future = (datetime.now().astimezone() + timedelta(days=1)).strftime(
            "%Y-%m-%d 12:00:00"
        )
        with db.get_conn() as conn:
            for terminal, fallback in (
                ("", self.stamp),
                (None, self.stamp),
                (previous, self.stamp),
                (future, self.stamp),
            ):
                conn.execute(
                    "INSERT INTO rss_entries(rss_item_id,title,status,created_at,submitted_at,processed_at) VALUES(?,?,'pending',?,'',?)",
                    (self.rss_id, "Fallback", fallback, terminal),
                )
                conn.execute(
                    "INSERT INTO download_log(source,title,status,created_at,updated_at,completed_at) VALUES('qb','Fallback','submitted',?,'',?)",
                    (fallback, terminal),
                )
        result = repository.today_content_summary()
        self.assertEqual(result["rss_entries"], {"pending": 2})
        self.assertEqual(result["downloads"], {"submitted": 2})
        self.assertEqual(result["event_count"], 4)
        self.assertEqual(result["content_titles"], ["Fallback"])

    def test_large_batches_do_not_materialize_all_events(self) -> None:
        self._seed()
        connect = db.get_conn
        materialized = 0

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def __iter__(self):
                nonlocal materialized
                for row in self.cursor:
                    materialized += 1
                    yield row

            def close(self):
                self.cursor.close()

            def fetchall(self):
                nonlocal materialized
                rows = self.cursor.fetchall()
                materialized += len(rows)
                return rows

        class Connection:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, parameters=()):
                return Cursor(self.conn.execute(sql, parameters))

        @contextmanager
        def hooked():
            with connect() as conn:
                yield Connection(conn)

        with patch.object(db, "get_conn", hooked):
            result = repository.today_content_summary()
        self.assertEqual(result["event_count"], 480)
        self.assertEqual(materialized, 65)

    def test_cross_source_counts_share_one_read_snapshot(self) -> None:
        self._seed(2)
        connect = db.get_conn
        changed = False

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def __iter__(self):
                return iter(self.cursor)

            def close(self):
                self.cursor.close()

            def fetchall(self):
                nonlocal changed
                rows = self.cursor.fetchall()
                if not changed:
                    self.cursor.close()
                    changed = True
                    with connect() as writer:
                        writer.execute("DELETE FROM download_log")
                return rows

        class Connection:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, parameters=()):
                return Cursor(self.conn.execute(sql, parameters))

        @contextmanager
        def hooked():
            with connect() as conn:
                yield Connection(conn)

        with patch.object(db, "get_conn", hooked):
            result = repository.today_content_summary()
        self.assertTrue(changed)
        self.assertEqual(result["event_count"], 8)
        self.assertEqual(result["downloads"], {"success": 1, "failed": 1})
        self.assertEqual(repository.today_content_summary()["event_count"], 6)

    def test_title_whitespace_dedup_and_unknown_status_mapping_stay_compatible(
        self,
    ) -> None:
        with db.get_conn() as conn:
            for title in ("Title", "\tTitle\u3000", " ", "\n", "Other"):
                conn.execute(
                    "INSERT INTO download_log(source,title,status,created_at) VALUES('qb',?,'historical_state',?)",
                    (title, self.stamp),
                )
        result = repository.today_content_summary()
        self.assertEqual(result["event_count"], 5)
        self.assertEqual(result["downloads"], {"processing": 5})
        self.assertEqual(result["content_titles"], ["Other", "Title"])
