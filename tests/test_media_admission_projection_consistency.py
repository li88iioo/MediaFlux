"""运行中批量巡检与即时/启动投影使用一致的媒体下载准入状态。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app import database as db
from tests.support import isolated_test_database


class MediaAdmissionProjectionConsistencyTests(unittest.TestCase):
    def test_persisted_failure_labels_do_not_follow_display_label_changes(self):
        for episode, (stage, label) in enumerate((("organize", "自动整理"), ("local_import", "本地入库"), ("strm", "STRM 联动")), 300):
            with self.subTest(stage=stage):
                request, admission, _ = self.seed(episode, status="completed", **{f"{stage}_status": "failed", f"{stage}_error": "failed-stage"})
                db.sync_media_download_admission_for_request(request)
                self.assertEqual(self.view(admission)[:2], ("failed", f"下载后处理失败（{label}）：failed-stage"))

    def test_postprocessing_attention_is_consistent_and_retains_admission_until_recovered(self):
        from app.routes.downloads_api import _attention_stages
        from app.repositories.download_requests import download_request_public_summary
        from app.modules.telegram_download_lifecycle import build_download_lifecycle_event

        cases = (
            ("local_import", "requires_manual", "请确认本地匹配结果"),
            ("strm", "partial", "STRM 同步部分完成，请查看运行记录"),
            ("strm", "partial", "STRM 已完成，媒体库刷新等待后台重试"),
            ("organize", "partial", "整理部分完成，仍有文件需要处理"),
        )
        for episode, (stage, state, reason) in enumerate(cases, 200):
            with self.subTest(stage=stage, state=state, reason=reason):
                fields = dict(status="completed", targets="both", qb_status="completed", gy_status="completed",
                              organize_status="completed", local_import_status="completed", strm_status="completed")
                fields.update({f"{stage}_status": state, f"{stage}_error": reason})
                request, admission, key = self.seed(episode, **fields)
                db.sync_media_download_admission_for_request(request)
                expected = self.view(admission)
                self.assertEqual(expected[0], "processing")
                self.assertIn(reason, expected[1])
                self.assertIsNone(expected[2])
                db.reconcile_startup_media_download_admissions()
                for present in (False, True):
                    db.reconcile_media_download_admissions(self.subscription, {key} if present else set(), expected_revision=1)
                    self.assertEqual(self.view(admission), expected)
                row = db.get_download_request(request)
                self.assertEqual(db.count_download_requests_requiring_attention(request), 1)
                issues = _attention_stages(row)
                self.assertTrue(any(item["key"] == stage and item["error"] == reason for item in issues))
                summary = download_request_public_summary(row)
                self.assertEqual(summary["status"], "completed")
                self.assertEqual(summary["local_import_status"], fields["local_import_status"])
                self.assertIn(reason, summary["error"])
                self.assertTrue(str(build_download_lifecycle_event(row).title).startswith("⚠️"))
                workflow = db.list_media_subscription_workflows([self.subscription])[self.subscription]
                self.assertEqual(workflow["manual_review_count"], 1)
                self.assertEqual(workflow["processing_count"], 0)
                self.assertIn(reason, "；".join(workflow["attention_reasons"]))
                alternative = db.replace_media_subscription_candidates(self.subscription, key, season=1, episode=episode,
                    candidates=[{"result_id": f"alternative-{episode}", "title": key}], expires_at="2099-01-01 00:00:00")[0]
                self.assertIsNone(db.claim_media_download_admission(media_key=key, tmdb_id="1", media_type="tv",
                    subscription_id=self.subscription, candidate_id=alternative, season=1, episode=episode, subscription_revision=1))
                db.update_download_request(request, **{f"{stage}_status": "completed", f"{stage}_error": ""})
                db.reconcile_media_download_admissions(self.subscription, {key}, expected_revision=1)
                self.assertEqual(self.view(admission)[:2], ("completed", ""))
                self.assertEqual(db.count_download_requests_requiring_attention(request), 0)
                saved = db.get_download_request(request)
                assert saved is not None
                self.assertEqual((saved["qb_status"], saved["gy_status"]), ("completed", "completed"))

    def setUp(self):
        self.path = self.enterContext(isolated_test_database("mediaflux.db"))
        self.enterContext(patch.object(db, "now", return_value="2026-09-08 12:00:00"))
        self.subscription = db.add_media_subscription(
            provider="tmdb",
            external_id="1",
            tmdb_id="1",
            media_type="tv",
            title="投影一致性",
            monitor_mode="missing",
            action="confirm",
            download_target="qb",
            check_interval_minutes=60,
        )

    def seed(self, episode=1, **fields):
        key = f"tmdb:1:tv:S01E{episode:03d}"
        candidate = db.replace_media_subscription_candidates(
            self.subscription,
            key,
            season=1,
            episode=episode,
            candidates=[{"result_id": f"candidate-{episode}", "title": key}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        admission = db.claim_media_download_admission(
            media_key=key,
            tmdb_id="1",
            media_type="tv",
            subscription_id=self.subscription,
            candidate_id=candidate,
            season=1,
            episode=episode,
            subscription_revision=1,
        )
        assert admission is not None
        self.assertTrue(
            db.begin_media_download_dispatch(
                admission, subscription_id=self.subscription, subscription_revision=1
            )
        )
        request, _ = db.create_download_request(
            f"projection-{episode}", "magnet", admission_id=admission
        )
        db.update_download_request(request, **fields)
        return request, admission, key

    @staticmethod
    def view(admission):
        with db.get_conn() as conn:
            return tuple(
                conn.execute(
                    "SELECT status,error,completed_at FROM media_download_admissions WHERE id=?",
                    (admission,),
                ).fetchone()
            )

    def test_batch_and_immediate_projection_have_identical_business_results(self):
        cases = [
            {"status": "failed", "error": "明确提交失败"},
            {"status": "manual_review", "error": "远端结果未知，请人工核验"},
            {
                "status": "completed",
                "organize_status": "failed",
                "organize_error": "整理失败",
            },
            {"status": "completed", "organize_started": -1},
            {"status": "completed", "strm_status": "failed", "strm_error": "STRM失败"},
            {"status": "completed", "local_import_status": "failed"},
            {"status": "completed"},
            {"status": "submitted"},
            {"status": "downloading"},
        ]
        for episode, fields in enumerate(cases, 1):
            with self.subTest(fields=fields):
                request, admission, _ = self.seed(episode, **fields)
                self.assertEqual(
                    db.sync_media_download_admission_for_request(request), 1
                )
                expected = self.view(admission)
                assert admission is not None
                db.update_media_download_admission(
                    admission, status="dispatching", error="", completed_at=None
                )
                db.reconcile_media_download_admissions(
                    self.subscription, set(), expected_revision=1
                )
                self.assertEqual(self.view(admission), expected)

    def test_live_timeout_is_visible_without_restart_or_releasing_duplicate_guard(self):
        request, admission, _ = self.seed(status="pending")
        self.assertTrue(db.claim_download_request(request, "qb"))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE download_requests SET qb_submitting_at='2000-01-01 00:00:00' WHERE id=?",
                (request,),
            )
        self.assertEqual(db.recover_stale_submitting_download_requests(), 1)
        self.assertEqual(
            db.reconcile_media_download_admissions(
                self.subscription, set(), expected_revision=1
            ),
            1,
        )
        state, error, completed = self.view(admission)
        self.assertEqual(state, "processing")
        self.assertIn("结果未知", error)
        self.assertIsNone(completed)
        self.assertEqual(
            len(db.list_active_media_download_admissions(self.subscription)), 1
        )

    def test_partial_both_targets_keep_attention_and_duplicate_guard_until_recovered(self):
        for episode, (failed_field, present) in enumerate(
            ((field, present) for field in ('qb_status', 'gy_status') for present in (False, True)), 70
        ):
            with self.subTest(failed_field=failed_field, present=present):
                fields = dict(status='completed', targets='both', qb_status='completed', gy_status='completed',
                              organize_status='completed', local_import_status='completed', strm_status='completed')
                fields[failed_field] = 'failed'
                request, admission, key = self.seed(episode, **fields)
                db.sync_media_download_admission_for_request(request)
                expected = self.view(admission)
                self.assertEqual(expected[0], 'processing')
                self.assertIn('下载目标未全部完成', expected[1])
                self.assertIsNone(expected[2])
                db.reconcile_startup_media_download_admissions()
                db.reconcile_media_download_admissions(self.subscription, {key} if present else set(), expected_revision=1)
                self.assertEqual(self.view(admission), expected)
                workflow = db.list_media_subscription_workflows([self.subscription])[self.subscription]
                self.assertEqual(workflow['manual_review_count'], 1)
                self.assertEqual(workflow['processing_count'], 0)
                alternative = db.replace_media_subscription_candidates(self.subscription, key, season=1, episode=episode,
                    candidates=[{'result_id':f'alternative-{episode}', 'title':key}], expires_at='2099-01-01 00:00:00')[0]
                self.assertIsNone(db.claim_media_download_admission(media_key=key, tmdb_id='1', media_type='tv',
                    subscription_id=self.subscription, candidate_id=alternative, season=1, episode=episode, subscription_revision=1))
                db.update_download_request(request, status='downloading', **{failed_field:'submitted'})
                db.sync_media_download_admission_for_request(request)
                self.assertEqual(self.view(admission)[:2], ('downloading', ''))
                saved = db.get_download_request(request)
                assert saved is not None
                self.assertEqual(saved['gy_status' if failed_field == 'qb_status' else 'qb_status'], 'completed')
                db.update_download_request(request, status='completed', **{failed_field:'completed'})
                db.reconcile_media_download_admissions(self.subscription, {key}, expected_revision=1)
                self.assertEqual(self.view(admission)[:2], ('completed', ''))
                self.assertEqual(db.list_media_subscription_workflows([self.subscription])[self.subscription]['manual_review_count'], 0)

    def test_historical_completed_admission_does_not_hide_failed_requested_target(self):
        _, admission, _ = self.seed(status='completed', targets='both', qb_status='completed', gy_status='failed')
        db.update_media_download_admission(admission, status='completed', error='')
        workflow = db.list_media_subscription_workflows([self.subscription])[self.subscription]
        self.assertEqual(workflow['manual_review_count'], 1)
        self.assertEqual(self.view(admission)[:2], ('completed', ''))  # 展示事实，不改写历史行。

    def test_unrequested_backend_failure_does_not_block_local_completion(self):
        request, admission, key = self.seed(status='completed', targets='qb', qb_status='completed', gy_status='failed')
        db.sync_media_download_admission_for_request(request)
        self.assertEqual(self.view(admission)[:2], ('processing', ''))
        db.reconcile_media_download_admissions(self.subscription, {key}, expected_revision=1)
        self.assertEqual(self.view(admission)[:2], ('completed', ''))
        self.assertEqual(db.list_media_subscription_workflows([self.subscription])[self.subscription]['manual_review_count'], 0)

    def test_mixed_batch_keeps_local_presence_precedence_and_bounded_reads(self):
        from contextlib import contextmanager

        entries = [
            self.seed(
                i, status=("manual_review" if i % 2 else "failed"), error="batch-error"
            )
            for i in range(1, 51)
        ]
        local_keys = {key for _, _, key in entries[:3]}
        statements = []
        actual = db.get_conn

        @contextmanager
        def traced():
            with actual() as conn:
                conn.set_trace_callback(statements.append)
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)

        with patch.object(db, "get_conn", traced):
            self.assertEqual(
                db.reconcile_media_download_admissions(
                    self.subscription, local_keys, expected_revision=1
                ),
                50,
            )
        reads = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(reads), 1, reads)
        for index, (_, admission, _) in enumerate(entries):
            status, error, completed = self.view(admission)
            self.assertEqual(
                status,
                "completed"
                if index < 3
                else "processing"
                if index % 2 == 0
                else "failed",
            )
            self.assertEqual(completed is not None, status != "processing")
            if status == "processing":
                self.assertEqual(error, "batch-error")

    def test_restart_and_zip_recovery_preserve_unknown_result_without_duplicate_admission(
        self,
    ):
        import subprocess
        import sys

        from app.modules import backup
        from tests.test_ten_pass_process_restore_audit import runtime_paths

        request, admission, key = self.seed(status="pending")
        paths = runtime_paths(self.path)
        self.assertEqual(self.path, paths.database_path)
        script = r"""
