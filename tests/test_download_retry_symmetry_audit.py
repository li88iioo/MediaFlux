"""失败下载目标重试：方向对称、保留另一端、认领竞态与 HTTP 身份。"""

from __future__ import annotations

import tests  # noqa: F401 - 应用导入前隔离运行路径与数据库

import json
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import database as db
from app.clients.qbittorrent import TorrentTask
from app.indexers.downloads import submit_download_input
from app.modules import download_dispatcher as dispatcher
from app.modules.download_tracker import DownloadTracker
from app.modules.rss import RSSEngine, rss_entry_delivery
from tests.support import isolated_test_database


class DownloadRetrySymmetryAuditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(
            patch.object(
                dispatcher,
                "get",
                side_effect=lambda key, default="": (
                    "http://qb.invalid" if key == "QB_URL" else default
                ),
            )
        )
        self.enterContext(
            patch.object(
                dispatcher,
                "analyze_offline_url",
                return_value=SimpleNamespace(allowed=True, reason=""),
            )
        )
        self.serial = 0

    def request(self, target="qb", peer_status="downloading", *, kind="magnet"):
        self.serial += 1
        item = dispatcher.DownloadInput(
            kind=kind,
            title="重试目标",
            source_value=(
                f"https://downloads.invalid/{self.serial}.torrent"
                if kind == "http"
                else f"magnet:?xt=urn:btih:{self.serial:040x}"
            ),
        )
        created = dispatcher.create_request(item, "", "audit")
        self.assertTrue(created["created"])
        request_id = int(created["id"])
        peer = "guangya" if target == "qb" else "qb"
        fields = {
            "status": "submitted",
            "targets": "both",
            "qb_task_id": "a" * 40,
            "gy_task_id": "gy-kept",
            "gy_task_ids": '["gy-kept"]',
            "gy_target_dir": "stage-kept",
            "gy_staging_name": "untouched",
            "local_import_target": "local-kept",
        }
        fields[("gy" if target == "guangya" else target) + "_status"] = "failed"
        fields[("gy" if peer == "guangya" else peer) + "_status"] = peer_status
        db.update_download_request(request_id, **fields)
        return request_id

    def test_either_failed_target_retries_in_place_while_peer_is_tracked(self):
        for target in ("qb", "guangya"):
            for peer_status in (
                "submitted",
                "downloading",
                "completed",
                "outcome_unknown",
            ):
                with self.subTest(target=target, peer_status=peer_status):
                    request_id = self.request(target, peer_status)
                    get_download_request_result_7 = db.get_download_request(request_id)
                    assert get_download_request_result_7 is not None
                    before = dict(get_download_request_result_7)
                    selected = "_submit_qb" if target == "qb" else "_submit_guangya"
                    other = "_submit_guangya" if target == "qb" else "_submit_qb"
                    with (
                        patch.object(
                            dispatcher,
                            selected,
                            return_value={"ok": True, "task_id": "retry"},
                        ) as submit,
                        patch.object(dispatcher, other) as untouched,
                    ):
                        result = dispatcher.resubmit_download_request(
                            request_id, target
                        )
                    self.assertTrue(result["ok"], result)
                    self.assertFalse(result["created"])
                    self.assertEqual(result["request_id"], request_id)
                    submit.assert_called_once()
                    untouched.assert_not_called()
                    get_download_request_result_8 = db.get_download_request(request_id)
                    assert get_download_request_result_8 is not None
                    after = dict(get_download_request_result_8)
                    self.assertEqual(after["request_key"], before["request_key"])
                    preserved = (
                        (
                            "gy_status",
                            "gy_task_id",
                            "gy_task_ids",
                            "gy_target_dir",
                            "gy_staging_name",
                        )
                        if target == "qb"
                        else ("qb_status", "qb_task_id", "local_import_target")
                    )
                    for field in preserved:
                        self.assertEqual(after[field], before[field], field)
                    with db.get_conn() as conn:
                        self.assertEqual(
                            conn.execute(
                                "SELECT count(*) FROM download_requests WHERE id=?",
                                (request_id,),
                            ).fetchone()[0],
                            1,
                        )

    def test_successor_qb_retry_preserves_pending_cloud_organize(self):
        for status, qb_status in (("completed", "failed"), ("manual_review", "failed")):
            with self.subTest(status=status):
                request_id = self.request(target="qb", peer_status="completed")
                db.update_download_request(
                    request_id, status=status, qb_status=qb_status,
                    organize_started=0, organize_status="settle_wait",
                )
                with patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "retry-qb"}):
                    result = dispatcher.resubmit_download_request(request_id, "qb")
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["created"])
                old = db.get_download_request(request_id)
                assert old is not None
                self.assertEqual(old["gy_status"], "completed")
                self.assertEqual(old["organize_started"], 0)
                self.assertEqual(old["organize_status"], "settle_wait")
                self.assertIn(request_id, {row["id"] for row in db.list_active_download_requests()})
                self.assertTrue(db.claim_download_request_organize(request_id))
                self.assertFalse(db.claim_download_request_organize(request_id))

    def test_cloud_successor_does_not_clear_unselected_local_import_failure(self):
        request_id = self.request(target="guangya", peer_status="completed")
        db.update_download_request(request_id, status="completed", local_import_status="failed")
        with patch.object(dispatcher, "_submit_guangya", return_value={"ok": True, "task_ids": ["retry-gy"]}):
            result = dispatcher.resubmit_download_request(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        old = db.get_download_request(request_id)
        self.assertTrue(result["source_attention_preserved"])
        assert old is not None
        self.assertEqual(old["local_import_status"], "failed")
        self.assertEqual(old["qb_status"], "completed")
        self.assertIn(request_id, {row["id"] for row in db.list_download_requests_requiring_attention()})

    def test_failed_retry_preserves_attention_and_other_backend(self):
        for backend_result in (
            {"ok": False, "error": "拒绝"},
            {"ok": False, "failure_code": "qb_outcome_unknown", "error": "结果未知"},
        ):
            with self.subTest(result=backend_result):
                request_id = self.request()
                with patch.object(
                    dispatcher, "_submit_qb", return_value=backend_result
                ) as submit:
                    result = dispatcher.resubmit_download_request(request_id, "qb")
                submit.assert_called_once()
                self.assertFalse(result["ok"])
                self.assertTrue(result["source_attention_preserved"])
                self.assertEqual(result["request_id"], request_id)
                get_download_request_result = db.get_download_request(request_id)
                assert get_download_request_result is not None
                self.assertEqual(
                    get_download_request_result["gy_status"], "downloading"
                )
                expected = (
                    "outcome_unknown"
                    if backend_result.get("failure_code")
                    else "failed"
                )
                get_download_request_result_2 = db.get_download_request(request_id)
                assert get_download_request_result_2 is not None
                self.assertEqual(
                    get_download_request_result_2["qb_status"], expected
                )

    def test_reentrant_retry_cannot_submit_twice(self):
        request_id = self.request()
        nested = []

        def submit(_row, **_kwargs):
            nested.append(dispatcher.resubmit_download_request(request_id, "qb"))
            return {"ok": True, "task_id": "b" * 40}

        with patch.object(dispatcher, "_submit_qb", side_effect=submit) as backend:
            result = dispatcher.resubmit_download_request(request_id, "qb")
        self.assertTrue(result["ok"], result)
        backend.assert_called_once()
        self.assertFalse(nested[0]["ok"])

    def test_cancellation_after_capability_check_blocks_backend_submission(self):
        request_id = self.request()
        original = dispatcher.download_resubmit_capabilities

        def capture_then_cancel(row, **kwargs):
            result = original(row, **kwargs)
            db.update_download_request(
                request_id, status="cancelled", qb_status="cancelled"
            )
            return result

        with (
            patch.object(
                dispatcher,
                "download_resubmit_capabilities",
                side_effect=capture_then_cancel,
            ),
            patch.object(dispatcher, "_submit_qb") as backend,
        ):
            result = dispatcher.resubmit_download_request(request_id, "qb")
        self.assertFalse(result["ok"])
        backend.assert_not_called()
        get_download_request_result_3 = db.get_download_request(request_id)
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3["status"], "cancelled")

    def test_http_retry_keeps_previously_resolved_torrent_identity(self):
        request_id = self.request(kind="http")
        client = SimpleNamespace(
            add_torrent_detailed=lambda **_kwargs: SimpleNamespace(
                ok=True, failure_code="", task_ids=[], retryable=False
            ),
        )
        with (
            patch.object(dispatcher, "QBittorrentClient", return_value=client),
            patch.object(dispatcher, "close_qbittorrent_client"),
            patch.object(dispatcher, "_submit_guangya") as untouched,
        ):
            result = dispatcher.resubmit_download_request(request_id, "qb")
        self.assertTrue(result["ok"], result)
        get_download_request_result_4 = db.get_download_request(request_id)
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4["qb_task_id"], "a" * 40)
        untouched.assert_not_called()


    def test_rss_feedback_runs_only_on_new_backend_acceptance(self):
        from app.repositories import rss

        request_id = self.request(target="guangya", peer_status="submitted")
        with patch.object(rss, "_sync_rss_download_entries_conn", wraps=rss._sync_rss_download_entries_conn) as sync:
            db.update_download_request(request_id, gy_status="failed")
            db.update_download_request(request_id, gy_status="outcome_unknown")
            sync.assert_not_called()
            db.update_download_request(request_id, gy_status="submitted")
            sync.assert_called_once()
            for status in ("submitted", "downloading", "downloading", "completed"):
                db.update_download_request(request_id, gy_status=status)
            self.assertEqual(sync.call_count, 1)
            db.update_download_request(request_id, qb_status="failed")
            db.update_download_request(request_id, qb_status="submitted")
            self.assertEqual(sync.call_count, 2)

    def test_request_acceptance_and_rss_projection_rollback_together(self):
        from app.repositories.download_requests import _update_download_request_conn
        from app.repositories.rss import bind_rss_entry_download

        request_id = self.request(target="guangya", peer_status="submitted")
        sub = db.add_rss_subscription("rollback", "https://fixture.invalid/rss", download_method="guangya")
        entry = db.add_rss_entry_with_media(sub, "rollback", "rollback")["id"]
        self.assertTrue(db.claim_rss_entry(entry))
        get_download_request_result_5 = db.get_download_request(request_id)
        assert get_download_request_result_5 is not None
        bind_rss_entry_download(entry, get_download_request_result_5["request_key"], "guangya")
        db.record_rss_entry_failure(entry, "guangya_manifest_unavailable", True)
        with self.assertRaisesRegex(RuntimeError, "rollback fixture"):
            with db.get_conn() as conn:
                _update_download_request_conn(conn, request_id, {"gy_status": "submitted"}, db.now())
                self.assertEqual(conn.execute("SELECT status FROM rss_entries WHERE id=?", (entry,)).fetchone()[0], "downloaded")
                raise RuntimeError("rollback fixture")
        get_download_request_result_6 = db.get_download_request(request_id)
        assert get_download_request_result_6 is not None
        self.assertEqual(get_download_request_result_6["gy_status"], "failed")
        get_rss_entry_result = db.get_rss_entry(entry)
        assert get_rss_entry_result is not None
        self.assertEqual(get_rss_entry_result["status"], "failed")


class DownloadSubmissionLogFailureTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        for target, name in (
            (socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "getaddrinfo"),
        ):
            self.enterContext(patch.object(target, name, side_effect=AssertionError("禁止测试访问网络")))
        self.enterContext(patch.object(
            dispatcher, "get", side_effect=lambda key, default="": "http://qb.invalid" if key == "QB_URL" else default,
        ))
        self.enterContext(patch.object(
            dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True, reason=""),
        ))
        self.qb = self.enterContext(patch.object(dispatcher, "_submit_qb", side_effect=lambda row, **kwargs: {
            "ok": True, "task_id": dispatcher.torrent_identity(row),
        }))
        self.gy = self.enterContext(patch.object(dispatcher, "_submit_guangya", return_value={
            "ok": True, "task_id": "cloud-accepted", "task_ids": ["cloud-accepted"],
        }))
        self.engine = RSSEngine()
        self.addCleanup(self.engine.close)
        self.serial = 0

    def item(self):
        self.serial += 1
        return dispatcher.normalize_download_url(f"magnet:?xt=urn:btih:{self.serial:040x}")

    def pending_rss_entry(self):
        item = self.item()
        sid = db.add_rss_subscription(f"日志故障 {self.serial}", f"https://feed.invalid/{self.serial}", download_method="qb")
        eid = db.add_rss_entry_with_media(
            sid, "Episode", f"guid-{self.serial}", payload=json.dumps({"torrent_url": item.source_value}),
        )["id"]
        return eid, item

    @staticmethod
    def reject_log_writes():
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER reject_download_log BEFORE INSERT ON download_log "
                "BEGIN SELECT RAISE(ABORT,'injected download log failure'); END"
            )

    def test_rss_log_failure_preserves_receipt_and_tracker_completion(self):
        eid, item = self.pending_rss_entry()
        self.reject_log_writes()
        result = self.engine.download(eid)
        self.assertTrue(result["ok"], result)
        request_id = result["request_id"]
        request = db.get_download_request(request_id)
        assert request is not None
        self.assertEqual((request["status"], request["qb_status"]), ("submitted", "submitted"))
        self.assertEqual(request["qb_task_id"], dispatcher.torrent_identity(request))
        entry = db.get_rss_entry(eid)
        assert entry is not None
        self.assertEqual((entry["status"], entry["processed"]), ("downloaded", 1))
        self.assertFalse(rss_entry_delivery(entry)["review_required"])
        self.assertEqual(db.list_download_logs(), [])
        self.assertTrue(self.engine.download(eid)["existing"])
        self.qb.assert_called_once()
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER reject_download_log")
        task = TorrentTask(
            hash=dispatcher.torrent_identity(request), name=item.title,
            progress=1.0, state="uploading", save_path="/downloads", content_path="/downloads/Episode.mkv",
            size=1024, downloaded=1024, dlspeed=0, upspeed=0, eta=0, ratio=0.0, category="", added_on=0,
        )
        tracker = DownloadTracker()
        with (
            patch.object(tracker, "_run_staging_reconciliation_if_due", return_value=0),
            patch.object(tracker, "_run_torrent_data_cleanup_if_due", return_value=0),
            patch.object(tracker, "_qb_tasks", return_value=(True, [task])) as scan,
            patch.object(tracker, "_start_local_import") as local_import,
            patch.object(DownloadTracker, "_publish_lifecycle", return_value=False),
        ):
            self.assertEqual(tracker.run_once(), 1)
        scan.assert_called_once()
        local_import.assert_called_once()
        self.assertEqual(local_import.call_args.args[0]["qb_status"], "completed")
        request = db.get_download_request(request_id)
        assert request is not None
        self.assertEqual((request["status"], request["qb_status"]), ("completed", "completed"))
        self.assertEqual(request["notification_event_status"], "completed")
        entry = db.get_rss_entry(eid)
        assert entry is not None
        self.assertEqual(entry["processed"], 1)
        self.qb.assert_called_once()

    def test_log_failure_preserves_first_supplement_and_successor_results(self):
        self.reject_log_writes()
        for entry in ("first", "supplement", "successor"):
            for target in ("qb", "guangya", "both"):
                if entry == "supplement" and target == "both":
                    continue
                with self.subTest(entry=entry, target=target):
                    self.qb.reset_mock()
                    self.gy.reset_mock()
                    item = self.item()
                    source_id = 0
                    peer_status = "downloading" if entry == "supplement" else "completed"
                    if entry != "first":
                        source_id = dispatcher.create_request(item, "", "log-fault")["id"]
                        selected = {"qb", "guangya"} if target == "both" else {target}
                        db.update_download_request(
                            source_id, status="submitted" if entry == "supplement" else "completed",
                            targets="both", qb_status="failed" if "qb" in selected else peer_status,
                            gy_status="failed" if "guangya" in selected else peer_status,
                            qb_task_id=dispatcher.torrent_identity({"kind": "magnet", "source_value": item.source_value}),
                            gy_task_id="cloud-kept", gy_task_ids='["cloud-kept"]',
                        )
                    if entry == "successor":
                        result = dispatcher.resubmit_download_request(source_id, target)
                        self.assertNotEqual(result["request_id"], source_id)
                    else:
                        result = submit_download_input(item, target, origin="log-fault")["dispatch"]
                        if source_id:
                            self.assertEqual(result["request_id"], source_id)
                    self.assertTrue(result["ok"], result)
                    self.assertFalse(result["review_required"], result)
                    request = db.get_download_request(result["request_id"])
                    assert request is not None
                    for backend, field, submit in (("qb", "qb_status", self.qb), ("guangya", "gy_status", self.gy)):
                        expected = target in {backend, "both"}
                        self.assertEqual(submit.call_count, int(expected))
                        if expected:
                            self.assertEqual(request[field], "submitted")
                            if entry == "successor":
                                source = db.get_download_request(source_id)
                                assert source is not None
                                self.assertEqual(source[field], "resubmitted")
                        elif source_id:
                            source = db.get_download_request(source_id)
                            assert source is not None
                            self.assertEqual(source[field], peer_status)

    def test_log_failure_keeps_unknown_backend_receipts_blocked(self):
        self.reject_log_writes()
        for backend_error in (False, True):
            with self.subTest(backend_error=backend_error):
                self.qb.reset_mock()
                self.qb.side_effect = RuntimeError("backend receipt unavailable") if backend_error else None
                self.qb.return_value = {"ok": False, "failure_code": "qb_outcome_unknown", "error": "回执未知"}
                eid, _item = self.pending_rss_entry()
                result = self.engine.download(eid)
                self.assertFalse(result["ok"], result)
                self.assertTrue(result["review_required"], result)
                request = db.get_download_request(result["request_id"])
                assert request is not None
                self.assertEqual(request["qb_status"], "outcome_unknown")
                entry = db.get_rss_entry(eid)
                assert entry is not None
                self.assertEqual((entry["status"], entry["processed"]), ("failed", 0))
                self.assertTrue(rss_entry_delivery(entry)["review_required"])
                self.assertFalse(self.engine.download(eid)["ok"])
                self.qb.assert_called_once()

    def test_failed_acceptance_transaction_still_requires_manual_review(self):
        self.reject_log_writes()
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER reject_download_acceptance BEFORE UPDATE ON download_requests "
                "WHEN NEW.qb_status='submitted' "
                "BEGIN SELECT RAISE(ABORT,'injected acceptance failure'); END"
            )
        item = self.item()
        result = submit_download_input(item, "qb", origin="acceptance-fault")
        self.assertEqual(result["summary"]["status"], "manual_review")
        self.assertFalse(result["summary"]["ok"])
        request = db.get_download_request(result["request_id"])
        assert request is not None
        self.assertEqual((request["status"], request["qb_status"]), ("manual_review", "manual_review"))
        self.assertEqual(request["qb_task_id"], f"{self.serial:040x}")
        self.assertEqual(db.list_download_logs(), [])
        repeated = submit_download_input(item, "qb", origin="acceptance-fault")
        self.assertTrue(repeated["summary"]["duplicate"])
        self.assertEqual(repeated["request_id"], result["request_id"])
        self.qb.assert_called_once()


class GuangYaRetryDestinationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.sources = [{"id": "adult-folder", "name": "NewNsfw"}]
        self.enterContext(patch("app.modules.organize_sources.list_nsfw_download_sources", side_effect=lambda: self.sources))
        self.enterContext(patch.object(dispatcher, "_recover_guangya_magnet_torrent", return_value=None))
        self.enterContext(patch.object(dispatcher, "get", side_effect=lambda key, default="": "http://qb.invalid" if key == "QB_URL" else default))
        self.enterContext(patch.object(dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True, reason="")))

    def request(self, serial):
        return dispatcher.create_request(dispatcher.DownloadInput(
            "magnet", "NSFW sample", f"magnet:?xt=urn:btih:{serial:040x}",
        ), "", "retry-route-test")["id"]

    def test_early_failure_preserves_nsfw_destination_for_both_retry_paths(self):
        for serial, active_peer in enumerate((False, True), 1):
            with self.subTest(active_peer=active_peer):
                request_id = self.request(serial)
                targets = "both" if active_peer else "guangya"
                with patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "q"}), patch.object(
                    dispatcher, "submit_offline", return_value={"ok": False, "error": "解析暂不可用"},
                ):
                    dispatcher.dispatch_request(request_id, targets, gy_target_dir="adult-folder", gy_target_name="NewNsfw")
                saved = db.get_download_request(request_id)
                assert saved is not None
                self.assertEqual(saved["gy_requested_target_dir"], "adult-folder")
                self.assertEqual(saved["gy_requested_nsfw"], 1)
                with patch.object(dispatcher, "submit_offline", return_value={"ok": True, "task_ids": ["accepted"]}) as submit:
                    result = dispatcher.resubmit_download_request(request_id, "guangya")
                self.assertTrue(result["ok"], result)
                self.assertEqual(submit.call_args.kwargs["target_dir_id"], "adult-folder")
                self.assertEqual(result["request_id"] == request_id, active_peer)
                successor = db.get_download_request(result["request_id"])
                assert successor is not None
                self.assertEqual(successor["gy_requested_target_dir"], "adult-folder")
                self.assertEqual(successor["gy_requested_nsfw"], 1)

    def test_removed_nsfw_source_fails_closed_but_does_not_block_qb_retry(self):
        request_id = self.request(3)
        with patch.object(dispatcher, "submit_offline", return_value={"ok": False, "error": "暂不可用"}):
            dispatcher.dispatch_request(request_id, "guangya", gy_target_dir="adult-folder")
        self.sources = []
        with patch.object(dispatcher, "submit_offline") as submit:
            result = dispatcher.resubmit_download_request(request_id, "guangya")
        self.assertFalse(result["ok"])
        self.assertIn("NSFW", result["error"])
        submit.assert_not_called()
        with patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "q"}) as qb:
            result = dispatcher.resubmit_download_request(result["request_id"], "qb")
        self.assertTrue(result["ok"], result)
        qb.assert_called_once()

    def test_legacy_staging_parent_is_preserved_not_reused_as_staging_child(self):
        request_id = self.request(4)
        db.update_download_request(request_id, status="failed", targets="guangya", gy_status="failed",
                                   gy_isolated=1, gy_target_dir="old-stage", gy_staging_parent_dir="adult-folder")
        with patch.object(dispatcher, "submit_offline", return_value={"ok": True, "task_ids": ["accepted"]}) as submit:
            result = dispatcher.resubmit_download_request(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assertEqual(submit.call_args.kwargs["target_dir_id"], "adult-folder")

    def test_unselected_normal_request_keeps_offline_default_resolution(self):
        request_id = self.request(5)
        with patch.object(dispatcher, "submit_offline", return_value={"ok": True, "task_ids": ["accepted"]}) as submit:
            result = dispatcher.dispatch_request(request_id, "guangya")
        self.assertTrue(result["ok"], result)
        self.assertEqual(submit.call_args.kwargs["target_dir_id"], "")

    def test_existing_database_adds_routing_columns_without_changing_history(self):
        request_id = self.request(6)
        db.update_download_request(request_id, status="failed", gy_staging_parent_dir="adult-folder")
        get_download_request_result_9 = db.get_download_request(request_id)
        assert get_download_request_result_9 is not None
        original = dict(get_download_request_result_9)
        with db.get_conn() as conn:
            conn.execute("ALTER TABLE download_requests DROP COLUMN gy_requested_target_dir")
            conn.execute("ALTER TABLE download_requests DROP COLUMN gy_requested_nsfw")
        db.init_db()
        get_download_request_result_10 = db.get_download_request(request_id)
        assert get_download_request_result_10 is not None
        restored = dict(get_download_request_result_10)
        self.assertEqual(restored["gy_requested_target_dir"], "")
        self.assertEqual(restored["gy_requested_nsfw"], 0)
        for key in ("id", "source_value", "status", "gy_staging_parent_dir"):
            self.assertEqual(restored[key], original[key])
