"""混合封面/身份/缓存与匿名有界 HTTP；除已落盘 fixture 外全程离线。"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

# 应用导入前必须 import tests，禁止自动排序越过隔离边界。
# isort: off
import tests  # noqa: F401  # 应用导入前隔离运行路径、数据库和配置。
import httpx

from tests.support import isolated_test_database
from app.discovery.cache import DiscoveryCache
from app.discovery.calendar.douban_http import CalendarDoubanClient
from app.discovery.calendar.metadata import CalendarMetadata, _douban_poster_key, _empty
from app.discovery.calendar.models import CalendarEntry, CalendarEvent, SourceUnavailable
from app.discovery.calendar.tmdb_http import CalendarTMDBClient
from app.routes.discovery_image import _canonical_poster_key
# isort: on

FIXTURES = Path(__file__).parent / "fixtures" / "calendar" / "metadata"


def fixture(name="langyabang"):
    return json.loads((FIXTURES / f"douban-suggest-{name}.json").read_text())


def entry(**changes):
    return replace(CalendarEntry(
        "tencent", "source_999", "琅琊榜", "tv", "https://v.qq.com/x/cover/source_999.html",
        year="2015", free_progress="免费至第3集", evidence="平台公开排期",
        events=(CalendarEvent("2026-09-09", "19:30", "第4集", "unknown"),),
    ), **changes)


def tmdb_row(**changes):
    # TMDB 候选为合成控制数据；公开 suggest fixture 不补造产地。
    return {"id": 42, "name": "琅琊榜", "original_name": "Nirvana in Fire",
            "first_air_date": "2015-09-19", "genre_ids": [18], "poster_path": "/poster.jpg",
            "vote_average": 8.5, "overview": "TMDB 简介", "origin_country": ["CN"], **changes}


def tmdb_payload(*rows):
    return {"results": list(rows), "page": 1, "total_pages": 1 if rows else 0, "total_results": len(rows)}


def douban_row(**changes):
    row = {**fixture()[0], **changes}
    if "id" in changes and "url" not in changes:
        row["url"] = f"https://movie.douban.com/subject/{row['id']}/"
    return row


class MixedMetadataTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.now = datetime(2026, 9, 9, 12)  # noqa: DTZ001 -- DiscoveryCache 使用本地无时区时间戳。
        self.cache = DiscoveryCache(clock=lambda: self.now)
        self.tmdb = SimpleNamespace(api_key="offline-key", config_error="", get=AsyncMock(
            return_value=tmdb_payload(tmdb_row())), aclose=AsyncMock())
        self.douban = SimpleNamespace(suggest=AsyncMock(return_value=fixture()), aclose=AsyncMock())
        self.mapper = CalendarMetadata(self.cache, client_factory=lambda: self.tmdb,
                                       douban_client_factory=lambda: self.douban)

    def test_real_fixture_tmdb_primary_douban_backup_with_proxy_compatible_keys(self):
        card = self.mapper.enrich((entry(),))[0]
        self.assertEqual((card["tmdb_id"], card["douban_id"]), ("42", "25754848"))
        self.assertEqual((card["poster_provider"], card["poster_key"]), ("tmdb", "poster.jpg"))
        self.assertEqual(card["tmdb_poster_key"], "poster.jpg")
        self.assertEqual(card["douban_poster_key"], "img1.doubanio.com/view/photo/s_ratio_poster/public/p2271982968.jpg")
        for provider in ("tmdb", "douban"):
            key = card[f"{provider}_poster_key"]
            self.assertEqual(_canonical_poster_key(provider, key), (provider, key))
        for field in entry().to_dict():
            self.assertEqual(card[field], entry().to_dict()[field])
        self.assertEqual((card["rating"], card["rating_source"]), (8.5, "tmdb"))
        self.assertEqual(card["origin_country"], ["CN"])
        self.assertFalse(any(key.startswith("_") for key in card))
        self.tmdb.get.assert_awaited_once()
        self.douban.suggest.assert_awaited_once()
        self.tmdb.aclose.assert_awaited_once()
        self.douban.aclose.assert_awaited_once()

    def test_missing_tmdb_poster_does_not_change_tmdb_identity_or_rating_to_douban(self):
        self.tmdb.get.return_value = tmdb_payload(tmdb_row(poster_path=None))
        card = self.mapper.enrich((entry(),))[0]
        self.assertEqual(card["poster_provider"], "douban")
        self.assertEqual(card["poster_key"], card["douban_poster_key"])
        self.assertEqual(card["tmdb_poster_key"], "")
        self.assertEqual((card["tmdb_id"], card["douban_id"], card["rating_source"]), ("42", "25754848", "tmdb"))
        self.assertNotIn("watchlist_provider", card)
        self.assertNotIn("external_id", card)

    def test_tmdb_failure_still_uses_douban_and_can_recover_next_call(self):
        self.tmdb.get.side_effect = RuntimeError("do-not-expose-test-secret")
        first = self.mapper.enrich((entry(),))[0]
        self.assertEqual((first["tmdb_id"], first["douban_id"], first["poster_provider"]), ("", "25754848", "douban"))
        self.assertEqual((first["rating"], first["rating_source"]), (None, ""))
        self.assertIsNone(first["origin_country"])
        self.assertNotIn("do-not-expose", json.dumps(first))
        self.tmdb.get.side_effect = None
        recovered = self.mapper.enrich((entry(),))[0]
        self.assertEqual(recovered["poster_provider"], "tmdb")
        self.assertEqual(recovered["origin_country"], ["CN"])
        self.assertEqual(self.douban.suggest.await_count, 1)

    def test_no_key_and_invalid_config_are_not_sticky_even_with_cached_douban_cover(self):
        for attribute, value in (("api_key", ""), ("config_error", "invalid fixture config")):
            with self.subTest(attribute=attribute):
                show = entry(source_id=attribute)
                original = getattr(self.tmdb, attribute)
                setattr(self.tmdb, attribute, value)
                first = self.mapper.enrich((show,))[0]
                self.assertEqual(first["poster_provider"], "douban")
                self.assertEqual(first["mapping_status"], "matched")
                setattr(self.tmdb, attribute, original)
                self.assertEqual(self.mapper.enrich((show,))[0]["tmdb_id"], "42")

    def test_unconfigured_no_match_is_not_negative_cached(self):
        self.tmdb.api_key = ""
        self.douban.suggest.return_value = []
        self.assertEqual(self.mapper.enrich((entry(),))[0]["mapping_status"], "not_configured")
        self.assertEqual(self.cache.get(self.mapper.key(entry())).status, "miss")
        self.tmdb.api_key = "offline-key"
        self.assertEqual(self.mapper.enrich((entry(),))[0]["tmdb_id"], "42")

    def test_failed_construction_and_close_preserve_programmes(self):
        self.mapper.client_factory = Mock(side_effect=RuntimeError("offline fixture error"))
        self.douban.aclose.side_effect = RuntimeError("close failure")
        card = self.mapper.enrich((entry(),))[0]
        self.assertEqual(card["poster_provider"], "douban")
        self.assertEqual(card["events"], entry().to_dict()["events"])
        self.mapper.client_factory.assert_called_once()

    def test_conflicting_douban_candidates_include_wrong_type_and_missing_poster(self):
        for conflict in (douban_row(id="25754849"), douban_row(id="25754849", type="person"),
                         douban_row(id="25754849", img="")):
            with self.subTest(conflict=conflict):
                self.douban.suggest.return_value = [douban_row(), conflict]
                profile = asyncio.run(self.mapper._search_douban_profile(self.douban, entry(), "2015", 1))
                self.assertEqual(profile["douban_id"], "")

    def test_exact_title_year_and_series_evidence_not_first_suggest_result(self):
        variants = (douban_row(title="琅琊榜2", sub_title="琅琊榜2"), douban_row(year="2016"),
                    douban_row(episode=""), douban_row(episode="unknow"), douban_row(episode="0"),
                    douban_row(type="person"), douban_row(type="book"))
        for row in variants:
            with self.subTest(row=row):
                self.douban.suggest.return_value = [row]
                profile = asyncio.run(self.mapper._search_douban_profile(self.douban, entry(), "2015", 1))
                self.assertEqual(profile["douban_id"], "")
        # 同名不同年，不先选最上面的图。
        self.douban.suggest.return_value = [douban_row(id="25754849", year="2016"), douban_row()]
        self.assertEqual(self.mapper.enrich((entry(),))[0]["douban_id"], "25754848")

    def test_real_animation_fixture_preserves_season_and_rejects_unknown_year(self):
        self.tmdb.api_key = ""
        self.douban.suggest.return_value = fixture("douluo")
        show = entry(title="斗罗大陆1 第一季", category="animation", year="2018")
        self.assertEqual(self.mapper.enrich((show,))[0]["douban_id"], "27040807")
        show = entry(title="斗罗大陆", category="animation", year="2018")
        self.assertEqual(self.mapper.enrich((show,))[0]["douban_id"], "")
        calls = self.douban.suggest.await_count
        show = entry(title="斗罗大陆", category="animation", year="")
        self.assertEqual(self.mapper.enrich((show,))[0]["douban_id"], "")
        self.assertEqual(self.douban.suggest.await_count, calls)

    def test_tmdb_year_can_disambiguate_douban_when_platform_year_is_unknown(self):
        card = self.mapper.enrich((entry(year=""),))[0]
        self.assertEqual(card["douban_id"], "25754848")
        self.assertEqual(card["year"], "")  # 不能回填或篡改平台事实。

    def test_bad_payload_or_field_types_and_over_limit_never_create_unique_match(self):
        bad = ({"results": [douban_row()]}, [douban_row()] * 21, [douban_row(), None])
        fields = {"id": True, "year": 2015, "title": ["琅琊榜"], "sub_title": {}, "type": True,
                  "episode": 54, "url": "https://movie.douban.com/subject/999/"}
        for payload in (*bad, *([douban_row(**{key: value})] for key, value in fields.items())):
            with self.subTest(payload=payload):
                self.douban.suggest.return_value = payload
                profile = asyncio.run(self.mapper._search_douban_profile(self.douban, entry(), "2015", 1))
                self.assertEqual(profile["douban_id"], "")
                self.assertTrue(profile["_incomplete"])

    def test_non_contract_rating_and_tracking_url_are_not_exposed(self):
        self.tmdb.api_key = ""
        self.douban.suggest.return_value = [douban_row(rating=9.9, origin_country=["CN"],
            url="https://movie.douban.com/subject/25754848/?suggest=tracking")]
        card = self.mapper.enrich((entry(),))[0]
        self.assertEqual(card["douban_id"], "25754848")
        self.assertEqual((card["rating"], card["rating_source"]), (None, ""))
        self.assertIsNone(card["origin_country"])
        self.assertNotIn("tracking", json.dumps(card))

    def test_stale_good_profile_survives_both_provider_failures_without_ttl_extension(self):
        first = self.mapper.enrich((entry(),))[0]
        self.now += timedelta(days=2)
        self.tmdb.get.side_effect = RuntimeError("offline outage")
        self.douban.suggest.side_effect = RuntimeError("offline outage")
        self.assertEqual(self.mapper.enrich((entry(),))[0], first)
        self.assertEqual(self.cache.get(self.mapper.key(entry())).status, "stale")

    def test_invalid_images_do_not_erase_existing_good_images_of_same_work(self):
        first = self.mapper.enrich((entry(),))[0]
        self.now += timedelta(days=2)
        self.tmdb.get.return_value = tmdb_payload(tmdb_row(poster_path="https://bad.invalid/image.jpg"))
        self.douban.suggest.return_value = [douban_row(img="https://bad.invalid/image.jpg")]
        second = self.mapper.enrich((entry(),))[0]
        self.assertEqual(second["tmdb_poster_key"], first["tmdb_poster_key"])
        self.assertEqual(second["douban_poster_key"], first["douban_poster_key"])

    def test_changed_work_id_cannot_inherit_old_poster_or_country(self):
        self.mapper.enrich((entry(),))
        self.now += timedelta(days=2)
        self.tmdb.get.return_value = tmdb_payload(tmdb_row(id=43, poster_path=None, origin_country=None))
        self.douban.suggest.return_value = [douban_row(id="25754849", img="")]
        card = self.mapper.enrich((entry(),))[0]
        self.assertEqual((card["tmdb_id"], card["douban_id"]), ("43", "25754849"))
        self.assertEqual(card["poster_key"], "")
        self.assertIsNone(card["origin_country"])

    def test_negative_cache_expires_and_v2_is_not_used(self):
        old_key = DiscoveryCache.make_key("calendar-tmdb", "profile-v2", "tv", 1, {
            "source": entry().source, "id": entry().source_id, "title": entry().title,
            "year": entry().year, "category": entry().category,
        })
        self.assertNotEqual(self.mapper.key(entry()), old_key)
        self.cache.set_success(old_key, "calendar-tmdb", _empty("not_configured"), ttl_seconds=86400, stale_seconds=86400)
        self.tmdb.get.return_value, self.douban.suggest.return_value = tmdb_payload(), []
        self.assertEqual(self.mapper.enrich((entry(),))[0]["mapping_status"], "unmatched")
        self.now += timedelta(hours=2)
        self.tmdb.get.return_value = tmdb_payload(tmdb_row())
        self.douban.suggest.return_value = fixture()
        self.assertEqual(self.mapper.enrich((entry(),))[0]["tmdb_id"], "42")

    def test_legacy_cache_keeps_unknown_country_and_cannot_replace_schedule_fields(self):
        self.mapper.enrich((entry(),))
        previous = self.cache.get(self.mapper.key(entry())).payload
        assert previous is not None
        previous.pop("origin_country")
        previous.update(free_progress="伪造进度", events=[], year="1900", source_id="42", url="http://bad.invalid")
        self.cache.set_success(self.mapper.key(entry()), "calendar-tmdb", previous, ttl_seconds=10, stale_seconds=10)
        card = self.mapper.enrich((entry(),))[0]
        for key, value in entry().to_dict().items():
            self.assertEqual(card[key], value)
        self.assertIsNone(card["origin_country"])
        self.assertIsNone(self.mapper.enrich((entry(),))[0]["origin_country"])
        self.tmdb.get.assert_awaited_once()
        self.douban.suggest.assert_awaited_once()
        self.now += timedelta(seconds=11)
        self.assertEqual(self.mapper.enrich((entry(),))[0]["origin_country"], ["CN"])
        self.assertEqual(self.tmdb.get.await_count, 2)
        self.assertEqual(self.douban.suggest.await_count, 2)

    def test_same_programme_only_queries_each_provider_once_per_enrich(self):
        second = entry(events=(CalendarEvent("2026-09-10", "19:30", "第5集"),))
        cards = self.mapper.enrich((entry(), second))
        self.assertNotEqual(cards[0]["events"], cards[1]["events"])
        self.assertTrue(all(card["origin_country"] == ["CN"] for card in cards))
        self.assertEqual(self.mapper.enrich((entry(), second)), cards)
        self.tmdb.get.assert_awaited_once()
        self.douban.suggest.assert_awaited_once()
        params = self.tmdb.get.await_args.args[1]
        self.assertNotIn("source_999", str(params))
        self.assertEqual(params["query"], "琅琊榜")

    def test_deadline_preserves_attempted_programme_and_marks_only_unattempted_pending(self):
        cancelled = []
        async def slow(*args, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)
        self.tmdb.get.side_effect = slow
        self.mapper.budget_seconds = 0.035
        started = time.monotonic()
        shows = (entry(), entry(source_id="source_2"))
        cards = self.mapper.enrich(shows)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual([card["mapping_status"] for card in cards], ["unmatched", "pending"])
        self.assertEqual(cancelled, [True])
        self.tmdb.aclose.assert_awaited_once()
        self.douban.suggest.assert_not_awaited()
        for card, show in zip(cards, shows):
            self.assertEqual(card["events"], show.to_dict()["events"])

    def test_sync_deadline_does_not_join_cancelled_dns_executor_work(self):
        release = threading.Event()
        async def dns_wait(*args, **kwargs):
            # FixedHostHttpClient / HTTPX 的 OS DNS 可能使用默认 executor。
            await asyncio.to_thread(release.wait, 0.5)
        self.tmdb.get.side_effect = dns_wait
        self.mapper.budget_seconds = 0.03
        started = time.monotonic()
        try:
            card = self.mapper.enrich((entry(),))[0]
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertEqual(card["free_progress"], entry().free_progress)
            self.tmdb.aclose.assert_awaited_once()
        finally:
            release.set()

    def test_zero_budget_does_not_open_clients_and_keeps_cached_good_card(self):
        first = self.mapper.enrich((entry(),))[0]
        self.mapper.budget_seconds = 0
        self.mapper.client_factory = Mock(side_effect=AssertionError("unexpected construction"))
        self.mapper.douban_client_factory = Mock(side_effect=AssertionError("unexpected construction"))
        cards = self.mapper.enrich((entry(), entry(source_id="source_2")))
        self.assertEqual(cards[0], first)
        self.assertEqual(cards[1]["mapping_status"], "pending")
        self.mapper.client_factory.assert_not_called()
        self.mapper.douban_client_factory.assert_not_called()

    def test_cache_read_and_write_errors_are_optional_and_cannot_drop_facts(self):
        with patch.object(self.cache, "get", side_effect=RuntimeError("cache offline")), \
                patch.object(self.cache, "set_success", side_effect=RuntimeError("cache offline")):
            card = self.mapper.enrich((entry(),))[0]
        self.assertEqual(card["tmdb_id"], "42")
        self.assertEqual(card["poster_provider"], "tmdb")
        self.assertEqual(card["free_progress"], entry().free_progress)

    def test_refresh_tmdb_year_invalidates_cached_douban_from_a_different_work(self):
        show = entry(year="")
        self.tmdb.api_key = ""
        # 先仅有年份明确的平台映射，随后测试与之相同 key 下的部分恢复状态。
        previous = {**_empty("matched"), "douban_id": "25754848",
                    "douban_poster_key": _douban_poster_key(douban_row()["img"]),
                    "poster_provider": "douban", "_douban_year": "2015",
                    "_douban_complete": True, "_tmdb_complete": False}
        previous["poster_key"] = previous["douban_poster_key"]
        self.cache.set_success(self.mapper.key(show), "calendar-tmdb", previous, ttl_seconds=10, stale_seconds=10)
        self.tmdb.api_key = "offline-key"
        self.tmdb.get.return_value = tmdb_payload(tmdb_row(first_air_date="2016-01-01"))
        self.douban.suggest.return_value = fixture()
        card = self.mapper.enrich((show,))[0]
        self.assertEqual(card["tmdb_id"], "42")
        self.assertEqual(card["douban_id"], "")
        self.assertEqual(card["douban_poster_key"], "")
        self.douban.suggest.assert_awaited_once()

    def test_never_ending_client_cleanup_is_bounded(self):
        async def slow_close():
            await asyncio.sleep(10)
        self.tmdb.aclose.side_effect = slow_close
        started = time.monotonic()
        card = self.mapper.enrich((entry(),))[0]
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(card["tmdb_id"], "42")
        self.douban.aclose.assert_awaited_once()

    def test_damaged_tmdb_envelope_is_not_cached_as_a_negative(self):
        self.tmdb.get.return_value = {**tmdb_payload(tmdb_row()), "total_results": 21, "total_pages": 2}
        self.douban.suggest.return_value = []
        self.assertEqual(self.mapper.enrich((entry(),))[0]["tmdb_id"], "")
        self.assertEqual(self.cache.get(self.mapper.key(entry())).status, "miss")
        for key, value in (("id", True), ("genre_ids", [False]), ("adult", "false"), ("name", {})):
            self.assertFalse(CalendarMetadata._matches(entry(), tmdb_row(**{key: value})))


class PosterKeyTests(unittest.TestCase):
    def test_douban_url_whitelist_matches_existing_proxy(self):
        for host in ("img1", "img2", "img3", "img9", "qnmob3"):
            key = _douban_poster_key(f"https://{host}.doubanio.com/view/photo/public/p1.jpg")
            self.assertTrue(key)
            self.assertEqual(_canonical_poster_key("douban", key), ("douban", key))

    def test_malformed_or_unsafe_image_urls_are_not_proxy_keys(self):
        path = "/view/photo/public/p1.jpg"
        for url in (None, {}, "img1.doubanio.com" + path, "//img1.doubanio.com" + path,
                    "http://img1.doubanio.com" + path, "https://img4.doubanio.com" + path,
                    "https://img1.doubanio.com.evil.invalid" + path, "https://127.0.0.1" + path,
                    "https://user:secret@img1.doubanio.com" + path,
                    "https://img1.doubanio.com:443" + path, "https://img1.doubanio.com" + path + "?token=x",
                    "https://img1.doubanio.com" + path + "#x", "https://img1.doubanio.com/a/../p1.jpg",
                    "https://img1.doubanio.com/a/%2e%2e/p1.jpg", "https://img1.doubanio.com//p1.jpg",
                    "https://img1.doubanio.com\\@evil.invalid/p1.jpg",
                    "https://img1.doubanio.com/\np1.jpg", "https://img1.doubanio.com/" + "a" * 1100):
            with self.subTest(url=url):
                self.assertEqual(_douban_poster_key(url), "")


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks=(), slow=False):
        self.chunks, self.slow, self.closed = chunks, slow, False

    async def __aiter__(self):
        if self.slow:
            while True:
                await asyncio.sleep(0.005)
                yield b" "
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class TrackingTransport(httpx.MockTransport):
    closed = False

    async def aclose(self):
        self.closed = True
        await super().aclose()


class DoubanHttpTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler, **kwargs):
        transport = TrackingTransport(handler)
        client = CalendarDoubanClient(
            transport=transport, resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
            min_interval=0, **kwargs,
        )
        self.addAsyncCleanup(client.aclose)
        return client, transport

    async def test_public_fixed_endpoint_no_config_credentials_or_received_cookies_forwarded(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers["host"], "movie.douban.com")
            self.assertEqual(request.url.path, "/j/subject_suggest")
            self.assertEqual(dict(request.url.params), {"q": "琅琊榜"})
            self.assertEqual(request.headers["accept-encoding"], "identity")
            for header in ("cookie", "authorization", "proxy-authorization"):
                self.assertNotIn(header, request.headers)
            return httpx.Response(200, json=fixture(), headers={"Set-Cookie": "dbcl2=must-not-return; Path=/"})
        with patch("app.config.get", side_effect=AssertionError("must not read configuration")):
            client, transport = self.client(handler)
            await client.suggest("琅琊榜", deadline_at=time.monotonic() + 1)
            await client.suggest("琅琊榜", deadline_at=time.monotonic() + 1)
            await client.aclose()
        self.assertEqual(len(calls), 2)
        self.assertTrue(transport.closed)

    async def test_slow_drip_deadline_closes_response_and_transport(self):
        body = Body(slow=True)
        client, transport = self.client(lambda request: httpx.Response(200, stream=body))
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await client.suggest("琅琊榜", deadline_at=started + 0.035)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(body.closed)
        await client.aclose()
        self.assertTrue(transport.closed)

    async def test_expired_deadline_does_not_start_request(self):
        client, _ = self.client(lambda request: self.fail("unexpected request"))
        with self.assertRaises(TimeoutError):
            await client.suggest("琅琊榜", deadline_at=time.monotonic() - 1)

    async def test_throttle_wait_is_inside_absolute_deadline_and_quota_is_bounded(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=[])
        client, _ = self.client(handler, max_requests=1)
        await client.suggest("琅琊榜", deadline_at=time.monotonic() + 1)
        with self.assertRaises(SourceUnavailable):
            await client.suggest("琅琊榜", deadline_at=time.monotonic() + 1)
        self.assertEqual(len(calls), 1)
        transport = TrackingTransport(handler)
        paced = CalendarDoubanClient(transport=transport, min_interval=2,
                                    resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))])
        self.addAsyncCleanup(paced.aclose)
        await paced.suggest("琅琊榜", deadline_at=time.monotonic() + 1)
        with self.assertRaises(TimeoutError):
            await paced.suggest("琅琊榜", deadline_at=time.monotonic() + 0.03)
        self.assertEqual(len(calls), 2)

    async def test_redirects_challenges_and_failures_are_never_retried_or_followed(self):
        for status in (301, 302, 307, 401, 403, 429, 503):
            with self.subTest(status=status):
                calls = []
                body = Body((b"{}",))
                def handler(request, calls=calls, status=status, body=body):
                    calls.append(request)
                    return httpx.Response(status, headers={"Location": "https://evil.invalid/"}, stream=body)
                client, _ = self.client(handler)
                with self.assertRaises(SourceUnavailable):
                    await client.suggest("琅琊榜", deadline_at=time.monotonic() + 1)
                self.assertEqual(len(calls), 1)
                self.assertTrue(body.closed)
        client, _ = self.client(lambda request: httpx.Response(200, text="<html>captcha verification required</html>"))
        with self.assertRaises(SourceUnavailable):
            await client.suggest("琅琊榜", deadline_at=time.monotonic() + 1)

    async def test_actual_and_declared_byte_limits_compression_and_json_shape(self):
        cases = (({"Content-Length": str(2 * 1024 * 1024 + 1)}, (b"[]",)),
                 ({"Content-Length": "-1"}, (b"[]",)),
                 ({"Content-Length": "9" * 5000}, (b"[]",)),
                 ({}, (b" " * (1024 * 1024), b" " * (1024 * 1024), b"x")),
                 ({"Content-Encoding": "gzip"}, (b"compressed-not-read",)),
                 ({}, (b"invalid json",)), ({}, (b"{}",)),
                 ({}, (json.dumps([{}] * 21).encode(),)), ({}, (b"[null]",)))
        for headers, chunks in cases:
            with self.subTest(headers=str(headers)[:100], lengths=[len(chunk) for chunk in chunks]):
                body = Body(chunks)
                client, _ = self.client(lambda request, headers=headers, body=body: httpx.Response(200, headers=headers, stream=body))
                with self.assertRaises(SourceUnavailable):
                    await client.suggest("琅琊榜", deadline_at=time.monotonic() + 1)
                self.assertTrue(body.closed)

    async def test_private_dns_and_bad_input_fail_without_transport_and_redact_errors(self):
        client = CalendarDoubanClient(transport=httpx.MockTransport(lambda req: self.fail("network")),
                                     resolver=lambda host, port: [(2, 1, 6, "", ("127.0.0.1", port))], min_interval=0)
        self.addAsyncCleanup(client.aclose)
        with self.assertRaises(SourceUnavailable):
            await client.suggest("琅琊榜", deadline_at=time.monotonic() + 1)
        for title in ("", {}, "a" * 201, "bad\ntitle"):
            with self.assertRaises(SourceUnavailable):
                await client.suggest(title, deadline_at=time.monotonic() + 1)
        client, _ = self.client(Mock(side_effect=httpx.ConnectError("Cookie=dbcl2=private-test-secret")))
        with self.assertRaises(SourceUnavailable) as caught:
            await client.suggest("琅琊榜", deadline_at=time.monotonic() + 1)
        self.assertNotIn("private-test-secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)


class TmdbAdditionalBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmdb_transport_error_does_not_expose_key_and_session_is_closed(self):
        settings = SimpleNamespace(api_key="private-test-secret", config_error="", language="zh-CN",
                                   base_url="https://api.themoviedb.org/3", session=SimpleNamespace(proxies={}), close=Mock())
        transport = TrackingTransport(Mock(side_effect=httpx.ConnectError("url?api_key=private-test-secret")))
        client = CalendarTMDBClient(settings_factory=lambda: settings, transport=transport)
        settings.close.assert_called_once()
        try:
            with self.assertRaises(SourceUnavailable) as caught:
                await client.get("/search/tv", {}, deadline_at=time.monotonic() + 1)
            self.assertNotIn("private-test-secret", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
        finally:
            await client.aclose()
        self.assertTrue(transport.closed)

    async def test_deadlines_and_parameter_whitelist_fail_before_request(self):
        settings = SimpleNamespace(api_key="fixture", config_error="", language="zh-CN",
                                   base_url="https://api.themoviedb.org/3", session=SimpleNamespace(proxies={}), close=Mock())
        client = CalendarTMDBClient(settings_factory=lambda: settings,
                                    transport=httpx.MockTransport(lambda req: self.fail("network")))
        try:
            for deadline in (float("inf"), float("nan"), "tomorrow"):
                with self.assertRaises(SourceUnavailable):
                    await client.get("/search/tv", {}, deadline_at=deadline)
            with self.assertRaises(TimeoutError):
                await client.get("/search/tv", {}, deadline_at=time.monotonic() - 1)
            with self.assertRaises(SourceUnavailable):
                await client.get("/search/tv", {"Cookie": "dbcl2=secret"}, deadline_at=time.monotonic() + 1)
        finally:
            await client.aclose()
