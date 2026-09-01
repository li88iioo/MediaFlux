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

    def test_protocol_cards_place_ed2k_before_http_and_remove_whole_magnet_fallback(self):
        keys = ("OFFLINE_MAGNET_ENABLED", "OFFLINE_ED2K_ENABLED", "OFFLINE_HTTP_ENABLED")
        positions = [self.template.index(f'data-key="{key}"') for key in keys]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("OFFLINE_MAGNET_UNVERIFIED_FALLBACK", self.template)
        self.assertNotIn("磁力解析失败自动隔离", self.template)

    def test_protocol_cards_keep_only_the_three_protocol_controls(self):
        start = self.template.index('<div class="offline-protocol-grid">')
        end = self.template.index('<!-- 2. 默认保存目录 -->', start)
        protocol_markup = self.template[start:end]
        self.assertNotIn("strm-toggle-card", protocol_markup)
        self.assertEqual(
            protocol_markup.count('class="offline-protocol-card organize-toggle-tile"'),
            3,
        )
        for contract in (
            'class="offline-protocol-copy"',
            'for="offlineMagnetEnabled"',
            'for="offlineEd2kEnabled"',
            'for="offlineHttpEnabled"',
        ):
            self.assertIn(contract, protocol_markup)
        for removed in (
            "offline-protocol-card--policy",
            "offline-protocol-policy-copy",
            "offline-protocol-policy-badge",
            'data-lucide="file-video-2"',
            "仅视频提交",
            "安全模式",
        ):
            self.assertNotIn(removed, protocol_markup)

    def test_protocol_layout_reserves_copy_width_without_equal_height_stretch(self):
        for contract in (
            ".offline-protocol-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); align-items: start;",
            ".offline-settings-section .offline-protocol-card { min-width: 0; min-height: 72px; height: auto;",
            ".offline-protocol-card .offline-protocol-title { min-width: 0; flex: 1 1 auto;",
            ".offline-protocol-copy { min-width: 0; flex: 1 1 auto; }",
            ".offline-protocol-hint { margin-top: 4px;",
            ".offline-settings-section .offline-protocol-card .toggle { margin-left: 12px; flex: 0 0 auto; }",
        ):
            self.assertIn(contract, self.styles)
        self.assertRegex(
            self.styles,
            re.compile(r"@media \(max-width: 900px\) \{\s+\.offline-protocol-grid,", re.S),
        )
        self.assertNotIn("offline-protocol-card--policy", self.styles)
        self.assertNotIn("offline-protocol-policy-badge", self.styles)

    def test_main_stylesheet_uses_content_hash_authority(self):
        self.assertIn("static_url('css/main.css')", self.base_template)
        self.assertNotRegex(self.base_template, r"css/main\.css[^\n]*\?v=20\d{6}[a-z]")


if __name__ == "__main__":
    unittest.main()
