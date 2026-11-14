"""TG 批次使用真实公共服务、隔离 SQLite 和通知 outbox；不连接 TG/云盘。"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta
from sqlite3 import Row
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.bot import handlers
from app.modules import organize_confirmation_batches as service
from app.modules import organize_confirmations as confirmations
from app.modules import telegram_confirmation_batch as batch
from app.modules import telegram_notification_center as notifications
from app.modules.organize import Organizer, OrganizeRules
from app.modules.telegram_notification_policy import NotificationTopic
from app.notifier import NotificationEvent, TelegramSendResult, render_event
from tests.support import FakeTelegramBot, IsolatedDatabaseTestCase, fake_telebot_types


class TelegramConfirmationBatchTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM organize_confirmation_delivery_outbox")
            conn.execute("DELETE FROM organize_confirmations")
            conn.execute("DELETE FROM telegram_notification_outbox")
        was_stopped = notifications._dispatch_stop.is_set()
        notifications._dispatch_stop.clear()
        self.addCleanup(notifications._dispatch_stop.set if was_stopped else notifications._dispatch_stop.clear)
        self.agent_enabled = self.enterContext(patch.object(batch, "recognition_review_enabled", return_value=False))
        self.review_enabled = self.enterContext(patch.object(confirmations, "_recognition_review_is_enabled", return_value=False))
        self.enterContext(patch.object(confirmations, "wake_recognition_review_dispatcher", return_value=False))
        self.enterContext(patch.object(confirmations, "wake_confirmation_dispatcher", return_value=False))
        self.dispatch = self.enterContext(patch.object(confirmations, "_dispatch_next_queued_confirmation", return_value={"idle": True}))
        self.enterContext(patch.object(notifications, "wake_telegram_notification_dispatcher"))
        self.enterContext(patch.object(notifications, "allows_notification", return_value=True))
        self.sent = self.enterContext(patch.object(notifications, "send_event_result", return_value=TelegramSendResult(True, message_id=77)))
        self.edited = self.enterContext(patch.object(notifications, "edit_event_result", return_value=TelegramSendResult(True, message_id=77)))
        self.rules = OrganizeRules.from_config().for_source("source")
        self.batch_id = "a" * 32

    def _group(self, index: int) -> dict:
        return {
            "_confirmation_batch": self.batch_id, "identity": f"媒体 {index}",
            "source_name": "隔离下载", "source_dir_id": "source",
            "directory": f"目录 {index}", "source_parent_id": f"parent-{index}",
            "reason": "需要人工核对候选", "files": [{
                "file_id": f"video-{index}", "parent_id": f"parent-{index}",
                "name": f"Show.{index}.S01E01.mkv", "size": 100,
                "season": 1, "episode": 1,
            }],
            "candidates": [{
                "tmdb_id": str(100 + index * 2 + candidate), "provider": "tmdb",
                "title": f"候选 {index}-{candidate}", "year": "2026", "media_type": "tv",
            } for candidate in range(2)],
        }

    def _create(self, count=2, *, start=0, chat_id="100") -> list[str]:
        return [confirmations.create_confirmation_actions(
            self._group(index), self.rules, chat_id=chat_id,
        )[0].callback_data.split(":")[1] for index in range(start, start + count)]

    def _confirmation(self, token: str) -> Row:
        row = db.get_organize_confirmation(token)
        assert row is not None, f"确认记录不存在：{token}"
        return row

    def _review(self, token: str, *, index=0, status="approved") -> None:
        db.update_organize_confirmation(
            token, review_status=status,
            review_result_json=json.dumps({
                "candidate_index": index, "summary": "核对季集后建议选择", "confidence": 0.92,
            }, ensure_ascii=False),
        )

    def _snapshot(self, *, chat_id="100"):
        snapshot = notifications.get_notification_thread_snapshot(
            f"confirmation-batch:{self.batch_id}", topic=NotificationTopic.CONFIRMATION,
            chat_id=chat_id,
        )
        assert snapshot is not None
        return snapshot

    def _action(self, label: str) -> str:
        for action in self._snapshot().event.actions:
            assert isinstance(action.label, str)
            if action.label.startswith(label):
                return action.callback_data
        raise AssertionError(f"未找到按钮：{label}")

    def _callback(self, data: str, *, chat_id="100", message_id=77) -> str:
        return batch.handle_batch_callback(data, chat_id=chat_id, message_id=message_id)

    def test_hundred_groups_persist_before_one_summary_without_agent_or_default_choice(self):
        groups = [self._group(index) for index in range(100)]
        with patch.object(service, "list_confirmations", wraps=service.list_confirmations) as listing, patch.object(
            confirmations, "publish_confirmation_event",
        ) as single_publish, patch.object(service, "request_reviews") as reviews:
            self.assertTrue(Organizer._deliver_task_confirmation_groups(
                groups, self.rules, chat_id="100", organize_task_id="task-100",
            ))
        single_publish.assert_not_called()
        reviews.assert_not_called()
        listing.assert_called_once()
        self.assertEqual(listing.call_args.kwargs["page_size"], 1000)
        self.assertEqual(listing.call_args.kwargs["status"], "all")
        with db.get_conn() as conn:
            rows = conn.execute("SELECT * FROM organize_confirmations").fetchall()
            threads = conn.execute("SELECT * FROM telegram_notification_outbox").fetchall()
        self.assertEqual(len(rows), 100)
        self.assertEqual(len(threads), 1)
        payloads = [json.loads(row["payload_json"]) for row in rows]
        ids = {payload["_confirmation_batch"] for payload in payloads}
        self.assertEqual(len(ids), 1)
        self.assertRegex(next(iter(ids)), r"^[0-9a-f]{16,32}$")
        self.assertTrue(all(payload["review_only"] for payload in payloads))
        self.assertTrue(all(row["status"] == "pending" and row["selected_index"] is None for row in rows))
        self.assertEqual(threads[0]["thread_key"], f"confirmation-batch:{next(iter(ids))}")
        self.assertEqual(self.sent.call_count, 1)
        self.edited.assert_not_called()
        self.assertTrue(all(len(action.callback_data.encode()) <= 64 for action in self.sent.call_args.args[0].actions))

    def test_agent_reviews_activate_only_after_all_groups_and_summary_are_persisted(self):
        self.review_enabled.return_value = True
        self.agent_enabled.return_value = True
        requested = service.request_reviews

        def request(tokens, **kwargs):
            with db.get_conn() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM organize_confirmations").fetchone()[0], 3)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_notification_outbox").fetchone()[0], 1)
            self.assertTrue(all(self._confirmation(token)["review_status"] == "waiting" for token in tokens))
            return requested(tokens, **kwargs)

        with patch.object(service, "request_reviews", side_effect=request) as reviews:
            self.assertTrue(Organizer._deliver_task_confirmation_groups(
                [self._group(index) for index in range(3)], self.rules, chat_id="100",
            ))
        reviews.assert_called_once()
        self.assertTrue(all(self._confirmation(token)["review_status"] == "pending" for token in reviews.call_args.args[0]))
        self.dispatch.assert_not_called()

    def test_rejected_summary_keeps_all_frozen_groups_without_activating_reviews(self):
        self.review_enabled.return_value = True
        with patch.object(batch, "publish_batch_summary", return_value=False), patch.object(service, "request_reviews") as reviews:
            self.assertFalse(Organizer._deliver_task_confirmation_groups(
                [self._group(index) for index in range(2)], self.rules, chat_id="100",
            ))
        reviews.assert_not_called()
        with db.get_conn() as conn:
            rows = conn.execute("SELECT status,review_status FROM organize_confirmations").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "pending" and row["review_status"] == "waiting" for row in rows))

    def test_pagination_details_and_legacy_selection_keep_same_message(self):
        self._create(7)
        self.assertTrue(batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True))
        self._callback(self._action("下一页"))
        self.assertIn(("分页", "2/2"), self._snapshot().event.fields)
        self._callback(self._action("查看"))
        detail = self._snapshot()
        choice = next(action.callback_data for action in detail.event.actions if action.callback_data.startswith("orgc:") and action.callback_data.endswith(":1"))
        token = choice.split(":")[1]
        bot = Mock()
        handlers._handle_organize_confirmation_callback(bot, SimpleNamespace(
            id="callback", data=choice, from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=77),
        ))
        row = self._confirmation(token)
        self.assertEqual(row["selected_index"], 1)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(json.loads(row["payload_json"])["_telegram_message_id"], 77)
        bot.edit_message_text.assert_not_called()
        self.assertEqual(self._snapshot().message_id, 77)
        self.assertEqual(self.sent.call_count, 1)
        self.assertTrue(all(call.kwargs["message_id"] == 77 for call in self.edited.call_args_list))
        delivery = db.get_organize_confirmation_delivery(token)
        assert delivery is not None
        self.assertEqual(delivery["status"], "sent")
        self._callback(self._action("返回批次"))
        self.assertIn(("分页", "2/2"), self._snapshot().event.fields)

    def test_frozen_digest_rejects_new_suggestion_then_only_submits_displayed_choices(self):
        first, second, untouched = self._create(3)
        self._review(first)
        batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True)
        old_approval = self._action("确认全部")
        self._review(second, index=1)
        with patch.object(service, "submit_choices", wraps=service.submit_choices) as submit:
            with self.assertRaisesRegex(ValueError, "建议已变化"):
                self._callback(old_approval)
            submit.assert_not_called()
            approval = self._action("确认全部")
            self.assertNotEqual(approval, old_approval)
            self.assertIn("已接纳 2 组", self._callback(approval))
            self.assertEqual(submit.call_count, 1)
            self.assertEqual({entry["confirmation_id"]: entry["candidate_index"] for entry in submit.call_args.args[0]}, {first: 0, second: 1})
            with self.assertRaisesRegex(ValueError, "建议已变化"):
                self._callback(approval)
            self.assertEqual(submit.call_count, 1)
        self.assertEqual(self._confirmation(untouched)["status"], "pending")
        self.assertIsNone(self._confirmation(untouched)["selected_index"])
        self.assertEqual(self._confirmation(first)["status"], "queued")
        self.assertEqual(self._confirmation(second)["status"], "queued")
        self.dispatch.assert_not_called()

    def test_review_updates_coalesce_and_final_abstention_publishes_pending_manual_item(self):
        first, last = self._create()
        self._review(first, status="running")
        self._review(last, status="pending")
        batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True)
        initial = self._snapshot()
        self._review(first)
        self.assertTrue(batch.publish_batch_summary(self.batch_id, chat_id="100"))
        self.assertEqual(self._snapshot().revision, initial.revision)
        self._review(last, index=None, status="abstained")
        self.assertTrue(batch.publish_batch_summary(self.batch_id, chat_id="100"))
        self.assertEqual(self._snapshot().revision, initial.revision + 1)
        self.assertTrue(self._action("确认全部"))
        self.assertEqual(self._confirmation(last)["status"], "pending")
        self.assertTrue(batch.publish_batch_summary(self.batch_id, chat_id="100"))
        self.assertEqual(self._snapshot().revision, initial.revision + 1)
        self.edited.assert_not_called()
        notifications.drain_telegram_notifications(limit=1)
        self.assertEqual(self.edited.call_count, 1)
        self.assertEqual(self.sent.call_count, 1)

    def test_readonly_review_button_preserves_approved_choice_and_never_executes(self):
        first, second = self._create()
        self._review(first, index=1)
        self.review_enabled.return_value = True
        self.agent_enabled.return_value = True
        batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True)
        notice = self._callback(self._action("请求 Agent"))
        self.assertIn("只读复核", notice)
        self.assertEqual(self._confirmation(second)["review_status"], "pending")
        first_row = self._confirmation(first)
        self.assertEqual(first_row["review_status"], "approved")
        self.assertEqual(json.loads(first_row["review_result_json"])["candidate_index"], 1)
        self.assertTrue(all(self._confirmation(token)["status"] == "pending" for token in (first, second)))
        self.dispatch.assert_not_called()

    def test_group_without_candidates_does_not_keep_finished_reviews_waiting(self):
        first, second = self._create()
        group = {**self._group(2), "candidates": [], "allow_skip_terminal": True}
        self.review_enabled.return_value = True
        actions = confirmations.create_confirmation_actions(group, self.rules, chat_id="100")
        token = actions[0].callback_data.split(":")[1]
        self.assertEqual(self._confirmation(token)["review_status"], "")
        self._review(first, status="running")
        self._review(second, status="pending")
        batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True)
        before = self._snapshot().revision
        self._review(first)
        self._review(second, index=None, status="abstained")
        self.assertTrue(batch.publish_batch_summary(self.batch_id, chat_id="100"))
        self.assertGreater(self._snapshot().revision, before)
        self.assertTrue(self._action("确认全部"))

    def test_partial_submission_reports_each_outcome_and_keeps_rejected_group_pending(self):
        first, second = self._create()
        for token in (first, second):
            self._review(token)
        batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True)
        original = confirmations.start_confirmation

        def start(token, index, **kwargs):
            if token == first:
                raise ValueError("此组已被其他请求取得所有权")
            return original(token, index, **kwargs)

        with patch.object(confirmations, "start_confirmation", side_effect=start):
            notice = self._callback(self._action("确认全部"))
        self.assertIn("已接纳 1 组，未提交 1 组", notice)
        self.assertIn("此组已被其他请求取得所有权", notice)
        self.assertEqual(self._confirmation(first)["status"], "pending")
        self.assertEqual(self._confirmation(second)["status"], "queued")
        self.dispatch.assert_not_called()

    def test_episode_mapping_requires_web_while_ordinary_suggestions_can_be_approved(self):
        ordinary, mapped = self._create()
        for token in (ordinary, mapped):
            self._review(token)
        original = service.preview_choices
        mappings = [{"file_index": 0, "source_season": 1, "source_episode": 1, "target_season": 2, "target_episode": 3}]

        def preview(entries, **kwargs):
            result = original(entries, **kwargs)
            for item in result["items"]:
                if item["confirmation_id"] == mapped:
                    item["episode_mappings"] = mappings
                    result["digest"] = hashlib.sha256((result["digest"] + json.dumps(mappings)).encode()).hexdigest()
            return result

        with patch.object(service, "preview_choices", side_effect=preview):
            batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True)
            summary = self._snapshot().event
            self.assertIn("季集映射", render_event(summary))
            self.assertIn("S01E01 → S02E03", render_event(summary))
            assert isinstance(summary.footer, str)
            self.assertIn("Web", summary.footer)
            self.assertIn("已接纳 1 组", self._callback(self._action("确认普通建议")))
            self.assertEqual(self._confirmation(ordinary)["status"], "queued")
            self.assertEqual(self._confirmation(mapped)["status"], "pending")
            self.assertIsNone(self._confirmation(mapped)["selected_index"])
            self.assertFalse(any(":a:" in action.callback_data for action in self._snapshot().event.actions))
            self._callback(f"orgb:{mapped}:d:1")
            detail = self._snapshot().event
            self.assertIn("S01E01 → S02E03", render_event(detail))
            self.assertFalse(any(action.callback_data.startswith("orgc:") and not action.callback_data.endswith(":skip") for action in detail.actions))
        self.dispatch.assert_not_called()

    def test_wrong_chat_message_anchor_and_malformed_callbacks_cannot_mutate_batch(self):
        tokens = self._create()
        batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True)
        callback = self._action("查看")
        revision = self._snapshot().revision
        for data, chat, message in (
            (callback, "200", 77), (callback, "100", 88),
            ("orgb:missing:p:1", "100", 77), ("orgb:token:p:0", "100", 77),
            ("orgb:" + "x" * 80 + ":p:1", "100", 77),
        ):
            with self.subTest(data=data, chat=chat, message=message), self.assertRaises(ValueError):
                self._callback(data, chat_id=chat, message_id=message)
        self.assertEqual(self._snapshot().revision, revision)
        self.assertTrue(all(self._confirmation(token)["status"] == "pending" for token in tokens))
        self.assertEqual(self.sent.call_count, 1)
        self.edited.assert_not_called()

    def test_truncated_batch_explicitly_requires_web_and_refuses_batch_actions(self):
        tokens = self._create()
        self._review(tokens[0])
        self.agent_enabled.return_value = True
        original = service.list_confirmations

        def truncated(**kwargs):
            return {**original(**kwargs), "selection_truncated": True}

        with patch.object(service, "list_confirmations", side_effect=truncated):
            batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True)
            event = self._snapshot().event
            assert isinstance(event.footer, str)
            self.assertIn("超过 1000", event.footer)
            self.assertIn("Web 分批", event.footer)
            self.assertFalse(any(":a:" in action.callback_data or ":r:" in action.callback_data for action in event.actions))
            for action, value in (("r", "1"), ("a", "a" * 16)):
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, "超过 1000"):
                    self._callback(f"orgb:{tokens[0]}:{action}:{value}")

    def test_over_thousand_groups_persist_without_silently_reviewing_only_a_subset(self):
        self.review_enabled.return_value = True
        self.agent_enabled.return_value = True
        with patch.object(service, "request_reviews") as reviews:
            self.assertTrue(Organizer._deliver_task_confirmation_groups(
                [self._group(index) for index in range(1001)], self.rules, chat_id="100",
            ))
        reviews.assert_not_called()
        event = self.sent.call_args.args[0]
        self.assertIn("超过 1000", event.footer)
        self.assertIn("Web 分批", event.footer)
        self.assertFalse(any(":a:" in action.callback_data or ":r:" in action.callback_data for action in event.actions))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM organize_confirmations").fetchone()[0], 1001)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_notification_outbox").fetchone()[0], 1)

    def test_downstream_partial_waiting_complete_and_detail_survive_queue_completion(self):
        tokens = self._create(3)
        downstream = (
            {"strm": "已完成", "media_refresh": "刷新失败", "partial": True, "error": "媒体库暂不可用"},
            {"strm": "已排队", "media_refresh": "等待 STRM 完成"},
            {"strm": "已完成", "media_refresh": "已完成"},
        )
        for token, result in zip(tokens, downstream):
            db.update_organize_confirmation(token, status="completed", result_json=json.dumps({"_downstream": result}, ensure_ascii=False))
        batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True)
        summary = self._snapshot().event
        self.assertIn(("后续处理", "部分完成 1 · 等待回执 1 · 已完成 1"), summary.fields)
        progress = dict(summary.fields)["进度"]
        assert isinstance(progress, str)
        self.assertIn("队列完成 3", progress)
        self.assertNotIn("入库已完成", render_event(summary))
        self._callback(f"orgb:{tokens[0]}:d:1")
        detail = self._snapshot().event
        self.assertIn(("STRM 状态", "已完成"), detail.fields)
        self.assertIn(("媒体库刷新", "刷新失败"), detail.fields)
        self.assertIn("媒体库暂不可用", render_event(detail))
        self.assertFalse(any(action.callback_data.startswith("orgc:") for action in detail.actions))
        self.assertTrue(batch.publish_batch_summary(self.batch_id, chat_id="100"))
        self.assertEqual(self._snapshot().event.lifecycle["confirmation_batch"]["detail_token"], tokens[0])

    def test_outbox_retries_failed_send_without_new_thread_and_rejects_stale_revision(self):
        tokens = self._create()
        self.sent.return_value = TelegramSendResult(False, status_code=503, error="unavailable")
        self.assertTrue(batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True))
        initial = self._snapshot()
        self.assertEqual(initial.status, "retry_wait")
        self.assertTrue(batch.publish_batch_summary(self.batch_id, chat_id="100"))
        self.assertEqual(self._snapshot().revision, initial.revision)
        self._review(tokens[0])
        self.assertFalse(batch.publish_batch_summary(self.batch_id, chat_id="100", expected_revision=initial.revision + 1))
        self.assertEqual(self._snapshot().revision, initial.revision)
        self.sent.return_value = TelegramSendResult(True, message_id=77)
        with db.get_conn() as conn:
            conn.execute("UPDATE telegram_notification_outbox SET next_attempt_at=?", (db.now(),))
        notifications.drain_telegram_notifications(limit=1)
        self.assertEqual(self._snapshot().message_id, 77)
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_notification_outbox").fetchone()[0], 1)

    def test_confirmation_leases_update_one_batch_thread_but_cannot_cross_batch_or_chat(self):
        tokens = self._create()
        batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True)
        self._create(1, start=10, chat_id="200")
        original_batch_id = self.batch_id
        self.batch_id = "b" * 32
        other_batch_id = self.batch_id
        self._create(1, start=20)
        self.batch_id = original_batch_id
        for token in tokens:
            confirmations.start_confirmation(token, 0, chat_id="100", dispatch=False)
        revisions = []
        for token in tokens:
            delivery = db.claim_due_organize_confirmation_delivery(
                current_time=db.now(), stale_before=(datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"), token=token,
            )
            assert delivery is not None
            lease = (delivery["id"], delivery["lease_generation"])
            self.assertTrue(batch.publish_batch_summary(
                self.batch_id, chat_id="100", expected_confirmation_delivery=lease,
            ))
            revision = self._snapshot().revision
            revisions.append(revision)
            self.assertFalse(batch.publish_batch_summary(
                other_batch_id, chat_id="100", expected_confirmation_delivery=lease,
            ))
            self.assertFalse(batch.publish_batch_summary(
                self.batch_id, chat_id="200", expected_confirmation_delivery=lease,
            ))
            self.assertFalse(batch.publish_batch_summary(
                self.batch_id, chat_id="100", expected_confirmation_delivery=(lease[0], lease[1] + 1),
            ))
            self.assertFalse(batch.publish_batch_summary(
                self.batch_id, chat_id="100", expected_confirmation_delivery=lease, expected_revision=revision + 1,
            ))
            self.assertEqual(self._snapshot().revision, revision)
            self.assertTrue(db.complete_organize_confirmation_delivery(
                lease[0], sent_at=db.now(), expected_lease_generation=lease[1],
            ))
            self.assertFalse(batch.publish_batch_summary(
                self.batch_id, chat_id="100", expected_confirmation_delivery=lease,
            ))
        self.assertEqual(self._snapshot().message_id, 77)
        self.assertEqual(self.sent.call_count, 1)
        self.assertEqual(revisions[0], revisions[1])
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_notification_outbox").fetchone()[0], 1)

    def test_leased_batch_thread_requires_actual_confirmation_payload_and_both_chats(self):
        first, _second = self._create()
        batch.publish_batch_summary(self.batch_id, chat_id="100", deliver_now=True)
        confirmations.start_confirmation(first, 0, chat_id="100", dispatch=False)
        delivery = db.claim_due_organize_confirmation_delivery(
            current_time=db.now(), stale_before=(datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"), token=first,
        )
        assert delivery is not None
        lease = (delivery["id"], delivery["lease_generation"])
        row = self._confirmation(first)
        initial = self._snapshot()
        for payload, confirmation_chat, delivery_chat in (
            ("{}", "100", "100"), ("[]", "100", "100"), ("invalid-json", "100", "100"),
            (json.dumps({"_confirmation_batch": "x" * 32}), "100", "100"),
            (row["payload_json"], "200", "100"), (row["payload_json"], "100", "200"),
        ):
            with self.subTest(payload=payload, confirmation_chat=confirmation_chat, delivery_chat=delivery_chat):
                with db.get_conn() as conn:
                    conn.execute("UPDATE organize_confirmations SET payload_json=?,chat_id=? WHERE token=?", (payload, confirmation_chat, first))
                    conn.execute("UPDATE organize_confirmation_delivery_outbox SET chat_id=? WHERE id=?", (delivery_chat, delivery["id"]))
                result = notifications.publish_notification_thread(
                    f"confirmation-batch:{self.batch_id}", NotificationEvent("不能越权改写"),
                    topic=NotificationTopic.CONFIRMATION, chat_id="100", deliver_now=False,
                    expected_confirmation_delivery=lease,
                )
                self.assertFalse(result)
                self.assertEqual(self._snapshot().revision, initial.revision)
                self.assertEqual(self._snapshot().event, initial.event)


class TelegramBatchRegistrationTests(IsolatedDatabaseTestCase):
    def test_registered_batch_callback_uses_existing_group_write_guard(self):
        class CallbackBot(FakeTelegramBot):
            def answer_callback_query(self, *args, **kwargs):
                pass

        bot = CallbackBot()
        values = {"TG_CHAT_ID": "-100", "TG_AGENT_ALLOWED_USER_IDS": "9"}
        with patch("app.bot.agent_adapter.AGENT_EXECUTOR.start", return_value=True), patch.object(
            handlers, "get", side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.agent_adapter.config.get", side_effect=lambda key, default="": values.get(key, default)), patch.object(
            handlers, "_handle_organize_batch_callback",
        ) as callback:
            handlers._register_commands(bot, fake_telebot_types())
            registered = next(handler for filters, handler in bot.callback_handlers
                              if filters["func"](SimpleNamespace(data="orgb:token:p:1")))
            call = SimpleNamespace(id="callback", data="orgb:token:p:1", from_user=SimpleNamespace(id=10),
                                   message=SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=77))
            registered(call)
            callback.assert_not_called()
            call.from_user.id = 9
            registered(call)
            callback.assert_called_once_with(bot, call)

    def test_batch_handler_only_acknowledges_and_reports_errors_without_direct_send_or_edit(self):
        bot = Mock()
        call = SimpleNamespace(id="callback", data="orgb:token:p:1", from_user=SimpleNamespace(id=9),
                               message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=77))
        with patch.object(batch, "handle_batch_callback", return_value="已刷新批次摘要") as callback:
            handlers._handle_organize_batch_callback(bot, call)
        callback.assert_called_once_with(call.data, chat_id="100", message_id=77)
        bot.answer_callback_query.assert_called_once_with("callback", "已刷新批次摘要")
        bot.send_message.assert_not_called()
        bot.edit_message_text.assert_not_called()
