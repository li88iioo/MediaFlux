"""订阅事务通知逐条移交，单条坏数据或回执异常不能扣住整个领取批次。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import database as db
from app.repositories import media_subscriptions as media_subscriptions_repo
from app.modules import media_subscription_notifications as handoff
from app.modules.telegram_notification_center import NotificationPublishResult
from app.notifier import TelegramSendResult
from app.repositories import media_experience as repository
from tests.support import isolated_test_database


class NotificationHandoffIsolationAuditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.enterContext(
            patch("app.modules.media_automation_rules.drain_automation_rules")
        )
        self.counter = 0

    def enqueue(self, payload=None):
        self.counter += 1
        sid = media_subscriptions_repo.add_media_subscription(
            provider="tmdb",
            external_id=str(self.counter),
            tmdb_id=str(self.counter),
            media_type="tv",
            title=f"Synthetic {self.counter}",
        )
        encoded = json.dumps(
            payload
            if payload is not None
            else {"title": f"Synthetic {self.counter}", "missing_count": 1}
        )
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO media_subscription_notification_outbox(event_key,subscription_id,subscription_revision,event_type,payload_json,next_attempt_at,created_at,updated_at) VALUES(?,?,1,?,?,?, ?,?)",
                (
                    f"audit:{self.counter}",
                    sid,
                    "missing",
                    encoded,
                    db.now(),
                    db.now(),
                    db.now(),
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    @staticmethod
    def row(notification_id):
        with db.get_conn() as conn:
            return dict(
                conn.execute(
                    "SELECT * FROM media_subscription_notification_outbox WHERE id=?",
                    (notification_id,),
                ).fetchone()
            )

    def test_bad_historical_counts_retry_without_blocking_the_next_event(self):
        bad = self.enqueue({"missing_count": "not-a-number"})
        good = self.enqueue()
        original = self.row(bad)["payload_json"]
        with patch(
            "app.modules.telegram_notification_center.publish_notification_event",
            return_value=NotificationPublishResult(True, queued=True, status="pending"),
        ) as publish:
            self.assertFalse(handoff.drain_media_subscription_notifications())
        self.assertEqual(self.row(bad)["status"], "retry_wait")
        self.assertEqual(self.row(bad)["attempts"], 1)
        self.assertEqual(self.row(bad)["payload_json"], original)
        self.assertEqual(self.row(good)["status"], "sent")
        publish.assert_called_once()
        self.assertIn(f":{good}:", publish.call_args.args[0])

    def test_publisher_exception_isolated_to_its_own_lease(self):
        bad, good = self.enqueue(), self.enqueue()
        with patch(
            "app.modules.telegram_notification_center.publish_notification_event",
            side_effect=[
                RuntimeError("synthetic handoff failure"),
                NotificationPublishResult(True, status="sent"),
            ],
        ) as publish:
            self.assertFalse(handoff.drain_media_subscription_notifications())
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(self.row(bad)["status"], "retry_wait")
        self.assertEqual(self.row(good)["status"], "sent")

    def test_retry_persistence_failure_preserves_lease_for_recovery_and_continues(self):
        bad, good = self.enqueue({"candidate_count": "bad"}), self.enqueue()
        with (
            patch.object(
                handoff,
                "retry_notification",
                side_effect=RuntimeError("synthetic persistence failure"),
            ),
            patch(
                "app.modules.telegram_notification_center.publish_notification_event",
                return_value=NotificationPublishResult(True, status="sent"),
            ),
        ):
            self.assertFalse(handoff.drain_media_subscription_notifications())
        self.assertEqual(self.row(bad)["status"], "sending")
        self.assertEqual(self.row(good)["status"], "sent")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_subscription_notification_outbox SET lease_until='' WHERE id=?",
                (bad,),
            )
        self.assertEqual(repository.recover_notifications(), 1)
        self.assertEqual(self.row(bad)["status"], "retry_wait")

    def test_ack_failure_replays_same_notification_key_without_resending(self):
        first, second = self.enqueue(), self.enqueue()
        original_ack = handoff.mark_notification_sent
        seen = []

        def ack(notification_id, *, lease_generation):
            if notification_id == first and first not in seen:
                seen.append(first)
                raise RuntimeError("synthetic domain ACK failure")
            return original_ack(notification_id, lease_generation=lease_generation)

        with (
            patch(
                "app.modules.telegram_notification_center.notification_target_chat_id",
                return_value="synthetic-chat",
            ),
            patch(
                "app.modules.telegram_notification_center.allows_notification",
                return_value=True,
            ),
            patch(
                "app.modules.telegram_notification_center.send_event_result",
                return_value=TelegramSendResult(ok=True, message_id=100),
            ) as send,
            patch.object(handoff, "mark_notification_sent", side_effect=ack),
        ):
            self.assertFalse(handoff.drain_media_subscription_notifications())
            self.assertEqual(self.row(first)["status"], "retry_wait")
            self.assertEqual(self.row(second)["status"], "sent")
            self.assertEqual(send.call_count, 2)
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE media_subscription_notification_outbox SET next_attempt_at=? WHERE id=?",
                    (db.now(), first),
                )
            self.assertTrue(handoff.drain_media_subscription_notifications())
            self.assertEqual(self.row(first)["status"], "sent")
            self.assertEqual(send.call_count, 2)

    def test_interruption_after_handoff_recovers_remaining_batch_without_resend(self):
        from app.modules import telegram_notification_center as center

        first, middle, last = self.enqueue(), self.enqueue(), self.enqueue()
        original_publish = center.publish_notification_event

        def interrupt(key, *args, **kwargs):
            result = original_publish(key, *args, **kwargs)
            if key == f"media-subscription:{middle}:missing":
                raise KeyboardInterrupt("synthetic process interruption")
            return result

        with (
            patch.object(
                center, "notification_target_chat_id", return_value="synthetic-chat"
            ),
            patch.object(center, "allows_notification", return_value=True),
            patch.object(
                center,
                "send_event_result",
                return_value=TelegramSendResult(ok=True, message_id=100),
            ) as send,
        ):
            with (
                patch.object(
                    center, "publish_notification_event", side_effect=interrupt
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                handoff.drain_media_subscription_notifications()
            self.assertEqual(self.row(first)["status"], "sent")
            self.assertEqual(self.row(middle)["status"], "sending")
            self.assertEqual(self.row(last)["status"], "sending")
            self.assertEqual(send.call_count, 2)
            db.init_db()
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE media_subscription_notification_outbox SET lease_until='' WHERE status='sending'"
                )
            self.assertEqual(repository.recover_notifications(), 2)
            self.assertTrue(handoff.drain_media_subscription_notifications())
            self.assertEqual(
                [self.row(n)["status"] for n in (first, middle, last)], ["sent"] * 3
            )
            self.assertEqual(send.call_count, 3)
            self.assertTrue(handoff.drain_media_subscription_notifications())
            self.assertEqual(send.call_count, 3)

    def test_poison_payload_reaches_existing_retry_limit_without_rewriting_history(
        self,
    ):
        bad = self.enqueue({"missing_count": "bad"})
        original_payload = self.row(bad)["payload_json"]
        with patch(
            "app.modules.telegram_notification_center.publish_notification_event",
            return_value=NotificationPublishResult(True, status="sent"),
        ):
            for attempt in range(repository._MAX_ATTEMPTS):
                good = self.enqueue()
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE media_subscription_notification_outbox SET next_attempt_at=? WHERE id=?",
                        (db.now(), bad),
                    )
                self.assertFalse(handoff.drain_media_subscription_notifications())
                self.assertEqual(self.row(good)["status"], "sent")
                self.assertEqual(self.row(bad)["attempts"], attempt + 1)
        self.assertEqual(self.row(bad)["status"], "failed")
        db.init_db()
        self.assertEqual(self.row(bad)["payload_json"], original_payload)
        self.assertEqual(self.row(bad)["status"], "failed")
        self.assertTrue(handoff.drain_media_subscription_notifications())
