"""腾讯真实周排期回放；固定上海日期，不访问公网或生产配置/DB。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

import tests  # noqa: F401 -- 任何应用导入之前隔离 config/DB。
from app.discovery.calendar.http import CalendarHttp
from app.discovery.calendar.models import SourceUnavailable
from app.discovery.calendar.providers.tencent import TencentCalendarProvider

FIXTURES = Path(__file__).parent / "fixtures" / "calendar" / "tencent"
TODAY = datetime(2026, 9, 9, 21, 0)  # noqa: DTZ001 -- 刻意验证注入上海 naive datetime。


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class RecordedTencentHttp:
    """只重放2026-09-09实采的本周七日动漫POST。"""
    def __init__(self):
        self.calls = []
        self.failure = None
        self.fail_at = None
        self.initial_override = None

    def response(self, url, body):
        self.calls.append((url, body))
        if self.failure is not None and (self.fail_at is None or len(self.calls) == self.fail_at):
            raise self.failure
        params = body["page_params"]
        if params["page_id"] != "100119":
            raise AssertionError("unknown source channel")
        if "week" not in params and self.initial_override is not None:
            return self.initial_override
        day = params.get("week", "20260909")
        if "week" in params:
            assert params["un_mod_id"] == "d4afd_17f79"
            assert params["un_module_key"] == ""
            assert body["page_bypass_params"]["params"]["week"] == day
        return fixture(f"calendar_{day}.json")

    async def post_json(self, url, *, json_body):
        return self.response(url, json_body)

    async def get_text(self, *args, **kwargs):
        raise AssertionError("JSON-LD/详情壳不得再作为主源")

    async def get_json(self, *args, **kwargs):
        raise AssertionError("官方排期使用已核验的只读POST")


class TencentWeeklyFixtureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.network_attempts = []
        def forbidden(*args, **kwargs):
            self.network_attempts.append(True)
            raise AssertionError("Tencent regression forbids live sockets/DNS")
        for target in ("socket.getaddrinfo", "socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex"):
            self.enterContext(patch(target, side_effect=forbidden))
        self.http = RecordedTencentHttp()
        self.provider = TencentCalendarProvider(clock=lambda: TODAY)

    def tearDown(self):
        self.assertEqual(self.network_attempts, [])

    async def fetch(self, expected_status="ok"):
        result = await self.provider.fetch(self.http)
        self.assertEqual(result.status, expected_status)
        return result, {entry.source_id: entry for entry in result.entries}

    async def test_real_week_has_28_anime_45_events_and_six_today_cards(self):
        result, entries = await self.fetch()
        self.assertEqual(len(result.entries), 28)
        events = [event for entry in result.entries for event in entry.events]
        self.assertEqual(len(events), 45)
        self.assertEqual({event.date for event in events}, {f"2026-09-{day:02d}" for day in range(7, 14)})
        self.assertEqual(sum(any(e.date == "2026-09-09" for e in item.events) for item in entries.values()), 6)
        self.assertEqual(sum(item.category == "animation" for item in entries.values()), 28)
        self.assertEqual({item.category for item in entries.values()}, {"animation"})
        self.assertIn("7/7", result.message)
        self.assertIn("官方追漫周历", result.message)
        self.assertIn("不代表全站片库", result.message)
        self.assertEqual((result.sampled, result.ignored), (28, 0))
        self.assertEqual(result.covered_dates, tuple(f"2026-09-{day:02d}" for day in range(7, 14)))
        self.assertLessEqual(len(entries), 100)
        self.assertTrue(all(len(item.events) <= 50 and item.evidence for item in entries.values()))

    async def test_exactly_seven_animation_posts_and_no_credentials_or_dynamic_urls(self):
        await self.fetch()
        self.assertEqual(len(self.http.calls), 7)
        self.assertTrue(all(body["page_params"]["page_id"] == "100119" for _, body in self.http.calls))
        requested = {body["page_params"]["week"] for _, body in self.http.calls if "week" in body["page_params"]}
        self.assertEqual(requested, {"20260907", "20260908", "20260910", "20260911", "20260912", "20260913"})
        self.assertEqual(self.provider.allowed_hosts, frozenset({"pbaccess.video.qq.com"}))
        for url, body in self.http.calls:
            self.assertEqual(url, "https://pbaccess.video.qq.com/trpc.vector_layout.page_view.PageService/getPage?video_appid=3000010&vversion_platform=2")
            serialized = json.dumps(body)
            for forbidden in ("ams_cookies", "video_guid", "access_token", "signature", "vdevice_guid"):
                self.assertNotIn(forbidden, serialized)
            self.assertIsNone(body["page_context"])
            self.assertEqual(body["page_bypass_params"]["params"]["page_id"], "100119")

    async def test_real_vip_svip_and_free_events_stay_separate(self):
        result, entries = await self.fetch()
        ling = entries["mzc0020082u0tna"]
        self.assertEqual({(e.date, e.update_time, e.audience) for e in ling.events}, {
            ("2026-09-09", "18:00", "member"), ("2026-09-10", "10:00", "member"),
            ("2026-09-10", "10:00", "free"),
        })
        self.assertTrue(all("每周三18点SVIP更新，周四10点VIP更新" in e.schedule for e in ling.events))
        # 排期目标集数含未来日期，不能当作已播进度；普通/VIP进度同样不回填。
        self.assertTrue(all(item.free_progress == "" for item in result.entries))
        self.assertTrue(all(item.free_weekdays == () for item in result.entries))

    async def test_requested_week_not_still_selected_today_controls_event_date(self):
        _, entries = await self.fetch()
        xian = entries["mzc00200aaogpgh"]
        self.assertEqual({(e.date, e.update_time, e.audience) for e in xian.events}, {
            ("2026-09-07", "10:00", "member"), ("2026-09-07", "10:00", "free"),
            ("2026-09-13", "18:00", "member"),
        })
        # 实采周一/周日响应里的导航selected仍为9月9日；不能把它当事件日期。
        self.assertFalse(any(e.date == "2026-09-09" for e in xian.events))

    async def test_real_free_only_calendar_rows_are_not_lost_to_member_description(self):
        _, entries = await self.fetch()
        for cid, day in (("mzc00200ot2jctt", "2026-09-10"), ("mzc002002qg0pbc", "2026-09-12")):
            events = entries[cid].events
            self.assertEqual([(e.date, e.update_time, e.audience) for e in events], [(day, "10:00", "free")])
            self.assertEqual(events[0].schedule, "会员看全集")  # 受众由free_time字段证明，不从文案混猜。

    async def test_real_future_premiere_is_a_dated_plan_not_played_progress(self):
        _, entries = await self.fetch()
        upcoming = entries["mzc00200wqewdyv"]
        self.assertEqual({(e.date, e.update_time, e.audience) for e in upcoming.events}, {
            ("2026-09-11", "10:00", "member"), ("2026-09-11", "10:00", "free"),
        })
        self.assertTrue(all("9月11日首播4集" in e.schedule for e in upcoming.events))
        self.assertEqual(upcoming.free_progress, "")

    async def test_clock_accepts_naive_shanghai_aware_utc_and_date(self):
        for value in (TODAY, date(2026, 9, 9), datetime(2026, 9, 8, 18, tzinfo=timezone.utc)):
            with self.subTest(value=value):
                provider = TencentCalendarProvider(clock=lambda value=value: value)
                self.assertEqual(provider._today(), date(2026, 9, 9))
                result = await provider.fetch(RecordedTencentHttp())
                self.assertEqual(sum(len(e.events) for e in result.entries), 45)

    async def test_missing_calendar_does_not_fall_back_to_featured_animation_samples(self):
        payload = fixture("calendar_20260909.json")
        payload["data"]["CardList"] = [module for module in payload["data"]["CardList"]
                                       if module.get("type") != "channel_play_schedule"]
        self.http.initial_override = payload
        result, entries = await self.fetch("partial")
        self.assertEqual(len(self.http.calls), 1)
        self.assertIn("0/7", result.message)
        self.assertEqual(entries, {})

    async def test_upstream_failure_stops_without_retry_or_sensitive_error_echo(self):
        self.http.failure = SourceUnavailable("Cookie=private; token=private")
        result = await self.provider.fetch(self.http)
        self.assertEqual((result.status, result.entries, len(self.http.calls)), ("unavailable", (), 1))
        self.assertNotIn("private", result.message)

    async def test_later_day_failure_keeps_real_completed_dates_and_stops(self):
        self.http.fail_at = 3  # 已取得默认周三和周一，周二请求失败。
        self.http.failure = SourceUnavailable("token=private")
        result, entries = await self.fetch("partial")
        self.assertEqual((len(entries), sum(len(e.events) for e in entries.values())), (10, 11))
        self.assertEqual(len(self.http.calls), 3)
        self.assertIn("2/7", result.message)
        self.assertIn("覆盖不全", result.message)
        self.assertNotIn("private", result.message)
        self.assertEqual({event.date for item in entries.values() for event in item.events},
                         {"2026-09-07", "2026-09-09"})

    async def test_malformed_later_day_does_not_count_as_covered_or_discard_first_day(self):
        original = self.http.response
        def response(url, body):
            payload = original(url, body)
            if body["page_params"].get("week") == "20260907":
                payload["data"]["CardList"][3]["children_list"] = {"list": {"cards": None}}
            return payload
        self.http.response = response
        result, entries = await self.fetch("partial")
        self.assertEqual((len(entries), sum(len(e.events) for e in entries.values())), (6, 6))
        self.assertEqual(len(self.http.calls), 2)
        self.assertIn("1/7", result.message)
        self.assertNotIn("7/7", result.message)

    def test_fixtures_are_sanitized_real_responses_not_generated_positive_cases(self):
        provenance = fixture("provenance.json")
        self.assertEqual(provenance["kind"], "sanitized_live_weekly_calendar")
        # 旧采集批次 provenance 不改写；仅校验保留的七个动漫日历快照。
        records = [record for record in provenance["records"] if record["fixture"].startswith("calendar_")]
        self.assertEqual({record["fixture"] for record in records},
                         {f"calendar_202609{day:02d}.json" for day in range(7, 14)})
        self.assertEqual(len(records), 7)
        for record in records:
            raw = (FIXTURES / record["fixture"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), record["sanitized_sha256"])
            self.assertEqual(record["status"], 200)
            self.assertLess(record["bytes"], 2 * 1024 * 1024)
            for removed in (b"Set-Cookie", b"ams_cookies", b"getvinfo", b"__STARTUP_CONFIG__"):
                self.assertNotIn(removed, raw)


    async def test_current_shared_http_post_contract_replays_exactly_seven_animation_responses(self):
        recording = RecordedTencentHttp()

        def handle(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["host"], "pbaccess.video.qq.com")
            self.assertFalse(request.headers.get("cookie"))
            self.assertFalse(request.headers.get("authorization"))
            body = json.loads(request.content)
            return httpx.Response(200, json=recording.response("official-fixed-endpoint", body))

        http = CalendarHttp(self.provider.allowed_hosts, transport=httpx.MockTransport(handle),
                            resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
                            max_requests=7, min_interval=0)
        try:
            result = await self.provider.fetch(http)
        finally:
            await http.aclose()
        self.assertEqual(result.status, "ok")
        self.assertEqual((len(result.entries), sum(len(item.events) for item in result.entries)), (28, 45))
        self.assertEqual(len(recording.calls), 7)
        self.assertTrue(all(body["page_params"]["page_id"] == "100119" for _, body in recording.calls))
        self.assertNotIn("读取失败", result.message)

    async def test_each_failed_day_preserves_only_previous_successful_dates(self):
        baseline = await self.provider.fetch(RecordedTencentHttp())
        requested_days = ["2026-09-09", "2026-09-07", "2026-09-08", "2026-09-10",
                          "2026-09-11", "2026-09-12", "2026-09-13"]
        for fail_at in range(2, 8):
            with self.subTest(fail_at=fail_at):
                http = RecordedTencentHttp()
                http.fail_at = fail_at
                http.failure = TimeoutError("private token")
                result = await self.provider.fetch(http)
                verified_dates = set(requested_days[:fail_at - 1])
                self.assertEqual(set(result.covered_dates), verified_dates)
                expected = {(entry.source_id, event) for entry in baseline.entries for event in entry.events
                            if event.date in verified_dates}
                self.assertEqual({(entry.source_id, event) for entry in result.entries for event in entry.events}, expected)
                self.assertEqual((result.status, len(http.calls)), ("partial", fail_at))
                self.assertIn(f"{fail_at - 1}/7", result.message)
                self.assertIn("覆盖不全", result.message)
                self.assertNotIn("private", result.message)

    async def test_other_categories_inside_anime_response_never_produce_entries(self):
        original = self.http.response

        def response(url, body):
            payload = original(url, body)
            for module in payload["data"]["CardList"]:
                for child in module.get("children_list", {}).get("list", {}).get("cards", []):
                    if child.get("type") == "poster":
                        child["params"]["type"] = "2"
            return payload

        self.http.response = response
        result, entries = await self.fetch("partial")
        self.assertEqual(entries, {})
        self.assertEqual(len(self.http.calls), 7)
        self.assertIn("7/7", result.message)
        self.assertGreater(result.ignored, 0)

    async def test_full_week_with_one_ignored_programme_stays_partial(self):
        baseline = await self.provider.fetch(RecordedTencentHttp())
        original = self.http.response
        rejected = []

        def response(url, body):
            payload = original(url, body)
            if len(self.http.calls) == 1:
                card = next(card for card in self.poster_children(payload) if card.get("type") == "poster")
                rejected.append(card["params"]["cid"])
                card["params"]["type"] = "2"
            return payload

        self.http.response = response
        result, _ = await self.fetch("partial")
        self.assertEqual((len(self.http.calls), result.ignored), (7, 1))
        self.assertEqual(result.covered_dates, baseline.covered_dates)
        self.assertIn("7/7", result.message)
        self.assertIn("1条", result.message)
        expected = {(entry.source_id, event) for entry in baseline.entries for event in entry.events
                    if (entry.source_id, event.date) != (rejected[0], "2026-09-09")}
        self.assertEqual({(entry.source_id, event) for entry in result.entries for event in entry.events}, expected)

    async def test_missing_day_without_request_error_stays_partial(self):
        original = self.http.response

        def response(url, body):
            payload = original(url, body)
            if body["page_params"].get("week") == "20260907":
                payload["data"]["CardList"] = [module for module in payload["data"]["CardList"]
                                               if module.get("type") != "channel_play_schedule"]
            return payload

        self.http.response = response
        result, entries = await self.fetch("partial")
        self.assertEqual((len(self.http.calls), len(result.covered_dates), result.ignored), (7, 6, 0))
        self.assertNotIn("2026-09-07", result.covered_dates)
        self.assertTrue(entries)
        self.assertIn("6/7", result.message)
        self.assertNotIn("读取失败", result.message)

    async def test_old_week_is_not_reassigned_or_followed(self):
        result = await TencentCalendarProvider(clock=lambda: date(2026, 9, 16)).fetch(self.http)
        self.assertEqual((result.status, result.entries, len(self.http.calls)), ("partial", (), 1))
        self.assertIn("0/7", result.message)

    async def test_later_old_week_navigation_stops_without_reassigning_events(self):
        # 只合成负向旧周响应，不更改磁盘实采 fixture 或正向日期。
        for fail_at in range(2, 8):
            class StaleDayHttp(RecordedTencentHttp):
                def response(self, url, body):
                    payload = super().response(url, body)
                    if len(self.calls) == fail_at:
                        module = next(m for m in payload["data"]["CardList"] if m.get("type") == "channel_play_schedule")
                        for child in module["children_list"]["list"]["cards"]:
                            if child.get("type") != "navigation":
                                continue
                            params = child["params"]
                            old = params["week"]
                            previous_week = (datetime.strptime(old, "%Y%m%d") - timedelta(days=7)).strftime("%Y%m%d")
                            params["week"] = previous_week
                            params["data_key"] = params["data_key"].replace(old, previous_week)
                    return payload
            with self.subTest(fail_at=fail_at):
                http = StaleDayHttp()
                result = await self.provider.fetch(http)
                expected_dates = {"2026-09-09"} | {
                    datetime.strptime(body["page_params"]["week"], "%Y%m%d").date().isoformat()
                    for _, body in http.calls[1:-1]
                }
                self.assertEqual(len(http.calls), fail_at)
                self.assertEqual({event.date for entry in result.entries for event in entry.events}, expected_dates)
                self.assertIn(f"{fail_at - 1}/7", result.message)
                self.assertIn("停止请求", result.message)

    async def test_later_missing_or_conflicting_navigation_is_not_counted(self):
        for remove in (False, True):
            class InvalidNavigationHttp(RecordedTencentHttp):
                def response(self, url, body):
                    payload = super().response(url, body)
                    if len(self.calls) == 2:
                        module = next(m for m in payload["data"]["CardList"] if m.get("type") == "channel_play_schedule")
                        cards = module["children_list"]["list"]["cards"]
                        if remove:
                            cards[:] = [c for c in cards if c.get("type") != "navigation"]
                        else:
                            for child in cards:
                                if child.get("type") == "navigation":
                                    child["params"]["data_key"] = child["params"]["data_key"].replace("page_id=100119", "page_id=100113")
                    return payload
            with self.subTest(remove=remove):
                http = InvalidNavigationHttp()
                result = await self.provider.fetch(http)
                self.assertEqual(len(http.calls), 2)
                self.assertEqual({event.date for entry in result.entries for event in entry.events}, {"2026-09-09"})
                self.assertIn("1/7", result.message)

    async def test_navigation_never_switches_channels_or_follows_untrusted_module_ids(self):
        for invalid_module in (False, True):
            with self.subTest(invalid_module=invalid_module):
                http = RecordedTencentHttp()
                payload = fixture("calendar_20260909.json")
                module = next(m for m in payload["data"]["CardList"] if m.get("type") == "channel_play_schedule")
                if invalid_module:
                    module["id"] = "https://127.0.0.1/private"
                else:
                    for child in module["children_list"]["list"]["cards"]:
                        if child.get("type") == "navigation":
                            child["params"]["data_key"] = child["params"]["data_key"].replace("page_id=100119", "page_id=100113")
                http.initial_override = payload
                result = await self.provider.fetch(http)
                self.assertEqual((result.entries, len(http.calls)), ((), 1))
                self.assertEqual(http.calls[0][1]["page_params"]["page_id"], "100119")

    async def test_malformed_status_fails_closed_without_extra_requests(self):
        for value in (None, True, "0", -1, {}, []):
            with self.subTest(value=value):
                http = RecordedTencentHttp()
                http.initial_override = {"ret": value, "data": fixture("calendar_20260909.json")["data"]}
                result = await self.provider.fetch(http)
                self.assertEqual((result.status, result.entries, len(http.calls)), ("unavailable", (), 1))

    async def test_cancellation_propagates_without_retry(self):
        for fail_at in (1, 3):
            with self.subTest(fail_at=fail_at):
                http = RecordedTencentHttp()
                http.fail_at = fail_at
                http.failure = asyncio.CancelledError()
                with self.assertRaises(asyncio.CancelledError):
                    await self.provider.fetch(http)
                self.assertEqual(len(http.calls), fail_at)

    async def fetch_poster_snapshot(self, payload=None):
        """只回放本轮默认日；第二次切日注入不可用，不伪造七日现场。"""
        http = RecordedTencentHttp()
        http.initial_override = fixture("platform_images_20260910.json") if payload is None else payload
        http.failure, http.fail_at = SourceUnavailable("仅有默认日离线快照"), 2
        result = await TencentCalendarProvider(clock=lambda: date(2026, 9, 10)).fetch(http)
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(result.status, "partial")
        self.assertIn("1/7", result.message)
        return result

    @staticmethod
    def poster_children(payload):
        module = next(m for m in payload["data"]["CardList"] if m.get("type") == "channel_play_schedule")
        return module["children_list"]["list"]["cards"]

    @staticmethod
    def without_posters(result):
        return replace(result, entries=tuple(replace(entry, platform_poster_key="") for entry in result.entries))

    async def test_real_original_image_fields_keep_identity_dates_and_free_progress_unchanged(self):
        payload = fixture("platform_images_20260910.json")
        expected = {c["params"]["cid"]: c["params"]["image_url"][8:]
                    for c in self.poster_children(payload) if c["type"] == "poster"}
        result = await self.fetch_poster_snapshot(payload)
        for child in self.poster_children(payload):
            child["params"].pop("image_url", None)
        baseline = await self.fetch_poster_snapshot(payload)
        self.assertEqual(self.without_posters(result), baseline)
        self.assertEqual(len(result.entries), 6)
        self.assertEqual({e.source_id: e.platform_poster_key for e in result.entries}, expected)
        self.assertEqual({event.date for e in result.entries for event in e.events}, {"2026-09-10"})
        for entry in result.entries:
            self.assertEqual(entry.stable_id, f"tencent:{entry.source_id}")
            self.assertEqual((entry.free_progress, entry.free_weekdays, entry.free_schedule), ("", (), ""))
        historical, _ = await self.fetch()
        self.assertTrue(all(not e.platform_poster_key for e in historical.entries))
        self.assertEqual((len(historical.entries), sum(len(e.events) for e in historical.entries)), (28, 45))

    async def test_missing_or_unsafe_original_images_never_drop_programmes_or_dates(self):
        template = fixture("platform_images_20260910.json")
        for child in self.poster_children(template):
            child["params"].pop("image_url", None)
        baseline = await self.fetch_poster_snapshot(template)
        bad_images = (None, "", False, 7, {}, [], "https://[broken", "x" * 2049,
            "data:image/png;base64,AAAA", "javascript:alert(1)", "http://127.0.0.1/private",
            "https://vcover-hz-pic.puui.qpic.cn.evil.invalid/image.jpg",
            "https://user:password@vcover-hz-pic.puui.qpic.cn/vcover_hz_pic/0/id/750",
            "https://vcover-hz-pic.puui.qpic.cn/../private",
            "https://vcover-hz-pic.puui.qpic.cn/vcover_hz_pic/0/id/750#fragment")
        for image in bad_images:
            with self.subTest(image=image):
                payload = copy.deepcopy(template)
                for child in self.poster_children(payload):
                    if child["type"] == "poster":
                        child["params"]["image_url"] = image
                self.assertEqual(await self.fetch_poster_snapshot(payload), baseline)

    async def test_same_cid_conflicting_images_clear_only_poster_independent_of_order(self):
        template = fixture("platform_images_20260910.json")
        posters = [c for c in self.poster_children(template) if c["type"] == "poster"]
        original = posters[0]
        conflict = copy.deepcopy(original)
        conflict["params"]["image_url"] = posters[1]["params"]["image_url"]
        baseline = self.without_posters(await self.fetch_poster_snapshot(template))
        for first, last in ((original, conflict), (conflict, original)):
            with self.subTest(conflict_first=first is conflict):
                payload = copy.deepcopy(template)
                children = self.poster_children(payload)
                children[:] = [c for c in children if c["type"] == "navigation"] + [first] + posters[1:] + [last]
                result = await self.fetch_poster_snapshot(payload)
                self.assertEqual(self.without_posters(result), baseline)
                entry = next(e for e in result.entries if e.source_id == original["params"]["cid"])
                self.assertEqual(entry.platform_poster_key, "")
                self.assertEqual(sum(bool(e.platform_poster_key) for e in result.entries), 5)

    async def test_duplicate_missing_bad_or_equivalent_images_do_not_poison_verified_image(self):
        template = fixture("platform_images_20260910.json")
        original = next(c for c in self.poster_children(template) if c["type"] == "poster")
        url = original["params"]["image_url"]
        baseline = await self.fetch_poster_snapshot(template)
        for image in (None, "https://127.0.0.1/private", url, "http:" + url[6:], url[6:]):
            for first in (True, False):
                with self.subTest(image=image, duplicate_first=first):
                    payload = copy.deepcopy(template)
                    duplicate = copy.deepcopy(original)
                    duplicate["params"]["image_url"] = image
                    children = self.poster_children(payload)
                    children.insert(0, duplicate) if first else children.append(duplicate)
                    self.assertEqual(await self.fetch_poster_snapshot(payload), baseline)

    async def test_same_cid_title_conflict_cannot_lend_a_poster_to_first_wins_identity(self):
        template = fixture("platform_images_20260910.json")
        original = next(c for c in self.poster_children(template) if c["type"] == "poster")
        conflict = copy.deepcopy(original)
        conflict["params"]["title"] = "同CID但不同节目名（合成冲突）"
        for first in (True, False):
            payload = copy.deepcopy(template)
            children = self.poster_children(payload)
            children.insert(0, conflict) if first else children.append(conflict)
            result = await self.fetch_poster_snapshot(payload)
            entry = next(e for e in result.entries if e.source_id == original["params"]["cid"])
            self.assertEqual(entry.platform_poster_key, "")
            self.assertEqual(entry.title, conflict["params"]["title"] if first else original["params"]["title"])
            self.assertEqual(len(result.entries), 6)  # 不改变既有 CID 合并/丢节目规则。

    async def test_unverified_category_or_undated_row_cannot_add_or_poison_poster(self):
        template = fixture("platform_images_20260910.json")
        posters = [c for c in self.poster_children(template) if c["type"] == "poster"]
        baseline = await self.fetch_poster_snapshot(template)
        for fields in ({"type": "2"}, {"pay_time": "", "free_time": ""}, {"is_trailer": "1"}):
            payload = copy.deepcopy(template)
            duplicate = copy.deepcopy(posters[0])
            duplicate["params"].update(fields, image_url=posters[1]["params"]["image_url"])
            self.poster_children(payload).insert(0, duplicate)
            result = await self.fetch_poster_snapshot(payload)
            self.assertEqual(result.entries, baseline.entries)

    def test_new_image_fixture_provenance_is_separate_sanitized_single_request_evidence(self):
        provenance = fixture("platform_images_20260910_provenance.json")
        self.assertEqual(provenance["kind"], "sanitized_live_platform_poster_fields")
        self.assertEqual(provenance["image_field"], "image_url")
        self.assertEqual(provenance["observed_image_hosts"], {"vcover-hz-pic.puui.qpic.cn": 6})
        data = (FIXTURES / provenance["fixture"]["fixture"]).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), provenance["fixture"]["sha256"])
        self.assertEqual(len(data), provenance["fixture"]["bytes"])
        for field in (b"ams_cookies", b"Set-Cookie", b"getvinfo", b"authorization", b"back_image"):
            self.assertNotIn(field, data)
        for name in ("schedule", "image_sample"):
            evidence = provenance[name]
            assert (datetime.fromisoformat(evidence["started_at"]).tzinfo) is not None
            self.assertEqual((evidence["status"], evidence["retries"], evidence["max_redirects"]), (200, 0, 0))
            self.assertLess(evidence["bytes"], 2 * 1024 * 1024)
            self.assertEqual(len(evidence["sha256"]), 64)
            self.assertTrue(evidence["pin_resolved_address"])
            self.assertFalse(evidence["credentials"])
        self.assertNotIn("week", provenance["schedule"]["json_body"]["page_params"])
        self.assertEqual(provenance["image_sample"]["mime"], "image/jpeg")
        self.assertTrue(provenance["image_sample"]["mime_magic_match"])
        self.assertEqual(provenance["queries"], [""])


if __name__ == "__main__":
    unittest.main()
