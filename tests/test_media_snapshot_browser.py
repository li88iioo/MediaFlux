"""看板与最近入库快照的真实模板、API 输出和隔离浏览器验证。"""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 浏览器门禁环境提供依赖
    sync_playwright = None

from app.clients.base import DashboardData, MediaItem
from tests import test_media_hub_pages as page_fixture
from tests.test_agent_kernel_browser import _chromium_executable

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-10-15T02:00:00+00:00"
SCREENSHOTS = ROOT / ".superpowers/tmp/dashboard-performance"
FETCH_FIXTURE = r"""
(() => {
    window.__snapshotRequests = [];
    window.__snapshotTimeouts = [];
    const nativeTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay, ...args) => {
        const id = nativeTimeout(callback, delay, ...args);
        if (delay === 35000) window.__snapshotTimeouts.push({id, delay, callback});
        return id;
    };
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (input, options = {}) => {
        const url = new URL(String(input), location.href);
        if (!['/api/dashboard', '/media/recent'].includes(url.pathname)) return nativeFetch(input, options);
        return new Promise((resolve, reject) => {
            const request = {url: url.href, headers: options.headers, signal: options.signal, resolve, reject, aborted: false};
            window.__snapshotRequests.push(request);
            options.signal?.addEventListener('abort', () => {
                request.aborted = true;
                reject(new DOMException('Aborted', 'AbortError'));
            }, {once: true});
        });
    };
    window.__finishSnapshot = (index, result) => {
        const request = window.__snapshotRequests[index];
        if (result.reject) { request.reject(new TypeError('Fixture network failure')); return; }
        const json = result.payload !== undefined;
        request.resolve(new Response(json ? JSON.stringify(result.payload) : result.html || '', {
            status: result.status || 200,
            headers: {'Content-Type': json ? 'application/json' : 'text/html; charset=utf-8'},
        }));
    };
})();
"""


