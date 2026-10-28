"""免费日历核心：已知免费事实、星期/缓存/后台生命周期与安全传输。"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.clients.http import HttpSecurityError
from app.discovery.cache import DiscoveryCache
from app.discovery.calendar.http import CalendarHttp
from app.discovery.calendar.metadata import CalendarMetadata
from app.discovery.calendar.models import (
    CalendarEntry,
    CalendarEvent,
    SourceResult,
    SourceUnavailable,
)
from app.discovery.calendar.service import CalendarService, calendar_dates
from tests.support import isolated_test_database


def entry(**changes):
    return replace(CalendarEntry("tencent", "show_1", "离线测试动漫", "animation", "https://v.qq.com/x/cover/show_1.html",
                                 year="2026", free_progress="免费更新至第3集", free_weekdays=(1, 3),
                                 free_schedule="非会员每周一、三更新", evidence="明确非会员排期"), **changes)


class NoNetwork:
    closed = 0
    def __init__(self, hosts):
        self.hosts = hosts
    async def aclose(self):
        type(self).closed += 1


class RawMetadata:
    def enrich(self, entries):
        return [e.to_dict() for e in entries]


class Source:
    source = "tencent"
    allowed_hosts: frozenset[str] | set[str] = frozenset({"v.qq.com"})
    calls = 0
    failure = False
    entries: tuple[CalendarEntry, ...] = (entry(),)
    async def fetch(self, http):
        self.calls += 1
        if self.failure:
            raise SourceUnavailable("公开数据暂不可用")
        return SourceResult(self.entries, "partial", "公开样本，非全站", sampled=2, ignored=1)


class CalendarCoreTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.now = datetime(2026, 9, 9, 12)
        self.tick = 100.0
        self.cache = DiscoveryCache(clock=lambda: self.now)
        self.source = Source()
        self.service = CalendarService(
            providers={"tencent": self.source}, cache=self.cache, metadata=RawMetadata(),
            http_factory=NoNetwork, submit=lambda fn: fn(), clock=lambda: self.now,
            monotonic=lambda: self.tick,
        )
        self.addCleanup(self.service.shutdown)

    def test_only_animation_is_published_even_when_a_provider_supplies_tv(self):
        self.source.entries = (entry(), entry(source_id="tv_show", category="tv", title="非动漫节目"))
        data = self.service.get_week()
        self.assertEqual(data["items_count"], 1)
        self.assertEqual(data["sources"][0]["ignored"], 2)
        get_result = self.cache.get(self.service.key("tencent")).payload
        assert get_result is not None
        cached_rows = get_result["entries"]
        self.assertEqual([row["category"] for row in cached_rows], ["animation"])
        self.assertTrue(all(card["category"] == "animation" for day in data["days"] for card in day["items"]))

    def test_calendar_scope_version_does_not_reuse_previous_tv_calendar_cache(self):
        previous_key = DiscoveryCache.make_key("calendar:tencent", "weekly-calendar-v3", "tv", 1, None)
        self.assertNotEqual(self.service.key("tencent"), previous_key)
        self.service.get_week()
        get_result_5 = self.cache.get(self.service.key("tencent")).payload
        assert get_result_5 is not None
        payload = dict(get_result_5)
        payload["entries"].append({**entry(source_id="old_tv", category="tv", title="旧电视剧缓存").to_dict(),
                                   "mapping_status": "pending"})
        self.cache.set_success(self.service.key("tencent"), "calendar:tencent", payload,
                               ttl_seconds=3600, stale_seconds=86400)
        data = self.service.get_week()
        self.assertEqual(data["items_count"], 1)
        self.assertFalse(data["refreshing"])
        self.assertFalse(any(card["category"] == "tv" for day in data["days"] for card in day["items"]))

    def test_legacy_cache_backfills_platform_posters_without_clearing_current_cards(self):
        self.service.get_week()
        key = self.service.key("tencent")
        get_result_6 = self.cache.get(key).payload
        assert get_result_6 is not None
        payload = dict(get_result_6)
        payload["entries"][0].pop("platform_poster_key")
        self.cache.set_success(key, "calendar:tencent", payload, ttl_seconds=3600, stale_seconds=86400)
        self.tick += 301
        pending = Future()
        self.service._submit = lambda fn: pending
        data = self.service.get_week()
        self.assertTrue(data["refreshing"])
        self.assertEqual(data["items_count"], 1)
        self.assertEqual(data["days"][0]["items"][0]["title"], "离线测试动漫")
        self.assertEqual(data["days"][0]["items"][0]["platform_poster_key"], "")
        self.assertEqual(self.source.calls, 1)
        self.service.get_week()
        self.assertEqual(len(self.service._pending), 1)
        pending.set_result(None)

    def test_valid_cache_with_explicit_missing_poster_does_not_repeatedly_refetch(self):
        self.service.get_week()
        self.tick += 301
        self.assertFalse(self.service._needs_platform_posters(self.cache.get(self.service.key("tencent")).payload))
        self.assertEqual(self.service.get_week()["items_count"], 1)
        self.assertEqual(self.source.calls, 1)

    def test_real_dated_schedule_does_not_require_a_free_label(self):
        self.source.entries = (entry(free_progress="", free_weekdays=(), free_schedule="", events=(
            CalendarEvent("2026-09-09", "10:00", "10:00更新1话"),
            CalendarEvent("2026-09-11", "11:00", "11:00更新1话"),
        )),)
        data = self.service.get_week()
        self.assertEqual([len(day["items"]) for day in data["days"]], [0, 0, 1, 0, 1, 0, 0])
        self.assertEqual(data["days"][2]["items"][0]["update_time"], "10:00")
        self.assertEqual(data["days"][4]["items"][0]["update_time"], "11:00")
        self.assertEqual(data["days"][2]["items"][0]["free_progress"], "")
        self.assertEqual(data["items_count"], 1)

    def test_event_audience_is_not_inferred_as_free_and_past_week_is_not_rebased(self):
        self.source.entries = (entry(free_progress="", free_weekdays=(), free_schedule="", events=(
            CalendarEvent("2026-09-02", "10:00", "10:00更新1话", "member"),
            CalendarEvent("2026-09-09", "12:00", "会员12:00更新1话", "member"),
        )),)
        data = self.service.get_week()
        card = data["days"][2]["items"][0]
        self.assertEqual(card["schedule_audience"], "member")
        self.assertEqual(card["free_progress"], "")
        self.assertEqual(len(card["events"]), 1)
        self.assertEqual(card["events"][0]["date"], "2026-09-09")

    def test_same_day_free_schedule_is_not_replaced_by_earlier_member_time(self):
        self.source.entries = (entry(free_progress="", free_weekdays=(), free_schedule="", events=(
            CalendarEvent("2026-09-09", "10:00", "会员10:00更新", "member"),
            CalendarEvent("2026-09-09", "20:00", "非会员20:00更新", "free"),
        )),)
        data = self.service.get_week()
        self.assertEqual(len(data["days"][2]["items"]), 1)
        card = data["days"][2]["items"][0]
        self.assertEqual(card["update_time"], "20:00")
        self.assertEqual(card["schedule_audience"], "free")
        self.assertEqual(card["free_progress"], "")
        self.assertEqual(len(card["events"]), 2)

    def test_pending_metadata_continues_without_refetching_platform_or_extending_fact_ttl(self):
        calls = []
        def enrich(entries):
            calls.append(True)
            return [{**item.to_dict(), "mapping_status": "pending" if len(calls) == 1 else "matched",
                     "tmdb_id": "" if len(calls) == 1 else "42"} for item in entries]
        self.service.metadata = SimpleNamespace(enrich=enrich)
        first = self.service.get_week()
        self.assertTrue(first["refreshing"])
        fetched = first["sources"][0]["fetched_at"]
        self.now += timedelta(seconds=10)
        self.tick += 10
        second = self.service.get_week()
        self.assertEqual(self.source.calls, 1)
        self.assertEqual(len(calls), 2)
        self.assertFalse(second["refreshing"])
        self.assertEqual(second["sources"][0]["fetched_at"], fetched)
        self.assertEqual(second["days"][0]["items"][0]["tmdb_id"], "42")
        self.now += timedelta(seconds=3591)
        self.assertEqual(self.cache.get(self.service.key("tencent")).status, "stale")

    def test_initial_metadata_completion_does_not_extend_platform_fact_deadlines(self):
        fetched_at = self.now

        def enrich(entries):
            self.now += timedelta(seconds=12)
            self.tick += 12
            return [{**item.to_dict(), "mapping_status": "matched", "tmdb_id": "42"}
                    for item in entries]

        self.service.metadata = SimpleNamespace(enrich=enrich)
        data = self.service.get_week()
        self.assertEqual(data["days"][0]["items"][0]["tmdb_id"], "42")
        self.assertEqual(self.source.calls, 1)
        self.now = fetched_at + timedelta(hours=1)
        self.assertEqual(self.cache.get(self.service.key("tencent")).status, "stale")
        self.now = fetched_at + timedelta(days=2)
        self.assertEqual(self.cache.get(self.service.key("tencent")).status, "expired")

    def test_metadata_next_batch_cannot_be_starved_by_already_unmatched_programmes(self):
        self.source.entries = (entry(source_id="one"), entry(source_id="two"), entry(source_id="three"))
        calls = []
        def enrich(entries):
            calls.append([item.source_id for item in entries])
            return [{**item.to_dict(), "mapping_status": "unmatched" if index == 0 else "pending"}
                    for index, item in enumerate(entries)]
        self.service.metadata = SimpleNamespace(enrich=enrich)
        self.service.get_week()
        for _ in range(2):
            self.now += timedelta(seconds=10)
            self.tick += 10
            data = self.service.get_week()
        self.assertEqual(calls, [["one", "two", "three"], ["two", "three"], ["three"]])
        self.assertFalse(data["refreshing"])
        self.assertEqual(data["items_count"], 3)
        self.assertEqual(self.source.calls, 1)

    def test_pending_metadata_is_bounded_even_if_enricher_never_finishes(self):
        self.service.metadata = SimpleNamespace(enrich=lambda entries: [{**item.to_dict(), "mapping_status": "pending"} for item in entries])
        self.assertTrue(self.service.get_week()["refreshing"])
        for _ in range(8):
            self.tick += 6
            self.now += timedelta(seconds=6)
            result = self.service.get_week()
        self.assertFalse(result["refreshing"])
        self.assertEqual(self.source.calls, 1)
        get_result_2 = self.cache.get(self.service.key("tencent")).payload
        assert get_result_2 is not None
        self.assertEqual(get_result_2["metadata_rounds"], 8)

    def test_only_explicit_weekdays_receive_cards_and_same_week_is_stable(self):
        data = self.service.get_week()
        self.assertEqual(data["week_start"], "2026-09-07")
        self.assertEqual([len(day["items"]) for day in data["days"]], [1, 0, 1, 0, 0, 0, 0])
        self.assertTrue(data["days"][2]["is_today"])
        self.assertEqual(data["items_count"], 1)
        self.assertFalse(data["refreshing"])
        self.service.get_week()
        self.assertEqual(self.source.calls, 1)

    def test_verified_free_progress_without_schedule_is_not_assigned_to_today(self):
        self.source.entries = (entry(free_weekdays=(), free_schedule=""),)
        data = self.service.get_week()
        self.assertTrue(all(not day["items"] for day in data["days"]))
        self.assertEqual(len(data["unscheduled"]), 1)

    def test_source_failure_retains_last_success_and_does_not_refresh_storm(self):
        original = self.service.get_week()
        self.source.failure = True
        self.now += timedelta(hours=2)
        self.tick += 7200
        data = self.service.get_week()
        self.assertEqual(data["days"][0]["items"][0]["title"], original["days"][0]["items"][0]["title"])
        self.assertEqual(data["sources"][0]["status"], "stale")
        for _ in range(5):
            self.service.get_week(force=True)
        self.assertEqual(self.source.calls, 2)

    def test_last_week_failed_cache_does_not_become_this_week_confirmed_schedule(self):
        self.now = datetime(2026, 9, 13, 23)
        self.service.get_week()
        self.now += timedelta(hours=2)
        self.tick += 7200
        self.source.failure = True
        data = self.service.get_week()
        self.assertEqual(data["week_start"], "2026-09-14")
        self.assertTrue(all(not day["items"] for day in data["days"]))
        self.assertEqual(len(data["unscheduled"]), 1)

    def test_singleflight_and_cancelled_queued_work_drain_on_shutdown(self):
        future = Future()
        self.service._submit = lambda fn: future
        self.assertTrue(self.service.get_week()["refreshing"])
        self.assertTrue(self.service.get_week(force=True)["refreshing"])
        self.assertEqual(len(self.service._pending), 1)
        self.assertTrue(self.service.shutdown())
        self.assertTrue(future.cancelled())
        with self.assertRaises(SourceUnavailable):
            self.service.get_week()

    def test_week_uses_shanghai_not_host_utc(self):
        self.assertEqual(calendar_dates(datetime(2026, 9, 6, 17, tzinfo=timezone.utc)), ("2026-09-07", "2026-09-07"))

    def test_model_rejects_unknown_free_info_and_unsafe_sources(self):
        for fields in (
            {"free_progress": "", "free_schedule": "", "free_weekdays": ()},
            {"url": "http://v.qq.com/x"}, {"url": "https://v.qq.com.evil.invalid/x"},
            {"free_weekdays": (8,)}, {"free_weekdays": (True,)}, {"free_update_time": "25:99"},
            {"free_schedule": ""}, {"evidence": ""},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                entry(**fields)


    def test_empty_partial_retains_only_unexpired_last_verified_facts(self):
        original = self.service.get_week()
        self.source.entries = ()
        self.now += timedelta(hours=2)
        self.tick += 7200
        stale = self.service.get_week()
        self.assertEqual(stale["items_count"], 1)
        self.assertEqual(stale["sources"][0]["status"], "stale")
        self.assertEqual(stale["sources"][0]["fetched_at"], original["sources"][0]["fetched_at"])
        self.now += timedelta(days=3)
        self.tick += 3 * 86400
        expired = self.service.get_week()
        self.assertEqual(expired["items_count"], 0)
        self.assertEqual(expired["sources"][0]["status"], "unavailable")

    def test_initial_empty_partial_is_short_error_not_fresh_success(self):
        self.source.entries = ()
        data = self.service.get_week()
        self.assertEqual(data["items_count"], 0)
        self.assertEqual(data["sources"][0]["status"], "unavailable")
        self.assertEqual(self.cache.get(self.service.key("tencent")).status, "error")
        self.now += timedelta(seconds=301)
        self.assertEqual(self.cache.get(self.service.key("tencent")).status, "expired")
        self.assertFalse(data["refreshing"])

    def test_legacy_empty_success_is_refetched_and_replaced_with_short_error(self):
        self.source.entries = ()
        self.cache.set_success(self.service.key("tencent"), "calendar:tencent", {
            "version": 1, "source": "tencent", "entries": [], "status": "partial",
            "fetched_at": self.now.isoformat(), "message": "未取得当前周排期",
        }, ttl_seconds=3600, stale_seconds=86400)
        data = self.service.get_week()
        self.assertEqual(self.source.calls, 1)
        self.assertEqual(data["sources"][0]["status"], "unavailable")
        self.assertEqual(self.cache.get(self.service.key("tencent")).status, "error")
        self.service.get_week()
        self.assertEqual(self.source.calls, 1)
        self.now += timedelta(seconds=301)
        self.tick += 301
        self.source.entries = (entry(),)
        recovered = self.service.get_week()
        self.assertEqual((self.source.calls, recovered["items_count"]), (2, 1))
        self.assertEqual(recovered["sources"][0]["status"], "partial")

    def test_failed_fresh_payload_can_recover_before_original_one_hour_ttl(self):
        original = self.service.get_week()
        self.source.failure = True
        self.now += timedelta(seconds=301)
        self.tick += 301
        failed = self.service.get_week(force=True)
        self.assertEqual((failed["items_count"], failed["sources"][0]["status"]), (1, "stale"))
        self.assertEqual(failed["sources"][0]["fetched_at"], original["sources"][0]["fetched_at"])
        self.service.get_week()
        self.assertEqual(self.source.calls, 2)
        self.source.failure = False
        self.now += timedelta(seconds=301)
        self.tick += 301
        recovered = self.service.get_week()
        self.assertEqual(self.source.calls, 3)
        self.assertEqual(recovered["sources"][0]["status"], "partial")
        self.assertNotEqual(recovered["sources"][0]["fetched_at"], original["sources"][0]["fetched_at"])

    def test_stale_preservation_requires_a_valid_animation_entry(self):
        self.assertFalse(self.service._has_entries({"entries": [{"category": "animation"}]}))
        self.assertFalse(self.service._has_entries({"entries": [entry(category="tv").to_dict()]}))
        self.assertTrue(self.service._has_entries({"entries": [entry().to_dict()]}))

    def test_raw_platform_facts_are_cached_before_optional_metadata(self):
        def enrich(entries):
            get_result_3 = self.cache.get(self.service.key("tencent")).payload
            assert get_result_3 is not None
            self.assertEqual(get_result_3["entries"][0]["free_progress"], entry().free_progress)
            return RawMetadata().enrich(entries)
        self.service.metadata = SimpleNamespace(enrich=enrich)
        self.assertEqual(self.service.get_week()["items_count"], 1)

    def test_publish_raw_facts_retains_existing_poster_until_metadata_completes(self):
        self.cache.set_success(CalendarMetadata.key(entry()), "calendar-tmdb", {
            "tmdb_id": "42", "poster_key": "poster.jpg", "mapping_status": "matched",
        }, ttl_seconds=3600, stale_seconds=86400)
        def enrich(entries):
            get_result_4 = self.cache.get(self.service.key("tencent")).payload
            assert get_result_4 is not None
            staged = get_result_4["entries"][0]
            self.assertEqual(staged["poster_key"], "poster.jpg")
            self.assertEqual(staged["tmdb_id"], "42")
            return [staged]
        self.service.metadata = SimpleNamespace(enrich=enrich)
        self.assertEqual(self.service.get_week()["days"][0]["items"][0]["poster_key"], "poster.jpg")

    def test_invalid_weekday_types_cannot_hide_behind_equal_integer_during_deduplication(self):
        for weekdays in ((1, True), (True, 1), (1, 1.0), (1.0, 1)):
            with self.subTest(weekdays=weekdays), self.assertRaises(ValueError):
                entry(free_weekdays=weekdays)

    def test_unexpected_metadata_failure_cannot_discard_verified_source_entries(self):
        fetched_at = self.now

        def fail_metadata(entries):
            self.now += timedelta(seconds=12)
            raise RuntimeError("metadata unavailable")

        self.service.metadata = SimpleNamespace(enrich=fail_metadata)
        data = self.service.get_week()
        self.assertEqual(data["items_count"], 1)
        self.assertEqual(data["sources"][0]["status"], "partial")
        self.assertEqual(data["days"][0]["items"][0]["mapping_status"], "unmatched")
        self.now = fetched_at + timedelta(hours=1)
        self.assertEqual(self.cache.get(self.service.key("tencent")).status, "stale")


class GatedSource(Source):
    def __init__(self, source="tencent"):
        self.source = source
        self.allowed_hosts = {"v.qq.com" if source == "tencent" else "www.iqiyi.com"}
        self.entries = (entry(source=source, url=f"https://{next(iter(self.allowed_hosts))}/show_1.html"),)
        self.calls = 0
        self.entered, self.release = threading.Event(), threading.Event()

    async def fetch(self, http):
        self.calls += 1
        self.entered.set()
        # 故意占住真实采集线程，模拟不能被 asyncio 超时立即终止的同步工作。
        if not self.release.wait(10):
            raise AssertionError("测试必须释放采集线程")
        if self.failure:
            raise SourceUnavailable("受控来源失败")
        return SourceResult(self.entries, "ok", "受控平台事实")


class CalendarSourceWaitTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.readers = self.enterContext(ThreadPoolExecutor(max_workers=3, thread_name_prefix="calendar-reader-test"))
        for name in ("getaddrinfo", "create_connection", "socket.connect", "socket.connect_ex"):
            self.enterContext(patch("socket." + name, side_effect=AssertionError("禁止真实网络/DNS")))
        self.now = datetime(2026, 9, 9, 12)

    def service(self, *sources, metadata=None, source_timeout=5):
        service = CalendarService(
            providers={source.source: source for source in sources},
            cache=DiscoveryCache(clock=lambda: self.now), metadata=metadata or RawMetadata(),
            http_factory=NoNetwork, clock=lambda: self.now, source_timeout=source_timeout,
        )

        def close():
            for source in sources:
                source.release.set()
            service.shutdown()
            if service._executor is not None:
                service._executor.shutdown(wait=True, cancel_futures=True)

        self.addCleanup(close)
        return service

    def start_refresh(self, service, *sources):
        page = self.readers.submit(service.get_week).result(timeout=2)
        for source in sources:
            self.assertTrue(source.entered.wait(2), "真实采集线程未进入 provider")
        return page

    def observe_waiters(self, service, count=1):
        entered, identities = threading.Event(), set()
        original_wait = service._guard.wait

        def wait(timeout=None):
            # 只记录线程确实进入 Condition；保留真实的锁释放、通知和超时行为。
            identities.add(threading.get_ident())
            if len(identities) >= count:
                entered.set()
            return original_wait(timeout)

        self.enterContext(patch.object(service._guard, "wait", side_effect=wait))
        return entered

    def test_wait_sources_returns_real_facts_published_by_inflight_refresh(self):
        source = GatedSource()
        service = self.service(source)
        page = self.start_refresh(service, source)
        self.assertEqual((page["items_count"], page["sources"][0]["status"]), (0, "loading"))
        entered = self.observe_waiters(service)
        waiting = self.readers.submit(service.get_week, wait_sources=("tencent",))
        self.assertTrue(entered.wait(2))
        self.assertFalse(waiting.done())
        source.release.set()
        data = waiting.result(timeout=2)
        self.assertEqual((data["items_count"], data["sources"][0]["status"]), (1, "ok"))
        self.assertEqual(data["days"][2]["items"][0]["free_progress"], source.entries[0].free_progress)
        self.assertEqual(source.calls, 1)

    def test_wait_deadline_preserves_ready_facts_while_selected_source_is_blocked(self):
        ready, blocked = GatedSource(), GatedSource("iqiyi")
        service = self.service(ready, blocked, source_timeout=1)
        self.start_refresh(service, ready, blocked)
        refresh = service._pending["tencent"]
        ready.release.set()
        assert refresh is not None
        refresh.result(timeout=2)
        entered = self.observe_waiters(service)
        started = time.monotonic()
        waiting = self.readers.submit(service.get_week, wait_sources=("tencent", "iqiyi"))
        self.assertTrue(entered.wait(2), "已有部分事实不能跳过选中冷源的等待")
        data = waiting.result(timeout=3)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, service.source_timeout + 1)
        self.assertLess(elapsed, service.source_timeout + 3)
        self.assertEqual(data["items_count"], 1)
        self.assertEqual({row["id"]: row["status"] for row in data["sources"]}["iqiyi"], "loading")
        self.assertEqual(data["days"][2]["items"][0]["source"], "tencent")
        self.assertFalse(blocked.release.is_set())
        assert service._pending['iqiyi'] is not None
        self.assertFalse(service._pending["iqiyi"].done())
        self.assertEqual((ready.calls, blocked.calls), (1, 1))

    def test_cancellation_stops_reader_without_cancelling_shared_refresh(self):
        source = GatedSource()
        service = self.service(source)
        self.start_refresh(service, source)
        refresh = service._pending["tencent"]
        entered = self.observe_waiters(service)
        cancelled = threading.Event()
        waiting = self.readers.submit(service.get_week, wait_sources=("tencent",), cancelled=cancelled.is_set)
        self.assertTrue(entered.wait(2))
        cancelled.set()  # 不发送来源通知，取消回调本身也必须有界生效。
        with self.assertRaisesRegex(SourceUnavailable, "已停止"):
            waiting.result(timeout=2)
        self.assertFalse(service.closed)
        assert refresh is not None
        self.assertFalse(refresh.done())
        self.assertFalse(source.release.is_set())
        source.release.set()
        refresh.result(timeout=2)
        self.assertEqual(service.get_week(wait_sources=("tencent",))["items_count"], 1)
        self.assertEqual(source.calls, 1)

    def test_selected_source_does_not_wait_for_another_inflight_source(self):
        selected, other = GatedSource(), GatedSource("iqiyi")
        service = self.service(selected, other)
        self.start_refresh(service, selected, other)
        entered = self.observe_waiters(service)
        waiting = self.readers.submit(service.get_week, wait_sources=("tencent",))
        self.assertTrue(entered.wait(2))
        selected.release.set()
        data = waiting.result(timeout=2)
        self.assertEqual(data["items_count"], 1)
        self.assertEqual({row["id"]: row["status"] for row in data["sources"]}["iqiyi"], "loading")
        self.assertFalse(other.release.is_set())
        assert service._pending['iqiyi'] is not None
        self.assertFalse(service._pending["iqiyi"].done())
        self.assertEqual((selected.calls, other.calls), (1, 1))

    def test_platform_fact_publication_unblocks_reader_before_metadata_finishes(self):
        metadata_entered, metadata_release = threading.Event(), threading.Event()

        class Metadata(RawMetadata):
            def enrich(self, entries):
                metadata_entered.set()
                if not metadata_release.wait(10):
                    raise AssertionError("测试必须释放元数据线程")
                return super().enrich(entries)

        source = GatedSource()
        service = self.service(source, metadata=Metadata())
        self.addCleanup(metadata_release.set)
        self.start_refresh(service, source)
        entered = self.observe_waiters(service)
        waiting = self.readers.submit(service.get_week, wait_sources=("tencent",))
        self.assertTrue(entered.wait(2))
        source.release.set()
        self.assertTrue(metadata_entered.wait(2))
        data = waiting.result(timeout=2)
        self.assertEqual((data["items_count"], data["sources"][0]["status"]), (1, "ok"))
        self.assertEqual(data["days"][2]["items"][0]["free_progress"], source.entries[0].free_progress)
        self.assertFalse(metadata_release.is_set())
        assert service._pending['tencent'] is not None
        self.assertFalse(service._pending["tencent"].done())
        self.assertEqual(source.calls, 1)

    def test_concurrent_readers_and_web_refresh_share_one_real_source_fetch(self):
        source = GatedSource()
        service = self.service(source)
        self.start_refresh(service, source)
        entered = self.observe_waiters(service, count=3)
        waiting = [self.readers.submit(service.get_week, wait_sources=("tencent",)) for _ in range(3)]
        self.assertTrue(entered.wait(2), "三个独立读线程必须同时等待同一采集")
        page = service.get_week(force=True)
        self.assertEqual((page["items_count"], page["sources"][0]["status"]), (0, "loading"))
        self.assertTrue(all(not future.done() for future in waiting))
        self.assertEqual((source.calls, len(service._pending)), (1, 1))
        source.release.set()
        results = [future.result(timeout=2) for future in waiting]
        self.assertEqual([result["items_count"] for result in results], [1, 1, 1])
        self.assertTrue(all(result["days"][2]["items"][0]["free_progress"] == source.entries[0].free_progress
                            for result in results))
        self.assertEqual(source.calls, 1)

    def test_failed_source_unblocks_waiters_without_an_automatic_retry(self):
        source = GatedSource()
        source.failure = True
        service = self.service(source)
        self.start_refresh(service, source)
        entered = self.observe_waiters(service)
        waiting = self.readers.submit(service.get_week, wait_sources=("tencent",))
        self.assertTrue(entered.wait(2))
        source.release.set()
        data = waiting.result(timeout=2)
        self.assertEqual((data["items_count"], data["sources"][0]["status"]), (0, "unavailable"))
        self.assertFalse(data["refreshing"])
        self.assertEqual(service.cache.get(service.key("tencent")).status, "error")
        self.assertEqual(service.get_week(wait_sources=("tencent",))["items_count"], 0)
        self.assertEqual(source.calls, 1)


class CalendarDNSTimeoutTests(unittest.TestCase):
    def test_dns_release_contract_allows_slow_error_cache_publication(self):
        original = DiscoveryCache.set_error

        def slow_publication(cache, *args, **kwargs):
            time.sleep(0.65)  # Storage publication is distinct from DNS cancellation.
            return original(cache, *args, **kwargs)

        with patch.object(DiscoveryCache, "set_error", slow_publication):
            self.test_source_timeout_publishes_error_and_releases_worker_before_os_dns_returns()

    def test_default_executor_join_cannot_satisfy_dns_release_contract(self):
        def join_default_executor(service, source):
            return asyncio.run(service._fetch(source))

        with patch.object(CalendarService, "_fetch_sync", join_default_executor):
            with self.assertRaisesRegex(AssertionError, "错误缓存未在DNS释放前发布"):
                self.test_source_timeout_publishes_error_and_releases_worker_before_os_dns_returns()

    def test_source_timeout_publishes_error_and_releases_worker_before_os_dns_returns(self):
        self.enterContext(isolated_test_database())
        entered, release, returned, closed, published, drained = (threading.Event() for _ in range(6))
        calls, threads = [], []
        for name in ("getaddrinfo", "create_connection", "socket.connect", "socket.connect_ex"):
            self.enterContext(patch("socket." + name, side_effect=AssertionError("禁止真实网络/DNS")))

        def resolve(host, port):
            threads.append(threading.current_thread())
            entered.set()
            if not release.wait(5):
                raise AssertionError("测试 resolver 必须有界释放")
            returned.set()
            return [(2, 1, 6, "", ("93.184.216.34", port))]

        def handle(request):
            calls.append(request)
            return httpx.Response(200, json={})

        class HTTP(CalendarHttp):
            async def aclose(self):
                await super().aclose()
                closed.set()

        class DNSProvider:
            allowed_hosts = {"v.qq.com"}
            async def fetch(self, http):
                await http.get_json("https://v.qq.com/public")
                return SourceResult((entry(),), "partial", "offline")

        class Cache(DiscoveryCache):
            def set_error(self, *args, **kwargs):
                super().set_error(*args, **kwargs)
                published.set()

        class Service(CalendarService):
            def _finished(self, source, future):
                super()._finished(source, future)
                drained.set()

        service = Service(providers={"tencent": DNSProvider()}, metadata=RawMetadata(), cache=Cache(),
            source_timeout=1, http_factory=lambda hosts: HTTP(hosts, min_interval=0,
                resolver=resolve, transport=httpx.MockTransport(handle)))
        future = None
        try:
            service.get_week()
            self.assertTrue(entered.wait(2))
            future = service._pending["tencent"]
            assert (future) is not None
            self.assertTrue(closed.wait(2))
            # HTTP关闭不等于缓存发布/队列清理已经完成。DNS始终由本测试持有，
            # 因此按真实阶段等待仍能证伪旧asyncio.run的默认executor join。
            self.assertTrue(published.wait(2), "错误缓存未在DNS释放前发布")
            self.assertTrue(drained.wait(1), "刷新队列未在DNS释放前清理")
            future.result(timeout=0)
            self.assertFalse(returned.is_set())
            self.assertEqual(calls, [])
            self.assertEqual(service._pending, {})
            cached = service.cache.get(service.key("tencent"))
            self.assertEqual(cached.status, "error")
            self.assertEqual(service.get_week()["sources"][0]["status"], "unavailable")
            self.assertTrue(service.shutdown())  # 业务任务已清理，不声称强杀OS线程。
            from app.discovery.calendar import service as module
            with patch.object(module, "_service", service):
                replacement = module.get_calendar_service()
                self.assertIsNot(replacement, service)
                replacement.shutdown()
            release.set()
            self.assertTrue(returned.wait(1))
            self.assertEqual(calls, [])  # 迟到DNS不得重新发HTTP或覆盖error cache。
            self.assertEqual(service.cache.get(service.key("tencent")), cached)
        finally:
            release.set()
            service.shutdown()
            if future is not None:
                future.result(timeout=3)
            self.assertTrue(returned.wait(2))
            for worker in threads:
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())


class CalendarDNSLoopTests(unittest.TestCase):
    def test_owned_loop_dns_preserves_native_arguments_results_and_other_loops(self):
        from app.discovery.calendar.http import _install_bounded_dns
        loop, other = asyncio.new_event_loop(), asyncio.new_event_loop()
        original_other = other.getaddrinfo
        # 配置型 TMDB 允许私网/IPv6 和自定义端口，不添加公开来源的白名单限制。
        records = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 8443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 8443)),
        ]
        try:
            with patch("socket.getaddrinfo", return_value=records) as resolver:
                _install_bounded_dns(loop)
                result = loop.run_until_complete(loop.getaddrinfo(
                    "configured.internal", 8443, family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM, proto=6, flags=socket.AI_ADDRCONFIG,
                ))
                resolver.assert_called_once_with(
                    "configured.internal", 8443, family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM, proto=6, flags=socket.AI_ADDRCONFIG,
                )
                self.assertIs(result, records)
                self.assertIs(socket.getaddrinfo, resolver)
                self.assertEqual(other.getaddrinfo, original_other)
        finally:
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()
            other.close()


class CalendarHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_dns_keeps_global_capacity_until_resolver_really_returns(self):
        from app.discovery.calendar import http as module
        entered, release, slot_returned = (threading.Event() for _ in range(3))
        calls = []

        class Slots(threading.BoundedSemaphore):
            def release(self, n=1):
                super().release(n)
                slot_returned.set()

        slots = Slots(1)
        def blocked(host, port):
            entered.set()
            if not release.wait(5):
                raise AssertionError("测试 resolver 必须释放")
            return [(2, 1, 6, "", ("93.184.216.34", port))]

        fast = Mock(return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])
        def handle(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})

        with patch.object(module, "_DNS_SLOTS", slots):
            first = CalendarHttp({"v.qq.com"}, resolver=blocked, transport=httpx.MockTransport(handle), min_interval=0)
            second = CalendarHttp({"v.qq.com"}, resolver=fast, transport=httpx.MockTransport(handle), min_interval=0)
            task = asyncio.create_task(first.get_json("https://v.qq.com/public"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(slot_returned.is_set())
                with self.assertRaises(HttpSecurityError):
                    await second.get_json("https://v.qq.com/public")
                fast.assert_not_called()
                self.assertEqual(calls, [])
                release.set()
                self.assertTrue(await asyncio.to_thread(slot_returned.wait, 2))
                self.assertEqual(await second.get_json("https://v.qq.com/public"), {"ok": True})
                fast.assert_called_once()
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await first.aclose()
                await second.aclose()
                self.assertTrue(await asyncio.to_thread(slot_returned.wait, 2))

    async def test_calendar_revalidates_cached_pages_without_cookie_or_hidden_retries(self):
        calls = []
        def handle(request):
            calls.append(request)
            self.assertEqual(request.headers["cache-control"], "no-cache")
            self.assertEqual(request.headers["pragma"], "no-cache")
            self.assertEqual(request.headers["accept-encoding"], "identity")
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, json={"week": "current"}, headers={"set-cookie": "tracking=discarded; Path=/"})
        client = CalendarHttp({"www.youku.com"}, transport=httpx.MockTransport(handle),
                              resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
                              max_requests=2, min_interval=0)
        try:
            for _ in range(2):
                self.assertEqual(await client.get_json("https://www.youku.com/ku/webcomic"), {"week": "current"})
            self.assertEqual(len(calls), 2)
        finally:
            await client.aclose()

    async def test_public_schedule_rejects_compression_before_reading_or_decoding(self):
        from app.clients.http import HttpInvalidResponse
        class Wire(httpx.AsyncByteStream):
            started = False
            closed = False
            async def __aiter__(self):
                self.started = True
                yield b"must not decode"
            async def aclose(self):
                self.closed = True

        wire = Wire()
        client = CalendarHttp({"www.youku.com"}, min_interval=0,
            resolver=lambda h, p: [(2, 1, 6, "", ("93.184.216.34", p))],
            transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=wire, headers={
                "Content-Type": "text/html", "Content-Encoding": "gzip",
            })))
        try:
            with self.assertRaises(HttpInvalidResponse):
                await client.get_text("https://www.youku.com/ku/webcomic")
            self.assertFalse(wire.started)
            self.assertTrue(wire.closed)
        finally:
            await client.aclose()

    async def test_budget_does_not_retry_or_follow_redirects(self):
        calls = []
        def handle(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"ok": True})
        client = CalendarHttp({"v.qq.com"}, transport=httpx.MockTransport(handle),
                              resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
                              max_requests=1, min_interval=0)
        try:
            self.assertEqual(await client.get_json("https://v.qq.com/public"), {"ok": True})
            with self.assertRaises(SourceUnavailable):
                await client.get_json("https://v.qq.com/public")
            self.assertEqual(len(calls), 1)
        finally:
            await client.aclose()

    async def test_readonly_post_shares_dns_and_request_budget_with_get(self):
        calls = []
        def handle(request):
            calls.append(request)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.read(), b'{"page_params":{"page_type":"channel"}}')
            return httpx.Response(200, json={"data": {"CardList": []}})
        client = CalendarHttp({"pbaccess.video.qq.com"}, transport=httpx.MockTransport(handle),
                              resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
                              max_requests=1, min_interval=0)
        try:
            data = await client.post_json("https://pbaccess.video.qq.com/public", json_body={"page_params": {"page_type": "channel"}})
            assert isinstance(data, dict)
            self.assertEqual(data["data"]["CardList"], [])
            with self.assertRaises(SourceUnavailable):
                await client.get_json("https://pbaccess.video.qq.com/public")
            self.assertEqual(len(calls), 1)
        finally:
            await client.aclose()

    async def test_anonymous_headers_are_exact_endpoint_get_only_and_share_all_budgets(self):
        endpoint = "https://acs.youku.com/h5/mtop.youku.columbus.home.query/1.0/"
        calls = []
        client = CalendarHttp({"acs.youku.com", "www.youku.com"}, max_requests=2, min_interval=0,
            resolver=lambda h, p: [(2, 1, 6, "", ("93.184.216.34", p))],
            transport=httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(
                200, json={"ok": True}, headers={"Set-Cookie": "tracking=ignored; Path=/"})))
        try:
            invalid = [
                (endpoint, {"Authorization": "Bearer fixture"}),
                (endpoint, {"Host": "evil.invalid"}),
                (endpoint, {"Accept-Encoding": "gzip"}),
                (endpoint, {"Origin": "https://evil.invalid"}),
                (endpoint, {"Accept": "application/json", "accept": "application/json"}),
                (endpoint, {"Cookie": "_m_h5_tk=x; account=forbidden"}),
                (endpoint, {"Cookie": "_m_h5_tk=x\r\nX-Other: value"}),
                (endpoint, {"Cookie": "_m_h5_tk_enc=without-primary"}),
                (endpoint + "other", {"Cookie": "_m_h5_tk=fixture"}),
                (endpoint + "?x=1", {"Accept": "application/json"}),
                ("https://www.youku.com/ku/webcomic", {"Cookie": "_m_h5_tk=fixture"}),
            ]
            for url, headers in invalid:
                with self.subTest(url=url, names=list(headers)):
                    with self.assertRaises(SourceUnavailable):
                        await client.request("GET", url, headers=headers)
            with self.assertRaises(SourceUnavailable):
                await client.request("POST", endpoint, headers={"Accept": "application/json"})
            self.assertEqual((client._requests, calls), (0, []))
            response = await client.request("GET", endpoint, params={"api": "fixture"}, headers={
                "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://www.youku.com", "Referer": "https://www.youku.com/ku/webcomic",
                "Cookie": "_m_h5_tk=synthetic_4102444800000; _m_h5_tk_enc=synthetic",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(calls[0].extensions["sni_hostname"], "acs.youku.com")
            self.assertEqual(calls[0].headers["accept-encoding"], "identity")
            self.assertEqual(calls[0].headers["cookie"], "_m_h5_tk=synthetic_4102444800000; _m_h5_tk_enc=synthetic")
            self.assertFalse(list(client._client._client.cookies.jar))
            await client.get_text("https://www.youku.com/ku/webcomic")
            self.assertNotIn("cookie", calls[1].headers)
            with self.assertRaises(SourceUnavailable):
                await client.request("GET", endpoint)
            self.assertEqual(len(calls), 2)
        finally:
            await client.aclose()

    async def test_private_dns_is_rejected_before_transport(self):
        client = CalendarHttp({"v.qq.com"}, transport=httpx.MockTransport(lambda req: self.fail("transport called")),
                              resolver=lambda host, port: [(2, 1, 6, "", ("127.0.0.1", port))], min_interval=0)
        try:
            with self.assertRaises(HttpSecurityError):
                await client.get_json("https://v.qq.com/public")
        finally:
            await client.aclose()


class CalendarMetadataTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.rows = [{"id": 42, "name": "离线测试动漫", "original_name": "Fixture", "first_air_date": "2026-01-01",
                      "genre_ids": [16], "poster_path": "/poster.jpg", "vote_average": 8.5, "overview": "离线简介"}]
        self.client = SimpleNamespace(api_key="fixture", get=AsyncMock(side_effect=lambda *a, **kw: {
            "results": self.rows, "page": 1, "total_pages": 1, "total_results": len(self.rows),
        }), aclose=AsyncMock())
        self.mapper = CalendarMetadata(
            DiscoveryCache(), client_factory=lambda: self.client,
            douban_client_factory=lambda: SimpleNamespace(suggest=AsyncMock(return_value=[]), aclose=AsyncMock()),
        )

    def test_unique_exact_title_year_and_category_can_enrich_without_changing_schedule(self):
        card = self.mapper.enrich((entry(),))[0]
        self.assertEqual(card["tmdb_id"], "42")
        self.assertEqual(card["poster_key"], "poster.jpg")
        self.assertEqual(card["free_weekdays"], (1, 3))

    def test_ambiguous_title_or_wrong_category_is_not_first_result_selected(self):
        self.rows = self.rows + [dict(self.rows[0], id=43)]
        self.assertEqual(self.mapper.enrich((entry(),))[0]["tmdb_id"], "")
        self.assertEqual(CalendarMetadata._matches(entry(category="tv"), self.rows[0]), False)

    def test_poster_cannot_be_arbitrary_url(self):
        self.rows[0]["poster_path"] = "https://private.invalid/image.jpg"
        self.assertEqual(self.mapper.enrich((entry(),))[0]["poster_key"], "")

    def test_search_truncation_or_missing_pagination_cannot_prove_unique_match(self):
        for changes in ({"total_pages": 2, "total_results": 21}, {"total_results": 11},
                        {"page": 2}, {"total_pages": True}, {"total_results": None}):
            with self.subTest(changes=changes):
                payload = {"results": self.rows, "page": 1, "total_pages": 1, "total_results": 1, **changes}
                self.client.get = AsyncMock(return_value=payload)
                self.assertEqual(asyncio.run(self.mapper._search_profile(self.client, entry(), 1))["tmdb_id"], "")

    def test_client_construction_and_cleanup_errors_preserve_free_progress(self):
        self.mapper.client_factory = Mock(side_effect=RuntimeError("fixture creation failure"))
        card = self.mapper.enrich((entry(),))[0]
        self.assertEqual(card["free_progress"], entry().free_progress)
        self.assertEqual(card["tmdb_id"], "")
        self.mapper.client_factory = lambda: self.client
        self.client.aclose = AsyncMock(side_effect=RuntimeError("fixture cleanup failure"))
        self.assertEqual(self.mapper.enrich((entry(),))[0]["tmdb_id"], "42")

    def test_negative_match_expires_and_stale_match_is_revalidated(self):
        now = [datetime(2026, 9, 9, 12)]
        self.mapper.cache = DiscoveryCache(clock=lambda: now[0])
        original = self.rows
        self.rows = []
        self.assertEqual(self.mapper.enrich((entry(),))[0]["tmdb_id"], "")
        now[0] += timedelta(hours=2)
        self.rows = original
        self.assertEqual(self.mapper.enrich((entry(),))[0]["tmdb_id"], "42")
        now[0] += timedelta(days=2)
        self.rows = [dict(original[0], id=43)]
        self.assertEqual(self.mapper.enrich((entry(),))[0]["tmdb_id"], "43")

    def test_tmdb_failure_retains_stale_profile_without_caching_false_negative(self):
        now = [datetime(2026, 9, 9, 12)]
        self.mapper.cache = DiscoveryCache(clock=lambda: now[0])
        self.mapper.enrich((entry(),))
        now[0] += timedelta(days=2)
        self.client.get = AsyncMock(side_effect=RuntimeError("fixture outage"))
        self.assertEqual(self.mapper.enrich((entry(),))[0]["tmdb_id"], "42")
        self.assertEqual(self.mapper.cache.get(self.mapper.key(entry())).status, "stale")

    def test_new_tmdb_configuration_is_not_stuck_in_unconfigured_cache(self):
        self.client.api_key = ""
        self.assertEqual(self.mapper.enrich((entry(),))[0]["mapping_status"], "not_configured")
        self.client.api_key = "fixture"
        self.assertEqual(self.mapper.enrich((entry(),))[0]["tmdb_id"], "42")

    def test_optional_metadata_deadline_cancels_request_without_losing_free_fact(self):
        cancelled = []
        async def slow(*args, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)
        self.client.get = slow
        self.mapper.budget_seconds = 0.03
        started = time.monotonic()
        card = self.mapper.enrich((entry(),))[0]
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(card["free_progress"], entry().free_progress)
        self.assertEqual(card["tmdb_id"], "")
        self.assertEqual(cancelled, [True])
        self.client.aclose.assert_awaited_once()
