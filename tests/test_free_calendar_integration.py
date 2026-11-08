"""三站脱敏真实结构 → HTTP/缓存/TMDB/鉴权API 全链路离线回放。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import database, sdk
from app.discovery.cache import DiscoveryCache
from app.discovery.calendar.http import CalendarHttp
from app.discovery.calendar.metadata import CalendarMetadata
from app.discovery.calendar.providers.iqiyi import IqiyiCalendarProvider
from app.discovery.calendar.providers.tencent import TencentCalendarProvider
from app.discovery.calendar.providers.youku import YoukuCalendarProvider
from app.discovery.calendar.service import CalendarService
from app.routes.discovery_image import decode_poster_token
from tests.test_discovery_api import _BaseClientTests

FIXTURES = Path(__file__).parent / "fixtures" / "calendar"


class FreeCalendarIntegrationTests(_BaseClientTests):
    fixture_prefix = ""
    replay_now = datetime(2026, 9, 9, 12)
    expected_programmes = 115
    iqiyi_covers_sunday = False

    def setUp(self):
        super().setUp()
        database.init_db()  # _BaseClientTests 已将DB_PATH固定为本测试的临时目录。
        self.now = self.replay_now
        self.tick = 100.0
        self.calls = []
        self.fail_youku = False
        self.stale_youku = False
        self.dynamic_youku = False
        self.dynamic_calls = 0
        self.unexpected = []
        self.cache = DiscoveryCache(clock=lambda: self.now)
        self.tmdb = SimpleNamespace(api_key="fixture", get=AsyncMock(return_value={
            "results": [], "page": 1, "total_pages": 0, "total_results": 0,
        }), aclose=AsyncMock())
        self.service = CalendarService(
            providers={provider.source: provider for provider in (
                TencentCalendarProvider(clock=lambda: self.now), IqiyiCalendarProvider(clock=lambda: self.now),
                YoukuCalendarProvider(clock=lambda: self.now),
            )},
            cache=self.cache, metadata=CalendarMetadata(
                self.cache, client_factory=lambda: self.tmdb,
                douban_client_factory=lambda: SimpleNamespace(suggest=AsyncMock(return_value=[]), aclose=AsyncMock()),
                budget_seconds=20,
            ),
            http_factory=lambda hosts: CalendarHttp(
                hosts, transport=httpx.MockTransport(self.handle), min_interval=0,
                resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
            ),
            submit=lambda fn: fn(), clock=lambda: self.now, monotonic=lambda: self.tick,
        )
        self.addCleanup(self.service.shutdown)
        self.enterContext(patch("app.routes.discovery_api.get_calendar_service", return_value=self.service))

    def handle(self, request):
        self.calls.append(str(request.url))
        host, path = request.headers.get("host"), request.url.path
        if host == "pbaccess.video.qq.com" and path.endswith("PageService/getPage"):
            self.assertEqual(request.method, "POST")
            params = json.loads(request.read())["page_params"]
            self.assertEqual(params["page_id"], "100119")
            filename = self.fixture_prefix + "calendar_" + params.get("week", self.now.strftime("%Y%m%d")) + ".json"
            return httpx.Response(200, json=json.loads((FIXTURES / "tencent" / filename).read_text()))
        if host == "mesh.if.iqiyi.com" and path == "/portal/lw/v7/channel/page/tracking":
            self.assertEqual(request.url.params.get("channelId"), "4")
            filename = self.fixture_prefix + "tracking.json" if self.fixture_prefix else "weekly_tracking.json"
            return httpx.Response(200, json=json.loads((FIXTURES / "iqiyi" / filename).read_text()))
        if host == "www.youku.com" and path == "/ku/webcomic":
            if self.fail_youku:
                return httpx.Response(503)
            self.assertEqual(request.headers["cache-control"], "no-cache")
            self.assertEqual(request.headers["accept-encoding"], "identity")
            filename = self.fixture_prefix + ("stale_webcomic.html" if self.stale_youku else "webcomic.html")
            return httpx.Response(200, text=(FIXTURES / "youku" / filename).read_text())
        if host == "acs.youku.com" and path == "/h5/mtop.youku.columbus.home.query/1.0/":
            self.dynamic_calls += 1
            if not self.dynamic_youku:
                return httpx.Response(503)  # 离线负向边界：动态源同样失败，不能算未知外网。
            self.assertEqual(request.method, "GET")
            if self.dynamic_calls % 2:
                self.assertNotIn("cookie", request.headers)
                return httpx.Response(200, json={"data": {}, "ret": ["FAIL_SYS_TOKEN_EMPTY::missing"]},
                    headers={"Set-Cookie": "_m_h5_tk=fixturetoken_4102444800000; Domain=.youku.com; Path=/; Secure"})
            self.assertEqual(request.headers["cookie"], "_m_h5_tk=fixturetoken_4102444800000")
            return httpx.Response(200, json=json.loads((FIXTURES / "youku/dynamic_week_20260910.json").read_text()))
        self.unexpected.append(str(request.url))
        return httpx.Response(599)

    def test_real_structure_replay_populates_weekday_programmes_and_actual_times(self):
        self.authenticate()
        response = self.client.get("/api/discovery/calendar")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(self.unexpected, [])
        self.assertEqual(len(self.calls), 9)  # 腾讯动漫7 POST、爱奇艺/优酷动漫各1 GET；禁止TV请求。
        self.assertEqual([source["status"] for source in data["sources"]], ["ok", "partial", "partial"])
        tencent = data["sources"][0]
        self.assertEqual((tencent["ignored"], len(tencent["covered_dates"])), (0, 7))
        self.assertIn("官方追漫周历", tencent["message"])
        self.assertIn("不代表全站片库", tencent["message"])
        with patch("app.discovery.calendar.service.get_calendar_service", return_value=self.service):
            self.assertEqual(sdk.media.calendar()["sources"], data["sources"])
        self.assertEqual(len(data["days"]), 7)
        self.assertTrue(all(day["items"] for day in data["days"]))
        self.assertEqual(data["items_count"], self.expected_programmes)
        wednesday = data["days"][2]["items"]
        self.assertEqual({card["source"] for card in wednesday}, {"tencent", "iqiyi", "youku"})
        self.assertTrue(all(card["category"] == "animation" for day in data["days"] for card in day["items"]))
        self.assertTrue(any(card["update_time"] == "10:00" for card in wednesday))
        for day in data["days"]:
            for card in day["items"]:
                self.assertTrue(card["events"])
                self.assertTrue(all(event["date"] == day["date"] for event in card["events"]))
                self.assertEqual(card["free_progress"], "")  # 排期集数不是已可免费观看的进度。
                self.assertEqual(card["tmdb_id"], "")
                self.assertEqual(card["poster_url"], "")
        thursday = data["days"][3]["items"]
        panlong = next(card for card in thursday if card["source"] == "tencent" and card["title"] == "盘龙")
        self.assertEqual(panlong["update_time"], "10:00")
        self.assertEqual(panlong["schedule_audience"], "free")
        # 9/9样本窗口9/6–12；9/10重新取得窗口9/7–13。按实采日期变化，不补造周日。
        self.assertEqual(any(card["source"] == "iqiyi" for card in data["days"][6]["items"]), self.iqiyi_covers_sunday)
        self.client.get("/api/discovery/calendar")
        self.assertEqual(len(self.calls), 9)

    def test_real_tmdb_replay_and_controlled_secondary_cover_share_existing_watchlist(self):
        headers = self.authenticate()
        real_profiles = {title: json.loads((FIXTURES / "metadata" / filename).read_text())
                         for title, filename in (("师兄啊师兄", "tmdb-shixiong.json"), ("盘龙", "tmdb-panlong.json"))}
        self.tmdb.get.side_effect = lambda path, params, **kw: real_profiles.get(params["query"], {
            "results": [], "page": 1, "total_pages": 0, "total_results": 0,
        })
        # 合成豆瓣候选仅用于“真实TMDB身份 + 备用图”的接口闭环，不宣称现场匹配。
        secondary = {"id": "12345", "title": "师兄啊师兄", "sub_title": "师兄啊师兄", "year": "2023",
                     "episode": "13", "type": "movie", "url": "https://movie.douban.com/subject/12345/",
                     "img": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123456.jpg"}
        self.service.metadata.douban_client_factory = lambda: SimpleNamespace(
            suggest=AsyncMock(side_effect=lambda title, **kw: [secondary] if title == secondary["title"] else []),
            aclose=AsyncMock(),
        )
        data = self.client.get("/api/discovery/calendar").json()
        cards = [card for day in data["days"] for card in day["items"]]
        matched = [card for card in cards if card["title"] == "师兄啊师兄"]
        self.assertTrue(matched)
        card = matched[0]
        self.assertEqual((card["tmdb_id"], card["douban_id"]), ("218642", "12345"))
        self.assertEqual(len(card["poster_urls"]), 2)
        self.assertEqual([url.split("/")[2] for url in card["poster_urls"]], ["tmdb", "douban"])
        identity = card["watchlist"]
        self.assertEqual(identity["provider"], "tmdb")
        self.assertEqual(decode_poster_token("tmdb", identity["poster_token"]), "hLUK05JYFVDVGYJLDlI7FUxV6jh.jpg")
        self.assertEqual(card["detail_url"], "/discovery?detail_provider=tmdb&detail_type=tv&detail_id=218642")
        self.assertEqual({item["tmdb_id"] for item in cards if item["title"] == "盘龙"}, {"283805"})
        response = self.client.post("/api/discovery/watchlist", headers=headers, json={
            **{key: identity[key] for key in ("provider", "external_id", "media_type", "poster_token")},
            "title": card["title"], "year": "2023",
        })
        self.assertEqual(response.status_code, 200)
        updated = self.client.get("/api/discovery/calendar").json()
        self.assertTrue(all(item["watchlist"]["in_watchlist"] for day in updated["days"] for item in day["items"]
                            if item["tmdb_id"] == "218642"))
        self.assertEqual(self.client.delete("/api/discovery/watchlist/tmdb/tv/218642", headers=headers).status_code, 200)
        self.assertEqual(self.unexpected, [])

    def test_one_source_outage_retains_its_last_free_facts_and_other_source_states(self):
        headers = self.authenticate()
        original = self.client.get("/api/discovery/calendar").json()
        self.fail_youku = True
        self.now += timedelta(hours=2)
        self.tick += 7200
        data = self.client.post("/api/discovery/calendar/refresh", headers=headers).json()
        self.assertEqual(data["items_count"], original["items_count"])
        self.assertEqual([source["status"] for source in data["sources"]], ["ok", "partial", "stale"])
        self.assertTrue(all(card["stale"] for day in data["days"] for card in day["items"] if card["source"] == "youku"))
        calls = len(self.calls)
        self.client.post("/api/discovery/calendar/refresh", headers=headers)
        self.assertEqual(len(self.calls), calls)

    def test_authenticated_calendar_renders_real_routes_without_triggering_sources(self):
        self.authenticate()
        response = self.client.get("/discovery/calendar")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-weekly-calendar', response.text)
        self.assertIn('追漫日历', response.text)
        self.assertNotIn('追剧日历', response.text)
        self.assertIn('/static/js/free-calendar.js', response.text)
        self.assertIn('/discovery/calendar', self.client.get('/discovery').text)
        self.assertEqual(self.calls, [])


class CurrentAnimeCalendarIntegrationTests(FreeCalendarIntegrationTests):
    """2026-09-10（上海时间）重新取得的三源匿名响应，只回放，不重新出网。"""
    fixture_prefix = "anime_20260910_"
    replay_now = datetime(2026, 9, 10, 0, 11, 43)
    expected_programmes = 119
    iqiyi_covers_sunday = True

    def test_stale_upstream_week_does_not_replace_verified_current_week_anime(self):
        headers = self.authenticate()
        original = self.client.get("/api/discovery/calendar").json()
        self.stale_youku = True
        self.now += timedelta(minutes=6)
        self.tick += 360
        data = self.client.post("/api/discovery/calendar/refresh", headers=headers).json()
        self.assertEqual(data["items_count"], self.expected_programmes)
        self.assertEqual(data["sources"][2]["status"], "stale")
        self.assertEqual(data["sources"][2]["fetched_at"], original["sources"][2]["fetched_at"])
        youku = [card for day in data["days"] for card in day["items"] if card["source"] == "youku"]
        self.assertEqual(len({card["stable_id"] for card in youku}), 47)
        self.assertTrue(all(card["stale"] and card["category"] == "animation" for card in youku))
        self.assertTrue(all("2026-09-07" <= event["date"] <= "2026-09-13" for card in youku for event in card["events"]))
        self.assertEqual(self.unexpected, [])

    def test_cold_start_with_real_old_week_never_rebases_it_to_current_week(self):
        self.authenticate()
        self.stale_youku = True
        data = self.client.get("/api/discovery/calendar").json()
        self.assertEqual(data["items_count"], 72)
        self.assertFalse(any(card["source"] == "youku" for day in data["days"] for card in day["items"]))
        self.assertIn("当前上海周", data["sources"][2]["message"])
        self.assertEqual(data["sources"][2]["status"], "unavailable")
        self.assertEqual(self.cache.get(self.service.key("youku")).status, "error")
        self.assertEqual(data["today"], "2026-09-10")
        self.assertEqual(self.unexpected, [])

    def test_real_dynamic_current_week_recovers_cold_source_through_http_cache_api(self):
        headers = self.authenticate()
        self.stale_youku = True
        old = self.client.get("/api/discovery/calendar").json()
        self.assertEqual((old["items_count"], old["sources"][2]["status"]), (72, "unavailable"))
        self.assertEqual(self.dynamic_calls, 1)
        self.dynamic_calls = 0
        self.dynamic_youku = True
        self.now += timedelta(minutes=6)
        self.tick += 360
        data = self.client.post("/api/discovery/calendar/refresh", headers=headers).json()
        self.assertEqual(self.dynamic_calls, 2)
        self.assertEqual((data["items_count"], data["sources"][2]["status"]), (119, "partial"))
        self.assertIn("动态", data["sources"][2]["message"])
        cards = [card for day in data["days"] for card in day["items"] if card["source"] == "youku"]
        unique = {card["stable_id"]: card for card in cards}
        self.assertEqual(len(unique), 47)
        self.assertEqual(sum(len(card["events"]) for card in cards), 92)
        payload = self.cache.get(self.service.key("youku")).payload
        assert payload is not None
        cached = {entry["stable_id"]: entry for entry in payload["entries"]}
        self.assertEqual(set(cached), set(unique))
        for stable_id, card in unique.items():
            with self.subTest(stable_id=stable_id):
                self.assertFalse(card["stale"])
                self.assertEqual((card["tmdb_id"], card["douban_id"], card["detail_url"], card["free_progress"]), ("", "", "", ""))
                self.assertIsNone(card["watchlist"])
                self.assertEqual(len(card["poster_urls"]), 1)
                self.assertTrue(card["poster_url"].startswith("/discovery-calendar-poster/youku/"))
                self.assertEqual(decode_poster_token("calendar-youku", card["poster_url"].rsplit("/", 1)[1]),
                                 cached[stable_id]["platform_poster_key"])
        serialized = json.dumps(payload)
        self.assertNotIn("_m_h5_tk", serialized)
        self.assertNotIn("fixturetoken", serialized)
        self.assertNotIn("trackInfo", serialized)
        count = len(self.calls)
        again = self.client.get("/api/discovery/calendar").json()
        self.assertEqual((again["days"], len(self.calls), self.unexpected), (data["days"], count, []))


class PlatformPosterCaptureIntegrationTests(_BaseClientTests):
    """本轮单次排期+原图字段回放；腾讯不是七日采样，优酷旧周仍不可用。"""

    def setUp(self):
        super().setUp()
        database.init_db()
        self.now = datetime(2026, 9, 10, 1, 38)
        self.calls = []
        self.cache = DiscoveryCache(clock=lambda: self.now)
        providers = (TencentCalendarProvider(clock=lambda: self.now),
                     IqiyiCalendarProvider(clock=lambda: self.now), YoukuCalendarProvider(clock=lambda: self.now))
        self.service = CalendarService(
            providers={p.source: p for p in providers}, cache=self.cache,
            metadata=CalendarMetadata(self.cache, budget_seconds=20,
                client_factory=lambda: SimpleNamespace(api_key="offline", get=AsyncMock(return_value={
                    "results": [], "page": 1, "total_pages": 0, "total_results": 0,
                }), aclose=AsyncMock()),
                douban_client_factory=lambda: SimpleNamespace(suggest=AsyncMock(return_value=[]), aclose=AsyncMock())),
            http_factory=lambda hosts: CalendarHttp(hosts, max_requests=1, min_interval=0,
                transport=httpx.MockTransport(self.handle),
                resolver=lambda h, p: [(2, 1, 6, "", ("93.184.216.34", p))]),
            submit=lambda fn: fn(), clock=lambda: self.now,
        )
        self.addCleanup(self.service.shutdown)
        self.enterContext(patch("app.routes.discovery_api.get_calendar_service", return_value=self.service))
        self.attempts = []

        def forbidden(*args, **kwargs):
            self.attempts.append(True)
            raise AssertionError("本轮实采结构回放不得发出真实网络请求")

        for name in ("getaddrinfo", "create_connection", "socket.connect", "socket.connect_ex"):
            self.enterContext(patch("socket." + name, side_effect=forbidden))

    def tearDown(self):
        self.assertEqual(self.attempts, [])
        super().tearDown()

    def handle(self, request):
        host, path = request.headers.get("host"), request.url.path
        self.calls.append((host, path))
        files = {
            ("pbaccess.video.qq.com", "/trpc.vector_layout.page_view.PageService/getPage"):
                "tencent/platform_images_20260910.json",
            ("mesh.if.iqiyi.com", "/portal/lw/v7/channel/page/tracking"):
                "iqiyi/platform_images_20260910.json",
            ("www.youku.com", "/ku/webcomic"):
                "youku/anime_20260910_completion_stale_webcomic.html",
        }
        self.assertIn((host, path), files)
        file = FIXTURES / files[(host, path)]
        if file.suffix == ".json":
            return httpx.Response(200, json=json.loads(file.read_text()))
        return httpx.Response(200, text=file.read_text())

    def test_captured_original_posters_flow_through_cache_and_api_without_fake_metadata(self):
        self.authenticate()
        response = self.client.get("/api/discovery/calendar")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        cards = [card for day in data["days"] for card in day["items"]]
        unique = {card["stable_id"]: card for card in cards}
        self.assertEqual(len(self.calls), 3)  # 每来源只有一个本轮真实响应，额外排期配额为零。
        self.assertEqual(len(unique), 50)
        self.assertEqual(sum(card["source"] == "iqiyi" for card in unique.values()), 44)
        self.assertEqual(sum(card["source"] == "tencent" for card in unique.values()), 6)
        for card in unique.values():
            with self.subTest(card=card["stable_id"]):
                self.assertEqual(card["mapping_status"], "unmatched")
                self.assertEqual((card["tmdb_id"], card["douban_id"], card["detail_url"]), ("", "", ""))
                self.assertIsNone(card["watchlist"])
                self.assertNotIn("platform_poster_key", card)
                self.assertEqual(len(card["poster_urls"]), 1)
                self.assertTrue(card["poster_url"].startswith("/discovery-calendar-poster/" + card["source"] + "/"))
                token = card["poster_url"].rsplit("/", 1)[1]
                key = decode_poster_token("calendar-" + card["source"], token)
                payload = self.cache.get(self.service.key(card["source"])).payload
                assert payload is not None
                cached = next(row for row in payload["entries"] if row["stable_id"] == card["stable_id"])
                self.assertEqual(cached["platform_poster_key"], key)
                self.assertEqual(card["free_progress"], "")
        # 截图四项空图现在有平台封面地址；此处只证明真实字段→签名地址，不冒充4次图片下载。
        screenshot_ids = {
            "1819813496352301": "全民御兽：开局山海经，我横扫全球 动态漫画",
            "2302717336669401": "苍绝剑尊",
            "2285069688018001": "全民诡异：开局掌握零元购 动态漫画",
            "4499849593516501": "斗罗大陆4终极斗罗 动态漫画 合集",
        }
        for source_id, title in screenshot_ids.items():
            card = unique["iqiyi:" + source_id]
            self.assertEqual(card["title"], title)
            self.assertTrue(card["poster_url"])
            self.assertIsNone(card["watchlist"])
        self.assertEqual(data["sources"][2]["status"], "unavailable")
        self.assertIn("08.31", data["sources"][2]["message"])
        self.assertFalse(any(card["source"] == "youku" for card in cards))
        self.assertEqual(self.cache.get(self.service.key("youku")).status, "error")
        # 同数据刷新读取使用缓存，平台封面不会触发反复源查询或改写身份。
        again = self.client.get("/api/discovery/calendar").json()
        self.assertEqual(again["days"], data["days"])
        self.assertEqual(len(self.calls), 3)
