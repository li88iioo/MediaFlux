from __future__ import annotations

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
