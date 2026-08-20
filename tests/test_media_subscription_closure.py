from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from app import database as db
from app.clients.guangya import GuangYaClient
from app.modules.download_dispatcher import DownloadInput, request_key
from app.modules.download_tracker import DownloadTracker
from app.modules.indexer_download import _persist_and_dispatch
from app.modules.media_subscriptions import MediaSubscriptionService
from tests.support import IsolatedDatabaseTestCase


class MediaSubscriptionClosureTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_download_admissions")
            conn.execute("DELETE FROM media_subscription_candidates")
            conn.execute("DELETE FROM media_subscription_runs")
            conn.execute("DELETE FROM media_subscriptions")
            conn.execute("DELETE FROM download_requests")

    def _seed(self):
        subscription_id = db.add_media_subscription(
            provider="tmdb", external_id="1", tmdb_id="1", media_type="tv",
            title="闭环测试", monitor_mode="missing", action="confirm",
            download_target="both", check_interval_minutes=60,
        )
        candidate_id = db.replace_media_subscription_candidates(
            subscription_id, "tmdb:1:tv:S01E001", season=1, episode=1,
            candidates=[{"result_id":"result-1","title":"闭环资源","download_state":"ready","relevance_score":99}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        return subscription_id, candidate_id

    def _claim_dispatching(self, subscription_id: int, candidate_id: int) -> int:
        admission_id = db.claim_media_download_admission(
            media_key=f"tmdb:{subscription_id}:tv:S01E001",
            tmdb_id=str(subscription_id),
            media_type="tv",
            subscription_id=subscription_id,
            candidate_id=candidate_id,
            season=1,
            episode=1,
            subscription_revision=1,
        )
        self.assertIsInstance(admission_id, int)
        self.assertTrue(db.begin_media_download_dispatch(
            int(admission_id),
            subscription_id=subscription_id,
            subscription_revision=1,
        ))
        return int(admission_id)

    def test_request_creation_binds_dispatching_admission_in_same_transaction(self):
        subscription_id, candidate_id = self._seed()
        admission_id = self._claim_dispatching(subscription_id, candidate_id)

        request_id, created = db.create_download_request(
            "admission-bound", "magnet", title="闭环资源", admission_id=admission_id
        )

        self.assertTrue(created)
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()
        self.assertEqual(int(admission["request_id"]), request_id)
        self.assertEqual(admission["status"], "dispatching")

    def test_request_creation_rolls_back_when_admission_cannot_bind(self):
        subscription_id, candidate_id = self._seed()
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

        with self.assertRaises(RuntimeError):
            db.create_download_request(
                "admission-rollback", "magnet", title="闭环资源",
                admission_id=int(admission_id),
            )

        self.assertIsNone(db.get_download_request_by_request_key("admission-rollback"))

    def test_request_and_admission_projection_roll_back_together(self):
        subscription_id, candidate_id = self._seed()
        admission_id = self._claim_dispatching(subscription_id, candidate_id)
        request_id, _ = db.create_download_request(
            "admission-atomic", "magnet", title="闭环资源", admission_id=admission_id
        )
        with db.get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER reject_admission_failure BEFORE UPDATE ON media_download_admissions "
                "WHEN NEW.status='failed' BEGIN SELECT RAISE(ABORT,'reject projection'); END"
            )
        try:
            with self.assertRaises(Exception):
                db.update_download_request_and_sync_media_admission(
                    request_id, status="failed", error="提交失败"
                )
        finally:
            with db.get_conn() as conn:
                conn.execute("DROP TRIGGER IF EXISTS reject_admission_failure")

        request = db.get_download_request(request_id)
        self.assertEqual(request["status"], "pending")
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT status FROM media_download_admissions WHERE id=?", (admission_id,)
            ).fetchone()
        self.assertEqual(admission["status"], "dispatching")

    def test_startup_reconcile_immediately_releases_unbound_admissions(self):
        first_subscription, first_candidate = self._seed()
        first_admission = self._claim_dispatching(first_subscription, first_candidate)
        old_stamp = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_download_admissions SET updated_at=? WHERE id=?",
                (old_stamp, first_admission),
            )

        second_subscription = db.add_media_subscription(
            provider="tmdb", external_id="2", tmdb_id="2", media_type="tv",
            title="闭环测试 2", monitor_mode="missing", action="confirm",
            download_target="both", check_interval_minutes=60,
        )
        second_candidate = db.replace_media_subscription_candidates(
            second_subscription, "tmdb:2:tv:S01E001", season=1, episode=1,
            candidates=[{"result_id":"result-2","title":"闭环资源 2","download_state":"ready"}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        second_admission = self._claim_dispatching(second_subscription, second_candidate)

        projected, released = db.reconcile_startup_media_download_admissions(stale_seconds=60)

        self.assertEqual(projected, 0)
        self.assertEqual(released, 2)
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id,status FROM media_download_admissions WHERE id IN (?,?) ORDER BY id",
                (first_admission, second_admission),
            ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["released", "released"])

    def test_released_stale_pending_request_is_reused_and_actually_dispatched(self):
        subscription_id, candidate_id = self._seed()
        original_admission = self._claim_dispatching(subscription_id, candidate_id)
        item = DownloadInput(
            kind="magnet", title="待恢复资源",
            source_value="magnet:?xt=urn:btih:pending-recovery",
        )
        request_id, created = db.create_download_request(
            request_key(item), "magnet", title=item.title,
            source_value=item.source_value, admission_id=original_admission,
        )
        self.assertTrue(created)
        old_stamp = (datetime.now() - timedelta(minutes=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_download_admissions SET updated_at=? WHERE id=?",
                (old_stamp, original_admission),
            )
        _projected, released = db.reconcile_startup_media_download_admissions(
            stale_seconds=60
        )
        self.assertEqual(released, 1)

        retry_admission = db.claim_media_download_admission(
            media_key=f"tmdb:{subscription_id}:tv:S01E001",
            tmdb_id=str(subscription_id), media_type="tv",
            subscription_id=subscription_id, candidate_id=candidate_id,
            season=1, episode=1, subscription_revision=1,
        )
        self.assertTrue(db.begin_media_download_dispatch(
            int(retry_admission), subscription_id=subscription_id,
            subscription_revision=1,
        ))
        dispatched = {
            "handled": True, "ok": True, "request_id": request_id,
            "status": "submitted", "succeeded": ["qb"], "failed": [],
            "duplicate": False, "error": "",
        }
        with patch(
            "app.modules.indexer_download.dispatch_request", return_value=dispatched
        ) as dispatch_mock:
            reused, reused_id, result = _persist_and_dispatch(
                item, "subscription:test", "qb",
                admission_id=int(retry_admission),
            )

        self.assertFalse(reused["created"])
        self.assertEqual(reused_id, request_id)
        self.assertEqual(result, dispatched)
        dispatch_mock.assert_called_once_with(request_id, "qb")
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                (retry_admission,),
            ).fetchone()
        self.assertEqual(int(row["request_id"]), request_id)
        self.assertEqual(row["status"], "dispatching")

    def test_dispatch_exception_preserves_request_and_admission_for_manual_review(self):
        subscription_id, candidate_id = self._seed()
        admission_id = self._claim_dispatching(subscription_id, candidate_id)
        item = DownloadInput(
            kind="magnet", title="结果未知资源",
            source_value="magnet:?xt=urn:btih:unknown-result",
        )

        with patch(
            "app.modules.indexer_download.dispatch_request",
            side_effect=RuntimeError("persist after backend failed"),
        ):
            created, request_id, result = _persist_and_dispatch(
                item, "indexer:test", "qb", admission_id=admission_id,
            )

        self.assertTrue(created["created"])
        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(result["request_id"], request_id)
        request = db.get_download_request(request_id)
        self.assertEqual(request["status"], "manual_review")
        self.assertEqual(request["qb_status"], "manual_review")
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()
        self.assertEqual(int(admission["request_id"]), request_id)
        self.assertEqual(admission["status"], "processing")

    def test_backend_success_followed_by_persistence_failure_becomes_manual_review(self):
        subscription_id, candidate_id = self._seed()
        admission_id = self._claim_dispatching(subscription_id, candidate_id)
        item = DownloadInput(
            kind="magnet", title="后端已接收资源",
            source_value="magnet:?xt=urn:btih:backend-accepted",
        )
        original_update = db.update_download_request_and_sync_media_admission
        update_calls = 0

        def fail_first_update(request_id, **fields):
            nonlocal update_calls
            update_calls += 1
            if update_calls == 1:
                raise RuntimeError("simulated sqlite write failure")
            return original_update(request_id, **fields)

        with patch(
            "app.modules.download_dispatcher._submit_qb",
            return_value={"ok": True, "task_id": "qb-accepted"},
        ), patch(
            "app.modules.download_dispatcher.db.update_download_request_and_sync_media_admission",
            side_effect=fail_first_update,
        ):
            _created, request_id, result = _persist_and_dispatch(
                item, "indexer:test", "qb", admission_id=admission_id,
            )

        self.assertEqual(update_calls, 2)
        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(result["request_id"], request_id)
        request = db.get_download_request(request_id)
        self.assertEqual(request["status"], "manual_review")
        self.assertEqual(request["qb_status"], "manual_review")
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()
        self.assertEqual(int(admission["request_id"]), request_id)
        self.assertEqual(admission["status"], "processing")

    def test_late_dispatch_result_cannot_reopen_completed_admission(self):
        subscription_id, candidate_id = self._seed()
        admission_id = self._claim_dispatching(subscription_id, candidate_id)
        db.complete_media_download_admissions([f"tmdb:{subscription_id}:tv:S01E001"])

        updated = db.update_media_download_admission(
            admission_id,
            expected_statuses=("dispatching",),
            status="submitted",
        )

        self.assertFalse(updated)
        with db.get_conn() as conn:
            status = conn.execute(
                "SELECT status FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()["status"]
        self.assertEqual(status, "completed")

    def test_startup_reconcile_projects_linked_manual_review(self):
        subscription_id, candidate_id = self._seed()
        admission_id = self._claim_dispatching(subscription_id, candidate_id)
        request_id, _ = db.create_download_request(
            "admission-manual-review", "magnet", title="闭环资源",
            admission_id=admission_id,
        )
        db.update_download_request(
            request_id, status="manual_review", error="提交结果未知，请人工核验"
        )

        projected, released = db.reconcile_startup_media_download_admissions(stale_seconds=60)

        self.assertEqual(projected, 1)
        self.assertEqual(released, 0)
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT status,error FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()
        self.assertEqual(admission["status"], "processing")
        self.assertIn("人工核验", admission["error"])

    def test_refresh_preserves_submitted_candidate_identity_and_admission_link(self):
        subscription_id, candidate_id = self._seed()
        request_id, _ = db.create_download_request(
            "submitted-refresh", "magnet", title="闭环资源"
        )
        admission_id = db.claim_media_download_admission(
            media_key="tmdb:1:tv:S01E001", tmdb_id="1", media_type="tv",
            subscription_id=subscription_id, candidate_id=candidate_id,
            season=1, episode=1, subscription_revision=1,
        )
        db.update_media_subscription_candidate(
            candidate_id, status="submitted", request_id=request_id
        )
        db.update_media_download_admission(
            admission_id, status="submitted", request_id=request_id
        )

        refreshed_id = db.replace_media_subscription_candidates(
            subscription_id, "tmdb:1:tv:S01E001", season=1, episode=1,
            candidates=[{
                "result_id": "result-1", "title": "闭环资源（刷新）",
                "site_id": "nyaa", "site_name": "Nyaa",
                "download_state": "ready", "relevance_score": 100,
            }],
            expires_at="2099-02-01 00:00:00",
        )[0]

        self.assertEqual(refreshed_id, candidate_id)
        candidate = db.get_media_subscription_candidate(candidate_id)
        self.assertEqual(candidate["status"], "submitted")
        self.assertEqual(int(candidate["request_id"]), request_id)
        self.assertEqual(candidate["title"], "闭环资源（刷新）")
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT candidate_id,request_id,status FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()
        self.assertEqual(int(admission["candidate_id"]), candidate_id)
        self.assertEqual(int(admission["request_id"]), request_id)
        self.assertEqual(admission["status"], "submitted")

    def test_legacy_guangya_create_preserves_returned_task_id(self):
        client = object.__new__(GuangYaClient)
        raw = Mock()
        raw.cloud_create_task.return_value = {"code": 0, "data": {"taskId": "gy-123"}}
        with patch.object(GuangYaClient, "raw", new_callable=PropertyMock, return_value=raw):
            result = client.add_offline_task("magnet:?xt=urn:btih:test", "stage")
        self.assertTrue(result["ok"])
        self.assertEqual(result["task_ids"], ["gy-123"])
        self.assertEqual(result["batch_count"], 1)

    def test_tracker_backfills_isolated_task_identity_and_syncs_admission(self):
        subscription_id, candidate_id = self._seed()
        request_id, _ = db.create_download_request("closure-key", "magnet", title="闭环资源")
        admission_id = db.claim_media_download_admission(
            media_key="tmdb:1:tv:S01E001", tmdb_id="1", media_type="tv",
            subscription_id=subscription_id, candidate_id=candidate_id, season=1, episode=1,
            subscription_revision=1,
        )
        db.update_media_download_admission(admission_id, status="submitted", request_id=request_id)
        db.update_download_request(
            request_id, targets="guangya", status="submitted", gy_status="submitted",
            gy_isolated=1, gy_target_dir="stage-1", gy_task_ids="[]", gy_batch_count=0,
        )
        row = db.get_download_request(request_id)
        task = {"id":"gy-1","name":"闭环资源","target_dir":"stage-1","status":0,"progress":0.4,"raw":{}}
        tracker = DownloadTracker()
        with patch.object(tracker, "_update_backend_log"), patch.object(tracker, "_notify_completion"):
            tracker._update_request(row, [], [task], qb_available=False, gy_available=True)
        updated = db.get_download_request(request_id)
        self.assertEqual(updated["gy_task_id"], "gy-1")
        self.assertEqual(updated["gy_task_ids"], '["gy-1"]')
        self.assertEqual(int(updated["gy_batch_count"]), 1)
        with db.get_conn() as conn:
            admission = conn.execute("SELECT status FROM media_download_admissions WHERE id=?", (admission_id,)).fetchone()
        self.assertEqual(admission["status"], "downloading")

    def test_both_is_one_download_admission_and_candidate_exposes_delivery(self):
        subscription_id, candidate_id = self._seed()
        request_id, _ = db.create_download_request("both-key", "magnet", title="闭环资源")
        db.update_download_request(request_id, targets="both", status="submitted", qb_status="submitted", gy_status="submitted")
        downloader = AsyncMock(return_value={
            "ok": True, "duplicate": False, "request_id": request_id,
            "target": "both", "status": "submitted", "succeeded": ["qb", "guangya"], "failed": [],
        })
        with patch("app.modules.media_subscriptions.download_indexer_result_public", new=downloader), patch(
            "app.modules.media_subscriptions.get_indexer_service", return_value=object()
        ):
            result = asyncio.run(MediaSubscriptionService().download_candidate(candidate_id, "both"))
        self.assertEqual(result["request_id"], request_id)
        self.assertEqual(downloader.await_args.args[2], "both")
        service = MediaSubscriptionService()
        rows = service.list_candidates(subscription_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delivery"]["request_status"], "submitted")
        subscription = service.get_subscription(subscription_id)
        self.assertEqual(subscription["workflow"]["primary"], "submitted")
        self.assertEqual(subscription["workflow"]["submitted_count"], 1)
        self.assertEqual(subscription["candidate_count"], 1)
        db.update_download_request(
            request_id, status="downloading", qb_status="downloading", gy_status="downloading"
        )
        db.sync_media_download_admission_for_request(request_id)
        self.assertEqual(
            service.get_subscription(subscription_id)["workflow"]["primary"], "downloading"
        )
        db.update_download_request(
            request_id, status="completed", qb_status="completed", gy_status="completed",
            organize_status="queued", local_import_status="pending",
        )
        db.sync_media_download_admission_for_request(request_id)
        self.assertEqual(
            service.get_subscription(subscription_id)["workflow"]["primary"], "processing"
        )
        with db.get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM media_download_admissions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_manual_review_result_keeps_candidate_and_admission_bound(self):
        subscription_id, candidate_id = self._seed()
        request_id, _ = db.create_download_request(
            "manual-review-result", "magnet", title="结果未知资源",
        )
        db.update_download_request(
            request_id,
            targets="qb", status="manual_review", qb_status="manual_review",
            error="提交结果未知，请人工核验",
        )
        downloader = AsyncMock(return_value={
            "ok": False, "duplicate": False, "request_id": request_id,
            "target": "qb", "status": "manual_review",
            "succeeded": [], "failed": [],
            "error": "提交结果未知，请人工核验",
        })

        with patch(
            "app.modules.media_subscriptions.download_indexer_result_public",
            new=downloader,
        ), patch(
            "app.modules.media_subscriptions.get_indexer_service", return_value=object(),
        ):
            result = asyncio.run(
                MediaSubscriptionService().download_candidate(candidate_id, "qb")
            )

        self.assertEqual(result["status"], "manual_review")
        candidate = db.get_media_subscription_candidate(candidate_id)
        self.assertEqual(candidate["status"], "submitted")
        self.assertEqual(int(candidate["request_id"]), request_id)
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT request_id,status FROM media_download_admissions WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        self.assertEqual(int(admission["request_id"]), request_id)
        self.assertEqual(admission["status"], "processing")

    def test_candidate_count_matches_visible_rows_after_retrying_same_episode(self):
        subscription_id, first_candidate_id = self._seed()
        first_request_id, _ = db.create_download_request(
            "first-failed", "magnet", title="失败候选"
        )
        first_admission_id = db.claim_media_download_admission(
            media_key="tmdb:1:tv:S01E001", tmdb_id="1", media_type="tv",
            subscription_id=subscription_id, candidate_id=first_candidate_id, season=1, episode=1,
            subscription_revision=1,
        )
        db.update_media_subscription_candidate(
            first_candidate_id, status="submitted", request_id=first_request_id
        )
        db.update_media_download_admission(
            first_admission_id, status="failed", request_id=first_request_id, error="下载失败"
        )
        second_candidate_id = db.replace_media_subscription_candidates(
            subscription_id, "tmdb:1:tv:S01E001", season=1, episode=1,
            candidates=[{
                "result_id": "result-2", "title": "替代候选",
                "download_state": "ready", "relevance_score": 98,
            }],
            expires_at="2099-01-01 00:00:00",
        )[0]
        second_request_id, _ = db.create_download_request(
            "second-submitted", "magnet", title="替代候选"
        )
        second_admission_id = db.claim_media_download_admission(
            media_key="tmdb:1:tv:S01E001", tmdb_id="1", media_type="tv",
            subscription_id=subscription_id, candidate_id=second_candidate_id, season=1, episode=1,
            subscription_revision=1,
        )
        db.update_media_subscription_candidate(
            second_candidate_id, status="submitted", request_id=second_request_id
        )
        db.update_media_download_admission(
            second_admission_id, status="submitted", request_id=second_request_id
        )

        service = MediaSubscriptionService()
        candidates = service.list_candidates(subscription_id)
        subscription = service.get_subscription(subscription_id)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(subscription["candidate_count"], len(candidates))
        self.assertEqual(subscription["workflow"]["submitted_candidate_count"], 2)
        self.assertEqual(subscription["workflow"]["primary"], "submitted")

    def test_auto_candidate_workflow_explains_why_download_has_not_started(self):
        subscription_id, _candidate_id = self._seed()
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_subscriptions SET action='auto',status='missing',missing_count=1,"
                "missing_json=? WHERE id=?",
                ('[{"season":1,"episode":1,"label":"S01E01"}]', subscription_id),
            )
        subscription = MediaSubscriptionService().get_subscription(subscription_id)
        self.assertEqual(subscription["workflow"]["primary"], "candidate_waiting_auto")
        self.assertEqual(subscription["workflow"]["available_candidate_count"], 1)
        self.assertEqual(subscription["workflow"]["max_relevance_score"], 99)
        self.assertEqual(subscription["candidate_count"], 1)
