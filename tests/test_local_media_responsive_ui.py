import unittest
from pathlib import Path
import re


class LocalMediaResponsiveUITests(unittest.TestCase):
    def test_organize_log_controls_detail_and_mobile_selection_contract(self):
        template = Path("app/templates/local_media.html").read_text(encoding="utf-8")
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")
        css = Path("app/static/css/local-media.css").read_text(encoding="utf-8")

        for marker in (
            'id="lmTaskSearch"', 'id="lmTaskSelectAll"', 'id="lmClearTasksBtn"',
            'id="lmTaskSelectionCount"', 'id="lmTaskDetailModal"',
        ):
            self.assertIn(marker, template)
        self.assertIn("全选当前筛选结果中可清除的整理日志", template)
        self.assertIn("不会删除、移动或修改任何媒体文件", js)
        self.assertIn("filteredTasks().filter((task) => task.clearable)", js)
        self.assertIn("new Promise((resolve) => window.setTimeout(resolve, 320))", js)
        self.assertIn("data-task-select", js)
        self.assertIn("data-task-detail", js)
        self.assertIn(".lm-task-table td.lm-task-select-cell", css)
        self.assertIn(".lm-task-detail-grid", css)

    def test_mobile_tab_actions_stay_scoped_and_source_add_button_is_not_duplicated(self):
        template = Path("app/templates/local_media.html").read_text(encoding="utf-8")
        css = Path("app/static/css/local-media.css").read_text(encoding="utf-8")

        self.assertIn('.lm-tab-action-group[hidden] { display: none !important; }', css)
        self.assertIn('.lm-tab-action-group[data-tab-action="tasks"] { display: grid;', css)
        self.assertIn('html[data-local-media-initial-tab="sources"] .lm-tab-action-group[data-tab-action="sources"],', css)
        self.assertIn('.lm-tab-action-group[data-tab-action="sources"] { display: none !important; }', css)
        self.assertIn('.lm-panel-subhead { flex-direction: column; align-items: stretch;', css)
        self.assertIn('id="lmAddSourceBtn"', template)
        self.assertIn('id="lmAddSourceMobileBtn"', template)
        self.assertEqual(template.count('新增来源</span>'), 2)

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
        self.assertIn("const firstLoad = !hasLoadedLocalMedia", js)
        self.assertIn("const animate = firstLoad || currentManual", js)
        self.assertIn("switchTab(resolveTargetTab(), false)", js)

        self.assertRegex(css, r"\.lm-source-grid\s*\{[^}]*min-height:\s*220px")
        self.assertRegex(css, r"\.lm-review-list\s*\{[^}]*min-height:\s*220px")
        self.assertIn(".lm-source-card,", css)
        self.assertIn(".lm-review-item,", css)
        self.assertIn("?v=20260821a", template)

    def test_all_initial_tab_loading_hints_are_delayed_and_have_minimum_visible_time(self):
        template = Path("app/templates/local_media.html").read_text(encoding="utf-8")
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")
        css = Path("app/static/css/local-media.css").read_text(encoding="utf-8")

        self.assertEqual(template.count("lm-initial-loading"), 4)
        self.assertEqual(template.count('lm-initial-loading" aria-hidden="true"'), 4)
        for text in (
            "正在读取整理日志…", "正在读取来源配置…",
            "正在读取待确认任务…", "正在读取本地媒体条目…",
        ):
            self.assertIn(text, template)
        self.assertIn("const INITIAL_LOADING_DELAY_MS = 400", js)
        self.assertIn("const INITIAL_LOADING_MIN_MS = 320", js)
        self.assertIn("function armInitialLoading()", js)
        self.assertIn("async function settleInitialLoading()", js)
        self.assertIn("await settleInitialLoading()", js)
        self.assertIn(".lm-tab-panel.active .lm-initial-loading", js)
        self.assertIn("armInitialLoading();", js)
        self.assertIn(".lm-initial-loading", css)
        self.assertIn("opacity: 0", css)
        self.assertIn(".lm-initial-loading.is-visible", css)
        self.assertIn(".lm-initial-loading svg", css)
        self.assertIn("?v=20260825a", template)

    def test_initial_hash_tab_is_prepainted_before_page_script_loads(self):
        template = Path("app/templates/local_media.html").read_text(encoding="utf-8")
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")
        css = Path("app/static/css/local-media.css").read_text(encoding="utf-8")

        self.assertIn("document.documentElement.dataset.localMediaInitialTab = target", template)
        self.assertIn("const target = hashTab || params.get('tab') || params.get('view')", template)
        self.assertIn("['sources', 'review', 'manual'].includes(target)", template)
        self.assertIn('html[data-local-media-initial-tab] .lm-tab-panel[data-tab-panel="tasks"]', css)
        for tab in ("sources", "review", "manual"):
            self.assertIn(f'html[data-local-media-initial-tab="{tab}"] .lm-tab-panel[data-tab-panel="{tab}"]', css)
            self.assertIn(f'html[data-local-media-initial-tab="{tab}"] .lm-tab-btn[data-tab-target="{tab}"]', css)
        self.assertIn("delete document.documentElement.dataset.localMediaInitialTab", js)

    def test_select_all_checkbox_avoids_native_first_paint_flash(self):
        template = Path("app/templates/local_media.html").read_text(encoding="utf-8")
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")
        css = Path("app/static/css/local-media.css").read_text(encoding="utf-8")

        self.assertIn('id="lmTaskSelectAllLabel" hidden', template)
        self.assertIn("$('lmTaskSelectAllLabel').hidden = false", js)
        self.assertIn("appearance: none", css)
        self.assertIn("-webkit-appearance: none", css)
        self.assertIn(".lm-task-select-all[hidden]", css)
        self.assertIn(".lm-task-select-all input:focus-visible", css)
        self.assertIn("box-shadow: 0 0 0 3px var(--accent-soft)", css)
        self.assertIn(".lm-task-table th:nth-child(1) { width: 96px; }", css)
        self.assertRegex(
            css,
            r"@media\s*\(max-width:\s*760px\)\s*\{[\s\S]*?\.lm-task-table thead\s*\{[^}]*min-height:\s*44px[^}]*margin-bottom:\s*8px",
        )
        self.assertIn(".lm-task-table thead th.lm-task-select-cell", css)
        self.assertIn('.lm-task-table thead .lm-task-select-all::after', css)
        self.assertIn('content: "当前筛选结果"', css)
        self.assertIn("text-align: left !important", css)
        self.assertIn("grid-template-columns: 16px max-content", css)
        self.assertIn("var(--text-muted) 62%", css)


    def test_manual_workspace_uses_configured_item_list_context_menu_and_scrape_modal(self):
        template = Path("app/templates/local_media.html").read_text(encoding="utf-8")
        shared_modal = Path("app/templates/_media_scrape_modal.html").read_text(encoding="utf-8")
        css = Path("app/static/css/local-media.css").read_text(encoding="utf-8")
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")

        for contract in (
            "LOCAL MEDIA INBOX",
            'id="lmMediaItems"',
            'id="lmMediaPathbar"',
            'id="lmMediaBrowseHome"',
            'id="lmMediaBreadcrumb"',
            'id="lmMediaBrowseUp"',
            'id="lmItemContextMenu"',
            '{% include "_media_scrape_modal.html" %}',
            '"source": "local"',
            'data-item-action="search"',
            'data-item-action="auto"',
            'data-item-action="delete"',
        ):
            self.assertIn(contract, template)
        self.assertIn('class="app-modal {{ media_scrape.css }}-modal"', shared_modal)
        self.assertIn('data-media-scrape-role="season"', shared_modal)
        self.assertIn('data-media-scrape-role="episode"', shared_modal)
        for removed in ('id="lmManualSource"', 'id="lmManualPath"', 'id="lmPickManualPathBtn"'):
            self.assertNotIn(removed, template)
        self.assertIn("grid-template-columns: minmax(340px, 35%) minmax(0, 1fr);", css)
        self.assertIn(".lm-item-context-menu", css)
        self.assertNotRegex(css, r"\.lm-media-row:hover\s*\{[^}]*transform")
        self.assertIn("/api/local-media/items", js)
        self.assertIn("/api/local-media/items/delete", js)
        self.assertIn("function openScrapeForItem", js)
        self.assertIn("function loadMediaDirectory", js)
        self.assertIn("data-open-directory", js)
        self.assertIn("lockElementHeight(list)", js)
        self.assertIn(".lm-media-pathbar", css)
        self.assertIn(".lm-media-list.is-navigating", css)
        self.assertNotIn("正在读取目录", js)
        for contract in (
            "function splitPathSegments",
            "function splitPlanTargetPath",
            "function commonPathPrefix",
            "function configuredPlanRoot",
            "function buildPlanTree",
            "function renderPlanTreeNode",
            "function renderPlanTree",
            'class="lm-plan-tree"',
            'class="lm-plan-timeline"',
            'class="lm-plan-timeline-directory"',
            'class="lm-plan-timeline-file',
            'class="lm-plan-tree-source"',
            "目标根目录",
        ):
            self.assertIn(contract, js)
        for selector in (
            ".lm-plan-tree", ".lm-plan-timeline::before",
            ".lm-plan-timeline-file", ".lm-plan-tree-source code",
        ):
            self.assertIn(selector, css)
        self.assertNotIn(".lm-plan-tree-branch > ul::before", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertNotIn("${esc(item.source_path)} → ${esc(item.target_name)}", js)

    def test_task_detail_uses_sorted_ordinal_timeline_and_stable_modal_layout(self):
        template = Path("app/templates/local_media.html").read_text(encoding="utf-8")
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")
        css = Path("app/static/css/local-media.css").read_text(encoding="utf-8")

        self.assertIn('id="lmTaskDetailModal"', template)
        self.assertIn("steps.map((step, ordinal)", js)
        self.assertIn('${ordinal + 1}</span>', js)
        self.assertNotIn("Number(step.step_index) + 1", js)
        self.assertIn(".lm-task-step-list::before", css)
        self.assertIn(".lm-task-step > div > span", css)
        self.assertIn(".lm-task-step.is-completed .lm-task-step-index", css)
        self.assertIn("function recognitionDisplay(task, recognition)", js)
        self.assertIn("history_inferred: '历史目标路径推断'", js)
        self.assertIn("taskDetailField('季 / 集', display.position)", js)
        self.assertIn(".lm-task-detail-origin", css)
        self.assertRegex(css, r"\.lm-task-detail-dialog\s*\{[^}]*padding:\s*0[^}]*gap:\s*0")
        self.assertRegex(css, r"\.lm-task-detail-body\s*\{[^}]*min-height:\s*0[^}]*flex:\s*1")
        self.assertRegex(css, r"\.lm-dialog-actions\s*\{[^}]*display:\s*flex[^}]*justify-content:\s*flex-end")

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
