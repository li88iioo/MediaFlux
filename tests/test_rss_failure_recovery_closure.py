"""RSS 的真实 DB 失败恢复 / 跨源消费闭环；仅替换外部下载后端。"""
from __future__ import annotations

from tests.support import assert_present

from typing import cast
from starlette.requests import Request

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.clients.qbittorrent import TorrentAddResult
from app.modules import download_dispatcher as dispatcher
from app.modules.rss import RSSEngine, rss_entry_delivery
from app.repositories.download_requests import apply_download_tracker_update
from app.repositories.rss import bind_rss_entry_download, rss_retry_source_is_safe
from app.routes import rss_api
from tests.support import InitializedWebTestCase, isolated_test_database


class RSSFailureRecoveryClosureTests(InitializedWebTestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(patch.object(dispatcher, "get", return_value="http://qb.invalid"))
        self.enterContext(patch.object(dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True, reason="")))
        self.enterContext(patch.object(rss_api, "require_api_login"))
        self.sequence = 0

    def pending(self, backend="qb", *, media_key="", enabled=1, action="download"):
        self.sequence += 1
        source = f"magnet:?xt=urn:btih:{self.sequence:040x}"
        sid = db.add_rss_subscription(
            f"RSS {self.sequence}", f"https://feed.invalid/{self.sequence}",
            download_method=backend, action=action, enabled=enabled,
        )
        eid = int(db.add_rss_entry_with_media(
            sid, "Episode", f"guid-{self.sequence}", media_key=media_key,
            payload=json.dumps({"torrent_url": source}),
        )["id"])
        return sid, eid, dispatcher.DownloadInput("magnet", "Episode", source)

    def accepted(self, backend="qb", *, media_key=""):
        sid, eid, item = self.pending(backend, media_key=media_key)
        self.assertTrue(db.claim_rss_entry(eid))
        bind_rss_entry_download(eid, dispatcher.request_key(item), backend)
        rid = int(dispatcher.create_request(item, "", "", origin=f"rss:{sid}")["id"])
        prefix = "qb" if backend == "qb" else "gy"
        db.update_download_request(rid, status="submitted", targets=backend, **{f"{prefix}_status": "submitted"})
        get_rss_entry_result = db.get_rss_entry(eid)
        assert get_rss_entry_result is not None
        get_rss_entry_result_2 = db.get_rss_entry(eid)
        assert get_rss_entry_result_2 is not None
        self.assertEqual((get_rss_entry_result["status"], get_rss_entry_result_2["processed"]), ("downloaded", 1))
        return rid, eid, item

    def fail_request(self, rid, backend="qb"):
        prefix = "qb" if backend == "qb" else "gy"
        assert (apply_download_tracker_update(
            assert_present(db.get_download_request(rid)), status="failed", error="下载失败 passkey=super-secret",
            **{f"{prefix}_status": "failed"},
        )) is not None

    @staticmethod
    def success(_row, **_kwargs):
        return {"ok": True, "task_id": "accepted-task", "task_ids": ["accepted-task"]}

    def dual_receipts(self, failed_backend, *, source="", torrent_data=None):
        _, qb_entry, item = self.pending()
        _, gy_entry, _ = self.pending("guangya")
        if source:
            item = dispatcher.DownloadInput("http", "Episode", source, torrent_data=torrent_data)
            dispatcher.create_request(item, "", "", origin="test:verified-http")
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET payload=? WHERE id=?",
                         (json.dumps({"torrent_url": item.source_value}), gy_entry))
            if source:
                conn.execute("UPDATE rss_entries SET payload=? WHERE id=?",
                             (json.dumps({"torrent_url": source}), qb_entry))
        engine = RSSEngine()
        self.addCleanup(engine.close)
        with patch.object(dispatcher, "_submit_qb", side_effect=self.success), patch.object(
            dispatcher, "_submit_guangya", side_effect=self.success
        ):
            qb_result = engine.download(qb_entry)
            gy_result = engine.download(gy_entry)
        self.assertTrue(qb_result["ok"], qb_result)
        self.assertTrue(gy_result["ok"], gy_result)
        rid = qb_result["request_id"]
        self.assertEqual(gy_result["request_id"], rid)
        selected, other = ("qb_status", "gy_status") if failed_backend == "qb" else ("gy_status", "qb_status")
        db.update_download_request(
            rid, status="completed", **{selected: "failed", other: "completed"},
            organize_started=0, organize_status="settle_wait" if failed_backend == "qb" else "",
            local_import_status="queued" if failed_backend == "guangya" else "",
        )
        return rid, {"qb": qb_entry, "guangya": gy_entry}, engine

    def test_single_backend_successor_never_steals_peer_rss_receipt(self):
        for backend in ("qb", "guangya"):
            for outcome in ("completed", "failed", "outcome_unknown"):
                with self.subTest(backend=backend, outcome=outcome):
                    original, entries, engine = self.dual_receipts(backend)
                    peer = "guangya" if backend == "qb" else "qb"
                    with patch.object(dispatcher, "_submit_qb", side_effect=self.success) as qb, patch.object(
                        dispatcher, "_submit_guangya", side_effect=self.success
                    ) as gy:
                        retry = engine.download(entries[backend])
                    self.assertTrue(retry["ok"], retry)
                    self.assertEqual(qb.call_count, int(backend == "qb"))
                    self.assertEqual(gy.call_count, int(backend == "guangya"))
                    successor = retry["request_id"]
                    self.assertNotEqual(successor, original)
                    selected_field, peer_field = ("qb_status", "gy_status") if backend == "qb" else ("gy_status", "qb_status")
                    root = "manual_review" if outcome == "outcome_unknown" else outcome
                    db.update_download_request(successor, status=root, **{selected_field: outcome})
                    current_peer = rss_entry_delivery(db.get_rss_entry(entries[peer]))
                    self.assertEqual(current_peer["request_id"], original)
                    self.assertEqual(current_peer["backend_status"], "completed")
                    self.assertFalse(current_peer["failed"])
                    self.assertFalse(current_peer["review_required"])
                    current_selected = rss_entry_delivery(db.get_rss_entry(entries[backend]))
                    self.assertEqual(current_selected["request_id"], successor)
                    self.assertEqual(current_selected["backend_status"], outcome)
                    self.assertEqual(current_selected["failed"], outcome == "failed")
                    self.assertEqual(current_selected["review_required"], outcome == "outcome_unknown")
                    self.assertEqual(assert_present(db.get_download_request(successor))[peer_field], "")
                    failed_ids = {row["id"] for row in db.list_rss_entries(status="failed").items}
                    self.assertNotIn(entries[peer], failed_ids)
                    if outcome == "failed":
                        self.assertIn(entries[backend], failed_ids)
                    if peer == "guangya":
                        self.assertTrue(db.claim_download_request_organize(original))
                        db.update_download_request(original, organize_status="failed", organize_error="peer organize fixture")
                        self.assertTrue(rss_entry_delivery(db.get_rss_entry(entries[peer]))["failed"])
                    else:
                        self.assertEqual(assert_present(db.get_download_request(original))["local_import_status"], "queued")
                        db.update_download_request(original, local_import_status="failed", local_import_error="peer import fixture")
                    self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries[peer]))["request_id"], original)

    def test_precreate_binding_gets_owner_in_creation_transaction_and_rolls_back_with_it(self):
        _, eid, item = self.pending()
        self.assertTrue(db.claim_rss_entry(eid))
        bind_rss_entry_download(eid, dispatcher.request_key(item), "qb")
        self.assertIsNone(assert_present(db.get_rss_entry(eid))["download_request_id"])
        with patch("app.repositories.rss._bind_rss_download_entries_conn", side_effect=RuntimeError("binding-fault")):
            with self.assertRaisesRegex(RuntimeError, "binding-fault"):
                dispatcher.create_request(item, "", "")
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_request_keys").fetchone()[0], 0)
        self.assertIsNone(assert_present(db.get_rss_entry(eid))["download_request_id"])
        rid = int(dispatcher.create_request(item, "", "")["id"])
        entry = assert_present(db.get_rss_entry(eid))
        self.assertEqual((entry["download_request_id"], entry["status"]), (rid, "submitting"))
        db.update_download_request(rid, status="submitted", targets="qb", qb_status="submitted")
        self.assertEqual(assert_present(db.get_rss_entry(eid))["status"], "downloaded")

    def test_successor_binding_failure_rolls_back_archive_claim_and_both_receipt_owners(self):
        from app.repositories.rss import _bind_rss_download_entries_conn

        original, entries, _ = self.dual_receipts("qb")
        before = dict(assert_present(db.get_download_request(original)))
        item = dispatcher.DownloadInput(before["kind"], before["title"], before["source_value"])

        def fail_after_transfer(conn, request_id, **kwargs):
            _bind_rss_download_entries_conn(conn, request_id, **kwargs)
            if kwargs.get("source_request_id") == original:
                raise RuntimeError("successor-binding-fault")

        with patch("app.repositories.rss._bind_rss_download_entries_conn", side_effect=fail_after_transfer):
            with self.assertRaisesRegex(RuntimeError, "successor-binding-fault"):
                dispatcher.create_request(item, "", "", initial_targets="qb",
                                          retry_source_request_id=original, retry_source_status="completed")
        self.assertEqual(dict(assert_present(db.get_download_request(original))), before)
        self.assertEqual(assert_present(db.get_download_request_by_request_key(dispatcher.request_key(item)))["id"], original)
        for eid in entries.values():
            self.assertEqual(assert_present(db.get_rss_entry(eid))["download_request_id"], original)
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], 1)

    def test_verified_http_alias_registration_binds_owner_before_failure_feedback(self):
        from app.repositories.download_requests import bind_verified_torrent_identity
        from tests.test_download_http_identity_lifecycle import TORRENT_A

        raw = dispatcher.normalize_download_url("https://feed.invalid/untyped-http")
        rid = int(dispatcher.create_request(raw, "", "")["id"])
        self.assertTrue(db.claim_download_request(rid, "qb"))
        db.update_download_request(rid, status="submitted", qb_status="submitted")
        self.assertEqual(db.claim_download_request_targets(rid, "guangya"), ("guangya",))
        verified = dispatcher.DownloadInput("http", "Episode", raw.source_value, torrent_data=TORRENT_A)
        _, eid, _ = self.pending("guangya")
        self.assertTrue(db.claim_rss_entry(eid))
        bind_rss_entry_download(eid, dispatcher.request_key(verified), "guangya")
        self.assertIsNone(assert_present(db.get_rss_entry(eid))["download_request_id"])
        self.assertEqual(bind_verified_torrent_identity(
            rid, raw.source_value, TORRENT_A, dispatcher.request_keys(verified),
            source_alias_key=dispatcher.request_source_alias_key(verified),
        ), rid)
        self.assertEqual(assert_present(db.get_rss_entry(eid))["download_request_id"], rid)
        db.update_download_request(rid, gy_status="failed")
        db.record_rss_entry_failure(eid, "guangya_unavailable", True, request_id=rid)
        self.assertEqual(assert_present(db.get_rss_entry(eid))["status"], "failed")

    def test_peer_unknown_acceptance_after_manual_successor_keeps_original_owner(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                original, entries, _ = self.dual_receipts(backend)
                peer = "guangya" if backend == "qb" else "qb"
                peer_field = "gy_status" if peer == "guangya" else "qb_status"
                db.update_download_request(original, status="manual_review", **{peer_field: "outcome_unknown"})
                with db.get_conn() as conn:
                    conn.execute("UPDATE rss_entries SET status='failed',processed=0,failure_code=? WHERE id=?",
                                 (f"{peer}_outcome_unknown", entries[peer]))
                with patch.object(dispatcher, "_submit_qb", side_effect=self.success), patch.object(dispatcher, "_submit_guangya", side_effect=self.success):
                    result = dispatcher.resubmit_download_request(original, backend)
                self.assertTrue(result["ok"], result)
                self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries[peer]))["request_id"], original)
                self.assertTrue(rss_entry_delivery(db.get_rss_entry(entries[peer]))["review_required"])
                db.update_download_request(original, **{peer_field: "completed"})
                peer_entry = assert_present(db.get_rss_entry(entries[peer]))
                self.assertEqual((peer_entry["status"], peer_entry["processed"]), ("downloaded", 1))
                self.assertEqual(rss_entry_delivery(peer_entry)["request_id"], original)
                self.assertFalse(rss_entry_delivery(peer_entry)["review_required"])
                self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries[backend]))["request_id"], result["request_id"])

    def test_late_failure_and_acceptance_only_touch_current_owner_and_selected_backend(self):
        from app.repositories.rss import _sync_rss_download_entries_conn

        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                original, entries, _ = self.dual_receipts(backend)
                source = assert_present(db.get_download_request(original))
                item = dispatcher.DownloadInput(source["kind"], source["title"], source["source_value"], torrent_data=source["torrent_data"])
                successor = int(dispatcher.create_request(
                    item, "", "", initial_targets=backend,
                    retry_source_request_id=original, retry_source_status="completed",
                )["id"])
                _, waiter, _ = self.pending(backend)
                self.assertTrue(db.claim_rss_entry(waiter))
                bind_rss_entry_download(waiter, dispatcher.request_key(item), backend)
                selected = entries[backend]
                self.assertEqual(assert_present(db.get_rss_entry(selected))["download_request_id"], successor)
                db.record_rss_entry_failure(selected, "qb_rate_limited", True, request_id=original)
                self.assertEqual(assert_present(db.get_rss_entry(selected))["status"], "downloaded")
                self.assertEqual(assert_present(db.get_rss_entry(waiter))["status"], "submitting")
                self.assertEqual(db.update_rss_entries_processed([selected], False), 0)
                # 把已受理回执模拟为新尝试的等待者，用当前 owner 才能终结同 backend 的等待者。
                with db.get_conn() as conn:
                    conn.execute("UPDATE rss_entries SET status='submitting',processed=0 WHERE id=?", (selected,))
                db.record_rss_entry_failure(selected, "qb_rate_limited", True, request_id=successor)
                self.assertEqual([assert_present(db.get_rss_entry(eid))["status"] for eid in (selected, waiter)], ["failed", "failed"])
                field = "qb_status" if backend == "qb" else "gy_status"
                with db.get_conn() as conn:
                    conn.execute(f"UPDATE download_requests SET {field}='submitted' WHERE id=?", (original,))
                    _sync_rss_download_entries_conn(conn, original, db.now())
                self.assertEqual([assert_present(db.get_rss_entry(eid))["status"] for eid in (selected, waiter)], ["failed", "failed"])
                db.update_download_request(successor, status="submitted", **{field: "submitted"})
                self.assertEqual([assert_present(db.get_rss_entry(eid))["status"] for eid in (selected, waiter)], ["downloaded", "downloaded"])
                peer = entries["guangya" if backend == "qb" else "qb"]
                self.assertEqual(assert_present(db.get_rss_entry(peer))["download_request_id"], original)

    def test_peer_postprocess_retry_reads_stable_owner_not_other_backend_canonical_owner(self):
        original, entries, engine = self.dual_receipts("qb")
        with patch.object(dispatcher, "_submit_qb", side_effect=self.success):
            first = engine.download(entries["qb"])
        qb_successor = first["request_id"]
        db.update_download_request(qb_successor, status="failed", qb_status="failed")
        db.update_download_request(original, organize_status="failed", organize_error="fixture organize failure")
        with patch.object(dispatcher, "resubmit_download_request", wraps=dispatcher.resubmit_download_request) as resubmit, patch.object(
            dispatcher, "_submit_guangya", side_effect=self.success
        ) as gy, patch.object(dispatcher, "_submit_qb") as qb:
            result = engine.download(entries["guangya"])
        resubmit.assert_called_once()
        self.assertEqual(resubmit.call_args.args[:2], (original, "guangya"))
        self.assertTrue(result["ok"], result)
        gy.assert_called_once()
        qb.assert_not_called()
        self.assertNotIn(result["request_id"], {original, qb_successor})
        self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries["qb"]))["request_id"], qb_successor)
        self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries["guangya"]))["request_id"], result["request_id"])

    def test_unselected_failure_can_retry_its_archived_owner_without_stealing_first_successor(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                original, entries, engine = self.dual_receipts(backend)
                peer = "guangya" if backend == "qb" else "qb"
                db.update_download_request(original, status="failed", qb_status="failed", gy_status="failed")
                with patch.object(dispatcher, "_submit_qb", side_effect=self.success), patch.object(dispatcher, "_submit_guangya", side_effect=self.success):
                    first = engine.download(entries[backend])
                    self.assertTrue(first["ok"], first)
                    field = "qb_status" if backend == "qb" else "gy_status"
                    db.update_download_request(first["request_id"], status="failed", **{field: "failed"})
                    peer_delivery = rss_entry_delivery(db.get_rss_entry(entries[peer]))
                    self.assertEqual(peer_delivery["request_id"], original)
                    self.assertTrue(peer_delivery["retryable"])
                    second = engine.download(entries[peer])
                self.assertTrue(second["ok"], second)
                self.assertNotIn(second["request_id"], {original, first["request_id"]})
                self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries[backend]))["request_id"], first["request_id"])
                self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries[peer]))["request_id"], second["request_id"])

    def test_active_successor_conflict_settles_archived_peer_rss_retry(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                original, entries, engine = self.dual_receipts(backend)
                peer = "guangya" if backend == "qb" else "qb"
                self.assertIsNotNone(apply_download_tracker_update(
                    assert_present(db.get_download_request(original)),
                    status="failed", qb_status="failed", gy_status="failed",
                ))
                with patch.object(dispatcher, "_submit_qb", side_effect=self.success) as qb, patch.object(
                    dispatcher, "_submit_guangya", side_effect=self.success
                ) as gy:
                    first = engine.download(entries[backend])
                    self.assertTrue(first["ok"], first)
                    successor = first["request_id"]
                    first_receipt = dict(assert_present(db.get_rss_entry(entries[backend])))
                    before = assert_present(db.get_rss_entry(entries[peer]))
                    self.assertTrue(before["delivery_retryable"])
                    self.assertEqual(before["download_request_id"], original)
                    qb.reset_mock()
                    gy.reset_mock()

                    blocked = engine.download(entries[peer])
                    qb.assert_not_called()
                    gy.assert_not_called()
                    self.assertFalse(blocked["ok"], blocked)
                    self.assertEqual(dict(assert_present(db.get_rss_entry(entries[backend]))), first_receipt)
                    after = assert_present(db.get_rss_entry(entries[peer]))
                    self.assertEqual(after["download_request_id"], original)
                    self.assertEqual((after["status"], after["processed"]), ("failed", 0),
                                     "未调用后端的 canonical 冲突必须收尾，不能遗留 submitting")
                    self.assertTrue(after["failure_code"])
                    self.assertNotIn(after["failure_code"], {
                        "qb_outcome_unknown", "guangya_outcome_unknown", "submission_outcome_unknown",
                    })
                    # 收尾仍受稳定 owner 保护；另一端 successor 的迟到反馈不得覆盖它。
                    db.record_rss_entry_failure(entries[peer], "submission_outcome_unknown", False,
                                                request_id=successor)
                    self.assertEqual(dict(assert_present(db.get_rss_entry(entries[peer]))), dict(after))
                    self.assertEqual(db.update_rss_entries_processed([entries[peer]], False), 1)

                    selected_field = "qb_status" if backend == "qb" else "gy_status"
                    self.assertIsNotNone(apply_download_tracker_update(
                        assert_present(db.get_download_request(successor)), status="failed",
                        **{selected_field: "failed"},
                    ))
                    retried = engine.download(entries[peer])
                    self.assertTrue(retried["ok"], retried)
                    self.assertNotIn(retried["request_id"], {original, successor})
                    self.assertEqual(qb.call_count, int(peer == "qb"))
                    self.assertEqual(gy.call_count, int(peer == "guangya"))
                    self.assertEqual(assert_present(db.get_rss_entry(entries[peer]))["download_request_id"],
                                     retried["request_id"])
                    self.assertEqual(assert_present(db.get_rss_entry(entries[backend]))["download_request_id"],
                                     successor)

    def test_submitting_canonical_conflict_settles_original_owner_without_replay(self):
        from app.indexers.downloads import submit_download_input

        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                original, entries, engine = self.dual_receipts(backend)
                peer = "guangya" if backend == "qb" else "qb"
                self.assertIsNotNone(apply_download_tracker_update(
                    assert_present(db.get_download_request(original)),
                    status="failed", qb_status="failed", gy_status="failed",
                ))
                with patch.object(dispatcher, "_submit_qb", side_effect=self.success), patch.object(
                    dispatcher, "_submit_guangya", side_effect=self.success
                ):
                    first = engine.download(entries[backend])
                self.assertTrue(first["ok"], first)
                canonical = first["request_id"]
                self.assertNotEqual(canonical, original)
                source = assert_present(db.get_download_request(original))
                item = dispatcher.DownloadInput(source["kind"], source["title"], source["source_value"])
                first_receipt = dict(assert_present(db.get_rss_entry(entries[backend])))
                entered, release = threading.Event(), threading.Event()

                def delayed_submit(row, **kwargs):
                    self.assertEqual(int(row["id"]), canonical)
                    entered.set()
                    self.assertTrue(release.wait(5), "canonical submission was not released")
                    return self.success(row, **kwargs)

                with patch.object(dispatcher, "_submit_qb", side_effect=delayed_submit) as qb, patch.object(
                    dispatcher, "_submit_guangya", side_effect=delayed_submit
                ) as gy, ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(submit_download_input, item, peer, origin="indexer:rss-conflict-test")
                    try:
                        self.assertTrue(entered.wait(5), "canonical did not reach the submit boundary")
                        field = "qb_status" if peer == "qb" else "gy_status"
                        self.assertEqual(assert_present(db.get_download_request(canonical))[field], "submitting")
                        blocked = engine.download(entries[peer])
                        settled = dict(assert_present(db.get_rss_entry(entries[peer])))
                    finally:
                        release.set()
                    self.assertTrue(pending.result(timeout=5)["summary"]["ok"])
                self.assertEqual(qb.call_count, int(peer == "qb"))
                self.assertEqual(gy.call_count, int(peer == "guangya"))
                self.assertFalse(blocked["ok"], blocked)
                self.assertFalse(blocked.get("pending"), blocked)
                self.assertEqual((blocked["status"], blocked["request_id"]), ("failed", original))
                self.assertEqual((settled["status"], settled["processed"], settled["download_request_id"]),
                                 ("failed", 0, original))
                self.assertTrue(settled["failure_code"])
                self.assertNotIn(settled["failure_code"], {
                    "qb_outcome_unknown", "guangya_outcome_unknown", "submission_outcome_unknown",
                })
                self.assertEqual(dict(assert_present(db.get_download_request(original))), dict(source))
                self.assertEqual(dict(assert_present(db.get_rss_entry(entries[backend]))), first_receipt)
                self.assertEqual(assert_present(db.get_download_request(canonical))[field], "submitted")
                self.assertEqual(dict(assert_present(db.get_rss_entry(entries[peer]))), settled)
                db.init_db()
                self.assertEqual(dict(assert_present(db.get_rss_entry(entries[peer]))), settled)
                self.assertEqual(db.update_rss_entries_processed([entries[peer]], False), 1)

    def test_duplicate_submission_pending_tracks_current_owner_until_acceptance(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                _, first, item = self.pending(backend)
                _, second, _ = self.pending(backend)
                with db.get_conn() as conn:
                    conn.execute("UPDATE rss_entries SET payload=? WHERE id=?",
                                 (json.dumps({"torrent_url": item.source_value}), second))
                engine = RSSEngine()
                self.addCleanup(engine.close)
                entered, release = threading.Event(), threading.Event()

                def delayed_submit(row, **kwargs):
                    entered.set()
                    self.assertTrue(release.wait(5), "owner submission was not released")
                    return self.success(row, **kwargs)

                with patch.object(dispatcher, "_submit_qb", side_effect=delayed_submit) as qb, patch.object(
                    dispatcher, "_submit_guangya", side_effect=delayed_submit
                ) as gy, ThreadPoolExecutor(max_workers=1) as pool:
                    task = pool.submit(engine.download, first)
                    try:
                        self.assertTrue(entered.wait(5), "owner did not reach the submit boundary")
                        result = engine.download(second)
                        self.assertFalse(result["ok"], result)
                        self.assertTrue(result["pending"], result)
                        for eid in (first, second):
                            waiting = assert_present(db.get_rss_entry(eid))
                            self.assertEqual((waiting["status"], waiting["processed"], waiting["download_request_id"]),
                                             ("submitting", 0, result["request_id"]))
                    finally:
                        release.set()
                    accepted = task.result(timeout=5)
                self.assertTrue(accepted["ok"], accepted)
                self.assertEqual(accepted["request_id"], result["request_id"])
                self.assertEqual(qb.call_count, int(backend == "qb"))
                self.assertEqual(gy.call_count, int(backend == "guangya"))
                for eid in (first, second):
                    receipt = assert_present(db.get_rss_entry(eid))
                    self.assertEqual((receipt["status"], receipt["processed"], receipt["download_request_id"]),
                                     ("downloaded", 1, result["request_id"]))
                    self.assertFalse(receipt["failure_code"])

    def test_conflicting_retry_feedback_cannot_overwrite_a_new_peer_owner(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                original, entries, engine = self.dual_receipts(backend)
                peer = "guangya" if backend == "qb" else "qb"
                self.assertIsNotNone(apply_download_tracker_update(
                    assert_present(db.get_download_request(original)),
                    status="failed", qb_status="failed", gy_status="failed",
                ))
                with patch.object(dispatcher, "_submit_qb", side_effect=self.success) as qb, patch.object(
                    dispatcher, "_submit_guangya", side_effect=self.success
                ) as gy:
                    first = engine.download(entries[backend])
                    self.assertTrue(first["ok"], first)
                    qb.reset_mock()
                    gy.reset_mock()
                    resubmit = dispatcher.resubmit_download_request
                    new_owner = {}

                    def conflict_then_transfer(*args, **kwargs):
                        conflict = resubmit(*args, **kwargs)
                        self.assertTrue(conflict.get("duplicate"), conflict)
                        field = "qb_status" if backend == "qb" else "gy_status"
                        self.assertIsNotNone(apply_download_tracker_update(
                            assert_present(db.get_download_request(first["request_id"])),
                            status="failed", **{field: "failed"},
                        ))
                        source = assert_present(db.get_download_request(original))
                        created = dispatcher.create_request(
                            dispatcher.DownloadInput(source["kind"], source["title"], source["source_value"]),
                            "", "late-peer-retry", origin=str(source["origin"]), initial_targets=peer,
                            retry_source_request_id=original, retry_source_status=str(source["status"]),
                        )
                        self.assertTrue(created["created"])
                        new_owner.update(dict(assert_present(db.get_rss_entry(entries[peer]))))
                        self.assertEqual(new_owner["download_request_id"], created["id"])
                        self.assertEqual((new_owner["status"], new_owner["processed"]), ("submitting", 0))
                        return conflict

                    with patch.object(dispatcher, "resubmit_download_request", side_effect=conflict_then_transfer):
                        result = engine.download(entries[peer])
                    self.assertFalse(result["ok"], result)
                    qb.assert_not_called()
                    gy.assert_not_called()
                self.assertEqual(dict(assert_present(db.get_rss_entry(entries[peer]))), new_owner)
                current = assert_present(db.get_download_request(new_owner["download_request_id"]))
                self.assertEqual(current["qb_status" if peer == "qb" else "gy_status"], "submitting")

    def test_archived_http_peer_retry_uses_frozen_content_not_current_canonical_owner(self):
        from tests.test_download_http_identity_lifecycle import TORRENT_A, MIME

        with patch("app.modules.rss._fetch_rss_payload", return_value=(TORRENT_A, {"content-type": MIME})):
            original, entries, engine = self.dual_receipts("qb", source="https://feed.invalid/shared.torrent", torrent_data=TORRENT_A)
        with patch.object(dispatcher, "_submit_qb", side_effect=self.success):
            first = engine.download(entries["qb"])
        self.assertTrue(first["ok"], first)
        db.update_download_request(first["request_id"], status="failed", qb_status="failed")
        db.update_download_request(original, organize_status="failed", organize_error="fixture organize failure")
        cloud_entry = assert_present(db.get_rss_entry(entries["guangya"]))
        self.assertEqual((cloud_entry["status"], cloud_entry["processed"], cloud_entry["delivery_retryable"]), ("downloaded", 1, 1))
        with patch.object(dispatcher, "_submit_guangya", side_effect=self.success) as gy, patch.object(dispatcher, "_submit_qb") as qb:
            result = engine.download(entries["guangya"])
        self.assertTrue(result["ok"], result)
        gy.assert_called_once()
        qb.assert_not_called()
        self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries["qb"]))["request_id"], first["request_id"])
        self.assertEqual(rss_entry_delivery(db.get_rss_entry(entries["guangya"]))["request_id"], result["request_id"])

    def test_accepted_failure_is_visible_in_api_failed_filter_without_rewriting_receipt(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                rid, eid, _ = self.accepted(backend)
                self.fail_request(rid, backend)
                response = rss_api.list_entries(cast(Request, None), subscription_id=None, status="failed", q="")
                row = next(row for row in json.loads(bytes(response.body))["items"] if row["id"] == eid)
                self.assertEqual((row["status"], row["processed"]), ("downloaded", True))
                self.assertEqual(row["delivery"]["request_id"], rid)
                self.assertTrue(row["delivery"]["failed"])
                self.assertTrue(row["delivery"]["retryable"])
                self.assertNotIn("super-secret", row["delivery"]["error"])
                self.assertNotIn("source_value", row)

    def test_retention_preserves_failed_and_unknown_delivery_not_healthy_receipts(self):
        failed_request, failed, _ = self.accepted()
        unknown_request, unknown, _ = self.accepted()
        _healthy_request, healthy, _ = self.accepted()
        self.fail_request(failed_request)
        db.update_download_request(unknown_request, status="manual_review", qb_status="outcome_unknown")
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET processed_at=datetime('now','-8 days')")
        self.assertEqual(db.purge_processed_rss_entries(), 1)
        self.assertIsNone(db.get_rss_entry(healthy))
        assert (db.get_rss_entry(failed)) is not None
        assert (db.get_rss_entry(unknown)) is not None

    def test_retry_preflight_keeps_receipt_when_target_is_not_ready(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                rid, eid, _ = self.accepted(backend)
                self.fail_request(rid, backend)
                before = dict(assert_present(db.get_rss_entry(eid)))
                with patch.object(dispatcher, "get", return_value=""), patch.object(
                    dispatcher, "analyze_offline_url",
                    return_value=SimpleNamespace(allowed=False, reason="已暂停光鸭离线下载"),
                ), patch.object(dispatcher, "_submit_qb") as qb, patch.object(
                    dispatcher, "_submit_guangya"
                ) as gy:
                    result = RSSEngine().download(eid)
                qb.assert_not_called()
                gy.assert_not_called()
                self.assertFalse(result["ok"])
                after = dict(assert_present(db.get_rss_entry(eid)))
                self.assertEqual(after, before, "准入失败不能改写历史受理回执或消耗重试次数")
                self.assertIn("未配置" if backend == "qb" else "已暂停", result["error"])
                with patch.object(dispatcher, "_submit_qb", side_effect=self.success) as qb, patch.object(
                    dispatcher, "_submit_guangya", side_effect=self.success
                ) as gy:
                    recovered = RSSEngine().download(eid)
                self.assertTrue(recovered["ok"], recovered)
                self.assertEqual(qb.call_count, int(backend == "qb"))
                self.assertEqual(gy.call_count, int(backend == "guangya"))
                self.assertNotEqual(recovered["request_id"], rid)

    def test_explicit_retry_uses_original_identity_and_backend_then_updates_same_entry(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                rid, eid, item = self.accepted(backend)
                self.fail_request(rid, backend)
                get_rss_entry_result_3 = db.get_rss_entry(eid)
                assert get_rss_entry_result_3 is not None
                before_key = get_rss_entry_result_3["download_request_key"]
                # feed URL / 订阅目标变化不得改变已受理请求的恢复身份。
                get_rss_entry_result_4 = db.get_rss_entry(eid)
                assert get_rss_entry_result_4 is not None
                db.update_rss_subscription(get_rss_entry_result_4["rss_item_id"], {"download_method": "guangya" if backend == "qb" else "qb"})
                with db.get_conn() as conn:
                    conn.execute("UPDATE rss_entries SET payload=? WHERE id=?", ('{"torrent_url":"https://changed.invalid/new.torrent"}', eid))
                with patch.object(dispatcher, "_submit_qb", side_effect=self.success) as qb, patch.object(dispatcher, "_submit_guangya", side_effect=self.success) as gy:
                    result = RSSEngine().download(eid)
                    repeated = RSSEngine().download(eid)
                self.assertTrue(result["ok"], result)
                self.assertFalse(result["existing"])
                self.assertNotEqual(result["request_id"], rid)
                self.assertEqual(qb.call_count, int(backend == "qb"))
                self.assertEqual(gy.call_count, int(backend == "guangya"))
                self.assertTrue(repeated["existing"])
                successor = db.get_download_request(result["request_id"])
                assert successor is not None
                self.assertEqual(successor["source_value"], item.source_value)
                current = db.get_rss_entry(eid)
                assert current is not None
                self.assertEqual(current["download_request_key"], before_key)
                self.assertEqual((current["status"], current["processed"]), ("downloaded", 1))
                self.assertEqual(current["delivery_request_id"], result["request_id"])
                self.assertFalse(rss_entry_delivery(current)["failed"])

    def test_mark_reopens_definite_backend_failure_without_clearing_identity(self):
        for backend in ("qb", "guangya"):
            rid, eid, _ = self.accepted(backend)
            self.fail_request(rid, backend)
            get_rss_entry_result_5 = db.get_rss_entry(eid)
            assert get_rss_entry_result_5 is not None
            key = get_rss_entry_result_5["download_request_key"]
            response = rss_api.mark_entries(cast(Request, None), {"entry_ids": [eid], "processed": False})
            self.assertEqual(json.loads(bytes(response.body))["updated"], 1)
            get_rss_entry_result_6 = db.get_rss_entry(eid)
            assert get_rss_entry_result_6 is not None
            self.assertEqual(get_rss_entry_result_6["status"], "pending")
            get_rss_entry_result_7 = db.get_rss_entry(eid)
            assert get_rss_entry_result_7 is not None
            self.assertEqual(get_rss_entry_result_7["download_request_key"], key)
            with patch.object(dispatcher, "_submit_qb", side_effect=self.success), patch.object(dispatcher, "_submit_guangya", side_effect=self.success):
                self.assertTrue(RSSEngine().download(eid)["ok"])

    def test_qb_retry_keeps_subscription_directory_and_rss_category(self):
        sid, eid, _ = self.pending()
        db.update_rss_subscription(sid, {"qb_save_path": "/synthetic/subscription"})
        values = {
            "QB_URL": "http://synthetic.invalid",
            "RSS_QB_CATEGORY": "rss-category",
            "TG_QB_CATEGORY": "tg-category",
            "TG_QB_SAVE_PATH": "/synthetic/tg-default",
        }
        get = lambda key, default="": values.get(key, default)
        with (
            patch("app.modules.rss.get", side_effect=get),
            patch.object(dispatcher, "get", side_effect=get),
            patch.object(dispatcher, "QBittorrentClient") as factory,
        ):
            qb = factory.return_value
            qb.add_torrent_detailed.return_value = TorrentAddResult(True)
            engine = RSSEngine()
            self.addCleanup(engine.close)
            first = engine.download(eid)
            self.assertTrue(first["ok"], first)
            self.fail_request(first["request_id"])
            retry = engine.download(eid)
            self.assertTrue(retry["ok"], retry)
            self.assertNotEqual(first["request_id"], retry["request_id"])
            self.assertTrue(engine.download(eid)["existing"])
        routes = [(call.kwargs["save_path"], call.kwargs["category"])
                  for call in qb.add_torrent_detailed.call_args_list]
        assert routes == [("/synthetic/subscription", "rss-category")] * 2, routes

    def test_qb_retry_keeps_frozen_config_when_live_config_changes(self):
        _sid, eid, _ = self.pending()
        runtime = {
            "url": "http://frozen.invalid", "username": "frozen-user",
            "password": "synthetic-password", "api_key": "synthetic-key", "timeout": 7,
            "category": "frozen-rss", "default_save_path": "/synthetic/frozen",
            "default_method": "qb",
        }
        original = dict(runtime)
        with (
            patch("app.modules.rss.get", side_effect=AssertionError("use frozen RSS config")),
            patch.object(dispatcher, "get", return_value=""),
            patch.object(dispatcher, "QBittorrentClient") as factory,
        ):
            qb = factory.return_value
            qb.add_torrent_detailed.return_value = TorrentAddResult(True)
            engine = RSSEngine()
            self.addCleanup(engine.close)
            first = engine._download_entry(db.get_rss_entry(eid), qb_runtime_config=runtime)
            assert first["ok"], first
            self.fail_request(first["request_id"])
            retry = engine._download_entry(db.get_rss_entry(eid), qb_runtime_config=runtime)
            assert retry["ok"], retry
        assert factory.call_count == 2
        for call in factory.call_args_list:
            assert call.kwargs == {
                "url": runtime["url"], "username": runtime["username"],
                "password": runtime["password"], "api_key": runtime["api_key"], "timeout": 7,
            }
        routes = [(call.kwargs["save_path"], call.kwargs["category"])
                  for call in qb.add_torrent_detailed.call_args_list]
        assert routes == [("/synthetic/frozen", "frozen-rss")] * 2, routes
        assert runtime == original

    def test_rss_retry_exports_shared_torrent_with_frozen_config_after_retention(self):
        from app.indexers.downloads import submit_download_input
        from tests.test_torrent_manifest import _torrent

        runtime = {
            "url": "http://frozen.invalid", "username": "frozen-user",
            "password": "synthetic-password", "api_key": "synthetic-key", "timeout": 7,
            "category": "frozen-rss", "default_save_path": "/synthetic/frozen",
            "default_method": "qb",
        }
        for live_url in ("http://other.invalid", ""):
            with self.subTest(live_url=live_url):
                payload = _torrent({
                    b"name": ("fixture-" + str(self.sequence) + ".mkv").encode(),
                    b"length": 4096, b"piece length": 16384, b"pieces": b"x" * 20,
                })
                item = dispatcher.torrent_download_input("fixture.torrent", payload)
                _sid, eid, _ = self.pending()
                with db.get_conn() as conn:
                    conn.execute("UPDATE rss_entries SET payload=? WHERE id=?",
                                 (json.dumps({"torrent_url": item.source_value}), eid))
                with (
                    patch("app.modules.rss.get", side_effect=AssertionError("use frozen RSS config")),
                    patch.object(dispatcher, "get", return_value=live_url),
                    patch.object(dispatcher, "QBittorrentClient") as factory,
                ):
                    qb = factory.return_value
                    qb.add_torrent_detailed.return_value = TorrentAddResult(True)
                    qb.export_torrent.return_value = payload
                    manual = submit_download_input(item, "qb", origin="manual-test", qb_runtime_config=runtime)
                    assert manual["dispatch"]["ok"], manual
                    engine = RSSEngine()
                    self.addCleanup(engine.close)
                    bound = engine._download_entry(db.get_rss_entry(eid), qb_runtime_config=runtime)
                    assert bound["ok"] and bound["request_id"] == manual["request_id"], bound
                    get_download_request_result = db.get_download_request(bound["request_id"])
                    assert get_download_request_result is not None
                    assert get_download_request_result["kind"] == "torrent"
                    assert qb.add_torrent_detailed.call_count == 1
                    self.fail_request(bound["request_id"])
                    with db.get_conn() as conn:
                        conn.execute("UPDATE download_requests SET completed_at=datetime('now','localtime','-31 days') WHERE id=?",
                                     (bound["request_id"],))
                    assert db.purge_expired_download_request_torrent_data(30) == 1
                    get_download_request_result_2 = db.get_download_request(bound["request_id"])
                    assert get_download_request_result_2 is not None
                    assert get_download_request_result_2["torrent_data"] is None
                    retry = engine._download_entry(db.get_rss_entry(eid), qb_runtime_config=runtime)
                    assert retry["ok"], retry
                qb.export_torrent.assert_called_once_with(dispatcher.parse_torrent_metadata(payload)[1])
                assert factory.call_count == 3
                for call in factory.call_args_list:
                    assert call.kwargs == {key: runtime[key] for key in
                                           ("url", "username", "password", "api_key", "timeout")}
                assert qb.add_torrent_detailed.call_args.kwargs["torrents"] == payload
                assert qb.add_torrent_detailed.call_args.kwargs["category"] == "frozen-rss"
                assert qb.add_torrent_detailed.call_args.kwargs["save_path"] == "/synthetic/frozen"

    def test_active_unknown_and_manual_skips_never_resubmit(self):
        for backend in ("qb", "guangya"):
            for status in ("submitting", "submitted", "downloading", "outcome_unknown", "manual_review", "completed"):
                with self.subTest(backend=backend, status=status):
                    rid, eid, _ = self.accepted(backend)
                    field = "qb_status" if backend == "qb" else "gy_status"
                    db.update_download_request(rid, status="manual_review" if status == "manual_review" else "submitted", **{field: status})
                    self.assertEqual(db.update_rss_entries_processed([eid], False), 0)
                    with patch.object(dispatcher, "_submit_qb") as qb, patch.object(dispatcher, "_submit_guangya") as gy:
                        result = RSSEngine().download(eid)
                    qb.assert_not_called(); gy.assert_not_called()
                    if status in {"outcome_unknown", "manual_review"}:
                        self.assertFalse(result["ok"])
                        self.assertTrue(result["review_required"])
        _, eid, _ = self.pending()
        self.assertEqual(db.update_rss_entries_processed([eid], True), 1)
        with patch.object(dispatcher, "_submit_qb") as qb:
            self.assertTrue(RSSEngine().download(eid)["already_processed"])
        qb.assert_not_called()

    def test_batch_does_not_count_unknown_existing_receipt_as_success(self):
        rid, eid, _ = self.accepted()
        db.update_download_request(rid, status="manual_review", qb_status="outcome_unknown")
        with patch.object(dispatcher, "_submit_qb") as backend:
            result = RSSEngine().download_many([eid])
        backend.assert_not_called()
        self.assertEqual(result["existing_count"], 0)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["outcome_unknown_count"], 1)

    def test_unknown_failure_cannot_be_reset_to_pending(self):
        for code in ("qb_outcome_unknown", "guangya_outcome_unknown", "submission_outcome_unknown"):
            _, eid, _ = self.pending()
            db.record_rss_entry_failure(eid, code, False)
            self.assertEqual(db.update_rss_entries_processed([eid], False), 0)
            get_rss_entry_result_8 = db.get_rss_entry(eid)
            assert get_rss_entry_result_8 is not None
            self.assertEqual(get_rss_entry_result_8["status"], "failed")
            self.assertEqual(db.update_rss_entries_processed([eid], True), 1)
            self.assertEqual(db.update_rss_entries_processed([eid], False), 0)

    def test_unknown_later_confirmed_failed_can_explicitly_reopen(self):
        key = "tmdb:42:tv:S01E001"
        rid, eid, _ = self.accepted(media_key=key)
        with db.get_conn() as conn:
            conn.execute("UPDATE rss_entries SET status='failed',processed=0,processed_at=NULL,"
                         "failure_code='qb_outcome_unknown',failure_retryable=0 WHERE id=?", (eid,))
        db.update_download_request(rid, status="manual_review", qb_status="outcome_unknown")
        self.assertEqual(db.update_rss_entries_processed([eid], False), 0)
        self.fail_request(rid)
        delivery = rss_entry_delivery(db.get_rss_entry(eid))
        self.assertTrue(delivery["retryable"])
        self.assertFalse(delivery["review_required"])
        _, alternate, _ = self.pending(media_key=key)
        get_rss_entry_result_9 = db.get_rss_entry(alternate)
        assert get_rss_entry_result_9 is not None
        self.assertEqual(get_rss_entry_result_9["status"], "pending")
        with patch.object(dispatcher, "_submit_qb", side_effect=self.success) as backend:
            result = RSSEngine().download(eid)
        self.assertTrue(result["ok"], result)
        backend.assert_called_once()
        get_rss_entry_result_10 = db.get_rss_entry(eid)
        assert get_rss_entry_result_10 is not None
        self.assertEqual(get_rss_entry_result_10["failure_code"], "")

    def test_rss_retry_fence_rechecks_unknown_change_after_entry_reopens(self):
        for backend in ("qb", "guangya"):
            with self.subTest(backend=backend):
                rid, eid, _ = self.accepted(backend)
                self.fail_request(rid, backend)
                real_resubmit = dispatcher.resubmit_download_request
                field = "qb_status" if backend == "qb" else "gy_status"
                def race(source_id, target, field=field, real_resubmit=real_resubmit, **kwargs):
                    # RSS 已通过重开判定，但统一重提读取原请求之前变为 manual_review。
                    db.update_download_request(source_id, status="manual_review", **{field: "manual_review"})
                    return real_resubmit(source_id, target, **kwargs)
                with db.get_conn() as conn:
                    before = conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0]
                with patch.object(dispatcher, "resubmit_download_request", side_effect=race), patch.object(dispatcher, "_submit_qb") as qb, patch.object(dispatcher, "_submit_guangya") as gy:
                    result = RSSEngine().download(eid)
                self.assertFalse(result["ok"], result)
                self.assertTrue(result["review_required"], result)
                qb.assert_not_called(); gy.assert_not_called()
                get_download_request_result_3 = db.get_download_request(rid)
                assert get_download_request_result_3 is not None
                self.assertEqual(get_download_request_result_3["status"], "manual_review")
                with db.get_conn() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], before)
                self.assertTrue(rss_entry_delivery(db.get_rss_entry(eid))["review_required"])

    def test_safe_retry_helper_matches_read_projection_for_all_backend_states(self):
        rid, eid, _ = self.accepted()
        for root in ("failed", "completed", "resubmitted", "cancelled", "manual_review", "submitted"):
            for target in ("qb", "guangya"):
                for peer in ("", "completed", "submitted", "manual_review", "outcome_unknown"):
                    values = {"qb_status": "failed" if target == "qb" else peer, "gy_status": "failed" if target == "guangya" else peer}
                    db.update_download_request(rid, status=root, **values)
                    with db.get_conn() as conn:
                        conn.execute("UPDATE rss_entries SET download_backend=? WHERE id=?", (target, eid))
                    get_rss_entry_result_11 = db.get_rss_entry(eid)
                    assert get_rss_entry_result_11 is not None
                    self.assertEqual(rss_retry_source_is_safe(db.get_download_request(rid), target), bool(get_rss_entry_result_11["delivery_retryable"]))

    def test_pending_from_paused_or_subscribe_only_source_does_not_block_auto_download(self):
        for enabled, action in ((0, "download"), (0, "subscribe"), (1, "subscribe"), (1, "download")):
            with self.subTest(enabled=enabled, action=action):
                media_key = f"tmdb:{self.sequence+1}:tv:S01E001"
                _, first, _ = self.pending(media_key=media_key, enabled=enabled, action=action)
                sid, second, _ = self.pending(media_key=media_key)
                get_rss_entry_result_12 = db.get_rss_entry(second)
                assert get_rss_entry_result_12 is not None
                self.assertEqual(get_rss_entry_result_12["status"], "pending")
                with patch.object(RSSEngine, "refresh", return_value={"total": 0, "new": 0, "skipped": 0}), patch.object(dispatcher, "_submit_qb", side_effect=self.success) as backend:
                    result = RSSEngine().auto_download(sid)
                self.assertEqual(result["downloaded"], 1, result)
                backend.assert_called_once()
                get_rss_entry_result_13 = db.get_rss_entry(first)
                assert get_rss_entry_result_13 is not None
                self.assertEqual(get_rss_entry_result_13["status"], "pending")

    def test_two_pending_resources_share_one_atomic_consumption_right(self):
        _, first, _ = self.pending(media_key="tmdb:42:tv:S01E001")
        _, second, _ = self.pending(media_key="tmdb:42:tv:S01E001")
        entered, release = threading.Event(), threading.Event()
        def submit(row, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.success(row, **kwargs)
        with patch.object(dispatcher, "_submit_qb", side_effect=submit) as backend, ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(RSSEngine().download, first)
            try:
                self.assertTrue(entered.wait(5))
                result = RSSEngine().download(second)
                self.assertTrue(result["existing"], result)
                get_rss_entry_result_14 = db.get_rss_entry(second)
                assert get_rss_entry_result_14 is not None
                self.assertEqual(get_rss_entry_result_14["status"], "skipped")
            finally:
                release.set()
            self.assertTrue(task.result(timeout=5)["ok"])
        backend.assert_called_once()

    def test_batch_claimed_snapshot_still_submits_same_episode_once(self):
        self.pending(media_key="tmdb:42:tv:S01E001")
        self.pending(media_key="tmdb:42:tv:S01E001")
        expected = [dict(row) for row in db.get_pending_rss_qb_snapshot()]
        with patch.object(dispatcher, "_submit_qb", side_effect=self.success) as backend:
            result = RSSEngine().submit_snapshot(expected, {"url": "http://qb.invalid", "default_method": "qb"}, claim=db.claim_pending_rss_qb_entries)
        self.assertFalse(result["conflict"])
        self.assertTrue(result["ok"], result)
        backend.assert_called_once()

    def test_refresh_reconsiders_automatic_skip_after_owner_failure_but_not_manual_skip(self):
        key = "tmdb:42:tv:S01E001"
        rid, _first, _ = self.accepted(media_key=key)
        sid, second, item = self.pending(media_key=key)
        get_rss_entry_result_15 = db.get_rss_entry(second)
        assert get_rss_entry_result_15 is not None
        self.assertEqual(get_rss_entry_result_15["status"], "skipped")
        self.fail_request(rid)
        def refresh():
            return db.add_rss_entry_with_media(sid, "Episode", f"guid-{self.sequence}", media_key=key, payload=json.dumps({"torrent_url": item.source_value}))
        self.assertIsNone(refresh()["id"])
        get_rss_entry_result_16 = db.get_rss_entry(second)
        assert get_rss_entry_result_16 is not None
        self.assertEqual(get_rss_entry_result_16["status"], "pending")
        self.assertEqual(db.update_rss_entries_processed([second], True), 1)
        refresh()
        get_rss_entry_result_17 = db.get_rss_entry(second)
        assert get_rss_entry_result_17 is not None
        self.assertEqual(get_rss_entry_result_17["status"], "skipped")
        self.assertEqual(db.list_rss_entries(sub_id=sid).items[0]["skip_reason"], "")

    def test_qb_cancellation_releases_explicit_alternative_without_replaying_original(self):
        key = "tmdb:42:tv:S01E001"
        rid, original, item = self.accepted(media_key=key)
        _, alternate, _ = self.pending(media_key=key)
        task_hash = item.source_value.rsplit(":", 1)[-1]
        db.update_download_request(rid, qb_task_id=task_hash)
        self.assertEqual(db.cancel_qb_download_tracking([task_hash]), [rid])
        original_entry = assert_present(db.get_rss_entry(original))
        self.assertEqual((original_entry["status"], original_entry["processed"]), ("downloaded", 1))
        delivery = rss_entry_delivery(original_entry)
        self.assertFalse(delivery["failed"])
        self.assertFalse(delivery["retryable"])
        self.assertFalse(delivery["review_required"])
        self.assertEqual(db.update_rss_entries_processed([original], False), 0)

        engine = RSSEngine()
        self.addCleanup(engine.close)
        with patch.object(RSSEngine, "refresh", return_value={"total": 0, "new": 0, "skipped": 0}), patch.object(
            dispatcher, "_submit_qb", side_effect=self.success
        ) as qb, patch.object(dispatcher, "_submit_guangya") as gy:
            self.assertTrue(engine.download(original)["existing"])
            automatic = engine.auto_download(int(original_entry["rss_item_id"]))
            self.assertEqual(automatic["downloaded"], 0, automatic)
            qb.assert_not_called()
            self.assertEqual(db.update_rss_entries_processed([alternate], False), 1)
            result = engine.download(alternate)
        qb.assert_called_once()
        gy.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["existing"], result)
        self.assertNotEqual(result["request_id"], rid)
        self.assertEqual(assert_present(db.get_download_request(rid))["status"], "cancelled")
        self.assertEqual(assert_present(db.get_rss_entry(original))["delivery_request_id"], rid)

    def test_selected_backend_cancellation_releases_discovery_refresh_and_binding(self):
        engine = RSSEngine()
        self.addCleanup(engine.close)
        for backend in ("qb", "guangya"):
            for peer in ("", "submitted", "completed", "outcome_unknown", "manual_review"):
                with self.subTest(backend=backend, peer=peer):
                    key = f"tmdb:{self.sequence+1}:tv:S01E001"
                    rid, original, _ = self.accepted(backend, media_key=key)
                    sid, alternate, item = self.pending(backend, media_key=key)
                    alternate_entry = assert_present(db.get_rss_entry(alternate))
                    self.assertEqual(alternate_entry["status"], "skipped")
                    selected, other = ("qb_status", "gy_status") if backend == "qb" else ("gy_status", "qb_status")
                    root = "downloading" if peer == "outcome_unknown" else peer or "cancelled"
                    db.update_download_request(rid, status=root, targets="both" if peer else backend,
                                               **{selected: "cancelled", other: peer})
                    _, fresh, _ = self.pending(backend, media_key=key)
                    self.assertEqual(assert_present(db.get_rss_entry(fresh))["status"], "pending")
                    refreshed = db.add_rss_entry_with_media(
                        sid, "Episode", alternate_entry["guid"],
                        media_key=key, payload=json.dumps({"torrent_url": item.source_value}),
                    )
                    self.assertIsNone(refreshed["id"])
                    self.assertEqual(assert_present(db.get_rss_entry(alternate))["status"], "pending")
                    with patch.object(dispatcher, "_submit_qb", side_effect=self.success) as qb, patch.object(
                        dispatcher, "_submit_guangya", side_effect=self.success
                    ) as gy:
                        result = engine.download(alternate)
                    self.assertTrue(result["ok"], result)
                    self.assertFalse(result["existing"], result)
                    self.assertEqual(qb.call_count, int(backend == "qb"))
                    self.assertEqual(gy.call_count, int(backend == "guangya"))
                    self.assertEqual(assert_present(db.get_download_request(rid))[selected], "cancelled")
                    self.assertEqual(assert_present(db.get_rss_entry(original))["status"], "downloaded")

    def test_peer_cancellation_keeps_selected_live_unknown_and_partial_receipts_blocking(self):
        engine = RSSEngine()
        self.addCleanup(engine.close)
        states = (
            ("submitted", "submitted"), ("downloading", "downloading"),
            ("completed", "completed"), ("outcome_unknown", "downloading"),
            ("manual_review", "manual_review"), ("completed", "manual_review"),
        )
        for backend in ("qb", "guangya"):
            for selected_status, root in states:
                with self.subTest(backend=backend, selected_status=selected_status, root=root):
                    key = f"tmdb:{self.sequence+1}:tv:S01E001"
                    rid, _, _ = self.accepted(backend, media_key=key)
                    selected, other = ("qb_status", "gy_status") if backend == "qb" else ("gy_status", "qb_status")
                    db.update_download_request(rid, status=root, targets="both",
                                               **{selected: selected_status, other: "cancelled"})
                    _, alternate, _ = self.pending(backend, media_key=key)
                    self.assertEqual(assert_present(db.get_rss_entry(alternate))["status"], "skipped")
                    self.assertEqual(db.update_rss_entries_processed([alternate], False), 1)
                    with patch.object(dispatcher, "_submit_qb") as qb, patch.object(dispatcher, "_submit_guangya") as gy:
                        result = engine.download(alternate)
                    qb.assert_not_called()
                    gy.assert_not_called()
                    self.assertEqual(result["status"], "skipped", result)

    def test_unlinked_historical_receipt_keeps_blocking_without_cancellation_evidence(self):
        key = "tmdb:42:tv:S01E001"
        rid, _, _ = self.accepted(media_key=key)
        db.update_download_request(rid, status="cancelled", qb_status="cancelled")
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_request_keys WHERE request_id=?", (rid,))
            conn.execute("UPDATE rss_entries SET download_request_id=NULL WHERE download_request_id=?", (rid,))
        _, alternate, _ = self.pending(media_key=key)
        self.assertEqual(assert_present(db.get_rss_entry(alternate))["status"], "skipped")
        self.assertEqual(db.update_rss_entries_processed([alternate], False), 1)
        engine = RSSEngine()
        self.addCleanup(engine.close)
        with patch.object(dispatcher, "_submit_qb") as qb, patch.object(dispatcher, "_submit_guangya") as gy:
            result = engine.download(alternate)
        qb.assert_not_called()
        gy.assert_not_called()
        self.assertEqual(result["status"], "skipped", result)

    def test_current_unknown_receipt_blocks_other_resources(self):
        for status in ("outcome_unknown", "manual_review"):
            key = f"tmdb:{self.sequence+1}:tv:S01E001"
            rid, _, _ = self.accepted(media_key=key)
            db.update_download_request(rid, status="manual_review", qb_status=status)
            _, other, _ = self.pending(media_key=key)
            get_rss_entry_result_18 = db.get_rss_entry(other)
            assert get_rss_entry_result_18 is not None
            self.assertEqual(get_rss_entry_result_18["status"], "skipped")
