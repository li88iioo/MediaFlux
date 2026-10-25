"""RSS 保留期清理必须保留未完成核验的交付回执。"""
from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from app import database as db
from app.modules import download_dispatcher as dispatcher
from app.modules.rss import rss_entry_delivery
from app.repositories.rss import bind_rss_entry_download
from tests.support import assert_present, isolated_test_database


class RSSRetentionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("禁止外联")))
        self.enterContext(patch.object(socket.socket, "connect_ex", side_effect=AssertionError("禁止外联")))
        self.serial = 0

    def receipt(self, backend, *, accepted=True):
        self.serial += 1
        item = dispatcher.DownloadInput("magnet", "Episode", f"magnet:?xt=urn:btih:{self.serial:040x}")
        sid = db.add_rss_subscription("retention", "https://feed.invalid/rss", download_method=backend)
        eid = int(db.add_rss_entry_with_media(sid, "Episode", f"guid-{self.serial}")["id"])
        self.assertTrue(db.claim_rss_entry(eid))
        bind_rss_entry_download(eid, dispatcher.request_key(item), backend)
        rid = int(dispatcher.create_request(item, "", "", origin=f"rss:{sid}")["id"])
        if accepted:
            field = "qb_status" if backend == "qb" else "gy_status"
            db.update_download_request(rid, status="submitted", targets=backend, **{field: "submitted"})
        return rid, eid

    @staticmethod
    def expire(entry_id):
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET processed_at=datetime('now','-8 days') WHERE id=?", (entry_id,))

    def test_missing_request_accepted_receipt_remains_until_reviewed(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                rid, eid = self.receipt(backend)
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM download_requests WHERE id=?", (rid,))
                before = dict(assert_present(db.get_rss_entry(eid)))
                self.assertIsNone(before["download_request_id"])
                self.assertTrue(rss_entry_delivery(before)["review_required"])
                self.expire(eid)
                self.assertEqual(db.purge_processed_rss_entries(), 0)
                self.assertEqual(rss_entry_delivery(db.get_rss_entry(eid)), rss_entry_delivery(before))

    def test_bound_failed_and_unknown_deliveries_are_retained(self):
        cases = (("failed", "failed"), ("downloading", "outcome_unknown"),
                 ("manual_review", "manual_review"), ("manual_review", "submitted"))
        for backend in ("qb", "guangya"):
            for root, selected in cases:
                with self.subTest(backend=backend, root=root, selected=selected):
                    rid, eid = self.receipt(backend)
                    field = "qb_status" if backend == "qb" else "gy_status"
                    db.update_download_request(rid, status=root, **{field: selected})
                    self.expire(eid)
                    delivery = rss_entry_delivery(db.get_rss_entry(eid))
                    self.assertTrue(delivery["failed"] or delivery["review_required"])
                    self.assertEqual(db.purge_processed_rss_entries(), 0)
                    self.assertIsNotNone(db.get_rss_entry(eid))
        rid, eid = self.receipt("guangya")
        db.update_download_request(rid, status="completed", gy_status="completed", organize_status="failed")
        self.expire(eid)
        self.assertTrue(rss_entry_delivery(db.get_rss_entry(eid))["failed"])
        self.assertEqual(db.purge_processed_rss_entries(), 0)
        self.assertIsNotNone(db.get_rss_entry(eid))

    def test_healthy_expired_receipt_is_cleaned_but_recent_or_unprocessed_rows_remain(self):
        rid, old = self.receipt("qb")
        db.update_download_request(rid, status="completed", qb_status="completed")
        _, recent = self.receipt("guangya")
        _, no_timestamp = self.receipt("qb")
        sid = int(assert_present(db.get_rss_entry(recent))["rss_item_id"])
        pending = int(db.add_rss_entry_with_media(sid, "Pending", "pending")["id"])
        skipped = int(db.add_rss_entry_with_media(sid, "Skipped", "skipped")["id"])
        self.assertEqual(db.update_rss_entries_processed([skipped], True), 1)
        for eid in (old, pending, skipped):
            self.expire(eid)
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET processed_at=NULL WHERE id=?", (no_timestamp,))
        self.assertEqual(db.purge_processed_rss_entries(), 2)
        self.assertIsNone(db.get_rss_entry(old))
        self.assertIsNone(db.get_rss_entry(skipped))
        for eid in (recent, pending, no_timestamp):
            self.assertIsNotNone(db.get_rss_entry(eid))

    def test_explicitly_skipped_unknown_receipts_can_expire(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                rid, eid = self.receipt(backend, accepted=False)
                field = "qb_status" if backend == "qb" else "gy_status"
                db.update_download_request(rid, status="manual_review", **{field: "outcome_unknown"})
                db.record_rss_entry_failure(eid, f"{backend}_outcome_unknown", False, request_id=rid)
                self.assertEqual(db.update_rss_entries_processed([eid], True), 1)
                row = assert_present(db.get_rss_entry(eid))
                self.assertEqual(row["status"], "skipped")
                self.assertTrue(rss_entry_delivery(row)["review_required"])
                self.expire(eid)
                self.assertEqual(db.purge_processed_rss_entries(), 1)
                self.assertIsNone(db.get_rss_entry(eid))
