"""设置页说明提示与索引站点选择的真实 Chromium 浏览器回归。"""
from __future__ import annotations

import unittest

from tests import test_agent_nsfw_clean_review_settings_browser as fixture

try:
    from playwright.sync_api import expect
except ImportError:  # pragma: no cover - 与既有浏览器门禁一致
    expect = None


@unittest.skipUnless(fixture.sync_playwright is not None, "未安装 Playwright")
class SettingsReviewTooltipsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.NsfwCleanReviewSettingsBrowserTests.setUpClass()
        cls.browser = fixture.NsfwCleanReviewSettingsBrowserTests.browser

    @classmethod
    def tearDownClass(cls):
        fixture.NsfwCleanReviewSettingsBrowserTests.tearDownClass()

    _page = fixture.NsfwCleanReviewSettingsBrowserTests._page
    _resolve_config = staticmethod(fixture.NsfwCleanReviewSettingsBrowserTests._resolve_config)
    _rect = staticmethod(fixture.NsfwCleanReviewSettingsBrowserTests._rect)
    _assert_same_rect = fixture.NsfwCleanReviewSettingsBrowserTests._assert_same_rect

    def _ready(self, width, *, touch=False):
        page, errors = self._page(width, {fixture.PARENT_KEY: "1"}, touch=touch)
        styles = fixture.TEMPLATE.split("<style>", 1)[1].split("</style>", 1)[0]
        page.add_style_tag(content=styles)
        self._resolve_config(page)
        return page, errors

    def _bounded(self, page, tooltip):
        rect = tooltip.bounding_box()
        viewport = page.viewport_size
        self.assertGreaterEqual(rect["x"], 0)
        self.assertGreaterEqual(rect["y"], 0)
        self.assertLessEqual(rect["x"] + rect["width"], viewport["width"] + .5)
        self.assertLessEqual(rect["y"] + rect["height"], viewport["height"] + .5)
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))

    def test_hover_keyboard_escape_and_popup_do_not_shift_rows_or_change_settings(self):
        page, errors = self._ready(1280)
        for name, prefix in (("受控季集研究", "episodeResearch"), ("允许光鸭 NSFW 清洗入库", "nsfwCleanReview")):
            with self.subTest(name=name):
                page.evaluate("document.activeElement?.blur()")
                page.mouse.move(0, 0)
                trigger = page.get_by_role("button", name=f"{name}说明", exact=True)
                trigger.scroll_into_view_if_needed()
                tooltip = page.locator(f"#{prefix}Tooltip")
                option = trigger.locator("xpath=ancestor::div[contains(@class,'metadata-option')]")
                before = self._rect(option)
                expect(tooltip).to_be_hidden()
                expect(page.locator(f"#{prefix}Description")).to_be_hidden()
                trigger.hover()
                expect(tooltip).to_be_visible()
                self._bounded(page, tooltip)
                self._assert_same_rect(before, self._rect(option))
                tooltip.hover()  # 鼠标可移入长说明继续阅读，不因离开图标立即消失。
                expect(tooltip).to_be_visible()
                page.mouse.move(0, 0)
                expect(tooltip).to_be_hidden()
                page.keyboard.press("Tab")
                trigger.focus()
                expect(tooltip).to_be_visible()
                trigger.press("Escape")
                expect(tooltip).to_be_hidden()
                expect(trigger).to_be_focused()
                trigger.press("Tab")
                expect(tooltip).to_be_hidden()
                self._assert_same_rect(before, self._rect(option))
        self.assertEqual(page.evaluate("window.__settingsWrites"), [])
        self.assertEqual(page.evaluate("collectConfigFields(document.getElementById('settings-panel-metadata'))"), {})
        self.assertEqual(errors, [])

    def test_touch_toggle_outside_dismiss_and_viewport_bounds(self):
        for width in (320, 390, 768):
            with self.subTest(width=width):
                page, errors = self._ready(width, touch=True)
                for name, prefix in (("受控季集研究", "episodeResearch"), ("允许光鸭 NSFW 清洗入库", "nsfwCleanReview")):
                    trigger = page.get_by_role("button", name=f"{name}说明", exact=True)
                    trigger.scroll_into_view_if_needed()
                    rect = trigger.bounding_box()
                    self.assertGreaterEqual(rect["width"], 44)
                    self.assertGreaterEqual(rect["height"], 44)
                    option = trigger.locator("xpath=ancestor::div[contains(@class,'metadata-option')]")
                    before = self._rect(option)
                    tooltip = page.locator(f"#{prefix}Tooltip")
                    trigger.tap()
                    expect(tooltip).to_be_visible()
                    self._bounded(page, tooltip)
                    self._assert_same_rect(before, self._rect(option))
                    trigger.tap()
                    expect(tooltip).to_be_hidden()
                    trigger.tap()
                    expect(tooltip).to_be_visible()
                    page.touchscreen.tap(width - 4, 4)
                    expect(tooltip).to_be_hidden()
                self.assertEqual(page.evaluate("window.__settingsWrites"), [])
                self.assertEqual(page.evaluate("collectConfigFields(document.getElementById('settings-panel-metadata'))"), {})
                self.assertEqual(errors, [])

    def test_mobile_auto_scroll_click_keeps_help_open_after_browser_frames(self):
        for width in (320, 390):
            with self.subTest(width=width):
                page, errors = self._ready(width)
                page.evaluate("window.scrollTo(0, 0)")
                page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                trigger = page.locator('[data-help-tooltip="episodeResearchTooltip"]')
                trigger.click()
                page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                self.assertGreater(page.evaluate("scrollY"), 0)
                expect(page.locator("#episodeResearchTooltip")).to_be_visible()
                self._bounded(page, page.locator("#episodeResearchTooltip"))
                self.assertEqual(page.evaluate("window.__settingsWrites"), [])
                self.assertEqual(errors, [])
                page.close()

    def test_scroll_events_close_help_only_when_the_trigger_actually_moves(self):
        page, errors = self._ready(390)
        trigger = page.locator('[data-help-tooltip="episodeResearchTooltip"]')
        trigger.scroll_into_view_if_needed()
        page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        trigger.click()
        tip = page.locator("#episodeResearchTooltip")
        expect(tip).to_be_visible()
        page.evaluate("document.dispatchEvent(new Event('scroll'))")
        expect(tip).to_be_visible()
        tip.dispatch_event("scroll")
        expect(tip).to_be_visible()
        page.evaluate("window.scrollBy(0, -30)")
        expect(tip).to_be_hidden()
        page.keyboard.press("Tab")
        trigger.focus()
        expect(tip).to_be_visible()
        trigger.press("Escape")
        expect(tip).to_be_hidden()
        self.assertEqual(page.evaluate("window.__settingsWrites"), [])
        self.assertEqual(errors, [])
        page.close()

    def test_loading_preserves_geometry_and_disabled_settings_still_expose_help(self):
        page, errors = self._page(390, {fixture.PARENT_KEY: "0"})
        trigger = page.locator('[data-help-tooltip="episodeResearchTooltip"]')
        # 现有页面在配置加载前整体visibility:hidden，但布局占位必须稳定。
        expect(trigger).to_be_hidden()
        before = self._rect(trigger.locator("xpath=ancestor::div[contains(@class,'metadata-option')]"))
        self._resolve_config(page)
        self._assert_same_rect(before, self._rect(trigger.locator("xpath=ancestor::div[contains(@class,'metadata-option')]")))
        trigger.scroll_into_view_if_needed()
        trigger.click()
        expect(page.locator("#episodeResearchTooltip")).to_be_visible()
        expect(page.locator('[data-key="AGENT_EPISODE_RESEARCH_ENABLED"]')).to_be_disabled()
        expect(page.locator('[data-key="AGENT_NSFW_CLEAN_REVIEW_ENABLED"]')).to_be_disabled()
        self.assertEqual(errors, [])


