"""深审：播放直链的本地缓存不能放大已经过期的上游响应。"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from app.modules.media_proxy import SignedUrlCache


class SignedUrlExpiryBusinessTests(unittest.IsolatedAsyncioTestCase):
    def _cache(self):
        return SignedUrlCache(
            ttl_seconds=60,
            clock=lambda: 100.0,
            wall_clock=lambda: 1_700_000_000.0,
            expiry_margin_seconds=5,
        )

    async def test_expired_absolute_url_is_refetched_instead_of_reused(self):
        for key in ("expires", "x-oss-expires", "oss-expires", "expiry", "exp"):
            for expired_at in (1_699_999_999, 1_700_000_000):
                with self.subTest(key=key, expired_at=expired_at):
                    cache = self._cache()
                    old_url = f"https://cdn.invalid/movie.mkv?{key}={expired_at}"
                    fresh_url = f"https://cdn.invalid/movie.mkv?{key}=1700000060"
                    fetch = mock.AsyncMock(side_effect=[old_url, fresh_url])
                    first = await cache.get_or_fetch_result("movie", fetch)
                    second = await cache.get_or_fetch_result("movie", fetch)
                    third = await cache.get_or_fetch_result("movie", fetch)
                    self.assertEqual(first.url, old_url)
                    self.assertEqual(second.url, fresh_url)
                    self.assertFalse(second.cache_hit)
                    self.assertEqual(third.url, fresh_url)
                    self.assertTrue(third.cache_hit)
                    self.assertEqual(fetch.await_count, 2)

    async def test_one_expired_file_does_not_evict_another_valid_file(self):
        cache = self._cache()
        valid = mock.AsyncMock(return_value="https://cdn.invalid/valid.mkv?expires=1700000060")
        expired = mock.AsyncMock(return_value="https://cdn.invalid/expired.mkv?expires=1699999999")
        await cache.get_or_fetch("valid", valid)
        await cache.get_or_fetch("expired", expired)
        await cache.get_or_fetch("valid", valid)
        await cache.get_or_fetch("expired", expired)
        self.assertEqual(cache.entry_count, 1)
        self.assertEqual(valid.await_count, 1)
        self.assertEqual(expired.await_count, 2)

    async def test_v4_relative_expiry_is_based_on_signing_time_for_both_routes(self):
        for prefix in ("x-oss", "x-amz"):
            for elapsed, expected_calls in ((30, 1), (4000, 2)):
                for mode in ("sync", "async"):
                    with self.subTest(prefix=prefix, elapsed=elapsed, mode=mode):
                        cache = self._cache()
                        issued = datetime.fromtimestamp(
                            1_700_000_000 - elapsed, tz=timezone.utc,
                        ).strftime("%Y%m%dT%H%M%SZ")
                        url = f"https://cdn.invalid/movie.mkv?{prefix}-date={issued}&{prefix}-expires=3600"
                        if mode == "sync":
                            fetch = mock.Mock(return_value=url)
                            cache.get_or_fetch_sync_result("movie", fetch)
                            result = cache.get_or_fetch_sync_result("movie", fetch)
                            calls = fetch.call_count
                        else:
                            fetch = mock.AsyncMock(return_value=url)
                            await cache.get_or_fetch_result("movie", fetch)
                            result = await cache.get_or_fetch_result("movie", fetch)
                            calls = fetch.await_count
                        self.assertEqual(result.url, url)
                        self.assertEqual(calls, expected_calls)
                        self.assertEqual(result.cache_hit, expected_calls == 1)

    async def test_url_without_expiry_keeps_bounded_default_ttl(self):
        # ts 是不确定语义的时间戳，不能把签发时间当成到期时间。
        # 无明确到期元数据（包括过去/当前/未来的 ts）均沿用有界短 TTL。
        for query in ("token=synthetic", "ts=1699999999", "ts=1700000000", "ts=1700000060"):
            with self.subTest(query=query):
                cache = self._cache()
                fetch = mock.AsyncMock(return_value=f"https://cdn.invalid/movie.mkv?{query}")
                first = await cache.get_or_fetch_result("movie", fetch)
                second = await cache.get_or_fetch_result("movie", fetch)
                self.assertFalse(first.cache_hit)
                self.assertTrue(second.cache_hit)
                fetch.assert_awaited_once()

    def test_sync_route_uses_the_same_expiry_policy(self):
        cache = self._cache()
        expired = "https://cdn.invalid/movie.mkv?expires=1699999999"
        fresh = "https://cdn.invalid/movie.mkv?expires=1700000060"
        fetch = mock.Mock(side_effect=[expired, fresh])
        cache.get_or_fetch_sync_result("movie", fetch)
        second = cache.get_or_fetch_sync_result("movie", fetch)
        self.assertEqual(second.url, fresh)
        self.assertFalse(second.cache_hit)
        self.assertEqual(fetch.call_count, 2)
