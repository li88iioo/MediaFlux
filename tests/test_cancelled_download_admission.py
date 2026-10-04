"""真实 pending 取消必须释放媒体准入，提交/未知结果仍保持防重。"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import tests  # noqa: F401 - 应用导入前隔离运行路径、配置与 SQLite
from app import database as db
from app.indexers import downloads
from app.modules import download_dispatcher as dispatcher
from app.modules import media_subscriptions as subscriptions
from app.repositories import media_subscriptions as repository
from tests.support import isolated_test_database


class CancelledDownloadAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.database_path = self.enterContext(isolated_test_database("cancelled-admission.db"))
        for target, name in (
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
            (socket, "getaddrinfo"),
        ):
            self.enterContext(patch.object(
                target, name, side_effect=AssertionError("unexpected external network")
            ))
        self.subscription = db.add_media_subscription(
            provider="tmdb", external_id="86034", tmdb_id="86034", media_type="tv",
            title="取消准入测试", monitor_mode="missing", action="confirm",
            download_target="guangya", check_interval_minutes=60,
        )
        self.key = "tmdb:86034:tv:S01E001"
        self.candidate = db.replace_media_subscription_candidates(
            self.subscription, self.key, season=1, episode=1,
            candidates=[{"result_id": "fixture", "title": "取消准入测试"}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        self.item = dispatcher.normalize_download_url("magnet:?xt=urn:btih:" + "e" * 40)

    def claim(self, candidate_id=None):
        return db.claim_media_download_admission(
            media_key=self.key, tmdb_id="86034", media_type="tv",
            subscription_id=self.subscription, candidate_id=candidate_id or self.candidate,
            season=1, episode=1, subscription_revision=1,
        )

    def assert_active_owner(self, admission_id):
        active = db.list_active_media_download_admissions(self.subscription)
        self.assertEqual([row["id"] for row in active], [admission_id])
        self.assertIsNone(self.admission(admission_id)["completed_at"])
        # 原候选已 submitted；用另一条 available 候选验证锁，而非被候选失效拦截。
        alternative = db.replace_media_subscription_candidates(
            self.subscription, self.key, season=1, episode=1,
            candidates=[{"result_id": "alternative", "title": "同集另一个候选"}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        self.assertIsNone(self.claim(alternative))

    def pending_request(self):
        return dispatcher.create_request(self.item, "r06-chat", "r06-message", user_id="r06-user")["id"]

    def bound_pending(self):
        admission = self.claim()
        self.assertIsNotNone(admission)
        self.assertTrue(db.begin_media_download_dispatch(
            admission, subscription_id=self.subscription, subscription_revision=1
        ))
        request = self.pending_request()
        self.assertTrue(db.bind_media_download_admission_request(admission, request))
        return request, admission

    @staticmethod
    def admission(admission_id):
        with db.get_conn() as conn:
            return dict(conn.execute(
                "SELECT * FROM media_download_admissions WHERE id=?", (admission_id,)
            ).fetchone())

    def assert_cancelled(self, request, admission):
        row = self.admission(admission)
        self.assertEqual(row["request_id"], request)
        self.assertEqual(row["status"], "cancelled")
        self.assertTrue(row["completed_at"])
        self.assertTrue(row["error"])
        self.assertEqual(db.list_active_media_download_admissions(self.subscription), [])
        retry = self.claim()
        self.assertIsNotNone(retry, "相同 media_key 应允许重新认领")
        self.assertNotEqual(retry, admission)

    @contextmanager
    def submission(self, *, before_claim=None, backend_result=None):
        real_dispatch = dispatcher.dispatch_request

        def dispatch_at_claim(request, target, **kwargs):
            if before_claim is not None:
                before_claim(request, target, kwargs)
            return real_dispatch(request, target, **kwargs)

        async def resolve(_service, _result_id, target, *, admission_id):
            # 与真实资源站提交相同：先复用/绑定请求，再由 dispatcher 认领。
            result = downloads.submit_download_input(
                self.item, target, origin="indexer", admission_id=admission_id,
            )
            return {**result["summary"], "request_id": result["request_id"]}

        with (
            patch.object(subscriptions, "get_indexer_service", return_value=object()),
            patch.object(subscriptions, "download_indexer_result_public", new=AsyncMock(side_effect=resolve)),
            patch.object(subscriptions, "inspect_series_episode_sources", return_value=[]),
            patch.object(downloads, "dispatch_request", side_effect=dispatch_at_claim),
            patch.object(dispatcher, "_submit_guangya", return_value=(
                backend_result if backend_result is not None else {"ok": True, "task_id": "fixture-task"}
            )) as backend,
            patch.object(dispatcher, "_submit_qb", side_effect=AssertionError("unexpected qb submission")),
        ):
            yield backend

    def download(self):
        return asyncio.run(subscriptions.MediaSubscriptionService().download_candidate(
            self.candidate, "guangya"
        ))

    def test_cancel_pending_publishes_bound_admission_in_same_transaction(self):
        request, admission = self.bound_pending()
        original_sync = repository._sync_media_download_admissions_conn
        observed = []

        def sync_in_transaction(conn, request_id, stamp):
            observed.append((conn.in_transaction, request_id, conn.execute(
                "SELECT status FROM download_requests WHERE id=?", (request_id,)
            ).fetchone()["status"]))
            return original_sync(conn, request_id, stamp)

        with patch.object(repository, "_sync_media_download_admissions_conn", side_effect=sync_in_transaction):
            self.assertTrue(db.cancel_pending_download_request(request, error="用户取消了未提交请求"))
        self.assertEqual(observed, [(True, request, "cancelled")])
        request_row = db.get_download_request(request)
        admission_row = self.admission(admission)
        self.assertEqual(admission_row["error"], "用户取消了未提交请求")
        self.assertEqual(admission_row["completed_at"], request_row["completed_at"])
        self.assertEqual(admission_row["updated_at"], request_row["updated_at"])
        self.assert_cancelled(request, admission)

    def test_cancel_pending_rolls_back_when_admission_sync_fails(self):
        request, admission = self.bound_pending()
        with patch.object(repository, "_sync_media_download_admissions_conn", side_effect=RuntimeError("projection unavailable")):
            with self.assertRaisesRegex(RuntimeError, "projection unavailable"):
                db.cancel_pending_download_request(request)
        self.assertEqual(db.get_download_request(request)["status"], "pending")
        self.assertEqual(self.admission(admission)["status"], "dispatching")
        self.assertIsNone(self.claim())

    def test_immediate_sync_projects_legacy_cancelled_request(self):
        request, admission = self.bound_pending()
        # 绕过新取消 API，模拟旧版本已经持久化、尚未同步准入的取消记录。
        db.update_download_request(request, status="cancelled", error="历史用户取消")
        self.assertEqual(db.sync_media_download_admission_for_request(request), 1)
        self.assertEqual(self.admission(admission)["error"], "历史用户取消")
        self.assert_cancelled(request, admission)

    def test_periodic_reconcile_projects_cancelled_request(self):
        request, admission = self.bound_pending()
        db.update_download_request(request, status="cancelled", error="")
        self.assertEqual(db.reconcile_media_download_admissions(
            self.subscription, set(), expected_revision=1
        ), 1)
        self.assert_cancelled(request, admission)

    def test_startup_releases_all_legacy_active_phases_bound_to_cancelled(self):
        request, admission = self.bound_pending()
        db.update_download_request(request, status="cancelled", error="历史取消")
        for phase in ("claimed", "dispatching", "submitted", "downloading", "processing"):
            with self.subTest(phase=phase):
                db.update_media_download_admission(admission, status=phase, error="", completed_at=None)
                db.init_db()
                db.reconcile_startup_media_download_admissions()
                row = self.admission(admission)
                self.assertEqual(row["status"], "cancelled")
                self.assertEqual(row["error"], "历史取消")
                self.assertTrue(row["completed_at"])
                self.assertEqual(db.list_active_media_download_admissions(self.subscription), [])
                self.assertEqual(db.reconcile_startup_media_download_admissions(), (0, 0))
        self.assert_cancelled(request, admission)

    def test_fresh_process_recovers_legacy_cancelled_owner(self):
        request, admission = self.bound_pending()
        db.update_download_request(request, status="cancelled", error="冷启动前已取消")
        db.update_media_download_admission(admission, status="processing")
        script = """
