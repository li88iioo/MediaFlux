"""完整季度范围：串行、有预算、取消及状态回归。"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import database as db
from app.repositories import media_subscriptions as media_subscriptions_repo
from app.modules.media_subscriptions import (
    MediaSubscriptionError,
    MediaSubscriptionService,
)
from tests.support import isolated_test_database


class SeasonCompletenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(isolated_test_database())
        self.sid = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="31", tmdb_id="31", media_type="tv", title="Long Show",
            monitor_mode="missing", action="auto", download_target="qb", check_interval_minutes=60,
        )
        self.calls = []
        self.season_count = 31
        owner = self
        class TMDB:
            def detail(self, *args):
                return {"name": "Long Show", "seasons": [{"season_number": i} for i in range(1, owner.season_count + 1)]}
            def tv_season_detail(self, tmdb_id, season):
                owner.calls.append(season)
                return {"episodes": [{"episode_number": 1, "air_date": "2020-01-01"}]}
        self.client = TMDB()
        self.enterContext(patch("app.modules.media_subscriptions.TMDBClient", return_value=self.client))
        self.close = self.enterContext(patch("app.modules.media_subscriptions.close_tmdb_client"))
        self.enterContext(patch(
            "app.modules.media_subscriptions.inspect_series_episode_sources",
            return_value=[{"status": "ready", "truncated": False, "episodes": [(i, 1) for i in range(1, 31)]}],
        ))
        self.enterContext(patch("app.modules.media_subscription_notifications.drain_media_subscription_notifications"))
        self.search = self.enterContext(patch.object(
            MediaSubscriptionService, "_search_missing_tv", new=AsyncMock(return_value=(0, 0, None)),
        ))

    async def test_31st_season_is_included_on_repeated_checks(self):
        for _ in range(2):
            result = (await MediaSubscriptionService().check_subscription(self.sid))["result"]
            self.assertEqual(result["status"], "missing")
            self.assertEqual(result["expected_count"], 31)
            self.assertEqual(result["missing_count"], 1)
            self.assertEqual(result["missing"][0]["season"], 31)
        self.assertEqual(self.calls, list(range(1, 32)) * 2)
        self.assertEqual(self.search.await_count, 2)

    async def test_oversized_scope_explicitly_fails_without_partial_success_or_network(self):
        self.season_count = 101
        with self.assertRaises(MediaSubscriptionError) as error:
            await MediaSubscriptionService().check_subscription(self.sid)
        self.assertEqual(error.exception.code, "season_limit_exceeded")
        self.assertEqual(self.calls, [])
        get_media_subscription_result = media_subscriptions_repo.get_media_subscription(self.sid)
        assert get_media_subscription_result is not None
        self.assertEqual(get_media_subscription_result["status"], "error")
        self.search.assert_not_awaited()

    async def test_selected_seasons_allow_late_season_of_oversized_show(self):
        self.season_count = 101
        media_subscriptions_repo.update_media_subscription_config(self.sid, monitor_mode="selected", seasons_json="[99]")
        result = (await MediaSubscriptionService().check_subscription(self.sid))["result"]
        self.assertEqual(result["expected_count"], 1)
        self.assertEqual(result["missing"][0]["season"], 99)
        self.assertEqual(self.calls, [99])

    async def test_time_budget_fails_instead_of_returning_partial_expected_set(self):
        with patch("app.modules.media_subscriptions.monotonic", Mock(side_effect=[0, 0, 61]), create=True):
            with self.assertRaises(MediaSubscriptionError) as error:
                await MediaSubscriptionService()._expected_tv(
                    media_subscriptions_repo.get_media_subscription(self.sid), self.client.detail(), require_active_check=False,
                )
        self.assertEqual(error.exception.code, "season_budget_exceeded")
        self.assertEqual(self.calls, [1])
        self.close.assert_called_once_with(self.client)

    async def test_cancel_after_first_season_stops_further_requests(self):
        cancel = threading.Event()
        original = self.client.tv_season_detail
        def cancelled(*args):
            result = original(*args)
            cancel.set()
            return result
        with patch.object(self.client, "tv_season_detail", side_effect=cancelled):
            with self.assertRaises(MediaSubscriptionError) as error:
                await MediaSubscriptionService().check_subscription(self.sid, cancel_event=cancel)
        self.assertEqual(error.exception.code, "cancelled")
        self.assertEqual(self.calls, [1])
        self.search.assert_not_awaited()

    async def test_100_seasons_are_serial_and_complete_at_hard_boundary(self):
        self.season_count = 100
        result = (await MediaSubscriptionService().check_subscription(self.sid))["result"]
        self.assertEqual(self.calls, list(range(1, 101)))
        self.assertEqual(result["expected_count"], 100)
        self.assertEqual(result["missing_count"], 70)

    async def test_unsupported_selected_season_fails_before_network(self):
        self.season_count = 101
        # 模拟历史坏配置；新的配置写入口已经禁止保存101季。
        with db.get_conn() as conn:
            conn.execute("UPDATE media_subscriptions SET monitor_mode='selected',seasons_json='[101]' WHERE id=?", (self.sid,))
        with self.assertRaises(MediaSubscriptionError) as error:
            await MediaSubscriptionService().check_subscription(self.sid)
        self.assertEqual(error.exception.code, "unsupported_season")
        self.assertEqual(self.calls, [])
