"""光鸭目录刮削页面结构与直接删除接线测试。"""
from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support import InitializedWebTestCase

from app.config import web_credentials
from app.main import create_app

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 由显式 system python3 门禁执行
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app/static/js/app.js").read_text("utf-8")
APP_MODAL_SCRIPT = APP_SOURCE[
    APP_SOURCE.index("    const modalStack = [];"):
    APP_SOURCE.index("\n\n    const confirmModal")
]
DIRECTORY_SCRIPT = ROOT / "app/static/js/guangya-directory-scrape.js"
POSITION_SCRIPT = ROOT / "app/static/js/media-scrape-position.js"
DIRECTORY_CSS = ROOT / "app/static/css/guangya-directory-scrape.css"
DIRECT_DELETE_HARNESS = textwrap.dedent(
    """
    <!doctype html>
    <meta charset="utf-8">
    <div id="gyDirectoryContextMenu" role="menu" hidden>
        <button type="button" role="menuitem" data-scrape-action="manual">manual</button>
        <button type="button" role="menuitem" data-scrape-action="auto">auto</button>
        <button type="button" role="menuitem" data-browser-action="delete-item" hidden>delete</button>
    </div>
    <div id="gyScrapeModal" hidden>
        <button id="gyScrapeCloseBtn"></button>
        <button id="gyScrapeCancelBtn"></button>
        <button id="gyScrapeRunBtn"><span></span></button>
        <button id="gyScrapeSearchBtn"><span></span></button>
        <button id="gyScrapeExternalBtn"><span></span></button>
        <input id="gyScrapeQuery">
        <select id="gyScrapeType">
            <option value="auto">auto</option>
            <option value="movie">movie</option>
            <option value="tv">tv</option>
        </select>
        <div id="gyScrapeEpisodeFields" hidden>
            <input id="gyScrapeSeason" type="number">
            <input id="gyScrapeEpisode" type="number">
        </div>
        <div id="gyScrapeDirectory"></div>
        <div id="gyScrapeCandidates"></div>
        <div id="gyScrapeExternalHints" hidden></div>
        <div id="gyScrapeCandidateCount"></div>
        <div id="gyScrapeDetail"></div>
        <div id="gyScrapeStatus"></div>
        <div id="gyScrapeArchiveTarget"></div>
        <div id="gyScrapePlanSummary"></div>
    </div>
    """
)


