"""A 域取消/配置有效性：只使用临时文件数据库和内存 provider。"""
from __future__ import annotations

from typing import cast
from app.indexers.providers.base import IndexerAdapter

import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import rss as rss_repo
from app.repositories import media_subscriptions as media_subscriptions_repo
from app.indexers.models import IndexerItem
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.runtime import bind_indexer_event_loop, unbind_indexer_event_loop
from app.indexers.service import IndexerService
from app.modules.media_subscriptions import MediaSubscriptionService
from app.modules.rss import RSSEngine, RSSEntry, rss_subscription_refresh_revision
from app.modules.rss_scheduler import RSSScheduler
from tests.support import isolated_test_database

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


class AdmissionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(isolated_test_database())
        bind_indexer_event_loop(asyncio.get_running_loop())
        self.addCleanup(unbind_indexer_event_loop, asyncio.get_running_loop())
        self.sid = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="1", tmdb_id="1", media_type="tv",
            title="Cancel", monitor_mode="missing", action="confirm",
            download_target="qb", check_interval_minutes=60,
        )
        self.store = IndexerResultStore()
        self.result_id = self.store.put(IndexerItem(
            site_id="memory", site_name="Memory", title="Cancel S01E01",
            download_state="ready", magnet=MAGNET, download_kinds=("magnet",),
        ))
        self.cid = media_subscriptions_repo.replace_media_subscription_candidates(
            self.sid, "tmdb:1:tv:S01E001", season=1, episode=1,
            candidates=[{"result_id": self.result_id, "title": "Cancel S01E01", "download_state": "ready"}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        self.enterContext(patch(
            "app.modules.media_subscriptions.inspect_series_episode_sources",
            return_value=[{"status": "ready", "episodes": [], "truncated": False}],
        ))

    async def test_hot_close_resolver_releases_unbound_admission_and_allows_retry(self):
        entered = asyncio.Event()
        class Provider:
            site_id = "memory"
            default_enabled = True
            async def resolve(self, stored):
                entered.set()
                await asyncio.Future()
        indexer = IndexerService(registry=IndexerRegistry({"memory": cast(IndexerAdapter, Provider())}), result_store=self.store)
        self.enterContext(patch("app.modules.media_subscriptions.get_indexer_service", return_value=indexer))
        service = MediaSubscriptionService()
        task = asyncio.create_task(service.download_candidate(self.cid))
        await asyncio.wait_for(entered.wait(), 3)
        await indexer.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(media_subscriptions_repo.list_active_media_download_admissions(self.sid), [])
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM download_requests").fetchone()[0], 0)
        # Retry reaches submission instead of returning a permanent duplicate.
        with patch("app.modules.media_subscriptions.download_indexer_result_public", side_effect=RuntimeError), \
             self.assertRaisesRegex(ValueError, "下载提交失败"):
            await service.download_candidate(self.cid)

    async def test_cancel_after_atomic_bind_preserves_unknown_request_lock(self):
        entered = asyncio.Event()
        request_ids = []
        async def bound_submit(*args, admission_id, **kwargs):
            rid, _ = download_requests_repo.create_download_request("bound-cancel", "magnet", admission_id=admission_id)
            download_requests_repo.update_download_request(rid, status="manual_review", qb_status="outcome_unknown")
            request_ids.append(rid)
            entered.set()
            await asyncio.Future()
        with patch("app.modules.media_subscriptions.get_indexer_service", return_value=object()), \
             patch("app.modules.media_subscriptions.download_indexer_result_public", side_effect=bound_submit):
            task = asyncio.create_task(MediaSubscriptionService().download_candidate(self.cid))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        active = media_subscriptions_repo.list_active_media_download_admissions(self.sid)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["request_id"], request_ids[0])
        get_download_request_result = download_requests_repo.get_download_request(request_ids[0])
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result["status"], "manual_review")

    async def test_cancel_before_late_thread_bind_rolls_back_request(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        errors = []
        def late_submit(admission_id):
            entered.set()
            try:
                if not release.wait(3):
                    raise AssertionError("thread not released")
                download_requests_repo.create_download_request("late-cancel", "magnet", admission_id=admission_id)
            except Exception as exc:  # noqa: BLE001 -- 捕获后台线程结果交由主测试断言
                errors.append(exc)
            finally:
                finished.set()
        async def submit(*args, admission_id, **kwargs):
            await asyncio.to_thread(late_submit, admission_id)
        with patch("app.modules.media_subscriptions.get_indexer_service", return_value=object()), \
             patch("app.modules.media_subscriptions.download_indexer_result_public", side_effect=submit):
            task = asyncio.create_task(MediaSubscriptionService().download_candidate(self.cid))
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            task.cancel()
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 3))
        self.assertEqual(len(errors), 1)
        self.assertEqual(type(errors[0]).__name__, "DownloadAdmissionBindingError")
        self.assertEqual(media_subscriptions_repo.list_active_media_download_admissions(self.sid), [])
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM download_requests").fetchone()[0], 0)


class RSSAutoCancellationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.sid = rss_repo.add_rss_subscription(
            "Auto", "https://feed.invalid/rss", action="download", enabled=1, download_method="qb",
        )
        self.engine = RSSEngine(tmdb_client=object())
        self.addCleanup(self.engine.close)
        self.backend = self.enterContext(patch(
            "app.modules.download_dispatcher._submit_qb", return_value={"ok": True, "task_id": "a" * 40},
        ))
        self.cloud_backend = self.enterContext(patch(
            "app.modules.download_dispatcher._submit_guangya", return_value={"ok": True, "task_id": "gy"},
        ))
        self.enterContext(patch("app.modules.download_tracker.get_download_tracker"))

    def _entry(self):
        return RSSEntry(title="Auto S01E01", guid=f"auto-one-{self.sid}", torrent_url=MAGNET)

    def test_configuration_changed_during_fetch_defers_without_claim(self):
        for changes in ({"enabled": 0}, {"action": "subscribe"}, {"urls": "https://new.invalid/feed"},
                        {"exclude_keywords": "different"}, {"download_method": "guangya"},
                        {"qb_save_path": "/isolated/new"}, {"gy_target_dir": "new-target"}):
            with self.subTest(changes=changes):
                self.sid = rss_repo.add_rss_subscription(
                    "Config change", "https://feed.invalid/rss", action="download", enabled=1, download_method="qb",
                )
                self.backend.reset_mock()
                self.cloud_backend.reset_mock()
                revision = rss_subscription_refresh_revision(rss_repo.get_rss_subscription(self.sid))
                def parse(_url, changes=changes):
                    rss_repo.update_rss_subscription(self.sid, changes)
                    return [self._entry()]
                self.engine.parser = Mock(parse=Mock(side_effect=parse), last_error_code="")
                result = self.engine.auto_download(self.sid, expected_revision=revision)
                self.backend.assert_not_called()
                self.cloud_backend.assert_not_called()
                self.assertTrue(result.get("conflict") or result.get("cancelled"))
                pending = rss_repo.list_rss_entries(sub_id=self.sid, status="pending").items
                self.assertEqual(len(pending), 0)
                current = rss_repo.get_rss_subscription(self.sid)
                assert current is not None
                self.assertIsNone(current["last_refreshed_at"])

    def test_automatic_without_explicit_revision_still_fences_fetch(self):
        def parse(_url):
            rss_repo.update_rss_subscription(self.sid, {"enabled": 0})
            return [self._entry()]
        self.engine.parser = Mock(parse=Mock(side_effect=parse), last_error_code="")
        result = self.engine.auto_download(self.sid)
        self.backend.assert_not_called()
        self.assertTrue(result.get("conflict") or result.get("cancelled"))

    def test_manual_download_remains_allowed_when_paused(self):
        self.engine.parser = Mock(parse=Mock(return_value=[self._entry()]), last_error_code="")
        self.engine.refresh(self.sid)
        rss_repo.update_rss_subscription(self.sid, {"enabled": 0, "action": "subscribe"})
        entry = rss_repo.list_rss_entries(sub_id=self.sid, status="pending").items[0]
        result = self.engine.download(int(entry["id"]))
        self.assertTrue(result["ok"])
        self.backend.assert_called_once()

    def test_pause_inside_batch_preserves_started_unknown_and_defers_queued_entries(self):
        # 同源组串行执行：首条真正提交后暂停，后续仍在同一个 20 条批次中。
        entries = [RSSEntry(title=f"Auto {i}", guid=f"auto-{i}", torrent_url=MAGNET) for i in range(5)]
        self.engine.parser = Mock(parse=Mock(return_value=entries), last_error_code="")
        def unknown_after_pause(*args, **kwargs):
            rss_repo.update_rss_subscription(self.sid, {"enabled": 0})
            return {"ok": False, "failure_code": "qb_outcome_unknown", "error": "unknown"}
        self.backend.side_effect = unknown_after_pause
        result = self.engine.auto_download(self.sid)
        self.backend.assert_called_once()
        self.assertEqual(result["deferred"], 4)
        self.assertTrue(result.get("cancelled"))
        self.assertTrue(result["review_required"])
        self.assertEqual(result["outcome_unknown_count"], 1)
        self.assertEqual(len(rss_repo.list_rss_entries(sub_id=self.sid, status="pending").items), 4)
        with db.get_conn() as conn:
            requests = conn.execute("SELECT status,qb_status FROM download_requests").fetchall()
        self.assertEqual([row["qb_status"] for row in requests], ["outcome_unknown"])
        self.assertEqual(len(requests), 1)
        scheduler = RSSScheduler()
        with patch("app.modules.rss_scheduler.RSSEngine", return_value=self.engine), \
             patch.object(self.engine, "auto_download", return_value=result), \
             patch.object(scheduler, "_notify_issue") as notify:
            scheduler._execute(self.sid, "download")
        self.assertEqual(notify.call_args.args[1], "outcome_unknown")

    def test_deletion_during_fetch_cannot_submit(self):
        def parse(_url):
            rss_repo.delete_rss_subscription(self.sid)
            return []
        self.engine.parser = Mock(parse=Mock(side_effect=parse), last_error_code="")
        result = self.engine.auto_download(self.sid)
        self.backend.assert_not_called()
        self.assertTrue(result["conflict"])

    def test_refresh_audit_timestamp_does_not_invalidate_unchanged_configuration(self):
        # refresh 更新 last_refreshed_at 时仓储也更新 updated_at；跨秒刷新不能
        # 把自己的统计写入误认为用户修改配置。
        self.engine.parser = Mock(parse=Mock(return_value=[self._entry()]), last_error_code="")
        revision = rss_subscription_refresh_revision(rss_repo.get_rss_subscription(self.sid))
        with patch("app.database.now", return_value="2099-01-01 00:00:00"):
            result = self.engine.auto_download(self.sid, expected_revision=revision)
        self.assertEqual(result.get("downloaded"), 1)
        self.assertFalse(result.get("conflict"))
        self.backend.assert_called_once()

    def test_parallel_inflight_writes_finish_but_paused_queued_groups_do_not_start(self):
        entries = [RSSEntry(title=f"Parallel {i}", guid=f"parallel-{i}",
                           torrent_url=f"magnet:?xt=urn:btih:{i:040x}") for i in range(1, 21)]
        self.engine.parser = Mock(parse=Mock(return_value=entries), last_error_code="")
        started = threading.Barrier(5)
        release = threading.Event()
        def backend(row, **kwargs):
            started.wait(5)
            if not release.wait(5):
                raise AssertionError("inflight writes not released")
            return {"ok": True, "task_id": kwargs["task_id_hint"]}
        self.backend.side_effect = backend
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.engine.auto_download, self.sid)
            try:
                started.wait(5)
                rss_repo.update_rss_subscription(self.sid, {"enabled": 0})
            finally:
                release.set()
            result = pending.result(timeout=5)
        self.assertEqual(self.backend.call_count, 4)
        self.assertEqual(result["downloaded"], 4)
        self.assertEqual(result["deferred"], 16)
        self.assertTrue(result["cancelled"])
        self.assertEqual(len(rss_repo.list_rss_entries(sub_id=self.sid, status="pending").items), 16)

    def test_manual_batch_download_remains_allowed_when_paused(self):
        self.engine.parser = Mock(parse=Mock(return_value=[self._entry()]), last_error_code="")
        self.engine.refresh(self.sid)
        rss_repo.update_rss_subscription(self.sid, {"enabled": 0, "action": "subscribe"})
        entry = rss_repo.list_rss_entries(sub_id=self.sid, status="pending").items[0]
        result = self.engine.download_many([int(entry["id"])])
        self.assertEqual(result["success_count"], 1)
        self.backend.assert_called_once()
