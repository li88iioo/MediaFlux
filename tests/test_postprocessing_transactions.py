"""确认终态、回执和下载收尾意图必须同一事务提交；不执行云端动作。"""
from __future__ import annotations

import json
from unittest import mock

from app import database as db
from app.repositories import organize_confirmations as organize_confirmations_repo
from app.repositories import download_staging_reconcile as reconcile
from tests.support import IsolatedDatabaseTestCase


class PostprocessingTransactionTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM download_staging_reconcile")
            conn.execute("DELETE FROM organize_confirmation_delivery_outbox")
            conn.execute("DELETE FROM organize_confirmations")
            conn.execute("DELETE FROM download_requests")
        self.payload = {
            "source_dir_id": "synthetic-staging-57",
            "download_request_ids": [57],
            "organize_task_id": "synthetic-task-57",
            "rules": {"clean_empty": True},
        }
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO download_requests(id,request_key,kind,targets,status,gy_isolated,"
                "gy_status,gy_target_dir,gy_staging_parent_dir,gy_staging_name,"
                "gy_staging_cleanup_status,organize_started,organize_status,organize_task_id,"
                "created_at,updated_at) VALUES(57,'synthetic-request-57','magnet','guangya',"
                "'completed',1,'completed','synthetic-staging-57','synthetic-parent',"
                "'synthetic-staging-name','retained',1,'requires_manual','synthetic-task-57',"
                "'2026-09-06 16:00:00','2026-09-06 16:00:00')"
            )
            conn.execute(
                "INSERT INTO organize_confirmations(id,token,fingerprint,payload_json,status,"
                "expires_at,created_at,updated_at) VALUES(78,'synthetic-token-78',"
                "'synthetic-fingerprint-78',?,'running','2099-01-01 00:00:00',"
                "'2026-09-06 16:00:00','2026-09-06 16:00:00')",
                (json.dumps(self.payload),),
            )
        self.result = json.dumps({"moved": 1, "failed": 0, "need_confirm": 0, "scan_complete": True})

    def _complete(self, *, enqueue_delivery: bool = True) -> None:
        organize_confirmations_repo.complete_organize_confirmation_with_delivery(
            "synthetic-token-78", result_json=self.result,
            event_json='{"title":"synthetic completion"}',
            chat_id="synthetic-chat", message_id=None, enqueue_delivery=enqueue_delivery,
        )

    def _state(self) -> tuple[str, int, int]:
        with db.get_conn() as conn:
            status = conn.execute("SELECT status FROM organize_confirmations WHERE id=78").fetchone()[0]
            intents = conn.execute("SELECT COUNT(*) FROM download_staging_reconcile").fetchone()[0]
            deliveries = conn.execute("SELECT COUNT(*) FROM organize_confirmation_delivery_outbox").fetchone()[0]
        return status, intents, deliveries

    def test_startup_recovery_is_one_transaction_and_rolls_back_every_domain(self) -> None:
        from app.repositories import agent_provider_plans

        original = agent_provider_plans._recover_after_restart
        def interrupted(conn, timestamp):
            self.assertTrue(conn.in_transaction)
            original(conn, timestamp)
            raise RuntimeError("fixture after last recovery owner")
        with mock.patch.object(db, "get_conn", side_effect=AssertionError("nested connection")), mock.patch.object(
            agent_provider_plans, "_recover_after_restart", side_effect=interrupted
        ), self.assertRaisesRegex(RuntimeError, "last recovery owner"):
            db.init_db()
        self.assertEqual(self._state(), ("running", 0, 0))
        with mock.patch.object(db, "get_conn", side_effect=AssertionError("nested connection")):
            db.init_db()
        self.assertEqual(self._state(), ("failed", 0, 1))
        db.init_db()
        self.assertEqual(self._state(), ("failed", 0, 1))

    def test_silent_confirmation_still_persists_identity_and_cleanup_intent(self) -> None:
        with mock.patch.object(reconcile, "enqueue_confirmation_cleanup", wraps=reconcile.enqueue_confirmation_cleanup) as enqueue:
            self._complete(enqueue_delivery=False)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(self._state(), ("completed", 1, 0))
        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM download_staging_reconcile").fetchone()
            identity = json.loads(row["identity_json"])
            self.assertEqual((row["confirmation_id"], row["request_id"], row["status"]), (78, 57, "pending"))
            self.assertEqual(identity["gy_target_dir"], self.payload["source_dir_id"])
            self.assertEqual(identity["organize_task_id"], self.payload["organize_task_id"])
            self.assertEqual(identity["gy_staging_name"], "synthetic-staging-name")
            self.assertEqual(row["result_json"], self.result)
            self.assertEqual(conn.execute(
                "SELECT gy_staging_cleanup_status FROM download_requests WHERE id=57"
            ).fetchone()[0], "retained")

    def test_crash_after_intent_insert_rolls_back_confirmation_and_intent(self) -> None:
        original = reconcile.enqueue_confirmation_cleanup

        def interrupted(conn, **kwargs):
            self.assertTrue(conn.in_transaction)
            assert (original(conn, **kwargs)) is not None
            raise RuntimeError("synthetic interruption after intent")

        with (
            mock.patch.object(reconcile, "enqueue_confirmation_cleanup", side_effect=interrupted),
            self.assertRaisesRegex(RuntimeError, "after intent"),
        ):
            self._complete()
        self.assertEqual(self._state(), ("running", 0, 0))
        self._complete()
        self.assertEqual(self._state(), ("completed", 1, 1))

    def test_receipt_failure_does_not_commit_half_finished_confirmation(self) -> None:
        with (
            mock.patch(
                "app.repositories.telegram_notifications._enqueue_confirmation_delivery", side_effect=RuntimeError("synthetic receipt failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "receipt failure"),
        ):
            self._complete()
        self.assertEqual(self._state(), ("running", 0, 0))

    def test_enqueue_uses_the_callers_connection_without_nested_database_scope(self) -> None:
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE organize_confirmations SET status='completed',result_json=? WHERE id=78", (self.result,))
            with mock.patch.object(db, "get_conn", side_effect=AssertionError("nested connection forbidden")):
                first = reconcile.enqueue_confirmation_cleanup(conn, token="synthetic-token-78", timestamp=db.now())
                second = reconcile.enqueue_confirmation_cleanup(conn, token="synthetic-token-78", timestamp=db.now())
            assert (first) is not None
            self.assertEqual(first, second)
        self.assertEqual(self._state(), ("completed", 1, 0))

    def test_explicit_wrong_download_binding_does_not_downgrade_to_source_match(self) -> None:
        self.payload["download_request_ids"] = [999]
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE id=78", (json.dumps(self.payload),))
        self._complete(enqueue_delivery=False)
        self.assertEqual(self._state(), ("completed", 0, 0))

    def test_non_download_confirmation_does_not_create_cleanup_work(self) -> None:
        self.payload = {"source_dir_id": "not-a-download-source", "rules": {"clean_empty": True}}
        with db.get_conn() as conn:
            conn.execute("UPDATE organize_confirmations SET payload_json=? WHERE id=78", (json.dumps(self.payload),))
        self._complete()
        self.assertEqual(self._state(), ("completed", 0, 1))
