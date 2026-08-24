"""媒体中心最近入库页与全局搜索页面契约。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.clients.base import DashboardData, Library, MediaItem
from app.config import web_credentials
from app.main import create_app
from tests.support import InitializedWebTestCase


class MediaHubPageContractTests(InitializedWebTestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app(), raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.client.close()

    @staticmethod
    def _csrf_token(response) -> str:
        match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', response.text)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def _login(self) -> None:
        login_page = self.client.get("/login")
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self._csrf_token(login_page),
                "username": username,
                "password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("location"), "/")

    @staticmethod
    def _dashboard() -> DashboardData:
        return DashboardData(
            server_name="家庭 Jellyfin",
            server_type="jellyfin",
            web_url="http://media.example:8096",
            online=True,
            total_items=16,
            movie_count=3,
            series_count=8,
            episode_count=13,
            libraries=[
                Library(
                    id="lib-1",
                    name="电影",
                    item_type="movies",
                    count=20,
                    web_url="http://media.example:8096/web/index.html#/movies?topParentId=lib-1",
                )
            ],
            recent_added=[
                MediaItem(
                    id="a" * 32,
                    name="沙丘 2",
                    type="Movie",
                    year="2024",
                    date_added="2026-07-29T10:00:00Z",
                    web_url="http://media.example:8096/web/index.html#!/details?id=item-1",
                )
            ],
        )

    def test_ctrl_k_opens_global_search_from_other_pages(self):
        root = Path(__file__).resolve().parents[1]
        base = (root / "app/templates/base.html").read_text(encoding="utf-8")
        script = (root / "app/static/js/app.js").read_text(encoding="utf-8")

        self.assertIn('name="global-search-url"', base)
        self.assertIn("meta[name=\"global-search-url\"]", script)
        self.assertIn("event.key.toLowerCase() !== 'k'", script)
        self.assertIn("#dashboardSearchInput, #globalSearchInput", script)
        self.assertIn("window.location.assign(globalSearchURL)", script)

    def test_workbench_topbars_use_compact_main_control_geometry(self):
        root = Path(__file__).resolve().parents[1]
        dashboard_styles = (root / "app/static/css/dashboard-workbench.css").read_text(encoding="utf-8")
        media_styles = (root / "app/static/css/media-hub.css").read_text(encoding="utf-8")
        dashboard_page = re.search(r"\.dashboard-page\s*\{(?P<body>[^}]*)\}", dashboard_styles, re.S)
        dashboard_topbars = re.findall(r"\.dashboard-page \.dashboard-topbar\s*\{(?P<body>[^}]*)\}", dashboard_styles, re.S)

        self.assertIsNotNone(dashboard_page)
        self.assertTrue(dashboard_topbars)
        self.assertNotIn("font-family: inherit", dashboard_page.group("body"))
        self.assertTrue(all("background:" not in rule for rule in dashboard_topbars))
        self.assertIn("Topbar controls inherit the compact", dashboard_styles)
        self.assertIn("border-radius: var(--dashboard-control-radius)", dashboard_styles)
        self.assertIn("Media-hub command bars use the same compact, quiet control language", media_styles)
        self.assertIn('"menu overview actions"', media_styles)
        self.assertIn("grid-template-rows: 38px 38px", media_styles)
        self.assertRegex(
            media_styles,
            r"\.media-hub-page\.media-recent-page \.media-hub-topbar\s*\{[^}]*background:\s*transparent;",
        )

    def test_media_hub_styles_are_scoped_and_responsive(self):
        root = Path(__file__).resolve().parents[1]
        styles = (root / "app/static/css/media-hub.css").read_text(encoding="utf-8")

        for contract in (
            ".media-hub-page .media-hub-shell",
            ".media-hub-page .global-top-hit",
            ".media-hub-page .global-top-context",
            ".media-hub-page .global-top-actions",
            "-webkit-line-clamp: 2",
            ".media-hub-page .global-media-grid",
            ".media-hub-page .global-automation-body",
            "align-items: start",
            ".media-hub-page .global-subscription-card",
            ".media-hub-page .global-task-progress",
            ".media-hub-page .global-timeline",
            ".media-hub-page .media-recent-shell",
            ".media-hub-page .media-recent-grid",
            "grid-template-columns: repeat(7, minmax(0, 1fr))",
            "aspect-ratio: 2 / 3",
            "@media (max-width: 1100px)",
            "@media (max-width: 700px)",
            "@media (prefers-reduced-motion: reduce)",
        ):
            self.assertIn(contract, styles)
        top_hit = re.search(
            r"\.media-hub-page \.global-top-hit\s*\{([^}]*)\}",
            styles,
        )
        self.assertIsNotNone(top_hit)
        self.assertIn("display: flex", top_hit.group(1))
        self.assertIn("justify-content: space-between", top_hit.group(1))
        mobile_search = re.search(
            r"\.media-hub-page\.global-search-page \.media-hub-search\s*\{([^}]*)\}",
            styles,
        )
        self.assertIsNotNone(mobile_search)
        self.assertTrue(
            "display: block" in mobile_search.group(1)
            or "display: flex" in mobile_search.group(1)
        )
        selector_blocks = re.findall(r"([^{}]+)\{", styles)
        for block in selector_blocks:
            block = block.strip()
            if not block or block.startswith("@") or block.startswith("/*") or block in {"from", "to"}:
                continue
            for selector in block.split(","):
                selector = selector.strip()
                self.assertTrue(
                    selector.startswith(".media-hub-page"),
                    f"媒体中心样式未限定页面作用域: {selector}",
                )

    def test_anonymous_media_hub_pages_redirect_to_login(self):
        for path in ("/media/recent", "/search?q=沙丘"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 302, path)
            self.assertEqual(response.headers.get("location"), "http://testserver/login")

    def test_dashboard_uses_real_media_and_search_destinations(self):
        self._login()
        with patch("app.routes.pages.get_cached_dashboards_or_stubs", return_value=([self._dashboard()], True)):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/search"', response.text)
        self.assertIn('href="/media/recent?server=jellyfin"', response.text)
        self.assertIn('href="http://media.example:8096"', response.text)
        self.assertIn("打开媒体库", response.text)
        self.assertIn("data-media-composition", response.text)
        self.assertIn("3 部电影 · 8 部剧集 · 13 集", response.text)
        self.assertNotIn("打开媒体服务", response.text)
        self.assertNotIn(">配置<", response.text)
        self.assertNotIn(">管理<", response.text)

    def test_dashboard_pending_indicator_routes_to_its_failure_source(self):
        self._login()
        base_summary = {
            "downloads_active": 0,
            "downloads_review": 0,
            "rss_subscriptions": 0,
            "media_subscriptions": 0,
            "subscriptions_total": 0,
            "rss_pending": 0,
            "rss_failed": 0,
            "organize_issues": 0,
            "strm_failures": 0,
            "strm_last_status": "",
            "strm_last_at": "",
            "issues": 0,
            "issue_source": "none",
            "healthy": True,
            "error": "",
        }
        cases = (
            ("downloads", 2, "/downloads?view=issues", "查看 2 项下载及后处理异常"),
            ("rss", 3, "/rss#rss", "查看 3 项 RSS 处理失败"),
            ("organize", 4, "/organize", "查看 4 项整理异常"),
            ("strm", 5, "/guangya/strm#diagnostics", "查看 5 项 STRM 失败任务"),
            ("mixed", 7, "#dashboardAutomationStatus", "查看 7 项跨模块待处理问题"),
        )

        for source, issue_count, href, title in cases:
            summary = {
                **base_summary,
                "issues": issue_count,
                "issue_source": source,
                "healthy": False,
            }
            with self.subTest(source=source), patch(
                "app.routes.pages.get_cached_dashboards_or_stubs",
                return_value=([self._dashboard()], True),
            ), patch("app.routes.pages.build_automation_summary", return_value=summary):
                response = self.client.get("/")

            self.assertEqual(response.status_code, 200)
            self.assertIn(f'href="{href}" title="{title}" aria-label="{title}"', response.text)

        self.assertIn('id="dashboardAutomationStatus"', response.text)

    def test_recent_media_page_renders_internal_results_and_filters(self):
        self._login()
        sources = [{
            "server_type": "jellyfin",
            "server_name": "家庭 Jellyfin",
            "web_url": "http://media.example:8096",
            "error": "",
            "items": [self._dashboard().recent_added[0]],
        }]
        with patch("app.routes.pages.build_recent_media", return_value=sources, create=True) as build:
            response = self.client.get("/media/recent?server=jellyfin&type=movie&q=沙丘")

        self.assertEqual(response.status_code, 200)
        build.assert_called_once()
        self.assertIn("最近入库", response.text)
        self.assertIn("沙丘 2", response.text)
        self.assertIn('name="server"', response.text)
        self.assertIn('name="type"', response.text)
        self.assertIn('name="q"', response.text)
        self.assertIn("/static/css/media-hub.css?v=20260818c", response.text)
        self.assertIn("dashboard-page media-hub-page", response.text)
        self.assertIn('class="media-recent-shell"', response.text)
        self.assertIn('class="media-recent-toolbar"', response.text)
        self.assertIn('class="media-recent-grid"', response.text)
        self.assertIn('data-media-recent-filter', response.text)
        self.assertIn('data-auto-submit', response.text)
        self.assertIn('data-lucide="filter-x"', response.text)
        self.assertIn('form.requestSubmit()', response.text)
        self.assertNotIn('media-hub-filter-submit', response.text)

    def test_global_search_page_renders_grouped_real_sources(self):
        self._login()
        result = {
            "query": "沙丘",
            "sections": [
                {
                    "key": "local",
                    "title": "本地媒体",
                    "target_url": "/media/recent?q=沙丘",
                    "error": "",
                    "items": [{
                        "title": "沙丘 2",
                        "subtitle": "家庭 Jellyfin · 2024",
                        "meta": "电影",
                        "url": "http://media.example:8096/web/index.html#!/details?id=item-1",
                        "image_url": "/media-image/jellyfin/" + "a" * 32,
                        "external": True,
                        "provider": "jellyfin", "source_label": "家庭 Jellyfin",
                        "type_label": "电影", "year": "2024", "is_local": True,
                        "overview": "保罗踏上复仇之路", "original_title": "Dune: Part Two",
                        "rating": None,
                    }, {
                        "title": "沙漠之梦",
                        "subtitle": "沙丘：预言 · 第 1 季 · 第 3 集",
                        "episode_context": "沙丘：预言 · 第 1 季 · 第 3 集",
                        "meta": "单集",
                        "url": "http://media.example:8096/web/index.html#!/details?id=episode-3",
                        "image_url": "",
                        "external": True,
                        "provider": "jellyfin", "source_label": "家庭 Jellyfin",
                        "type_label": "单集", "year": "2026", "is_local": True,
                        "overview": "第三集", "original_title": "沙丘：预言",
                        "rating": None,
                    }],
                },
                {
                    "key": "rss",
                    "title": "RSS 订阅",
                    "target_url": "/rss",
                    "error": "",
                    "items": [{
                        "title": "沙丘订阅",
                        "subtitle": "已启用",
                        "meta": "Mikan",
                        "url": "/rss",
                        "image_url": "",
                        "external": False,
                        "provider": "mikan", "source_label": "Mikan",
                        "enabled": True, "parser": "MIKAN", "rules": ["自动下载"],
                        "last_refreshed_at": "2026-07-29 14:20:00",
                    }],
                },
            ],
        }
        result["section_map"] = {section["key"]: section for section in result["sections"]}
        result["media_items"] = result["section_map"]["local"]["items"]
        result["top_match"] = result["media_items"][0]
        result["counts"] = {"media": 2, "rss": 1, "downloads": 0, "logs": 0}
        result["total_count"] = 2
        with (
            patch("app.routes.pages.build_global_search", return_value=result, create=True) as search,
            patch("app.routes.pages.config.get_bool", return_value=False),
        ):
            response = self.client.get("/search?q=沙丘")

        self.assertEqual(response.status_code, 200)
        search.assert_called_once_with("沙丘")
        self.assertIn("沙丘 2", response.text)
        self.assertIn("沙丘订阅", response.text)
        self.assertIn("沙漠之梦", response.text)
        self.assertIn("沙丘：预言 · 第 1 季 · 第 3 集", response.text)
        self.assertIn("单集", response.text)
        self.assertIn('value="沙丘"', response.text)
        for contract in (
            'class="global-top-hit"',
            'class="global-media-grid"',
            'class="global-subscription-card"',
            'class="global-automation-panel is-subscriptions"',
            'class="global-automation-panel is-downloads"',
            'class="global-automation-panel is-logs"',
            'class="global-automation-body"',
            'data-global-search-section="rss"',
        ):
            self.assertIn(contract, response.text)
        self.assertNotIn("unsafe-inline-result", response.text)
        self.assertNotIn("global-search-filterbar", response.text)
        self.assertNotIn("data-global-search-filter", response.text)
        self.assertNotIn(">详情<", response.text)
        self.assertNotIn("/discovery?q=", response.text)
        self.assertIn("打开媒体库", response.text)
        self.assertNotIn("媒体档案", response.text)
        self.assertIn("/static/css/media-hub.css?v=20260818c", response.text)
        self.assertRegex(
            response.text,
            re.compile(r'<div class="global-top-primary">.*<div class="global-top-context">', re.S),
        )

    def test_global_search_nonlocal_media_opens_discovery_archive_directly(self):
        self._login()
        detail_url = (
            "/discovery?detail_provider=tmdb&detail_type=movie&detail_id=693134"
            "&return_query=%E6%B2%99%E4%B8%98"
        )
        item = {
            "title": "沙丘2", "subtitle": "TMDB · 2024", "meta": "电影",
            "url": detail_url, "detail_url": detail_url,
            "resource_url": "/discovery?q=沙丘2", "image_url": "",
            "external": False, "provider": "tmdb", "source_label": "TMDB",
            "external_id": "693134", "media_type": "movie",
            "type_label": "电影", "year": "2024", "is_local": False,
            "overview": "保罗继续他的旅程。", "original_title": "Dune: Part Two",
            "rating": 8.4, "rating_source": "tmdb",
        }
        result = {
            "query": "沙丘",
            "sections": [{
                "key": "discovery", "title": "影视探索",
                "target_url": "/discovery?q=沙丘", "error": "", "items": [item],
            }],
            "section_map": {
                "discovery": {
                    "key": "discovery", "title": "影视探索",
                    "target_url": "/discovery?q=沙丘", "error": "", "items": [item],
                },
            },
            "media_items": [item], "top_match": item,
            "counts": {"media": 1, "rss": 0, "downloads": 0, "logs": 0},
            "total_count": 1,
        }
        with (
            patch("app.routes.pages.build_global_search", return_value=result, create=True),
            patch("app.routes.pages.config.get_bool", return_value=True),
        ):
            response = self.client.get("/search?q=沙丘")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'href="/discovery?detail_provider=tmdb&amp;detail_type=movie&amp;detail_id=693134'
            '&amp;return_query=%E6%B2%99%E4%B8%98"',
            response.text,
        )
        self.assertIn("查看媒体档案", response.text)
        self.assertIn('data-lucide="book-open"', response.text)
        self.assertNotIn("搜索资源", response.text)

    def test_global_search_rejects_invalid_query_without_calling_sources(self):
        self._login()
        with patch("app.routes.pages.build_global_search", create=True) as search:
            response = self.client.get("/search?q=" + "x" * 121)

        self.assertEqual(response.status_code, 400)
        search.assert_not_called()
        self.assertIn("搜索关键词必须为 1 到 120 个可见字符", response.text)


if __name__ == "__main__":
    unittest.main()
