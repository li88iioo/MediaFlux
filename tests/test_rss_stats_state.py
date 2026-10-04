from __future__ import annotations

import json
import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class RSSStatsStateTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_entries")
            conn.execute("DELETE FROM rss_items")

    @staticmethod
    def _csrf(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def _seed(self) -> tuple[int, list[int]]:
        active = db.add_rss_subscription("active", "https://example.invalid/a", refresh_interval_minutes=10)
        db.add_rss_subscription("disabled", "https://example.invalid/b", enabled=0, refresh_interval_minutes=10)
        db.add_rss_subscription("manual", "https://example.invalid/c", refresh_interval_minutes=0)
        entry_ids = [
            db.add_rss_entry_with_media(active, f"entry-{index}", f"guid-{index}")["id"]
            for index in range(5)
        ]
        for entry_id, status in zip(entry_ids, ["pending", "failed", "skipped", "submitting", "downloaded"]):
            db.update_rss_entry_status(int(entry_id), status)
        return active, [int(item) for item in entry_ids]

    def test_entry_creation_exposes_only_the_media_aware_api(self) -> None:
        self.assertFalse(hasattr(db, "add_rss_entry"))

    def test_duplicate_guid_insert_is_atomic_and_returns_none(self) -> None:
        sid = db.add_rss_subscription("dedupe", "https://example.invalid/dedupe")
        first = db.add_rss_entry_with_media(sid, "first", "same-guid")["id"]
        second = db.add_rss_entry_with_media(sid, "second", "same-guid")["id"]

        self.assertIsInstance(first, int)
        self.assertIsNone(second)
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM rss_entries WHERE rss_item_id=? AND guid=?",
                (sid, "same-guid"),
            ).fetchone()[0]
        self.assertEqual(count, 1)
    def test_rss_entry_lookup_indexes_exist(self) -> None:
        with db.get_conn() as conn:
            indexes = {row["name"] for row in conn.execute("PRAGMA index_list('rss_entries')")}
        self.assertIn("idx_rss_entries_item_guid", indexes)
        self.assertIn("idx_rss_entries_item_status_id", indexes)

    def test_rss_entries_sort_by_published_time_with_stable_fallback(self) -> None:
        sid = db.add_rss_subscription("sorted", "https://example.invalid/sorted")
        newest = db.add_rss_entry_with_media(sid, "newest", "newest", pub_date="2026-08-15 12:00")["id"]
        oldest = db.add_rss_entry_with_media(sid, "oldest", "oldest", pub_date="2026-08-13 12:00")["id"]
        middle = db.add_rss_entry_with_media(sid, "middle", "middle", pub_date="2026-08-14 12:00")["id"]
        unknown = db.add_rss_entry_with_media(sid, "unknown", "unknown", pub_date="not-a-date")["id"]

        rows = db.list_rss_entries(sub_id=sid)

        self.assertEqual(
            [int(row["id"]) for row in rows],
            [int(newest), int(middle), int(oldest), int(unknown)],
        )
        received = db.list_rss_entries(sub_id=sid, order="received_desc")
        self.assertEqual(int(received[0]["id"]), int(unknown))

    def test_unprocessed_sort_precedes_limit_and_preserves_filters(self) -> None:
        sid = db.add_rss_subscription("priority", "https://example.invalid/priority")
        pending = db.add_rss_entry_with_media(sid, "keep pending", "old", pub_date="2020-01-01 12:00")["id"]
        failed = db.add_rss_entry_with_media(sid, "keep failed", "failed", pub_date="2021-01-01 12:00")["id"]
        db.update_rss_entry_status(failed, "failed")
        for i in range(305):
            processed = db.add_rss_entry_with_media(sid, f"keep processed {i}", f"new-{i}", pub_date="2026-10-04 12:00")["id"]
            db.update_rss_entry_status(processed, "downloaded")
        for _ in range(2):
            rows = db.list_rss_entries(sub_id=sid, keyword="keep", order="unprocessed_first", limit=300)
            self.assertEqual([row["id"] for row in rows[:2]], [failed, pending])
            self.assertEqual(len(rows), 300)
        filtered = db.list_rss_entries(sub_id=sid, status="pending", keyword="pending", order="unprocessed_first")
        self.assertEqual([row["id"] for row in filtered], [pending])
        self.assertNotIn(pending, [row["id"] for row in db.list_rss_entries(sub_id=sid, limit=300)])

    def test_repeated_entry_reads_do_not_scan_or_write_legacy_history(self) -> None:
        sid = db.add_rss_subscription(
            "legacy-read-trace", "https://example.invalid/legacy-read-trace"
        )
        timestamp = db.now()
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO rss_entries(rss_item_id,title,status,processed,payload,created_at) "
                "VALUES(?,?,'failed',0,?,?)",
                [
                    (
                        sid,
                        f"legacy-{index}",
                        json.dumps({
                            "torrent_url": f"magnet:?xt=urn:btih:{index + 1:040x}"
                        }),
                        timestamp,
                    )
                    for index in range(500)
                ],
            )

        # Historical work belongs to startup recovery, not each listing/page request.
        db.init_db()
        statements: list[str] = []
        connect = db._connect

        def traced_connect():
            conn = connect()
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(db, "_connect", side_effect=traced_connect):
            for _ in range(10):
                rows = db.list_rss_entries(limit=300)
                self.assertEqual(len(rows), 300)

        self.assertEqual(len(statements), 10)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in statements))
        history_sql = ("download_log", "download_requests", "download_request_keys")
        self.assertFalse(any(any(table in statement for table in history_sql) for statement in statements))
        self.assertFalse(any(statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
                             for statement in statements))

    def test_generic_retry_snapshot_and_claim_match_backend_and_safe_codes(self) -> None:
        qb_sub = db.add_rss_subscription(
            "retry-qb", "https://example.invalid/retry-qb", download_method="qb",
            qb_save_path="/downloads/qb",
        )
        gy_sub = db.add_rss_subscription(
            "retry-gy", "https://example.invalid/retry-gy", download_method="guangya",
            gy_target_dir="target-gy", gy_target_dir_name="动漫",
        )

        def failed(sub_id: int, title: str, code: str, retryable: bool = True) -> int:
            entry_id = int(db.add_rss_entry_with_media(sub_id, title, title)["id"])
            db.record_rss_entry_failure(entry_id, code, retryable)
            return entry_id

        qb_entry = failed(qb_sub, "qb-safe", "qb_unavailable")
        gy_entry = failed(gy_sub, "gy-safe", "guangya_manifest_unavailable")
        failed(gy_sub, "old-qb-code-on-gy", "qb_unavailable")
        failed(qb_sub, "gy-code-on-qb", "guangya_unavailable")
        failed(qb_sub, "ordinary-qb-rejection", "qb_rejected")

        snapshot = db.get_retryable_failed_rss_snapshot(default_method="qb", limit=21)
        by_id = {int(row["id"]): row for row in snapshot}
        self.assertEqual(set(by_id), {qb_entry, gy_entry})
        self.assertEqual(by_id[gy_entry]["gy_target_dir"], "target-gy")
        self.assertEqual(by_id[gy_entry]["gy_target_dir_name"], "动漫")
        self.assertEqual(len(db.claim_retryable_failed_rss_entries(snapshot)), 2)
        self.assertEqual(db.get_rss_entry(qb_entry)["retry_count"], 1)
        self.assertEqual(db.get_rss_entry(gy_entry)["retry_count"], 1)

    def test_manual_failed_claim_obeys_backend_attempts_and_rate_cooldown(self) -> None:
        gy_sub = db.add_rss_subscription(
            "manual-gy", "https://example.invalid/manual-gy", download_method="guangya"
        )

        def failed(title: str, code: str, retry_count: int = 0) -> int:
            entry_id = int(db.add_rss_entry_with_media(gy_sub, title, title)["id"])
            db.record_rss_entry_failure(entry_id, code, True)
            if retry_count:
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE rss_entries SET retry_count=? WHERE id=?",
                        (retry_count, entry_id),
                    )
            return entry_id

        cooling = failed("cooling", "guangya_rate_limited")
        capped = failed("capped", "guangya_unavailable", 5)
        mismatched = failed("mismatched", "qb_unavailable")
        ordinary = failed("ordinary", "guangya_submit_failed")
        pending = int(db.add_rss_entry_with_media(gy_sub, "pending", "pending")["id"])
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET retry_count=5 WHERE id=?", (pending,)
            )

        self.assertFalse(db.claim_rss_entry(cooling))
        self.assertFalse(db.claim_rss_entry(capped))
        self.assertFalse(db.claim_rss_entry(mismatched))
        self.assertFalse(db.claim_rss_entry(ordinary))
        self.assertTrue(db.claim_rss_entry(pending))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE rss_entries SET failed_at=datetime('now','localtime','-61 seconds') WHERE id=?",
                (cooling,),
            )
        self.assertTrue(db.claim_rss_entry(cooling))
        self.assertEqual(db.get_rss_entry(cooling)["retry_count"], 1)

    def test_rss_stats_counts_global_active_and_pending_entries(self) -> None:
        self._seed()
        self.assertEqual(db.get_rss_stats(), {
            "subscription_total": 3,
            "active_subscriptions": 1,
            "entry_total": 5,
            "pending_total": 1,
        })

        with TestClient(create_app(start_background=False)) as client:
            self.assertEqual(client.get("/api/rss/stats").status_code, 401)
            login_page = client.get("/login")
            logged_in = client.post("/login", data={
                "username": "admin", "password": "123456", "csrf_token": self._csrf(login_page.text),
            }, follow_redirects=False)
            self.assertEqual(logged_in.status_code, 302)
            response = client.get("/api/rss/stats")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pending_total"], 1)

    def test_subscription_enabled_api_directly_pauses_and_resumes_scheduled_refresh(self) -> None:
        subscription_id = db.add_rss_subscription(
            "toggle", "https://example.invalid/toggle", refresh_interval_minutes=10
        )

        with TestClient(create_app(start_background=False)) as client:
            login_page = client.get("/login")
            csrf = self._csrf(login_page.text)
            logged_in = client.post("/login", data={
                "username": "admin", "password": "123456", "csrf_token": csrf,
            }, follow_redirects=False)
            self.assertEqual(logged_in.status_code, 302)
            csrf = self._csrf(client.get("/rss").text)
            headers = {"X-CSRF-Token": csrf}
            with patch("app.routes.rss_api.wake_rss_scheduler") as wake_scheduler:
                paused = client.put(
                    f"/api/rss/subscriptions/{subscription_id}",
                    json={"enabled": False},
                    headers=headers,
                )
                self.assertEqual(paused.status_code, 200, paused.text)
                self.assertEqual(int(db.get_rss_subscription(subscription_id)["enabled"]), 0)
                self.assertNotIn(
                    subscription_id,
                    [int(row["id"]) for row in db.list_due_rss_subscriptions("2099-01-01 00:00:00")],
                )

                resumed = client.put(
                    f"/api/rss/subscriptions/{subscription_id}",
                    json={"enabled": True},
                    headers=headers,
                )
                self.assertEqual(resumed.status_code, 200, resumed.text)
                self.assertEqual(int(db.get_rss_subscription(subscription_id)["enabled"]), 1)
                self.assertIn(
                    subscription_id,
                    [int(row["id"]) for row in db.list_due_rss_subscriptions("2099-01-01 00:00:00")],
                )
                self.assertEqual(wake_scheduler.call_count, 2)

    def test_rss_api_rejects_nonempty_cron_preserves_legacy_value_and_keeps_interval_schedule(self) -> None:
        timestamp = db.now()
        with db.get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO rss_items(name,enabled,refresh_cron,refresh_interval_minutes,urls,parser,"
                "exclude_keywords,action,download_method,qb_save_path,gy_target_dir,gy_target_dir_name,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-cron",
                    1,
                    "0 4 * * *",
                    0,
                    "https://example.invalid/legacy",
                    "mikan",
                    "",
                    "subscribe",
                    "",
                    "",
                    "",
                    "",
                    timestamp,
                    timestamp,
                ),
            )
            legacy_id = int(cursor.lastrowid)

        with TestClient(create_app(start_background=False)) as client:
            login_page = client.get("/login")
            csrf = self._csrf(login_page.text)
            logged_in = client.post(
                "/login",
                data={
                    "username": "admin",
                    "password": "123456",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )
            self.assertEqual(logged_in.status_code, 302)
            headers = {"X-CSRF-Token": self._csrf(client.get("/rss").text)}
            with patch("app.routes.rss_api.wake_rss_scheduler"):
                rejected_create = client.post(
                    "/api/rss/subscriptions",
                    headers=headers,
                    json={
                        "name": "new-cron",
                        "urls": "https://example.invalid/new",
                        "refresh_cron": "0 4 * * *",
                    },
                )
                self.assertEqual(rejected_create.status_code, 400)
                self.assertIn("refresh_interval_minutes", rejected_create.text)

                rejected_update = client.put(
                    f"/api/rss/subscriptions/{legacy_id}",
                    headers=headers,
                    json={"refresh_cron": "0 5 * * *"},
                )
                self.assertEqual(rejected_update.status_code, 400)
                self.assertIn("refresh_interval_minutes", rejected_update.text)

                preserved_update = client.put(
                    f"/api/rss/subscriptions/{legacy_id}",
                    headers=headers,
                    json={"name": "legacy-renamed", "refresh_cron": ""},
                )
                self.assertEqual(preserved_update.status_code, 200, preserved_update.text)

                echoed_update = client.put(
                    f"/api/rss/subscriptions/{legacy_id}",
                    headers=headers,
                    json={"name": "legacy-renamed", "refresh_cron": "0 4 * * *"},
                )
                self.assertEqual(echoed_update.status_code, 200, echoed_update.text)

                empty_create = client.post(
                    "/api/rss/subscriptions",
                    headers=headers,
                    json={
                        "name": "interval-feed",
                        "urls": "https://example.invalid/interval",
                        "refresh_cron": "",
                        "refresh_interval_minutes": 10,
                    },
                )
                self.assertEqual(empty_create.status_code, 200, empty_create.text)
                interval_id = int(empty_create.json()["id"])

        self.assertEqual(db.get_rss_subscription(legacy_id)["name"], "legacy-renamed")
        self.assertEqual(db.get_rss_subscription(legacy_id)["refresh_cron"], "0 4 * * *")
        self.assertIn(
            interval_id,
            [int(row["id"]) for row in db.list_due_rss_subscriptions("2099-01-01 00:00:00")],
        )

    def test_bulk_processed_updates_preserve_inflight_and_downloaded_states(self) -> None:
        _active, entry_ids = self._seed()
        self.assertEqual(db.update_rss_entries_processed(entry_ids, True), 3)
        with db.get_conn() as conn:
            states = {
                int(row["id"]): (str(row["status"]), int(row["processed"] or 0))
                for row in conn.execute(
                    f"SELECT id,status,processed FROM rss_entries WHERE id IN ({','.join('?' for _ in entry_ids)})",
                    entry_ids,
                ).fetchall()
            }
        self.assertEqual([states[item] for item in entry_ids], [
            ("skipped", 1), ("skipped", 1), ("skipped", 1),
            ("submitting", 0), ("downloaded", 1),
        ])

        self.assertEqual(db.update_rss_entries_processed(entry_ids, False), 3)
        with db.get_conn() as conn:
            states = {
                int(row["id"]): (str(row["status"]), int(row["processed"] or 0), row["processed_at"])
                for row in conn.execute(
                    f"SELECT id,status,processed,processed_at FROM rss_entries WHERE id IN ({','.join('?' for _ in entry_ids)})",
                    entry_ids,
                ).fetchall()
            }
        for entry_id in entry_ids[:3]:
            self.assertEqual(states[entry_id], ("pending", 0, None))
        self.assertEqual(states[entry_ids[3]][:2], ("submitting", 0))
        self.assertEqual(states[entry_ids[4]][:2], ("downloaded", 1))
