"""侧栏顺序与媒体档案弹窗标题的前端契约。"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional browser dependency
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
BASE_TEMPLATE = ROOT / "app/templates/base.html"
DISCOVERY_TEMPLATE = ROOT / "app/templates/discovery.html"
PROFILE_DIALOG = ROOT / "app/templates/_media_profile_dialog.html"
MORE_TEMPLATE = ROOT / "app/templates/guangya_more.html"
THEME_BOOTSTRAP = ROOT / "app/templates/_theme_bootstrap.html"
APP_SCRIPT = ROOT / "app/static/js/app.js"
MAIN_STYLES = ROOT / "app/static/css/main.css"


class NavigationDiscoveryUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = BASE_TEMPLATE.read_text(encoding="utf-8")
        self.discovery = (
            DISCOVERY_TEMPLATE.read_text(encoding="utf-8")
            + PROFILE_DIALOG.read_text(encoding="utf-8")
        )
        self.more = MORE_TEMPLATE.read_text(encoding="utf-8")

    def test_guangya_submenu_defaults_open_and_remembers_manual_choice(self):
        bootstrap = THEME_BOOTSTRAP.read_text(encoding="utf-8")
        script = APP_SCRIPT.read_text(encoding="utf-8")
        styles = MAIN_STYLES.read_text(encoding="utf-8")

        self.assertIn('class="nav-cluster open{% if active in', self.base)
        self.assertIn('data-nav-cluster="guangya" data-nav-default-open="true"', self.base)
        self.assertIn('aria-controls="guangyaSubmenu guangyaFlyout" aria-expanded="true"', self.base)
        self.assertIn("localStorage.getItem('mediaflux.nav.guangya.open')", bootstrap)
        self.assertIn("document.documentElement.dataset.navGuangya = guangyaNav", bootstrap)
        self.assertIn("`mediaflux.nav.${clusterName}.open`", script)
        self.assertIn("saveNavClusterPreference(cluster, open)", script)
        self.assertIn("delete document.documentElement.dataset.navGuangya", script)
        self.assertIn(':root[data-nav-guangya="closed"]', styles)

    def test_guangya_secondary_tools_are_merged_into_more_in_both_menus(self):
        for menu_id in ("guangyaSubmenu", "guangyaFlyout"):
            start = self.base.index(f'id="{menu_id}"')
            end = self.base.index("</div>", start)
            menu = self.base[start:end]
            self.assertEqual(menu.count("<span>更多</span>"), 1)
            self.assertIn("url_for('pages.guangya_more')", menu)
            self.assertNotIn("<span>分享转存</span>", menu)
            self.assertNotIn("<span>GCID 清单</span>", menu)

    def test_more_page_keeps_both_tools_in_accessible_stable_panels(self):
        self.assertIn('class="strm-nav-tabs guangya-more-tabs"', self.more)
        self.assertEqual(self.more.count('class="strm-tab-btn'), 2)
        self.assertIn('role="tablist"', self.more)
        self.assertIn('data-more-view="share"', self.more)
        self.assertIn('data-more-view="gcid"', self.more)
        self.assertIn('{% include "_share_transfer_content.html" %}', self.more)
        self.assertIn('{% include "_gcid_content.html" %}', self.more)
        self.assertIn("panel.hidden=name!==next", self.more)
        self.assertIn("window.history.replaceState", self.more)

    @unittest.skipIf(sync_playwright is None, "system Python 未安装 Playwright")
    def test_sidebar_icons_reserve_space_before_lucide_hydration(self):
        browser_path = next(
            (
                path
                for path in (
                    shutil.which("google-chrome"),
                    shutil.which("google-chrome-stable"),
                    shutil.which("chromium"),
                    shutil.which("chromium-browser"),
                )
                if path
            ),
            None,
        )
        if not browser_path:
            self.skipTest("未找到可用的本机 Chrome/Chromium")

        styles = MAIN_STYLES.read_text(encoding="utf-8")
        html = f"""
            <!doctype html>
            <html data-theme="dark" data-sidebar="expanded">
              <head><style>{styles}</style></head>
              <body>
                <aside class="sidebar">
                  <nav class="nav">
                    <a class="nav-item active">
                      <i data-lucide="layout-dashboard"></i><span>看板</span>
                    </a>
                    <div class="nav-cluster open">
                      <div class="nav-submenu">
                        <a class="nav-subitem active">
                          <i data-lucide="folder-cog"></i><span>光鸭整理</span>
                        </a>
                      </div>
                    </div>
                    <a class="nav-flyout-item active">
                      <i data-lucide="log-in"></i><span>登录</span>
                    </a>
                  </nav>
                </aside>
              </body>
            </html>
        """

        assert sync_playwright is not None
        playwright = sync_playwright().start()
        browser = None
        try:
            browser = playwright.chromium.launch(
                headless=True, executable_path=browser_path, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(html)
            before = page.evaluate(
                """
                () => [...document.querySelectorAll('.nav-item, .nav-subitem, .nav-flyout-item')].map((item) => {
                    const icon = item.querySelector('[data-lucide]');
                    const label = item.querySelector('span');
                    const iconRect = icon.getBoundingClientRect();
                    const labelRect = label.getBoundingClientRect();
                    return {iconWidth: iconRect.width, labelX: labelRect.x};
                })
                """
            )
            page.evaluate(
                """
                () => document.querySelectorAll('i[data-lucide]').forEach((placeholder) => {
                    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                    icon.setAttribute('data-lucide', placeholder.dataset.lucide);
                    placeholder.replaceWith(icon);
                })
                """
            )
            after = page.evaluate(
                """
                () => [...document.querySelectorAll('.nav-item, .nav-subitem, .nav-flyout-item')].map((item) => {
                    const icon = item.querySelector('[data-lucide]');
                    const label = item.querySelector('span');
                    const iconRect = icon.getBoundingClientRect();
                    const labelRect = label.getBoundingClientRect();
                    return {iconWidth: iconRect.width, labelX: labelRect.x};
                })
                """
            )
        finally:
            if browser is not None:
                browser.close()
            playwright.stop()

        self.assertEqual([item["iconWidth"] for item in before], [18, 14, 15])
        self.assertEqual(after, before)

    def test_catalogue_dialog_uses_single_bilingual_title(self):
        title = '<h2 id="discovery-detail-title">CATALOGUE RECORD / 媒体档案</h2>'
        self.assertIn(title, self.discovery)
        dialog_start = self.discovery.index('id="discovery-detail-dialog"')
        dialog_end = self.discovery.index("</dialog>", dialog_start)
        dialog = self.discovery[dialog_start:dialog_end]
        self.assertNotIn('<span class="discovery-eyebrow">CATALOGUE RECORD</span>', dialog)

    def test_catalogue_dialog_title_stays_on_one_line_on_mobile(self):
        styles = (ROOT / "app/static/css/main.css").read_text(encoding="utf-8")
        self.assertIn(
            ".discovery-dialog-head h2 { font-size: clamp(13px,4.2vw,18px); "
            "line-height: 1.2; letter-spacing: -.01em; white-space: nowrap; }",
            styles,
        )


@unittest.skipIf(sync_playwright is None, "未安装 Playwright")
class OrganizeRulesFirstPaintTests(unittest.TestCase):
    """完整模板与真实静态脚本；API 全部拦截，不连接开发服务或读真实配置。"""

    @classmethod
    def setUpClass(cls):
        from jinja2 import Environment, FileSystemLoader

        assert sync_playwright is not None
        cls.playwright = cls.enterClassContext(sync_playwright())
        bundled = Path(cls.playwright.chromium.executable_path)
        executable = str(bundled) if bundled.is_file() else next(
            (path for name in ("google-chrome", "chromium", "chromium-browser")
             if (path := shutil.which(name))), None,
        )
        if not executable:
            raise unittest.SkipTest("未找到 Chrome/Chromium")
        cls.browser = cls.playwright.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox"],
        )
        cls.addClassCleanup(cls.browser.close)
        cls.templates = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)

    @classmethod
    def _html(cls, *, execute=False, pause_parser=True):
        html = cls.templates.get_template("organize.html").render(
            organize_view="execute" if execute else "rules",
            active="organize" if execute else "organize_rules",
            organize_initial_sources=[], organize_video_exts=["mkv", "mp4"],
            organize_metadata_exts=["srt", "nfo"], csrf_token=lambda: "test",
            url_for=lambda name: "/" + name.split(".")[-1].replace("_", "-"),
            static_url=lambda path: "/static/" + path,
        )
        if pause_parser and not execute:
            # 模拟 HTML 分块到达：所有 panel 刚解析完、handoff 尚未运行就强制停下，
            # 检验首帧 CSS，而不是只等 load 完成后才断言。
            html = html.replace(
                '</form>\n    \n</div>',
                '</form>\n<script src="/__parser_pause.js"></script>\n    \n</div>',
                1,
            )
            if '/__parser_pause.js' not in html:
                raise AssertionError("规则表单测试解析暂停点失效")
        return html

    def _page(self, width=1440, *, execute=False, javascript=True, block_business=False):
        import json
        import time
        from urllib.parse import urlparse

        context = self.browser.new_context(
            viewport={"width": width, "height": 900}, java_script_enabled=javascript,
            reduced_motion="reduce",
        )
        self.addCleanup(context.close)
        page = context.new_page()
        errors, writes = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        context.add_init_script("""(() => {
            window.__rulesFrames = [];
            window.__captureRules = () => {
                const panels = [...document.querySelectorAll('#organizeWorkspace [data-tab-panel]')];
                const tabs = [...document.querySelectorAll('#organizeRulesNav [data-tab-target]')];
                const form = document.getElementById('organizeConfigForm');
                const rect = form?.getBoundingClientRect();
                return {
                    panels: panels.filter(p => getComputedStyle(p).display !== 'none').map(p => p.dataset.tabPanel),
                    colors: Object.fromEntries(tabs.map(b => [b.dataset.tabTarget, getComputedStyle(b).backgroundColor])),
                    active: tabs.filter(b => b.classList.contains('active')).map(b => b.dataset.tabTarget),
                    marked: document.documentElement.dataset.organizeRulesInitialTab || '',
                    form: rect ? {x:rect.x, y:rect.y, width:rect.width} : null,
                };
            };
            function sample() {
                if (document.querySelectorAll('#organizeWorkspace [data-tab-panel]').length === 3)
                    window.__rulesFrames.push(window.__captureRules());
                if (window.__rulesFrames.length < 300) requestAnimationFrame(sample);
            }
            requestAnimationFrame(sample);
        })();""")
        html = self._html(execute=execute)

        def respond(route):
            request = route.request
            path = urlparse(request.url).path
            if request.method != "GET":
                writes.append((request.method, path))
                route.abort()
            elif path in {"/organize-rules", "/organize"}:
                route.fulfill(body=html, content_type="text/html")
            elif path == "/__parser_pause.js":
                time.sleep(0.08)
                route.fulfill(body="window.__parserCheckpoint=window.__captureRules();", content_type="application/javascript")
            elif path.startswith("/static/"):
                file = (ROOT / "app" / path.lstrip("/")).resolve()
                if not file.is_relative_to(ROOT / "app/static") or not file.is_file():
                    route.abort()
                elif path.endswith("/organize.js"):
                    if block_business:
                        route.abort()
                    else:
                        time.sleep(0.08)
                        route.fulfill(
                            body="window.__beforeBusiness=window.__captureRules();\n" + file.read_text(),
                            content_type="application/javascript",
                        )
                else:
                    route.fulfill(path=str(file))
            elif path.startswith("/api/"):
                data = {"GY_ORGANIZE_AUTOMATIC_MATCH_PRESET": "balanced"} if path == "/api/config" else {}
                route.fulfill(body=json.dumps(data), content_type="application/json")
            else:
                route.abort()

        context.route("**/*", respond)
        return page, errors, writes

    def _assert_initial(self, page, target):
        page.wait_for_function("window.__rulesFrames.length > 0")
        checkpoint = page.evaluate("window.__parserCheckpoint")
        assert (checkpoint) is not None
        self.assertEqual(checkpoint["panels"], [target])
        # 解析期目标 tab 已高亮，与 DOM 接管完成后的颜色一致。
        before = page.evaluate("window.__beforeBusiness")
        current = page.evaluate("window.__captureRules()")
        self.assertEqual(checkpoint["colors"], current["colors"])
        self.assertEqual(before["active"], [target])
        self.assertEqual(before["marked"], "")
        self.assertEqual(current["panels"], [target])
        self.assertEqual(before["form"], current["form"])
        for frame in page.evaluate("window.__rulesFrames"):
            self.assertEqual(frame["panels"], [target])
        for name in ("naming", "policy", "delivery"):
            button = page.locator(f'#organizeRulesNav [data-tab-target="{name}"]')
            self.assertEqual(button.get_attribute("aria-selected"), str(name == target).lower())
            self.assertEqual(button.evaluate("el=>el.tabIndex"), 0 if name == target else -1)
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))

    def test_policy_delivery_repeated_refresh_never_paints_naming(self):
        for width in (320, 390, 1440):
            page, errors, writes = self._page(width)
            for target in ("policy", "delivery"):
                for attempt in range(5):
                    with self.subTest(width=width, target=target, refresh=attempt):
                        if attempt:
                            page.reload(wait_until="load")
                        else:
                            page.goto("about:blank")
                            page.goto("http://mediaflux.test/organize-rules#" + target)
                        self._assert_initial(page, target)
            self.assertEqual(errors, [])
            self.assertEqual(writes, [])

    def test_default_invalid_hash_and_history_keep_original_navigation(self):
        page, errors, writes = self._page()
        for fragment in ("", "#naming", "#unknown", "#POLICY", "#%70olicy"):
            with self.subTest(fragment=fragment):
                page.goto("http://mediaflux.test/organize-rules" + fragment)
                page.reload()
                self._assert_initial(page, "naming")
        page.evaluate("location.hash='policy'")
        page.wait_for_function("document.querySelector('[data-tab-panel=policy]').hidden === false")
        page.evaluate("location.hash='delivery'")
        page.wait_for_function("document.querySelector('[data-tab-panel=delivery]').hidden === false")
        page.go_back()
        page.wait_for_function("document.querySelector('[data-tab-panel=policy]').hidden === false")
        page.go_forward()
        page.wait_for_function("document.querySelector('[data-tab-panel=delivery]').hidden === false")
        page.locator('#organizeRulesNav [data-tab-target="naming"]').click()
        self.assertEqual(page.evaluate("location.hash"), "")
        self.assertEqual(page.evaluate("window.__captureRules().panels"), ["naming"])
        self.assertEqual(errors, [])
        self.assertEqual(writes, [])

    def test_failed_business_script_still_displays_requested_panel(self):
        page, errors, writes = self._page(block_business=True)
        page.goto("http://mediaflux.test/organize-rules#delivery")
        self.assertEqual(page.evaluate("window.__captureRules().panels"), ["delivery"])
        self.assertEqual(page.evaluate("window.__captureRules().active"), ["delivery"])
        self.assertEqual(page.evaluate("window.__captureRules().marked"), "")
        self.assertEqual(errors, [])
        self.assertEqual(writes, [])

    def test_no_javascript_falls_back_to_visible_default_execute_has_no_bootstrap(self):
        page, _errors, writes = self._page(javascript=False)
        page.goto("http://mediaflux.test/organize-rules#policy")
        self.assertTrue(page.locator('[data-tab-panel="naming"]').is_visible())
        self.assertFalse(page.locator('[data-tab-panel="policy"]').is_visible())
        self.assertEqual(writes, [])
        html = self._html(execute=True)
        self.assertNotIn("organizeRulesInitialTab", html)
        self.assertNotIn("organize-rules-nav-card", html)
        self.assertIn('id="organizeSourceList"', html)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(sync_playwright is None, "未安装 Playwright")
class SidebarMotionTests(unittest.TestCase):
    """真实侧栏模板/脚本，逐帧检查宽度动画，不连接生产服务。"""

    def test_labels_keep_geometry_during_expansion_and_rapid_reversal(self):
        import mimetypes
        from urllib.parse import urlsplit

        from jinja2 import Environment, FileSystemLoader

        assert sync_playwright is not None
        html = Environment(
            loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True,
        ).get_template("base.html").render(
            active="settings", agent_enabled=True, discovery_enabled=True,
            csrf_token=lambda: "test", url_for=lambda name: "/" + name.split(".")[-1],
            static_url=lambda path: "/static/" + path,
        )
        with sync_playwright() as playwright:
            bundled = Path(playwright.chromium.executable_path)
            executable = str(bundled) if bundled.is_file() else shutil.which("google-chrome") or shutil.which("chromium")
            if not executable:
                self.skipTest("未找到 Chrome/Chromium")
            browser = playwright.chromium.launch(executable_path=executable, headless=True, args=["--no-sandbox"])
            for width in (901, 1440):
                with self.subTest(width=width), browser.new_context(viewport={"width": width, "height": 1100}) as context:
                    errors, unexpected = [], []
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def respond(route):
                        path = urlsplit(route.request.url).path
                        if path == "/":
                            route.fulfill(body=html, content_type="text/html")
                        elif path.startswith("/static/"):
                            file = ROOT / "app" / path.lstrip("/")
                            route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or "application/octet-stream")
                        else:
                            unexpected.append(path)
                            route.abort()

                    page.route("**/*", respond)
                    page.goto("http://mediaflux.test/")
                    samples = page.evaluate("""() => {
                        const sidebar = document.querySelector('.sidebar');
                        const toggle = document.querySelector('#collapseSidebar');
                        const measure = () => [...sidebar.querySelectorAll('.nav-item > span, .nav-subitem > span')].map(el => {
                            const rect = el.getBoundingClientRect();
                            const row = el.parentElement.getBoundingClientRect();
                            return {text: el.textContent, width: rect.width, height: rect.height, y: row.y, rowHeight: row.height};
                        });
                        const baseline = measure();
                        const frames = [];
                        function seek(time) {
                            sidebar.getBoundingClientRect();
                            const animation = sidebar.getAnimations().find(a => a.transitionProperty === 'width');
                            if (!animation) throw new Error('没有实际运行侧栏宽度动画');
                            animation.pause();
                            animation.currentTime = time;
                            sidebar.getBoundingClientRect();
                        }
                        toggle.click(); seek(350);
                        // 每次在展开到一半时反向，再重新展开；不依赖机器速度恰好抓到首帧。
                        for (let cycle = 0; cycle < 3; cycle++) {
                            toggle.click();
                            for (const time of [0, 16, 32, 64, 175]) {
                                seek(time);
                                frames.push(measure());
                            }
                            toggle.click(); seek(cycle === 2 ? 350 : 16);
                        }
                        return {baseline, frames};
                    }""")
                    for index, frame in enumerate(samples["frames"]):
                        for actual, expected in zip(frame, samples["baseline"], strict=True):
                            for key in ("width", "height", "y", "rowHeight"):
                                self.assertAlmostEqual(actual[key], expected[key], delta=0.1,
                                                       msg=f"frame={index} {actual['text']} {key}")
                    self.assertEqual(page.locator("html").get_attribute("data-sidebar"), "collapsed")
                    self.assertFalse(page.locator("#guangyaSubmenu").is_visible())
                    page.locator(".nav-cluster-toggle").click()
                    self.assertTrue(page.locator("#guangyaFlyout").is_visible())
                    page.keyboard.press("Escape")
                    self.assertFalse(page.locator("#guangyaFlyout").is_visible())
                    page.reload()
                    self.assertEqual(page.locator("html").get_attribute("data-sidebar"), "collapsed")
                    page.emulate_media(reduced_motion="reduce")
                    page.locator("#collapseSidebar").click()
                    self.assertTrue(page.locator("#guangyaSubmenu").is_visible())
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.locator("#toggleSidebar").click()
                    self.assertTrue(page.locator("#sidebar").evaluate("el => el.classList.contains('open')"))
                    self.assertEqual(page.locator(".nav-subitem > span").evaluate_all(
                        "els => els.map(el => getComputedStyle(el).whiteSpace)"
                    ), ["nowrap"] * 6)
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                    page.keyboard.press("Escape")
                    self.assertFalse(page.locator("#sidebar").evaluate("el => el.classList.contains('open')"))
                    self.assertEqual(errors, [])
                    self.assertEqual(unexpected, [])
            browser.close()
