"""下载状态归一化与 Tracker 快照竞争的隔离回归，不连接真实下载器。"""

from __future__ import annotations

import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.clients.guangya import GuangYaClient
from app.modules import download_dispatcher
from app.modules.download_tracker import DownloadTracker
from app.repositories.download_requests import apply_download_tracker_update
from tests.support import isolated_test_database


class DownloadTrackerStateRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(isolated_test_database())
        for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "getaddrinfo")):
            self.enterContext(patch.object(owner, name, side_effect=AssertionError("禁止外联")))
        self.tracker = DownloadTracker()
        self.notify = self.enterContext(
            patch.object(self.tracker, "_notify_completion")
        )
        self.local_import = self.enterContext(
            patch.object(self.tracker, "_start_local_import")
        )
        self.organize = self.enterContext(patch.object(self.tracker, "_start_organize"))
        self.staging = self.enterContext(
            patch.object(self.tracker, "_staging_ready_for_organize", return_value=True)
        )

    @staticmethod
    def _qb_task(progress: float = 1.0):
        return SimpleNamespace(
            hash="a" * 40,
            name="Tracker race",
            progress=progress,
            state="uploading" if progress == 1.0 else "downloading",
            content_path="/isolated-download/Tracker race.mkv",
        )

    @staticmethod
    def _request(**fields) -> int:
        item = download_dispatcher.DownloadInput(
            kind="magnet",
            title="Tracker race",
            source_value="magnet:?xt=urn:btih:" + "a" * 40,
        )
        request_id, _ = db.create_download_request(
            download_dispatcher.request_key(item),
            item.kind,
            title=item.title,
            source_value=item.source_value,
        )
        db.update_download_request(
            request_id,
            status="submitted",
            targets="both",
            qb_status="submitted",
            qb_task_id="a" * 40,
            gy_status="submitted",
            gy_task_id="gy-1",
            gy_task_ids='["gy-1"]',
            gy_batch_count=1,
        )
        if fields:
            db.update_download_request(request_id, **fields)
        return request_id

    @staticmethod
    def _bind_admission(request_id: int) -> int:
        subscription_id = db.add_media_subscription(
            provider="tmdb",
            external_id="1",
            tmdb_id="1",
            media_type="tv",
            title="Tracker race",
            monitor_mode="missing",
            action="confirm",
            download_target="guangya",
            check_interval_minutes=60,
        )
        candidate_id = db.replace_media_subscription_candidates(
            subscription_id,
            "tmdb:1:tv:S01E001",
            season=1,
            episode=1,
            candidates=[{"result_id": "race-result", "title": "Tracker race"}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        admission_id = db.claim_media_download_admission(
            media_key="tmdb:1:tv:S01E001",
            tmdb_id="1",
            media_type="tv",
            subscription_id=subscription_id,
            candidate_id=candidate_id,
            season=1,
            episode=1,
            subscription_revision=1,
        )
        db.begin_media_download_dispatch(
            admission_id,
            subscription_id=subscription_id,
            subscription_revision=1,
        )
        db.bind_media_download_admission_request(admission_id, request_id)
        return subscription_id

    def test_guangya_failure_codes_use_same_classification_end_to_end(self) -> None:
        for state in (4, "4", -1, "-1", "failed", "error", "cancelled", "invalid"):
            with self.subTest(state=state):
                task = GuangYaClient._to_offline_task(
                    {"taskId": "gy-1", "status": state, "progress": 20}
                )
                self.assertEqual(task["status_kind"], "failed")
                self.assertEqual(self.tracker._gy_task_state(task), "failed")

    def test_guangya_explicit_failure_wins_over_full_progress(self) -> None:
        for state in (4, "4", -1, "-1", "failed", "error"):
            with self.subTest(state=state):
                task = GuangYaClient._to_offline_task(
                    {"taskId": "gy-1", "status": state, "progress": 100}
                )
                self.assertEqual(task["status_kind"], "failed")
                self.assertEqual(self.tracker._gy_task_state(task), "failed")
                # 兼容旧调用方只提供 status/progress 的字典，不能另走不同分类规则。
                self.assertEqual(
                    self.tracker._gy_task_state({"status": state, "progress": 1.0}),
                    "failed",
                )

    def test_guangya_failure_releases_admission_without_organizing(self) -> None:
        for task_ids in ("[]", '["gy-1"]'):
            with self.subTest(task_ids=task_ids), isolated_test_database():
                request_id = self._request(
                    targets="guangya", qb_status="", gy_task_ids=task_ids
                )
                subscription_id = self._bind_admission(request_id)
                task = GuangYaClient._to_offline_task(
                    {"taskId": "gy-1", "status": 4, "progress": 20}
                )
                self.tracker._update_request(
                    db.get_download_request(request_id), [], [task], qb_available=False
                )
                row = db.get_download_request(request_id)
                self.assertEqual(
                    (row["status"], row["gy_status"]), ("failed", "failed")
                )
                self.assertEqual(
                    db.list_active_media_download_admissions(subscription_id), []
                )
                self.organize.assert_not_called()
                self.staging.assert_not_called()

    @staticmethod
    def _gy_batch(*states):
        return [
            GuangYaClient._to_offline_task({"taskId": f"gy-{index}", "status": state, "progress": 0})
            for index, state in enumerate(states, 1) if state is not None
        ]

    def test_guangya_batch_failure_keeps_live_admission_and_blocks_resource_retry(self) -> None:
        item = download_dispatcher.normalize_download_url("magnet:?xt=urn:btih:" + "d" * 40)
        request_id = download_dispatcher.create_request(item, "", "batch-repro")["id"]
        with (
            patch.object(download_dispatcher, "get", side_effect=lambda key, default="": default),
            patch.object(download_dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True)),
            patch.object(download_dispatcher, "_recover_guangya_magnet_torrent", return_value=None),
            patch.object(download_dispatcher, "_submit_guangya", return_value={
                "ok": True, "task_ids": ["gy-1", "gy-2"], "batch_count": 2,
            }) as submit,
        ):
            self.assertTrue(download_dispatcher.dispatch_request(request_id, "guangya")["ok"])
            subscription_id = self._bind_admission(request_id)
            self.tracker._update_request(
                db.get_download_request(request_id), [], self._gy_batch(4, 0), qb_available=False,
            )
            row = db.get_download_request(request_id)
            active = db.list_active_media_download_admissions(subscription_id)
            retry = download_dispatcher.resubmit_download_request(request_id, "guangya")

        self.assertEqual((row["status"], row["gy_status"]), ("downloading", "downloading"))
        self.assertIsNone(row["completed_at"])
        self.assertEqual(len(active), 1)
        self.assertFalse(retry["ok"])
        submit.assert_called_once()
        self.organize.assert_not_called()
        self.staging.assert_not_called()

    def test_guangya_batch_terminal_state_requires_every_batch_to_be_known(self) -> None:
        cases = (
            ((4, 4), "", 2, "failed"),
            ((4, 0), "", 2, "downloading"),
            ((4, "unknown"), "", 2, "downloading"),
            ((4, None), "", 2, "submitted"),
            ((4, None), "2000-01-01 00:00:00", 2, "manual_review"),
            ((4, 1), "", 2, "manual_review"),
            ((1, 0), "", 2, "downloading"),
            ((1, 1), "", 2, "completed"),
            ((4, 0), "", 3, "manual_review"),
        )
        for states, missing_since, batch_count, expected in cases:
            with self.subTest(states=states, missing_since=missing_since, batch_count=batch_count), isolated_test_database():
                self.organize.reset_mock()
                self.staging.reset_mock()
                request_id = self._request(
                    targets="guangya", qb_status="", gy_task_ids='["gy-1","gy-2"]',
                    gy_batch_count=batch_count, gy_task_missing_since=missing_since or None,
                )
                subscription_id = self._bind_admission(request_id)
                self.tracker._update_request(
                    db.get_download_request(request_id), [], self._gy_batch(*states), qb_available=False,
                )
                row = db.get_download_request(request_id)
                self.assertEqual((row["status"], row["gy_status"]), (expected, expected))
                self.assertEqual(len(db.list_active_media_download_admissions(subscription_id)), int(expected != "failed"))
                if expected == "completed":
                    self.organize.assert_called_once()
                else:
                    self.organize.assert_not_called()
                    self.staging.assert_not_called()
                if expected in {"submitted", "downloading"}:
                    self.assertIsNone(row["completed_at"])
                    self.assertEqual(row["notification_event_status"], "")

    def test_guangya_batch_mixed_terminal_does_not_report_success_with_completed_qb(self) -> None:
        request_id = self._request(
            qb_status="completed", local_import_status="completed",
            gy_task_ids='["gy-1","gy-2"]', gy_batch_count=2,
        )
        subscription_id = self._bind_admission(request_id)
        self.tracker._update_request(
            db.get_download_request(request_id), [], self._gy_batch(4, 1), qb_available=False,
        )
        row = db.get_download_request(request_id)
        self.assertEqual((row["status"], row["gy_status"]), ("manual_review", "manual_review"))
        self.assertEqual(row["notification_event_status"], "manual_review")
        self.assertEqual(len(db.list_active_media_download_admissions(subscription_id)), 1)
        self.organize.assert_not_called()

    def test_guangya_batch_without_id_list_cannot_collapse_to_one_terminal_task(self) -> None:
        for state in (4, 1):
            with self.subTest(state=state), isolated_test_database():
                request_id = self._request(
                    targets="guangya", qb_status="", gy_task_ids="[]", gy_batch_count=2, gy_isolated=1,
                )
                subscription_id = self._bind_admission(request_id)
                self.tracker._update_request(
                    db.get_download_request(request_id), [], self._gy_batch(state), qb_available=False,
                )
                row = db.get_download_request(request_id)
                self.assertEqual((row["status"], row["gy_status"]), ("manual_review", "manual_review"))
                self.assertEqual((row["gy_task_ids"], row["gy_batch_count"]), ("[]", 2))
                self.assertEqual(len(db.list_active_media_download_admissions(subscription_id)), 1)
                self.organize.assert_not_called()

    def test_guangya_batch_still_allows_only_failed_qb_to_retry_in_place(self) -> None:
        request_id = self._request(qb_status="failed", gy_task_ids='["gy-1","gy-2"]', gy_batch_count=2)
        self.tracker._update_request(
            db.get_download_request(request_id), [], self._gy_batch(4, 0), qb_available=False,
        )
        with (
            patch.object(download_dispatcher, "get", side_effect=lambda key, default="": "http://qb.invalid" if key == "QB_URL" else default),
            patch.object(download_dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True)),
            patch.object(download_dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "a" * 40}) as qb,
            patch.object(download_dispatcher, "_submit_guangya") as gy,
        ):
            result = download_dispatcher.resubmit_download_request(request_id, "qb")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["created"])
        self.assertEqual(result["request_id"], request_id)
        row = db.get_download_request(request_id)
        self.assertEqual((row["gy_status"], row["gy_task_ids"]), ("downloading", '["gy-1","gy-2"]'))
        qb.assert_called_once()
        gy.assert_not_called()

    def test_guangya_batch_unavailable_backend_does_not_finalize_stale_snapshot(self) -> None:
        for task_ids in ('["gy-1","gy-2"]', "[]"):
            with self.subTest(task_ids=task_ids), isolated_test_database():
                request_id = self._request(
                    targets="guangya", qb_status="", gy_task_ids=task_ids, gy_batch_count=2,
                    gy_task_missing_since="2000-01-01 00:00:00",
                )
                subscription_id = self._bind_admission(request_id)
                self.tracker._update_request(
                    db.get_download_request(request_id), [], self._gy_batch(4, 4),
                    qb_available=False, gy_available=False,
                )
                row = db.get_download_request(request_id)
                self.assertEqual((row["status"], row["gy_status"]), ("submitted", "submitted"))
                self.assertIsNone(row["completed_at"])
                self.assertEqual(len(db.list_active_media_download_admissions(subscription_id)), 1)
                self.organize.assert_not_called()

    def test_stale_snapshot_cannot_revive_successfully_resubmitted_request(
        self,
    ) -> None:
        request_id = self._request(
            status="manual_review", qb_status="downloading", gy_status="manual_review"
        )
        snapshot = db.get_download_request(request_id)
        with (
            patch.object(
                download_dispatcher,
                "download_resubmit_capabilities",
                return_value={"guangya": {"enabled": True}},
            ),
            patch.object(
                download_dispatcher,
                "_submit_guangya",
                return_value={
                    "ok": True,
                    "task_id": "new-gy",
                    "task_ids": ["new-gy"],
                    "batch_count": 1,
                },
            ),
        ):
            result = download_dispatcher.resubmit_download_request(
                request_id, "guangya"
            )
        self.assertTrue(result["ok"])
        self.assertEqual(db.get_download_request(request_id)["status"], "resubmitted")
        with patch.object(self.tracker, "_update_backend_log") as backend_log:
            self.tracker._update_request(
                snapshot, [self._qb_task(0.5)], [], gy_available=False
            )
            self.assertEqual(
                db.get_download_request(request_id)["status"], "resubmitted"
            )
            self.assertEqual(db.count_download_requests_requiring_attention(), 0)
            backend_log.assert_not_called()
            self.notify.assert_not_called()

        # 冲突只丢弃旧观察；下一轮必须继续跟踪没有被接管的 qB，不能一刀切停掉旧请求。
        self.tracker._update_request(
            db.get_download_request(request_id),
            [self._qb_task()],
            [],
            gy_available=False,
        )
        row = db.get_download_request(request_id)
        self.assertEqual(
            (row["status"], row["qb_status"], row["gy_status"]),
            ("resubmitted", "completed", "resubmitted"),
        )
        self.assertNotIn(
            request_id, {row["id"] for row in db.list_active_download_requests()}
        )
        self.local_import.assert_called_once()
        self.assertEqual(self.local_import.call_args.args[0]["qb_status"], "completed")

    def test_cancelled_request_rejects_stale_and_current_snapshot_side_effects(
        self,
    ) -> None:
        request_id = self._request()
        old_snapshot = db.get_download_request(request_id)
        db.update_download_request(
            request_id, status="cancelled", qb_status="cancelled", gy_status="cancelled"
        )
        cancelled_snapshot = db.get_download_request(request_id)
        done = GuangYaClient._to_offline_task({"taskId": "gy-1", "status": 1})
        with patch.object(self.tracker, "_update_backend_log") as backend_log:
            for snapshot in (old_snapshot, cancelled_snapshot):
                self.tracker._update_request(snapshot, [self._qb_task()], [done])
                self.assertEqual(
                    db.get_download_request(request_id)["status"], "cancelled"
                )
            backend_log.assert_not_called()
        self.local_import.assert_not_called()
        self.organize.assert_not_called()
        self.staging.assert_not_called()
        self.notify.assert_not_called()

    def test_normal_qb_completion_handoff_uses_persisted_completed_row(self) -> None:
        request_id = self._request(targets="qb", gy_status="")
        self.tracker._update_request(
            db.get_download_request(request_id),
            [self._qb_task()],
            [],
            gy_available=False,
        )
        row = db.get_download_request(request_id)
        self.assertEqual((row["status"], row["qb_status"]), ("completed", "completed"))
        self.local_import.assert_called_once()
        handed_off = self.local_import.call_args.args[0]
        self.assertEqual(
            (handed_off["status"], handed_off["qb_status"]), ("completed", "completed")
        )
        self.assertEqual(handed_off["notification_event_status"], "completed")

    def test_success_and_progress_fallback_remain_consistent(self) -> None:
        for state, progress, expected in (
            (1, 0, "completed"),
            ("2", 0, "completed"),
            (3, 0, "completed"),
            ("done", 0, "completed"),
            (0, 20, "downloading"),
            ("unknown", 100, "completed"),
            ("unknown", "invalid", "downloading"),
        ):
            with self.subTest(state=state, progress=progress):
                task = GuangYaClient._to_offline_task(
                    {"status": state, "progress": progress}
                )
                self.assertEqual(self.tracker._gy_task_state(task), expected)
                self.assertEqual(
                    task["status_kind"],
                    "done" if expected == "completed" else "running",
                )

    def test_same_second_backend_identity_change_rejects_observation(self) -> None:
        request_id = self._request(targets="qb", gy_status="")
        snapshot = db.get_download_request(request_id)
        # 模拟同一秒内后端认领更替，不能把秒级 updated_at 当成 CAS 版本。
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE download_requests SET qb_task_id=?,updated_at=? WHERE id=?",
                ("b" * 40, snapshot["updated_at"], request_id),
            )
        self.tracker._update_request(
            snapshot, [self._qb_task()], [], gy_available=False
        )
        row = db.get_download_request(request_id)
        self.assertEqual((row["qb_task_id"], row["qb_status"]), ("b" * 40, "submitted"))
        self.local_import.assert_not_called()
        self.notify.assert_not_called()

    def test_cancellation_at_persistence_boundary_prevents_all_side_effects(
        self,
    ) -> None:
        request_id = self._request()
        log_id = db.add_download_log(
            source="qb", request_id=request_id, status="submitted"
        )
        snapshot = db.get_download_request(request_id)

        def cancel_then_persist(expected, **fields):
            db.update_download_request(request_id, status="cancelled")
            return apply_download_tracker_update(expected, **fields)

        with patch(
            "app.modules.download_tracker.apply_download_tracker_update",
            side_effect=cancel_then_persist,
        ):
            self.tracker._update_request(
                snapshot,
                [self._qb_task()],
                [GuangYaClient._to_offline_task({"taskId": "gy-1", "status": 1})],
            )
        self.assertEqual(db.get_download_request(request_id)["status"], "cancelled")
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM download_log WHERE id=?", (log_id,)
                ).fetchone()[0],
                "submitted",
            )
        self.local_import.assert_not_called()
        self.organize.assert_not_called()
        self.staging.assert_not_called()
        self.notify.assert_not_called()

    def test_admission_projection_failure_rolls_back_before_logs_or_handoff(
        self,
    ) -> None:
        request_id = self._request(targets="qb", gy_status="")
        subscription_id = self._bind_admission(request_id)
        snapshot = db.get_download_request(request_id)
        log_id = db.add_download_log(
            source="qb", request_id=request_id, status="submitted"
        )
        with (
            patch(
                "app.repositories.media_subscriptions._sync_media_download_admissions_conn",
                side_effect=RuntimeError("isolated projection failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "isolated projection failure"),
        ):
            self.tracker._update_request(
                snapshot, [self._qb_task()], [], gy_available=False
            )
        self.assertEqual(dict(db.get_download_request(request_id)), dict(snapshot))
        self.assertEqual(
            db.list_active_media_download_admissions(subscription_id)[0]["status"],
            "dispatching",
        )
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM download_log WHERE id=?", (log_id,)
                ).fetchone()[0],
                "submitted",
            )
        self.local_import.assert_not_called()
        self.notify.assert_not_called()

    def test_changed_postprocessing_state_rejects_stale_handoff(self) -> None:
        request_id = self._request(
            targets="qb", gy_status="", qb_status="completed", status="completed"
        )
        snapshot = db.get_download_request(request_id)
        db.update_download_request(request_id, local_import_status="completed")
        self.tracker._update_request(
            snapshot, [self._qb_task()], [], gy_available=False
        )
        self.assertEqual(
            db.get_download_request(request_id)["local_import_status"], "completed"
        )
        self.local_import.assert_not_called()
        self.notify.assert_not_called()

    def test_notification_handoff_uses_only_the_active_repository_api(self) -> None:
        from app.repositories import download_requests

        self.assertFalse(hasattr(db, "renew_download_request_notification_lease"))
        self.assertFalse(hasattr(download_requests, "renew_download_request_notification_lease"))

    def test_notification_outbox_owns_transport_retries_after_tracker_handoff(self) -> None:
        import threading
        from app.modules import telegram_notification_center as center
        from app.notifier import TelegramSendResult

        request_id = self._request(
            status="completed", targets="qb", qb_status="completed", gy_status="",
            notification_event_status="completed", notification_delivery_status="pending",
        )
        with db.get_conn() as conn:
            conn.execute("UPDATE download_requests SET chat_id='100' WHERE id=?", (request_id,))
        with (
            patch.object(center, "_dispatch_stop", threading.Event()),
            patch.object(center, "allows_notification", return_value=True),
            patch.object(center, "send_event_result", side_effect=[
                TelegramSendResult(False, status_code=500, error="ServerError"),
                TelegramSendResult(True, message_id=101),
            ]) as transport,
        ):
            DownloadTracker._notify_completion(db.get_download_request(request_id), "completed", "", {})
            request = db.get_download_request(request_id)
            self.assertEqual(request["notification_delivery_status"], "sent")
            self.assertEqual(request["notification_attempts"], 0)
            with db.get_conn() as conn:
                outbox = conn.execute("SELECT * FROM telegram_notification_outbox").fetchone()
                self.assertEqual(outbox["status"], "retry_wait")
                self.assertEqual(outbox["attempts"], 1)
                conn.execute("UPDATE telegram_notification_outbox SET next_attempt_at='2000-01-01 00:00:00'")
            DownloadTracker._notify_completion(request, "completed", "", {})
            self.assertEqual(transport.call_count, 1)
            self.assertTrue(center.drain_telegram_notifications())
            self.assertEqual(transport.call_count, 2)
            with db.get_conn() as conn:
                outbox = conn.execute("SELECT * FROM telegram_notification_outbox").fetchone()
                self.assertEqual((outbox["status"], outbox["message_id"]), ("sent", 101))
            center.drain_telegram_notifications()
            self.assertEqual(transport.call_count, 2)

    def test_tracker_retries_if_outbox_persistence_fails(self) -> None:
        from app.modules import telegram_notification_center as center

        request_id = self._request(
            status="failed", qb_status="failed", gy_status="failed",
            notification_event_status="failed", notification_delivery_status="pending",
        )
        with db.get_conn() as conn:
            conn.execute("UPDATE download_requests SET chat_id='100' WHERE id=?", (request_id,))
        with patch.object(center, "upsert_notification", side_effect=RuntimeError("outbox unavailable")):
            DownloadTracker._notify_completion(db.get_download_request(request_id), "failed", "failed", {})
        request = db.get_download_request(request_id)
        self.assertEqual(request["notification_delivery_status"], "retry_wait")
        self.assertEqual(request["notification_attempts"], 1)
        self.assertTrue(request["notification_next_retry_at"])
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_notification_outbox").fetchone()[0], 0)

    def test_resubmitted_request_can_finish_existing_notification_delivery(
        self,
    ) -> None:
        request_id = self._request(
            status="resubmitted",
            qb_status="completed",
            gy_status="resubmitted",
            local_import_status="completed",
            notification_delivery_status="pending",
            notification_event_status="manual_review",
            notification_next_retry_at=None,
        )
        self.notify.side_effect = DownloadTracker._notify_completion
        with patch.object(
            self.tracker.__class__, "_publish_lifecycle", return_value=True
        ):
            self.tracker._update_request(
                db.get_download_request(request_id),
                [],
                [],
                qb_available=False,
                gy_available=False,
            )
        self.assertEqual(
            db.get_download_request(request_id)["notification_delivery_status"], "sent"
        )
        self.assertNotIn(
            request_id, {row["id"] for row in db.list_active_download_requests()}
        )
        persisted = self.notify.call_args.args[0]
        self.assertEqual(persisted["status"], "resubmitted")

    def test_cancelled_request_is_not_polled_for_stale_notification_or_import(
        self,
    ) -> None:
        request_id = self._request(
            status="cancelled",
            qb_status="completed",
            local_import_status="pending",
            notification_delivery_status="pending",
            notification_event_status="completed",
            notification_next_retry_at=None,
        )
        for cursor in (0, request_id):
            self.assertNotIn(
                request_id,
                {
                    row["id"]
                    for row in db.list_active_download_requests(
                        include_local_import=True,
                        after_id=cursor,
                        wrap=True,
                    )
                },
            )


if __name__ == "__main__":
    unittest.main()
