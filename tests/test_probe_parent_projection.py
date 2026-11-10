"""普通生命周期重投影不能吞掉同父持久 probe 失败或提前宣布完成。"""
from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import strm as strm_repo
from app.repositories import organize_probe as organize_probe_repo
from app.modules import telegram_notification_center as center
from app.modules.organize_probe_notifications import (
    build_notification_context,
    publish_probe_scope,
    tag_probe_changes,
)
from app.modules.telegram_download_lifecycle import publish_download_lifecycle
from app.modules.telegram_organize_lifecycle import (
    organize_lifecycle_downstream_settled,
    update_organize_lifecycle_downstream,
)
from app.notifier import NotificationEvent, TelegramSendResult
from tests.support import IsolatedDatabaseTestCase


class ProbeParentProjectionTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        with db.get_conn() as conn:
            for table in ("organize_probe_queue", "organize_log", "strm_change_queue", "telegram_notification_outbox", "download_requests"):
                conn.execute(f"DELETE FROM {table}")
        stopped = center._dispatch_stop.is_set()
        center._dispatch_stop.clear()
        self.addCleanup(center._dispatch_stop.set if stopped else center._dispatch_stop.clear)
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("真实通知连接被禁止")))
        self.enterContext(patch("app.modules.telegram_notification_policy.notifications_enabled", return_value=True))
        self.enterContext(patch("app.modules.telegram_notification_policy.notification_level", return_value="standard"))
        self.enterContext(patch("app.modules.telegram_download_lifecycle.config.get_bool", return_value=True))
        self.send = self.enterContext(patch.object(center, "send_event_result", return_value=TelegramSendResult(ok=True, message_id=101)))
        self.edit = self.enterContext(patch.object(center, "edit_event_result", return_value=TelegramSendResult(ok=True, message_id=101)))
        self.request_id, _ = download_requests_repo.create_download_request("projection-synthetic", "magnet", chat_id="chat-a")
        download_requests_repo.update_download_request(self.request_id, status="completed", gy_status="completed", organize_status="completed", strm_status="completed")
        self.context = build_notification_context(download_request_ids=[self.request_id])
        publish_download_lifecycle(self.request_id)

    def snapshot(self) -> dict:
        with db.get_conn() as conn:
            row = dict(conn.execute("SELECT * FROM telegram_notification_outbox WHERE thread_key=? AND chat_id='chat-a'", (f"download:{self.request_id}",)).fetchone())
        row["event"] = json.loads(row["event_json"])
        return row

    def job(self, status: str, *, context=None, file_id="synthetic-file") -> int:
        log_id = db.add_organize_log("guangya", "old", "new", file_id, "success", "1")
        job_id = organize_probe_repo.enqueue_organize_probe_completion(log_id, source_id="synthetic-source", rel_dir="series", rules={}, notification_context=context or self.context)
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET status=?,pending_strm_changes_json='[]' WHERE id=?", (status, job_id))
        return job_id

    def probe_error(self) -> None:
        publish_probe_scope({"probe": True, "context": self.context, "notify_override": True}, strm_status="失败", media_refresh="未完成", partial=True, error="private-provider-error")

    def test_scheduler_projection_preserves_a_failed_probe_without_changing_business_state(self) -> None:
        from app.modules.scheduler import _publish_linked_notification_threads
        job_id = self.job("failed")
        self.probe_error()
        _publish_linked_notification_threads({"download_request_ids": [self.request_id], "notify_override": True}, strm_status="完成", media_refresh="完成")
        self.assertEqual(self.snapshot()["importance"], "error")
        self.assertIn("⚠️", self.snapshot()["event"]["title"])
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT status FROM organize_probe_queue WHERE id=?", (job_id,)).fetchone()[0], "failed")
        get_download_request_result = download_requests_repo.get_download_request(self.request_id)
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result["strm_status"], "completed")
        self.assertNotIn("private-provider-error", self.snapshot()["event_json"])
        self.assertEqual(self.send.call_count, 1)

    def test_task_projection_preserves_a_cancelled_probe(self) -> None:
        from app.modules.organize_tasks import _publish_download_lifecycles
        self.job("cancelled")
        self.probe_error()
        _publish_download_lifecycles([self.request_id])
        self.assertEqual(self.snapshot()["importance"], "error")
        self.assertEqual(self.snapshot()["event"]["state"], "partial")

    def test_pending_probe_cannot_be_projected_as_fully_completed(self) -> None:
        self.job("queued")
        publish_download_lifecycle(self.request_id)
        self.assertIn("处理中", self.snapshot()["event"]["title"])
        self.assertEqual(self.snapshot()["event"]["state"], "processing")
        self.assertIn("进行中", self.snapshot()["event_json"])

    def test_failed_strm_handoff_is_not_downgraded_to_success_or_processing(self) -> None:
        self.job("completed")
        change = {"source_id": "synthetic-source", "rel_dir": "series", "file_id": "synthetic-file", "kind": "video", "name": "file.mkv"}
        strm_repo.enqueue_strm_change_targets(tag_probe_changes([change], self.context, notify_enabled=True))
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_change_queue SET state='failed',last_error='synthetic read error'")
        self.probe_error()
        publish_download_lifecycle(self.request_id)
        self.assertEqual(self.snapshot()["importance"], "error")
        self.assertEqual(self.snapshot()["event"]["state"], "partial")

    def test_silent_and_other_chat_probe_failures_do_not_promote_this_parent(self) -> None:
        silent = build_notification_context(download_request_ids=[self.request_id], notify_enabled=False)
        self.job("failed", context=silent, file_id="silent-file")
        other = build_notification_context(download_request_ids=[self.request_id])
        other["chat_id"] = "chat-b"
        for ref in other["notification_threads"]:
            ref["chat_id"] = "chat-b"
        self.job("failed", context=other, file_id="other-chat-file")
        publish_download_lifecycle(self.request_id)
        self.assertEqual(self.snapshot()["importance"], "result")
        self.assertIn("✅", self.snapshot()["event"]["title"])
        self.assertEqual(self.send.call_count, 1)

    def test_action_priority_is_preserved_alongside_probe_failure(self) -> None:
        self.job("failed")
        download_requests_repo.update_download_request(self.request_id, organize_status="requires_manual")
        publish_download_lifecycle(self.request_id)
        self.assertEqual(self.snapshot()["importance"], "action")
        self.assertIn("需要处理", self.snapshot()["event"]["title"])
        self.assertIn("后台规格补全", self.snapshot()["event_json"])

    def test_confirmed_recovery_clears_only_probe_failure_not_business_failure(self) -> None:
        job_id = self.job("failed")
        self.probe_error()
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET status='completed' WHERE id=?", (job_id,))
        publish_download_lifecycle(self.request_id)
        self.assertEqual(self.snapshot()["importance"], "result")
        self.assertIn("✅", self.snapshot()["event"]["title"])
        download_requests_repo.update_download_request(self.request_id, organize_status="failed", organize_error="original business failure")
        publish_download_lifecycle(self.request_id)
        self.assertEqual(self.snapshot()["importance"], "error")
        self.assertIn("original business failure", self.snapshot()["event_json"])

    def test_persisted_silent_handoff_override_cannot_promote_parent_error(self) -> None:
        self.job("completed")
        change = {"source_id": "synthetic-source", "rel_dir": "series", "file_id": "synthetic-file", "kind": "video", "name": "file.mkv"}
        strm_repo.enqueue_strm_change_targets(tag_probe_changes([change], self.context, notify_enabled=False))
        target = strm_repo.claim_strm_change_targets(owner="silent-test")[0]
        strm_repo.fail_strm_change_target(target["id"], expected_owner="silent-test", expected_lease_generation=target["lease_generation"], error="private silent failure", max_attempts=1)
        publish_download_lifecycle(self.request_id)
        self.assertEqual(self.snapshot()["importance"], "result")
        self.assertNotIn("需要复核", self.snapshot()["event_json"])
        self.assertEqual(self.send.call_count, 1)

    def test_effective_silent_job_rule_is_not_overridden_by_frozen_identity(self) -> None:
        job_id = self.job("failed")
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET rules_json=? WHERE id=?", (json.dumps({"notify_enabled": False}), job_id))
        publish_download_lifecycle(self.request_id)
        self.assertEqual(self.snapshot()["importance"], "result")
        self.assertNotIn("需要复核", self.snapshot()["event_json"])

    def test_success_callback_preserves_existing_unresolved_failure_payload(self) -> None:
        self.job("completed")
        change = {"source_id": "synthetic-source", "rel_dir": "series", "file_id": "synthetic-file", "kind": "video", "name": "file.mkv"}
        strm_repo.enqueue_strm_change_targets(tag_probe_changes([change], self.context, notify_enabled=True))
        with db.get_conn() as conn:
            conn.execute("UPDATE strm_change_queue SET state='failed',last_error='still failed'")
        self.probe_error()
        before = self.snapshot()["event_json"]
        publish_probe_scope({"probe": True, "context": self.context, "notify_override": True}, strm_status="完成", media_refresh="完成")
        self.assertEqual(self.snapshot()["importance"], "error")
        self.assertEqual(self.snapshot()["event_json"], before)

    def test_unavailable_download_route_does_not_guess_default_recipient(self) -> None:
        with (patch.object(download_requests_repo, "get_download_request", side_effect=OSError("route unavailable")),
              patch("app.modules.organize_probe_notifications.get", return_value="admin")):
            context = build_notification_context(download_request_ids=[self.request_id])
        publish_probe_scope({"probe": True, "context": context, "notify_override": True}, strm_status="失败", media_refresh="", partial=True, error="private failure")
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_notification_outbox").fetchone()[0], 1)
        self.assertEqual(self.send.call_count, 1)

    def test_organize_probe_in_progress_remains_a_nonterminal_machine_state(self) -> None:
        center.publish_notification_thread("organize:synthetic-parent", NotificationEvent("✅ 目录刮削完成", fields=(("STRM", "完成"), ("媒体库", "完成")), state="completed"), topic="organize", importance="result", chat_id="chat-a")
        update_organize_lifecycle_downstream("synthetic-parent", chat_id="chat-a", strm_status="后台规格补全进行中", media_refresh="完成")
        snapshot = center.get_notification_thread_snapshot("organize:synthetic-parent", topic="organize", chat_id="chat-a")
        assert snapshot is not None
        self.assertEqual(snapshot.event.state, "queued")
        self.assertFalse(organize_lifecycle_downstream_settled(snapshot.event))