import tests
import os, sys, socket
from app import database as db
socket.socket.connect = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network forbidden"))
db.configure_database(sys.argv[1], test_mode=True)
assert db.claim_download_request(int(sys.argv[2]), "qb")
with db.get_conn() as conn:
 conn.execute("UPDATE download_requests SET qb_submitting_at='2000-01-01 00:00:00' WHERE id=?", (int(sys.argv[2]),))
assert db.recover_stale_submitting_download_requests() == 1
os._exit(43)
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(paths.database_path), str(request)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(child.returncode, 43, child.stderr)
        archive = backup.create_backup(paths, reason="unprojected-unknown")
        db.init_db()
        self.assertEqual(
            db.reconcile_media_download_admissions(
                self.subscription, set(), expected_revision=1
            ),
            1,
        )
        expected = self.view(admission)
        self.assertEqual(expected[0], "processing")
        self.assertIn("结果未知", expected[1])
        backup.restore_backup(paths, archive)
        db.init_db()
        projected, released = db.reconcile_startup_media_download_admissions()
        self.assertEqual((projected, released), (1, 0))
        self.assertEqual(self.view(admission), expected)
        self.assertEqual(
            len(db.list_active_media_download_admissions(self.subscription)), 1
        )
        self.assertEqual(
            db.reconcile_media_download_admissions(
                self.subscription, {key}, expected_revision=1
            ),
            1,
        )
        self.assertEqual(self.view(admission)[0], "completed")
