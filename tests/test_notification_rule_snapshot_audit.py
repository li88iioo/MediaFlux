"""通知规则的查询与写回执必须投影同一个真实 SQLite 版本。"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from app import database as db
from app.repositories import media_subscriptions as media_subscriptions_repo
from app.repositories import media_experience as repository
from tests.support import IsolatedDatabaseTestCase


class NotificationRuleSnapshotAuditTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_subscription_notification_rules")
            conn.execute("DELETE FROM media_subscriptions")
        self.sid = media_subscriptions_repo.add_media_subscription(
            provider="tmdb",
            external_id=self.id(),
            tmdb_id="321",
            media_type="tv",
            title="Before",
            action="confirm",
            download_target="guangya",
            sites=("mikan",),
            enabled=True,
        )
        self.initial = repository.get_notification_rule(self.sid)
        assert self.initial is not None

    def _save(self, **updates):
        assert self.initial is not None
        return repository.set_notification_rule(
            self.sid,
            expected_rule_revision=0,
            expected_subscription_revision=self.initial["subscription_revision"],
            updates=updates,
        )

    def test_read_never_mixes_subscription_before_with_rule_after(self) -> None:
        saved = self._save(enabled=True)
        assert saved is not None
        connect = db.get_conn
        changed = False

        def update_both():
            nonlocal changed
            if changed:
                return
            changed = True
            with connect() as writer:
                writer.execute(
                    "UPDATE media_subscriptions SET title='After',revision=revision+1 WHERE id=?",
                    (self.sid,),
                )
                writer.execute(
                    "UPDATE media_subscription_notification_rules SET enabled=0,revision=revision+1 WHERE subscription_id=?",
                    (self.sid,),
                )

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchone(self):
                row = self.cursor.fetchone()
                self.cursor.close()
                update_both()
                return row

        class Connection:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, parameters=()):
                cursor = self.conn.execute(sql, parameters)
                if "FROM media_subscriptions" in sql and not changed:
                    return Cursor(cursor)
                return cursor

        @contextmanager
        def hooked():
            with connect() as conn:
                yield Connection(conn)

        with patch.object(db, "get_conn", hooked):
            result = repository.get_notification_rule(self.sid)
        self.assertTrue(changed)
        self.assertEqual(result, saved)
        latest = repository.get_notification_rule(self.sid)
        assert latest is not None
        self.assertEqual((latest["title"], latest["enabled"]), ("After", False))
        self.assertEqual(latest["revision"], saved["revision"] + 1)

    def test_write_receipt_is_own_commit_not_next_writer(self) -> None:
        connect = db.get_conn
        changed = False

        @contextmanager
        def hooked():
            nonlocal changed
            with connect() as conn:
                yield conn
                wrote = conn.in_transaction
            if wrote and not changed:
                changed = True
                with connect() as writer:
                    writer.execute(
                        "UPDATE media_subscription_notification_rules SET enabled=0,revision=revision+1 WHERE subscription_id=?",
                        (self.sid,),
                    )

        with patch.object(db, "get_conn", hooked):
            receipt = self._save(enabled=True)
        self.assertTrue(changed)
        assert receipt is not None
        self.assertEqual((receipt["enabled"], receipt["revision"]), (True, 1))
        latest = repository.get_notification_rule(self.sid)
        assert latest is not None
        self.assertEqual((latest["enabled"], latest["revision"]), (False, 2))

    def test_commit_receipt_survives_later_subscription_deletion(self) -> None:
        connect = db.get_conn
        deleted = False

        @contextmanager
        def hooked():
            nonlocal deleted
            with connect() as conn:
                yield conn
                wrote = conn.in_transaction
            if wrote and not deleted:
                deleted = True
                with connect() as writer:
                    writer.execute(
                        "UPDATE media_subscriptions SET deleted_at='2026-01-01 00:00:00' WHERE id=?",
                        (self.sid,),
                    )

        with patch.object(db, "get_conn", hooked):
            receipt = self._save(enabled=True)
        self.assertTrue(deleted)
        assert (receipt) is not None
        self.assertIsNone(repository.get_notification_rule(self.sid))

    def test_defaults_cas_partial_update_and_missing_subscription(self) -> None:
        assert self.initial is not None
        self.assertFalse(self.initial["explicit"])
        self.assertFalse(self.initial["enabled"])
        self.assertEqual(self.initial["revision"], 0)
        self.assertTrue(self.initial["notify_on_missing"])
        saved = self._save(enabled=True, notify_on_error=False)
        assert saved is not None
        self.assertEqual(saved, repository.get_notification_rule(self.sid))
        self.assertTrue(saved["explicit"])
        self.assertTrue(saved["notify_on_missing"])
        self.assertFalse(saved["notify_on_error"])
        self.assertIsNone(self._save(enabled=False))
        self.assertEqual(saved, repository.get_notification_rule(self.sid))
        self.assertIsNone(repository.get_notification_rule(-1))
