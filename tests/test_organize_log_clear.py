"""清理和列表必须使用同一范围，真实临时数据库验证，不接触媒体。"""
import json
import re
import socket
import unittest
from typing import Any
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import JSONResponse
from fastapi.testclient import TestClient

from app import config
from app import database as db
from app.repositories import local_media as local_media_repo
from app.repositories import organize_history
from app.repositories import download_requests as download_requests_repo
from app.main import create_app
from app.modules.organize import Organizer
from app.routes.logs_api import clear_organize_log_records
from tests.support import assert_present, initialized_web_runtime, isolated_test_database


class OrganizeLogClearTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("unexpected network")))

    def cloud(self, status="failed", name="Match", **kwargs):
        return db.add_organize_log("guangya", "/" + name, "/target", name, status,
                                   original_name=name + ".mkv", **kwargs)

    def local(self, status="failed", name="Match", owner="admin"):
        source = local_media_repo.create_local_media_source(f"local-{status}-{name}", "", "", "/downloads", owner=owner)
        task = local_media_repo.create_local_media_task(source, "", f"/downloads/{status}-{name}", owner=owner, trigger="manual")
        local_media_repo.update_local_media_task(task, owner=owner, status=status)
        return task

    def test_filters_match_listing_across_pages_and_retry_is_idempotent(self):
        ids = [self.cloud(name=f"Match-{i}") for i in range(205)]
        kept = [self.cloud("success"), self.cloud(name="Other"), self.local()]
        filters = {"origin": "guangya", "status": "failed", "keyword": "Match"}
        self.assertEqual(db.count_organize_timeline(**filters), 205)
        self.assertEqual(len(db.list_organize_timeline(**filters)), 20)
        result = db.clear_organize_logs(**filters)
        self.assertEqual(result["deleted"], 205)
        self.assertTrue(all(db.get_organize_log(i) is None for i in ids))
        self.assertTrue(all(db.get_organize_log(i) is not None for i in kept[:2]))
        self.assertIsNotNone(local_media_repo.get_local_media_task(kept[2], owner="admin"))
        self.assertEqual(db.clear_organize_logs(**filters)["deleted"], 0)

    def test_status_mapping_and_keyword_are_shared_with_listing(self):
        for status in ("failed", "partial_failed", "interrupted", "revert_failed", "deleted", "success", "skipped", "reverted"):
            self.cloud(status, name=status)
        self.local("completed", "local-completed")
        self.local("failed", "local-failed")
        selected = db.list_organize_timeline(status="issues", limit=100)
        result = db.clear_organize_logs(status="issues")
        self.assertEqual(result["deleted"], len(selected))
        self.assertEqual(db.count_organize_timeline(status="failed"), 0)
        self.assertEqual({row["raw_status"] for row in db.list_organize_timeline(status="deleted")}, {"deleted"})
        self.assertEqual(db.clear_organize_logs(status="deleted")["deleted"], 1)
        self.assertEqual(db.clear_organize_logs(status="deleted")["deleted"], 0)
        self.assertEqual(db.clear_organize_logs(origin="local", status="success")["deleted"], 1)
        self.assertIsNotNone(db.list_organize_timeline(status="success")[0])

    def test_pending_processing_and_unmapped_states_are_kept(self):
        kept = [self.cloud("manual"), self.cloud("reorganizing"), self.cloud("planned")]
        kept.append(self.cloud("skipped", "legacy", error="等待人工确认"))
        tasks = [self.local("requires_manual"), self.local("moving")]
        self.cloud("success", "done")
        result = db.clear_organize_logs()
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["skipped_busy"], 6)
        self.assertTrue(all(db.get_organize_log(i) is not None for i in kept))
        self.assertTrue(all(local_media_repo.get_local_media_task(i, owner="admin") is not None for i in tasks))
        self.assertEqual(db.clear_organize_logs(status="manual")["deleted"], 0)

    def test_active_confirmation_protects_terminal_records_until_finished(self):
        cloud = self.cloud("failed", "review")
        token_log = self.cloud("success", "new", operation_token="recognition-confirm:review-token")
        local = self.local("failed", "local-review")
        db.create_organize_confirmation(
            token="review-token", fingerprint="review", chat_id="", source_name="", directory_path="/",
            payload={"files": [{"file_id": "review"}], "local_task_id": local}, expires_at="2099-01-01 00:00:00",
        )
        result = db.clear_organize_logs()
        self.assertEqual((result["deleted"], result["skipped_busy"]), (0, 3))
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET status='completed'")
        self.assertEqual(db.clear_organize_logs()["deleted"], 3)
        self.assertIsNone(db.get_organize_log(cloud))
        self.assertIsNone(db.get_organize_log(token_log))
        self.assertIsNotNone(db.get_organize_confirmation("review-token"))

    def confirmation(self, token, files, *, status="running", result=None):
        db.create_organize_confirmation(
            token=token, fingerprint=token, chat_id="", source_name="", directory_path="/",
            payload={"files": [{"file_id": file_id} for file_id in files]},
            expires_at="2099-01-01 00:00:00",
        )
        db.update_organize_confirmation(
            token, status=status, result_json=json.dumps(result or {}),
            completed_at=db.now() if status == "completed" else None,
        )

    def pending_group(self, token, files, *, source="scan-root", companions=None, **payload_fields):
        with patch.object(db, "now", return_value="2026-11-14 18:27:00"):
            db.create_organize_confirmation(
                token=token, fingerprint=token, chat_id="", source_name="Synthetic scan", directory_path="/",
                payload={"source_dir_id": source, "files": files, "companions": companions or [], **payload_fields},
                expires_at="2026-11-15 18:27:00",
            )

    def committed_audit(self, file_id, *, source="scan-root", parent="leaf", stamp="2026-11-14 19:18:00"):
        with patch.object(db, "now", return_value=stamp):
            return Organizer._write_organize_audit(
                ("guangya", "/" + file_id, "/target/" + file_id, file_id, "success"),
                {"operation_token": "manual-" + file_id, "source_dir_id": source,
                 "original_parent_id": parent, "original_name": file_id, "current_parent_id": "target",
                 "target_parent_id": "target", "current_name": file_id},
                [{"file_id": file_id, "role": "video", "original_parent_id": parent,
                  "current_parent_id": "target", "target_parent_id": "target", "current_name": file_id,
                  "target_name": file_id, "status": "success", "size": 100, "etag": "hash"}],
            )

    def test_restart_repairs_production_shaped_inplace_and_directory_audits_only(self):
        # 生产 API 的日志 ID、时序和入口形状；确认 payload 为合成冻结快照。
        records = (
            (10133, "inplace", "scan-root", "leaf-a", "reorganize", "16:55:35", "20:17:25", 3),
            (11207, "directory-a", "leaf-b", "leaf-b", "organize", "19:18:18", "19:18:18", 1),
            (11206, "directory-b", "leaf-c", "leaf-c", "organize", "19:18:05", "19:18:05", 1),
        )
        for log_id, file_id, source, parent, operation, created, updated, version in records:
            with patch.object(db, "now", return_value="2026-11-14 " + created):
                generated = self.cloud("success", file_id, source_dir_id=source, original_parent_id=parent,
                                       current_parent_id="target", target_parent_id="target", current_name=file_id,
                                       operation_type=operation, operation_token="manual-" + file_id)
            with db.get_conn() as conn:
                conn.execute("UPDATE organize_log SET id=?,version=?,updated_at=? WHERE id=?",
                             (log_id, version, "2026-11-14 " + updated, generated))
            db.add_organize_log_items(log_id, [{
                "file_id": file_id, "role": "video", "original_parent_id": parent,
                "current_parent_id": "target", "target_parent_id": "target", "current_name": file_id,
                "target_name": file_id, "size": 100, "etag": "hash", "status": "success",
            }])
            self.pending_group(file_id, [{"file_id": file_id, "parent_id": parent, "size": 100, "etag": "hash"}],
                               _organize_log_upper_bound=11205)
        with patch.object(db, "now", return_value="2026-11-14 20:17:25"):
            organize_history.add_organize_operation_step(
                10133, "manual-inplace", 1, "move_rename", file_id="inplace", status="success",
                from_parent_id="leaf-a", to_parent_id="target", to_name="inplace", finished_at=db.now(),
            )
        manual_ids = [self.cloud("manual", name) for name in ("real-manual-a", "real-manual-b")]
        for name in ("real-manual-a", "real-manual-b"):
            self.pending_group(name, [{"file_id": name, "parent_id": "leaf", "size": 100, "etag": "hash"}])
        with db.get_conn() as conn:
            for token, review in (("inplace", "waiting"), ("directory-a", "pending"), ("directory-b", "running")):
                conn.execute("UPDATE organize_confirmations SET review_status=? WHERE token=?", (review, token))
        self.assertEqual(db.count_organize_timeline(), 5)
        db.init_db()
        for log_id, file_id, *_ in records:
            card = assert_present(db.get_organize_confirmation(file_id))
            self.assertEqual((card["status"], card["review_status"]), ("completed", "cancelled"))
            self.assertEqual(json.loads(card["result_json"])["organize_log_ids"], [log_id])
            self.assertTrue(card["review_completed_at"])
            self.assertFalse(db.stage_organize_confirmation_review_result(file_id, {}))
        snapshots = [dict(assert_present(db.get_organize_confirmation(name))) for name in ("inplace", "directory-a", "directory-b")]
        db.init_db()
        self.assertEqual([dict(assert_present(db.get_organize_confirmation(name))) for name in ("inplace", "directory-a", "directory-b")], snapshots)
        result = db.clear_organize_logs()
        self.assertEqual((result["deleted"], result["skipped_busy"]), (3, 2))
        self.assertTrue(all(assert_present(db.get_organize_log(log_id)) is not None for log_id in manual_ids))
        self.assertTrue(all(assert_present(db.get_organize_confirmation(name))["status"] == "pending"
                            for name in ("real-manual-a", "real-manual-b")))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_staging_reconcile").fetchone()[0], 0)

    def test_live_audit_closes_only_matching_group_after_all_members_commit(self):
        files = [{"file_id": name, "parent_id": "leaf", "size": 100, "etag": "hash"} for name in ("first", "second")]
        self.pending_group("group", files)
        self.pending_group("unrelated", [{"file_id": "unrelated", "parent_id": "leaf", "size": 100}])
        first = self.committed_audit("first", source="leaf")
        self.assertEqual(assert_present(db.get_organize_confirmation("group"))["status"], "pending")
        second = self.committed_audit("second", source="leaf")
        card = assert_present(db.get_organize_confirmation("group"))
        self.assertEqual(card["status"], "completed")
        self.assertEqual(json.loads(card["result_json"])["organize_log_ids"], [first, second])
        self.assertEqual(assert_present(db.get_organize_confirmation("unrelated"))["status"], "pending")

    def test_clear_repairs_only_selected_success_files_without_restart(self):
        selected = self.committed_audit("selected")
        other = self.committed_audit("other")
        for file_id in ("selected", "other"):
            self.pending_group(file_id, [{"file_id": file_id, "parent_id": "leaf", "size": 100}],
                               _organize_log_upper_bound=0)
        result = db.clear_organize_logs(origin="guangya", status="success", keyword="selected")
        self.assertEqual((result["deleted"], result["skipped_busy"]), (1, 0))
        self.assertIsNone(db.get_organize_log(selected))
        self.assertIsNotNone(db.get_organize_log(other))
        self.assertEqual(assert_present(db.get_organize_confirmation("selected"))["status"], "completed")
        self.assertEqual(assert_present(db.get_organize_confirmation("other"))["status"], "pending")

    def test_new_confirmation_and_unrelated_source_do_not_reuse_old_success(self):
        log_id = self.committed_audit("old-success")
        for token, stamp, source in (("same-second", "2026-11-14 19:18:00", "scan-root"),
                                     ("later", "2026-11-14 19:19:00", "scan-root"),
                                     ("different-root", "2026-11-14 18:27:00", "unrelated-root")):
            self.pending_group(token, [{"file_id": "old-success", "parent_id": "leaf", "size": 100, "etag": "hash"}], source=source)
            with db.get_conn() as conn:
                conn.execute("UPDATE organize_confirmations SET created_at=? WHERE token=?", (stamp, token))
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(file_ids=["old-success"]), [])
        self.assertIsNotNone(db.get_organize_log(log_id))

    def test_live_reconciliation_rolls_back_review_and_confirmation_but_keeps_success_audit(self):
        self.pending_group("atomic-live", [{"file_id": "atomic-live", "parent_id": "leaf", "size": 100}])
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET review_status='running'")
            conn.execute("CREATE TRIGGER reject_terminal_receipt BEFORE INSERT ON organize_confirmation_delivery_outbox "
                         "BEGIN SELECT RAISE(ABORT,'receipt-failure'); END")
        log_id = self.committed_audit("atomic-live")
        self.assertEqual(assert_present(db.get_organize_log(log_id))["status"], "success")
        card = assert_present(db.get_organize_confirmation("atomic-live"))
        self.assertEqual((card["status"], card["review_status"]), ("pending", "running"))
        with db.get_conn() as conn:
            conn.execute("DROP TRIGGER reject_terminal_receipt")
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(file_ids=["atomic-live"]), ["atomic-live"])

    def test_queued_running_and_real_review_history_survive_live_audit(self):
        for state in ("queued", "running", "approved", "abstained"):
            with self.subTest(state=state):
                self.pending_group(state, [{"file_id": state, "parent_id": "leaf", "size": 100}])
                if state in {"queued", "running"}:
                    db.update_organize_confirmation(state, status=state)
                else:
                    db.update_organize_confirmation(state, review_status=state, review_result_json='{"reason":"actual"}',
                                                   review_completed_at="2026-11-14 18:30:00")
                self.committed_audit(state)
                card = assert_present(db.get_organize_confirmation(state))
                if state in {"queued", "running"}:
                    self.assertEqual(card["status"], state)
                else:
                    self.assertEqual((card["status"], card["review_status"]), ("completed", state))
                    self.assertEqual(card["review_completed_at"], "2026-11-14 18:30:00")
                    self.assertEqual(json.loads(card["review_result_json"]), {"reason": "actual"})

    def test_broken_ambiguous_cancelled_or_cross_root_download_binding_stays_protected(self):
        for case in ("missing", "mismatched-task", "cancelled", "ambiguous", "cross-root", "isolated-entry"):
            with self.subTest(case=case):
                source = "stage-" + case
                fields: dict[str, Any] = {}
                if case == "missing":
                    fields["download_request_ids"] = [999999]
                else:
                    for index in range(2 if case == "ambiguous" else 1):
                        request_id, _ = download_requests_repo.create_download_request(case + str(index), "magnet")
                        download_requests_repo.update_download_request(
                            request_id, targets="guangya", status="cancelled" if case == "cancelled" else "completed",
                            gy_status="completed", gy_isolated=1, gy_target_dir="isolated-leaf" if case == "isolated-entry" else source,
                            gy_staging_parent_dir="incoming", gy_staging_name="MF-test", organize_started=1,
                            organize_status="requires_manual", organize_task_id="original-task",
                        )
                    if case == "mismatched-task":
                        fields["organize_task_id"] = "different-task"
                parent = "isolated-leaf" if case == "isolated-entry" else "leaf"
                self.pending_group(case, [{"file_id": case, "parent_id": parent, "size": 100}], source=source, **fields)
                self.committed_audit(case, parent=parent, source=parent if case in {"cross-root", "isolated-entry"} else source)
                self.assertEqual(assert_present(db.get_organize_confirmation(case))["status"], "pending")
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_staging_reconcile").fetchone()[0], 0)

    def test_unfinished_companion_keeps_whole_ordinary_group_pending(self):
        self.pending_group("with-subtitle", [{"file_id": "video", "parent_id": "leaf", "size": 100}],
                           companions=[{"file_id": "subtitle", "parent_id": "leaf", "size": 10, "etag": "sub-hash"}])
        log_id = self.committed_audit("video", source="leaf")
        db.add_organize_log_items(log_id, [{
            "file_id": "subtitle", "role": "subtitle", "original_parent_id": "leaf", "status": "failed",
            "current_parent_id": "target", "target_parent_id": "target", "current_name": "subtitle",
            "target_name": "subtitle", "size": 10, "etag": "sub-hash",
        }])
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(file_ids=["video"]), [])
        subtitle = next(item for item in db.list_organize_log_items(log_id) if item["file_id"] == "subtitle")
        db.update_organize_log_item(subtitle["id"], status="success")
        self.assertEqual(organize_history.reconcile_reorganized_confirmations(file_ids=["video"]), ["with-subtitle"])

    def test_completed_confirmation_settles_skipped_and_failed_file_receipts_atomically(self):
        previous = [self.cloud("manual", name) for name in ("skipped-file", "failed-file")]
        self.confirmation("partial", ["skipped-file", "failed-file"])
        receipts = [self.cloud(status, name, operation_token="recognition-confirm:partial")
                    for status, name in (("skipped", "skipped-file"), ("failed", "failed-file"))]
        db.complete_organize_confirmation_with_delivery(
            "partial", result_json=json.dumps({"skipped": 1, "failed": 1}),
            event_json="{}", chat_id="", message_id=None, enqueue_delivery=False,
        )
        for log_id in previous:
            log = db.get_organize_log(log_id)
            assert log is not None
            self.assertEqual(log["status"], "confirmed")
        self.assertEqual({r["id"] for r in db.list_organize_timeline()}, set(receipts))
        self.assertEqual(db.clear_organize_logs()["deleted"], 2)

    def test_restart_recovers_legacy_skipped_member_and_clear_is_idempotent(self):
        # 生产中入库成员与确认完成在同一事务写入同一时间戳，恢复以此识别入库成员。
        with patch.object(db, "now", return_value="2026-11-09 01:00:00"):
            loser = self.cloud("manual", "loser")
            winner = self.cloud("confirmed", "winner")
            db.add_organize_log_items(loser, [{"file_id": "loser", "status": "manual"}])
            self.confirmation("legacy", ["loser", "winner"], status="completed", result={
                "total": 2, "moved": 1, "skipped": 1, "failed": 0, "need_confirm": 0,
            })
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=json_remove(payload_json,'$._organize_log_upper_bound')")
            conn.execute("UPDATE organize_log SET created_at='2026-10-01 00:00:00'")
            conn.execute("UPDATE organize_log SET updated_at=created_at WHERE id=?", (loser,))
        db.init_db()
        log = db.get_organize_log(loser)
        assert log is not None
        self.assertEqual(log["status"], "skipped")
        self.assertIn("入库 1 个、跳过 1 个", log["error"])
        self.assertEqual(db.list_organize_log_items(loser)[0]["status"], "skipped")
        db.init_db()
        recovered = db.get_organize_log(loser)
        unchanged = db.get_organize_log(winner)
        assert recovered is not None and unchanged is not None
        self.assertEqual(recovered["version"], log["version"])
        self.assertEqual(unchanged["status"], "confirmed")
        self.assertEqual(db.clear_organize_logs(status="skipped")["deleted"], 1)
        self.assertEqual(db.clear_organize_logs(status="skipped")["deleted"], 0)

    def test_mixed_group_counts_cannot_identify_skipped_member_without_winner_evidence(self):
        first, second = self.cloud("manual", "first"), self.cloud("manual", "second")
        self.confirmation("mixed", ["first", "second"], status="completed", result={
            "total": 2, "moved": 1, "skipped": 1, "failed": 0, "need_confirm": 0,
        })
        self.confirmation("new-second", ["second"], status="pending")
        db.init_db()
        for log_id in (first, second):
            log = db.get_organize_log(log_id)
            assert log is not None
            self.assertEqual(log["status"], "manual")

    def test_confirmation_success_preserves_new_manual_log_in_same_second(self):
        with patch.object(db, "now", return_value="2026-11-09 01:00:00"):
            previous = self.cloud("manual", "same-file")
            self.confirmation("frozen", ["same-file"])
            newer = self.cloud("manual", "same-file")
            Organizer._write_organize_audit(
                ("guangya", "/same-file", "/target", "same-file", "success"),
                {"operation_token": "recognition-confirm:frozen"},
                [{"file_id": "same-file", "status": "success"}],
            )
            db.complete_organize_confirmation_with_delivery(
                "frozen", result_json='{"total":1,"moved":1,"skipped":0,"failed":0,"need_confirm":0}',
                event_json="{}", chat_id="", message_id=None, enqueue_delivery=False,
            )
        old_log, new_log = db.get_organize_log(previous), db.get_organize_log(newer)
        assert old_log is not None and new_log is not None
        self.assertEqual(old_log["status"], "confirmed")
        self.assertEqual(new_log["status"], "manual")
        self.assertEqual([r["id"] for r in db.list_organize_timeline(status="manual")], [newer])

    def test_completed_history_does_not_close_new_or_uncertain_manual_work(self):
        base = {"total": 1, "moved": 0, "skipped": 1, "failed": 0, "need_confirm": 0}
        kept = []
        for name, result in (
            ("new-card", base), ("new-log", base), ("unfinished", {**base, "need_confirm": 1}),
            ("failed", {**base, "failed": 1}), ("malformed", {**base, "skipped": "1"}),
            ("missing-result", {}), ("stopped", {**base, "stopped": 1}),
        ):
            kept.append(self.cloud("manual", name))
            self.confirmation(name, [name], status="completed", result=result)
        self.confirmation("newer", ["new-card"], status="pending")
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_log SET updated_at='2099-01-01 00:00:00' WHERE file_id='new-log'")
        db.init_db()
        for log_id in kept:
            log = db.get_organize_log(log_id)
            assert log is not None
            self.assertEqual(log["status"], "manual")
        self.assertEqual(db.clear_organize_logs()["skipped_busy"], len(kept))

    def test_confirmation_log_settlement_failure_rolls_back_terminal_state(self):
        pending = self.cloud("manual", "atomic")
        self.confirmation("atomic", ["atomic"])
        self.cloud("skipped", "atomic", operation_token="recognition-confirm:atomic")
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_settlement BEFORE UPDATE ON organize_log "
                         "BEGIN SELECT RAISE(ABORT, 'settlement failure'); END")
        with self.assertRaisesRegex(Exception, "settlement failure"):
            db.complete_organize_confirmation_with_delivery(
                "atomic", result_json="{}", event_json="{}", chat_id="", message_id=None,
            )
        confirmation = db.get_organize_confirmation("atomic")
        log = db.get_organize_log(pending)
        assert confirmation is not None and log is not None
        self.assertEqual(confirmation["status"], "running")
        self.assertEqual(log["status"], "manual")

    def test_local_page_clear_and_timeline_share_confirmation_protection(self):
        pending = self.local("requires_manual", "pending")
        referenced = self.local("failed", "reviewing")
        finished = self.local("completed", "finished")
        db.create_organize_confirmation(
            token="local-pending", fingerprint="local-pending", chat_id="", source_name="", directory_path="/",
            payload={"local_task_id": pending}, expires_at="2099-01-01 00:00:00",
        )
        db.create_organize_confirmation(
            token="local-review", fingerprint="local-review", chat_id="", source_name="", directory_path="/",
            payload={"local_task_id": referenced}, expires_at="2099-01-01 00:00:00",
        )
        result = local_media_repo.delete_local_media_tasks([pending, referenced, finished, 999999])
        self.assertEqual(result, {"requested": 4, "deleted": 1, "skipped_busy": 2, "missing": 1})
        self.assertEqual(db.clear_organize_logs(origin="local")["skipped_busy"], 2)
        self.assertIsNotNone(local_media_repo.get_local_media_task(pending))
        self.assertIsNotNone(local_media_repo.get_local_media_task(referenced))
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET status='completed'")
        self.assertEqual(local_media_repo.delete_local_media_tasks([referenced])["deleted"], 1)
        # 确认已结束后仍可显式放弃人工任务；批量清空不会替用户做此决定。
        self.assertEqual(db.clear_organize_logs(origin="local")["deleted"], 0)
        self.assertEqual(local_media_repo.delete_local_media_tasks([pending])["deleted"], 1)

    def test_local_repository_does_not_coerce_invalid_task_ids(self):
        task = self.local("failed", "kept")
        invalid_ids: list[Any] = [True, 1.9, "1", -1, 0]
        for value in invalid_ids:
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_media_repo.delete_local_media_tasks([value])
        self.assertIsNotNone(local_media_repo.get_local_media_task(task))

    def test_children_outside_filter_and_other_owner_survive(self):
        parent = self.cloud("failed", "parent")
        child = self.cloud("success", "child", parent_log_id=parent)
        own, other = self.local(), self.local(owner="another-user")
        self.assertEqual(db.clear_organize_logs(status="failed")["deleted"], 2)
        self.assertIsNone(local_media_repo.get_local_media_task(own, owner="admin"))
        self.assertIsNotNone(local_media_repo.get_local_media_task(other, owner="another-user"))
        child_row = db.get_organize_log(child)
        assert child_row is not None
        self.assertIsNone(child_row["parent_log_id"])
        self.assertEqual(child_row["status"], "success")

    def test_database_failure_rolls_back_every_selected_row(self):
        first, second = self.cloud(name="first"), self.cloud(name="second")
        with db.get_conn() as conn:
            conn.execute("CREATE TRIGGER reject_clear BEFORE DELETE ON organize_log BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(Exception):
            db.clear_organize_logs()
        self.assertIsNotNone(db.get_organize_log(first))
        self.assertIsNotNone(db.get_organize_log(second))

    def test_api_rejects_invalid_filters_without_broadening_scope(self):
        kept = self.cloud()
        request = Request({"type": "http", "method": "DELETE", "path": "/api/logs/organize", "headers": []})
        with patch("app.routes.logs_api.require_api_login"):
            for field, value in (("origin", "unknown"), ("status", "unknown"), ("status", False), ("origin", []), ("q", None)):
                with self.subTest(field=field, value=value):
                    result = clear_organize_log_records(request, {"confirm": "CLEAR", field: value})
                    assert isinstance(result, JSONResponse)
                    self.assertEqual(result.status_code, 400)
                    self.assertIsNotNone(db.get_organize_log(kept))
            result = clear_organize_log_records(request, {"confirm": "CLEAR", "origin": "guangya", "status": "failed", "q": " Match "})
        assert isinstance(result, dict)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["q"], "Match")
        self.assertIn("未删除任何云端或本地媒体文件", result["message"])
        self.assertNotIn("error", json.dumps(result))

    def test_authenticated_http_listing_and_clear_use_the_same_real_database_scope(self):
        self.enterContext(initialized_web_runtime())
        selected = self.cloud(name="HTTP Match")
        cloud_other = self.cloud("success", "HTTP Match")
        local_other = self.local(name="HTTP Match")
        client = TestClient(create_app(start_background=False))
        self.addCleanup(client.close)

        def csrf(html):
            match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', html)
            assert match is not None
            return match.group(1)

        token = csrf(client.get('/login').text)
        username, password = config.web_credentials()
        login = client.post('/login', data={"username": username, "password": password, "csrf_token": token}, follow_redirects=False)
        self.assertEqual(login.status_code, 302)
        headers = {"X-CSRF-Token": csrf(client.get('/').text)}
        filters = {"origin": "guangya", "status": "failed", "q": "HTTP Match"}
        before = client.get('/api/logs/organize/timeline', params=filters)
        self.assertEqual(before.status_code, 200)
        self.assertEqual([item['id'] for item in before.json()['items']], [selected])
        result = client.request('DELETE', '/api/logs/organize', headers=headers, json={"confirm": "CLEAR", **filters})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['deleted'], before.json()['total'])
        self.assertEqual(client.get('/api/logs/organize/timeline', params=filters).json()['total'], 0)
        self.assertIsNotNone(db.get_organize_log(cloud_other))
        self.assertIsNotNone(local_media_repo.get_local_media_task(local_other, owner="admin"))
