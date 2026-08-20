"""光鸭目录/文件操作菜单的真实浏览器行为回归测试。"""
from __future__ import annotations

import shutil
import textwrap
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 由显式 system python3 门禁执行
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "app/static/js/guangya-directory-scrape.js").read_text("utf-8")
STYLES = (ROOT / "app/static/css/guangya-directory-scrape.css").read_text("utf-8")
HARNESS = textwrap.dedent(
    """
    <!doctype html>
    <meta charset="utf-8">
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; }
        .content { transform: translateY(10px); }
        #gyDirectoryContextMenu {
            position: fixed;
            width: 120px;
            padding: 0;
            border: 0;
        }
        #gyDirectoryContextMenu[hidden],
        #gyDirectoryContextMenu button[hidden],
        #gyScrapeModal[hidden] {
            display: none;
        }
        #gyDirectoryContextMenu button {
            display: block;
            width: 120px;
            height: 20px;
            min-height: 20px;
            margin: 0;
            padding: 0;
            border: 0;
        }
        .test-action {
            position: fixed;
            width: 20px;
            height: 20px;
            margin: 0;
            padding: 0;
            border: 0;
        }
    </style>
    <main class="content">
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
            <button id="gyScrapeCleanBtn"><span>一键精简</span></button>
            <button id="gyScrapeExternalBtn"><span>豆瓣 / BGM 线索</span></button>
            <input id="gyScrapeQuery">
            <select id="gyScrapeType">
                <option value="auto">auto</option>
                <option value="movie">movie</option>
                <option value="tv">tv</option>
            </select>
            <div id="gyScrapeEpisodeFields" hidden>
                <label id="gyScrapeSeasonField"><input id="gyScrapeSeason" type="number"></label>
                <label id="gyScrapeEpisodeField"><input id="gyScrapeEpisode" type="number"></label>
            </div>
            <div id="gyScrapeDirectory"></div>
            <div id="gyScrapeInspection" data-state="ready">
                <div id="gyScrapeInspectionSummary"></div>
                <div id="gyScrapeInspectionHint"></div>
            </div>
            <div id="gyScrapeCandidates"></div>
            <div id="gyScrapeExternalHints" hidden></div>
            <div id="gyScrapeCandidateCount"></div>
            <div id="gyScrapeDetail"></div>
            <div id="gyScrapeStatus"></div>
            <div id="gyScrapeArchiveTarget"></div>
            <div id="gyScrapePlanSummary"></div>
        </div>
    </main>
    """
)


