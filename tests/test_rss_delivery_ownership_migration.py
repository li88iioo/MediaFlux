"""RSS 稳定交付归属的一次升级；真实 SQLite/dispatcher，禁止访问下载器。"""
from __future__ import annotations

import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db, database_migrations
from app.modules import download_dispatcher as dispatcher
from app.modules.rss import rss_entry_delivery
from app.repositories.rss import bind_rss_entry_download
from tests.support import assert_present, isolated_test_database


class RSSDeliveryOwnershipMigrationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("禁止外联")))
        self.enterContext(patch.object(socket.socket, "connect_ex", side_effect=AssertionError("禁止外联")))
        self.enterContext(patch.object(dispatcher, "get", side_effect=lambda key, default="": "http://qb.invalid" if key == "QB_URL" else default))
        self.enterContext(patch.object(dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True, reason="")))
        self.serial = 0

    def receipt(self, backend, item=None):
        self.serial += 1
        item = item or dispatcher.DownloadInput("magnet", "Episode", f"magnet:?xt=urn:btih:{self.serial:040x}")
        sid = db.add_rss_subscription(f"feed-{self.serial}", "https://feed.invalid/rss", download_method=backend)
        eid = int(db.add_rss_entry_with_media(sid, "Episode", f"guid-{self.serial}", payload=json.dumps({"torrent_url": item.source_value}))["id"])
        self.assertTrue(db.claim_rss_entry(eid))
        bind_rss_entry_download(eid, dispatcher.request_key(item), backend)
        rid = int(dispatcher.create_request(item, "", "", origin=f"rss:{sid}")["id"])
        current = assert_present(db.get_download_request(rid))
        targets = "both" if current["targets"] and current["targets"] != backend else backend
        field = "qb_status" if backend == "qb" else "gy_status"
        db.update_download_request(rid, status="submitted", targets=targets, **{field: "submitted"})
        return rid, eid, item

    def dual_successor(self, backend, *, evidence=True):
        original, qb_entry, item = self.receipt("qb")
        shared, gy_entry, _ = self.receipt("guangya", item)
        self.assertEqual(shared, original)
        peer = "guangya" if backend == "qb" else "qb"
        entries = {"qb": qb_entry, "guangya": gy_entry}
        fields = {"qb_status": "failed" if backend == "qb" else "completed", "gy_status": "failed" if backend == "guangya" else "completed"}
        db.update_download_request(original, status="completed", **fields,
                                   organize_started=0, organize_status="settle_wait" if peer == "guangya" else "",
                                   local_import_status="queued" if peer == "qb" else "")
        if evidence:
            self.log(entries[peer], original, peer)
        with patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "retry-qb"}), patch.object(
            dispatcher, "_submit_guangya", return_value={"ok": True, "task_id": "retry-gy"}
        ):
            result = dispatcher.resubmit_download_request(original, backend)
        self.assertTrue(result["ok"], result)
        return original, result["request_id"], entries, item

    def log(self, entry, request, backend):
        row = assert_present(db.get_rss_entry(entry))
        db.add_download_log(backend, title=row["title"], path="[magnet]", rss_item_id=row["rss_item_id"], request_id=request, status="completed")

    def legacy_schema(self, version=35):
        with db.get_conn() as conn:
            conn.execute("DROP INDEX idx_rss_entries_delivery_request")
            conn.execute("ALTER TABLE rss_entries DROP COLUMN download_request_id")
            conn.execute(f"PRAGMA user_version={version}")

    def test_upgrade_recovers_archived_peer_by_evidence_and_keeps_current_selected_backend(self):
        for backend in ("qb", "guangya"):
            for version in (35, 0, db.SCHEMA_VERSION):
                with self.subTest(backend=backend, version=version), isolated_test_database():
                    original, successor, entries, item = self.dual_successor(backend)
                    self.assertNotEqual(assert_present(db.get_download_request(original))["request_key"], dispatcher.request_key(item))
                    peer = "guangya" if backend == "qb" else "qb"
                    self.legacy_schema(version)
                    db.init_db()
                    expected = {backend: successor, peer: original}
                    for selected, request_id in expected.items():
                        self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries[selected]))["request_id"], request_id)
                    old = assert_present(db.get_download_request(original))
                    self.assertEqual(old["organize_status" if peer == "guangya" else "local_import_status"], "settle_wait" if peer == "guangya" else "queued")
                    before = {key: dict(assert_present(db.get_rss_entry(eid))) for key, eid in entries.items()}
                    db.init_db()
                    self.assertEqual({key: dict(assert_present(db.get_rss_entry(eid))) for key, eid in entries.items()}, before)
                    with db.get_conn() as conn:
                        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
                        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unproven_archived_peer_remains_unknown_and_is_not_guessed_on_later_startup(self):
        original, successor, entries, _ = self.dual_successor("qb", evidence=False)
        self.legacy_schema()
        db.init_db()
        cloud = rss_entry_delivery(db.get_rss_entry(entries["guangya"]))
        self.assertEqual((cloud["request_id"], cloud["status"], cloud["backend_status"]), (0, "", ""))
        self.assertTrue(cloud["review_required"])
        self.assertFalse(cloud["retryable"])
        self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries["qb"]))["request_id"], successor)
        # 后来出现相似日志也不能让正常启动再运行历史归属推断。
        self.log(entries["guangya"], original, "guangya")
        db.init_db()
        self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries["guangya"])), cloud)

    def test_unknown_and_partial_backend_receipts_never_become_completed_during_upgrade(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend), isolated_test_database():
                rid, eid, _ = self.receipt(backend)
                field = "qb_status" if backend == "qb" else "gy_status"
                db.update_download_request(rid, status="manual_review", **{field: "outcome_unknown"})
                with db.get_conn() as conn:
                    conn.execute("UPDATE rss_entries SET status='failed',processed=0,failure_code=? WHERE id=?", (f"{backend}_outcome_unknown", eid))
                self.legacy_schema()
                db.init_db()
                delivery = rss_entry_delivery(db.get_rss_entry(eid))
                self.assertEqual((delivery["request_id"], delivery["backend_status"]), (rid, "outcome_unknown"))
                self.assertTrue(delivery["review_required"])
                self.assertFalse(delivery["retryable"])
                self.assertEqual(assert_present(db.get_rss_entry(eid))["status"], "failed")

    def test_legacy_entry_with_two_backend_logs_and_no_selected_backend_stays_unbound(self):
        rid, eid, item = self.receipt("qb")
        self.receipt("guangya", item)
        self.log(eid, rid, "qb")
        self.log(eid, rid, "guangya")
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET status='failed',processed=0,download_request_key='',download_backend='' WHERE id=?", (eid,))
        self.legacy_schema()
        db.init_db()
        row = assert_present(db.get_rss_entry(eid))
        self.assertIsNone(row["download_request_id"])
        self.assertEqual((row["status"], row["download_backend"]), ("failed", ""))

    def test_migration_failure_rolls_back_column_binding_index_and_version_then_retries(self):
        rid, eid, _ = self.receipt("qb")
        self.log(eid, rid, "qb")
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET status='failed',processed=0,download_request_key='',download_backend='' WHERE id=?", (eid,))
        self.legacy_schema()
        with db.get_conn() as conn:
            before = dict(conn.execute("SELECT * FROM rss_entries WHERE id=?", (eid,)).fetchone())
        migrate = database_migrations._migrate_rss_delivery_ownership_v36

        def fail_after_backfill(conn):
            migrate(conn)
            raise RuntimeError("r11-ownership-migration-fault")

        with patch.dict(database_migrations._SCHEMA_MIGRATIONS, {35: fail_after_backfill}):
            with self.assertRaisesRegex(RuntimeError, "r11-ownership-migration-fault"):
                db.init_db()
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 35)
            self.assertNotIn("download_request_id", {row[1] for row in conn.execute("PRAGMA table_info(rss_entries)")})
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_rss_entries_delivery_request'").fetchone())
            self.assertEqual(dict(conn.execute("SELECT * FROM rss_entries WHERE id=?", (eid,)).fetchone()), before)
        db.init_db()
        row = assert_present(db.get_rss_entry(eid))
        self.assertEqual(row["download_request_id"], rid)
        self.assertEqual((row["status"], row["download_backend"]), ("downloaded", "qb"))

    def test_request_removal_preserves_receipt_without_fabricating_an_owner(self):
        rid, eid, _ = self.receipt("qb")
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_requests WHERE id=?", (rid,))
        row = assert_present(db.get_rss_entry(eid))
        self.assertEqual(row["status"], "downloaded")
        self.assertIsNone(row["download_request_id"])
        self.assertTrue(rss_entry_delivery(row)["review_required"])