@unittest.skipIf(sync_playwright is None, "未安装 Playwright")
class MediaSnapshotBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert sync_playwright is not None
        cls.playwright = cls.enterClassContext(sync_playwright())
        executable = _chromium_executable(cls.playwright)
        if not executable:
            raise unittest.SkipTest("没有可用的本地 Chromium")
        cls.browser = cls.playwright.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox"],
        )
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.fixture = page_fixture.MediaHubPageContractTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture._login()
        self.context = self.browser.new_context(
            viewport={"width": 1440, "height": 1000}, reduced_motion="reduce", timezone_id="UTC",
        )
        self.addCleanup(self.context.close)
        self.errors = []
        self.blocked_requests = []
        self.navigations = []

    def tearDown(self):
        self.assertEqual(self.errors, [], "页面脚本异常")
        self.assertEqual(self.blocked_requests, [], "夹具不应请求外网或生产")

    def board(self, **changes):
        items = [MediaItem(
            id=f"snapshot-{index}", name=f"沙丘 {index + 1}", type="Movie", year="2024",
            runtime=155 + index, date_added=f"2026-10-15T01:0{index}:00Z",
            primary_image=f"/snapshot-poster/{index}.svg",
            web_url=f"http://mediaflux.test/watch/{index}",
            last_played="2026-10-14T08:00:00Z", progress=45.5,
        ) for index in range(5)]
        base = replace(
            self.fixture._dashboard(), updated_at=STAMP, server_product="Jellyfin",
            total_items=1135, movie_count=350, series_count=70, episode_count=715,
            recent_added=items, recent_played=items[:4],
        )
        return replace(base, **changes)

    def sources(self, board=None):
        board = board or self.board()
        return [{
            "server_type": board.server_type, "server_name": board.server_name,
            "web_url": board.web_url, "items": board.recent_added,
            "error": board.refresh_error or board.error,
            "updated_at": board.updated_at, "stale": board.stale,
        }]

    def dashboard_html(self, board, *, cached=True):
        with patch("app.routes.pages.get_cached_dashboards_or_stubs", return_value=([board], cached)):
            response = self.fixture.client.get("/")
        self.assertEqual(response.status_code, 200)
        return response.text

    def dashboard_payload(self, board):
        # 经过真实 API 序列化，保证 runtime、episode_label 等字段和生产一致。
        with patch("app.routes.api.build_dashboards", return_value=[board]):
            response = self.fixture.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def recent_html(self, sources, *, cached=True, xhr=False, path="/media/recent"):
        with patch("app.routes.pages.get_cached_recent_media_or_stubs", return_value=(sources, cached)), patch(
            "app.routes.pages.build_recent_media", return_value=sources,
        ):
            response = self.fixture.client.get(path, headers={"X-Requested-With": "MediaFlux"} if xhr else {})
        self.assertEqual(response.status_code, 200)
        return response.text

    def open_page(self, html, path="/", width=1440):
        page = self.context.new_page()
        page.set_viewport_size({"width": width, "height": 1000 if width > 400 else 844})
        page.set_default_timeout(5000)
        page.add_init_script(FETCH_FIXTURE)
        page.on("pageerror", lambda error: self.errors.append(str(error)))

        def serve(route):
            request = route.request
            url = urlsplit(request.url)
            if url.netloc != "mediaflux.test":
                self.blocked_requests.append(request.url)
                route.abort()
            elif request.is_navigation_request():
                self.navigations.append(request.url)
                route.fulfill(content_type="text/html; charset=utf-8", body=html)
            elif url.path.startswith("/snapshot-poster/"):
                index = int(url.path.rsplit("/", 1)[-1].split(".")[0])
                colors = ["#7e6259", "#657878", "#8c816c", "#5c6478", "#7a6b82"]
                poster = f'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="480"><rect width="320" height="480" fill="{colors[index]}"/><circle cx="240" cy="120" r="90" fill="#e4cfaa" opacity=".35"/><path d="M0 320 Q160 200 320 350V480H0Z" fill="#343b45" opacity=".5"/></svg>'
                route.fulfill(content_type="image/svg+xml", body=poster)
            elif url.path.startswith("/static/"):
                response = self.fixture.client.get(url.path)
                route.fulfill(status=response.status_code, content_type=response.headers.get("content-type"), body=response.content)
            else:
                route.fulfill(status=404, body="隔离测试中未提供此路径")

        page.route("**/*", serve)
        page.goto("http://mediaflux.test" + path, wait_until="networkidle")
        return page

    def finish(self, page, *, index=0, **result):
        page.evaluate("args => window.__finishSnapshot(args.index, args.result)", {"index": index, "result": result})

    def wait_dashboard(self, page):
        page.wait_for_function("() => document.querySelector('[data-dashboard-refresh]').getAttribute('aria-busy') === 'false'")

    def wait_recent(self, page):
        page.wait_for_function("() => document.querySelector('[data-media-recent-filter]').getAttribute('aria-busy') === 'false'")

    def remember(self, page, selector):
        page.evaluate("selector => { window.__originalCards = [...document.querySelectorAll(selector)]; }", selector)

    def assert_retained(self, page, selector):
        self.assertTrue(page.evaluate("selector => { const nodes = [...document.querySelectorAll(selector)]; return nodes.length === window.__originalCards.length && nodes.every((node, i) => node === window.__originalCards[i] && node.isConnected); }", selector))

    def test_dashboard_expired_snapshot_reads_once_and_keeps_unchanged_cards(self):
        board = self.board(stale=True)
        page = self.open_page(self.dashboard_html(board))
        selectors = ".dashboard-poster-card,.dashboard-playback-row,.dashboard-library-row,[data-metric]"
        self.remember(page, selectors)
        self.assertEqual(page.locator('[data-metric="total_items"]').inner_text(), "1,135")
        self.assertEqual(page.locator(".dashboard-status-value").inner_text(), "更新中")
        self.finish(page, payload=self.dashboard_payload(self.board(updated_at="2026-10-15T02:01:00+00:00")))
        self.wait_dashboard(page)
        self.assert_retained(page, selectors)
        self.assertEqual(page.locator(".dashboard-status-value").inner_text(), "正常")
        self.assertEqual(page.evaluate("window.__snapshotRequests.length"), 1)

    def test_dashboard_offline_snapshot_remains_visible_and_retryable(self):
        page = self.open_page(self.dashboard_html(self.board(stale=True)))
        selectors = ".dashboard-poster-card,.dashboard-playback-row,.dashboard-library-row"
        self.remember(page, selectors)
        old = self.board(online=False, stale=True, refresh_error="读取失败，显示上次数据")
        self.finish(page, payload=self.dashboard_payload(old))
        self.wait_dashboard(page)
        self.assert_retained(page, selectors)
        self.assertEqual(page.locator('[data-metric="total_items"]').inner_text(), "1,135")
        self.assertEqual(page.locator(".dashboard-status-value").inner_text(), "更新失败")
        self.assertEqual(page.locator("[data-dashboard-updated-at]").inner_text(), "02:00:00")
        text_content = page.locator("[data-dashboard-updated-at]").get_attribute("title")
        assert text_content is not None
        self.assertIn("2026-10-15 02:00:00+00:00", text_content)
        self.assertTrue(page.locator(".dashboard-primary-grid").is_visible())
        self.assertTrue(page.locator("[data-dashboard-refresh]").is_visible())
        page.locator("[data-dashboard-refresh]").click()
        self.assertIn("refresh=1", page.evaluate("window.__snapshotRequests[1].url"))
        self.finish(page, index=1, payload=self.dashboard_payload(self.board(total_items=1136)))
        self.wait_dashboard(page)
        self.assert_retained(page, selectors)
        self.assertEqual(page.locator('[data-metric="total_items"]').inner_text(), "1,136")

    def test_dashboard_http_failure_and_timeout_keep_cards_and_release_retry(self):
        page = self.open_page(self.dashboard_html(self.board(stale=True)))
        self.remember(page, ".dashboard-poster-card")
        self.finish(page, status=503)
        self.wait_dashboard(page)
        self.assert_retained(page, ".dashboard-poster-card")
        page.locator("[data-dashboard-refresh]").click()
        page.evaluate("window.__snapshotTimeouts.at(-1).callback()")
        self.wait_dashboard(page)
        text_content = page.locator("[data-dashboard-snapshot-message]").get_attribute("title")
        assert text_content is not None
        self.assertIn("读取超时", text_content)
        self.assertTrue(page.evaluate("window.__snapshotRequests[1].aborted"))
        self.assertFalse(page.locator("[data-dashboard-refresh]").is_disabled())
        self.assert_retained(page, ".dashboard-poster-card")

    def test_dashboard_cold_offline_uses_dashes_until_success(self):
        stub = DashboardData(server_name="家庭 Jellyfin", server_type="jellyfin", server_product="Jellyfin")
        page = self.open_page(self.dashboard_html(stub, cached=False), width=390)
        rect = page.locator(".dashboard-primary-grid").bounding_box()
        self.assertEqual(page.locator("[data-metric]").all_inner_texts(), ["—"] * 3)
        self.assertNotIn("暂无", page.locator(".dashboard-primary-grid").inner_text())
        self.finish(page, payload=self.dashboard_payload(replace(stub, error="暂时无法连接")))
        self.wait_dashboard(page)
        self.assertEqual(page.locator("[data-metric]").all_inner_texts(), ["—"] * 3)
        self.assertEqual(page.locator(".dashboard-primary-grid").bounding_box(), rect)
        self.assertTrue(page.locator("[data-dashboard-refresh]").is_visible())
        page.locator("[data-dashboard-refresh]").click()
        self.finish(page, index=1, payload=self.dashboard_payload(self.board()))
        self.wait_dashboard(page)
        self.assertEqual(page.locator('[data-metric="recent_added"]').inner_text(), "5")

    def test_dashboard_keeps_server_timezone_on_initial_render_and_refresh(self):
        # 浏览器在另一个时区，也必须显示MF容器生成的本地时间。
        for stamp, refreshed in (
            ("2026-10-15T13:29:48+08:00", "2026-10-15T13:31:02+08:00"),
            ("2026-10-15T10:59:48+05:30", "2026-10-15T11:01:02+05:30"),
            ("2026-10-15T01:29:48-04:00", "2026-10-15T01:31:02-04:00"),
        ):
            with self.subTest(stamp=stamp):
                page = self.open_page(self.dashboard_html(self.board(updated_at=stamp)))
                time = page.locator("[data-dashboard-updated-at]")
                self.assertEqual(time.inner_text(), stamp[11:19])
                self.assertEqual(time.get_attribute("datetime"), stamp)
                self.assertEqual(time.get_attribute("title"), "更新时间 " + stamp.replace("T", " "))
                page.locator("[data-dashboard-refresh]").click()
                self.assertEqual(time.inner_text(), stamp[11:19])
                self.finish(page, payload=self.dashboard_payload(self.board(updated_at=refreshed)))
                self.wait_dashboard(page)
                self.assertEqual(time.inner_text(), refreshed[11:19])
                self.assertEqual(time.get_attribute("datetime"), refreshed)
                self.assertEqual(time.get_attribute("title"), "更新时间 " + refreshed.replace("T", " "))
                page.close()

    def test_dashboard_keeps_original_playback_and_library_heights(self):
        for width in (1440, 2048, 390):
            with self.subTest(width=width):
                page = self.open_page(self.dashboard_html(self.board()), width=width)
                # 原布局由表格内容撑高，媒体库沿用工作台样式的 306px，
                # 不能为了刷新占位给两块内容统一叠加更大的最小高度。
                self.assertEqual(page.locator(".dashboard-table-scroll").evaluate(
                    "node => getComputedStyle(node).minHeight"), "0px")
                self.assertEqual(page.locator(".dashboard-library-body").evaluate(
                    "node => getComputedStyle(node).minHeight"), "306px")
                panels = page.locator(".dashboard-primary-grid > .dashboard-panel")
                before = [panel.bounding_box() for panel in panels.all()]
                page.locator("[data-dashboard-refresh]").click()
                self.assertEqual([panel.bounding_box() for panel in panels.all()], before)
                self.finish(page, payload=self.dashboard_payload(self.board()))
                self.wait_dashboard(page)
                self.assertEqual([panel.bounding_box() for panel in panels.all()], before)
                page.close()

    def test_dashboard_time_lives_in_status_card_without_separate_notice(self):
        for width in (1440, 390):
            with self.subTest(width=width):
                page = self.open_page(self.dashboard_html(self.board()), width=width)
                card = page.locator(".dashboard-metric").last
                time = card.locator("[data-dashboard-updated-at]")
                self.assertEqual(time.inner_text(), "02:00:00")
                self.assertEqual(time.get_attribute("datetime"), STAMP)
                self.assertEqual(page.locator(".dashboard-snapshot-status").count(), 0)
                self.assertNotIn("看板数据已同步", page.locator(".dashboard-server-panel").inner_text())
                self.assertEqual(card.locator("[data-dashboard-refresh]").count(), 1)
                rect = page.locator(".dashboard-primary-grid").bounding_box()
                card_rect = card.bounding_box()
                card.locator("[data-dashboard-refresh]").click()
                self.assertEqual(time.inner_text(), "02:00:00")
                self.finish(page, status=503)
                self.wait_dashboard(page)
                self.assertEqual(card.bounding_box(), card_rect)
                self.assertEqual(page.locator(".dashboard-primary-grid").bounding_box(), rect)
                self.assertEqual(card.locator("[data-dashboard-refresh]").get_attribute("aria-label"), "重试看板更新")
                card.locator("[data-dashboard-refresh]").click()
                self.finish(page, index=1, payload=self.dashboard_payload(self.board(updated_at="2026-10-15T03:01:02+00:00")))
                self.wait_dashboard(page)
                self.assertEqual(time.inner_text(), "03:01:02")
                self.assertEqual(card.bounding_box(), card_rect)
                self.assertEqual(page.locator(".dashboard-primary-grid").bounding_box(), rect)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                page.close()

    def test_dashboard_successful_empty_snapshot_is_a_real_zero(self):
        board = self.board(total_items=0, movie_count=0, series_count=0, episode_count=0, libraries=[], recent_added=[], recent_played=[])
        page = self.open_page(self.dashboard_html(board))
        self.assertEqual(page.locator("[data-metric]").all_inner_texts(), ["0"] * 3)
        self.assertEqual(page.evaluate("window.__snapshotRequests.length"), 0)
        self.assertEqual(page.locator(".dashboard-status-value").inner_text(), "正常")

    def test_recent_has_no_standalone_sync_notice_or_timestamp(self):
        for width in (1440, 390):
            with self.subTest(width=width):
                page = self.open_page(self.recent_html(self.sources()), path="/media/recent", width=width)
                self.assertEqual(page.locator(".media-recent-snapshot-status,.media-recent-snapshot-copy").count(), 0)
                self.assertEqual(page.locator("time[data-recent-updated-at]").count(), 0)
                self.assertNotIn("最近入库已同步", page.locator("body").inner_text())
                self.assertEqual(page.locator("[data-media-recent-state] > [data-media-recent-results]").count(), 1)
                self.assertEqual(page.evaluate("window.__snapshotRequests.length"), 0)
                rect = page.locator(".media-recent-card").first.bounding_box()
                toolbar = page.locator(".media-hub-topbar").bounding_box()
                page.locator("[data-recent-retry]").click()
                self.finish(page, status=503)
                self.wait_recent(page)
                self.assertIn("重试", page.locator(".media-recent-overview strong").inner_text())
                self.assertEqual(page.locator(".media-recent-card").first.bounding_box(), rect)
                self.assertEqual(page.locator(".media-hub-topbar").bounding_box(), toolbar)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                page.close()

    def test_recent_stale_failure_keeps_cards_and_does_not_loop(self):
        page = self.open_page(self.recent_html(self.sources(self.board(stale=True))), path="/media/recent")
        self.remember(page, ".media-recent-card")
        rect = page.locator(".media-recent-card").first.bounding_box()
        self.assertEqual(page.evaluate("window.__snapshotRequests[0].headers['X-Requested-With']"), "MediaFlux")
        failed = self.sources(self.board(stale=True, refresh_error="读取失败，显示上次数据"))
        self.finish(page, html=self.recent_html(failed, xhr=True))
        self.wait_recent(page)
        self.assert_retained(page, ".media-recent-card")
        self.assertEqual(page.locator(".media-recent-card").first.bounding_box(), rect)
        text_content = page.locator("[data-recent-retry]").get_attribute("title")
        assert text_content is not None
        self.assertIn("读取失败", text_content)
        self.assertEqual(page.evaluate("window.__snapshotRequests.length"), 1)
        self.assertEqual(len(self.navigations), 1)
        page.locator("[data-recent-retry]").click()
        self.finish(page, index=1, html=self.recent_html(self.sources(), xhr=True))
        self.wait_recent(page)
        self.assert_retained(page, ".media-recent-card")
        self.assertEqual(page.locator("[data-recent-retry]").get_attribute("title"), "刷新最近入库")

    def test_recent_cold_http_failure_changes_only_placeholder_copy_and_retries(self):
        sources = self.sources(self.board(updated_at="", recent_added=[]))
        page = self.open_page(self.recent_html(sources, cached=False), path="/media/recent", width=390)
        self.remember(page, ".media-recent-loading")
        rect = page.locator(".media-recent-loading").bounding_box()
        self.assertEqual(page.locator(".media-recent-overview-copy b").inner_text(), "—")
        self.assertNotIn("没有符合条件", page.locator("[data-media-recent-results]").inner_text())
        self.finish(page, status=502)
        self.wait_recent(page)
        self.assert_retained(page, ".media-recent-loading")
        self.assertEqual(page.locator(".media-recent-loading strong").inner_text(), "读取失败")
        self.assertEqual(page.locator(".media-recent-loading").bounding_box(), rect)
        self.assertFalse(page.locator(".media-hub-topbar").evaluate("node => node.classList.contains('is-submitting')"))
        self.assertEqual(len(self.navigations), 1)
        page.locator("[data-recent-retry]").click()
        self.finish(page, index=1, html=self.recent_html(self.sources(), xhr=True))
        self.wait_recent(page)
        self.assertEqual(page.locator(".media-recent-card").count(), 5)
        self.assertEqual(page.locator(".media-recent-overview-copy b").inner_text(), "5")

    def test_recent_timeout_and_server_cold_failure_remain_retryable(self):
        sources = self.sources(self.board(updated_at="", recent_added=[]))
        page = self.open_page(self.recent_html(sources, cached=False), path="/media/recent")
        page.evaluate("window.__snapshotTimeouts[0].callback()")
        self.wait_recent(page)
        self.assertEqual(page.locator(".media-recent-loading strong").inner_text(), "读取超时")
        self.assertTrue(page.evaluate("window.__snapshotRequests[0].aborted"))
        page.locator("[data-recent-retry]").click()
        failed = self.sources(self.board(updated_at="", recent_added=[], error="媒体服务器暂时不可用"))
        self.finish(page, index=1, html=self.recent_html(failed, xhr=True))
        self.wait_recent(page)
        self.assertEqual(page.locator(".media-recent-overview-copy b").inner_text(), "—")
        self.assertEqual(page.locator(".media-recent-loading strong").inner_text(), "读取失败")
        self.assertFalse(page.locator("[data-recent-retry]").is_disabled())
        self.assertEqual(page.evaluate("window.__snapshotRequests.length"), 2)

    def test_recent_filters_keep_old_content_and_ignore_older_responses(self):
        sources = self.sources()
        page = self.open_page(self.recent_html(sources), path="/media/recent")
        page.emulate_media(reduced_motion="no-preference")
        self.remember(page, ".media-recent-card")
        page.locator('[name="q"]').fill("沙丘 1")
        page.locator('[data-media-recent-filter]').evaluate("form => form.requestSubmit()")
        self.assert_retained(page, ".media-recent-card")
        page.locator('[name="q"]').fill("沙丘 2")
        page.locator('[data-media-recent-filter]').evaluate("form => form.requestSubmit()")
        self.assertTrue(page.evaluate("window.__snapshotRequests[0].aborted"))
        self.finish(page, index=1, html=self.recent_html(sources, xhr=True, path="/media/recent?q=沙丘+2"))
        self.wait_recent(page)
        self.assertEqual(page.locator(".media-recent-card").count(), 1)
        self.assertEqual(page.locator(".media-recent-copy strong").inner_text(), "沙丘 2")
        self.finish(page, index=0, html=self.recent_html(sources, xhr=True, path="/media/recent?q=沙丘+1"))
        self.assertEqual(page.locator(".media-recent-copy strong").inner_text(), "沙丘 2")
        self.assertEqual(len(self.navigations), 1)

    def test_desktop_and_mobile_snapshot_refresh_screenshots(self):
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        stale = self.board(stale=True)
        for name, path, html in [
            ("dashboard", "/", self.dashboard_html(stale)),
            ("recent", "/media/recent", self.recent_html(self.sources(stale))),
        ]:
            for width in (1440, 390):
                with self.subTest(page=name, width=width):
                    page = self.open_page(html, path=path, width=width)
                    self.assertEqual(page.evaluate("window.__snapshotRequests.length"), 1)
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    page.screenshot(path=str(SCREENSHOTS / f"{name}-{'desktop' if width > 400 else 'mobile'}.png"), full_page=True)
                    page.close()


if __name__ == "__main__":
    unittest.main()
