import unittest
from pathlib import Path
import re


class LocalMediaResponsiveUITests(unittest.TestCase):
    def test_local_media_number_only_motion_contract(self):
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")
        css = Path("app/static/css/local-media.css").read_text(encoding="utf-8")
        template = Path("app/templates/local_media.html").read_text(encoding="utf-8")

        self.assertIn("window.MFAnim.countUp", js)
        self.assertNotIn("window.MFAnim.staggerIn", js)
        self.assertNotIn("window.MFAnim.crossfade", js)
        self.assertIn("function lockElementHeight(element)", js)
        self.assertIn("requestAnimationFrame(() => window.requestAnimationFrame", js)
        self.assertIn("renderSources(false, animate)", js)
        self.assertIn("renderTasks(false, animate)", js)
        self.assertIn("renderMediaItems(false, animate)", js)
        self.assertIn("const animate = !hasLoadedLocalMedia || currentManual", js)
        self.assertIn("switchTab(resolveTargetTab(), false)", js)

        self.assertRegex(css, r"\.lm-source-grid\s*\{[^}]*min-height:\s*220px")
        self.assertRegex(css, r"\.lm-review-list\s*\{[^}]*min-height:\s*220px")
        self.assertIn(".lm-source-card,", css)
        self.assertIn(".lm-review-item,", css)
        self.assertIn("?v=20260820c", template)


    def test_manual_workspace_uses_configured_item_list_context_menu_and_scrape_modal(self):
        template = Path("app/templates/local_media.html").read_text(encoding="utf-8")
        css = Path("app/static/css/local-media.css").read_text(encoding="utf-8")
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")

        for contract in (
            "LOCAL MEDIA INBOX",
            'id="lmMediaItems"',
            'id="lmItemContextMenu"',
            'class="app-modal lm-scrape-modal"',
            'data-item-action="search"',
            'data-item-action="auto"',
            'data-item-action="delete"',
        ):
            self.assertIn(contract, template)
        for removed in ('id="lmManualSource"', 'id="lmManualPath"', 'id="lmPickManualPathBtn"'):
            self.assertNotIn(removed, template)
        self.assertIn("grid-template-columns: minmax(340px, 35%) minmax(0, 1fr);", css)
        self.assertIn(".lm-item-context-menu", css)
        self.assertNotRegex(css, r"\.lm-media-row:hover\s*\{[^}]*transform")
        self.assertIn("/api/local-media/items", js)
        self.assertIn("/api/local-media/items/delete", js)
        self.assertIn("function openScrapeForItem", js)

    def test_local_media_empty_state_unified_monochrome_icons_contract(self):
        css_path = Path("app/static/css/local-media.css")
        self.assertTrue(css_path.is_file(), "local-media.css 必须存在")
        css = css_path.read_text(encoding="utf-8")

        # 1. 验证空状态图标规范：统一使用无底圈、无彩色实体边框的柔和灰阶设计
        self.assertIn(".lm-empty-icon", css)
        self.assertIn(".lm-empty-state", css)
        self.assertIn(".lm-table-empty-wrap", css)

        # 2. 验证 .lm-empty-icon 取消了实体圆圈背景与边框
        self.assertIsNotNone(
            re.search(
                r"\.lm-empty-icon\s*\{[^}]*background:\s*transparent[^}]*border:\s*0",
                css,
            )
        )

        # 3. 验证图标尺寸统一为 26px，透明度 opacity: 0.6
        self.assertIsNotNone(
            re.search(
                r"\.lm-empty-icon\s+svg\s*\{[^}]*width:\s*26px[^}]*height:\s*26px[^}]*opacity:\s*0\.6",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"\.lm-scrape-placeholder\s+svg\s*\{[^}]*width:\s*26px[^}]*height:\s*26px[^}]*opacity:\s*\.6",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"\.lm-table-empty-wrap\s+svg\s*\{[^}]*width:\s*26px[^}]*height:\s*26px[^}]*opacity:\s*0\.6",
                css,
            )
        )

        # 4. 验证 is-success 空状态同样对齐柔和灰阶规范
        self.assertIsNotNone(
            re.search(
                r"\.lm-empty-icon\.is-success\s+svg\s*\{[^}]*color:\s*var\(--text-muted\)[^}]*opacity:\s*0\.6",
                css,
            )
        )

    def test_local_media_mobile_empty_table_centering_contract(self):
        css_path = Path("app/static/css/local-media.css")
        css = css_path.read_text(encoding="utf-8")

        # 验证 @media (max-width: 760px) 中对表格空状态行的重置与居中
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*760px\)\s*\{[\s\S]*?\.lm-task-table\s+tr\.is-empty-row,[\s\S]*?\.lm-task-table\s+tr:has\(\.table-empty\)\s*\{[^}]*display:\s*flex[^}]*justify-content:\s*center",
                css,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"@media\s*\(max-width:\s*760px\)\s*\{[\s\S]*?\.lm-task-table\s+td\.table-empty\s*\{[^}]*display:\s*flex[^}]*flex-direction:\s*column[^}]*align-items:\s*center[^}]*justify-content:\s*center",
                css,
            )
        )


if __name__ == "__main__":
    unittest.main()
