"""通知规则重置/重建复用整数版本时，确认不能覆盖另一份业务状态。"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

from app import database as db
from app.modules import media_subscription_notifications as actions
from app.modules.media_subscriptions import MediaSubscriptionError
from app.agent.models import ToolContext
from app.repositories import media_experience as repository
from tests.support import IsolatedDatabaseTestCase


class NotificationRuleReconfirmAuditTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_subscription_notification_rules")
            conn.execute("DELETE FROM media_subscriptions")
        self.context = ToolContext(owner="audit", session_id="isolated")
        self.sid = db.add_media_subscription(
            provider="tmdb",
            external_id="606",
            tmdb_id="606",
            media_type="tv",
            title="Reconfirm",
            action="confirm",
            download_target="guangya",
            sites=("mikan",),
        )
        current = repository.get_notification_rule(self.sid)
        assert current is not None
        self.initial = repository.set_notification_rule(
            self.sid,
            expected_rule_revision=0,
            expected_subscription_revision=current["subscription_revision"],
            updates={"enabled": True},
        )
        assert self.initial is not None

    def _replace_after_read(self, number):
        current = repository.get_notification_rule(number)
        assert current is not None
        self.assertTrue(
            repository.reset_notification_rule(
                number,
                expected_rule_revision=current["revision"],
                expected_subscription_revision=current["subscription_revision"],
            )
        )
        replacement = repository.set_notification_rule(
            number,
            expected_rule_revision=0,
            expected_subscription_revision=current["subscription_revision"],
            updates={"enabled": False},
        )
        assert replacement is not None
        self.assertEqual(replacement["revision"], current["revision"])
        self.assertEqual(
            replacement["subscription_revision"], current["subscription_revision"]
        )
        return current

    def test_reset_recreate_between_precheck_and_set_rejects_old_confirmation(
        self,
    ) -> None:
        arguments = {
            "subscription_id": self.sid,
            "enabled": True,
            "notify_on_satisfied": True,
        }
        fingerprint = actions.prepare_rule_change('set_notification', arguments)["snapshot"]
        with patch.object(actions, "notification_rule", self._replace_after_read):
            with self.assertRaises(MediaSubscriptionError) as raised:
                actions.apply_rule_change('set_notification', arguments, expected_snapshot=fingerprint)
        self.assertEqual(raised.exception.code, "confirmation_stale")
        actual = repository.get_notification_rule(self.sid)
        assert actual is not None
        self.assertFalse(actual["enabled"])
        self.assertFalse(actual["notify_on_satisfied"])
        self.assertEqual(actual["revision"], 1)

    def test_reset_recreate_between_precheck_and_reset_keeps_new_rule(self) -> None:
        arguments = {"subscription_id": self.sid}
        fingerprint = actions.prepare_rule_change('reset_notification', arguments)["snapshot"]
        with patch.object(actions, "notification_rule", self._replace_after_read):
            with self.assertRaises(MediaSubscriptionError) as raised:
                actions.apply_rule_change('reset_notification', arguments, expected_snapshot=fingerprint)
        self.assertEqual(raised.exception.code, "confirmation_stale")
        actual = repository.get_notification_rule(self.sid)
        assert actual is not None
        self.assertTrue(actual["explicit"])
        self.assertFalse(actual["enabled"])

    def test_write_failure_rolls_back_and_restart_can_retry_same_confirmation(
        self,
    ) -> None:
        arguments = {"subscription_id": self.sid, "notify_on_satisfied": True}
        fingerprint = actions.prepare_rule_change('set_notification', arguments)["snapshot"]
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER audit_rule_failure BEFORE UPDATE ON media_subscription_notification_rules BEGIN SELECT RAISE(ABORT,'audit interruption'); END"
            )
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                actions.apply_rule_change('set_notification', arguments, expected_snapshot=fingerprint)
            self.assertEqual(repository.get_notification_rule(self.sid), self.initial)
        finally:
            with db.get_conn() as conn:
                conn.execute("DROP TRIGGER audit_rule_failure")
        db.init_db()
        receipt = actions.apply_rule_change('set_notification', arguments, expected_snapshot=fingerprint)
        self.assertTrue(receipt["ok"])
        with self.assertRaises(MediaSubscriptionError) as raised:
            actions.apply_rule_change('set_notification', arguments, expected_snapshot=fingerprint)
        self.assertEqual(raised.exception.code, "confirmation_stale")
        actual = repository.get_notification_rule(self.sid)
        assert actual is not None
        self.assertTrue(actual["notify_on_satisfied"])
        self.assertEqual(actual["revision"], 2)

    def test_new_confirmation_after_reset_recreate_is_valid(self) -> None:
        self._replace_after_read(self.sid)
        arguments = {"subscription_id": self.sid, "enabled": True}
        fingerprint = actions.prepare_rule_change('set_notification', arguments)["snapshot"]
        result = actions.apply_rule_change('set_notification', arguments, expected_snapshot=fingerprint)
        self.assertTrue(result["ok"])
        reset_fingerprint = actions.prepare_rule_change('reset_notification', {"subscription_id": self.sid})["snapshot"]
        reset = actions.apply_rule_change('reset_notification', {"subscription_id": self.sid}, expected_snapshot=reset_fingerprint)
        self.assertTrue(reset["ok"])
        current = repository.get_notification_rule(self.sid)
        assert current is not None
        self.assertFalse(current["explicit"])
        self.assertEqual(current["revision"], 0)
