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
MORE_TEMPLATE = ROOT / "app/templates/guangya_more.html"
THEME_BOOTSTRAP = ROOT / "app/templates/_theme_bootstrap.html"
APP_SCRIPT = ROOT / "app/static/js/app.js"
MAIN_STYLES = ROOT / "app/static/css/main.css"


class NavigationDiscoveryUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = BASE_TEMPLATE.read_text(encoding="utf-8")
        self.discovery = DISCOVERY_TEMPLATE.read_text(encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
