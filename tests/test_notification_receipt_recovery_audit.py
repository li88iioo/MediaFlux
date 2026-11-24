"""回调已提供真实消息身份时，未知首次投递应安全恢复为原位编辑。"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from app import database as db
from app.modules import telegram_notification_center as center
from app.notifier import NotificationAction, NotificationEvent, TelegramSendResult
from app.repositories import telegram_notifications as repository
from tests.support import isolated_test_database


class NotificationReceiptRecoveryAuditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(patch.object(center, "_dispatch_stop", threading.Event()))
        self.enterContext(
            patch.object(center, "allows_notification", return_value=True)
        )
        self.enterContext(patch.object(center, "wake_telegram_notification_dispatcher"))
        self.sender = self.enterContext(
            patch.object(
                center,
                "send_event_result",
                return_value=TelegramSendResult(
                    ok=False, status_code=408, error="ReadTimeout"
                ),
            )
        )
        self.editor = self.enterContext(
            patch.object(
                center, "edit_event_result", return_value=TelegramSendResult(ok=True)
            )
        )
        self.initial = NotificationEvent(
            "待确认", actions=(NotificationAction("确认", "orgc:audit:0"),)
        )
        self.terminal = NotificationEvent("已完成", state="completed")

    def publish(self, event, *, message_id=0, deliver_now=True):
        return center.publish_notification_thread(
            "confirmation:receipt-audit",
            event,
            topic="confirmation",
            chat_id="100",
            preferred_message_id=message_id,
            deliver_now=deliver_now,
        )

    def test_changed_terminal_with_callback_message_id_recovers_unknown_first_send(
        self,
    ):
        first = self.publish(self.initial)
        self.assertEqual(first.status, "outcome_unknown")
        result = self.publish(self.terminal, message_id=77)
        self.assertTrue(result.delivered, result)
        self.sender.assert_called_once()
        self.editor.assert_called_once()
        self.assertEqual(self.editor.call_args.kwargs["message_id"], 77)
        self.assertEqual(self.editor.call_args.args[0].actions, ())
        row = repository.get_notification(first.event_key)
        assert row is not None
        self.assertEqual(
            (row["revision"], row["delivered_revision"], row["message_id"]), (2, 2, 77)
        )
        self.assertEqual(row["last_error"], "")

    def test_same_payload_with_message_receipt_recovers_without_revision_inflation(
        self,
    ):
        first = self.publish(self.initial)
        result = self.publish(self.initial, message_id=77)
        self.assertTrue(result.delivered)
        get_notification_result = repository.get_notification(first.event_key)
        assert get_notification_result is not None
        self.assertEqual(get_notification_result["revision"], 1)
        self.sender.assert_called_once()
        self.editor.assert_called_once()

    def test_no_receipt_never_replays_unknown_send_even_after_reopen(self):
        first = self.publish(self.initial)
        db.init_db()
        repository.recover_notifications()
        result = self.publish(self.terminal)
        self.assertEqual(result.status, "outcome_unknown")
        self.assertFalse(result.queued)
        self.sender.assert_called_once()
        self.editor.assert_not_called()
        get_notification_result_2 = repository.get_notification(first.event_key)
        assert get_notification_result_2 is not None
        self.assertFalse(get_notification_result_2["message_id"])

    def test_later_preferred_id_cannot_replace_the_already_bound_thread(self):
        first = self.publish(self.initial)
        self.publish(self.terminal, message_id=77)
        result = self.publish(NotificationEvent("后处理完成"), message_id=99)
        self.assertTrue(result.delivered)
        get_notification_result_3 = repository.get_notification(first.event_key)
        assert get_notification_result_3 is not None
        self.assertEqual(get_notification_result_3["message_id"], 77)
        self.assertEqual(self.editor.call_args.kwargs["message_id"], 77)
        self.assertEqual(self.editor.call_count, 2)
        self.sender.assert_called_once()

    def test_receipt_during_inflight_keeps_lease_then_delivers_latest_revision_only(
        self,
    ):
        first = self.publish(self.initial, deliver_now=False)
        claimed = repository.claim_due_notifications(event_key=first.event_key)[0]
        updated = self.publish(self.terminal, message_id=77, deliver_now=False)
        self.assertEqual(updated.status, "sending")
        self.assertEqual(
            repository.retry_notification(
                claimed["id"],
                lease_generation=claimed["lease_generation"],
                claimed_revision=claimed["revision"],
                error="old send outcome unknown",
                outcome_unknown=True,
            ),
            "pending",
        )
        self.assertTrue(center.drain_telegram_notifications(event_key=first.event_key))
        row = repository.get_notification(first.event_key)
        assert row is not None
        self.assertEqual(
            (row["status"], row["revision"], row["delivered_revision"]), ("sent", 2, 2)
        )
        self.sender.assert_not_called()
        self.editor.assert_called_once()

    def test_terminal_edit_timeout_retries_same_message_without_republication(self):
        self.sender.return_value = TelegramSendResult(ok=True, message_id=77)
        self.assertTrue(self.publish(self.initial).delivered)
        self.editor.side_effect = [
            TelegramSendResult(ok=False, status_code=408, error="ReadTimeout"),
            TelegramSendResult(ok=True, message_id=77),
        ]
        result = self.publish(self.terminal)
        row = repository.get_notification(result.event_key)
        assert row is not None
        self.assertEqual((row["status"], row["attempts"]), ("retry_wait", 1))
        self.assertEqual((row["message_id"], row["delivered_revision"]), (77, 1))
        self.assertFalse(center.drain_telegram_notifications(event_key=result.event_key))
        self.assertEqual(self.editor.call_count, 1)
        with db.get_conn() as conn:
            assert row is not None
            conn.execute(
                "UPDATE telegram_notification_outbox SET next_attempt_at=? WHERE id=?",
                (db.now(), row["id"]),
            )
        self.assertTrue(center.drain_telegram_notifications(event_key=result.event_key))
        row = repository.get_notification(result.event_key)
        assert row is not None
        self.assertEqual((row["status"], row["revision"], row["delivered_revision"]), ("sent", 2, 2))
        self.assertEqual(self.editor.call_args.kwargs["message_id"], 77)
        self.assertEqual(self.editor.call_args.args[0].actions, ())
        self.sender.assert_called_once()

    def test_unknown_edit_retries_are_bounded_and_restart_does_not_reset_budget(self):
        self.editor.return_value = TelegramSendResult(
            ok=False, status_code=408, error="ReadTimeout"
        )
        result = self.publish(self.terminal, message_id=77)
        for attempt in range(1, repository._MAX_ATTEMPTS + 1):
            row = repository.get_notification(result.event_key)
            assert row is not None
            self.assertEqual(row["attempts"], attempt)
            if attempt == repository._MAX_ATTEMPTS:
                assert row is not None
                self.assertEqual(row["status"], "failed")
                break
            assert row is not None
            self.assertEqual(row["status"], "retry_wait")
            db.init_db()
            repository.recover_notifications()
            get_notification_result_4 = repository.get_notification(result.event_key)
            assert get_notification_result_4 is not None
            self.assertEqual(get_notification_result_4["attempts"], attempt)
            with db.get_conn() as conn:
                assert row is not None
                conn.execute(
                    "UPDATE telegram_notification_outbox SET next_attempt_at=? WHERE id=?",
                    (db.now(), row["id"]),
                )
            center.drain_telegram_notifications(event_key=result.event_key)
        db.init_db()
        repository.recover_notifications()
        self.assertFalse(center.drain_telegram_notifications(event_key=result.event_key))
        self.assertEqual(self.editor.call_count, repository._MAX_ATTEMPTS)
        self.sender.assert_not_called()

    def test_legacy_known_unknown_edits_recover_through_both_startup_paths(self):
        for recovery in (repository.recover_notifications, db.init_db):
            with self.subTest(recovery=recovery.__name__), isolated_test_database():
                known = self.publish(self.terminal, message_id=77, deliver_now=False)
                unknown = center.publish_notification_thread(
                    "unknown-without-receipt", self.terminal,
                    topic="confirmation", chat_id="100", deliver_now=False,
                )
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE telegram_notification_outbox SET status='outcome_unknown',attempts=2"
                    )
                recovery()
                row = repository.get_notification(known.event_key)
                assert row is not None
                self.assertEqual((row["status"], row["attempts"]), ("retry_wait", 2))
                get_notification_result_5 = repository.get_notification(unknown.event_key)
                assert get_notification_result_5 is not None
                self.assertEqual(get_notification_result_5["status"], "outcome_unknown")
                self.assertTrue(center.drain_telegram_notifications(event_key=known.event_key))
                self.assertFalse(center.drain_telegram_notifications(event_key=unknown.event_key))
        self.sender.assert_not_called()

    def test_legacy_exhausted_edit_is_not_revived_by_restart(self):
        result = self.publish(self.terminal, message_id=77, deliver_now=False)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE telegram_notification_outbox SET status='outcome_unknown',attempts=?",
                (repository._MAX_ATTEMPTS,),
            )
        self.assertEqual(repository.recover_notifications(), 1)
        get_notification_result_6 = repository.get_notification(result.event_key)
        assert get_notification_result_6 is not None
        self.assertEqual(get_notification_result_6["status"], "failed")
        db.init_db()
        self.assertFalse(center.drain_telegram_notifications(event_key=result.event_key))
        self.editor.assert_not_called()
        self.sender.assert_not_called()

    def test_late_unknown_result_cannot_change_recovered_edit_lease(self):
        result = self.publish(self.terminal, message_id=77, deliver_now=False)
        old = repository.claim_due_notifications(event_key=result.event_key)[0]
        repository.recover_notifications()
        current = repository.claim_due_notifications(event_key=result.event_key)[0]
        before = repository.get_notification(result.event_key)
        self.assertFalse(repository.reject_notification_message(
            old["id"], lease_generation=old["lease_generation"], message_id=77,
        ))
        status = repository.retry_notification(
            old["id"], lease_generation=old["lease_generation"],
            claimed_revision=old["revision"], error="late timeout",
            outcome_unknown=True,
        )
        self.assertEqual(status, "stale")
        self.assertEqual(repository.get_notification(result.event_key), before)
        self.assertTrue(repository.complete_notification(
            current["id"], lease_generation=current["lease_generation"],
            claimed_revision=current["revision"], message_id=77,
        ))
