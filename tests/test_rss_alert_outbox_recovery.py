"""RSS 告警与真实隔离通知 outbox：恢复复发、跨订阅、崩溃后不丢不重。"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

from app import database as db
from app.modules import telegram_notification_center as center
from app.modules.rss_scheduler import RSSScheduler
from app.notifier import NotificationEvent
from app.repositories.telegram_notifications import (
    claim_due_notifications,
    complete_notification,
)
from tests.support import IsolatedDatabaseTestCase


class RSSAlertOutboxRecoveryTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM telegram_notification_outbox")
            conn.execute("DELETE FROM settings_kv WHERE key LIKE 'rss.scheduler.alert_signature.%'")
        for mock in (
            patch("app.repositories.rss.get_rss_subscription", return_value={"name": "测试订阅"}),
            patch.object(center, "notification_target_chat_id", return_value="test-chat"),
            patch.object(center, "allows_notification", return_value=True),
            patch.object(center._dispatch_stop, "is_set", return_value=True),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    @staticmethod
    def _notify(scheduler, sub_id=1, code="partial_failure"):
        scheduler._notify_issue(sub_id, code, [("失败", 1)])

    @staticmethod
    def _outbox():
        with db.get_conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM telegram_notification_outbox ORDER BY id")]

    @staticmethod
    def _ack_all():
        for row in claim_due_notifications(limit=100):
            complete_notification(
                row["id"], lease_generation=row["lease_generation"],
                claimed_revision=row["revision"], message_id=row["id"] + 100,
            )

    def test_recovery_then_identical_failure_creates_new_deliverable_event(self) -> None:
        scheduler = RSSScheduler()
        self._notify(scheduler)
        self._ack_all()
        self._notify(scheduler)
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 1)
        scheduler._clear_issue(1)
        self._notify(RSSScheduler())
        rows = self._outbox()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["status"] for row in rows], ["sent", "pending"])
        self.assertNotEqual(rows[0]["event_key"], rows[1]["event_key"])

    def test_different_issue_then_same_issue_is_a_new_occurrence(self) -> None:
        scheduler = RSSScheduler()
        for code in ("partial_failure", "outcome_unknown", "partial_failure"):
            self._notify(scheduler, code=code)
            self._ack_all()
        self.assertEqual(len(self._outbox()), 3)

    def test_subscriptions_keep_independent_occurrence_identities(self) -> None:
        scheduler = RSSScheduler()
        self._notify(scheduler, 1)
        self._notify(scheduler, 2)
        scheduler._clear_issue(1)
        self._notify(RSSScheduler(), 1)
        self._notify(RSSScheduler(), 2)
        self.assertEqual(len(self._outbox()), 3)

    def test_crash_after_outbox_before_ack_reuses_reserved_event(self) -> None:
        publish = center.publish_notification_event

        def interrupted(*args, **kwargs):
            publish(*args, **kwargs)
            raise KeyboardInterrupt("crash after durable outbox insert")

        with (
            patch.object(center, "publish_notification_event", side_effect=interrupted),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._notify(RSSScheduler())
        self._ack_all()
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 1)
        # 即使 ACK 写入前崩溃，也要记住本次 occurrence，恢复后复发不能被旧 key 吞掉。
        RSSScheduler()._clear_issue(1)
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 2)

    def test_legacy_signature_is_not_replayed_but_recurrence_is_new(self) -> None:
        scheduler = RSSScheduler()
        legacy = json.dumps(("partial_failure", "1"), ensure_ascii=False, separators=(",", ":"))
        db.kv_set(scheduler._alert_key(1), legacy)
        digest = hashlib.sha256(legacy.encode()).hexdigest()[:20]
        center.publish_notification_event(
            f"rss-alert:1:{digest}", NotificationEvent("历史告警"), topic="rss", importance="error",
        )
        self._ack_all()
        self._notify(scheduler)
        self.assertEqual(len(self._outbox()), 1)
        scheduler._clear_issue(1)
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 2)

    def test_ack_write_failure_retries_same_outbox_event_after_restart(self) -> None:
        scheduler = RSSScheduler()
        save = scheduler._save_alert

        def fail_ack(sub_id, alert):
            if alert["accepted"]:
                raise RuntimeError("disk busy after outbox commit")
            return save(sub_id, alert)

        with patch.object(scheduler, "_save_alert", side_effect=fail_ack):
            self._notify(scheduler)
        pending = json.loads(db.kv_get(scheduler._alert_key(1)))
        self.assertFalse(pending["accepted"])
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 1)
        self.assertTrue(json.loads(db.kv_get(scheduler._alert_key(1)))["accepted"])

    def test_failed_reservation_does_not_publish_unrecoverable_event(self) -> None:
        with (
            patch.object(RSSScheduler, "_save_alert", side_effect=RuntimeError("disk unavailable")),
            patch.object(center, "publish_notification_event") as publish,
        ):
            self._notify(RSSScheduler())
        publish.assert_not_called()
        self.assertEqual(self._outbox(), [])

    def test_failed_publish_retries_pending_identity_and_recovery_clears_it(self) -> None:
        scheduler = RSSScheduler()
        with patch.object(center, "publish_notification_event", return_value=False):
            self._notify(scheduler)
        reserved = json.loads(db.kv_get(scheduler._alert_key(1)))["event_key"]
        with patch.object(center, "publish_notification_event", wraps=center.publish_notification_event) as publish:
            self._notify(RSSScheduler())
        self.assertEqual(publish.call_args.args[0], reserved)
        self.assertEqual(len(self._outbox()), 1)
        scheduler._clear_issue(1)
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 2)

    def test_unknown_history_does_not_replay_until_business_recovery(self) -> None:
        scheduler = RSSScheduler()
        db.kv_set(scheduler._alert_key(1), '{"version":999,"signature":["partial_failure","1"]}')
        self._notify(scheduler)
        self.assertEqual(self._outbox(), [])
        scheduler._clear_issue(1)
        self._notify(RSSScheduler())
        self.assertEqual(len(self._outbox()), 1)
