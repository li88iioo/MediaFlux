"""公共补偿业务：真实偏好事务、历史载荷与组织执行器状态。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.database import media_owner_digest
from app.modules.activity_undo import (
    execute_undo, inspect_undo, preference_compensation, preference_undo_receipt, prepare_undo,
)
from app.modules.media_preferences import (
    apply_media_preferences_change, media_preferences_preview, prepare_media_preferences_change,
)
from app.repositories.media_experience import get_media_preferences
from app.repositories import agent_compensations as receipts
from tests.support import IsolatedDatabaseTestCase


class ActionUndoTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_compensations")
            conn.execute("DELETE FROM agent_media_preferences")
        self.context = SimpleNamespace(owner="undo-test", session_id="session")

    def write_preferences(self, updates):
        digest = media_owner_digest(self.context.owner)
        prepared = prepare_media_preferences_change(digest, updates)
        candidate = preference_compensation("preferences.update", updates, {"data": media_preferences_preview(prepared)})
        after = apply_media_preferences_change(digest, updates, expected_fingerprint=prepared["fingerprint"])
        self.assertIsNotNone(after)
        return after, candidate

    def forward(self):
        after, candidate = self.write_preferences({"preferred_download_target": "qb"})
        receipt = preference_undo_receipt(candidate, after, key="receipt-test", owner=self.context.owner)
        assert receipt is not None
        return receipt

    def test_settings_can_be_undone_once_and_restore_implicit_defaults(self):
        receipt = self.forward()
        self.assertEqual(get_media_preferences(media_owner_digest(self.context.owner))["preferred_download_target"], "qb")
        prepared = prepare_undo(receipt, owner=self.context.owner)
        self.assertTrue(execute_undo(receipt, expected_fingerprint=prepared["fingerprint"], owner=self.context.owner)["ok"])
        self.assertFalse(get_media_preferences(media_owner_digest(self.context.owner))["explicit"])
        with self.assertRaises(ValueError):
            prepare_undo(receipt, owner=self.context.owner)

    def test_intervening_changes_make_receipt_stale(self):
        receipt = self.forward()
        self.write_preferences({"preferred_download_target": "both"})
        with self.assertRaises(ValueError):
            prepare_undo(receipt, owner=self.context.owner)
        self.assertEqual(get_media_preferences(media_owner_digest(self.context.owner))["preferred_download_target"], "both")

    def test_owner_cannot_consume_another_owners_receipt(self):
        receipt = self.forward()
        with self.assertRaises(ValueError):
            prepare_undo(receipt, owner="other")

    def test_stored_legacy_preference_payloads_use_shared_business_without_old_handlers(self):
        for explicit in (False, True):
            for prefix in ("media", "preferences"):
                with self.subTest(explicit=explicit, prefix=prefix):
                    with db.get_conn() as conn:
                        conn.execute("DELETE FROM agent_media_preferences")
                        conn.execute("DELETE FROM agent_compensations")
                    if explicit:
                        self.write_preferences({"preferred_download_target": "guangya", "memory_notes": ["保留备忘"]})
                    after, candidate = self.write_preferences({"preferred_download_target": "qb"})
                    from app.modules.media_preferences import snapshot_media_preferences_change
                    assert candidate is not None
                    updates = candidate["updates"]
                    payload = {"receipt_id": "historical", "expected": snapshot_media_preferences_change(after, updates)["fingerprint"]}
                    payload.update(kind="setting", tool=(f"{prefix}.set_preferences" if explicit else f"{prefix}.clear_preferences")
                                   if prefix == "media" else ("preferences.update" if explicit else "preferences.clear"),
                                   arguments=updates or {})
                    receipt = receipts.create("historical", media_owner_digest(self.context.owner), payload=payload)
                    prepared = prepare_undo(receipt, owner=self.context.owner)
                    self.assertTrue(execute_undo(receipt, expected_fingerprint=prepared["fingerprint"], owner=self.context.owner)["ok"])
                    restored = get_media_preferences(media_owner_digest(self.context.owner))
                    self.assertEqual(restored["explicit"], explicit)
                    self.assertEqual(restored["preferred_download_target"], "guangya")
                    self.assertEqual(restored["memory_notes"], ["保留备忘"] if explicit else [])

    def test_unknown_receipt_cannot_replace_frozen_receipt(self):
        receipt = self.forward()
        expected = prepare_undo(receipt, owner=self.context.owner)["fingerprint"]
        receipt = "unknown-receipt"
        with self.assertRaises(ValueError):
            execute_undo(receipt, expected_fingerprint=expected, owner=self.context.owner)

    def test_unsupported_irreversible_tools_do_not_issue_compensation(self):
        for name in ("ingest.submit", "guangya.recycle.clear", "rss.delete_subscription", "preferences.clear"):
            self.assertIsNone(preference_compensation(name, {}, {}))

    def test_newer_write_before_receipt_creation_is_not_captured_as_our_write(self):
        after, candidate = self.write_preferences({"preferred_download_target": "qb"})
        self.write_preferences({"preferred_download_target": "both"})
        self.assertIsNone(preference_undo_receipt(candidate, after, key="late-receipt", owner=self.context.owner))
        self.assertEqual(get_media_preferences(media_owner_digest(self.context.owner))["preferred_download_target"], "both")

    def test_missing_transactional_after_snapshot_does_not_issue_receipt(self):
        _, candidate = self.write_preferences({"preferred_download_target": "qb"})
        self.assertIsNone(preference_undo_receipt(candidate, None, key="no-proof", owner=self.context.owner))

    def organize_receipt(self):
        value = {"title": "示例", "version": 1, "changes": [], "affected": 1}
        # 组织快照只来自当前真实日志，不假设旧 target 已迁入账本。
        receipt = receipts.create("organize-receipt", media_owner_digest(self.context.owner), payload={"kind": "organize",
                               "log_id": 1, "expected": "version-one", "version": 1})
        with patch("app.modules.activity_undo._organize_state", return_value=(value, "version-one")):
            expected = prepare_undo(receipt, owner=self.context.owner)["fingerprint"]
        return value, receipt, expected

    def test_organize_second_read_cannot_adopt_new_unapproved_version(self):
        from unittest.mock import patch

        first, args, token = self.organize_receipt()
        second = {**first, "version": 2}
        with (
            patch(
                "app.modules.activity_undo._organize_state",
                side_effect=[(first, "version-one"), (second, "version-two")],
            ),
            patch(
                "app.modules.organize_correction.OrganizeCorrectionService"
            ) as service,
            patch("app.modules.organize_tasks.get_organize_manager") as manager,
            self.assertRaises(ValueError),
        ):
            execute_undo(args, expected_fingerprint=token, owner=self.context.owner)
        service.assert_not_called()
        manager.assert_not_called()

    def test_claim_database_failure_does_not_create_external_service(self):
        import sqlite3
        from unittest.mock import patch

        first, args, token = self.organize_receipt()
        with (
            patch(
                "app.modules.activity_undo._organize_state",
                return_value=(first, "version-one"),
            ),
            patch(
                "app.modules.activity_undo.receipts.claim",
                side_effect=sqlite3.OperationalError("busy"),
            ),
            patch(
                "app.modules.organize_correction.OrganizeCorrectionService"
            ) as service,
            self.assertRaises(sqlite3.OperationalError),
        ):
            execute_undo(args, expected_fingerprint=token, owner=self.context.owner)
        service.assert_not_called()

    def test_queued_organize_uses_frozen_version_and_closes_service(self):
        from unittest.mock import Mock, patch

        first, args, token = self.organize_receipt()
        client = Mock()
        client.revert_latest.return_value = {"success": True}
        callbacks = []
        manager = Mock()
        manager.start_operation.side_effect = lambda _name, _title, callback: (
            callbacks.append(callback) or {"ok": True, "task_id": "private-undo-task"}
        )
        with (
            patch(
                "app.modules.activity_undo._organize_state",
                return_value=(first, "version-one"),
            ),
            patch(
                "app.modules.organize_correction.OrganizeCorrectionService",
                return_value=client,
            ),
            patch(
                "app.modules.organize_tasks.get_organize_manager", return_value=manager
            ),
        ):
            result = execute_undo(args, expected_fingerprint=token, owner=self.context.owner)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["data"]["task_id"], "private-undo-task")
            self.assertEqual(result["data"]["log_id"], 1)
            client.revert_latest.assert_not_called()
            callbacks[0]()
            client.revert_latest.assert_called_once_with(1, "undo-organize-receipt", 1)
            client.close.assert_called_once()

    def test_busy_organize_can_reinspect_and_retry_same_receipt_exactly_once(self):
        from unittest.mock import Mock, patch

        from app.modules.organize_tasks import OrganizeTaskManager
        from app.repositories import agent_compensations as receipts

        public = {"title": "示例", "version": 1, "changes": [], "affected": 1}

        manager = OrganizeTaskManager()
        self.addCleanup(manager.shutdown, timeout=2)
        unused, worker_client = Mock(), Mock()
        worker_client.revert_latest.return_value = {"success": True}
        digest = media_owner_digest(self.context.owner)
        with (
            patch("app.modules.activity_undo._organize_state", return_value=(public, "version-one")),
            patch("app.modules.organize_correction.OrganizeCorrectionService", side_effect=[unused, worker_client]),
            patch("app.modules.organize_tasks.get_organize_manager", return_value=manager),
        ):
            receipt = inspect_undo(1, owner=self.context.owner)["data"]["receipt_id"]
            arguments = receipt
            expected = prepare_undo(arguments, owner=self.context.owner)["fingerprint"]
            self.assertTrue(manager._lock.acquire(blocking=False))
            try:
                rejected = execute_undo(arguments, expected_fingerprint=expected, owner=self.context.owner)
            finally:
                manager._lock.release()
            self.assertEqual(rejected["status"], "busy")
            unused.revert_latest.assert_not_called()
            unused.close.assert_called_once()
            self.assertEqual(receipts.state(receipt, digest), "available")
            fresh_receipt = inspect_undo(1, owner=self.context.owner)["data"]["receipt_id"]
            self.assertEqual(receipt, fresh_receipt)
            expected = prepare_undo(fresh_receipt, owner=self.context.owner)["fingerprint"]
            accepted = execute_undo(arguments, expected_fingerprint=expected, owner=self.context.owner)
            self.assertEqual(accepted["status"], "accepted")
            self.assertTrue(manager.shutdown(timeout=2))
            worker_client.revert_latest.assert_called_once()
            worker_client.close.assert_called_once()
            self.assertEqual(receipts.state(receipt, digest), "completed")
            with self.assertRaises(ValueError):
                prepare_undo(arguments, owner=self.context.owner)
            with self.assertRaises(ValueError):
                prepare_undo(fresh_receipt, owner=self.context.owner)

    def test_explicit_organize_admission_rejection_keeps_receipt_available(self):
        from unittest.mock import Mock, patch

        from app.modules.organize_tasks import OrganizeTaskManager
        from app.repositories import agent_compensations as receipts

        public = {"title": "示例", "version": 1, "changes": [], "affected": 1}

        for rejection in ("shutdown", "thread_start_failed"):
            with self.subTest(rejection=rejection):
                context = SimpleNamespace(owner=self.context.owner + rejection, session_id=rejection)
                manager, client = OrganizeTaskManager(), Mock()
                self.addCleanup(manager.shutdown, timeout=2)
                if rejection == "shutdown":
                    manager.begin_shutdown()
                thread = Mock()
                thread.start.side_effect = RuntimeError("cannot start")
                with (
                    patch("app.modules.activity_undo._organize_state", return_value=(public, "version-one")),
                    patch("app.modules.organize_correction.OrganizeCorrectionService", return_value=client),
                    patch("app.modules.organize_tasks.get_organize_manager", return_value=manager),
                    patch("app.modules.organize_tasks.threading.Thread", return_value=thread),
                ):
                    receipt = inspect_undo(1, owner=context.owner)["data"]["receipt_id"]
                    arguments = receipt
                    expected = prepare_undo(arguments, owner=context.owner)["fingerprint"]
                    result = execute_undo(arguments, expected_fingerprint=expected, owner=context.owner)
                    self.assertEqual(result["status"], "busy")
                    self.assertEqual(receipts.state(receipt, media_owner_digest(context.owner)), "available")
                    prepare_undo(arguments, owner=context.owner)
                client.revert_latest.assert_not_called()
                client.close.assert_called_once()

    def test_unknown_admission_exception_does_not_release_receipt(self):
        from unittest.mock import Mock, patch

        from app.repositories import agent_compensations as receipts

        public, arguments, expected = self.organize_receipt()
        manager = Mock()
        manager.start_operation.side_effect = RuntimeError("admission response lost")
        with (
            patch("app.modules.activity_undo._organize_state", return_value=(public, "version-one")),
            patch("app.modules.organize_correction.OrganizeCorrectionService"),
            patch("app.modules.organize_tasks.get_organize_manager", return_value=manager),
            self.assertRaises(RuntimeError),
        ):
            execute_undo(arguments, expected_fingerprint=expected, owner=self.context.owner)
        self.assertEqual(receipts.state("organize-receipt", media_owner_digest(self.context.owner)), "outcome_unknown")
        with self.assertRaises(ValueError):
            prepare_undo(arguments, owner=self.context.owner)

    def test_accepted_callback_failure_stays_closed_to_retries(self):
        from unittest.mock import Mock, patch

        from app.repositories import agent_compensations as receipts

        public = {"title": "示例", "version": 1, "changes": [], "affected": 1}

        for raises in (False, True):
            with self.subTest(raises=raises):
                context = SimpleNamespace(owner=self.context.owner + str(raises), session_id=f"failed-{raises}")
                manager, client, callbacks = Mock(), Mock(), []
                client.revert_latest.return_value = {"success": False}
                if raises:
                    client.revert_latest.side_effect = OSError("write outcome unknown")
                manager.start_operation.side_effect = lambda _operation, _title, callback: (
                    callbacks.append(callback) or {"ok": True, "task_id": "private-task"}
                )
                with (
                    patch("app.modules.activity_undo._organize_state", return_value=(public, "version-one")),
                    patch("app.modules.organize_correction.OrganizeCorrectionService", return_value=client),
                    patch("app.modules.organize_tasks.get_organize_manager", return_value=manager),
                ):
                    receipt = inspect_undo(1, owner=context.owner)["data"]["receipt_id"]
                    arguments = receipt
                    expected = prepare_undo(arguments, owner=context.owner)["fingerprint"]
                    self.assertEqual(execute_undo(arguments, expected_fingerprint=expected, owner=context.owner)["status"], "accepted")
                    if raises:
                        with self.assertRaises(OSError):
                            callbacks[0]()
                    else:
                        callbacks[0]()
                    self.assertEqual(receipts.state(receipt, media_owner_digest(context.owner)), "outcome_unknown")
                    with self.assertRaises(ValueError):
                        prepare_undo(arguments, owner=context.owner)
                client.revert_latest.assert_called_once()
                client.close.assert_called_once()

    def test_unaccepted_release_cannot_reopen_settled_or_other_owner_receipts(self):
        from app.repositories import agent_compensations as receipts

        digest = media_owner_digest(self.context.owner)
        for completed in (False, True):
            key = f"settled-{completed}"
            receipts.create(key, digest, payload={"kind": "organize", "expected": key})
            self.assertTrue(receipts.claim(key, digest))
            self.assertFalse(receipts.claim(key, digest))
            self.assertFalse(receipts.release_unaccepted(key, "other-owner"))
            self.assertEqual(receipts.state(key, digest), "executing")
            receipts.finish(key, digest, completed=completed)
            self.assertFalse(receipts.release_unaccepted(key, digest))
            receipts.create(key, digest, payload={"kind": "organize", "expected": key})
            self.assertFalse(receipts.claim(key, digest))