import tests
import socket
import sys

def deny_network(*args, **kwargs):
    raise AssertionError("unexpected external network")

socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.getaddrinfo = deny_network
from app import database as db

db.configure_database(sys.argv[1], test_mode=True)
db.init_db()
db.reconcile_startup_media_download_admissions()
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.database_path)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_cancelled(request, admission)

    def test_real_cancel_between_admission_binding_and_dispatch_claim_returns_409(self):
        request = self.pending_request()
        admissions = []

        def cancel_at_claim(request_id, _target, _kwargs):
            self.assertEqual(request_id, request)
            active = db.list_active_media_download_admissions(self.subscription)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["request_id"], request)
            admissions.append(active[0]["id"])
            self.assertTrue(db.cancel_pending_download_request(request, error="用户取消了未提交请求"))

        with self.submission(before_claim=cancel_at_claim) as backend:
            with self.assertRaises(subscriptions.MediaSubscriptionError) as raised:
                self.download()
            backend.assert_not_called()
        self.assertEqual((raised.exception.status_code, raised.exception.code), (409, "cancelled"))
        self.assertEqual(db.get_download_request(request)["status"], "cancelled")
        candidate = db.get_media_subscription_candidate(self.candidate)
        self.assertEqual(candidate["status"], "available")
        self.assertIsNone(candidate["request_id"])
        db.sync_media_download_admission_for_request(request)
        db.reconcile_startup_media_download_admissions()
        self.assert_cancelled(request, admissions[0])

    def test_cancel_between_pending_lookup_and_admission_binding_is_also_projected(self):
        request = self.pending_request()
        real_bind = db.bind_media_download_admission_request
        admissions = []

        def cancel_then_bind(admission_id, request_id):
            self.assertEqual(request_id, request)
            self.assertIsNone(self.admission(admission_id)["request_id"])
            self.assertTrue(db.cancel_pending_download_request(request_id))
            admissions.append(admission_id)
            return real_bind(admission_id, request_id)

        with self.submission() as backend, patch.object(
            db, "bind_media_download_admission_request", side_effect=cancel_then_bind,
        ):
            with self.assertRaises(subscriptions.MediaSubscriptionError) as raised:
                self.download()
            backend.assert_not_called()
        self.assertEqual((raised.exception.status_code, raised.exception.code), (409, "cancelled"))
        candidate = db.get_media_subscription_candidate(self.candidate)
        self.assertEqual(candidate["status"], "available")
        self.assertIsNone(candidate["request_id"])
        # 取消发生时尚未绑定，必须由 service 读取真实 cancelled 后补齐同步。
        self.assert_cancelled(request, admissions[0])

    def test_explicit_retry_uses_new_request_instead_of_earlier_cancelled_owner(self):
        previous = self.pending_request()
        self.assertTrue(db.cancel_pending_download_request(previous))
        with self.submission() as backend:
            result = self.download()
            backend.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertNotEqual(result["request_id"], previous)
        self.assertEqual(db.get_download_request(previous)["status"], "cancelled")
        self.assertEqual(db.get_download_request(result["request_id"])["status"], "submitted")
        row = self.admission(result["admission_id"])
        self.assertEqual(row["request_id"], result["request_id"])
        self.assertEqual(row["status"], "submitted")
        self.assert_active_owner(result["admission_id"])

    def test_dispatch_wins_cancel_cas_and_duplicate_keeps_active_owner(self):
        request = self.pending_request()

        def submit_first(request_id, target, kwargs):
            result = dispatcher.dispatch_request(request_id, target, **kwargs)
            self.assertTrue(result["ok"])
            self.assertFalse(db.cancel_pending_download_request(request_id))

        with self.submission(before_claim=submit_first) as backend:
            result = self.download()
            backend.assert_called_once()
        self.assertTrue(result["duplicate"])
        self.assertEqual(db.get_download_request(request)["status"], "submitted")
        self.assertEqual(db.get_media_subscription_candidate(self.candidate)["status"], "submitted")
        db.reconcile_startup_media_download_admissions()
        self.assertEqual(self.admission(result["admission_id"])["status"], "submitted")
        self.assert_active_owner(result["admission_id"])

    def test_unknown_backend_result_cannot_be_cleared_by_pending_cancel(self):
        with self.submission(backend_result={
            "ok": False, "outcome_unknown": True, "error": "后端超时，结果未知",
        }) as backend:
            result = self.download()
            backend.assert_called_once()
        request = result["request_id"]
        self.assertEqual(result["status"], "manual_review")
        self.assertFalse(db.cancel_pending_download_request(request))
        self.assertEqual(db.get_download_request(request)["gy_status"], "outcome_unknown")
        self.assertNotEqual(db.get_download_request(request)["status"], "cancelled")
        self.assertEqual(db.get_media_subscription_candidate(self.candidate)["status"], "submitted")
        db.sync_media_download_admission_for_request(request)
        db.init_db()
        db.reconcile_startup_media_download_admissions()
        row = self.admission(result["admission_id"])
        self.assertIn(row["status"], {"submitted", "processing"})
        self.assertIsNone(row["completed_at"])
        self.assert_active_owner(result["admission_id"])

    def test_failed_cancel_cas_does_not_sync_or_change_active_backend(self):
        request, admission = self.bound_pending()
        self.assertTrue(db.claim_download_request(request, "guangya"))
        for status, backend_status in (
            ("submitting", "submitting"), ("submitted", "submitted"),
            ("downloading", "downloading"), ("manual_review", "outcome_unknown"),
        ):
            with self.subTest(status=status):
                db.update_download_request(request, status=status, gy_status=backend_status, error="不能清除")
                db.sync_media_download_admission_for_request(request)
                before = dict(db.get_download_request(request))
                before_admission = self.admission(admission)
                with patch.object(repository, "_sync_media_download_admissions_conn", wraps=repository._sync_media_download_admissions_conn) as sync:
                    self.assertFalse(db.cancel_pending_download_request(request))
                    sync.assert_not_called()
                self.assertEqual(dict(db.get_download_request(request)), before)
                self.assertEqual(self.admission(admission), before_admission)
                self.assertIsNone(self.claim())

    def test_late_service_result_preserves_newer_manual_review_projection(self):
        original = db.update_media_subscription_candidate
        observed = []

        def change(candidate_id, **fields):
            changed = original(candidate_id, **fields)
            if fields.get("request_id"):
                db.update_download_request_and_sync_media_admission(
                    fields["request_id"], status="manual_review", error="新回执需要人工核验",
                )
                observed.append(dict(db.list_active_media_download_admissions(self.subscription)[0]))
            return changed

        with self.submission(), patch.object(db, "update_media_subscription_candidate", side_effect=change):
            result = self.download()
        current = self.admission(result["admission_id"])
        self.assertEqual(current["status"], "processing")
        self.assertEqual(current["error"], observed[0]["error"])
        self.assertIsNone(current["completed_at"])
        self.assert_active_owner(result["admission_id"])

    def test_existing_completed_request_preserves_postprocessing_failure(self):
        request = self.pending_request()
        db.update_download_request(
            request, status="completed", targets="guangya", gy_status="completed",
            organize_status="failed", organize_error="归档失败，需要处理",
        )
        with self.submission() as backend:
            result = self.download()
        backend.assert_not_called()
        current = self.admission(result["admission_id"])
        self.assertEqual(current["status"], "failed")
        self.assertIn("归档失败，需要处理", current["error"])
        self.assertIsNotNone(current["completed_at"])
        self.assertEqual(db.list_active_media_download_admissions(self.subscription), [])
        self.assertEqual(db.sync_media_download_admission_for_request(request), 0)


if __name__ == "__main__":
    unittest.main()