@unittest.skipUnless(fixture.sync_playwright is not None, "未安装 Playwright")
class SettingsIndexerSiteBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.NsfwCleanReviewSettingsBrowserTests.setUpClass()
        cls.browser = fixture.NsfwCleanReviewSettingsBrowserTests.browser

    @classmethod
    def tearDownClass(cls):
        fixture.NsfwCleanReviewSettingsBrowserTests.tearDownClass()

    _page = fixture.NsfwCleanReviewSettingsBrowserTests._page

    def ready(self, config, width=390):
        page, errors = self._page(width, {
            "DISCOVERY_RESOURCE_RESULTS_ENABLED": "1", "INDEXER_SEARCH_ENABLED": "1", **config,
        })
        fixture.NsfwCleanReviewSettingsBrowserTests._resolve_config(page)
        page.locator('[data-settings-target="discovery"]').click()
        box = page.locator('[data-indexer-site-box]')
        expect(box).to_be_visible()
        return page, errors, box.locator('[data-indexer-site="btbtla"]')

    @staticmethod
    def selected(page):
        return [site for site in page.locator('[data-key="INDEXER_ENABLED_SITES"]').input_value().split(',') if site]

    def test_manual_sites_wrap_and_save_without_agent_only_sources(self):
        default_sites = ['nyaa', 'mikan', 'btbtla', 'tpb']
        labels = ['Nyaa', 'Mikan', '综合', 'The Pirate Bay', 'Sukebei']
        site_order = ['nyaa', 'mikan', 'btbtla', 'tpb', 'sukebei']
        for width in (320, 1280):
            with self.subTest(width=width):
                page, errors, site_toggle = self.ready({'INDEXER_ENABLED_SITES': ','.join(default_sites)}, width)
                self.assertEqual(page.locator('[data-indexer-site-chip] strong').all_text_contents(), labels)
                self.assertEqual(page.locator('[data-indexer-site]').evaluate_all(
                    "inputs => inputs.map(input => input.dataset.indexerSite)"), site_order)
                self.assertEqual(self.selected(page), default_sites)
                expect(page.locator('[data-indexer-site="sukebei"]')).not_to_be_checked()
                geometry = page.locator('[data-indexer-site-selector]').evaluate("""selector => {
                    const bounds = [...selector.children].map(option => option.getBoundingClientRect());
                    const rect = selector.getBoundingClientRect();
                    return {
                        inside: bounds.every(option => option.left >= rect.left - 0.5 && option.right <= rect.right + 0.5),
                        rows: new Set(bounds.map(option => Math.round(option.top))).size,
                        width: rect.width,
                        height: rect.height,
                    };
                }""")
                self.assertTrue(geometry['inside'])
                self.assertGreaterEqual(geometry['rows'], 2 if width == 320 else 1)
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

                before = site_toggle.locator('..').bounding_box()
                site_toggle.locator('..').click()
                remaining = ['nyaa', 'mikan', 'tpb']
                self.assertEqual(self.selected(page), remaining)
                after = site_toggle.locator('..').bounding_box()
                self.assertEqual((before['width'], before['height']), (after['width'], after['height']))
                page.locator('#settings-panel-discovery [data-save-settings]').click()
                page.wait_for_function('window.__settingsWrites.length === 1')
                self.assertEqual(page.evaluate('window.__settingsWrites[0].INDEXER_ENABLED_SITES'), ','.join(remaining))
                page.evaluate('window.__resolveSettingsSave()')
                expect(page.locator('#settings-panel-discovery [data-save-settings]')).to_be_enabled()
                self.assertEqual(errors, [])
                page.close()

    def test_api_site_list_is_authoritative_and_legacy_sensitive_flag_is_ignored(self):
        page, errors, btbtla = self.ready({
            'INDEXER_ENABLED_SITES': 'mikan',
            'INDEXER_SUKEBEI_ENABLED': '1',
        })
        self.assertEqual(self.selected(page), ['mikan'])
        self.assertFalse(btbtla.is_checked())
        expect(page.locator('[data-indexer-site="mikan"]')).to_be_checked()
        expect(page.locator('[data-indexer-site="sukebei"]')).not_to_be_checked()
        self.assertNotIn('INDEXER_ENABLED_SITES', page.evaluate("collectConfigFields(document.getElementById('settings-panel-discovery'))"))
        self.assertEqual(errors, [])
        page.close()

    def test_root_toggle_does_not_change_selected_sites_or_btbtla(self):
        for sites in ('nyaa', 'nyaa,btbtla'):
            with self.subTest(sites=sites):
                page, errors, site_toggle = self.ready({'INDEXER_ENABLED_SITES': sites})
                if 'btbtla' in sites:
                    expect(site_toggle).to_be_checked()
                else:
                    expect(site_toggle).not_to_be_checked()
                for key in ('INDEXER_SEARCH_ENABLED', 'DISCOVERY_RESOURCE_RESULTS_ENABLED'):
                    toggle = page.locator(f'[data-key="{key}"]')
                    toggle.locator('..').click()
                    expect(site_toggle).to_be_disabled()
                    self.assertEqual(self.selected(page), [site for site in sites.split(',') if site])
                    toggle.locator('..').click()
                    expect(site_toggle).to_be_enabled()
                    self.assertEqual(self.selected(page), [site for site in sites.split(',') if site])
                self.assertFalse(site_toggle.evaluate('el => el.indeterminate'))
                self.assertEqual(errors, [])
                page.close()
