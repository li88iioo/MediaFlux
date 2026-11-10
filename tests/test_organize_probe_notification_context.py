"""probe 补全的通知身份、收件范围和持久交接回归；全程隔离 DB / fake I/O。"""
from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient

import inspect
import json
from unittest.mock import Mock, patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import strm as strm_repo
from app.repositories import organize_probe as organize_probe_repo
from app.modules.organize import Organizer, OrganizeRules
from app.modules.organize_probe_worker import OrganizeProbeWorker
from tests.support import IsolatedDatabaseTestCase


class ProbeNotificationContextTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("禁止外联")))
        self.enterContext(patch("app.modules.organize.get", side_effect=lambda key, default="": {
            "GY_STRM_BASE_URL": "http://example.invalid", "STRM_ROOT": "/fake-strm",
        }.get(key, default)))

    def test_organize_context_is_available_before_execution(self):
        self.assertIn("notification_context", inspect.signature(Organizer.organize).parameters)
        context = {"version": 1, "task_id": "web-task", "chat_id": "chat-a", "notify_enabled": True}
        organizer = object.__new__(Organizer)
        with patch.object(organizer, "_organize", return_value=([], {})) as execute:
            organizer.organize("source", OrganizeRules(), dry_run=False, notification_context=context)
        self.assertEqual(execute.call_args.args[0].notification_context["task_id"], "web-task")

    def test_builder_snapshots_chat_without_rules_secrets(self):
        from app.modules.organize_probe_notifications import build_notification_context
        with patch("app.modules.organize_probe_notifications.get", return_value="chat-a"):
            context = build_notification_context(task_id="web-task", notify_enabled=True)
        self.assertEqual(context["chat_id"], "chat-a")
        self.assertEqual(context["notification_threads"][0]["thread_key"], "organize:web-task")
        self.assertNotIn("rules", context)

    def test_legacy_probe_never_reverts_to_foreground_summary(self):
        scheduler = Mock()
        scheduler.trigger.return_value = {"ok": True}
        change = {"source_id": "s", "file_id": "f", "name": "new.mkv", "kind": "video"}
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
            self.assertTrue(OrganizeProbeWorker()._handoff_pending({
                "pending_strm_changes_json": json.dumps([change]), "rules_json": "{}",
            }))
        forwarded = scheduler.trigger.call_args.kwargs["organize_changes"][0]
        self.assertIn("_probe_notification_context", forwarded)
        self.assertEqual(forwarded["_probe_notification_context"], {})
        self.assertFalse(scheduler.trigger.call_args.kwargs["force_full"])

    def test_queue_context_is_immutable_on_idempotent_enqueue(self):
        from app.modules.organize_probe_notifications import build_notification_context
        from app.repositories.organize_probe import enqueue_organize_probe_completion
        log_id = db.add_organize_log("guangya", "old", "new", "f", "success", "1")
        first = build_notification_context(task_id="parent-a", chat_id="chat-a")
        job_id = enqueue_organize_probe_completion(log_id, source_id="s", rel_dir="r", rules={}, notification_context=first)
        enqueue_organize_probe_completion(log_id, source_id="s", rel_dir="r", rules={}, notification_context=build_notification_context(task_id="parent-b", chat_id="chat-b"))
        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM organize_probe_queue WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(json.loads(row["notification_context_json"]), first)


