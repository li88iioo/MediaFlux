from __future__ import annotations

import shutil
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional browser contract
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
VIEWPORT_SCRIPT = ROOT / "app/static/js/viewport-inset.js"
MAIN_STYLES = ROOT / "app/static/css/main.css"
SETTINGS_STYLES = ROOT / "app/static/css/settings-agent.css"
BASE_TEMPLATE = ROOT / "app/templates/base.html"


class WindowViewportInsetContractTests(unittest.TestCase):
    def test_fixed_bottom_surfaces_share_the_visible_window_inset(self) -> None:
        main_css = MAIN_STYLES.read_text(encoding="utf-8")
        settings_css = SETTINGS_STYLES.read_text(encoding="utf-8")
        base = BASE_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("--mf-window-bottom-inset: 0px;", main_css)
        self.assertIn("inset: 0 auto var(--mf-window-bottom-inset) 0;", main_css)
        self.assertIn("bottom: var(--mf-window-bottom-inset);", main_css)
        self.assertIn("+ var(--mf-window-bottom-inset));", main_css)
        self.assertIn("bottom: var(--mf-window-bottom-inset);", settings_css)
        self.assertIn("calc(84px + var(--mf-window-bottom-inset))", settings_css)
        self.assertIn("calc(88px + var(--mf-window-bottom-inset))", settings_css)
        self.assertIn("@media (max-width: 900px) {\n    .compact-workspace-page", main_css)
        self.assertIn("@media (max-width: 900px) {\n    .settings-page .content", settings_css)
        self.assertIn("js/viewport-inset.js", base)

    @staticmethod
    def _browser_path() -> str | None:
        return next(
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

    @unittest.skipIf(sync_playwright is None, "system Python 未安装 Playwright")
    def test_only_regular_desktop_window_overflow_becomes_a_bottom_inset(self) -> None:
        browser_path = self._browser_path()
        if not browser_path:
            self.skipTest("未找到可用的本机 Chrome/Chromium")

        script = VIEWPORT_SCRIPT.read_text(encoding="utf-8")
        playwright = sync_playwright().start()
        browser = None
        try:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=browser_path,
                args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(f"<!doctype html><html><head><script>{script}</script></head><body></body></html>")
            result = page.evaluate(
                """
                () => ({
                    clippedDesktop: window.MediaFluxViewport.computeBottomInset({
                        windowTop: 32,
                        outerWidth: 1600,
                        outerHeight: 1000,
                        innerWidth: 1556,
                        availableTop: 0,
                        availableHeight: 1000,
                    }),
                    fullscreen: window.MediaFluxViewport.computeBottomInset({
                        windowTop: 0,
                        outerWidth: 1600,
                        outerHeight: 1000,
                        innerWidth: 1600,
                        availableTop: 0,
                        availableHeight: 1000,
                    }),
                    visibleWindow: window.MediaFluxViewport.computeBottomInset({
                        windowTop: 32,
                        outerWidth: 1600,
                        outerHeight: 968,
                        innerWidth: 1556,
                        availableTop: 0,
                        availableHeight: 1000,
                    }),
                    tabletPortrait: window.MediaFluxViewport.computeBottomInset({
                        windowTop: 32,
                        outerWidth: 820,
                        outerHeight: 1000,
                        innerWidth: 820,
                        availableTop: 0,
                        availableHeight: 1000,
                    }),
                    dockedDevTools: window.MediaFluxViewport.computeBottomInset({
                        windowTop: 32,
                        outerWidth: 1600,
                        outerHeight: 1000,
                        innerWidth: 1041,
                        availableTop: 0,
                        availableHeight: 1000,
                    }),
                    deviceEmulation: window.MediaFluxViewport.computeBottomInset({
                        windowTop: 0,
                        outerWidth: 1180,
                        outerHeight: 1041,
                        innerWidth: 1180,
                        availableTop: 0,
                        availableHeight: 941,
                    }),
                })
                """
            )
        finally:
            if browser is not None:
                browser.close()
            playwright.stop()

        self.assertEqual(result["clippedDesktop"], 32)
        self.assertEqual(result["fullscreen"], 0)
        self.assertEqual(result["visibleWindow"], 0)
        self.assertEqual(result["tabletPortrait"], 0)
        self.assertEqual(result["dockedDevTools"], 0)
        self.assertEqual(result["deviceEmulation"], 0)

    @unittest.skipIf(sync_playwright is None, "system Python 未安装 Playwright")
    def test_settings_savebar_and_logout_stay_visible_across_tablet_sizes(self) -> None:
        browser_path = self._browser_path()
        if not browser_path:
            self.skipTest("未找到可用的本机 Chrome/Chromium")

        styles = MAIN_STYLES.read_text(encoding="utf-8") + SETTINGS_STYLES.read_text(encoding="utf-8")
        tabs = "".join(
            f'<button class="settings-tab"><span>分区 {index}</span></button>'
            for index in range(1, 8)
        )
        html = f"""
            <!doctype html>
            <html data-theme="light" data-sidebar="expanded">
              <head><style>{styles}</style></head>
              <body class="settings-page">
                <div class="app-shell">
                  <aside class="sidebar">
                    <div class="brand">MediaFlux</div>
                    <nav class="nav"><a class="nav-item active"><span>设置</span></a></nav>
                    <footer class="sidebar-footer"><a class="nav-item logout"><span>退出登录</span></a></footer>
                  </aside>
                  <main class="main">
                    <header class="workspace-bar"><button id="toggleSidebar"></button></header>
                    <div class="content">
                      <div class="settings-workspace">
                        <nav class="settings-index">{tabs}</nav>
                        <div class="settings-stage">
                          <form class="settings-panel settings-panel-agent active">
                            <div class="agent-settings-shell">
                              <div class="agent-settings-stack">
                                <section class="agent-settings-card agent-settings-card-global"><header class="agent-settings-card-head"><h3>Media Agent</h3></header></section>
                                <section class="agent-settings-card agent-settings-card-model"><header class="agent-settings-card-head"><h3>模型路由</h3></header></section>
                              </div>
                            </div>
                            <div class="settings-savebar agent-settings-savebar">
                              <span>仅保存当前分区</span>
                              <button class="btn btn-primary">保存 Agent 设置</button>
                            </div>
                          </form>
                        </div>
                      </div>
                    </div>
                  </main>
                </div>
              </body>
            </html>
        """

        playwright = sync_playwright().start()
        browser = None
        try:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=browser_path,
                args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(html)
            results = []
            for width, height in ((768, 1024), (820, 1180), (1041, 941), (1180, 820), (1440, 900)):
                page.set_viewport_size({"width": width, "height": height})
                page.evaluate("document.documentElement.style.setProperty('--mf-window-bottom-inset', '0px')")
                page.wait_for_timeout(450)
                result = page.evaluate(
                    """
                    async () => {
                        const savebar = document.querySelector('.settings-savebar');
                        const button = savebar.querySelector('.btn');
                        const sidebar = document.querySelector('.sidebar');
                        const barRect = savebar.getBoundingClientRect();
                        const buttonRect = button.getBoundingClientRect();
                        const closedSidebarTransform = getComputedStyle(sidebar).transform;
                        sidebar.classList.add('open');
                        await new Promise((resolve) => setTimeout(resolve, 450));
                        const logoutRect = document.querySelector('.logout').getBoundingClientRect();
                        const openSidebarRect = sidebar.getBoundingClientRect();
                        sidebar.classList.remove('open');
                        return {
                            width: innerWidth,
                            height: innerHeight,
                            flexDirection: getComputedStyle(savebar).flexDirection,
                            bar: {left: barRect.left, right: barRect.right, bottom: barRect.bottom},
                            button: {left: buttonRect.left, right: buttonRect.right, bottom: buttonRect.bottom},
                            closedSidebarTransform,
                            agentColumnCount: getComputedStyle(document.querySelector('.agent-settings-stack')).gridTemplateColumns.split(' ').filter(Boolean).length,
                            openSidebarBottom: openSidebarRect.bottom,
                            logoutBottom: logoutRect.bottom,
                        };
                    }
                    """
                )
                results.append(result)
        finally:
            if browser is not None:
                browser.close()
            playwright.stop()

        for result in results:
            width = result["width"]
            height = result["height"]
            expected_left = 0 if width <= 900 else 160
            expected_direction = "column" if width <= 900 else "row"
            self.assertAlmostEqual(result["bar"]["left"], expected_left, delta=1)
            self.assertAlmostEqual(result["bar"]["right"], width, delta=1)
            self.assertAlmostEqual(result["bar"]["bottom"], height, delta=1)
            self.assertGreaterEqual(result["button"]["left"], expected_left)
            self.assertLessEqual(result["button"]["right"], width + 1)
            self.assertLessEqual(result["button"]["bottom"], height + 1)
            self.assertEqual(result["flexDirection"], expected_direction)
            self.assertEqual(result["agentColumnCount"], 1 if width <= 1280 else 2)
            if width <= 900:
                self.assertNotEqual(result["closedSidebarTransform"], "none")
                self.assertAlmostEqual(result["openSidebarBottom"], height, delta=1)
                self.assertLessEqual(result["logoutBottom"], height + 1)


    @unittest.skipIf(sync_playwright is None, "system Python 未安装 Playwright")
    def test_shared_workspace_savebar_uses_the_same_tablet_breakpoint(self) -> None:
        browser_path = self._browser_path()
        if not browser_path:
            self.skipTest("未找到可用的本机 Chrome/Chromium")

        styles = MAIN_STYLES.read_text(encoding="utf-8")
        html = f"""
            <!doctype html>
            <html data-theme="light" data-sidebar="expanded">
              <head><style>{styles}</style></head>
              <body class="persistent-savebar-page compact-workspace-page">
                <aside class="sidebar"><footer class="sidebar-footer"><span>退出登录</span></footer></aside>
                <main class="main">
                  <div class="content"><div style="height:1200px"></div></div>
                  <div class="settings-savebar workspace-savebar">
                    <span>尚未修改</span>
                    <button class="btn btn-primary">保存 STRM 设置</button>
                  </div>
                </main>
              </body>
            </html>
        """

        playwright = sync_playwright().start()
        browser = None
        try:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=browser_path,
                args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_content(html)
            results = []
            for width, height in ((768, 1024), (820, 1180), (1180, 820), (1440, 900)):
                page.set_viewport_size({"width": width, "height": height})
                page.evaluate("document.documentElement.style.setProperty('--mf-window-bottom-inset', '0px')")
                page.wait_for_timeout(450)
                results.append(
                    page.evaluate(
                        """
                        () => {
                            const savebar = document.querySelector('.workspace-savebar');
                            const button = savebar.querySelector('.btn');
                            const barRect = savebar.getBoundingClientRect();
                            const buttonRect = button.getBoundingClientRect();
                            return {
                                width: innerWidth,
                                height: innerHeight,
                                flexDirection: getComputedStyle(savebar).flexDirection,
                                bar: {left: barRect.left, right: barRect.right, bottom: barRect.bottom},
                                button: {left: buttonRect.left, right: buttonRect.right, bottom: buttonRect.bottom},
                            };
                        }
                        """
                    )
                )
        finally:
            if browser is not None:
                browser.close()
            playwright.stop()

        for result in results:
            width = result["width"]
            height = result["height"]
            expected_left = 0 if width <= 900 else 160
            self.assertAlmostEqual(result["bar"]["left"], expected_left, delta=1)
            self.assertAlmostEqual(result["bar"]["right"], width, delta=1)
            self.assertAlmostEqual(result["bar"]["bottom"], height, delta=1)
            self.assertGreaterEqual(result["button"]["left"], expected_left)
            self.assertLessEqual(result["button"]["right"], width + 1)
            self.assertLessEqual(result["button"]["bottom"], height + 1)
            self.assertEqual(result["flexDirection"], "column" if width <= 900 else "row")


if __name__ == "__main__":
    unittest.main()
