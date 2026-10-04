from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from app import database as db
from app.clients.guangya import GuangYaClient
from app.modules import download_dispatcher
from app.modules.download_dispatcher import DownloadInput, request_key
from app.modules.download_tracker import DownloadTracker
from app.indexers.downloads import _persist_and_dispatch
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

    def _seed_failed_subscription_request(self, suffix: int = 86034):
        tmdb_id = str(suffix)
        media_key = f"tmdb:{tmdb_id}:tv:S01E001"
        subscription_id = db.add_media_subscription(
            provider="tmdb", external_id=tmdb_id, tmdb_id=tmdb_id, media_type="tv",
            title="重试准入闭环", monitor_mode="missing", action="confirm",
            download_target="qb", check_interval_minutes=4320,
        )
        candidate_id = db.replace_media_subscription_candidates(
            subscription_id, media_key, season=1, episode=1,
            candidates=[{
                "result_id": f"retry-{suffix}", "title": "同集旧资源",
                "download_state": "ready", "relevance_score": 99,
            }],
            expires_at="2099-01-01 00:00:00",
        )[0]
        admission_id = db.claim_media_download_admission(
            media_key=media_key, tmdb_id=tmdb_id, media_type="tv",
            subscription_id=subscription_id, candidate_id=candidate_id,
            season=1, episode=1, subscription_revision=1,
        )
        self.assertTrue(admission_id)
        self.assertTrue(db.begin_media_download_dispatch(
            int(admission_id), subscription_id=subscription_id,
            subscription_revision=1,
        ))
        item = DownloadInput(
            kind="magnet", title="同集旧资源",
            source_value=f"magnet:?xt=urn:btih:{suffix:040x}",
        )
        created = download_dispatcher.create_request(
            item, "", "subscription", origin="indexer:test", admission_id=admission_id,
        )
        request_id = int(created["id"])
        db.update_media_subscription_candidate(
            candidate_id, status="submitted", request_id=request_id,
        )
        db.update_download_request(
            request_id, targets="qb", status="failed", qb_status="failed",
            error="模拟的确定失败",
        )
        db.sync_media_download_admission_for_request(request_id)
        return subscription_id, candidate_id, admission_id, request_id, media_key

    def test_new_subscription_admission_binds_when_same_hash_failed_owner_is_archived(self):
        subscription, _old_candidate, old_admission, old_request, media_key = (
            self._seed_failed_subscription_request(86080)
        )
        candidate = db.replace_media_subscription_candidates(
            subscription, media_key, season=1, episode=1,
            candidates=[{
                "result_id": "retry-new-candidate", "title": "同 hash 新候选",
                "download_state": "ready",
            }],
            expires_at="2099-01-01 00:00:00",
        )[0]
        new_admission = db.claim_media_download_admission(
            media_key=media_key, tmdb_id="86080", media_type="tv",
            subscription_id=subscription, candidate_id=candidate,
            season=1, episode=1, subscription_revision=1,
        )
        self.assertTrue(new_admission)
        self.assertTrue(db.begin_media_download_dispatch(
            int(new_admission), subscription_id=subscription, subscription_revision=1,
        ))
        source = db.get_download_request(old_request)
        item = DownloadInput(
            kind="magnet", title=str(source["title"]),
            source_value=str(source["source_value"]),
        )

        created = download_dispatcher.create_request(
            item, "", "subscription:new-candidate", origin="indexer:test",
            admission_id=int(new_admission),
        )

        self.assertTrue(created["created"])
        successor_id = int(created["id"])
        self.assertNotEqual(successor_id, old_request)
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id,request_id,status FROM media_download_admissions ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [(int(row["id"]), int(row["request_id"]), row["status"]) for row in rows],
            [
                (old_admission, old_request, "failed"),
                (int(new_admission), successor_id, "dispatching"),
            ],
        )

    def test_manual_same_hash_readd_is_not_fenced_by_paused_subscription(self):
        _subscription, _candidate, admission_id, source_id, _media_key = (
            self._seed_failed_subscription_request(86081)
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_subscriptions SET enabled=0,status='paused',revision=revision+1 "
                "WHERE id=(SELECT subscription_id FROM media_download_admissions WHERE id=?)",
                (admission_id,),
            )
        source = db.get_download_request(source_id)
        item = DownloadInput(
            kind="magnet", title=str(source["title"]),
            source_value=str(source["source_value"]),
        )

        created = download_dispatcher.create_request(
            item, "", "manual-readd", origin="web",
        )

        self.assertTrue(created["created"])
        self.assertNotEqual(int(created["id"]), source_id)
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()
        self.assertEqual((int(admission["request_id"]), admission["status"]),
                         (source_id, "failed"))

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

        projected, released = db.reconcile_startup_media_download_admissions()

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
        _projected, released = db.reconcile_startup_media_download_admissions()
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
            "app.indexers.downloads.dispatch_request", return_value=dispatched
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

    def test_startup_reconcile_immediately_releases_fresh_bound_pending_request(self):
        subscription_id, candidate_id = self._seed()
        admission_id = self._claim_dispatching(subscription_id, candidate_id)
        request_id, created = db.create_download_request(
            "fresh-bound-pending", "magnet", title="刚创建的待提交资源",
            admission_id=admission_id,
        )
        self.assertTrue(created)

        projected, released = db.reconcile_startup_media_download_admissions()

        self.assertEqual(projected, 0)
        self.assertEqual(released, 1)
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()
        self.assertEqual(int(admission["request_id"]), request_id)
        self.assertEqual(admission["status"], "released")

    def test_dispatch_exception_preserves_request_and_admission_for_manual_review(self):
        subscription_id, candidate_id = self._seed()
        admission_id = self._claim_dispatching(subscription_id, candidate_id)
        item = DownloadInput(
            kind="magnet", title="结果未知资源",
            source_value="magnet:?xt=urn:btih:unknown-result",
        )

        with patch(
            "app.indexers.downloads.dispatch_request",
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
        finalize_calls = 0

        def fail_finalize(_request_id, _claimed_targets, **_fields):
            nonlocal finalize_calls
            finalize_calls += 1
            raise RuntimeError("simulated sqlite write failure")

        with patch(
            "app.modules.download_dispatcher._submit_qb",
            return_value={"ok": True, "task_id": "qb-accepted"},
        ), patch(
            "app.modules.download_dispatcher.db.finalize_download_request_submission",
            side_effect=fail_finalize,
        ):
            _created, request_id, result = _persist_and_dispatch(
                item, "indexer:test", "qb", admission_id=admission_id,
            )

        self.assertEqual(finalize_calls, 1)
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

        projected, released = db.reconcile_startup_media_download_admissions()

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
        async def submit(_service, _result_id, _target, *, admission_id):
            db.bind_media_download_admission_request(admission_id, request_id)
            return downloader.return_value

        downloader = AsyncMock(side_effect=submit, return_value={
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

    def test_completed_download_with_failed_postprocess_releases_media_key(self):
        subscription_id, candidate_id = self._seed()
        request_id, _ = db.create_download_request(
            "postprocess-failure", "magnet", title="后处理失败资源"
        )
        admission_id = db.claim_media_download_admission(
            media_key="tmdb:1:tv:S01E001", tmdb_id="1", media_type="tv",
            subscription_id=subscription_id, candidate_id=candidate_id,
            season=1, episode=1, subscription_revision=1,
        )
        db.update_media_download_admission(
            admission_id, status="submitted", request_id=request_id
        )
        db.update_download_request(
            request_id,
            status="completed",
            qb_status="completed",
            local_import_status="failed",
            local_import_error="目标目录不可写",
        )

        self.assertEqual(db.sync_media_download_admission_for_request(request_id), 1)
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT status,error,completed_at FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()
        self.assertEqual(admission["status"], "failed")
        self.assertIn("本地入库", admission["error"])
        self.assertTrue(admission["completed_at"])

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
        async def submit(_service, _result_id, _target, *, admission_id):
            db.bind_media_download_admission_request(admission_id, request_id)
            return downloader.return_value

        downloader = AsyncMock(side_effect=submit, return_value={
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

    def test_failed_subscription_retry_transfers_admission_before_successor_submit(self):
        subscription_id, candidate_id, admission_id, original_request_id, media_key = (
            self._seed_failed_subscription_request()
        )

        observed_before_submit = []

        def submit_without_network(row, **_kwargs):
            with db.get_conn() as conn:
                admission = conn.execute(
                    "SELECT status,request_id FROM media_download_admissions WHERE id=?",
                    (admission_id,),
                ).fetchone()
                candidate = conn.execute(
                    "SELECT request_id FROM media_subscription_candidates WHERE id=?",
                    (candidate_id,),
                ).fetchone()
            observed_before_submit.append((
                int(row["id"]), admission["status"], admission["request_id"],
                candidate["request_id"],
            ))
            return {"ok": True, "task_id": "b" * 40}

        with (
            patch.object(
                download_dispatcher, "get",
                side_effect=lambda key, default="": "http://qb.invalid" if key == "QB_URL" else default,
            ),
            patch.object(
                download_dispatcher, "analyze_offline_url",
                return_value=type("Decision", (), {"allowed": False, "reason": "disabled"})(),
            ),
            patch.object(download_dispatcher, "_submit_qb", side_effect=submit_without_network),
        ):
            result = download_dispatcher.resubmit_download_request(original_request_id, "qb")

        successor_id = int(result["request_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(observed_before_submit, [(
            successor_id, "dispatching", successor_id, successor_id,
        )])
        self.assertEqual(db.get_download_request(successor_id)["status"], "submitted")

        second_candidate_id = db.replace_media_subscription_candidates(
            subscription_id, media_key, season=1, episode=1,
            candidates=[{
                "result_id": "retry-different-hash", "title": "同集新资源",
                "download_state": "ready", "relevance_score": 99,
            }],
            expires_at="2099-01-01 00:00:00",
        )[0]
        second_admission_id = db.claim_media_download_admission(
            media_key=media_key, tmdb_id="86034", media_type="tv",
            subscription_id=subscription_id, candidate_id=second_candidate_id,
            season=1, episode=1, subscription_revision=1,
        )
        self.assertIsNone(second_admission_id)

    def test_failed_retry_failure_and_unknown_results_keep_subscription_projection(self):
        capabilities = {
            name: {"enabled": True, "reason": ""}
            for name in ("qb", "guangya", "both")
        }
        cases = (
            ({"ok": True, "task_id": "task-ok"}, True, "submitted", "submitted"),
            ({"ok": False, "error": "明确拒绝"}, False, "failed", "failed"),
            ({"ok": False, "failure_code": "qb_outcome_unknown", "error": "超时"},
             False, "submitted", "submitted"),
        )
        for offset, (backend_result, expected_ok, request_status, admission_status) in enumerate(cases):
            with self.subTest(request_status=request_status, admission_status=admission_status):
                _subscription, candidate_id, admission_id, source_id, _media_key = (
                    self._seed_failed_subscription_request(86040 + offset)
                )
                with (
                    patch.object(
                        download_dispatcher, "download_resubmit_capabilities",
                        return_value=capabilities,
                    ),
                    patch.object(
                        download_dispatcher, "_submit_qb", return_value=backend_result,
                    ) as submit,
                ):
                    result = download_dispatcher.resubmit_download_request(source_id, "qb")
                submit.assert_called_once()
                self.assertEqual(bool(result["ok"]), expected_ok)
                self.assertEqual(
                    bool(result.get("outcome_unknown")),
                    backend_result.get("failure_code") == "qb_outcome_unknown",
                )
                successor_id = int(result["request_id"])
                self.assertEqual(db.get_download_request(successor_id)["status"], request_status)
                candidate = db.get_media_subscription_candidate(candidate_id)
                self.assertEqual(int(candidate["request_id"]), successor_id)
                with db.get_conn() as conn:
                    admission = conn.execute(
                        "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                        (admission_id,),
                    ).fetchone()
                self.assertEqual(int(admission["request_id"]), successor_id)
                self.assertEqual(admission["status"], admission_status)
                replacement_candidate = db.replace_media_subscription_candidates(
                    _subscription, _media_key, season=1, episode=1,
                    candidates=[{
                        "result_id": f"retry-alternate-{offset}",
                        "title": "同集不同 hash 候选", "download_state": "ready",
                    }],
                    expires_at="2099-01-01 00:00:00",
                )[0]
                next_admission = db.claim_media_download_admission(
                    media_key=_media_key, tmdb_id=str(86040 + offset), media_type="tv",
                    subscription_id=_subscription, candidate_id=replacement_candidate,
                    season=1, episode=1, subscription_revision=1,
                )
                if admission_status in {"submitted", "processing"}:
                    self.assertIsNone(next_admission)
                else:
                    self.assertIsInstance(next_admission, int)

    def test_manual_review_source_transfers_its_failed_admission(self):
        _subscription, candidate_id, admission_id, source_id, _media_key = (
            self._seed_failed_subscription_request(86049)
        )
        db.update_download_request(
            source_id, status="manual_review", qb_status="manual_review",
            error="历史结果待人工核验",
        )
        db.update_media_download_admission(
            admission_id, status="failed", error="待重试准入",
        )
        capabilities = {
            name: {"enabled": True, "reason": ""}
            for name in ("qb", "guangya", "both")
        }
        observed = []

        def submit(row, **_kwargs):
            with db.get_conn() as conn:
                admission = conn.execute(
                    "SELECT status,request_id FROM media_download_admissions WHERE id=?",
                    (admission_id,),
                ).fetchone()
                candidate = conn.execute(
                    "SELECT request_id FROM media_subscription_candidates WHERE id=?",
                    (candidate_id,),
                ).fetchone()
            observed.append((int(row["id"]), admission["status"], admission["request_id"], candidate["request_id"]))
            return {"ok": True, "task_id": "task-manual-review"}

        with (
            patch.object(
                download_dispatcher, "download_resubmit_capabilities",
                return_value=capabilities,
            ),
            patch.object(download_dispatcher, "_submit_qb", side_effect=submit),
        ):
            result = download_dispatcher.resubmit_download_request(source_id, "qb")
        self.assertTrue(result["ok"], result)
        successor_id = int(result["request_id"])
        self.assertEqual(observed, [(successor_id, "dispatching", successor_id, successor_id)])
        self.assertEqual(db.get_download_request(source_id)["status"], "resubmitted")

    def test_original_source_retry_moves_admission_from_each_archived_failed_successor(self):
        _subscription_id, candidate_id, admission_id, source_id, _media_key = (
            self._seed_failed_subscription_request(86048)
        )
        capabilities = {
            name: {"enabled": True, "reason": ""}
            for name in ("qb", "guangya", "both")
        }
        observed = []

        def record(target, row):
            with db.get_conn() as conn:
                admission = conn.execute(
                    "SELECT status,request_id FROM media_download_admissions WHERE id=?",
                    (admission_id,),
                ).fetchone()
                candidate = conn.execute(
                    "SELECT request_id FROM media_subscription_candidates WHERE id=?",
                    (candidate_id,),
                ).fetchone()
            observed.append((target, int(row["id"]), admission["status"],
                             admission["request_id"], candidate["request_id"]))

        qb_results = iter((
            {"ok": False, "error": "qB 明确失败"},
            {"ok": True, "task_id": "qB-final"},
        ))

        def submit_qb(row, **_kwargs):
            record("qb", row)
            return next(qb_results)

        def submit_guangya(row, **_kwargs):
            record("guangya", row)
            return {"ok": False, "error": "光鸭明确失败"}

        with (
            patch.object(
                download_dispatcher, "download_resubmit_capabilities",
                return_value=capabilities,
            ),
            patch.object(download_dispatcher, "_submit_qb", side_effect=submit_qb),
            patch.object(download_dispatcher, "_submit_guangya", side_effect=submit_guangya),
        ):
            first = download_dispatcher.resubmit_download_request(source_id, "qb")
            first_successor = int(first["request_id"])
            self.assertFalse(first["ok"])
            self.assertEqual(db.get_download_request(source_id)["status"], "failed")
            self.assertEqual(db.get_download_request(first_successor)["status"], "failed")
            with db.get_conn() as conn:
                admission = conn.execute(
                    "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                    (admission_id,),
                ).fetchone()
            self.assertEqual((int(admission["request_id"]), admission["status"]),
                             (first_successor, "failed"))

            second = download_dispatcher.resubmit_download_request(source_id, "guangya")
            second_successor = int(second["request_id"])
            self.assertFalse(second["ok"])
            self.assertEqual(db.get_download_request(source_id)["status"], "failed")
            self.assertEqual(db.get_download_request(second_successor)["status"], "failed")
            with db.get_conn() as conn:
                admission = conn.execute(
                    "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                    (admission_id,),
                ).fetchone()
            self.assertEqual((int(admission["request_id"]), admission["status"]),
                             (second_successor, "failed"))

            third = download_dispatcher.resubmit_download_request(source_id, "qb")

        third_successor = int(third["request_id"])
        self.assertTrue(third["ok"])
        self.assertNotIn(third_successor, {source_id, first_successor, second_successor})
        self.assertEqual(db.get_download_request(source_id)["status"], "resubmitted")
        self.assertEqual(
            [entry[0] for entry in observed], ["qb", "guangya", "qb"],
        )
        self.assertEqual(
            observed,
            [
                ("qb", first_successor, "dispatching", first_successor, first_successor),
                ("guangya", second_successor, "dispatching", second_successor, second_successor),
                ("qb", third_successor, "dispatching", third_successor, third_successor),
            ],
        )
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM download_requests"
            ).fetchone()[0], 4)
        self.assertEqual((int(admission["request_id"]), admission["status"]),
                         (third_successor, "submitted"))
        self.assertEqual(
            int(db.get_media_subscription_candidate(candidate_id)["request_id"]),
            third_successor,
        )

    def test_subscription_revision_and_pause_fence_failed_retry_before_submit(self):
        capabilities = {name: {"enabled": True, "reason": ""}
                        for name in ("qb", "guangya", "both")}
        updates = (
            "UPDATE media_subscriptions SET revision=revision+1 WHERE id=?",
            "UPDATE media_subscriptions SET enabled=0,status='paused',revision=revision+1 WHERE id=?",
        )
        for offset, update in enumerate(updates):
            with self.subTest(update=update):
                subscription, candidate, admission_id, source_id, _key = (
                    self._seed_failed_subscription_request(86050 + offset)
                )
                with db.get_conn() as conn:
                    before = conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0]
                    conn.execute(update, (subscription,))
                with patch.object(
                    download_dispatcher, "download_resubmit_capabilities",
                    return_value=capabilities,
                ), patch.object(download_dispatcher, "_submit_qb") as submit:
                    result = download_dispatcher.resubmit_download_request(source_id, "qb")
                self.assertTrue(result["retry_blocked"])
                self.assertTrue(result["source_attention_preserved"])
                submit.assert_not_called()
                self.assertEqual(db.get_download_request(source_id)["status"], "failed")
                with db.get_conn() as conn:
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM download_requests"
                    ).fetchone()[0], before)
                    row = conn.execute(
                        "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                        (admission_id,),
                    ).fetchone()
                self.assertEqual((int(row["request_id"]), row["status"]),
                                 (source_id, "failed"))
                self.assertEqual(int(db.get_media_subscription_candidate(candidate)["request_id"]),
                                 source_id)

    def test_stale_cancelled_source_retry_is_fenced_in_creation_transaction(self):
        _subscription, _candidate, _admission, source_id, _key = (
            self._seed_failed_subscription_request(86060)
        )
        capabilities = {name: {"enabled": True, "reason": ""}
                        for name in ("qb", "guangya", "both")}
        create = download_dispatcher.create_request

        def cancel_before_create(*args, **kwargs):
            db.update_download_request(source_id, status="cancelled", qb_status="cancelled")
            return create(*args, **kwargs)

        with (
            patch.object(download_dispatcher, "download_resubmit_capabilities", return_value=capabilities),
            patch.object(download_dispatcher, "create_request", side_effect=cancel_before_create),
            patch.object(download_dispatcher, "_submit_qb") as submit,
        ):
            result = download_dispatcher.resubmit_download_request(source_id, "qb")
        self.assertTrue(result["retry_blocked"])
        submit.assert_not_called()
        self.assertEqual(db.get_download_request(source_id)["status"], "cancelled")
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], 1)

    def test_repeated_retry_during_first_submit_uses_same_active_successor(self):
        _subscription, candidate, admission_id, source_id, _key = (
            self._seed_failed_subscription_request(86070)
        )
        capabilities = {name: {"enabled": True, "reason": ""}
                        for name in ("qb", "guangya", "both")}
        nested = []

        def submit(_row, **_kwargs):
            nested.append(download_dispatcher.resubmit_download_request(source_id, "qb"))
            return {"ok": True, "task_id": "task-once"}

        with (
            patch.object(download_dispatcher, "download_resubmit_capabilities", return_value=capabilities),
            patch.object(download_dispatcher, "_submit_qb", side_effect=submit) as backend,
        ):
            first = download_dispatcher.resubmit_download_request(source_id, "qb")
        self.assertTrue(first["ok"])
        backend.assert_called_once()
        self.assertTrue(nested[0]["duplicate"])
        successor = int(first["request_id"])
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0], 2)
            admission = conn.execute(
                "SELECT request_id,status FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()
        self.assertEqual((int(admission["request_id"]), admission["status"]),
                         (successor, "submitted"))
        self.assertEqual(int(db.get_media_subscription_candidate(candidate)["request_id"]), successor)

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
