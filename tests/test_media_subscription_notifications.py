"""媒体追更通知规则、原子 outbox、重试与恢复回归。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from app import database as db
from app.modules.media_subscription_notifications import (
    drain_media_subscription_notifications,
)
from app.notifier import TelegramSendResult
from app.repositories.media_experience import (
    claim_due_notifications,
    get_notification_rule,
    list_notification_outbox,
    mark_notification_sent,
    recover_notifications,
    retry_notification,
    set_notification_rule,
)
from tests.support import IsolatedDatabaseTestCase


class MediaSubscriptionNotificationTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_subscription_notification_outbox")
            conn.execute("DELETE FROM media_subscription_notification_rules")
            conn.execute("DELETE FROM media_subscription_runs")
            conn.execute("DELETE FROM media_subscriptions")
        self.sid = db.add_media_subscription(
            provider="tmdb",
            external_id="54321",
            tmdb_id="54321",
            media_type="tv",
            title="安全追更标题",
            original_title="PRIVATE ORIGINAL",
            year="2026",
            poster_key="/private/poster.jpg",
            action="confirm",
            download_target="guangya",
            sites=("mikan",),
            enabled=True,
        )

    def _enable_rule(self, **updates: bool) -> None:
        current = get_notification_rule(self.sid)
        assert current is not None
        result = set_notification_rule(
            self.sid,
            expected_rule_revision=int(current["revision"]),
            expected_subscription_revision=int(current["subscription_revision"]),
            updates={"enabled": True, **updates},
        )
        self.assertIsNotNone(result)

    def _finalize(self, *, status: str = "missing", missing_count: int = 1) -> int:
        run_id = db.claim_media_subscription_check_run(self.sid, "manual")
        self.assertIsNotNone(run_id)
        revision = int(db.get_media_subscription(self.sid)["revision"])
        committed = db.finalize_media_subscription_check(
            self.sid,
            int(run_id),
            status=status,
            run_status=status,
            summary="检查完成",
            payload={"status": status},
            interval_minutes=60,
            expected_count=10,
            local_count=10 - missing_count,
            missing_count=missing_count,
            missing_json="[]",
            result_json="{}",
            subscription_revision=revision,
        )
        self.assertTrue(committed)
        return int(run_id)

    def test_disabled_rule_does_not_enqueue(self) -> None:
        self._finalize()
        self.assertEqual(list_notification_outbox(), [])

    def test_finalize_enqueues_once_in_committed_transaction(self) -> None:
        self._enable_rule(notify_on_missing=True)
        run_id = self._finalize()
        rows = list_notification_outbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_key"], f"media-subscription-run:{run_id}:missing")
        self.assertEqual(rows[0]["status"], "pending")

        revision = int(db.get_media_subscription(self.sid)["revision"])
        duplicate = db.finalize_media_subscription_check(
            self.sid,
            run_id,
            status="missing",
            run_status="missing",
            summary="迟到结果",
            payload={},
            interval_minutes=60,
            expected_count=10,
            local_count=9,
            missing_count=1,
            missing_json="[]",
            result_json="{}",
            subscription_revision=revision,
        )
        self.assertFalse(duplicate)
        self.assertEqual(len(list_notification_outbox()), 1)

    def test_stale_run_does_not_enqueue(self) -> None:
        self._enable_rule(notify_on_missing=True)
        run_id = db.claim_media_subscription_check_run(self.sid, "manual")
        self.assertIsNotNone(run_id)
        old_revision = int(db.get_media_subscription(self.sid)["revision"])
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_subscriptions SET revision=revision+1,status='new' WHERE id=?",
                (self.sid,),
            )
        committed = db.finalize_media_subscription_check(
            self.sid,
            int(run_id),
            status="missing",
            run_status="missing",
            summary="旧结果",
            payload={},
            interval_minutes=60,
            expected_count=1,
            local_count=0,
            missing_count=1,
            missing_json="[]",
            result_json="{}",
            subscription_revision=old_revision,
        )
        self.assertFalse(committed)
        self.assertEqual(list_notification_outbox(), [])

    def test_failure_event_is_enqueued_without_error_body(self) -> None:
        self._enable_rule(notify_on_error=True)
        run_id = db.claim_media_subscription_check_run(self.sid, "manual")
        self.assertIsNotNone(run_id)
        revision = int(db.get_media_subscription(self.sid)["revision"])
        self.assertTrue(db.fail_media_subscription_check(
            self.sid,
            int(run_id),
            interval_minutes=60,
            error="PRIVATE traceback /private/path token=SECRET",
            subscription_revision=revision,
        ))
        row = list_notification_outbox()[0]
        self.assertEqual(row["event_type"], "error")
        self.assertNotIn("PRIVATE traceback", row["payload_json"])
        self.assertNotIn("SECRET", row["payload_json"])

    def test_drain_success_is_idempotent_and_safe(self) -> None:
        self._enable_rule(notify_on_missing=True)
        self._finalize()
        with patch(
            "app.notifier.send_result", return_value=TelegramSendResult(ok=True)
        ) as send:
            self.assertTrue(drain_media_subscription_notifications())
            self.assertTrue(drain_media_subscription_notifications())
        send.assert_called_once()
        body = send.call_args.args[0]
        self.assertIn("安全追更标题", body)
        self.assertNotIn("PRIVATE", body)
        self.assertEqual(list_notification_outbox()[0]["status"], "sent")

    def test_notification_title_is_escaped_for_telegram_html(self) -> None:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_subscriptions SET title=? WHERE id=?",
                ("<b>伪装 & <broken", self.sid),
            )
        self._enable_rule(notify_on_missing=True)
        self._finalize()
        with patch(
            "app.notifier.send_result", return_value=TelegramSendResult(ok=True)
        ) as send:
            self.assertTrue(drain_media_subscription_notifications())
        body = send.call_args.args[0]
        self.assertIn("&lt;b&gt;伪装 &amp;", body)
        self.assertIn("&lt;broken", body)
        self.assertNotIn("<b>", body)
        self.assertNotIn("<broken", body)

    def test_retry_after_recovery_uses_lease_fence(self) -> None:
        self._enable_rule(notify_on_missing=True)
        self._finalize()
        first = claim_due_notifications(limit=1)[0]
        first_generation = int(first["lease_generation"])
        self.assertEqual(recover_notifications(), 0)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_subscription_notification_outbox "
                "SET lease_until='2000-01-01 00:00:00' WHERE id=?",
                (first["id"],),
            )
        self.assertEqual(recover_notifications(), 1)
        second = claim_due_notifications(limit=1)[0]
        second_generation = int(second["lease_generation"])
        self.assertGreater(second_generation, first_generation)
        self.assertFalse(mark_notification_sent(
            first["id"], lease_generation=first_generation
        ))
        self.assertEqual(
            retry_notification(
                first["id"],
                lease_generation=first_generation,
                error="late worker",
            ),
            "stale",
        )
        self.assertTrue(mark_notification_sent(
            second["id"], lease_generation=second_generation
        ))

    def test_retry_respects_telegram_retry_after(self) -> None:
        self._enable_rule(notify_on_missing=True)
        self._finalize()
        with patch(
            "app.notifier.send_result",
            return_value=TelegramSendResult(
                ok=False, error="rate limited", retry_after_seconds=120
            ),
        ):
            self.assertFalse(drain_media_subscription_notifications())
        row = list_notification_outbox()[0]
        self.assertEqual(row["status"], "retry_wait")
        self.assertEqual(int(row["attempts"]), 1)
        self.assertEqual(row["last_error"], "telegram_rate_limited")
        next_at = datetime.strptime(row["next_attempt_at"], "%Y-%m-%d %H:%M:%S")
        updated = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
        self.assertGreaterEqual((next_at - updated).total_seconds(), 120)

    def test_scheduler_drain_keeps_outboxes_isolated(self) -> None:
        from app.modules.scheduler import STRMScheduler

        with (
            patch(
                "app.repositories.organize_notifications.count_pending_organize_notifications",
                return_value=1,
            ),
            patch(
                "app.modules.organize_notification_outbox.drain_organize_notifications",
                side_effect=RuntimeError("organize failed"),
            ),
            patch(
                "app.modules.media_subscription_notifications.drain_media_subscription_notifications"
            ) as media,
        ):
            STRMScheduler._drain_notification_outbox()
        media.assert_called_once_with(limit=20)

    def test_scheduler_recovers_both_notification_queues(self) -> None:
        from app.modules.scheduler import STRMScheduler

        with (
            patch(
                "app.modules.organize_notification_outbox.recover_organize_notifications"
            ) as organize,
            patch(
                "app.modules.media_subscription_notifications.recover_media_subscription_notifications"
            ) as media,
        ):
            STRMScheduler._recover_notification_outbox()
        organize.assert_called_once_with()
        media.assert_called_once_with()
