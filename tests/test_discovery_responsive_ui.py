import unittest
from pathlib import Path
import re


class DiscoveryResponsiveUITests(unittest.TestCase):
    def test_discovery_responsive_toolbar_and_dialog_css_contract(self):
        css_path = Path("app/static/css/main.css")
        self.assertTrue(css_path.is_file(), "main.css 样式表文件必须存在")
        css = css_path.read_text(encoding="utf-8")

        # 1. 验证全局与移动端 dialog 动态视口高度（dvh / svh 兼容与安全区域适配）
        self.assertTrue(
            "height: min(820px,calc(100dvh - 28px))" in css
            or "height: min(820px, calc(100dvh - 28px))" in css
        )
        self.assertTrue(
            "max-height: min(820px,calc(100dvh - 28px))" in css
            or "max-height: min(820px, calc(100dvh - 28px))" in css
        )

        # 2. 验证 @media (max-width: 1160px) 平板与中等屏幕规则
        # 探索工具栏在平板/中等屏幕第一行为搜索输入框 + 状态指示灯与刷新按钮；第二行为筛选器
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*1160px\)\s*\{[\s\S]*?\.discovery-toolbar\s*\{[^}]*grid-template-columns:\s*minmax\(0,1fr\)\s+auto",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*1160px\)\s*\{[\s\S]*?\.discovery-search-form\s*\{[^}]*grid-column:\s*1[^}]*grid-row:\s*1",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*1160px\)\s*\{[\s\S]*?\.discovery-toolbar-meta\s*\{[^}]*grid-column:\s*2[^}]*grid-row:\s*1",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*1160px\)\s*\{[\s\S]*?\.discovery-filter-region\s*\{[^}]*grid-column:\s*1\s*/\s*-1[^}]*grid-row:\s*2",
                css,
            )
        )

        # 3. 验证 @media (max-width: 900px) 移动端规则
        # 探索工具栏在移动端第一行为搜索输入框 + 状态指示灯与刷新按钮；第二行为筛选器（若有）
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*900px\)\s*\{[\s\S]*?\.discovery-toolbar\s*\{[^}]*grid-template-columns:\s*minmax\(0,1fr\)\s+auto",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*900px\)\s*\{[\s\S]*?\.discovery-search-form\s*\{[^}]*grid-column:\s*1[^}]*grid-row:\s*1",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*900px\)\s*\{[\s\S]*?\.discovery-toolbar-meta\s*\{[^}]*grid-column:\s*2[^}]*grid-row:\s*1",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*900px\)\s*\{[\s\S]*?\.discovery-filter-region\s*\{[^}]*grid-column:\s*1\s*/\s*-1[^}]*grid-row:\s*2",
                css,
            )
        )

        # 4. 验证 @media (max-width: 560px) 紧凑移动端规则
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*560px\)\s*\{[\s\S]*?\.discovery-toolbar\s*\{[^}]*grid-template-columns:\s*minmax\(0,1fr\)\s+auto",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*560px\)\s*\{[\s\S]*?\.discovery-search-form\s*\{[^}]*grid-column:\s*1[^}]*grid-row:\s*1",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*560px\)\s*\{[\s\S]*?\.discovery-toolbar-meta\s*\{[^}]*grid-column:\s*2[^}]*grid-row:\s*1",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*560px\)\s*\{[\s\S]*?\.discovery-filter-region\s*\{[^}]*grid-column:\s*1\s*/\s*-1[^}]*grid-row:\s*2",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*560px\)\s*\{[\s\S]*?\.discovery-filter-control\s*\{[^}]*flex:\s*1\s+1\s+0",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*560px\)\s*\{[\s\S]*?\.discovery-dialog\s*\{[^}]*100dvh",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*560px\)\s*\{[\s\S]*?\.discovery-resource-bulk\s*\{[^}]*env\(safe-area-inset-bottom",
                css,
            )
        )

    def test_discovery_js_tab_switch_restores_cached_view_before_cold_cleanup(self):
        js_path = Path("app/static/js/discovery.js")
        self.assertTrue(js_path.is_file(), "discovery.js 文件必须存在")
        js = js_path.read_text(encoding="utf-8")
        match = re.search(
            r"function activateTab\(button\)\s*\{(?P<body>[\s\S]*?)"
            r"\n    \}\n\n    elements\.tabs\.addEventListener\('click'",
            js,
        )
        self.assertIsNotNone(match)
        body = match.group("body")

        self.assertIn("saveCurrentViewSnapshot();", body)
        self.assertIn("const cachedSnapshot = cachedKey ? restoreCachedView(cachedKey) : null;", body)
        self.assertIn("snapshotIsFresh(cachedSnapshot)", body)
        self.assertIn("loadActive({preserveContent: true})", body)
        self.assertLess(body.index("restoreCachedView"), body.index("state.filters = {}"))
        self.assertLess(body.index("if (cachedSnapshot)"), body.index("activeTarget.replaceChildren()"))


if __name__ == "__main__":
    unittest.main()
