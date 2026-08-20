"""侧栏顺序与媒体档案弹窗标题的前端契约。"""
from __future__ import annotations

import unittest
from pathlib import Path


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