class GuangYaDirectoryScrapeUiTests(InitializedWebTestCase):
    def setUp(self):
        self.client = TestClient(create_app(start_background=False))
        login = self.client.get("/login")
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self._csrf(login.text),
                "username": username,
                "password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.client.close()

    @staticmethod
    def _csrf(html: str) -> str:
        match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', html)
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def test_guangya_page_renders_scrape_dialog_and_page_assets(self):
        response = self.client.get("/guangya")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="gyScrapeModal"', response.text)
        self.assertIn('id="gyScrapeCandidates"', response.text)
        self.assertIn('id="gyScrapeDetail"', response.text)
        self.assertIn('id="gyScrapeRunBtn"', response.text)
        self.assertIn('id="gyScrapeEpisodeFields"', response.text)
        self.assertIn('id="gyScrapeNumberingField"', response.text)
        self.assertIn('id="gyScrapeNumbering"', response.text)
        self.assertIn('value="season_continuous"', response.text)
        self.assertIn('id="gyScrapeSeasonField"', response.text)
        self.assertIn('id="gyScrapeEpisodeField"', response.text)
        self.assertIn('id="gyScrapeSeason"', response.text)
        self.assertIn('id="gyScrapeEpisode"', response.text)
        self.assertIn('id="gyScrapeInspection"', response.text)
        self.assertIn('id="gyScrapeInspectionSummary"', response.text)
        self.assertIn('id="gyScrapeInspectionHint"', response.text)
        self.assertIn('id="gyScrapeExternalBtn"', response.text)
        self.assertIn('id="gyScrapeExternalHints"', response.text)
        self.assertIn('豆瓣 / BGM 线索', response.text)
        self.assertIn('data-scrape-mobile-pane="candidates"', response.text)
        self.assertIn('data-scrape-mobile-pane="preview"', response.text)
        self.assertLess(
            response.text.index('class="gy-scrape-workspace"'),
            response.text.index('class="gy-scrape-footer"'),
        )
        self.assertLess(
            response.text.index('class="gy-scrape-footer"'),
            response.text.index('id="gyScrapeInspection"'),
        )
        self.assertLess(
            response.text.index('id="gyScrapeInspection"'),
            response.text.index('class="gy-scrape-footer-actions"'),
        )
        self.assertIn('class="gy-scrape-footer-context"', response.text)
        self.assertNotIn('id="gyScrapeTitle"', response.text)
        self.assertIn('aria-label="搜索并刮削所选媒体"', response.text)
        self.assertIn('id="gySortSelect"', response.text)
        for value in (
            "name_asc", "name_desc", "created_desc", "created_asc",
            "updated_desc", "updated_asc", "type_asc", "size_desc", "size_asc",
        ):
            self.assertIn(f'value="{value}"', response.text)
        self.assertIn("guangya-directory-scrape.css?v=20260828c", response.text)
        self.assertIn("media-scrape-position.js?v=20260828a", response.text)
        self.assertIn("guangya-directory-scrape.js?v=20260829a", response.text)
        self.assertIn('aria-label="列表视图" aria-pressed="true"', response.text)
        self.assertIn('aria-label="网格视图" aria-pressed="false"', response.text)
        self.assertNotIn('title="列表视图"', response.text)
        self.assertNotIn('title="网格视图"', response.text)
        self.assertIn("dirList.classList.toggle('is-list'", response.text)
        self.assertIn("dirList.classList.toggle('is-grid'", response.text)
        self.assertIn("dirList.classList.add('is-view-switching')", response.text)
        self.assertIn("void dirList.offsetWidth", response.text)
        self.assertIn("dirList.classList.remove('is-view-switching')", response.text)
        self.assertNotIn("dirList.className =", response.text)

    def test_large_directories_render_in_bounded_batches(self):
        response = self.client.get("/guangya")

        self.assertEqual(response.status_code, 200)
        self.assertIn("const GY_DIR_RENDER_BATCH_SIZE = 200", response.text)
        self.assertIn("function gyRenderNextBatch(version)", response.text)
        self.assertIn("document.createDocumentFragment()", response.text)
        self.assertIn("gyDirList.querySelector('.gy-dir-load-more')?.remove()", response.text)
        self.assertIn("继续加载（剩余 ${remaining.toLocaleString('zh-CN')} 项）", response.text)
        self.assertNotIn("dirs.forEach(item=>gyDirList.appendChild", response.text)
        self.assertNotIn("files.forEach(item=>gyDirList.appendChild", response.text)

    def test_cloud_file_rows_keep_the_complete_original_filename(self):
        response = self.client.get("/guangya")

        self.assertEqual(response.status_code, 200)
        self.assertIn("const parsed = isDirectory", response.text)
        self.assertIn("title: String(item.name || '')", response.text)
        self.assertIn("tags: []", response.text)

    def test_directory_cards_use_semantic_tags_without_dropping_unknown_brackets(self):
        if not shutil.which("node"):
            self.skipTest("node 不可用")
        html = self.client.get("/guangya").text
        match = re.search(
            r"function parseGuangYaDirName\(rawName\) \{([\s\S]*?)\n\}\n\nconst gyNameCollator",
            html,
        )
        self.assertIsNotNone(match)
        node_script = textwrap.dedent(
            f"""
            function parseGuangYaDirName(rawName) {{{match.group(1)}
            }}
            const samples = [
                '[H-Enc] Isekai Maou to Shoukan Shoujo no Dorei Majutsu (BDRip 1080p HEVC FLAC)',
                '[KTXP][Isekai_Maou_to_Shoukan_Shoujo_no_Dorei_Majutsu][01-10][GB][1080p][BDrip][HEVC]',
                'Movie [1080p] [导演剪辑版]',
            ];
            process.stdout.write(JSON.stringify(samples.map(parseGuangYaDirName)));
            """
        )
        completed = subprocess.run(
            ["node", "-e", node_script],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        parsed = __import__("json").loads(completed.stdout)
        self.assertEqual(
            parsed[0]["title"],
            "Isekai Maou to Shoukan Shoujo no Dorei Majutsu",
        )
        self.assertEqual(
            [(tag["type"], tag["text"]) for tag in parsed[0]["tags"]],
            [
                ("group", "H-Enc"),
                ("resolution", "1080P"),
                ("source", "BDRip"),
                ("video", "HEVC"),
                ("audio", "FLAC"),
            ],
        )
        self.assertEqual(
            parsed[1]["title"],
            "Isekai Maou to Shoukan Shoujo no Dorei Majutsu",
        )
        self.assertIn({"type": "season", "text": "01-10"}, parsed[1]["tags"])
        self.assertEqual(parsed[2]["title"], "Movie [导演剪辑版]")
        self.assertEqual(parsed[2]["tags"], [{"type": "resolution", "text": "1080P"}])

        self.assertIn("const GY_DIR_VISIBLE_TAG_LIMIT = 3", html)
        self.assertIn("more.textContent = `+${hiddenTags.length}`", html)
        self.assertIn("more.title = hiddenTags.map(tag => tag.text).join(' · ')", html)

    def test_directory_grid_cards_keep_a_stable_tag_budget(self):
        css = DIRECTORY_CSS.read_text("utf-8")

        self.assertRegex(
            css,
            r"#gyDirList\.is-grid \.gy-dir-row\s*\{[^}]*height:\s*168px;[^}]*min-height:\s*168px;",
        )
        self.assertRegex(
            css,
            r"\.gy-dir-tags\s*\{[^}]*flex-wrap:\s*nowrap;[^}]*min-height:\s*18px;[^}]*overflow:\s*hidden;",
        )
        self.assertRegex(
            css,
            r"#gyDirList\.is-grid \.gy-dir-name\s*\{[^}]*-webkit-line-clamp:\s*2;[^}]*min-height:\s*34px;[^}]*max-height:\s*34px;",
        )
        self.assertIn(".gy-dir-tag-badge.tag-more", css)
        self.assertIn("#gyDirList.is-list .gy-dir-tags.is-empty", css)
        self.assertRegex(
            css,
            r"#gyDirList\.is-view-switching \.gy-dir-row,[\s\S]*?transition:\s*none !important;",
        )

    def test_directory_action_menu_has_simplified_accessible_contract(self):
        response = self.client.get("/guangya")

        self.assertIn('id="gyDirectoryContextMenu"', response.text)
        self.assertIn('role="menu"', response.text)
        self.assertIn('data-scrape-action="manual"', response.text)
        self.assertIn('data-scrape-action="auto"', response.text)
        self.assertIn('data-browser-action="delete-item"', response.text)
        self.assertIn('aria-haspopup="menu"', response.text)
        self.assertNotIn('data-browser-action="cleanup-directory"', response.text)

    def test_search_dialog_reserves_stable_candidate_and_detail_regions(self):
        response = self.client.get("/guangya")

        self.assertIn('class="gy-scrape-workspace"', response.text)
        self.assertIn('class="gy-scrape-candidate-region"', response.text)
        self.assertIn('class="gy-scrape-detail-region"', response.text)
        self.assertIn('aria-live="polite"', response.text)
        self.assertIn('id="gyScrapeArchiveTarget"', response.text)

    def test_context_menu_keeps_body_portal_and_visual_viewport_contract(self):
        script = DIRECTORY_SCRIPT.read_text("utf-8")

        self.assertIn("document.body.appendChild(menu)", script)
        self.assertIn("document.body.appendChild(modal)", script)
        self.assertIn("window.visualViewport", script)
        self.assertNotIn("bounds.right - 264", script)

    def test_file_delete_uses_direct_api_without_recycle_ui(self):
        html = self.client.get("/guangya").text
        script = DIRECTORY_SCRIPT.read_text("utf-8")

        self.assertIn("bindMediaRow(row, item, action)", html)
        self.assertIn("/api/guangya/delete-item", script)
        self.assertNotIn("GuangYaRecycleBinUI", script)
        self.assertNotIn("cleanup-directory", script)
        self.assertNotIn('id="gyRecycleModal"', html)
        self.assertNotIn("guangya-recycle-bin.css", html)
        self.assertNotIn("guangya-recycle-bin.js", html)


    def test_token_area_keeps_capability_status_but_removes_token_grid_and_overview_card(self):
        html = self.client.get("/guangya").text

        self.assertNotIn('id="gyTokenStatus"', html)
        self.assertNotIn('id="gyAccessToken"', html)
        self.assertNotIn('id="gyRefreshToken"', html)
        self.assertNotIn('id="gyExpiresAt"', html)
        self.assertNotIn("光鸭全链路能力", html)
        self.assertLess(
            html.index('class="gy-token-header"'),
            html.index('id="gyCapabilityBadge"'),
        )

    def test_one_click_clean_preserves_bracketed_title_and_removes_release_tags(self):
        if not shutil.which("node"):
            self.skipTest("node 不可用")
        shared_script = POSITION_SCRIPT.read_text("utf-8")
        directory_script = DIRECTORY_SCRIPT.read_text("utf-8")
        self.assertIn("window.MediaScrapePosition.sanitizeSearchQuery", directory_script)
        node_script = "const window = {};\n" + shared_script + textwrap.dedent(
            """
            const sanitizeSearchQuery = window.MediaScrapePosition.sanitizeSearchQuery;
            const results = [];
            results.push(sanitizeSearchQuery(
                '[Arifureta Shokugyou de Sekai Saikyou S2][01-12][BIG5][1080P][MP4]'
            ));
            results.push(sanitizeSearchQuery(
                '[ANi] 被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生（仅限港澳台)'
            ));
            results.push(sanitizeSearchQuery(
                '[Sakurato] Kage no Jitsuryokusha ni Naritakute! [01-20 FIN][AVC-8bit 1080P AAC][CHS]'
            ));
            results.push(sanitizeSearchQuery(
                '[LoliHouse] The Ghost in the Shell - 03 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv',
                {knownEpisode: 3}
            ));
            process.stdout.write(results.join('\\n'));
            """
        )
        completed = subprocess.run(
            ["node", "-e", node_script],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "Arifureta Shokugyou de Sekai Saikyou",
                "被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生",
                "Kage no Jitsuryokusha ni Naritakute!",
                "The Ghost in the Shell",
            ],
        )

    def test_preview_renderer_does_not_truncate_directory_files(self):
        script = DIRECTORY_SCRIPT.read_text("utf-8")

        self.assertIn("plans.forEach((plan) =>", script)
        self.assertIn("companionPlans.forEach((plan) =>", script)
        self.assertIn("pending.forEach((item) =>", script)
        self.assertNotIn("plans.slice(0, 8)", script)
        self.assertNotIn("个文件未展开", script)

    def test_directory_sorting_is_local_stable_and_persisted(self):
        html = self.client.get("/guangya").text

        self.assertIn("new Intl.Collator('zh-CN'", html)
        self.assertIn("localStorage.getItem('mediaflux_gy_sort_mode')", html)
        self.assertIn("gyDirectoryItems.filter(item=>item.is_dir).sort(gyCompareItems)", html)
        self.assertIn("gyDirectoryItems.filter(item=>!item.is_dir).sort(gyCompareItems)", html)
        self.assertIn("gyOrderedDirectoryItems = [...dirs, ...files]", html)
        self.assertIn("gyRenderNextBatch(gyRenderVersion)", html)
        self.assertNotIn("files.slice(0, 20)", html)
        self.assertNotIn("个文件未显示", html)
        self.assertIn("created_at", html)
        self.assertIn("updated_at", html)

    def test_scrape_styles_use_compact_menu_and_fixed_control_dimensions(self):
        css = DIRECTORY_CSS.read_text("utf-8")

        self.assertRegex(css, r"\.gy-directory-context-menu\s*\{[^}]*width:\s*220px;")
        self.assertRegex(css, r"\.gy-directory-context-menu button\s*\{[^}]*min-height:\s*48px;")
        self.assertIn("--gy-scrape-control-height: 36px", css)
        self.assertRegex(css, r"\.gy-scrape-footer\s*\{[^}]*flex:\s*0 0 48px;")
        self.assertRegex(css, r"\.gy-scrape-footer-context\s*\{[^}]*display:\s*flex;")
        self.assertRegex(css, r"\.gy-scrape-inspection\s*\{[^}]*border-right:\s*1px solid var\(--border-soft\);")
        self.assertRegex(css, r"#gyScrapeCancelBtn\s*\{[^}]*width:\s*80px;")
        self.assertRegex(css, r"#gyScrapeRunBtn\s*\{[^}]*width:\s*160px;")

    def test_manual_scrape_uses_source_target_workbench_layout(self):
        html = self.client.get("/guangya").text
        script = DIRECTORY_SCRIPT.read_text(encoding="utf-8")
        css = DIRECTORY_CSS.read_text(encoding="utf-8")

        self.assertIn("媒体与归档预览", html)
        self.assertIn("手动刮削", html)
        self.assertNotIn("候选详情与命名预览", html)
        self.assertIn("gy-scrape-media-summary", script)
        self.assertIn("gy-scrape-map-head", script)
        self.assertIn("gy-scrape-map-row", script)
        self.assertIn("gy-scrape-map-source", script)
        self.assertIn("gy-scrape-map-target", script)
        self.assertIn("原始扫描文件（SOURCE）", script)
        self.assertIn("规范化归档目标（TARGET）", script)
        self.assertIn("冲突检查：无冲突", script)
        self.assertIn("将整理至：等待目录检查", html)
        self.assertIn("function archiveTargetLabel(target)", script)
        self.assertIn("return `将整理至：${value}`", script)
        self.assertIn("elements.detail.setAttribute('aria-busy'", script)
        self.assertIn("function setMobilePane(pane)", script)
        self.assertIn("setMobilePane('candidates')", script)
        self.assertIn("setMobilePane('preview')", script)
        self.assertIn("function setPlanReady(ready)", script)
        self.assertIn("gy-scrape-candidate-state", script)
        self.assertIn("button.setAttribute('aria-pressed', 'false')", script)
        self.assertIn("item.setAttribute('aria-pressed', selected ? 'true' : 'false')", script)
        self.assertNotIn("'旧预览已失效'", script)
        self.assertIn('.gy-scrape-mobile-tabs { display: none; }', css)
        self.assertIn('.gy-scrape-dialog[data-mobile-pane="candidates"] .gy-scrape-detail-region', css)
        self.assertIn('.gy-scrape-dialog:not(.has-plan) .gy-scrape-target { display: none; }', css)
        self.assertIn('.gy-scrape-episode-fields { display: contents; }', css)
        self.assertIn(':has(.gy-scrape-episode-fields.is-season-only:not([hidden]))', css)
        self.assertNotIn('box-shadow: inset 3px 0 0 var(--accent);', css)
        self.assertRegex(
            css,
            r"\.gy-scrape-workspace\s*\{[^}]*grid-template-columns:\s*clamp\(276px, 23vw, 340px\) minmax\(0, 1fr\);",
        )
        self.assertRegex(
            css,
            r"\.gy-scrape-map-head,\s*\.gy-scrape-map-row\s*\{[^}]*grid-template-columns:",
        )
        self.assertIn("@media (max-width: 760px)", css)
        self.assertRegex(
            css,
            r"@media \(max-width: 760px\)[\s\S]*?\.gy-scrape-map-head\s*\{[^}]*display:\s*none;",
        )

    def test_preview_requests_are_abortable_and_deduplicated(self):
        script = DIRECTORY_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("async function api(path, payload, {signal} = {})", script)
        self.assertIn("signal,", script)
        self.assertIn("state.pendingPreviewKey === previewKey", script)
        self.assertIn("state.pendingSearchKey === searchKey", script)
        self.assertIn("state.previewController?.abort()", script)
        self.assertIn("state.searchController?.abort()", script)
        self.assertIn("error.name === 'AbortError'", script)

    def test_partial_cleanup_failures_are_visible_in_task_polling(self):
        script = DIRECTORY_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("['completed', 'partial'].includes(task.status)", script)
        self.assertIn("'source_dir_cleanup_failed'", script)
        self.assertIn("'empty_dir_cleanup_failed'", script)
        self.assertIn("'scan_errors'", script)
        self.assertIn("目录刮削部分完成", script)

    def test_hidden_browser_actions_do_not_affect_menu_measurement(self):
        css = Path("app/static/css/guangya-directory-scrape.css").read_text("utf-8")

        self.assertRegex(
            css,
            r"\.gy-directory-context-menu button\[hidden\]\s*\{\s*display:\s*none;",
        )


    def test_modal_and_task_polling_share_page_lifecycle(self):
        script = DIRECTORY_SCRIPT.read_text("utf-8")

        for contract in (
            "const modalLifecycle = window.createAppModal(modal",
            "onRequestClose: closeModal",
            "modalLifecycle.open(state.activeAction, {initialFocus: elements.query})",
            "const taskPollEntries = new Map()",
            "taskPollInFlight",
            "fetch('/api/guangya/organize/status', {signal: controller.signal})",
            "document.addEventListener('visibilitychange'",
            "window.addEventListener('pagehide'",
            "window.addEventListener('pageshow'",
            "if (!event.persisted) taskPollEntries.clear()",
        ):
            self.assertIn(contract, script)
        self.assertNotIn("for (let attempt = 0; attempt < 1800; attempt += 1)", script)
        self.assertNotIn("else if (!modal.hidden) closeModal()", script)


