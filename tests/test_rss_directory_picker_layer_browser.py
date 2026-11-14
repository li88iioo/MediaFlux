"""RSS 双弹窗真实浏览器回归：纯 Jinja/静态路由，不导入应用或连接后端。

直接运行：.venv/bin/python tests/test_rss_directory_picker_layer_browser.py -v
复用既有浏览器定位辅助；所有 HTTP/WebSocket 都在浏览器上下文中拦截。
"""
from __future__ import annotations

import mimetypes
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from jinja2 import Environment, FileSystemLoader

try:
    from .test_agent_kernel_browser import _chromium_executable, sync_playwright
except ImportError:
    from test_agent_kernel_browser import _chromium_executable, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
VIEWPORTS = ((1280, 900), (390, 844), (320, 640))
LONG_NAME = "动画归档_" + "Season_超长目录名_" * 12
DIRECTORIES = {
    "0": [{"is_dir": True, "file_id": "drive-a", "name": "离线测试驱动器"}],
    "drive-a": [{"is_dir": True, "file_id": "series", "name": LONG_NAME}],
    "series": [{"is_dir": True, "file_id": f"child-{i}", "name": f"第 {i} 季"} for i in range(20)],
}


@unittest.skipIf(sync_playwright is None, "未安装 Playwright")
class RssDirectoryPickerLayerBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert sync_playwright is not None
        cls.playwright = cls.enterClassContext(sync_playwright())
        executable = _chromium_executable(cls.playwright)
        if executable is None:
            raise unittest.SkipTest("未找到 Chromium")
        cls.browser = cls.playwright.chromium.launch(
            executable_path=executable, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        cls.addClassCleanup(cls.browser.close)
        env = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
        cls.html = env.get_template("rss.html").render(
            active="rss", app_version="offline-test", discovery_enabled=False,
            agent_enabled=False, csrf_token=lambda: "offline-csrf",
            static_url=lambda name: "/static/" + name,
            url_for=lambda name: "/" + name.split(".")[-1],
        )

    def make_page(self, viewport=(1280, 900), *, delayed=False):
        context = self.browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]},
            service_workers="block", is_mobile=viewport[0] < 600,
            has_touch=viewport[0] < 600,
        )
        self.addCleanup(context.close)
        unexpected, errors, pending = [], [], []
        subscription = {
            "id": 7, "name": "离线 RSS", "urls": "https://rss.invalid/feed.xml",
            "enabled": True, "action": "subscribe", "download_method": "guangya",
            "gy_target_dir": "old-dir", "gy_target_dir_name": "原有目录",
        }
        fixtures = {
            "/api/rss/subscriptions": [subscription], "/api/rss/entries": {"items": [], "offset": 0, "limit": 100, "total": 0, "has_more": False},
            "/api/rss/stats": {}, "/api/subscriptions/stats": {},
        }

        def route_request(route):
            url = urlsplit(route.request.url)
            path = unquote(url.path)
            if url.netloc == "testserver" and route.request.method == "GET":
                if path == "/rss":
                    route.fulfill(status=200, content_type="text/html", body=self.html)
                    return
                if path.startswith("/static/"):
                    asset = (ROOT / "app" / path.lstrip("/")).resolve()
                    if asset.is_relative_to(ROOT / "app/static") and asset.is_file():
                        route.fulfill(status=200, body=asset.read_bytes(), content_type=
                                      mimetypes.guess_type(str(asset))[0] or "application/octet-stream")
                        return
                if path in fixtures:
                    route.fulfill(status=200, json=fixtures[path])
                    return
                if path == "/api/guangya/dirs":
                    parent = parse_qs(url.query).get("parent_id", ["0"])[0]
                    if parent in DIRECTORIES:
                        if delayed:
                            pending.append((route, parent))
                        else:
                            route.fulfill(status=200, json=DIRECTORIES[parent])
                        return
            unexpected.append(route.request.method + " " + route.request.url)
            route.abort()

        context.route("**/*", route_request)
        context.route_web_socket("**/*", lambda ws: ws.close())
        page = context.new_page()
        page.set_default_timeout(3000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://testserver/rss#rss")
        page.locator('[title="编辑订阅"]').wait_for()
        self.addCleanup(lambda: self.assertEqual(unexpected, [], "必须完全离线且不可写 API"))
        self.addCleanup(lambda: self.assertEqual(errors, [], "页面脚本不能报错"))
        return page, pending

    def open_editor(self, page):
        page.locator('[title="编辑订阅"]').click()
        page.locator("#f_name").fill("未保存编辑内容")
        page.locator("#f_exclude").fill("720p,试看")
        self.assertEqual(page.locator("#f_gy_target").input_value(), "old-dir")

    def open_picker(self, page):
        page.locator("#pickRssTargetBtn").click()
        page.locator("#rssTargetModal").wait_for()
        page.evaluate("""async () => {
            await Promise.all(document.querySelector('#rssTargetModal').getAnimations({subtree:true})
                .filter(a => a.effect.getTiming().iterations !== Infinity).map(a => a.finished));
        }""")

    def assert_frontmost(self, page, selector):
        self.assertTrue(page.locator(selector).evaluate("""node => {
            const r = node.getBoundingClientRect();
            return node.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2));
        }"""), f"{selector} 必须通过真实命中测试，不能被下层遮挡")

    def assert_editor_preserved(self, page, target="old-dir"):
        self.assertTrue(page.locator("#subModal").is_visible())
        self.assertEqual(page.locator("#f_name").input_value(), "未保存编辑内容")
        self.assertEqual(page.locator("#f_exclude").input_value(), "720p,试看")
        self.assertEqual(page.locator("#f_gy_target").input_value(), target)
        self.assertTrue(page.locator("#pickRssTargetBtn").evaluate("e => e === document.activeElement"))

    def assert_no_overflow(self, page):
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), page.viewport_size["width"])
        for selector in ("#subModal .rss-sub-modal-card", "#rssTargetModal .settings-dir-dialog"):
            node = page.locator(selector)
            if node.count():
                self.assertTrue(node.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), selector)
                rect = node.bounding_box()
                self.assertGreaterEqual(rect["x"], 0)
                self.assertLessEqual(rect["x"] + rect["width"], page.viewport_size["width"] + 1)
                self.assertGreaterEqual(rect["y"], 0)
                self.assertLessEqual(rect["y"] + rect["height"], page.viewport_size["height"] + 1)

    def test_refresh_intervals_and_legacy_edit_values(self):
        for viewport in ((1280, 900), (390, 844)):
            with self.subTest(viewport=viewport):
                page, _ = self.make_page(viewport)
                expected = ["0", "30", "60", "180", "360", "720", "1440"]
                page.evaluate("openSubForm()")
                select = page.locator("#f_interval")
                self.assertEqual(select.locator("option").evaluate_all("nodes => nodes.map(n => n.value)"), expected)
                self.assertEqual(select.locator('option[value="720"]').text_content(), "每 12 小时")
                self.assertEqual(select.locator('option[value="1440"]').text_content(), "每 24 小时")
                for minutes in (720, 1440, 5, 15, 10):
                    page.evaluate("minutes => {closeSubForm(); subscriptions[0].refresh_interval_minutes=minutes; openSubForm(7);}", minutes)
                    self.assertEqual(select.input_value(), str(minutes))
                    self.assertEqual(select.locator("[data-current-interval]").count(), int(minutes < 30))
                page.evaluate("closeSubForm(); openSubForm()")
                self.assertEqual(select.input_value(), "0")
                self.assertEqual(select.locator("option").evaluate_all("nodes => nodes.map(n => n.value)"), expected)
                self.assert_no_overflow(page)

    def test_picker_hit_testing_expansion_selection_and_cancel_at_all_widths(self):
        for viewport in VIEWPORTS:
            with self.subTest(viewport=viewport):
                page, _ = self.make_page(viewport)
                self.open_editor(page)
                self.open_picker(page)
                self.assert_frontmost(page, "#rssTargetModal [data-dir-close]")
                page.locator("#rssTargetModal .settings-dir-open").click()
                page.locator("#rssTargetModal .settings-dir-open").click()
                page.locator("#rssTargetModal .settings-dir-open").first.wait_for()
                self.assert_no_overflow(page)
                page.locator("#rssTargetModal [data-dir-select-current]").click()
                self.assertEqual(page.locator("#rssTargetModal").count(), 0)
                self.assert_editor_preserved(page, "series")
                self.assertEqual(page.locator("#f_gy_target_name").input_value(), LONG_NAME)
                self.assert_no_overflow(page)
                self.open_picker(page)
                page.locator("#rssTargetModal [data-dir-close]").click()
                self.assert_editor_preserved(page, "series")
                page.close()

    def test_escape_closes_only_top_layer_then_editor_and_restores_focus(self):
        page, _ = self.make_page()
        self.open_editor(page)
        self.open_picker(page)
        page.keyboard.press("Escape")
        self.assertEqual(page.locator("#rssTargetModal").count(), 0)
        self.assert_editor_preserved(page)
        self.assertTrue(page.evaluate("document.body.classList.contains('modal-open')"))
        page.keyboard.press("Escape")
        self.assertFalse(page.locator("#subModal").is_visible())
        self.assertTrue(page.locator('[title="编辑订阅"]').evaluate("e => e === document.activeElement"))
        self.assertFalse(page.evaluate("document.body.classList.contains('modal-open')"))

    def test_backdrop_closes_only_top_layer_at_all_widths(self):
        for viewport in VIEWPORTS:
            with self.subTest(viewport=viewport):
                page, _ = self.make_page(viewport)
                self.open_editor(page)
                self.open_picker(page)
                page.mouse.click(2, 2)
                self.assertEqual(page.locator("#rssTargetModal").count(), 0)
                self.assert_editor_preserved(page)
                page.mouse.click(2, 2)
                self.assertFalse(page.locator("#subModal").is_visible())
                page.close()

    def test_tab_and_programmatic_focus_cannot_reach_lower_editor(self):
        page, _ = self.make_page()
        self.open_editor(page)
        self.open_picker(page)
        page.locator("#f_name").evaluate("e => e.focus()")
        self.assertTrue(page.evaluate("document.querySelector('#rssTargetModal').contains(document.activeElement)"))
        for key in ("Shift+Tab", "Tab", *(["Tab"] * 10), *(["Shift+Tab"] * 10)):
            page.keyboard.press(key)
            self.assertTrue(page.evaluate("document.querySelector('#rssTargetModal').contains(document.activeElement)"))
        page.keyboard.press("Escape")
        page.locator("#subModal button").last.focus()
        page.keyboard.press("Tab")
        self.assertTrue(page.evaluate("document.querySelector('#subModal').contains(document.activeElement)"))

    def finish_directory(self, page, pending, *, items=None, status=200):
        page.wait_for_function("document.querySelector('#rssTargetModal') !== null")
        # 给路由事件一次轮询机会，不使用外部服务或固定的网络 sleep。
        for _ in range(40):
            if pending:
                break
            page.wait_for_timeout(25)
        self.assertTrue(pending, "目录请求应被离线路由捕获")
        route, parent = pending.pop(0)
        route.fulfill(status=status, json=DIRECTORIES[parent] if items is None else items)

    def geometry(self, page):
        return [page.locator(selector).bounding_box() for selector in (
            "#rssTargetModal .settings-dir-dialog", "#rssTargetModal [data-dir-list]",
            "#rssTargetModal [data-dir-select-current]",
        )]

    def test_delayed_loading_empty_error_and_many_rows_keep_geometry_stable(self):
        for viewport in (*VIEWPORTS, (320, 360)):
            with self.subTest(viewport=viewport):
                page, pending = self.make_page(viewport, delayed=True)
                self.open_editor(page)
                self.open_picker(page)
                before = self.geometry(page)
                self.finish_directory(page, pending)
                page.locator("#rssTargetModal .settings-dir-open").wait_for()
                self.assertEqual(self.geometry(page), before)
                page.locator("#rssTargetModal .settings-dir-open").click()
                self.finish_directory(page, pending)
                page.wait_for_function("document.querySelector('#rssTargetModal [data-dir-list]').getAttribute('aria-busy') === 'false'")
                self.assertEqual(self.geometry(page), before)
                page.locator("#rssTargetModal .settings-dir-open").click()
                self.assertTrue(page.evaluate("document.querySelector('#rssTargetModal').contains(document.activeElement)"))
                self.assertTrue(page.locator("#rssTargetModal [data-dir-select-current]").is_disabled())
                self.finish_directory(page, pending)
                page.locator("#rssTargetModal .settings-dir-open").nth(19).wait_for()
                self.assertEqual(self.geometry(page), before)
                self.assert_no_overflow(page)
                page.locator("#rssTargetModal [data-dir-up]").click()
                self.finish_directory(page, pending, status=500, items={"error": "离线模拟错误"})
                page.get_by_text("离线模拟错误", exact=True).wait_for()
                self.assertEqual(self.geometry(page), before)
                page.locator("#rssTargetModal [data-dir-up]").click()
                self.finish_directory(page, pending, items=[])
                page.wait_for_function("document.querySelector('#rssTargetModal [data-dir-list]').getAttribute('aria-busy') === 'false'")
                self.assertEqual(page.locator("#rssTargetModal .settings-dir-open").count(), 0)
                self.assertEqual(self.geometry(page), before)
                page.close()

    def test_cancel_pending_request_and_reopen_cannot_backfill_old_selection(self):
        page, pending = self.make_page(delayed=True)
        self.open_editor(page)
        self.open_picker(page)
        page.keyboard.press("Escape")
        self.assert_editor_preserved(page)
        self.open_picker(page)
        # 已关闭窗口的迟到响应不能更新新窗口，更不能覆盖编辑值。
        self.finish_directory(page, pending, items=[{"is_dir": True, "file_id": "stale", "name": "过期目录"}])
        self.finish_directory(page, pending)
        page.get_by_text("离线测试驱动器", exact=True).wait_for()
        self.assertEqual(page.get_by_text("过期目录", exact=True).count(), 0)
        page.keyboard.press("Escape")
        self.assert_editor_preserved(page)

    def test_delayed_initial_focus_does_not_steal_another_input(self):
        page, _ = self.make_page((320, 640))
        page.evaluate("""async () => {
            document.querySelector('[title="编辑订阅"]').click();
            document.querySelector('#f_exclude').focus();
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        }""")
        self.assertTrue(page.locator("#f_exclude").evaluate("e => e === document.activeElement"))
        page.keyboard.type("720p")
        self.assertEqual(page.locator("#f_exclude").input_value(), "720p")
        self.assertEqual(page.locator("#f_name").input_value(), "离线 RSS")

    def test_single_selection_hides_bulk_confirm_and_row_selection_preserves_draft(self):
        page, _ = self.make_page((320, 640))
        self.open_editor(page)
        self.open_picker(page)
        self.assertFalse(page.locator("#rssTargetModal [data-dir-confirm]").is_visible())
        self.assertFalse(page.locator("#rssTargetModal [data-dir-selection-count]").is_visible())
        page.locator("#rssTargetModal .settings-dir-select").click()
        self.assert_editor_preserved(page, "drive-a")
        # 连续开关不得遗留临时层级、inert 或多个目录实例。
        for _ in range(5):
            self.open_picker(page)
            page.locator("#rssTargetModal [data-dir-title]").click()
            self.assertTrue(page.locator("#rssTargetModal").is_visible())
            page.keyboard.press("Escape")
            self.assert_editor_preserved(page, "drive-a")
            self.assertFalse(page.locator("#subModal").evaluate("e => e.inert"))
            self.assertEqual(page.locator("#subModal").evaluate("e => e.style.zIndex"), "")

    def test_programmatic_parent_close_destroys_child_and_cannot_write_next_editor(self):
        page, _ = self.make_page()
        self.open_editor(page)
        self.open_picker(page)
        page.evaluate("closeSubForm()")
        self.assertEqual(page.locator("#rssTargetModal").count(), 0)
        self.assertFalse(page.locator("#subModal").is_visible())
        page.locator('[title="编辑订阅"]').click()
        self.assertEqual(page.locator("#f_name").input_value(), "离线 RSS")
        self.assertEqual(page.locator("#f_gy_target").input_value(), "old-dir")
        self.assertFalse(page.locator("#subModal").evaluate("e => e.inert"))


if __name__ == "__main__":
    unittest.main()
