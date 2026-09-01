"""下载完成后自动媒体库复核的持久化状态机测试。"""
from __future__ import annotations

from datetime import datetime
import os
import threading
import unittest
from unittest.mock import Mock, patch

from app import database as db
from app.agent.models import ToolResult
from app.agent.feature_gate import invalidate_agent_runtime_generation
from app.agent.recent_download_submissions import (
    RecentDownloadSubmission,
    RecentDownloadVerification,
    build_recent_download_status,
    enqueue_recent_download_library_verification,
)
from app.modules.agent_download_verification_scheduler import (
    DownloadLibraryVerificationScheduler,
)
from app.notifier import TelegramSendResult
from tests.support import IsolatedDatabaseTestCase


_CONTEXT = {
    "title": "The Show",
    "tmdb_id": "12345",
    "season": 2,
    "episode": 3,
    "as_of": "2026-08-03",
    "library_name": "动漫库",
}

_TG_OWNER = "tg:v1:100\x1f200"
_TG_CHAT_ID = "100"


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _submission_result(request_id: int, *, created: bool = True) -> ToolResult:
    return ToolResult(True, "accepted", "submitted", data={
        "request_id": request_id,
        "target": "qb",
        "status": "submitted",
        "succeeded": ["qb"],
        "failed": [],
        "created": created,
        "duplicate": False,
        "magnet": "magnet:?xt=urn:btih:must-not-persist",
        "path": "/volume/private/must-not-persist",
        "session_token": "must-not-persist",
    })


def _audit_missing() -> ToolResult:
    return ToolResult(True, "updates_available", "missing", data={
        "missing_count": 1,
        "missing_sample": [{"season": 2, "episode": 3}],
        "missing_sample_truncated": False,
    })


def _audit_visible() -> ToolResult:
    return ToolResult(True, "up_to_date", "visible", data={"missing_count": 0})


class DownloadLibraryVerificationDatabaseTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_maintenance")
            conn.execute("DELETE FROM agent_download_verification_notification_outbox")
            conn.execute("DELETE FROM agent_download_verifications")
            conn.execute("DELETE FROM download_requests")

    def _request(self, key: str = "auto-verify", *, completed: bool = False) -> int:
        request_id, _ = db.create_download_request(
            key,
            "magnet",
            title="The Show S02E03",
            origin="agent",
        )
        db.update_download_request(
            request_id,
            targets="qb",
            status="completed" if completed else "downloading",
            qb_status="completed" if completed else "downloading",
        )
        return request_id

    def test_confirmation_enqueue_is_strict_unique_and_safe(self):
        request_id = self._request()
        self.assertTrue(enqueue_recent_download_library_verification(
            _submission_result(request_id), _CONTEXT
        ))
        self.assertFalse(enqueue_recent_download_library_verification(
            _submission_result(request_id), _CONTEXT
        ))
        row = db.get_agent_download_verification(request_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["tmdb_id"], "12345")
        self.assertEqual((row["season"], row["episode"]), (2, 3))
        self.assertEqual(row["library_name"], "动漫库")
        serialized = repr(dict(row))
        for secret in ("magnet:", "torrent", "/volume", "session"):
            self.assertNotIn(secret, serialized)

        other_id = self._request("invalid-auto-verify")
        self.assertFalse(enqueue_recent_download_library_verification(
            _submission_result(other_id, created=False), _CONTEXT
        ))
        self.assertFalse(enqueue_recent_download_library_verification(
            _submission_result(other_id), {**_CONTEXT, "as_of": "9999-12-31"}
        ))
        self.assertIsNone(db.get_agent_download_verification(other_id))

    def test_namespaced_agent_indexer_origin_can_enqueue(self):
        request_id, _ = db.create_download_request(
            "agent-indexer-origin",
            "magnet",
            title="The Show S02E03",
            origin="agent:nyaa",
        )
        db.update_download_request(
            request_id, targets="qb", status="submitted", qb_status="submitted"
        )

        self.assertTrue(enqueue_recent_download_library_verification(
            _submission_result(request_id), _CONTEXT
        ))
        self.assertIsNotNone(db.get_agent_download_verification(request_id))

    def test_due_claim_is_atomic_and_terminal_rows_are_not_reclaimed(self):
        request_id = self._request()
        self.assertTrue(enqueue_recent_download_library_verification(
            _submission_result(request_id), _CONTEXT
        ))
        first = db.claim_due_agent_download_verification(
            current_time="9999-12-31 23:59:59"
        )
        second = db.claim_due_agent_download_verification(
            current_time="9999-12-31 23:59:59"
        )
        self.assertEqual(first["request_id"], request_id)
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["lease_generation"], 1)
        self.assertIsNone(second)
        self.assertTrue(db.update_agent_download_verification(
            request_id,
            status="visible",
            result="visible",
            attempts=1,
            next_check_at="9999-12-31 23:59:59",
            last_checked_at="9999-12-31 23:59:59",
            expected_lease_generation=first["lease_generation"],
        ))
        self.assertFalse(db.update_agent_download_verification(
            request_id,
            status="attention",
            result="inconclusive",
            attempts=2,
            next_check_at="9999-12-31 23:59:59",
            last_checked_at="9999-12-31 23:59:59",
            expected_lease_generation=first["lease_generation"],
        ))
        self.assertIsNone(db.claim_due_agent_download_verification(
            current_time="9999-12-31 23:59:59"
        ))
        self.assertEqual(
            db.get_agent_download_verification(request_id)["title"],
            "",
        )

    def test_running_lease_can_only_be_renewed_by_current_generation(self):
        request_id = self._request("renew-auto-verify")
        self.assertTrue(enqueue_recent_download_library_verification(
            _submission_result(request_id), _CONTEXT
        ))
        claimed = db.claim_due_agent_download_verification(
            current_time="9999-12-31 23:59:59"
        )
        self.assertTrue(db.renew_agent_download_verification_lease(
            request_id,
            expected_lease_generation=claimed["lease_generation"],
            renewed_at="9999-12-31 23:59:58",
        ))
        self.assertEqual(
            db.get_agent_download_verification(request_id)["updated_at"],
            "9999-12-31 23:59:58",
        )
        self.assertFalse(db.renew_agent_download_verification_lease(
            request_id,
            expected_lease_generation=claimed["lease_generation"] + 1,
        ))

    def test_retention_purges_only_expired_terminal_verifications(self):
        request_ids = {
            name: self._request(f"retention-{name}")
            for name in (
                "old-visible", "old-attention", "boundary-visible",
                "new-attention", "pending", "running", "retry-wait",
            )
        }
        for request_id in request_ids.values():
            self.assertTrue(enqueue_recent_download_library_verification(
                _submission_result(request_id), _CONTEXT
            ))
        states = {
            "old-visible": ("visible", "2026-07-26 11:59:59"),
            "old-attention": ("attention", "2026-07-20 12:00:00"),
            "boundary-visible": ("visible", "2026-07-27 12:00:00"),
            "new-attention": ("attention", "2026-08-02 12:00:00"),
            "pending": ("pending", "2026-07-01 12:00:00"),
            "running": ("running", "2026-07-01 12:00:00"),
            "retry-wait": ("retry_wait", "2026-07-01 12:00:00"),
        }
        with db.get_conn() as conn:
            for name, (status, updated_at) in states.items():
                conn.execute(
                    "UPDATE agent_download_verifications SET status=?,updated_at=? "
                    "WHERE request_id=?",
                    (status, updated_at, request_ids[name]),
                )

        deleted = db.purge_expired_agent_task_history(
            current_time="2026-08-03 12:00:00",
            next_cleanup_at="2026-08-04 12:00:00",
            terminal_before="2026-07-27 12:00:00",
            limit_per_table=50,
        )

        self.assertTrue(deleted["performed"])
        self.assertEqual(deleted["download_verifications"], 2)
        self.assertEqual(deleted["patrol_notification_outbox"], 0)
        self.assertIsNone(db.get_agent_download_verification(request_ids["old-visible"]))
        self.assertIsNone(db.get_agent_download_verification(request_ids["old-attention"]))
        for name in (
            "boundary-visible", "new-attention", "pending", "running", "retry-wait",
        ):
            self.assertIsNotNone(
                db.get_agent_download_verification(request_ids[name]),
                name,
            )

        skipped = db.purge_expired_agent_task_history(
            current_time="2026-08-03 12:00:01",
            next_cleanup_at="2026-08-04 12:00:01",
            terminal_before="2026-07-27 12:00:01",
        )
        self.assertFalse(skipped["performed"])
        self.assertEqual(skipped["next_cleanup_at"], "2026-08-04 12:00:00")

    def test_retention_respects_per_table_batch_limit(self):
        request_ids = [self._request(f"retention-limit-{index}") for index in range(2)]
        for request_id in request_ids:
            self.assertTrue(enqueue_recent_download_library_verification(
                _submission_result(request_id), _CONTEXT
            ))
        with db.get_conn() as conn:
            conn.executemany(
                "UPDATE agent_download_verifications SET status='visible',updated_at=? "
                "WHERE request_id=?",
                [("2026-07-20 12:00:00", request_id) for request_id in request_ids],
            )

        deleted = db.purge_expired_agent_task_history(
            current_time="2026-08-03 12:00:00",
            next_cleanup_at="2026-08-04 12:00:00",
            terminal_before="2026-07-27 12:00:00",
            limit_per_table=1,
        )

        self.assertEqual(deleted["download_verifications"], 1)
        remaining = sum(
            db.get_agent_download_verification(request_id) is not None
            for request_id in request_ids
        )
        self.assertEqual(remaining, 1)

    def test_enqueue_rejects_non_agent_pending_and_failed_submission(self):
        pending_id, _ = db.create_download_request(
            "pending-auto-verify", "magnet", origin="agent"
        )
        self.assertFalse(enqueue_recent_download_library_verification(
            _submission_result(pending_id), _CONTEXT
        ))

        foreign_id, _ = db.create_download_request(
            "foreign-auto-verify", "magnet", origin="telegram"
        )
        db.update_download_request(
            foreign_id, targets="qb", status="submitted", qb_status="submitted"
        )
        self.assertFalse(enqueue_recent_download_library_verification(
            _submission_result(foreign_id), _CONTEXT
        ))

        failed = _submission_result(pending_id)
        failed.data["succeeded"] = []
        failed.data["failed"] = ["qb"]
        failed.data["status"] = "failed"
        self.assertFalse(enqueue_recent_download_library_verification(
            failed, _CONTEXT
        ))

    def test_init_recovers_interrupted_running_job(self):
        request_id = self._request()
        enqueue_recent_download_library_verification(
            _submission_result(request_id), _CONTEXT
        )
        claimed = db.claim_due_agent_download_verification(
            current_time="9999-12-31 23:59:59"
        )
        self.assertEqual(claimed["lease_generation"], 1)
        self.assertEqual(db.get_agent_download_verification(request_id)["status"], "running")

        db.init_db()

        recovered = db.get_agent_download_verification(request_id)
        self.assertEqual(recovered["status"], "retry_wait")
        self.assertEqual(recovered["lease_generation"], 2)
        self.assertTrue(recovered["next_check_at"])
class DownloadLibraryVerificationSchedulerTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        route_env = patch.dict(
            os.environ,
            {
                "AGENT_ENABLED": "1",
                "TG_AGENT_ENABLED": "1",
                "TG_CHAT_ID": _TG_CHAT_ID,
                "TG_AGENT_ALLOWED_USER_IDS": "200",
            },
            clear=False,
        )
        route_env.start()
        self.addCleanup(route_env.stop)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_maintenance")
            conn.execute("DELETE FROM agent_download_verification_notification_outbox")
            conn.execute("DELETE FROM agent_download_verifications")
            conn.execute("DELETE FROM download_requests")
        self.clock = MutableClock(datetime(2026, 8, 3, 12, 0, 0))

    def test_revoked_current_route_discards_outbox_without_delivery(self):
        for key, value in (
            ("TG_CHAT_ID", "999"),
            ("TG_AGENT_ALLOWED_USER_IDS", "201"),
            ("TG_AGENT_ENABLED", "0"),
        ):
            with self.subTest(key=key):
                with db.get_conn() as conn:
                    conn.execute(
                        "DELETE FROM agent_download_verification_notification_outbox"
                    )
                    conn.execute("DELETE FROM agent_download_verifications")
                    conn.execute("DELETE FROM download_requests")
                self.clock.value = datetime(2026, 8, 3, 12, 0, 0)
                request_id = self._request(f"revoked-{key}", owner=_TG_OWNER)
                notifier = Mock(return_value=True)
                scheduler = DownloadLibraryVerificationScheduler(
                    audit_executor=Mock(return_value=(_audit_visible(), 1)),
                    terminal_notifier=notifier,
                    clock=self.clock,
                )
                self.assertEqual(scheduler.run_once(), 1)
                self.clock.value = datetime(2026, 8, 3, 12, 0, 31)
                with patch.dict(os.environ, {key: value}, clear=False):
                    self.assertEqual(scheduler.run_once(), 1)

                queued = db.list_agent_download_verification_notifications()
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0]["status"], "discarded")
                self.assertEqual(
                    queued[0]["last_error_type"],
                    "AuthorizationRevoked",
                )
                self.assertEqual(queued[0]["payload_json"], "")
                self.assertEqual(
                    db.get_agent_download_verification(request_id)["status"],
                    "visible",
                )
                notifier.assert_not_called()

    def _request(
        self, key: str, *, phase: str = "completed", owner: str = ""
    ) -> int:
        request_id, _ = db.create_download_request(key, "magnet", origin="agent")
        fields = {"targets": "qb"}
        if phase == "completed":
            fields.update(status="completed", qb_status="completed")
        elif phase == "downloading":
            fields.update(status="downloading", qb_status="downloading")
        elif phase == "failed":
            fields.update(status="failed", qb_status="failed")
        db.update_download_request(request_id, **fields)
        self.assertTrue(enqueue_recent_download_library_verification(
            _submission_result(request_id), _CONTEXT, owner
        ))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_download_verifications SET status='pending',result='',"
                "attempts=0,next_check_at=?,last_checked_at=NULL WHERE request_id=?",
                (self.clock().strftime("%Y-%m-%d %H:%M:%S"), request_id),
            )
        return request_id

    def test_long_audit_heartbeat_renews_current_lease(self):
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            clock=self.clock,
        )
        renewed = threading.Event()

        def renew(*_args, **_kwargs):
            renewed.set()
            return True

        with patch(
            "app.modules.agent_download_verification_scheduler._VERIFICATION_LEASE_HEARTBEAT_SECONDS",
            0.01,
        ), patch.object(
            db, "renew_agent_download_verification_lease", side_effect=renew
        ) as renew_mock:
            with scheduler._lease_heartbeat(123, 7):
                self.assertTrue(renewed.wait(1.0))

        renew_mock.assert_called_with(123, expected_lease_generation=7)

    def test_active_download_is_rescheduled_without_audit_attempt(self):
        request_id = self._request("active-auto-verify", phase="downloading")
        executor = Mock()
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=executor,
            clock=self.clock,
            interval=30,
        )

        self.assertEqual(scheduler.run_once(), 1)

        row = db.get_agent_download_verification(request_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["next_check_at"], "2026-08-03 12:00:30")
        executor.assert_not_called()

    def test_completed_download_becomes_visible_after_exact_audit(self):
        request_id = self._request("visible-auto-verify", owner=_TG_OWNER)
        executor = Mock(return_value=(_audit_visible(), 3))
        notifier = Mock(return_value=True)
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=executor,
            clock=self.clock,
            terminal_notifier=notifier,
        )
        with patch(
            "app.modules.agent_download_verification_scheduler.invalidate_episode_audit_cache"
        ) as invalidator:
            self.assertEqual(scheduler.run_once(), 1)
            executor.assert_not_called()
            self.clock.value = datetime(2026, 8, 3, 12, 0, 31)
            self.assertEqual(scheduler.run_once(), 1)

        expected = {
            "query": "The Show",
            "tmdb_id": "12345",
            "season": 2,
            "target_episode": 3,
            "as_of": "2026-08-03",
            "library_name": "动漫库",
        }
        invalidator.assert_called_once_with(expected)
        executor.assert_called_once_with(expected)
        row = db.get_agent_download_verification(request_id)
        self.assertEqual(row["status"], "visible")
        self.assertEqual(row["result"], "visible")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["title"], "")
        self.assertEqual(row["last_checked_at"], "2026-08-03 12:00:31")
        notifier.assert_called_once_with(
            owner=_TG_OWNER,
            chat_id=_TG_CHAT_ID,
            title="The Show",
            season=2,
            episode=3,
            status="visible",
            result="visible",
            attempts=1,
        )
        self.clock.value = datetime(2026, 8, 3, 12, 1, 0)
        self.assertEqual(scheduler.run_once(), 0)
        self.assertEqual(notifier.call_count, 1)

    def test_runtime_invalidation_stops_remaining_verification_batch(self):
        first_id = self._request("generation-batch-first")
        second_id = self._request("generation-batch-second")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_download_verifications SET last_checked_at=?",
                ("2026-08-03 11:59:00",),
            )

        def audit_then_disable(_arguments):
            invalidate_agent_runtime_generation()
            return _audit_visible(), 1

        executor = Mock(side_effect=audit_then_disable)
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=executor,
            clock=self.clock,
        )

        self.assertEqual(scheduler.run_once(limit=2), 1)

        first = db.get_agent_download_verification(first_id)
        second = db.get_agent_download_verification(second_id)
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["attempts"], 0)
        self.assertEqual(second["status"], "pending")
        self.assertEqual(second["lease_generation"], 0)
        executor.assert_called_once()

    def test_stop_during_audit_releases_job_without_terminal_notification(self):
        request_id = self._request("stop-during-audit", owner=_TG_OWNER)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_download_verifications SET last_checked_at=? WHERE request_id=?",
                ("2026-08-03 11:59:00", request_id),
            )
        notifier = Mock(return_value=True)
        scheduler = DownloadLibraryVerificationScheduler(
            terminal_notifier=notifier,
            clock=self.clock,
        )

        def audit_then_stop(_arguments):
            scheduler.stop(timeout=0)
            return _audit_visible(), 1

        scheduler._audit_executor = Mock(side_effect=audit_then_stop)

        self.assertEqual(scheduler.run_once(), 1)

        row = db.get_agent_download_verification(request_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(db.list_agent_download_verification_notifications(), [])
        notifier.assert_not_called()

    def test_stop_during_failed_notification_releases_without_retry_budget(self):
        self._request("stop-during-notify", owner=_TG_OWNER)
        job = db.claim_due_agent_download_verification(
            current_time="2026-08-03 12:00:00"
        )
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            clock=self.clock,
        )
        scheduler._finish(
            job,
            status="attention",
            result="missing",
            attempts=1,
            current="2026-08-03 12:00:00",
        )

        def notify_then_stop(**_payload):
            scheduler.stop(timeout=0)
            return False

        scheduler._terminal_notifier = Mock(side_effect=notify_then_stop)
        scheduler._notification_enabled_override = True
        with patch(
            "app.modules.agent_download_verification_scheduler."
            "telegram_owner_route_is_currently_authorized",
            return_value=True,
        ):
            self.assertEqual(scheduler.dispatch_notification_once(), 1)

        item = db.list_agent_download_verification_notifications()[0]
        self.assertEqual(item["status"], "retry_wait")
        self.assertEqual(item["attempts"], 0)
        self.assertEqual(item["last_error_type"], "")

    def test_missing_retries_then_visible(self):
        request_id = self._request("retry-auto-verify")
        executor = Mock(side_effect=[(_audit_missing(), 2), (_audit_visible(), 2)])
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=executor,
            clock=self.clock,
        )

        self.assertEqual(scheduler.run_once(), 1)
        executor.assert_not_called()
        self.clock.value = datetime(2026, 8, 3, 12, 0, 31)
        self.assertEqual(scheduler.run_once(), 1)
        first = db.get_agent_download_verification(request_id)
        self.assertEqual(first["status"], "retry_wait")
        self.assertEqual(first["result"], "missing")
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["title"], "The Show")
        self.assertEqual(first["next_check_at"], "2026-08-03 12:01:31")

        self.clock.value = datetime(2026, 8, 3, 12, 1, 32)
        self.assertEqual(scheduler.run_once(), 1)
        final = db.get_agent_download_verification(request_id)
        self.assertEqual(final["status"], "visible")
        self.assertEqual(final["attempts"], 2)

    def test_retry_budget_and_failed_download_end_in_attention(self):
        retry_id = self._request("attention-auto-verify")
        executor = Mock(return_value=(
            ToolResult(False, "inconclusive", "unavailable"), 2
        ))
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=executor,
            clock=self.clock,
            max_attempts=1,
        )
        scheduler.run_once()
        executor.assert_not_called()
        self.clock.value = datetime(2026, 8, 3, 12, 0, 31)
        scheduler.run_once()
        retry = db.get_agent_download_verification(retry_id)
        self.assertEqual(retry["status"], "attention")
        self.assertEqual(retry["result"], "inconclusive")
        self.assertEqual(retry["attempts"], 1)

        failed_id = self._request("failed-auto-verify", phase="failed")
        scheduler.run_once()
        failed = db.get_agent_download_verification(failed_id)
        self.assertEqual(failed["status"], "attention")
        self.assertEqual(failed["attempts"], 0)
        self.assertEqual(executor.call_count, 1)

    def test_terminal_notification_failure_does_not_rollback_or_leak(self):
        request_id = self._request("notify-failure-auto-verify", owner=_TG_OWNER)
        notifier = Mock(side_effect=RuntimeError("SECRET-TOKEN"))
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(return_value=(_audit_visible(), 1)),
            terminal_notifier=notifier,
            clock=self.clock,
        )
        scheduler.run_once()
        self.clock.value = datetime(2026, 8, 3, 12, 0, 31)

        with self.assertLogs(
            "app.modules.agent_download_verification_scheduler", level="WARNING"
        ) as captured:
            self.assertEqual(scheduler.run_once(), 1)

        row = db.get_agent_download_verification(request_id)
        self.assertEqual(row["status"], "visible")
        self.assertEqual(notifier.call_count, 1)
        queued = db.list_agent_download_verification_notifications()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["status"], "retry_wait")
        self.assertEqual(queued[0]["last_error_type"], "RuntimeError")
        self.assertNotIn("SECRET-TOKEN", "\n".join(captured.output))

    def test_terminal_notification_false_does_not_rollback(self):
        request_id = self._request("notify-false-auto-verify", owner=_TG_OWNER)
        notifier = Mock(return_value=False)
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(return_value=(_audit_visible(), 1)),
            terminal_notifier=notifier,
            clock=self.clock,
        )
        scheduler.run_once()
        self.clock.value = datetime(2026, 8, 3, 12, 0, 31)

        self.assertEqual(scheduler.run_once(), 1)

        row = db.get_agent_download_verification(request_id)
        self.assertEqual(row["status"], "visible")
        self.assertEqual(row["result"], "visible")
        self.assertEqual(row["attempts"], 1)
        notifier.assert_called_once()
        queued = db.list_agent_download_verification_notifications()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["status"], "retry_wait")
        self.assertEqual(queued[0]["attempts"], 1)
        notifier.return_value = True
        self.clock.value = datetime(2026, 8, 3, 12, 1, 32)
        self.assertEqual(scheduler.run_once(), 0)
        self.assertEqual(notifier.call_count, 2)
        self.assertEqual(
            db.list_agent_download_verification_notifications()[0]["status"],
            "sent",
        )

    def test_disabled_notifications_discard_persisted_outbox_without_delivery(self):
        request_id = self._request("notify-disabled-auto-verify")
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(return_value=(_audit_visible(), 1)),
            clock=self.clock,
        )
        scheduler.run_once()
        self.clock.value = datetime(2026, 8, 3, 12, 0, 31)
        with patch("app.config.get_bool", return_value=False), patch(
            "app.modules.agent_download_verification_scheduler.notify_download_verification_terminal_result"
        ) as notifier:
            self.assertEqual(scheduler.run_once(), 1)

        self.assertEqual(db.get_agent_download_verification(request_id)["status"], "visible")
        queued = db.list_agent_download_verification_notifications()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["status"], "discarded")
        self.assertEqual(queued[0]["payload_json"], "")
        notifier.assert_not_called()

    def test_web_owner_terminal_notification_is_discarded_without_delivery(self):
        request_id = self._request(
            "web-owner-notify-auto-verify",
            owner="web:v1:0123456789abcdef",
        )
        notifier = Mock(return_value=True)
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(return_value=(_audit_visible(), 1)),
            terminal_notifier=notifier,
            clock=self.clock,
        )
        scheduler.run_once()
        self.clock.value = datetime(2026, 8, 3, 12, 0, 31)

        self.assertEqual(scheduler.run_once(), 1)

        verification = db.get_agent_download_verification(request_id)
        self.assertEqual(verification["status"], "visible")
        queued = db.list_agent_download_verification_notifications()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["owner"], "web:v1:0123456789abcdef")
        self.assertEqual(queued[0]["chat_id"], "")
        self.assertEqual(queued[0]["status"], "discarded")
        self.assertEqual(queued[0]["payload_json"], "")
        self.assertEqual(queued[0]["last_error_type"], "InvalidRoute")
        notifier.assert_not_called()
    def test_stale_sending_notification_is_discarded_after_unknown_delivery(self):
        self._request("notify-restart-auto-verify", owner=_TG_OWNER)
        job = db.claim_due_agent_download_verification(
            current_time="2026-08-03 12:00:00"
        )
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            terminal_notifier=Mock(return_value=True),
            clock=self.clock,
        )
        scheduler._finish(
            job,
            status="attention",
            result="missing",
            attempts=2,
            current="2026-08-03 12:00:00",
        )
        claimed = db.claim_due_agent_download_verification_notification(
            current_time="2026-08-03 12:00:00"
        )
        self.assertEqual(claimed["status"], "sending")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_download_verification_notification_outbox "
                "SET updated_at=? WHERE id=?",
                ("2026-08-03 11:50:00", int(claimed["id"])),
            )

        self.clock.value = datetime(2026, 8, 3, 12, 6, 0)
        self.assertEqual(scheduler.dispatch_notification_once(), 0)
        scheduler._terminal_notifier.assert_not_called()
        discarded = db.list_agent_download_verification_notifications()[0]
        self.assertEqual(discarded["status"], "discarded")
        self.assertEqual(discarded["payload_json"], "")
        self.assertEqual(discarded["last_error_type"], "DeliveryOutcomeUnknown")
        self.assertGreater(
            int(discarded["lease_generation"]), int(claimed["lease_generation"])
        )

    def test_delivery_outcome_unknown_is_discarded_without_duplicate_retry(self):
        self._request("notify-unknown-auto-verify", owner=_TG_OWNER)
        job = db.claim_due_agent_download_verification(
            current_time="2026-08-03 12:00:00"
        )
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            terminal_notifier=Mock(return_value=TelegramSendResult(
                ok=False, error="timeout", status_code=0,
            )),
            clock=self.clock,
        )
        scheduler._finish(
            job,
            status="attention",
            result="missing",
            attempts=2,
            current="2026-08-03 12:00:00",
        )

        self.assertEqual(scheduler.dispatch_notification_once(), 1)
        notification = db.list_agent_download_verification_notifications()[0]
        self.assertEqual(notification["status"], "discarded")
        self.assertEqual(notification["attempts"], 0)
        self.assertEqual(notification["payload_json"], "")
        self.assertEqual(notification["last_error_type"], "DeliveryOutcomeUnknown")

    def test_reload_discards_notification_backlog_when_notifications_disabled(self):
        self._request("notify-disabled-reload", owner=_TG_OWNER)
        job = db.claim_due_agent_download_verification(
            current_time="2026-08-03 12:00:00"
        )
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(), terminal_notifier=Mock(), clock=self.clock,
        )
        scheduler._finish(
            job, status="attention", result="missing", attempts=2,
            current="2026-08-03 12:00:00",
        )

        scheduler._notification_enabled_override = False
        with patch("app.config.get_bool", return_value=False):
            scheduler.reload()

        notification = db.list_agent_download_verification_notifications()[0]
        self.assertEqual(notification["status"], "discarded")
        self.assertEqual(notification["payload_json"], "")

    def test_pre_audit_exception_is_rescheduled_and_secret_is_not_logged(self):
        request_id = self._request("exception-auto-verify", phase="downloading")
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            clock=self.clock,
            interval=30,
        )
        with patch(
            "app.modules.agent_download_verification_scheduler.build_recent_download_status",
            side_effect=RuntimeError("SECRET-TOKEN"),
        ), self.assertLogs(
            "app.modules.agent_download_verification_scheduler", level="WARNING"
        ) as captured:
            self.assertEqual(scheduler.run_once(), 1)

        row = db.get_agent_download_verification(request_id)
        self.assertEqual(row["status"], "retry_wait")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["next_check_at"], "2026-08-03 12:00:30")
        self.assertNotIn("SECRET-TOKEN", "\n".join(captured.output))

    def test_stale_running_job_is_reclaimed_without_restart(self):
        request_id = self._request("stale-auto-verify", phase="downloading")
        claimed = db.claim_due_agent_download_verification(
            current_time="2026-08-03 12:00:00"
        )
        self.assertTrue(db.update_agent_download_verification(
            request_id,
            status="running",
            result="",
            attempts=1,
            next_check_at="2026-08-03 11:00:00",
            last_checked_at="2026-08-03 11:00:00",
            expected_lease_generation=claimed["lease_generation"],
        ))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_download_verifications SET updated_at=? WHERE request_id=?",
                ("2026-08-03 11:00:00", request_id),
            )
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            clock=self.clock,
            interval=30,
        )

        self.assertEqual(scheduler.run_once(), 1)
        row = db.get_agent_download_verification(request_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["lease_generation"], 2)

    def test_expired_generation_cannot_finish_or_notify_after_reclaim(self):
        request_id = self._request("lease-generation-auto-verify", owner=_TG_OWNER)
        old_job = db.claim_due_agent_download_verification(
            current_time="2026-08-03 12:00:00"
        )
        self.assertEqual(old_job["lease_generation"], 1)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_download_verifications SET updated_at=? WHERE request_id=?",
                ("2026-08-03 11:00:00", request_id),
            )
        new_job = db.claim_due_agent_download_verification(
            current_time="2026-08-03 12:31:00",
            stale_before="2026-08-03 12:01:00",
        )
        self.assertEqual(new_job["lease_generation"], 2)
        notifier = Mock(return_value=True)
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            terminal_notifier=notifier,
            clock=self.clock,
        )

        scheduler._finish(
            old_job,
            status="visible",
            result="visible",
            attempts=1,
            current="2026-08-03 12:31:00",
        )
        notifier.assert_not_called()
        self.assertEqual(
            db.get_agent_download_verification(request_id)["status"],
            "running",
        )

        scheduler._finish(
            new_job,
            status="attention",
            result="inconclusive",
            attempts=2,
            current="2026-08-03 12:31:00",
        )

        self.clock.value = datetime(2026, 8, 3, 12, 31, 0)
        self.assertEqual(scheduler.dispatch_notification_once(), 1)
        row = db.get_agent_download_verification(request_id)
        self.assertEqual(row["status"], "attention")
        self.assertEqual(row["result"], "inconclusive")
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["title"], "")
        self.assertEqual(row["lease_generation"], 2)
        notifier.assert_called_once_with(
            owner=_TG_OWNER,
            chat_id=_TG_CHAT_ID,
            title="The Show",
            season=2,
            episode=3,
            status="attention",
            result="inconclusive",
            attempts=2,
        )
        scheduler._finish(
            new_job,
            status="attention",
            result="inconclusive",
            attempts=2,
            current="2026-08-03 12:31:00",
        )
        self.assertEqual(notifier.call_count, 1)

    def test_history_cleanup_runs_on_start_and_at_most_daily(self):
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            clock=self.clock,
        )
        with patch(
            "app.modules.agent_download_verification_scheduler."
            "db.purge_expired_agent_task_history",
            return_value={
                "performed": True,
                "next_cleanup_at": "2026-08-04 12:00:00",
                "download_verifications": 0,
                "patrol_notification_outbox": 0,
            },
        ) as purge:
            self.assertEqual(scheduler.run_once(), 0)
            purge.assert_called_once_with(
                current_time="2026-08-03 12:00:00",
                next_cleanup_at="2026-08-04 12:00:00",
                terminal_before="2026-07-27 12:00:00",
                limit_per_table=500,
            )

            self.clock.value = datetime(2026, 8, 4, 11, 59, 59)
            self.assertEqual(scheduler.run_once(), 0)
            self.assertEqual(purge.call_count, 1)

            self.clock.value = datetime(2026, 8, 4, 12, 0, 0)
            self.assertEqual(scheduler.run_once(), 0)
            self.assertEqual(purge.call_count, 2)
            self.assertEqual(
                purge.call_args.kwargs["terminal_before"],
                "2026-07-28 12:00:00",
            )


    def test_private_plan_cleanup_failures_are_isolated(self):
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            clock=self.clock,
        )
        with (
            patch(
                "app.modules.guangya_rename.maintain_rename_plans",
                side_effect=RuntimeError("rename cleanup failed"),
            ) as rename_cleanup,
            patch(
                "app.modules.guangya_residual_cleanup.maintain_cleanup_plans",
                return_value={"removed": 1, "remaining": 0, "bytes": 0},
            ) as residual_cleanup,
            patch(
                "app.modules.guangya_workspace.maintain_workspace_observations",
                return_value={"removed": 1, "remaining": 0, "bytes": 0},
            ) as workspace_cleanup,
            patch(
                "app.modules.agent_download_verification_scheduler."
                "db.purge_expired_agent_task_history",
                return_value={
                    "performed": True,
                    "next_cleanup_at": "2026-08-04 12:00:00",
                },
            ) as purge,
        ):
            self.assertEqual(scheduler.run_once(), 0)
        rename_cleanup.assert_called_once_with()
        residual_cleanup.assert_called_once_with()
        workspace_cleanup.assert_called_once_with()
        purge.assert_called_once()

    def test_history_cleanup_failure_retries_after_five_minutes(self):
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            clock=self.clock,
        )
        with patch(
            "app.modules.agent_download_verification_scheduler."
            "db.purge_expired_agent_task_history",
            side_effect=RuntimeError("PRIVATE-DB-ERROR"),
        ) as purge:
            self.assertEqual(scheduler.run_once(), 0)
            self.clock.value = datetime(2026, 8, 3, 12, 4, 59)
            self.assertEqual(scheduler.run_once(), 0)
            self.assertEqual(purge.call_count, 1)
            self.clock.value = datetime(2026, 8, 3, 12, 5, 0)
            self.assertEqual(scheduler.run_once(), 0)
            self.assertEqual(purge.call_count, 2)

    def test_history_cleanup_gate_is_shared_across_scheduler_instances(self):
        first_request = self._request("cleanup-gate-first")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_download_verifications SET status='visible',updated_at=? "
                "WHERE request_id=?",
                ("2026-07-20 12:00:00", first_request),
            )
        first_scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            clock=self.clock,
        )
        self.assertEqual(first_scheduler.run_once(), 0)
        self.assertIsNone(db.get_agent_download_verification(first_request))

        second_request = self._request("cleanup-gate-second")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_download_verifications SET status='attention',updated_at=? "
                "WHERE request_id=?",
                ("2026-07-20 12:00:00", second_request),
            )
        second_scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            clock=self.clock,
        )
        self.assertEqual(second_scheduler.run_once(), 0)
        self.assertIsNotNone(db.get_agent_download_verification(second_request))

        self.clock.value = datetime(2026, 8, 4, 12, 0, 0)
        self.assertEqual(second_scheduler.run_once(), 0)
        self.assertIsNone(db.get_agent_download_verification(second_request))

    def test_status_projection_exposes_only_safe_automatic_state(self):
        request_id = self._request("projection-auto-verify")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_download_verifications SET status='retry_wait',"
                "result='missing',attempts=2,next_check_at=?,last_checked_at=? "
                "WHERE request_id=?",
                ("2026-08-03 12:05:00", "2026-08-03 12:00:00", request_id),
            )
        record = RecentDownloadSubmission(
            request_id=request_id,
            target="qb",
            dispatch_status="submitted",
            succeeded=("qb",),
            failed=(),
            created=True,
            duplicate=False,
            result_status="accepted",
            captured_at="2026-08-03T11:00:00+08:00",
            verification=RecentDownloadVerification(**_CONTEXT),
        )

        result = build_recent_download_status(record, position=1)

        projected = result.data["library_verification"]
        self.assertEqual(projected["status"], "retry_wait")
        self.assertEqual(projected["result"], "missing")
        self.assertEqual(projected["attempts"], 2)
        self.assertEqual((projected["season"], projected["episode"]), (2, 3))
        serialized = repr(projected)
        for secret in ("request_id", "magnet", "path", "error"):
            self.assertNotIn(secret, serialized)

    def test_start_stop_is_reentrant(self):
        scheduler = DownloadLibraryVerificationScheduler(
            audit_executor=Mock(),
            clock=self.clock,
            interval=0.1,
        )
        scheduler.start()
        first = scheduler._thread
        scheduler.start()
        try:
            self.assertIs(first, scheduler._thread)
            self.assertTrue(first.daemon)
        finally:
            scheduler.stop()
        self.assertFalse(first.is_alive())
        self.assertFalse(scheduler.status()["running"])


if __name__ == "__main__":
    unittest.main()
