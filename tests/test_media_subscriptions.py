"""媒体订阅数据一致性、检查失效与安全下载准入测试。"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import database as db
from app.modules.media_subscriptions import (
    MediaSubscriptionError,
    MediaSubscriptionService,
    _ExpectedMedia,
    _rotated_missing_targets,
)
from tests.support import IsolatedDatabaseTestCase


class MediaSubscriptionTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_download_admissions")
            conn.execute("DELETE FROM media_subscription_candidates")
            conn.execute("DELETE FROM media_subscription_runs")
            conn.execute("DELETE FROM media_subscriptions")

    @staticmethod
    def _seed_subscription(*, tmdb_id: str = "86034", media_type: str = "tv") -> int:
        return db.add_media_subscription(
            provider="tmdb",
            external_id=tmdb_id,
            tmdb_id=tmdb_id,
            media_type=media_type,
            title="平凡职业造就世界最强" if media_type == "tv" else "沙丘",
            year="2019" if media_type == "tv" else "2021",
            monitor_mode="missing",
            action="confirm",
            download_target="guangya",
            check_interval_minutes=60,
        )

    @staticmethod
    def _add_candidates(subscription_id: int, *, media_key: str, result_ids: tuple[str, ...]) -> list[int]:
        return db.replace_media_subscription_candidates(
            subscription_id,
            media_key,
            season=1,
            episode=1,
            candidates=[
                {
                    "result_id": result_id,
                    "site_id": "mikan",
                    "site_name": "Mikan",
                    "title": f"资源 {result_id}",
                    "size_text": "1.2 GB",
                    "size_bytes": 1_288_490_188,
                    "seeders": 12,
                    "published_at": "2026-08-09 10:00:00",
                    "relevance_score": 98,
                    "download_state": "available",
                    "match_reasons": ["精确季集"],
                }
                for result_id in result_ids
            ],
            expires_at="2099-01-01 00:00:00",
        )

    def test_create_normalizes_identity_and_atomically_upserts_same_tmdb_media(self) -> None:
        service = MediaSubscriptionService()
        detail = {
            "name": "平凡职业造就世界最强",
            "original_name": "ありふれた職業で世界最強",
            "first_air_date": "2019-07-08",
            "poster_path": "/poster.jpg",
        }
        with patch("app.modules.media_subscriptions.TMDBClient.detail", return_value=detail):
            first = asyncio.run(service.create_subscription({
                "provider": "tmdb",
                "external_id": "00086034",
                "tmdb_id": "00086034",
                "media_type": "tv",
                "monitor_mode": "selected",
                "seasons": [2, "1", 2],
                "sites": ["Mikan", "mikan", "ani"],
                "action": "confirm",
            }))
            second = asyncio.run(service.create_subscription({
                "tmdb_id": "86034",
                "media_type": "tv",
                "monitor_mode": "selected",
                "seasons": [3],
                "sites": ["mikan"],
                "action": "notify",
            }))

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["subscription"]["id"], second["subscription"]["id"])
        self.assertEqual(second["subscription"]["tmdb_id"], "86034")
        self.assertEqual(second["subscription"]["seasons"], [3])
        self.assertEqual(second["subscription"]["sites"], ["mikan"])
        self.assertEqual(second["subscription"]["action"], "notify")
        self.assertEqual(int(second["subscription"]["check_interval_minutes"]), 4320)
        self.assertEqual(int(second["subscription"]["revision"]), 2)
        self.assertEqual(db.count_media_subscriptions(), 1)

    def test_movie_and_update_validation_reject_unsafe_or_unknown_fields(self) -> None:
        service = MediaSubscriptionService()
        detail = {
            "title": "沙丘",
            "original_title": "Dune",
            "release_date": "2021-09-15",
            "poster_path": "/dune.jpg",
        }
        with patch("app.modules.media_subscriptions.TMDBClient.detail", return_value=detail):
            with self.assertRaisesRegex(MediaSubscriptionError, "电影订阅不支持"):
                asyncio.run(service.create_subscription({
                    "tmdb_id": "438631",
                    "media_type": "movie",
                    "monitor_mode": "selected",
                    "seasons": [1],
                }))
        subscription_id = self._seed_subscription()
        with self.assertRaisesRegex(MediaSubscriptionError, "不支持的订阅参数"):
            service.update_subscription(subscription_id, {"title": "越权修改"})

    def test_interval_writes_only_accept_three_or_seven_days_and_preserve_legacy_values(self) -> None:
        service = MediaSubscriptionService()
        subscription_id = self._seed_subscription()

        unchanged = service.update_subscription(subscription_id, {"action": "notify"})
        self.assertEqual(int(unchanged["check_interval_minutes"]), 60)

        for interval in (4320, 10080):
            updated = service.update_subscription(
                subscription_id, {"check_interval_minutes": interval}
            )
            self.assertEqual(int(updated["check_interval_minutes"]), interval)

        for interval in (60, 1440, 4321):
            with self.subTest(interval=interval):
                with self.assertRaisesRegex(MediaSubscriptionError, "仅支持每 3 天或每 7 天"):
                    service.update_subscription(
                        subscription_id, {"check_interval_minutes": interval}
                    )
        with self.assertRaisesRegex(MediaSubscriptionError, "检查间隔必须是整数"):
            service.update_subscription(
                subscription_id, {"check_interval_minutes": "hourly"}
            )

    def test_non_tmdb_identity_requires_confirmed_mapping(self) -> None:
        service = MediaSubscriptionService()
        with patch("app.modules.media_subscriptions.TMDBClient.detail") as detail:
            with self.assertRaises(MediaSubscriptionError) as ctx:
                asyncio.run(service.create_subscription({
                    "provider": "bangumi",
                    "external_id": "12345",
                    "tmdb_id": "86034",
                    "media_type": "tv",
                }))
        self.assertEqual(ctx.exception.code, "mapping_required")
        detail.assert_not_called()

    def test_config_update_invalidates_old_run_candidates_and_download_intent(self) -> None:
        service = MediaSubscriptionService()
        subscription_id = self._seed_subscription()
        media_key = "tmdb:86034:tv:S01E001"
        candidate_id = self._add_candidates(
            subscription_id, media_key=media_key, result_ids=("result-a",)
        )[0]
        run_id = db.claim_media_subscription_check_run(subscription_id, "manual")
        self.assertIsNotNone(run_id)
        revision = int(db.get_media_subscription(subscription_id)["revision"])
        admission_id = db.claim_media_download_admission(
            media_key=media_key,
            tmdb_id="86034",
            media_type="tv",
            subscription_id=subscription_id,
            candidate_id=candidate_id,
            season=1,
            episode=1,
            subscription_revision=revision,
            require_active_check=True,
        )
        self.assertIsInstance(admission_id, int)
        self.assertGreater(int(admission_id or 0), 0)

        updated = service.update_subscription(subscription_id, {"action": "notify"})
        self.assertEqual(updated["status"], "new")
        self.assertEqual(int(updated["revision"]), revision + 1)
        self.assertEqual(db.get_media_subscription_candidate(candidate_id)["status"], "expired")
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT status FROM media_download_admissions WHERE id=?", (admission_id,)
            ).fetchone()
            run = conn.execute(
                "SELECT status FROM media_subscription_runs WHERE id=?", (run_id,)
            ).fetchone()
        self.assertEqual(admission["status"], "cancelled")
        self.assertEqual(run["status"], "cancelled")
        committed = db.finalize_media_subscription_check(
            subscription_id,
            int(run_id),
            status="satisfied",
            run_status="satisfied",
            summary="旧检查结果",
            payload={"status": "satisfied"},
            interval_minutes=60,
            expected_count=1,
            local_count=1,
            missing_count=0,
            missing_json="[]",
            result_json='{"status":"satisfied"}',
            subscription_revision=revision,
        )
        self.assertFalse(committed)
        self.assertEqual(db.get_media_subscription(subscription_id)["status"], "new")

    def test_stale_run_cannot_overwrite_or_dispatch_after_reclaim(self) -> None:
        subscription_id = self._seed_subscription()
        media_key = "tmdb:86034:tv:S01E001"
        candidate_id = self._add_candidates(
            subscription_id, media_key=media_key, result_ids=("stale-run",)
        )[0]
        stale_run_id = db.claim_media_subscription_check_run(subscription_id, "scheduler")
        self.assertIsNotNone(stale_run_id)
        revision = int(db.get_media_subscription(subscription_id)["revision"])
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE media_subscriptions SET updated_at=datetime('now','localtime','-31 minutes') "
                "WHERE id=?",
                (subscription_id,),
            )

        self.assertEqual(db.recover_stale_media_subscription_checks(stale_minutes=30), 1)
        current_run_id = db.claim_media_subscription_check_run(subscription_id, "retry")
        self.assertIsNotNone(current_run_id)
        self.assertNotEqual(stale_run_id, current_run_id)
        self.assertFalse(db.media_subscription_check_is_active(
            subscription_id, revision, run_id=int(stale_run_id)
        ))
        self.assertTrue(db.media_subscription_check_is_active(
            subscription_id, revision, run_id=int(current_run_id)
        ))

        stale_admission = db.claim_media_download_admission(
            media_key=media_key,
            tmdb_id="86034",
            media_type="tv",
            subscription_id=subscription_id,
            candidate_id=candidate_id,
            season=1,
            episode=1,
            subscription_revision=revision,
            require_active_check=True,
            check_run_id=int(stale_run_id),
        )
        self.assertEqual(stale_admission, 0)

        stale_committed = db.finalize_media_subscription_check(
            subscription_id,
            int(stale_run_id),
            status="satisfied",
            run_status="satisfied",
            summary="迟到的旧检查结果",
            payload={"status": "satisfied"},
            interval_minutes=60,
            expected_count=1,
            local_count=1,
            missing_count=0,
            missing_json="[]",
            result_json='{"status":"satisfied","run":"stale"}',
            subscription_revision=revision,
        )
        self.assertFalse(stale_committed)
        self.assertEqual(db.get_media_subscription(subscription_id)["status"], "checking")

        current_committed = db.finalize_media_subscription_check(
            subscription_id,
            int(current_run_id),
            status="missing",
            run_status="missing",
            summary="当前检查结果",
            payload={"status": "missing"},
            interval_minutes=60,
            expected_count=1,
            local_count=0,
            missing_count=1,
            missing_json='[{"media_key":"tmdb:86034:tv:S01E001"}]',
            result_json='{"status":"missing","run":"current"}',
            subscription_revision=revision,
        )
        self.assertTrue(current_committed)
        current = db.get_media_subscription(subscription_id)
        self.assertEqual(current["status"], "missing")
        self.assertIn('"run":"current"', current["result_json"])

    def test_soft_delete_hides_subscription_but_preserves_audit_rows(self) -> None:
        subscription_id = self._seed_subscription()
        run_id = db.add_media_subscription_run(subscription_id, "manual")
        db.finish_media_subscription_run(run_id, status="satisfied", summary="已满足")

        self.assertTrue(db.delete_media_subscription(subscription_id))
        self.assertIsNone(db.get_media_subscription(subscription_id))
        deleted = db.get_media_subscription(subscription_id, include_deleted=True)
        self.assertIsNotNone(deleted)
        self.assertEqual(int(deleted["enabled"]), 0)
        self.assertTrue(deleted["deleted_at"])
        runs = db.list_media_subscription_runs(subscription_id=subscription_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["summary"], "已满足")

    def test_download_admission_deduplicates_same_media_key_and_reports_failure(self) -> None:
        service = MediaSubscriptionService()
        subscription_id = self._seed_subscription()
        media_key = "tmdb:86034:tv:S01E001"
        first_id, second_id = self._add_candidates(
            subscription_id, media_key=media_key, result_ids=("result-a", "result-b")
        )
        with patch(
            "app.modules.media_subscriptions.download_indexer_result_public",
            new=AsyncMock(return_value={"ok": True, "duplicate": False, "request_id": 0}),
        ), patch("app.modules.media_subscriptions.get_indexer_service", return_value=object()):
            first = asyncio.run(service.download_candidate(first_id, "guangya"))
            duplicate = asyncio.run(service.download_candidate(second_id, "guangya"))
        self.assertTrue(first["ok"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(db.list_active_media_download_admissions(subscription_id)), 1)

        other_key = "tmdb:86034:tv:S01E002"
        failed_candidate = db.replace_media_subscription_candidates(
            subscription_id,
            other_key,
            season=1,
            episode=2,
            candidates=[{
                "result_id": "result-failed",
                "site_id": "mikan",
                "site_name": "Mikan",
                "title": "失败资源",
                "relevance_score": 99,
                "download_state": "available",
            }],
            expires_at="2099-01-01 00:00:00",
        )[0]
        with patch(
            "app.modules.media_subscriptions.download_indexer_result_public",
            new=AsyncMock(return_value={"ok": False, "duplicate": False, "error": "下载器拒绝"}),
        ), patch("app.modules.media_subscriptions.get_indexer_service", return_value=object()):
            with self.assertRaises(MediaSubscriptionError) as ctx:
                asyncio.run(service.download_candidate(failed_candidate, "qb"))
        self.assertEqual(ctx.exception.code, "download_failed")
        with db.get_conn() as conn:
            failure = conn.execute(
                "SELECT status,error FROM media_download_admissions WHERE candidate_id=?",
                (failed_candidate,),
            ).fetchone()
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["error"], "下载器拒绝")

        detailed_key = "tmdb:86034:tv:S01E003"
        detailed_candidate = db.replace_media_subscription_candidates(
            subscription_id,
            detailed_key,
            season=1,
            episode=3,
            candidates=[{
                "result_id": "result-detailed-failure",
                "site_id": "mikan",
                "site_name": "Mikan",
                "title": "磁力解析失败资源",
                "relevance_score": 99,
                "download_state": "available",
            }],
            expires_at="2099-01-01 00:00:00",
        )[0]
        request_id, created = db.create_download_request(
            "candidate-detailed-failure", "magnet", title="磁力解析失败资源",
        )
        self.assertTrue(created)
        db.update_download_request(
            request_id,
            targets="guangya",
            status="failed",
            gy_status="failed",
            error="guangya: 磁力资源连续 4 次未解析到可验证文件列表 api_key=super-secret",
        )
        with patch(
            "app.modules.media_subscriptions.download_indexer_result_public",
            new=AsyncMock(return_value={
                "ok": False,
                "duplicate": False,
                "request_id": request_id,
                "error": "下载提交失败",
            }),
        ), patch("app.modules.media_subscriptions.get_indexer_service", return_value=object()):
            with self.assertRaises(MediaSubscriptionError) as detailed_ctx:
                asyncio.run(service.download_candidate(detailed_candidate, "guangya"))
        self.assertIn("连续 4 次未解析到可验证文件列表", str(detailed_ctx.exception))
        self.assertNotIn("super-secret", str(detailed_ctx.exception))
        self.assertIn("api_key=********", str(detailed_ctx.exception))
        with db.get_conn() as conn:
            detailed_failure = conn.execute(
                "SELECT status,error FROM media_download_admissions WHERE candidate_id=?",
                (detailed_candidate,),
            ).fetchone()
        self.assertEqual(detailed_failure["status"], "failed")
        self.assertIn("连续 4 次未解析到可验证文件列表", detailed_failure["error"])
        self.assertNotIn("super-secret", detailed_failure["error"])
        self.assertIn("api_key=********", detailed_failure["error"])

    def test_long_season_search_rotation_advances_wraps_and_survives_missing_cursor_item(self) -> None:
        missing = [
            _ExpectedMedia(
                media_key=f"tmdb:86034:tv:S01E{episode:03d}",
                season=1,
                episode=episode,
            )
            for episode in range(1, 25)
        ]
        first, first_cursor = _rotated_missing_targets(
            missing, None, revision=3, limit=12
        )
        second, second_cursor = _rotated_missing_targets(
            missing, first_cursor, revision=3, limit=12
        )
        wrapped, _ = _rotated_missing_targets(
            missing, second_cursor, revision=3, limit=12
        )

        self.assertEqual([item.episode for item in first], list(range(1, 13)))
        self.assertEqual([item.episode for item in second], list(range(13, 25)))
        self.assertEqual([item.episode for item in wrapped], list(range(1, 13)))

        without_cursor_item = [item for item in missing if item.episode != 12]
        after_disappeared_cursor, _ = _rotated_missing_targets(
            without_cursor_item, first_cursor, revision=3, limit=3
        )
        self.assertEqual([item.episode for item in after_disappeared_cursor], [13, 14, 15])

        reset_after_revision, _ = _rotated_missing_targets(
            missing, first_cursor, revision=4, limit=3
        )
        self.assertEqual([item.episode for item in reset_after_revision], [1, 2, 3])

    def test_stale_revision_cancels_dispatch_before_external_download(self) -> None:
        service = MediaSubscriptionService()
        subscription_id = self._seed_subscription()
        candidate_id = self._add_candidates(
            subscription_id,
            media_key="tmdb:86034:tv:S01E001",
            result_ids=("stale",),
        )[0]
        row = db.get_media_subscription(subscription_id)
        old_revision = int(row["revision"])
        admission_id = db.claim_media_download_admission(
            media_key="tmdb:86034:tv:S01E001",
            tmdb_id="86034",
            media_type="tv",
            subscription_id=subscription_id,
            candidate_id=candidate_id,
            season=1,
            episode=1,
            subscription_revision=old_revision,
        )
        self.assertIsInstance(admission_id, int)

        service.update_subscription(subscription_id, {"action": "notify"})
        allowed = db.begin_media_download_dispatch(
            int(admission_id),
            subscription_id=subscription_id,
            subscription_revision=old_revision,
        )
        self.assertFalse(allowed)
        with db.get_conn() as conn:
            admission = conn.execute(
                "SELECT status,error FROM media_download_admissions WHERE id=?",
                (int(admission_id),),
            ).fetchone()
        self.assertEqual(admission["status"], "cancelled")
        self.assertIn("配置已变更", admission["error"])

    def test_candidate_scope_and_unavailable_status_fail_closed(self) -> None:
        service = MediaSubscriptionService()
        first_id = self._seed_subscription(tmdb_id="86034")
        second_id = self._seed_subscription(tmdb_id="127532")
        candidate_id = self._add_candidates(
            second_id,
            media_key="tmdb:127532:tv:S01E001",
            result_ids=("other",),
        )[0]
        claimed = db.claim_media_download_admission(
            media_key="tmdb:127532:tv:S01E001",
            tmdb_id="127532",
            media_type="tv",
            subscription_id=first_id,
            candidate_id=candidate_id,
            season=1,
            episode=1,
            subscription_revision=int(db.get_media_subscription(first_id)["revision"]),
        )
        self.assertEqual(claimed, 0)

        db.update_media_subscription_candidate(candidate_id, status="expired")
        downloader = AsyncMock()
        with patch(
            "app.modules.media_subscriptions.download_indexer_result_public", downloader
        ):
            with self.assertRaises(MediaSubscriptionError) as ctx:
                asyncio.run(service.download_candidate(candidate_id, "guangya"))
        self.assertEqual(ctx.exception.code, "unavailable")
        downloader.assert_not_awaited()

    def test_cancelled_scheduler_never_submits_external_download(self) -> None:
        service = MediaSubscriptionService()
        subscription_id = self._seed_subscription()
        candidate_id = self._add_candidates(
            subscription_id,
            media_key="tmdb:86034:tv:S01E001",
            result_ids=("cancelled",),
        )[0]
        cancel_event = threading.Event()
        cancel_event.set()
        downloader = AsyncMock()
        with patch(
            "app.modules.media_subscriptions.download_indexer_result_public", downloader
        ):
            with self.assertRaises(MediaSubscriptionError) as ctx:
                asyncio.run(service.download_candidate(
                    candidate_id,
                    "guangya",
                    cancel_event=cancel_event,
                ))
        self.assertEqual(ctx.exception.code, "cancelled")
        downloader.assert_not_awaited()
        self.assertEqual(db.list_active_media_download_admissions(subscription_id), [])

    def test_async_cancellation_marks_subscription_run_cancelled(self) -> None:
        service = MediaSubscriptionService()
        subscription_id = self._seed_subscription()

        with patch.object(
            service,
            "_check_tv",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(service.check_subscription(subscription_id))

        runs = db.list_media_subscription_runs(subscription_id=subscription_id)
        self.assertEqual(runs[0]["status"], "cancelled")
        subscription = db.get_media_subscription(subscription_id)
        self.assertEqual(subscription["status"], "new")
        self.assertEqual(subscription["last_error"], "")
        self.assertIn(
            subscription_id,
            [int(row["id"]) for row in db.list_due_media_subscriptions()],
        )
        reclaimed = db.claim_media_subscription_check_run(subscription_id, "retry")
        self.assertIsNotNone(reclaimed)

    def test_cancel_event_releases_checking_lease_immediately(self) -> None:
        service = MediaSubscriptionService()
        subscription_id = self._seed_subscription()
        cancel_event = threading.Event()
        cancel_event.set()

        with self.assertRaises(MediaSubscriptionError) as ctx:
            asyncio.run(
                service.check_subscription(
                    subscription_id, trigger="scheduler", cancel_event=cancel_event
                )
            )

        self.assertEqual(ctx.exception.code, "cancelled")
        runs = db.list_media_subscription_runs(subscription_id=subscription_id)
        self.assertEqual(runs[0]["status"], "cancelled")
        self.assertEqual(db.get_media_subscription(subscription_id)["status"], "new")
        self.assertIn(
            subscription_id,
            [int(row["id"]) for row in db.list_due_media_subscriptions()],
        )

    def test_stale_cancel_cannot_release_new_configuration(self) -> None:
        subscription_id = self._seed_subscription()
        run_id = db.claim_media_subscription_check_run(subscription_id, "manual")
        self.assertIsNotNone(run_id)
        old_revision = int(db.get_media_subscription(subscription_id)["revision"])
        service = MediaSubscriptionService()
        service.update_subscription(subscription_id, {"action": "notify"})

        self.assertFalse(
            db.cancel_media_subscription_run(
                int(run_id),
                subscription_id=subscription_id,
                subscription_revision=old_revision,
                reason="旧检查已取消",
            )
        )
        current = db.get_media_subscription(subscription_id)
        self.assertEqual(current["action"], "notify")
        self.assertEqual(current["status"], "new")

    def test_stale_failed_check_preserves_new_configuration(self) -> None:
        service = MediaSubscriptionService()
        subscription_id = self._seed_subscription()
        run_id = db.claim_media_subscription_check_run(subscription_id, "manual")
        self.assertIsNotNone(run_id)
        old_revision = int(db.get_media_subscription(subscription_id)["revision"])
        service.update_subscription(subscription_id, {"action": "notify"})

        committed = db.fail_media_subscription_check(
            subscription_id,
            int(run_id),
            interval_minutes=60,
            error="旧检查失败",
            subscription_revision=old_revision,
        )
        self.assertFalse(committed)
        current = db.get_media_subscription(subscription_id)
        self.assertEqual(current["action"], "notify")
        self.assertEqual(current["status"], "new")
        with db.get_conn() as conn:
            run = conn.execute(
                "SELECT status FROM media_subscription_runs WHERE id=?", (int(run_id),)
            ).fetchone()
        self.assertEqual(run["status"], "cancelled")


class MediaSubscriptionHotspotTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_download_admissions")
            conn.execute("DELETE FROM media_subscription_candidates")
            conn.execute("DELETE FROM media_subscription_runs")
            conn.execute("DELETE FROM media_subscriptions")

    def _seed(self) -> tuple[int, int]:
        subscription_id = MediaSubscriptionTests._seed_subscription()
        candidate_id = MediaSubscriptionTests._add_candidates(
            subscription_id,
            media_key="tmdb:86034:tv:S01E001",
            result_ids=("hotspot",),
        )[0]
        revision = int(db.get_media_subscription(subscription_id)["revision"])
        admission_id = db.claim_media_download_admission(
            media_key="tmdb:86034:tv:S01E001",
            tmdb_id="86034",
            media_type="tv",
            subscription_id=subscription_id,
            candidate_id=candidate_id,
            season=1,
            episode=1,
            subscription_revision=revision,
        )
        return subscription_id, int(admission_id)

    def test_reconcile_admissions_uses_one_repository_boundary(self) -> None:
        subscription_id, admission_id = self._seed()
        updated = db.reconcile_media_download_admissions(
            subscription_id,
            {"tmdb:86034:tv:S01E001"},
        )
        self.assertEqual(updated, 1)
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT status,completed_at,error FROM media_download_admissions WHERE id=?",
                (admission_id,),
            ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertTrue(row["completed_at"])
        self.assertEqual(row["error"], "")

    def test_sync_admissions_is_offloaded_from_event_loop(self) -> None:
        service = MediaSubscriptionService()
        started = threading.Event()
        release = threading.Event()

        seen_kwargs: dict[str, int] = {}

        def blocking_reconcile(subscription_id, local_keys, **kwargs):
            seen_kwargs.update(kwargs)
            started.set()
            release.wait(timeout=2)
            return 0

        async def scenario():
            with patch(
                "app.modules.media_subscriptions.db.reconcile_media_download_admissions",
                side_effect=blocking_reconcile,
            ):
                task = asyncio.create_task(
                    service._sync_admissions({"id": 7, "revision": 1}, set())
                )
                await asyncio.to_thread(started.wait, 1)
                ticked = False
                await asyncio.sleep(0)
                ticked = True
                release.set()
                await task
                return ticked

        self.assertTrue(asyncio.run(scenario()))
        self.assertEqual(seen_kwargs, {"expected_revision": 1})

    def test_expected_tv_stops_before_requesting_following_season(self) -> None:
        service = MediaSubscriptionService()
        subscription_id = MediaSubscriptionTests._seed_subscription()
        row = db.get_media_subscription(subscription_id)
        cancel_event = threading.Event()
        calls: list[int] = []

        def season_detail(tmdb_id: str, season: int):
            calls.append(season)
            cancel_event.set()
            return {"episodes": []}

        detail = {"seasons": [{"season_number": 1}, {"season_number": 2}]}
        with patch(
            "app.modules.media_subscriptions.TMDBClient.tv_season_detail",
            side_effect=season_detail,
        ):
            with self.assertRaises(MediaSubscriptionError) as ctx:
                asyncio.run(service._expected_tv(row, detail, cancel_event=cancel_event))
        self.assertEqual(ctx.exception.code, "cancelled")
        self.assertEqual(calls, [1])


class MediaSubscriptionAutoCandidateSelectionTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_download_admissions")
            conn.execute("DELETE FROM media_subscription_candidates")
            conn.execute("DELETE FROM media_subscriptions")

    @staticmethod
    def _seed_auto_subscription(media_type: str) -> tuple[int, object]:
        subscription_id = db.add_media_subscription(
            provider="tmdb",
            external_id="7",
            tmdb_id="7",
            media_type=media_type,
            title="测试媒体",
            year="2026",
            action="auto",
            download_target="qb",
        )
        return subscription_id, db.get_media_subscription(subscription_id)

    @staticmethod
    def _item(result_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            download_state="ready",
            relevance_score=99,
            to_public_dict=lambda: {
                "result_id": result_id,
                "site_id": "site",
                "site_name": "站点",
                "title": f"候选 {result_id}",
                "size_text": "1 GB",
                "size_bytes": 1_000_000_000,
                "seeders": 10,
                "published_at": "2026-08-16 00:00:00",
                "relevance_score": 99,
                "download_state": "ready",
                "match_reasons": [],
            },
        )

    def test_movie_auto_search_skips_refreshed_submitted_candidate(self) -> None:
        service = MediaSubscriptionService()
        subscription_id, row = self._seed_auto_subscription("movie")
        submitted_id = db.replace_media_subscription_candidates(
            subscription_id,
            "tmdb:7:movie",
            season=None,
            episode=None,
            candidates=[self._item("already-submitted").to_public_dict()],
            expires_at="2099-01-01 00:00:00",
        )[0]
        db.update_media_subscription_candidate(submitted_id, status="submitted")
        aggregated = SimpleNamespace(items=[
            self._item("already-submitted"),
            self._item("available-next"),
        ])
        download = AsyncMock(return_value={"ok": True})

        async def scenario() -> tuple[int, int]:
            with patch("app.modules.media_subscriptions.config.get_bool", return_value=True), patch(
                "app.modules.media_subscriptions.get_indexer_service",
                return_value=SimpleNamespace(search_media=lambda *_args, **_kwargs: object()),
            ), patch(
                "app.modules.media_subscriptions.run_indexer_awaitable",
                new=AsyncMock(return_value=aggregated),
            ), patch.object(service, "_ensure_active_check"), patch.object(
                service, "download_candidate", new=download
            ):
                return await service._search_movie(row, {}, "tmdb:7:movie")

        _candidate_total, auto_submitted = asyncio.run(scenario())
        rows = db.list_media_subscription_candidates(subscription_id, status="", limit=20)
        by_result = {str(item["result_id"]): int(item["id"]) for item in rows}
        self.assertEqual(auto_submitted, 1)
        download.assert_awaited_once_with(
            by_result["available-next"],
            "qb",
            require_active_check=True,
            cancel_event=None,
        )
        self.assertEqual(
            db.get_media_subscription_candidate(submitted_id)["status"],
            "submitted",
        )

    def test_tv_auto_search_skips_refreshed_submitted_candidate(self) -> None:
        service = MediaSubscriptionService()
        subscription_id, row = self._seed_auto_subscription("tv")
        media_key = "tmdb:7:tv:S01E001"
        submitted_id = db.replace_media_subscription_candidates(
            subscription_id,
            media_key,
            season=1,
            episode=1,
            candidates=[self._item("already-submitted").to_public_dict()],
            expires_at="2099-01-01 00:00:00",
        )[0]
        db.update_media_subscription_candidate(submitted_id, status="submitted")
        ranked = {"items": [
            {
                **self._item("already-submitted").to_public_dict(),
                "quality": {"eligible": True, "match": "exact_episode", "confidence": "high"},
            },
            {
                **self._item("available-next").to_public_dict(),
                "quality": {"eligible": True, "match": "exact_episode", "confidence": "high"},
            },
        ]}
        aggregated = SimpleNamespace(items=[
            self._item("already-submitted"), self._item("available-next")
        ])
        download = AsyncMock(return_value={"ok": True})

        async def scenario() -> tuple[int, int, dict[str, int] | None]:
            with patch("app.modules.media_subscriptions.config.get_bool", return_value=True), patch(
                "app.modules.media_subscriptions.get_indexer_service",
                return_value=SimpleNamespace(search_media=lambda *_args, **_kwargs: object()),
            ), patch(
                "app.modules.media_subscriptions.run_indexer_awaitable",
                new=AsyncMock(return_value=aggregated),
            ), patch(
                "app.modules.media_subscriptions.rank_episode_search", return_value=ranked
            ), patch.object(service, "_ensure_active_check"), patch.object(
                service, "download_candidate", new=download
            ):
                return await service._search_missing_tv(
                    row,
                    {},
                    [_ExpectedMedia(media_key=media_key, season=1, episode=1)],
                )

        _candidate_total, auto_submitted, _rotation = asyncio.run(scenario())
        rows = db.list_media_subscription_candidates(subscription_id, status="", limit=20)
        by_result = {str(item["result_id"]): int(item["id"]) for item in rows}
        self.assertEqual(auto_submitted, 1)
        download.assert_awaited_once_with(
            by_result["available-next"],
            "qb",
            require_active_check=True,
            cancel_event=None,
        )

    def test_auto_search_continues_after_candidate_loses_available_race(self) -> None:
        service = MediaSubscriptionService()
        subscription_id, row = self._seed_auto_subscription("movie")
        aggregated = SimpleNamespace(items=[self._item("first"), self._item("second")])
        download = AsyncMock(side_effect=[
            MediaSubscriptionError("候选已提交", status_code=409, code="unavailable"),
            {"ok": True},
        ])

        async def scenario() -> tuple[int, int]:
            with patch("app.modules.media_subscriptions.config.get_bool", return_value=True), patch(
                "app.modules.media_subscriptions.get_indexer_service",
                return_value=SimpleNamespace(search_media=lambda *_args, **_kwargs: object()),
            ), patch(
                "app.modules.media_subscriptions.run_indexer_awaitable",
                new=AsyncMock(return_value=aggregated),
            ), patch.object(service, "_ensure_active_check"), patch.object(
                service, "download_candidate", new=download
            ):
                return await service._search_movie(row, {}, "tmdb:7:movie")

        _candidate_total, auto_submitted = asyncio.run(scenario())
        self.assertEqual(auto_submitted, 1)
        self.assertEqual(download.await_count, 2)


class MediaSubscriptionCancellationBoundaryTests(IsolatedDatabaseTestCase):
    def test_reconcile_does_not_revive_admission_after_revision_change(self) -> None:
        subscription_id = MediaSubscriptionTests._seed_subscription()
        candidate_id = MediaSubscriptionTests._add_candidates(
            subscription_id,
            media_key="tmdb:86034:tv:S01E001",
            result_ids=("race",),
        )[0]
        revision = int(db.get_media_subscription(subscription_id)["revision"])
        admission_id = db.claim_media_download_admission(
            media_key="tmdb:86034:tv:S01E001",
            tmdb_id="86034",
            media_type="tv",
            subscription_id=subscription_id,
            candidate_id=candidate_id,
            season=1,
            episode=1,
            subscription_revision=revision,
        )
        self.assertTrue(
            db.update_media_subscription_config(subscription_id, monitor_mode="future")
        )

        updated = db.reconcile_media_download_admissions(
            subscription_id,
            {"tmdb:86034:tv:S01E001"},
            expected_revision=revision,
        )

        self.assertEqual(updated, 0)
        with db.get_conn() as conn:
            status = conn.execute(
                "SELECT status FROM media_download_admissions WHERE id=?",
                (int(admission_id),),
            ).fetchone()["status"]
        self.assertEqual(status, "cancelled")

    def test_movie_search_propagates_revision_cancellation(self) -> None:
        service = MediaSubscriptionService()
        cancelled = MediaSubscriptionError("检查已取消", code="cancelled")
        row = {
            "id": 7,
            "revision": 1,
            "title": "Movie",
            "original_title": "",
            "year": "2026",
            "sites_json": "[]",
            "action": "confirm",
            "download_target": "qb",
        }
        aggregated = type("Aggregated", (), {"items": []})()
        indexer = type(
            "Indexer",
            (),
            {"search_media": staticmethod(lambda *_args, **_kwargs: object())},
        )()

        async def scenario():
            with (
                patch("app.modules.media_subscriptions.config.get_bool", return_value=True),
                patch(
                    "app.modules.media_subscriptions.get_indexer_service",
                    return_value=indexer,
                ),
                patch(
                    "app.modules.media_subscriptions.run_indexer_awaitable",
                    new=AsyncMock(return_value=aggregated),
                ),
                patch.object(
                    service,
                    "_ensure_active_check",
                    side_effect=[None, cancelled],
                ),
            ):
                await service._search_movie(row, {}, "tmdb:7:movie")

        with self.assertRaises(MediaSubscriptionError) as ctx:
            asyncio.run(scenario())
        self.assertIs(ctx.exception, cancelled)
