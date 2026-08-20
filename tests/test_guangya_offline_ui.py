"""光鸭离线转存协议卡片前端布局契约。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app/templates/guangya_offline.html"
BASE_TEMPLATE = ROOT / "app/templates/base.html"
STYLES = ROOT / "app/static/css/main.css"


class GuangyaOfflineUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = TEMPLATE.read_text(encoding="utf-8")
        self.base_template = BASE_TEMPLATE.read_text(encoding="utf-8")
        self.styles = STYLES.read_text(encoding="utf-8")

    def test_protocol_cards_place_ed2k_before_http_and_fallback_on_second_row(self):
        keys = (
            "OFFLINE_MAGNET_ENABLED",
            "OFFLINE_ED2K_ENABLED",
            "OFFLINE_HTTP_ENABLED",
            "OFFLINE_MAGNET_UNVERIFIED_FALLBACK",
        )
        positions = [self.template.index(f'data-key="{key}"') for key in keys]
        self.assertEqual(positions, sorted(positions))

    def test_protocol_cards_use_isolated_layout_instead_of_conflicting_strm_card(self):
        start = self.template.index('<div class="offline-protocol-grid">')
        end = self.template.index('<div class="form-row offline-textarea-row">', start)
        protocol_markup = self.template[start:end]
        self.assertNotIn("strm-toggle-card", protocol_markup)
        fallback_start = protocol_markup.index(
            'class="offline-protocol-card offline-protocol-card--fallback organize-toggle-tile"'
        )
        fallback_markup = protocol_markup[fallback_start:]
        self.assertNotIn('data-lucide="shield-check"', fallback_markup)
        for contract in (
            'class="offline-protocol-card organize-toggle-tile"',
            'class="offline-protocol-card offline-protocol-card--fallback organize-toggle-tile"',
            'class="offline-protocol-copy"',
            'class="offline-protocol-hint"',
            'class="offline-protocol-copy offline-protocol-fallback-copy"',
            'for="offlineHttpEnabled"',
            'for="offlineMagnetFallback"',
            'id="offlineMagnetFallback" type="checkbox" data-key="OFFLINE_MAGNET_UNVERIFIED_FALLBACK" checked',
            '磁力解析失败自动隔离',
            '默认开启，可手动关闭',
        ):
            self.assertIn(contract, protocol_markup)

    def test_protocol_layout_reserves_copy_width_without_equal_height_stretch(self):
        for contract in (
            ".offline-protocol-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); align-items: start;",
            ".offline-settings-section .offline-protocol-card { min-width: 0; min-height: 72px; height: auto;",
            ".offline-protocol-card .offline-protocol-title { min-width: 0; flex: 1 1 auto;",
            ".offline-protocol-copy { min-width: 0; flex: 1 1 auto; }",
            ".offline-protocol-hint { margin-top: 4px;",
            ".offline-settings-section .offline-protocol-card--fallback { grid-column: 1 / -1; min-height: 78px; display: flex; flex-direction: row;",
            ".offline-protocol-card--fallback .offline-protocol-copy > label { color: var(--text-primary); font-size: 13.5px; font-weight: 700;",
            ".offline-settings-section .offline-protocol-card .toggle { margin-left: 12px; flex: 0 0 auto; }",
        ):
            self.assertIn(contract, self.styles)
        self.assertRegex(
            self.styles,
            re.compile(r"@media \(max-width: 900px\) \{\s+\.offline-protocol-grid,", re.S),
        )

    def test_main_stylesheet_cache_key_includes_offline_protocol_release(self):
        match = re.search(r"css/main\.css'\) }}\?v=(\d{8}[a-z])", self.base_template)
        self.assertIsNotNone(match, "main.css 应带静态资源缓存版本")
        self.assertGreaterEqual(match.group(1), "20260809i")


if __name__ == "__main__":
    unittest.main()