class ProbeNotificationDeliveryTests(IsolatedDatabaseTestCase):
    def setUp(self):
        from app.modules import telegram_notification_center as center
        from app.notifier import TelegramSendResult
        self.center = center
        stopped = center._dispatch_stop.is_set()
        center._dispatch_stop.clear()
        self.addCleanup(center._dispatch_stop.set if stopped else center._dispatch_stop.clear)
        with db.get_conn() as conn:
            for table in ("organize_probe_queue", "organize_log_items", "organize_log", "strm_change_queue", "telegram_notification_outbox"):
                conn.execute(f"DELETE FROM {table}")
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("禁止外联")))
        self.enterContext(patch("app.modules.organize_probe_notifications.get", side_effect=lambda key, default="": "admin" if key == "TG_CHAT_ID" else default))
        self.enterContext(patch("app.modules.telegram_notification_policy.notifications_enabled", return_value=True))
        self.enterContext(patch("app.modules.telegram_notification_policy.notification_level", return_value="standard"))
        self.send = self.enterContext(patch.object(center, "send_event_result", return_value=TelegramSendResult(ok=True, message_id=101)))
        self.edit = self.enterContext(patch.object(center, "edit_event_result", return_value=TelegramSendResult(ok=True, message_id=101)))
        self.enterContext(patch("app.modules.organize.get", side_effect=lambda key, default="": {
            "GY_STRM_BASE_URL": "http://example.invalid", "STRM_ROOT": "/fake-strm",
        }.get(key, default)))

    def context(self, task="parent-a", chat="chat-a", **kwargs):
        from app.modules.organize_probe_notifications import build_notification_context
        return build_notification_context(task_id=task, chat_id=chat, **kwargs)

    def parent(self, context, *, state="completed", title="✅ 目录刮削完成", actions=()):
        from app.notifier import NotificationEvent
        return self.center.publish_notification_thread(
            "organize:" + context["task_id"],
            NotificationEvent(title, fields=(("STRM", "排队"), ("媒体库", "未完成")), actions=actions, state=state),
            topic="organize", importance="action" if actions else "result", chat_id=context["chat_id"],
        )

    def scope(self, context=None, *, enabled=True):
        return {"probe": True, "context": context or {}, "notify_override": enabled}

    def publish(self, context, *, partial=False, error=""):
        from app.modules.organize_probe_notifications import publish_probe_scope
        return publish_probe_scope(self.scope(context), strm_status="部分完成" if partial else "完成",
                                   media_refresh="完成", partial=partial, error=error)

    def rows(self):
        with db.get_conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM telegram_notification_outbox ORDER BY id")]

    def test_250_persisted_probe_handoffs_edit_one_parent_without_new_summary(self):
        from app.modules.organize_probe_notifications import notification_scopes
        from app.modules.scheduler import (
            STRMScheduler,
            _publish_linked_notification_threads,
        )
        context = self.context()
        self.parent(context)
        scheduler = STRMScheduler()
        captured = []

        def accepted(trigger, options):
            scheduler._run_lock.release()
            captured.append(options)
            _publish_linked_notification_threads(options, strm_status="完成", media_refresh="完成")
            scheduler._notify_scoped_results(options, {}, {}, 0.0, trigger, [], "/fake-strm")
            return {"ok": True, "queued": True}

        for index in range(250):
            log_id = db.add_organize_log("guangya", "old", "new", f"video-{index}", "success", "1")
            job_id = organize_probe_repo.enqueue_organize_probe_completion(log_id, source_id="s", rel_dir="season", rules={}, notification_context=context)
            with db.get_conn() as conn:
                conn.execute("UPDATE organize_probe_queue SET next_attempt_at='2000-01-01 00:00:00',pending_strm_changes_json=? WHERE id=?", (json.dumps([{
                    "source_id": "s", "rel_dir": "season", "kind": "video", "file_id": f"video-{index}", "name": f"new-{index}.mkv",
                }]), job_id))
        worker = OrganizeProbeWorker()
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), \
                patch.object(scheduler, "_start_locked_worker", side_effect=accepted), \
                patch.object(scheduler, "_notify_success") as summary, \
                patch.object(scheduler, "_notify_details") as details, \
                patch.object(worker, "_runtime_client", side_effect=AssertionError("交接不能重 probe/云盘 I/O")):
            for _ in range(250):
                self.assertTrue(worker._process_one())
            self.assertFalse(worker._process_one())
        summary.assert_not_called()
        details.assert_not_called()
        self.assertEqual(len(captured), 250)
        self.assertEqual({json.dumps(notification_scopes(item), sort_keys=True) for item in captured}, {json.dumps([self.scope(context)], sort_keys=True)})
        self.assertEqual(organize_probe_repo.count_organize_probe_jobs()["completed"], 250)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.send.call_count, 1)  # 只有初始父消息
        self.assertEqual(self.edit.call_count, 1)  # 稳定 payload 聚合，不逐项反复编辑
        claimed = strm_repo.claim_strm_change_targets(owner="restart", limit=10)
        self.assertEqual(sum(len(row["changes"]) for row in claimed), 250)
        self.assertTrue(all(change["_probe_notification_context"] == context for row in claimed for change in row["changes"]))

    def test_success_missing_parent_stays_silent_and_error_falls_back_once(self):
        context = self.context()
        for _ in range(20):
            self.publish(context)
        self.assertEqual(self.rows(), [])
        for _ in range(3):
            self.publish(context, partial=True, error="secret-other-file-path")
        self.assertEqual(self.send.call_count, 1)
        row = self.rows()[0]
        self.assertEqual(row["importance"], "error")
        self.assertEqual(row["chat_id"], "chat-a")
        self.assertNotIn("secret-other-file-path", row["event_json"])
        self.assertTrue(row["thread_key"].startswith("probe-error:"))

    def test_legacy_success_silent_but_error_is_one_sanitized_admin_thread(self):
        for _ in range(250):
            self.publish({})
        self.send.assert_not_called()
        for _ in range(5):
            self.publish({}, partial=True, error="private-a/movie.mkv")
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.rows()[0]["chat_id"], "admin")
        self.assertNotIn("private-a", self.rows()[0]["event_json"])

    def test_different_parents_and_chats_do_not_share_threads(self):
        contexts = [self.context("p-a", "chat-a"), self.context("p-b", "chat-a"), self.context("p-a", "chat-b")]
        for context in contexts:
            self.parent(context)
        self.publish(contexts[0])
        revisions = {(row["thread_key"], row["chat_id"]): row["revision"] for row in self.rows()}
        self.assertEqual(revisions, {("organize:p-a", "chat-a"): 2, ("organize:p-b", "chat-a"): 1, ("organize:p-a", "chat-b"): 1})

    def test_silent_and_disabled_topic_do_not_update_even_existing_parent(self):
        for key in ("notify_enabled", "topic_enabled"):
            with self.subTest(key=key):
                context = self.context(task=key, **{key: False})
                self.parent(context)
                self.publish(context, partial=True, error="failure")
                self.publish(context)
        self.edit.assert_not_called()
        self.assertEqual(len(self.rows()), 2)

    def test_global_off_is_respected_and_essential_keeps_errors(self):
        context = self.context()
        with patch("app.modules.telegram_notification_policy.notifications_enabled", return_value=False):
            self.publish(context, partial=True, error="failed")
        self.send.assert_not_called()
        with patch("app.modules.telegram_notification_policy.notification_level", return_value="essential"):
            self.publish(context)
            self.publish(context, partial=True, error="failed")
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.rows()[0]["importance"], "error")

    def test_success_cannot_clear_error_or_pending_action(self):
        from app.notifier import NotificationAction
        context = self.context()
        self.parent(context)
        self.publish(context, partial=True, error="failed")
        errored = self.rows()[0]
        self.publish(context)
        self.assertEqual(self.rows()[0]["event_json"], errored["event_json"])
        self.assertEqual(self.rows()[0]["importance"], "error")
        actionable = self.context("pending", "chat-a")
        self.parent(actionable, state="queued", title="⏳ 待人工确认", actions=(NotificationAction("确认", "orgc:token:0"),))
        before = self.rows()[-1]
        self.publish(actionable)
        self.assertEqual(self.rows()[-1]["event_json"], before["event_json"])
        self.assertEqual(self.rows()[-1]["importance"], "action")

    def test_unknown_initial_send_is_never_retried_or_fallback_sent(self):
        from app.notifier import TelegramSendResult
        context = self.context()
        self.send.return_value = TelegramSendResult(ok=False, error="timeout", status_code=0)
        self.parent(context)
        self.assertEqual(self.rows()[0]["status"], "outcome_unknown")
        for _ in range(5):
            self.publish(context)
            self.publish(context, partial=True, error="failed")
        self.assertEqual(self.send.call_count, 1)
        self.edit.assert_not_called()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["status"], "outcome_unknown")

    def test_busy_merge_silent_probe_cannot_swallow_foreground_or_other_chat(self):
        from app.modules.organize_probe_notifications import tag_probe_changes
        from app.modules.scheduler import (
            STRMScheduler,
            _publish_linked_notification_threads,
        )
        scheduler = STRMScheduler()
        self.assertTrue(scheduler._run_lock.acquire(False))
        self.addCleanup(scheduler._run_lock.release)
        scheduler._pending_thread = Mock(is_alive=Mock(return_value=True))
        silent = self.context("silent", "chat-silent", notify_enabled=False)
        linked = self.context("linked", "chat-linked")
        self.parent(silent)
        self.parent(linked)
        change = {"source_id": "s", "kind": "video", "file_id": "silent", "name": "silent.mkv"}
        self.assertTrue(scheduler.trigger("organize", organize_changes=tag_probe_changes([change], silent, notify_enabled=False), notify_override=False, detail_notify_override=False)["ok"])
        self.assertTrue(scheduler.trigger("organize", organize_changes=[{**change, "file_id": "foreground"}], chat_id="chat-foreground", notify_override=True)["ok"])
        self.assertTrue(scheduler.trigger("organize", organize_changes=[{**change, "file_id": "linked"}], chat_id="chat-linked", notification_threads=linked["notification_threads"], notify_override=True)["ok"])
        options = scheduler._pending_organize_options
        assert options is not None
        self.assertEqual(len(options["notification_scopes"]), 3)
        _publish_linked_notification_threads(options, strm_status="完成", media_refresh="完成")
        with patch.object(scheduler, "_notify_success") as summary, patch.object(scheduler, "_notify_details"), patch.object(scheduler, "_notify_failure") as failure:
            scheduler._notify_scoped_results(options, {}, {}, 0.0, "organize", [], "/fake-strm")
            scheduler._notify_scoped_failure(options, "private-file", "organize")
        summary.assert_called_once()
        failure.assert_called_once()
        self.assertEqual(summary.call_args.kwargs["chat_ids"], ["chat-foreground"])
        self.assertEqual(failure.call_args.kwargs["chat_ids"], ["chat-foreground"])
        self.assertTrue(summary.call_args.kwargs["has_silent_notification_scope"])
        revisions = {row["chat_id"]: row["revision"] for row in self.rows()}
        self.assertEqual(revisions, {"chat-silent": 1, "chat-linked": 2})

    def pending_job(self, context, file_id="video", *, status="queued"):
        log_id = db.add_organize_log("guangya", "old", "new", file_id, "success", "1")
        job_id = organize_probe_repo.enqueue_organize_probe_completion(log_id, source_id="s", rel_dir="season", rules={}, notification_context=context)
        change = {"source_id": "s", "rel_dir": "season", "kind": "video", "file_id": file_id, "name": file_id + ".mkv"}
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET status=?,next_attempt_at='2000-01-01 00:00:00',pending_strm_changes_json=? WHERE id=?", (status, json.dumps([change]), job_id))
        return job_id, change

    def test_first_completion_keeps_batch_in_progress_until_both_queues_terminal(self):
        from app.modules.organize_probe_notifications import tag_probe_changes
        from app.repositories.organize_probe import (
            get_organize_probe_notification_progress,
        )
        context = self.context()
        self.parent(context)
        first, change = self.pending_job(context, "first", status="completed")
        second, _ = self.pending_job(context, "second")
        # 其他 chat 同名 task 不能影响本父批次终态。
        self.pending_job(self.context(chat="another-chat"), "other")
        self.publish(context)
        self.assertIn("后台规格补全进行中", self.rows()[0]["event_json"])
        strm_repo.enqueue_strm_change_targets(tag_probe_changes([change], context, notify_enabled=True))
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET status='completed',pending_strm_changes_json='[]' WHERE id IN (?,?)", (first, second))
        self.publish(context)
        self.assertIn("后台规格补全进行中", self.rows()[0]["event_json"])
        target = strm_repo.claim_strm_change_targets(owner="finisher")[0]
        self.assertEqual(strm_repo.complete_strm_change_target(target["id"], expected_owner="finisher", expected_lease_generation=target["lease_generation"]), "completed")
        self.publish(context)
        self.assertNotIn("后台规格补全进行中", self.rows()[0]["event_json"])
        self.assertEqual(self.rows()[0]["importance"], "result")
        progress = get_organize_probe_notification_progress(topic="organize", thread_key="organize:parent-a", chat_id="chat-a")
        self.assertEqual(progress, {"total": 2, "pending": 0, "failed": 0, "cancelled": 0, "strm_pending": 0, "strm_failed": 0})
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.edit.call_count, 2)

    def test_persistent_failed_sibling_cannot_be_overwritten_by_success(self):
        context = self.context()
        self.parent(context)
        self.pending_job(context, "success", status="completed")
        self.pending_job(context, "failure", status="failed")
        self.publish(context)
        self.assertIn("需复核", self.rows()[0]["event_json"])
        self.assertEqual(self.rows()[0]["importance"], "error")
        self.publish(context)
        self.assertEqual(self.edit.call_count, 1)

    def test_last_strm_finishes_before_probe_ack_still_closes_parent(self):
        from app.modules.scheduler import (
            STRMScheduler,
            _publish_linked_notification_threads,
        )
        context = self.context()
        self.parent(context)
        job_id, _change = self.pending_job(context)
        scheduler = STRMScheduler()

        def run_before_ack(trigger, options):
            scheduler._run_lock.release()
            target = strm_repo.claim_strm_change_targets(owner="early-finisher")[0]
            strm_repo.complete_strm_change_target(target["id"], expected_owner="early-finisher", expected_lease_generation=target["lease_generation"])
            _publish_linked_notification_threads(options, strm_status="完成", media_refresh="完成")
            self.assertIn("后台规格补全进行中", self.rows()[0]["event_json"])
            return {"ok": True}

        worker = OrganizeProbeWorker()
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch.object(scheduler, "_start_locked_worker", side_effect=run_before_ack), patch.object(worker, "_runtime_client", side_effect=AssertionError("不能重 probe")):
            self.assertTrue(worker._process_one())
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT status FROM organize_probe_queue WHERE id=?", (job_id,)).fetchone()[0], "completed")
        self.assertNotIn("后台规格补全进行中", self.rows()[0]["event_json"])
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.edit.call_count, 2)

    def test_scheduler_restart_recovers_parent_from_persisted_changes_and_completes(self):
        from app.modules.organize_probe_notifications import tag_probe_changes
        from app.modules.scheduler import STRMScheduler
        context = self.context()
        self.parent(context)
        _job, change = self.pending_job(context, status="completed")
        strm_repo.enqueue_strm_change_targets(tag_probe_changes([change], context, notify_enabled=True))
        restarted = STRMScheduler()
        stats = restarted._empty_stats()
        stats.update(total=1, generated=1)
        self.enterContext(patch.object(restarted, "validate_config", return_value=""))
        self.enterContext(patch.object(restarted, "_source_dirs", return_value=[{"id": "s", "name": "source", "rel_prefix": ""}]))
        self.enterContext(patch.object(restarted, "_video_exts", return_value={"mkv"}))
        self.enterContext(patch.object(restarted, "_metadata_exts", return_value=set()))
        self.enterContext(patch.object(restarted, "_refresh_media_servers", return_value={}))
        self.enterContext(patch("app.modules.scheduler.get", side_effect=lambda key, default="": {"GY_STRM_BASE_URL": "http://example.invalid", "STRM_ROOT": "/fake-strm"}.get(key, default)))
        self.enterContext(patch("app.modules.scheduler.get_int", return_value=0))
        with patch.object(restarted, "_run_incremental_sources", return_value=(stats, [{"id": "s", "name": "source", "stats": stats}], False, "")) as incremental, patch.object(restarted, "_run_full_sources", side_effect=AssertionError("禁止全量或远端访问")), patch.object(restarted, "_notify_details"), patch.object(restarted, "_notify_success") as summary:
            result = restarted.run_blocking("organize", sync_mode="fast")
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "fast")
        incremental.assert_called_once()
        # CLI 自身仍有唯一 foreground scope；probe 不会多调用汇总发布。
        summary.assert_called_once()
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.edit.call_count, 1)
        self.assertIn("organize:parent-a", self.rows()[0]["thread_key"])
        self.assertEqual(strm_repo.claim_strm_change_targets(owner="check"), [])

    def test_confirmation_context_uses_only_explicit_verified_refs_not_business_task(self):
        from app.modules.organize_probe_notifications import build_notification_context
        context = build_notification_context(confirmation_token="trusted-token", chat_id="chat-a", notification_threads=[{
            "topic": "confirmation", "thread_key": "confirmation:trusted-token", "token": "trusted-token", "chat_id": "chat-a", "topic_enabled": True,
        }])
        self.assertEqual(context["task_id"], "")
        self.assertEqual(len(context["notification_threads"]), 1)
        self.assertEqual(context["notification_threads"][0]["thread_key"], "confirmation:trusted-token")

    def test_one_linked_chat_does_not_suppress_unlinked_chat_in_same_scope(self):
        from app.modules.scheduler import STRMScheduler
        scheduler = STRMScheduler()
        options = {"chat_ids": ["chat-a", "chat-b"], "uses_default_notification_scope": False,
                   "notify_override": True, "notification_threads": self.context()["notification_threads"]}
        with patch.object(scheduler, "_notify_success") as summary, patch.object(scheduler, "_notify_failure") as failure, patch.object(scheduler, "_notify_details"):
            scheduler._notify_scoped_results(options, {}, {}, 0, "organize", [], "/fake-strm")
            scheduler._notify_scoped_failure(options, "private-path", "organize")
        summary.assert_called_once()
        failure.assert_called_once()
        self.assertEqual(summary.call_args.kwargs["chat_ids"], ["chat-b"])
        self.assertEqual(failure.call_args.kwargs["chat_ids"], ["chat-b"])
        self.assertTrue(failure.call_args.kwargs["has_silent_notification_scope"])

    def test_corrupt_context_shape_degrades_without_blocking_handoff(self):
        from app.repositories.organize_probe import normalize_notification_context
        for raw in ("not-json", {"version": 1, "notification_threads": 42}, {"version": 1, "download_request_ids": 42}):
            with self.subTest(raw=raw):
                normalized = normalize_notification_context(raw)
                self.assertTrue(normalized["untrusted"])
                self.assertFalse(normalized["notify_enabled"])
        scheduler = Mock()
        scheduler.trigger.return_value = {"ok": True}
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
            self.assertTrue(OrganizeProbeWorker()._handoff_pending({
                "pending_strm_changes_json": json.dumps([{"source_id": "s", "file_id": "f", "name": "new.mkv"}]),
                "notification_context_json": '{"version":1,"notification_threads":42}', "rules_json": "{}",
            }))
        forwarded = scheduler.trigger.call_args.kwargs["organize_changes"][0]["_probe_notification_context"]
        self.assertTrue(forwarded["untrusted"])
        self.assertFalse(forwarded["notify_enabled"])

    def test_duplicate_confirmation_ref_cannot_reenable_disabled_topic(self):
        from app.modules.organize_probe_notifications import build_notification_context
        context = build_notification_context(confirmation_token="token", chat_id="chat-a", notification_threads=[{
            "topic": "confirmation", "thread_key": "confirmation:token", "token": "token", "chat_id": "chat-a", "topic_enabled": False,
        }])
        self.assertEqual(len(context["notification_threads"]), 1)
        self.assertFalse(context["notification_threads"][0]["topic_enabled"])

    def test_real_incremental_keeps_probe_metadata_and_removes_old_path(self):
        import hashlib
        import tempfile
        from pathlib import Path

        from app.clients.guangya import GuangYaFile
        from app.modules.organize_probe_notifications import tag_probe_changes
        from app.modules.strm import generate_strm, sync_strm_incremental
        from tests.test_strm_p2_incremental import _IncrementalClient
        context = self.context()
        old = GuangYaFile("same-video", "Old.mkv", False, 100, "etag", "target")
        new = GuangYaFile("same-video", "New.mkv", False, 100, "etag", "target")
        change = {"source_id": "s", "kind": "video", "action": "upsert", "file_id": new.file_id,
                  "name": new.name, "parent_id": "target", "etag": "etag", "size": 100, "rel_dir": "season"}
        strm_repo.enqueue_strm_change_targets(tag_probe_changes([change], context, notify_enabled=True))
        target = strm_repo.claim_strm_change_targets(owner="real-incremental")[0]
        self.assertEqual(target["changes"][0]["_probe_notification_context"], context)
        with tempfile.TemporaryDirectory() as root:
            old_path = generate_strm(old, "season", "http://example.invalid", root)
            strm_repo.upsert_strm_index("guangya:s", old.file_id, old.etag, old.size, old.name, str(old_path), "sha256:" + hashlib.sha256(old_path.read_bytes()).hexdigest())
            client = _IncrementalClient({new.file_id: new})
            stats = sync_strm_incremental("s", target["changes"], "http://example.invalid", root, client=cast(GuangYaClient, client))
            self.assertFalse(stats["fallback_required"])
            self.assertFalse(old_path.exists())
            self.assertEqual(len(list(Path(root).rglob("New.strm"))), 1)
            self.assertEqual(client.list_calls, 0)

    def test_download_parent_routes_remain_frozen_after_request_route_changes(self):
        from app.modules.organize_probe_notifications import build_notification_context
        from app.notifier import NotificationEvent
        request_id, _created = download_requests_repo.create_download_request("probe-notify-download", "magnet", chat_id="chat-a")
        context = build_notification_context(download_request_ids=[request_id])
        self.assertEqual(context["notification_threads"][0]["chat_id"], "chat-a")
        self.center.publish_notification_thread(f"download:{request_id}", NotificationEvent("✅ 下载完成", fields=(("STRM", "排队"), ("媒体库", "未完成")), state="completed"), topic="download", chat_id="chat-a")
        download_requests_repo.update_download_request(request_id, chat_id="chat-b")
        self.publish(context)
        self.assertEqual(self.edit.call_args.kwargs["chat_id"], "chat-a")
        self.assertEqual({row["chat_id"] for row in self.rows()}, {"chat-a"})

    def test_exhausted_probe_publishes_error_without_remote_io(self):
        context = self.context()
        self.parent(context)
        job_id, _change = self.pending_job(context)
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET attempts=max_attempts-1,pending_strm_changes_json='[]' WHERE id=?", (job_id,))
        worker = OrganizeProbeWorker()
        with patch.object(worker, "_execute_job", side_effect=RuntimeError("private-movie-name")):
            self.assertTrue(worker._process_one())
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT status FROM organize_probe_queue WHERE id=?", (job_id,)).fetchone()[0], "failed")
        self.assertEqual(self.rows()[0]["importance"], "error")
        self.assertNotIn("private-movie-name", self.rows()[0]["event_json"])
        self.publish(context)
        self.assertEqual(self.rows()[0]["importance"], "error")

    def test_ack_updates_ready_parent_without_waiting_for_other_parent(self):
        from app.modules.organize_probe_notifications import (
            build_notification_context,
            publish_probe_acknowledged,
        )
        first = self.context("first")
        second = self.context("second")
        self.parent(first)
        self.parent(second)
        both = build_notification_context(chat_id="chat-a", notification_threads=[*first["notification_threads"], *second["notification_threads"]])
        self.pending_job(both, "finished", status="completed")
        self.pending_job(second, "other-still-queued")
        publish_probe_acknowledged({"notification_context_json": json.dumps(both)})
        revisions = {row["thread_key"]: row["revision"] for row in self.rows()}
        self.assertEqual(revisions, {"organize:first": 2, "organize:second": 1})

    def test_future_or_malformed_nonempty_context_never_falls_back_to_admin(self):
        for context in (
            {**self.context("future-private", "private-chat", notify_enabled=False), "version": 2},
            {"version": 1, "notification_threads": 123, "chat_id": "private-chat", "notify_enabled": False},
            {"version": 99, "chat_id": "private-chat", "notify_enabled": True},
        ):
            with self.subTest(context=context):
                self.publish(context, partial=True, error="private-error")
        self.send.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_last_cancelled_probe_updates_parent_after_terminal_commit(self):
        from app.modules.organize_probe_worker import _ProbeCompletionCancelled
        context = self.context()
        self.parent(context)
        job_id, _change = self.pending_job(context)
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET pending_strm_changes_json='[]' WHERE id=?", (job_id,))
        worker = OrganizeProbeWorker()
        with patch.object(worker, "_execute_job", side_effect=_ProbeCompletionCancelled("云端文件身份不一致")):
            self.assertTrue(worker._process_one())
        self.assertEqual(json.loads(self.rows()[0]["event_json"])["state"], "partial")
        self.assertEqual(self.rows()[0]["importance"], "error")
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT status FROM organize_probe_queue WHERE id=?", (job_id,)).fetchone()[0], "cancelled")
        self.publish(context)
        self.assertEqual(self.rows()[0]["importance"], "error")

    def test_last_noop_probe_closes_in_progress_parent(self):
        context = self.context()
        self.parent(context)
        job_id, _change = self.pending_job(context)
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET pending_strm_changes_json='[]' WHERE id=?", (job_id,))
        self.publish(context)
        self.assertIn("后台规格补全进行中", self.rows()[0]["event_json"])
        worker = OrganizeProbeWorker()
        with patch.object(worker, "_execute_job", return_value=False):
            self.assertTrue(worker._process_one())
        self.assertNotIn("后台规格补全进行中", self.rows()[0]["event_json"])
        self.assertEqual(json.loads(self.rows()[0]["event_json"])["state"], "completed")

    def test_notification_exception_never_reopens_committed_terminal_job(self):
        from app.modules.organize_probe_worker import _ProbeCompletionCancelled
        context = self.context()
        for expected, outcome, notifier in (
            ("completed", False, "publish_probe_acknowledged"),
            ("cancelled", _ProbeCompletionCancelled("changed"), "publish_probe_scope"),
            ("failed", RuntimeError("failed"), "publish_probe_scope"),
        ):
            with self.subTest(status=expected):
                job_id, _change = self.pending_job(context, expected)
                with db.get_conn() as conn:
                    conn.execute("UPDATE organize_probe_queue SET attempts=max_attempts-1,pending_strm_changes_json='[]' WHERE id=?", (job_id,))
                worker = OrganizeProbeWorker()
                execute_patch = patch.object(worker, "_execute_job", side_effect=outcome) if isinstance(outcome, Exception) else patch.object(worker, "_execute_job", return_value=outcome)
                with execute_patch, patch("app.modules.organize_probe_notifications." + notifier, side_effect=RuntimeError("outbox unavailable")):
                    self.assertTrue(worker._process_one())
                with db.get_conn() as conn:
                    row = conn.execute("SELECT status,pending_strm_changes_json,lease_owner FROM organize_probe_queue WHERE id=?", (job_id,)).fetchone()
                self.assertEqual(row["status"], expected)
                self.assertEqual(row["pending_strm_changes_json"], "[]")
                self.assertEqual(row["lease_owner"], "")
                self.assertFalse(worker._process_one())

    def test_probe_retry_does_not_publish_terminal_error_until_exhausted(self):
        from app.modules.organize_probe_worker import _ProbeCompletionUnavailable
        context = self.context()
        self.parent(context)
        job_id, _change = self.pending_job(context)
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_probe_queue SET pending_strm_changes_json='[]' WHERE id=?", (job_id,))
        worker = OrganizeProbeWorker()
        with patch.object(worker, "_execute_job", side_effect=_ProbeCompletionUnavailable("probe unavailable")):
            self.assertTrue(worker._process_one())
            self.edit.assert_not_called()
            with db.get_conn() as conn:
                conn.execute("UPDATE organize_probe_queue SET next_attempt_at='2000-01-01 00:00:00' WHERE id=?", (job_id,))
            self.assertTrue(worker._process_one())
        self.assertEqual(self.rows()[0]["importance"], "error")
        self.assertEqual(self.edit.call_count, 1)
