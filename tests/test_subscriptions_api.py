"""统一订阅中心 API 与页面契约测试。"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class SubscriptionAPITests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_download_admissions")
            conn.execute("DELETE FROM media_subscription_candidates")
            conn.execute("DELETE FROM media_subscription_runs")
            conn.execute("DELETE FROM media_subscriptions")
        self.client = TestClient(create_app(start_background=False))
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    @staticmethod
    def _csrf(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> dict[str, str]:
        token = self._csrf(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return {"X-CSRF-Token": self._csrf(self.client.get("/rss").text)}

    def test_api_requires_login_and_csrf_for_mutations(self) -> None:
        self.assertEqual(self.client.get("/api/subscriptions/media").status_code, 401)
        self.assertEqual(
            self.client.post("/api/subscriptions/media", json={"tmdb_id": "1"}).status_code,
            401,
        )
        self.login()
        self.assertEqual(
            self.client.post("/api/subscriptions/media", json={"tmdb_id": "1"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete("/api/subscriptions/media/1").status_code,
            403,
        )

    def test_thin_routes_forward_crud_check_and_candidate_scope(self) -> None:
        headers = self.login()
        service = Mock()
        service.list_subscriptions.return_value = []
        service.create_subscription = AsyncMock(return_value={
            "created": True,
            "subscription": {"id": 7, "tmdb_id": "86034", "media_type": "tv"},
        })
        service.update_subscription.return_value = {"id": 7, "enabled": False}
        service.check_subscription = AsyncMock(return_value={"result": {"status": "satisfied"}})
        service.delete_subscription.return_value = True
        with patch(
            "app.routes.subscriptions_api.get_media_subscription_service", return_value=service
        ), patch("app.routes.subscriptions_api._wake_scheduler"):
            created = self.client.post(
                "/api/subscriptions/media",
                headers=headers,
                json={"tmdb_id": "86034", "media_type": "tv"},
            )
            updated = self.client.put(
                "/api/subscriptions/media/7", headers=headers, json={"enabled": False}
            )
            checked = self.client.post("/api/subscriptions/media/7/check", headers=headers)
            removed = self.client.delete("/api/subscriptions/media/7", headers=headers)

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(removed.status_code, 200, removed.text)
        service.create_subscription.assert_awaited_once_with({"tmdb_id": "86034", "media_type": "tv"})
        service.update_subscription.assert_called_once_with(7, {"enabled": False})
        service.check_subscription.assert_awaited_once_with(7, trigger="manual")
        service.delete_subscription.assert_called_once_with(7)

        subscription_id = db.add_media_subscription(
            provider="tmdb", external_id="86034", tmdb_id="86034", media_type="tv",
            title="平凡职业造就世界最强",
        )
        other_id = db.add_media_subscription(
            provider="tmdb", external_id="127532", tmdb_id="127532", media_type="tv",
            title="我独自升级",
        )
        candidate_id = db.replace_media_subscription_candidates(
            other_id,
            "tmdb:127532:tv:S01E001",
            season=1,
            episode=1,
            candidates=[{"result_id": "other", "title": "other"}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        scoped_service = Mock()
        scoped_service.download_candidate = AsyncMock()
        with patch(
            "app.routes.subscriptions_api.get_media_subscription_service",
            return_value=scoped_service,
        ):
            response = self.client.post(
                f"/api/subscriptions/media/{subscription_id}/download",
                headers=headers,
                json={"candidate_id": candidate_id, "target": "guangya"},
            )
        self.assertEqual(response.status_code, 404, response.text)
        scoped_service.download_candidate.assert_not_awaited()

    def test_watchlist_subscription_requires_confirmed_tmdb_mapping(self) -> None:
        headers = self.login()
        watchlist = [{
            "provider": "bangumi",
            "external_id": "12345",
            "media_type": "tv",
            "title": "测试动画",
            "year": "2026",
            "poster_key": "",
            "created_at": "2026-08-09 12:00:00",
        }]
        discovery = Mock()
        discovery.list_watchlist.return_value = watchlist
        discovery.map_to_tmdb_async = AsyncMock(return_value={
            "confirmed": False,
            "candidates": [{"tmdb_id": "999", "title": "测试动画"}],
        })
        service = Mock()
        service.list_subscriptions.return_value = []
        with patch("app.routes.subscriptions_api.get_discovery_service", return_value=discovery), patch(
            "app.routes.subscriptions_api.get_media_subscription_service", return_value=service
        ), patch("app.routes.subscriptions_api._wake_scheduler"):
            pending = self.client.post(
                "/api/subscriptions/media/from-watchlist",
                headers=headers,
                json={
                    "provider": "bangumi",
                    "external_id": "12345",
                    "media_type": "tv",
                    "monitor_mode": "selected",
                    "seasons": [2],
                },
            )
        self.assertEqual(pending.status_code, 409, pending.text)
        self.assertEqual(pending.json()["code"], "mapping_required")
        self.assertFalse(service.create_subscription.called)

        discovery.map_to_tmdb_async.return_value = {"confirmed": True, "tmdb_id": "999"}
        service.create_subscription = AsyncMock(return_value={
            "created": True,
            "subscription": {"id": 9, "tmdb_id": "999", "media_type": "tv"},
        })
        with patch("app.routes.subscriptions_api.get_discovery_service", return_value=discovery), patch(
            "app.routes.subscriptions_api.get_media_subscription_service", return_value=service
        ), patch("app.routes.subscriptions_api._wake_scheduler"):
            created = self.client.post(
                "/api/subscriptions/media/from-watchlist",
                headers=headers,
                json={
                    "provider": "bangumi",
                    "external_id": "12345",
                    "media_type": "tv",
                    "tmdb_id": "999",
                    "monitor_mode": "selected",
                    "seasons": [2],
                    "action": "auto",
                    "download_target": "both",
                },
            )
        self.assertEqual(created.status_code, 201, created.text)
        service.create_subscription.assert_awaited_once_with({
            "provider": "bangumi",
            "external_id": "12345",
            "media_type": "tv",
            "tmdb_id": "999",
            "monitor_mode": "selected",
            "seasons": [2],
            "action": "auto",
            "download_target": "both",
        }, identity_confirmed=True)

    def test_subscription_center_page_exposes_unified_navigation_and_stable_hooks(self) -> None:
        self.login()
        response = self.client.get("/rss")
        self.assertEqual(response.status_code, 200, response.text)
        html = response.text
        self.assertIn("订阅 - MediaFlux", html)
        self.assertIn('data-subscription-center', html)
        self.assertIn('data-subscription-tab="media"', html)
        self.assertIn('data-subscription-tab="rss"', html)
        self.assertIn('data-subscription-tab="watchlist"', html)
        self.assertIn('data-subscription-tab="runs"', html)
        self.assertIn('id="mediaSubscriptionList"', html)
        self.assertIn('id="subscriptionWatchlist"', html)
        self.assertIn('id="subscriptionRunList"', html)
        self.assertIn('/static/js/subscriptions.js', html)
        self.assertIn('<option value="4320" selected>每 3 天</option>', html)
        self.assertIn('<option value="10080">每 7 天</option>', html)
        self.assertNotIn('<option value="60" selected>每 1 小时</option>', html)
        self.assertIn('subscriptionInitialTab', html)
        self.assertIn('MEDIA FOLLOW</span><span class="subscription-heading-divider"', html)
        self.assertIn('RSS SOURCES</span><span class="subscription-heading-divider"', html)
        self.assertIn('WATCHLIST</span><span class="subscription-heading-divider"', html)
        self.assertIn('AUDIT LOG</span><span class="subscription-heading-divider"', html)
        self.assertIn('class="btn btn-primary btn-sm subscription-rss-create"', html)
        self.assertIn('class="subscription-metric-value" id="subscriptionStatTotal"', html)
        self.assertRegex(html, r'href="/rss"[^>]*>.*?<span>订阅</span>')

        root = Path(__file__).resolve().parents[1]
        styles = (root / 'app/static/css/main.css').read_text(encoding='utf-8')
        scripts = (root / 'app/static/js/subscriptions.js').read_text(encoding='utf-8')
        self.assertIn('html[data-subscription-initial-tab="rss"] #subscriptionPanelRss', styles)
        self.assertIn('html[data-subscription-initial-tab="watchlist"] #subscriptionPanelWatchlist', styles)
        self.assertIn('html[data-subscription-initial-tab="runs"] #subscriptionPanelRuns', styles)
        self.assertRegex(
            styles,
            r'\.subscription-panel-actions \.subscription-create\s*\{[^}]*width:\s*auto',
        )
        self.assertRegex(
            styles,
            r'\.subscription-section-toolbar \.subscription-rss-create\s*\{[^}]*width:\s*auto',
        )
        self.assertIn('grid-template-columns: 64px minmax(0, 1fr) auto', styles)
        self.assertIn('.subscription-watch-icon { width: 32px; height: 32px;', styles)
        self.assertNotIn('repeat(auto-fill, minmax(360px, 440px))', styles)
        self.assertIn('grid-template-columns: 40px minmax(0, 1fr) auto', styles)
        self.assertIn('.subscription-media-list { align-content: start; grid-auto-rows: max-content; }', styles)
        self.assertIn('.media-subscription-card {', styles)
        self.assertIn('min-height: 112px;', styles)
        self.assertIn('.subscription-skeleton.is-media {', styles)
        self.assertIn('.subscription-skeleton.is-media > span:nth-child(3) { grid-column: 2;', styles)
        self.assertIn('padding: 8px 8px 4px 0;', styles)
        self.assertIn('.media-subscription-details {', styles)
        self.assertIn('.media-subscription-candidate-count {', styles)
        self.assertNotIn('media-subscription-facts', scripts)
        self.assertIn("void window.appAlert?.({", scripts)
        self.assertIn("partial ? '部分目标提交失败' : `已发送到${targetLabel}`", scripts)
        self.assertIn("title: duplicate ? '未重复提交' : `发送到${targetLabel}失败`", scripts)
        self.assertIn('const contextActive = state.candidateSubscriptionId === subscriptionId', scripts)
        self.assertIn("const target = subscription.download_target || 'guangya'", scripts)
        self.assertIn("target === 'both' ? '同时发送到 qB + 光鸭'", scripts)
        self.assertNotIn("[['guangya', '光鸭'], ['qb', 'qB']]", scripts)
        self.assertIn('candidateDeliveryView(item)', scripts)
        self.assertIn('mediaWorkflowSummary(item, result)', scripts)
        self.assertIn('item.candidate_count ?? workflow.candidate_count', scripts)
        self.assertIn('已推送至 ${target}，等待下载', scripts)
        self.assertIn('尚未满足自动下载条件', scripts)
        self.assertIn("'icon-action subscription-watch-icon'", scripts)
        self.assertIn("return mediaDetailURL(item, false, '/rss#media')", scripts)
        self.assertIn('title.href = mediaSubscriptionLink(item)', scripts)
        self.assertIn("return mediaDetailURL(item, resourceFocus, '/rss#watchlist')", scripts)
        self.assertIn("params.set('return_to', returnTo)", scripts)
        self.assertIn('delete document.documentElement.dataset.subscriptionInitialTab', scripts)
        self.assertIn("setMediaInterval(4320);", scripts)
        self.assertIn("当前旧设置：${formatLegacyMediaInterval(normalized)}", scripts)
        self.assertIn("fields.interval.dataset.legacyInterval === fields.interval.value", scripts)
        self.assertIn("delete payload.check_interval_minutes;", scripts)
        self.assertIn("document.addEventListener('mediaflux:rss-stats-updated'", scripts)
        self.assertNotIn('window.updateStats =', scripts)
        self.assertIn(
            "document.dispatchEvent(new CustomEvent('mediaflux:rss-stats-updated'))",
            html,
        )


class SubscriptionMotionContractTests(unittest.TestCase):
    def test_subscription_cards_stay_static_and_numbers_count_up(self) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (root / "app/templates/rss.html").read_text(encoding="utf-8")
        script = (root / "app/static/js/subscriptions.js").read_text(encoding="utf-8")
        styles = (root / "app/static/css/main.css").read_text(encoding="utf-8")

        self.assertIn("window.MFAnim.countUp", script)
        self.assertNotIn("window.MFAnim.staggerIn", script)
        self.assertNotIn("window.MFAnim.crossfade", script)
        self.assertIn("function lockElementHeight(element)", script)
        self.assertIn("requestAnimationFrame(() => window.requestAnimationFrame", script)
        self.assertIn("preserve && hadContent ? lockElementHeight", script)
        self.assertIn("delete document.documentElement.dataset.subscriptionInitialTab", script)
        self.assertIn("subscriptions.js?v=20260822a", template)

        self.assertIn(".subscription-tab,", styles)
        self.assertIn(".media-subscription-card,", styles)
        self.assertIn(".rss-entry-card { transition: none; }", styles)


class SubscriptionMappingAsyncContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _watchlist():
        return [{
            "provider": "bangumi",
            "external_id": "12345",
            "media_type": "tv",
            "title": "测试动画",
            "year": "2026",
        }]

    @staticmethod
    def _payload():
        return {
            "provider": "bangumi",
            "external_id": "12345",
            "media_type": "tv",
        }

    async def test_watchlist_mapping_does_not_block_event_loop(self):
        from app.routes import subscriptions_api

        entered = threading.Event()
        release = threading.Event()
        discovery = Mock()
        discovery.list_watchlist.return_value = self._watchlist()

        def slow_map(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=0.5)
            return {"confirmed": False, "tmdb_id": "", "candidates": []}

        async def slow_map_async(*args, **kwargs):
            return await asyncio.to_thread(slow_map, *args, **kwargs)

        discovery.map_to_tmdb_async = AsyncMock(side_effect=slow_map_async)
        with (
            patch.object(subscriptions_api, "require_api_login"),
            patch.object(
                subscriptions_api, "get_discovery_service", return_value=discovery
            ),
        ):
            task = asyncio.create_task(
                subscriptions_api.create_from_watchlist(
                    SimpleNamespace(), self._payload()
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertFalse(task.done(), "同步 TMDB 映射阻塞了事件循环")
            release.set()
            response = await task

        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["code"], "mapping_required")
        discovery.map_to_tmdb_async.assert_awaited_once_with(
            "bangumi", "12345", "tv", "测试动画", "2026",
            confirmed_tmdb_id="",
        )

    async def test_cancelling_mapping_does_not_create_subscription(self):
        from app.routes import subscriptions_api

        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        discovery = Mock()
        discovery.list_watchlist.return_value = self._watchlist()

        def slow_map(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=1)
            finished.set()
            return {"confirmed": True, "tmdb_id": "999"}

        async def slow_map_async(*args, **kwargs):
            return await asyncio.to_thread(slow_map, *args, **kwargs)

        discovery.map_to_tmdb_async = AsyncMock(side_effect=slow_map_async)
        service = Mock()
        service.create_subscription = AsyncMock()
        with (
            patch.object(subscriptions_api, "require_api_login"),
            patch.object(
                subscriptions_api, "get_discovery_service", return_value=discovery
            ),
            patch.object(
                subscriptions_api,
                "get_media_subscription_service",
                return_value=service,
            ),
            patch.object(subscriptions_api, "_wake_scheduler") as wake_scheduler,
        ):
            task = asyncio.create_task(
                subscriptions_api.create_from_watchlist(
                    SimpleNamespace(), self._payload()
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            service.create_subscription.assert_not_awaited()
            wake_scheduler.assert_not_called()
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))