@unittest.skipIf(sync_playwright is None, "system Python 未安装 Playwright")
class GuangYaDirectDeleteBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.browser_path = next(
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
        if not cls.browser_path:
            raise unittest.SkipTest("未找到可用的本机 Chrome/Chromium")
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(
                headless=True,
                executable_path=cls.browser_path,
                args=["--no-sandbox"],
            )
        except Exception as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(f"无法启动本机浏览器: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        super().tearDownClass()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 800, "height": 600})
        self.page_errors: list[str] = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.set_content(DIRECT_DELETE_HARNESS)
        self.page.evaluate(
            """
            () => {
                window.renderLucideIcons = () => {};
                window.__alerts = [];
                window.__confirms = [];
                window.__confirmResult = true;
                window.__requests = [];
                window.__reloads = 0;
                window.appAlert = async options => {
                    window.__alerts.push(options);
                    return true;
                };
                window.appConfirm = async options => {
                    window.__confirms.push(options);
                    return window.__confirmResult;
                };
                window.gyNavigator = {
                    state: () => ({id: 'parent-a', path: []}),
                    reload: async () => { window.__reloads += 1; },
                };
                window.fetch = async (path, options = {}) => {
                    window.__requests.push({
                        path,
                        method: options.method || 'GET',
                        body: options.body ? JSON.parse(options.body) : null,
                    });
                    return {
                        ok: true,
                        status: 200,
                        json: async () => ({ok: true, file_id: 'junk'}),
                    };
                };
            }
            """
        )
        self.page.add_script_tag(content=APP_MODAL_SCRIPT)
        self.page.add_script_tag(content=POSITION_SCRIPT.read_text("utf-8"))
        self.page.add_script_tag(content=DIRECTORY_SCRIPT.read_text("utf-8"))

    def tearDown(self):
        self.page.close()

    def _open_file_menu(self):
        self.page.evaluate(
            """
            () => {
                const row = document.createElement('div');
                const action = document.createElement('button');
                action.type = 'button';
                action.id = 'direct-delete-action';
                action.innerHTML = '<span>操作</span>';
                row.appendChild(action);
                document.body.appendChild(row);
                window.__deleteAction = action;
                window.GuangYaDirectoryScrapeUI.bindMediaRow(
                    row,
                    {file_id: 'junk', name: '广告.txt', is_dir: false, is_video: false},
                    action,
                );
                action.click();
            }
            """
        )
        self.page.wait_for_function(
            """
            () => {
                const menu = document.getElementById('gyDirectoryContextMenu');
                const deleteButton = menu.querySelector(
                    '[data-browser-action="delete-item"]'
                );
                return !menu.hidden && !deleteButton.hidden;
            }
            """
        )

    def test_menu_ignores_initial_browser_resize_then_closes_normally(self):
        self._open_file_menu()

        self.page.set_viewport_size({"width": 801, "height": 600})
        self.assertIsNone(
            self.page.get_attribute("#gyDirectoryContextMenu", "hidden")
        )

        self.page.wait_for_timeout(180)
        self.page.set_viewport_size({"width": 802, "height": 600})
        self.page.wait_for_function(
            "() => document.getElementById('gyDirectoryContextMenu').hidden"
        )

    def test_file_delete_confirms_once_calls_direct_api_and_reloads(self):
        self._open_file_menu()
        self.page.evaluate(
            "() => document.querySelector('[data-browser-action=\"delete-item\"]').click()"
        )
        self.page.wait_for_function("() => window.__reloads === 1")

        state = self.page.evaluate(
            """
            () => ({
                confirms: window.__confirms,
                requests: window.__requests,
                reloads: window.__reloads,
                alerts: window.__alerts,
            })
            """
        )
        self.assertEqual(len(state["confirms"]), 1)
        self.assertEqual(state["confirms"][0]["title"], "删除文件")
        self.assertEqual(state["confirms"][0]["confirmText"], "删除文件")
        self.assertTrue(state["confirms"][0]["danger"])
        self.assertIn("广告.txt", state["confirms"][0]["message"])
        self.assertIn("光鸭回收站", state["confirms"][0]["message"])
        self.assertNotIn("verifyText", state["confirms"][0])
        self.assertEqual(
            state["requests"],
            [{
                "path": "/api/guangya/delete-item",
                "method": "POST",
                "body": {"file_id": "junk"},
            }],
        )
        self.assertEqual(state["reloads"], 1)
        self.assertEqual(state["alerts"][-1]["type"], "success")
        self.assertEqual(self.page_errors, [])

    def test_reload_failure_keeps_delete_success_and_prevents_misleading_retry(self):
        self.page.evaluate(
            """
            () => {
                window.__reloadSettled = false;
                window.gyNavigator.reload = async () => {
                    window.__reloads += 1;
                    setTimeout(() => { window.__reloadSettled = true; }, 0);
                    throw new Error('opaque-refresh-secret');
                };
            }
            """
        )
        self._open_file_menu()
        self.page.evaluate(
            "() => document.querySelector('[data-browser-action=\"delete-item\"]').click()"
        )
        self.page.wait_for_function("() => window.__reloadSettled")

        state = self.page.evaluate(
            """
            () => ({
                actionState: window.__deleteAction.dataset.state,
                actionDisabled: window.__deleteAction.disabled,
                alerts: window.__alerts,
                requests: window.__requests,
            })
            """
        )
        self.assertEqual(state["actionState"], "done")
        self.assertTrue(state["actionDisabled"])
        self.assertEqual(len(state["requests"]), 1)
        self.assertEqual(len(state["alerts"]), 1)
        self.assertEqual(state["alerts"][0]["type"], "warning")
        self.assertIn("文件已删除", state["alerts"][0]["title"])
        self.assertNotIn("删除失败", state["alerts"][0]["title"])
        self.assertNotIn("opaque-refresh-secret", str(state["alerts"]))
        self.assertEqual(self.page_errors, [])

    def test_pending_delete_blocks_duplicate_request_and_failure_reenables_action(self):
        self.page.evaluate(
            """
            () => {
                window.__deleteResolvers = [];
                window.fetch = (path, options = {}) => {
                    window.__requests.push({
                        path,
                        method: options.method || 'GET',
                        body: options.body ? JSON.parse(options.body) : null,
                    });
                    return new Promise(resolve => {
                        window.__deleteResolvers.push(resolve);
                    });
                };
            }
            """
        )
        self._open_file_menu()
        self.page.evaluate(
            "() => document.querySelector('[data-browser-action=\"delete-item\"]').click()"
        )
        self.page.wait_for_function("() => window.__requests.length === 1")

        self.page.evaluate(
            """
            () => {
                window.__deleteAction.click();
                const menu = document.getElementById('gyDirectoryContextMenu');
                const deleteButton = document.querySelector(
                    '[data-browser-action="delete-item"]'
                );
                if (!menu.hidden && !deleteButton.hidden) deleteButton.click();
            }
            """
        )
        self.page.wait_for_function(
            "() => window.__deleteAction.disabled || window.__confirms.length >= 2"
        )
        pending = self.page.evaluate(
            """
            () => ({
                actionDisabled: window.__deleteAction.disabled,
                requests: window.__requests.length,
                confirms: window.__confirms.length,
            })
            """
        )

        self.page.evaluate(
            """
            () => {
                window.__deleteResolvers.forEach(resolve => resolve({
                    ok: false,
                    status: 500,
                    json: async () => ({error: '光鸭删除文件失败'}),
                }));
            }
            """
        )
        self.page.wait_for_function(
            "() => window.__deleteAction.dataset.state === 'error'"
        )
        final = self.page.evaluate(
            """
            () => ({
                actionDisabled: window.__deleteAction.disabled,
                alerts: window.__alerts.length,
                requests: window.__requests.length,
                confirms: window.__confirms.length,
            })
            """
        )
        self.page.evaluate("() => window.__deleteAction.click()")
        menu_hidden = self.page.get_attribute("#gyDirectoryContextMenu", "hidden")

        self.assertEqual(
            pending,
            {"actionDisabled": True, "requests": 1, "confirms": 1},
        )
        self.assertEqual(
            final,
            {"actionDisabled": False, "alerts": 1, "requests": 1, "confirms": 1},
        )
        self.assertIsNone(menu_hidden)
        self.assertEqual(self.page_errors, [])


if __name__ == "__main__":
    unittest.main()