@unittest.skipIf(sync_playwright is None, "system Python 未安装 Playwright")
class GuangYaDirectoryScrapeBrowserTests(unittest.TestCase):
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
        self.page.set_content(HARNESS)
        self.page.evaluate(
            """
            () => {
                const values = {left: 40, top: 30, width: 400, height: 300};
                const viewport = window.visualViewport;
                Object.defineProperties(viewport, {
                    offsetLeft: {configurable: true, get: () => values.left},
                    offsetTop: {configurable: true, get: () => values.top},
                    width: {configurable: true, get: () => values.width},
                    height: {configurable: true, get: () => values.height},
                });
                window.__viewportValues = values;
                window.__rows = [];
                window.__requests = [];
                window.renderLucideIcons = () => {};
                window.appAlert = async () => true;
                window.appConfirm = async () => false;
                window.fetch = async (path, options = {}) => {
                    const body = options.body ? JSON.parse(options.body) : null;
                    window.__requests.push({path, body});
                    if (path.endsWith('/inspect')) {
                        return {
                            ok: true,
                            json: async () => ({
                                inspection_id: 'inspection-1',
                                directory: {id: 'parent-a', name: '媒体'},
                                suggested_query: 'The Ghost in the Shell',
                                media_type: 'tv',
                                season: 1,
                                episode: 3,
                                season_inferred: true,
                                counts: {video: 1, subtitle: 0, metadata: 0},
                                ...(window.__inspection || {}),
                                archive_target: {id: 'archive', name: '媒体库'},
                            }),
                        };
                    }
                    if (path.endsWith('/search')) {
                        return {
                            ok: true,
                            json: async () => ({candidates: [{
                                tmdb_id: '255358',
                                media_type: 'tv',
                                title: '攻壳机动队',
                                score: 0.95,
                            }]}),
                        };
                    }
                    if (path.endsWith('/preview')) {
                        return {
                            ok: true,
                            json: async () => ({
                                preview_id: 'preview-1',
                                plans: [],
                                archive_target: {id: 'archive', name: '媒体库'},
                                ...(window.__preview || {}),
                            }),
                        };
                    }
                    return {ok: true, json: async () => ({})};
                };
                window.__bindItem = ({isDirectory, isVideo = false, left, top}) => {
                    const index = window.__rows.length;
                    const row = document.createElement('div');
                    row.id = `row-${index}`;
                    const action = document.createElement('button');
                    action.id = `action-${index}`;
                    action.className = 'test-action';
                    action.style.left = `${left}px`;
                    action.style.top = `${top}px`;
                    action.setAttribute('aria-expanded', 'false');
                    let falseWrites = 0;
                    const setAttribute = action.setAttribute.bind(action);
                    action.setAttribute = (name, value) => {
                        if (name === 'aria-expanded' && value === 'false') falseWrites += 1;
                        return setAttribute(name, value);
                    };
                    row.appendChild(action);
                    document.body.appendChild(row);
                    const item = {
                        file_id: `item-${index}`,
                        name: `item-${index}`,
                        is_dir: isDirectory,
                        is_video: isVideo,
                    };
                    window.GuangYaDirectoryScrapeUI.bindMediaRow(row, item, action);
                    const record = {
                        row,
                        action,
                        falseWrites: () => falseWrites,
                    };
                    window.__rows.push(record);
                    return {row: `#${row.id}`, action: `#${action.id}`, index};
                };
            }
            """
        )
        self.page.add_script_tag(content=SCRIPT)
        self.assertEqual(self.page_errors, [])

    def tearDown(self):
        self.page.close()

    def _bind(
        self,
        *,
        is_directory: bool,
        left: int,
        top: int,
        is_video: bool = False,
    ) -> dict[str, object]:
        return self.page.evaluate(
            "options => window.__bindItem(options)",
            {
                "isDirectory": is_directory,
                "isVideo": is_video,
                "left": left,
                "top": top,
            },
        )

    def _menu_position(self) -> tuple[float, float]:
        position = self.page.evaluate(
            """
            () => {
                const menu = document.getElementById('gyDirectoryContextMenu');
                return {left: parseFloat(menu.style.left), top: parseFloat(menu.style.top)};
            }
            """
        )
        return position["left"], position["top"]

    def _open_button(self, binding: dict[str, object]) -> None:
        self.page.eval_on_selector(str(binding["action"]), "element => element.click()")

    def _open_context(self, binding: dict[str, object], *, x: int, y: int) -> None:
        self.page.eval_on_selector(
            str(binding["row"]),
            """
            (element, point) => element.dispatchEvent(new MouseEvent('contextmenu', {
                bubbles: true,
                cancelable: true,
                clientX: point.x,
                clientY: point.y,
            }))
            """,
            {"x": x, "y": y},
        )

    def _close_menu(self) -> None:
        self.page.evaluate("window.GuangYaDirectoryScrapeUI.closeMenu()")

    def test_button_and_context_anchors_use_visual_viewport_offsets(self):
        binding = self._bind(is_directory=True, left=300, top=80)

        self._open_button(binding)
        self.assertEqual(self._menu_position(), (200, 106))

        self._close_menu()
        self._open_context(binding, x=100, y=100)
        self.assertEqual(self._menu_position(), (100, 100))

    def test_menu_flips_and_clamps_to_eight_pixel_visual_viewport_margin(self):
        binding = self._bind(is_directory=True, left=418, top=300)

        self._open_button(binding)
        self.assertEqual(self._menu_position(), (312, 234))

        self._close_menu()
        self._open_context(binding, x=438, y=328)
        self.assertEqual(self._menu_position(), (312, 262))

        self._close_menu()
        self._open_context(binding, x=42, y=32)
        self.assertEqual(self._menu_position(), (48, 38))

    def test_menu_rules_match_for_action_button_and_context_menu(self):
        cases = (
            (True, False, ["manual", "auto", "delete-item"], "manual"),
            (False, True, ["manual", "auto", "delete-item"], "manual"),
            (False, False, ["delete-item"], "delete-item"),
        )
        for case_index, (is_directory, is_video, actions, focused) in enumerate(cases):
            binding = self._bind(
                is_directory=is_directory,
                is_video=is_video,
                left=200,
                top=80 + case_index * 40,
            )
            for opener in ("button", "context"):
                with self.subTest(
                    is_directory=is_directory,
                    is_video=is_video,
                    opener=opener,
                ):
                    if opener == "button":
                        self._open_button(binding)
                    else:
                        self._open_context(binding, x=220, y=100 + case_index * 40)
                    state = self.page.evaluate(
                        """
                        () => ({
                            actions: [...document.querySelectorAll(
                                '#gyDirectoryContextMenu [role="menuitem"]:not([hidden])'
                            )].map(button => (
                                button.dataset.scrapeAction || button.dataset.browserAction
                            )),
                            focused: document.activeElement.dataset.scrapeAction
                                || document.activeElement.dataset.browserAction,
                        })
                        """
                    )
                    self.assertEqual(
                        state,
                        {"actions": actions, "focused": focused},
                    )
                    self._close_menu()

    def test_inspect_payload_uses_exactly_one_scope_key(self):
        directory = self._bind(is_directory=True, left=200, top=80)
        self._open_button(directory)
        self.page.evaluate(
            "() => document.querySelector('[data-scrape-action=\"manual\"]').click()"
        )
        self.page.wait_for_function(
            "() => window.__requests.some(request => request.path.endsWith('/inspect'))"
        )
        directory_request = self.page.evaluate(
            "() => window.__requests.find(request => request.path.endsWith('/inspect'))"
        )
        self.assertEqual(directory_request["body"], {"directory_id": "item-0"})

        self.page.evaluate(
            """
            () => {
                document.getElementById('gyScrapeModal').hidden = true;
                window.__requests = [];
            }
            """
        )
        video = self._bind(
            is_directory=False,
            is_video=True,
            left=200,
            top=120,
        )
        self._open_context(video, x=220, y=140)
        self.page.evaluate(
            "() => document.querySelector('[data-scrape-action=\"manual\"]').click()"
        )
        self.page.wait_for_function(
            "() => window.__requests.some(request => request.path.endsWith('/inspect'))"
        )
        file_request = self.page.evaluate(
            "() => window.__requests.find(request => request.path.endsWith('/inspect'))"
        )
        self.assertEqual(file_request["body"], {"file_id": "item-1"})
        self.assertEqual(self.page_errors, [])

    def test_episode_fields_support_file_episode_and_directory_season_override(self):
        video = self._bind(is_directory=False, is_video=True, left=200, top=120)
        self._open_button(video)
        self.page.evaluate(
            "() => document.querySelector('[data-scrape-action=\"manual\"]').click()"
        )
        self.page.wait_for_function("() => !document.getElementById('gyScrapeModal').hidden")

        file_state = self.page.evaluate(
            """
            () => ({
                query: document.getElementById('gyScrapeQuery').value,
                mediaType: document.getElementById('gyScrapeType').value,
                hidden: document.getElementById('gyScrapeEpisodeFields').hidden,
                season: document.getElementById('gyScrapeSeason').value,
                episode: document.getElementById('gyScrapeEpisode').value,
            })
            """
        )
        self.assertEqual(file_state, {
            "query": "The Ghost in the Shell",
            "mediaType": "tv",
            "hidden": False,
            "season": "1",
            "episode": "3",
        })

        self.page.fill('#gyScrapeSeason', '0')
        self.page.locator('.gy-scrape-candidate').first.click()
        self.page.wait_for_function(
            "() => window.__requests.some(request => request.path.endsWith('/preview'))"
        )
        preview_request = self.page.evaluate(
            "() => window.__requests.find(request => request.path.endsWith('/preview'))"
        )
        self.assertEqual(preview_request["body"]["season"], 0)
        self.assertEqual(preview_request["body"]["episode"], 3)

        self.page.select_option('#gyScrapeType', 'movie')
        self.assertTrue(
            self.page.evaluate("() => document.getElementById('gyScrapeEpisodeFields').hidden")
        )

        self.page.evaluate(
            """
            () => {
                document.getElementById('gyScrapeModal').hidden = true;
                window.__requests = [];
                window.__inspection = {
                    suggested_query: 'Arifureta Shokugyou de Sekai Saikyou',
                    media_type: 'tv',
                    season: 2,
                    episode: null,
                    season_inferred: false,
                    counts: {video: 13, subtitle: 0, metadata: 0},
                };
            }
            """
        )
        directory = self._bind(is_directory=True, left=200, top=160)
        self._open_button(directory)
        self.page.evaluate(
            "() => document.querySelector('[data-scrape-action=\"manual\"]').click()"
        )
        self.page.wait_for_function("() => !document.getElementById('gyScrapeModal').hidden")
        directory_state = self.page.evaluate(
            """
            () => ({
                hidden: document.getElementById('gyScrapeEpisodeFields').hidden,
                seasonHidden: document.getElementById('gyScrapeSeasonField').hidden,
                episodeHidden: document.getElementById('gyScrapeEpisodeField').hidden,
                season: document.getElementById('gyScrapeSeason').value,
            })
            """
        )
        self.assertEqual(directory_state, {
            "hidden": False,
            "seasonHidden": False,
            "episodeHidden": True,
            "season": "2",
        })
        self.page.locator('.gy-scrape-candidate').first.click()
        self.page.wait_for_function(
            "() => window.__requests.some(request => request.path.endsWith('/preview'))"
        )
        directory_preview = self.page.evaluate(
            "() => window.__requests.find(request => request.path.endsWith('/preview'))"
        )
        self.assertEqual(directory_preview["body"]["season"], 2)
        self.assertNotIn("episode", directory_preview["body"])
        self.assertEqual(self.page_errors, [])

    def test_season_only_inspection_does_not_send_partial_episode_override(self):
        self.page.evaluate(
            """
            () => {
                window.__inspection = {
                    suggested_query: 'The Ghost in the Shell',
                    media_type: 'tv',
                    season: 1,
                    episode: null,
                    season_inferred: false,
                };
            }
            """
        )
        video = self._bind(is_directory=False, is_video=True, left=200, top=120)
        self._open_button(video)
        self.page.evaluate(
            "() => document.querySelector('[data-scrape-action=\"manual\"]').click()"
        )
        self.page.wait_for_function("() => !document.getElementById('gyScrapeModal').hidden")
        self.page.locator('.gy-scrape-candidate').first.click()
        self.page.wait_for_function(
            "() => window.__requests.some(request => request.path.endsWith('/preview'))"
        )
        preview_request = self.page.evaluate(
            "() => window.__requests.find(request => request.path.endsWith('/preview'))"
        )
        self.assertNotIn("season", preview_request["body"])
        self.assertNotIn("episode", preview_request["body"])
        self.assertEqual(self.page_errors, [])

    def test_one_click_clean_preserves_official_parenthesis_and_searches(self):
        self.page.evaluate(
            """
            () => {
                window.__inspection = {
                    suggested_query: '[ANi] 被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生（仅限港澳台)',
                    media_type: 'tv',
                    season: 1,
                    episode: null,
                    season_inferred: true,
                };
            }
            """
        )
        directory = self._bind(is_directory=True, left=200, top=120)
        self._open_button(directory)
        self.page.evaluate(
            '() => document.querySelector(\'[data-scrape-action="manual"]\').click()'
        )
        self.page.wait_for_function("() => !document.getElementById('gyScrapeModal').hidden")
        self.page.evaluate("() => { window.__requests = []; }")
        self.page.locator('#gyScrapeCleanBtn').click()
        self.page.wait_for_function(
            "() => window.__requests.some(request => request.path.endsWith('/search'))"
        )

        value = self.page.locator('#gyScrapeQuery').input_value()
        request = self.page.evaluate(
            "() => window.__requests.find(request => request.path.endsWith('/search'))"
        )
        self.assertEqual(
            value,
            '被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生',
        )
        self.assertEqual(request['body']['query'], value)
        self.assertEqual(request['body']['media_type'], 'tv')
        self.assertEqual(self.page_errors, [])

    def test_one_click_clean_preserves_bracketed_title_and_searches(self):
        raw = '[Arifureta Shokugyou de Sekai Saikyou S2][01-12][BIG5][1080P][MP4]'
        self.page.evaluate(
            """
            value => {
                window.__inspection = {
                    suggested_query: value,
                    media_type: 'tv',
                    season: 2,
                    episode: null,
                    season_inferred: true,
                };
            }
            """,
            raw,
        )
        directory = self._bind(is_directory=True, left=200, top=120)
        self._open_button(directory)
        self.page.evaluate(
            '() => document.querySelector(\'[data-scrape-action="manual"]\').click()'
        )
        self.page.wait_for_function("() => !document.getElementById('gyScrapeModal').hidden")
        self.page.evaluate("() => { window.__requests = []; }")
        self.page.locator('#gyScrapeCleanBtn').click()
        self.page.wait_for_function(
            "() => window.__requests.some(request => request.path.endsWith('/search'))"
        )

        value = self.page.locator('#gyScrapeQuery').input_value()
        request = self.page.evaluate(
            "() => window.__requests.find(request => request.path.endsWith('/search'))"
        )
        self.assertEqual(value, 'Arifureta Shokugyou de Sekai Saikyou')
        self.assertEqual(request['body']['query'], value)
        self.assertEqual(request['body']['media_type'], 'tv')
        self.assertEqual(self.page_errors, [])

    def test_one_click_clean_removes_orion_origin_and_japanese_audio_tag(self):
        raw = (
            '[orion origin] Undead Unluck [01-24] [BDRip] [1080p] '
            '[H265 10bit_FLAC] [CHS＆JPN]'
        )
        self.page.evaluate(
            """
            value => {
                window.__inspection = {
                    suggested_query: value,
                    media_type: 'tv',
                    season: 1,
                    episode: null,
                    season_inferred: true,
                };
            }
            """,
            raw,
        )
        directory = self._bind(is_directory=True, left=200, top=120)
        self._open_button(directory)
        self.page.evaluate(
            '() => document.querySelector(\'[data-scrape-action="manual"]\').click()'
        )
        self.page.wait_for_function("() => !document.getElementById('gyScrapeModal').hidden")
        self.page.evaluate("() => { window.__requests = []; }")
        self.page.locator('#gyScrapeCleanBtn').click()
        self.page.wait_for_function(
            "() => window.__requests.some(request => request.path.endsWith('/search'))"
        )

        value = self.page.locator('#gyScrapeQuery').input_value()
        self.assertEqual(value, 'Undead Unluck')
        self.assertEqual(self.page_errors, [])

    def test_one_click_clean_preserves_japanese_word_in_title(self):
        raw = '[Japanese] Story.2003.1080p.WEB-DL.H264.AAC.mkv'
        self.page.evaluate(
            """
            value => {
                window.__inspection = {
                    suggested_query: value,
                    media_type: 'movie',
                    season: null,
                    episode: null,
                    season_inferred: false,
                };
            }
            """,
            raw,
        )
        directory = self._bind(is_directory=True, left=200, top=120)
        self._open_button(directory)
        self.page.evaluate(
            '() => document.querySelector(\'[data-scrape-action="manual"]\').click()'
        )
        self.page.wait_for_function("() => !document.getElementById('gyScrapeModal').hidden")
        self.page.evaluate("() => { window.__requests = []; }")
        self.page.locator('#gyScrapeCleanBtn').click()
        self.page.wait_for_function(
            "() => window.__requests.some(request => request.path.endsWith('/search'))"
        )

        self.assertEqual(self.page.locator('#gyScrapeQuery').input_value(), 'Japanese Story 2003')
        self.assertEqual(self.page_errors, [])

    def test_one_click_clean_removes_known_episode_and_release_tags(self):
        raw = (
            '[ANi] 被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生'
            '（仅限港澳台） - 01 [1080P][Bilibili][WEB-DL][AAC AVC][CHT CHS][MP4].mp4'
        )
        self.page.evaluate(
            """
            value => {
                window.__inspection = {
                    suggested_query: value,
                    media_type: 'tv',
                    season: 1,
                    episode: 1,
                    season_inferred: false,
                };
            }
            """,
            raw,
        )
        video = self._bind(is_directory=False, is_video=True, left=200, top=120)
        self._open_button(video)
        self.page.evaluate(
            '() => document.querySelector(\'[data-scrape-action="manual"]\').click()'
        )
        self.page.wait_for_function("() => !document.getElementById('gyScrapeModal').hidden")
        self.page.evaluate("() => { window.__requests = []; }")
        self.page.locator('#gyScrapeCleanBtn').click()
        self.page.wait_for_function(
            "() => window.__requests.some(request => request.path.endsWith('/search'))"
        )

        value = self.page.locator('#gyScrapeQuery').input_value()
        self.assertEqual(
            value,
            '被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生',
        )
        self.assertNotIn('01', value)
        self.assertNotIn('mp4', value.lower())
        self.assertEqual(self.page_errors, [])

    def test_pending_video_is_visible_and_never_hidden_in_archive_summary(self):
        self.page.evaluate(
            """
            () => {
                window.__inspection = {
                    counts: {video: 12, video_total: 13, pending_video: 1, subtitle: 0, metadata: 0},
                    pending_videos: [{
                        file_id: 'unknown',
                        name: 'x.mp4',
                        relative_dir: '',
                        reason: '无法从文件名确认媒体归属，已保留在源目录',
                    }],
                };
                window.__preview = {
                    plans: [{file_id: 'e1', action: 'move', new_name: 'Example.Show.S01E01.mkv'}],
                    pending_videos: window.__inspection.pending_videos,
                };
            }
            """
        )
        directory = self._bind(is_directory=True, left=200, top=120)
        self._open_button(directory)
        self.page.evaluate(
            '() => document.querySelector(\'[data-scrape-action="manual"]\').click()'
        )
        self.page.wait_for_function("() => !document.getElementById('gyScrapeModal').hidden")

        self.assertEqual(
            self.page.locator('#gyScrapeInspectionSummary').text_content(),
            '12 可刮削 · 1 待确认',
        )
        self.assertIn('不会移动', self.page.locator('#gyScrapeInspectionHint').text_content())
        self.page.wait_for_selector('.gy-scrape-candidate')
        self.page.locator('.gy-scrape-candidate').click()
        self.page.wait_for_function(
            "() => document.getElementById('gyScrapePlanSummary').textContent.includes('1 个待确认不移动')"
        )
        self.assertIn('x.mp4（不会移动）', self.page.locator('#gyScrapeDetail').text_content())
        self.assertEqual(self.page_errors, [])

    def test_naming_preview_renders_every_plan_without_more_placeholder(self):
        self.page.evaluate(
            """
            () => {
                window.__inspection = {
                    counts: {video: 12, subtitle: 1, metadata: 0},
                    pending_videos: [{
                        file_id: 'pending', name: 'x.mp4', relative_dir: '',
                        reason: '无法确认媒体归属',
                    }],
                };
                window.__preview = {
                    plans: Array.from({length: 12}, (_, index) => ({
                        file_id: `e${index + 1}`,
                        action: 'move',
                        new_name: `Example.Show.S01E${String(index + 1).padStart(2, '0')}.mkv`,
                        target_path: '剧集/Example Show/Season 01',
                    })),
                    companion_plans: [{
                        file_id: 'subtitle', role: 'subtitle',
                        target_name: 'Example.Show.S01E01.zh.ass',
                    }],
                    pending_videos: window.__inspection.pending_videos,
                };
            }
            """
        )
        directory = self._bind(is_directory=True, left=200, top=120)
        self._open_button(directory)
        self.page.evaluate(
            '() => document.querySelector(\'[data-scrape-action="manual"]\').click()'
        )
        self.page.wait_for_function("() => !document.getElementById('gyScrapeModal').hidden")
        self.page.wait_for_selector('.gy-scrape-candidate')
        self.page.locator('.gy-scrape-candidate').click()
        self.page.wait_for_function(
            "() => document.getElementById('gyScrapeDetail').textContent.includes('S01E12')"
        )

        rows = self.page.locator('#gyScrapeDetail .gy-scrape-plan-files > div')
        self.assertEqual(rows.count(), 14)
        detail = self.page.locator('#gyScrapeDetail').text_content()
        self.assertIn('Example.Show.S01E12.mkv', detail)
        self.assertIn('Example.Show.S01E01.zh.ass', detail)
        self.assertIn('x.mp4（不会移动）', detail)
        self.assertNotIn('未展开', detail)
        self.assertNotIn('更多', detail)
        self.assertEqual(self.page_errors, [])

    def test_production_css_keeps_menu_and_scrape_controls_compact_and_equal_height(self):
        page = self.browser.new_page(viewport={"width": 1000, "height": 700})
        try:
            page.set_content(
                """
                <div class="gy-directory-context-menu" id="menu">
                    <button><span>删除</span></button>
                </div>
                <section class="gy-scrape-dialog">
                    <div class="gy-scrape-searchbar">
                        <label><span>名称</span><input class="form-input" id="query"></label>
                        <label><span>类型</span><select class="form-select" id="type"><option>剧集</option></select></label>
                        <div class="gy-scrape-episode-fields">
                            <label><span>季</span><input class="form-input" id="season"></label>
                            <label><span>集</span><input class="form-input" id="episode"></label>
                        </div>
                        <button class="btn" id="search">搜索</button>
                    </div>
                    <div class="gy-scrape-footer-actions">
                        <button class="jump-btn" id="gyScrapeCancelBtn">取消</button>
                        <button class="btn" id="gyScrapeRunBtn">确认并开始刮削</button>
                    </div>
                </section>
                """
            )
            page.add_style_tag(content=STYLES)
            dimensions = page.evaluate(
                """
                () => ({
                    menuWidth: document.getElementById('menu').getBoundingClientRect().width,
                    menuItemHeight: document.querySelector('#menu button').getBoundingClientRect().height,
                    heights: ['query', 'type', 'season', 'episode', 'search',
                              'gyScrapeCancelBtn', 'gyScrapeRunBtn'].map(
                        id => document.getElementById(id).getBoundingClientRect().height
                    ),
                    cancelWidth: document.getElementById('gyScrapeCancelBtn').getBoundingClientRect().width,
                    runWidth: document.getElementById('gyScrapeRunBtn').getBoundingClientRect().width,
                })
                """
            )
            self.assertEqual(dimensions["menuWidth"], 220)
            self.assertEqual(dimensions["menuItemHeight"], 48)
            self.assertEqual(dimensions["heights"], [36] * 7)
            self.assertEqual(dimensions["cancelWidth"], 80)
            self.assertEqual(dimensions["runWidth"], 160)
        finally:
            page.close()

    def test_escape_and_viewport_events_close_menu_without_duplicate_side_effects(self):
        binding = self._bind(is_directory=True, left=200, top=80)

        self._open_button(binding)
        self.page.keyboard.press("Escape")
        escape_state = self.page.evaluate(
            """
            index => {
                const record = window.__rows[index];
                return {
                    hidden: document.getElementById('gyDirectoryContextMenu').hidden,
                    expanded: record.action.getAttribute('aria-expanded'),
                    focused: document.activeElement === record.action,
                    falseWrites: record.falseWrites(),
                };
            }
            """,
            binding["index"],
        )
        self.assertEqual(
            escape_state,
            {"hidden": True, "expanded": "false", "focused": True, "falseWrites": 1},
        )

        for event_name, expected_writes in (("scroll", 2), ("resize", 3)):
            self._open_button(binding)
            self.page.evaluate(
                "eventName => window.visualViewport.dispatchEvent(new Event(eventName))",
                event_name,
            )
            viewport_state = self.page.evaluate(
                """
                index => {
                    const record = window.__rows[index];
                    return {
                        hidden: document.getElementById('gyDirectoryContextMenu').hidden,
                        expanded: record.action.getAttribute('aria-expanded'),
                        falseWrites: record.falseWrites(),
                    };
                }
                """,
                binding["index"],
            )
            self.assertEqual(
                viewport_state,
                {"hidden": True, "expanded": "false", "falseWrites": expected_writes},
            )

        self.page.evaluate(
            """
            () => {
                window.visualViewport.dispatchEvent(new Event('scroll'));
                window.dispatchEvent(new Event('resize'));
                document.dispatchEvent(new Event('scroll'));
            }
            """
        )
        self.assertEqual(
            self.page.evaluate(
                "index => window.__rows[index].falseWrites()",
                binding["index"],
            ),
            3,
        )

        self._open_button(binding)
        self.page.evaluate("window.dispatchEvent(new Event('resize'))")
        self.assertEqual(
            self.page.get_attribute(str(binding["action"]), "aria-expanded"),
            "false",
        )

        self._open_button(binding)
        self.page.evaluate("document.dispatchEvent(new Event('scroll'))")
        self.assertEqual(
            self.page.get_attribute(str(binding["action"]), "aria-expanded"),
            "false",
        )


if __name__ == "__main__":
    unittest.main()
