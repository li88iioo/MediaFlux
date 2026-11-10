"""RSS 批量提交只读取一次有界快照，单项和批量使用同一下载配置投影。"""

from __future__ import annotations

from tests.support import assert_present

from typing import cast

import json
from contextlib import contextmanager
from unittest.mock import patch

from app import database as db
from app.repositories import rss as rss_repo
from app.modules.rss import RSSEngine
from tests.support import InitializedWebTestCase, isolated_test_database
from tests.support import seed_rss_entry_state


class RSSBatchReadAuditTests(InitializedWebTestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.sid = rss_repo.add_rss_subscription(
            "Synthetic feed",
            "https://synthetic.invalid/feed",
            download_method="qb",
            qb_save_path="/synthetic/downloads",
            gy_target_dir="synthetic-target",
            gy_target_dir_name="Synthetic",
        )

    def entries(self, count):
        ids = []
        for n in range(count):
            entry_id = rss_repo.add_rss_entry_with_media(
                self.sid,
                f"Episode {n}",
                f"audit:{n}",
                payload=json.dumps(
                    {"torrent_url": f"magnet:?xt=urn:btih:{n + 1:040x}"}
                ),
            )["id"]
            assert (entry_id) is not None
            ids.append(entry_id)
        return ids

    @contextmanager
    def reads(self):
        original = db.get_conn
        counts = {"connections": 0, "entry_selects": 0}

        def trace(sql):
            if (
                sql.lstrip().upper().startswith("SELECT")
                and "FROM RSS_ENTRIES" in sql.upper()
            ):
                counts["entry_selects"] += 1

        @contextmanager
        def counted():
            counts["connections"] += 1
            with original() as conn:
                conn.set_trace_callback(trace)
                yield conn

        with patch.object(db, "get_conn", side_effect=counted):
            yield counts

    def test_twenty_entry_download_batch_uses_one_read_connection(self):
        ids = self.entries(20)
        engine = RSSEngine()
        with (
            self.reads() as counts,
            patch.object(
                engine,
                "_download_entry",
                return_value={"ok": True, "method": "qBittorrent"},
            ) as submit,
        ):
            result = engine.download_many(ids)
        self.assertEqual(result["success_count"], 20)
        self.assertEqual(submit.call_count, 20)
        self.assertEqual(counts, {"connections": 1, "entry_selects": 1})

    def test_missing_ids_and_repeated_selection_keep_result_positions(self):
        first, second = self.entries(2)
        engine = RSSEngine()
        missing = second + 100

        def submit(entry, **_kwargs):
            return {
                "ok": entry is not None,
                "method": "qBittorrent",
                "error": "missing" if entry is None else "",
            }

        with patch.object(engine, "_download_entry", side_effect=submit):
            result = engine.download_many([second, missing, first, second])
        self.assertEqual(result["total"], 3)
        self.assertEqual([row["id"] for row in result["succeeded"]], [second, first])
        self.assertEqual(result["failed"], [{"id": missing, "error": "missing"}])

    def test_bulk_projection_matches_single_read_and_uses_bounded_sql(self):
        ids = self.entries(2)
        expected = {entry_id: dict(assert_present(rss_repo.get_rss_entry(entry_id))) for entry_id in ids}
        requested = [ids[1], *range(1000, 1501), ids[0], ids[0]]
        with self.reads() as counts:
            actual = rss_repo.get_rss_entries_by_ids(iter(requested))
        self.assertEqual(
            {entry_id: dict(row) for entry_id, row in actual.items()}, expected
        )
        self.assertEqual(counts, {"connections": 1, "entry_selects": 2})
        self.assertEqual(actual[ids[0]]["qb_save_path"], "/synthetic/downloads")
        self.assertEqual(actual[ids[0]]["gy_target_dir"], "synthetic-target")
        self.assertIsNone(rss_repo.get_rss_entry(999))

    def test_empty_batch_does_not_open_a_connection_or_submit(self):
        engine = RSSEngine()
        with self.reads() as counts, patch.object(engine, "_download_entry") as submit:
            self.assertEqual(engine.download_many([])["total"], 0)
            self.assertEqual(rss_repo.get_rss_entries_by_ids([]), {})
        self.assertEqual(counts, {"connections": 0, "entry_selects": 0})
        submit.assert_not_called()

    def test_chunked_read_keeps_one_snapshot_during_subscription_changes(self):
        first, second = self.entries(2)
        original = db.get_conn
        changed = []

        class ReadCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchall(self):
                rows = self.cursor.fetchall()
                if not changed:
                    changed.append(True)
                    with original() as writer:
                        writer.execute(
                            "UPDATE rss_items SET qb_save_path='/changed' WHERE id=?",
                            (self_sid,),
                        )
                return rows

        class ReadConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args):
                cursor = self.connection.execute(sql, *args)
                return ReadCursor(cursor) if sql.startswith("SELECT e.*") else cursor

        @contextmanager
        def reader():
            with original() as connection:
                yield ReadConnection(connection)

        self_sid = self.sid
        with patch.object(db, "get_conn", side_effect=reader):
            rows = rss_repo.get_rss_entries_by_ids([first, *range(1000, 1499), second])
        self.assertEqual(rows[first]["qb_save_path"], "/synthetic/downloads")
        self.assertEqual(rows[second]["qb_save_path"], "/synthetic/downloads")
        get_rss_entry_result = rss_repo.get_rss_entry(second)
        assert get_rss_entry_result is not None
        self.assertEqual(get_rss_entry_result["qb_save_path"], "/changed")

    def test_single_read_preserves_existing_sqlite_id_coercion(self):
        entry_id = self.entries(1)[0]
        for value in (entry_id, str(entry_id), float(entry_id)):
            with self.subTest(value=value):
                get_rss_entry_result_2 = rss_repo.get_rss_entry(cast(int, value))
                assert get_rss_entry_result_2 is not None
                self.assertEqual(get_rss_entry_result_2["id"], entry_id)
        self.assertIsNone(rss_repo.get_rss_entry(cast(int, None)))
        self.assertIsNone(rss_repo.get_rss_entry(-1))

    def test_web_entries_keep_unprocessed_first_and_filters_after_reload(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        from tests.test_rss_stats_state import RSSStatsStateTests

        first, second = self.entries(2)
        seed_rss_entry_state(second, "downloaded")
        with TestClient(create_app(start_background=False)) as client:
            self.assertEqual(client.get("/api/rss/entries").status_code, 401)
            csrf = RSSStatsStateTests._csrf(client.get("/login").text)
            login = client.post("/login", data={
                "username": "admin", "password": "123456", "csrf_token": csrf,
            }, follow_redirects=False)
            self.assertEqual(login.status_code, 302)
            with patch.object(rss_repo, "list_rss_entries", wraps=rss_repo.list_rss_entries) as listing:
                for _ in range(2):
                    response = client.get("/api/rss/entries", params={"subscription_id": self.sid, "q": "  Episode  "})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual([row["id"] for row in response.json()["items"]], [first, second])
                    listing.assert_called_with(sub_id=self.sid, status=None, keyword="Episode", limit=100, offset=0, order="unprocessed_first")
                response = client.get("/api/rss/entries", params={"subscription_id": self.sid, "status": "downloaded", "q": "Episode"})
                self.assertEqual([row["id"] for row in response.json()["items"]], [second])
                listing.assert_called_with(sub_id=self.sid, status="downloaded", keyword="Episode", limit=100, offset=0, order="unprocessed_first")
                self.assertEqual(client.get("/api/rss/entries?subscription_id=bad").status_code, 400)

    def test_authenticated_web_batch_retries_both_backends_without_replaying_unknown(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        from app.modules import download_dispatcher as dispatcher
        from tests.test_rss_stats_state import RSSStatsStateTests

        qb_entry = self.entries(1)[0]
        cloud_sub = rss_repo.add_rss_subscription("Cloud", "https://synthetic.invalid/cloud", download_method="guangya", gy_target_dir="chosen-dir")
        cloud_entries = [int(rss_repo.add_rss_entry_with_media(
            cloud_sub, f"cloud-{n}", f"cloud-{n}", payload=json.dumps({"torrent_url": f"magnet:?xt=urn:btih:{n:040x}"}),
        )["id"]) for n in (100, 101)]
        rss_repo.record_rss_entry_failure(qb_entry, "qb_unavailable", True)
        rss_repo.record_rss_entry_failure(cloud_entries[0], "guangya_manifest_unavailable", True)
        rss_repo.record_rss_entry_failure(cloud_entries[1], "guangya_outcome_unknown", False)
        with TestClient(create_app(start_background=False)) as client:
            csrf = RSSStatsStateTests._csrf(client.get("/login").text)
            client.post("/login", data={"username": "admin", "password": "123456", "csrf_token": csrf}, follow_redirects=False)
            headers = {"X-CSRF-Token": RSSStatsStateTests._csrf(client.get("/rss").text)}
            with patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "qb"}) as qb, patch.object(dispatcher, "_submit_guangya", return_value={"ok": True, "task_id": "gy"}) as gy:
                response = client.post("/api/rss/entries/batch-download", headers=headers, json={"entry_ids": [qb_entry, *cloud_entries]})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()["result"]
                self.assertEqual(result["success_count"], 2, result)
                self.assertEqual(result["failure_count"], 1, result)
                qb.assert_called_once()
                gy.assert_called_once()
                self.assertEqual(gy.call_args.kwargs["target_dir_id"], "chosen-dir")
                repeated = client.post("/api/rss/entries/batch-download", headers=headers, json={"entry_ids": [qb_entry, cloud_entries[0]]})
                self.assertEqual(repeated.status_code, 200)
                self.assertEqual(qb.call_count, 1)
                self.assertEqual(gy.call_count, 1)
        get_rss_entry_result_3 = rss_repo.get_rss_entry(cloud_entries[1])
        assert get_rss_entry_result_3 is not None
        self.assertEqual(get_rss_entry_result_3["failure_code"], "guangya_outcome_unknown")
