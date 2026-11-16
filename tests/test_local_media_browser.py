"""本地媒体页面结构、导航和稳定占位契约。"""
from __future__ import annotations

import re
import mimetypes
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from jinja2 import Environment, FileSystemLoader

from fastapi.testclient import TestClient

from app.main import create_app
from tests.support import IsolatedDatabaseTestCase
from tests import test_agent_kernel_browser as browser_support

if TYPE_CHECKING:
    from playwright.sync_api import Browser


class LocalMediaBrowserTests(IsolatedDatabaseTestCase):
    def test_page_has_four_stable_work_areas_and_navigation(self):
        from tests.support import initialized_web_runtime
        self.enterContext(initialized_web_runtime())
        with TestClient(create_app(start_background=False)) as client:
            login = client.get("/login")
            search_result = re.search(r'name="csrf_token"\s+value="([^"]+)"', login.text)
            assert search_result is not None
            token = search_result.group(1)
            response = client.post(
                "/login", data={"username": "admin", "password": "123456", "csrf_token": token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            page = client.get("/local-media")
        self.assertEqual(page.status_code, 200)
        for marker in (
            'id="lmSourcesSection"', 'id="lmMediaItems"', 'id="lmReviewList"',
            'id="lmTaskList"', 'id="lmSourceModal"', 'id="lmItemContextMenu"',
            'id="lmScrapeModal"', 'id="lmSearchQuery"', 'id="lmExecuteBtn"',
            'id="lmScrapeEpisodeFields"', 'id="lmScrapeSeason"', 'id="lmScrapeEpisode"',
            'id="lmTaskSearch"', 'id="lmTaskSelectAll"', 'id="lmClearTasksBtn"',
            'id="lmTaskDetailModal"', 'id="lmTaskDetailBody"',
            'id="lmMediaPathbar"', 'id="lmMediaBrowseHome"',
            'id="lmMediaBreadcrumb"', 'id="lmMediaBrowseUp"',
            'id="lmPickLocalRootBtn"', 'id="lmSourceMediaType"',
            'id="lmSourceEnabled"', 'qB 自动整理',
            '<option value="true">开启</option>', '<option value="false">关闭</option>',
            '<option value="nsfw">成人番号（仅 MetaTube）</option>',
            'class="lm-source-routing-row"',
            'js/local-media.js', 'css/local-media.css',
        ):
            self.assertIn(marker, page.text)
        for removed_marker in (
            'id="lmStableSeconds"', 'id="lmScanMinutes"', 'id="lmScanEnabled"',
            '>稳定等待<', '>扫描间隔<', '>定时扫描<',
        ):
            self.assertNotIn(removed_marker, page.text)
        self.assertIn("本地媒体", page.text)
        self.assertIn("整理日志", page.text)
        self.assertNotIn("<span>整理任务</span>", page.text)
        self.assertIn("正在读取来源配置", page.text)
        self.assertRegex(page.text, re.compile(r'class="[^"]*\blocal-media-page\b[^"]*"'))
        self.assertIn('class="app-modal lm-modal"', page.text)
        self.assertIn("待整理（你的qB下载路径）", page.text)
        self.assertIn("/media/downloads", page.text)
        for legacy_marker in (
            'data-windows-package', 'id="lmSmbAuthSection"', 'id="lmSmbUser"',
            'id="lmSmbPass"', "SMB 网络共享", "NAS 访问账号",
        ):
            self.assertNotIn(legacy_marker, page.text)
        self.assertNotIn("下载完成，直接进入媒体库", page.text)
        self.assertNotIn("LOCAL MEDIA PIPELINE", page.text)
        self.assertNotIn("硬链接", page.text)
        self.assertNotIn("复制模式", page.text)
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")
        self.assertIn("/api/local-media/directories", js)
        self.assertNotIn("/api/local-media/media-servers", js)
        self.assertIn("/api/local-media/items", js)
        self.assertIn("/api/local-media/items/delete", js)
        self.assertIn("function openScrapeForItem", js)
        self.assertIn("function loadMediaDirectory", js)
        self.assertIn("data-open-directory", js)
        self.assertIn("data-media-breadcrumb", js)
        self.assertIn("mediaItemsUrl()", js)
        self.assertIn("itemRequestSerial === mediaBrowseRequestSerial", js)
        self.assertIn("仅来源根目录一级条目可移入回收区", js)
        self.assertIn("function filteredTasks()", js)
        self.assertIn("function updateTaskSelectionState", js)
        self.assertIn("function openTaskDetail", js)
        self.assertIn("function clearSelectedTasks", js)
        self.assertIn("data-task-detail", js)
        self.assertIn("method: 'DELETE'", js)
        self.assertIn("confirm: 'CLEAR'", js)
        self.assertNotIn("data-target-library", js)
        self.assertIn("归档映射", js)
        self.assertIn("成人番号专用", js)
        self.assertIn("MetaTube 精确识别", js)
        self.assertIn("activeNsfwSource", js)
        self.assertIn("lmSourceMode", page.text)
        self.assertNotIn("row.dataset.libraryRequest", js)
        self.assertIn("allowRoot: !isRootsMode && Boolean(sourceId)", js)
        self.assertIn('id="lmTaskMore"', page.text)
        self.assertIn("taskDisplayLimit = 60", js)
        self.assertIn("function schedulePoll()", js)
        self.assertIn("document.addEventListener('visibilitychange'", js)
        self.assertIn("let refreshQueued = false", js)
        self.assertIn("if (refreshing) return refreshPromise", js)
        self.assertIn("while (refreshQueued)", js)
        self.assertIn("refreshQueued = true", js)
        for contract in (
            "let previewRequestSerial = 0",
            "let searchRequestSerial = 0",
            "let appliedPreviewContext = null",
            "function invalidatePreview()",
            "requestSerial !== previewRequestSerial",
            "inspection?.inspection_id !== context.inspectionId",
            "positionControls.payload(mediaType",
            "appliedPreviewContext = Object.freeze",
            "preview_digest: confirmedPreview.preview_digest",
            "const firstLoad = !hasLoadedLocalMedia",
            "hasLoadedLocalMedia = Object.values(loadedResources).some(Boolean)",
            "await settleInitialLoading()",
            "function renderInitialResourceFailure(resource, message)",
            "renderInitialResourceFailure('sources', resources.sources.error?.message)",
            "if (currentManual && window.appAlert)",
        ):
            self.assertIn(contract, js)
        self.assertNotIn("if (refreshing) return;", js)
        self.assertNotIn("setInterval(() => loadAll(false), 10000)", js)
        self.assertNotIn('id="lmManualSource"', page.text)
        self.assertNotIn('id="lmManualPath"', page.text)
        self.assertNotIn('id="lmPickManualPathBtn"', page.text)
        css = Path("app/static/css/local-media.css").read_text(encoding="utf-8")
        self.assertIn(".lm-media-list", css)
        self.assertIn(".lm-item-context-menu", css)
        self.assertIn(".lm-scrape-workspace", css)
        self.assertIn(".lm-scrape-episode-fields", css)
        self.assertIn(".lm-task-search", css)
        self.assertIn(".lm-task-detail-dialog", css)
        self.assertIn(".lm-task-actions", css)
        self.assertIn(".lm-form-grid-3", css)
        self.assertIn(".lm-source-routing-row", css)
        self.assertIn("grid-template-columns: minmax(260px, 1.75fr) minmax(160px, .72fr) minmax(140px, .55fr)", css)
        self.assertIn("grid-template-columns: minmax(340px, 35%) minmax(0, 1fr)", css)
        self.assertNotIn(".lm-source-trigger-control", css)
        self.assertNotIn(".lm-source-head-toggle", css)
        self.assertNotRegex(css, r"\.lm-media-row:hover\s*\{[^}]*transform")

    def test_background_poll_keeps_expensive_item_refresh_opt_in(self):
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")

        poll_block = js[
            js.index("function schedulePoll()"):
            js.index("function sourceCard(")
        ]
        self.assertIn("await loadAll(false);", poll_block)
        self.assertNotIn("includeItems", poll_block)
        for contract in (
            "let queuedItemRefresh = false",
            "function loadAll(manual = false, {includeItems = false} = {})",
            "queuedItemRefresh = queuedItemRefresh || includeItems",
            "const shouldLoadItems = queuedItemRefresh",
            "queuedItemRefresh = false",
            "const collectedResources = {}",
            "resources: outcomeResources",
            "loadAll(true, {includeItems: true})",
            "loadAll(false, {includeItems: currentTab === 'manual'})",
            "loadAll(false, {includeItems: true})",
        ):
            self.assertIn(contract, js)
        self.assertNotIn("const shouldLoadItems = currentTab === 'manual' || currentManual", js)
        self.assertGreaterEqual(js.count("loadAll(false, {includeItems: true})"), 6)

    def test_item_context_menu_ignores_initial_browser_viewport_event_and_restores_focus(self):
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")

        for contract in (
            "let itemContextMenuOpenedAt = 0",
            "function closeItemContextMenu({restoreFocus = false} = {})",
            "const returnFocus = activeContextTrigger",
            "returnFocus.focus({preventScroll: true})",
            "function closeItemContextMenuAfterViewportChange(event)",
            "event?.isTrusted",
            "performance.now() - itemContextMenuOpenedAt < 160",
            "itemContextMenuOpenedAt = performance.now()",
            "window.visualViewport?.addEventListener('scroll', closeItemContextMenuAfterViewportChange)",
            "window.visualViewport?.addEventListener('resize', closeItemContextMenuAfterViewportChange)",
            "document.addEventListener('scroll', closeItemContextMenuAfterViewportChange, true)",
            "closeItemContextMenu({restoreFocus: true})",
        ):
            self.assertIn(contract, js)
        self.assertNotIn("window.addEventListener('scroll', closeItemContextMenu, true)", js)

    def test_execute_submits_only_the_confirmed_preview_identity(self):
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")
        execute = js.split("async function execute()", 1)[1].split("async function ", 1)[0]
        payload = execute.split("body: JSON.stringify({", 1)[1].split("}),", 1)[0]
        self.assertIn("inspection_id: confirmedContext.inspectionId", payload)
        self.assertIn("preview_digest: confirmedPreview.preview_digest", payload)
        for field in ("tmdb_id:", "media_type:", "season:", "episode:", "rules_snapshot:"):
            self.assertNotIn(field, payload)

    def test_scrape_modal_uses_shared_modal_lifecycle(self):
        js = Path("app/static/js/local-media.js").read_text(encoding="utf-8")

        self.assertIn("const scrapeLifecycle = window.createAppModal($('lmScrapeModal')", js)
        self.assertIn("onRequestClose: closeScrape", js)
        self.assertIn("scrapeLifecycle.open(trigger, {initialFocus: $('lmScrapeCloseBtn')})", js)
        self.assertIn("scrapeLifecycle.close()", js)
        self.assertNotIn("else if (!$('lmScrapeModal').hidden) closeScrape()", js)


@unittest.skipIf(browser_support.sync_playwright is None, '系统环境未安装 Playwright')
class LocalMediaExecutionBrowserTests(unittest.TestCase):
    browser: Browser

    @classmethod
    def setUpClass(cls):
        browser_support.AgentKernelBrowserTests.setUpClass.__func__(cls)
        env = Environment(loader=FileSystemLoader(browser_support.ROOT / 'app/templates'), autoescape=True)
        cls.html = env.get_template('local_media.html').render(
            active='local_media', app_version='offline-execute-test', discovery_enabled=False,
            agent_enabled=False, csrf_token=lambda: 'fake', static_url=lambda value: '/static/' + value,
            url_for=lambda name: '/' + name.split('.')[-1],
        )

    def make_page(self, width):
        context = self.browser.new_context(viewport={'width': width, 'height': 844},
                                           reduced_motion='reduce', service_workers='block')
        self.addCleanup(context.close)
        errors, unexpected = [], []

        def route(request):
            url = urlsplit(request.request.url)
            path = unquote(url.path)
            if url.netloc == 'local-media.test' and path == '/local-media':
                request.fulfill(body=self.html, content_type='text/html')
                return
            if url.netloc == 'local-media.test' and path.startswith('/static/'):
                asset = (browser_support.ROOT / 'app' / path.lstrip('/')).resolve()
                if asset.is_relative_to(browser_support.ROOT / 'app/static') and asset.is_file():
                    request.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(str(asset))[0] or 'application/octet-stream')
                    return
            unexpected.append(request.request.url)
            request.abort()

        context.route('**/*', route)
        context.route_web_socket('**/*', lambda websocket: websocket.close())
        context.add_init_script(r"""(() => {
            const fixture = window.__lmFixture = {
                calls: [], executes: [], refreshes: [], holdRefresh: false, failResources: [], previewSerial: 0,
                previews: [], holdPreview: false,
            };
            const json = (body, status = 200) => new Response(JSON.stringify(body), {
                status, headers: {'Content-Type': 'application/json'},
            });
            const items = ['A', 'B'].map(name => ({
                source_id: 1, source_name: '离线来源', name: name + '.mp4', path: '/fixture/' + name + '.mp4',
                kind: 'video', size: 1024, organize_ready: true, deletable: true, identity: {size: 1024, mtime_ns: 1},
            }));
            const resources = {
                sources: {sources: [{id: 1, name: '离线来源', enabled: true, media_type: 'movie', local_root: '/fixture',
                    targets: [{path: '/archive', category: 'movie'}]}]},
                tasks: {tasks: [], has_more: false}, items: {items, sources: []},
            };
            window.fetch = async (url, options = {}) => {
                const path = new URL(String(url), location.href).pathname;
                const body = JSON.parse(options.body || '{}');
                fixture.calls.push({path, body});
                const resource = path.split('/').pop();
                if (path.startsWith('/api/local-media/') && resource in resources) {
                    const reply = () => fixture.failResources.includes(resource)
                        ? json({error: resource + '刷新失败'}, 503) : json(resources[resource]);
                    return fixture.holdRefresh ? new Promise(resolve => fixture.refreshes.push(() => resolve(reply()))) : reply();
                }
                if (path === '/api/local-media/inspect') return json({
                    inspection_id: body.path, selected_name: body.path.split('/').pop(), selected_kind: 'file',
                    video_count: 1, file_count: 1, suggested_query: body.path,
                });
                if (path === '/api/local-media/preview') {
                    const result = json({status: 'planned',
                        preview_digest: `digest:${body.inspection_id}:${++fixture.previewSerial}`,
                        plans: [{target_path: `/archive/${body.inspection_id.split('/').pop()}.${fixture.previewSerial}`,
                            target_name: body.inspection_id.split('/').pop(), action: 'move', role: 'video'}],
                    });
                    return fixture.holdPreview ? new Promise(resolve => fixture.previews.push(() => resolve(result))) : result;
                }
                if (path === '/api/local-media/execute') return new Promise(resolve => fixture.executes.push(
                    (result, status = 200) => resolve(json(result, status)),
                ));
                throw new Error('unexpected fake request: ' + path);
            };
        })();""")
        page = context.new_page()
        page.set_default_timeout(3000)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://local-media.test/local-media#manual')
        page.locator('[data-open-item-menu]').first.wait_for()
        page.evaluate('() => document.fonts.ready')
        return page, unexpected, errors

    @staticmethod
    def open_plan(page, index):
        page.locator('[data-open-item-menu]').nth(index).click()
        page.locator('[data-item-action="auto"]').click()
        page.wait_for_function("!document.querySelector('#lmExecuteBtn').disabled && document.querySelector('#lmScrapeStatus').textContent === '计划已就绪'")

    @staticmethod
    def start_execute(page, count=1):
        page.locator('#lmExecuteBtn').click()
        page.locator('#appConfirmSubmit').click()
        page.wait_for_function('count => window.__lmFixture.executes.length === count', arg=count)

    @staticmethod
    def plan_snapshot(page):
        return page.evaluate("""() => ({
            detail: document.querySelector('#lmScrapeDetail').innerHTML,
            path: document.querySelector('#lmScrapeItemPath').textContent,
            summary: document.querySelector('#lmPlanSummary').textContent,
            target: document.querySelector('#lmScrapeTarget').textContent,
            label: document.querySelector('#lmExecuteBtn').textContent,
            disabled: document.querySelector('#lmExecuteBtn').disabled,
            hasPlan: document.querySelector('.lm-scrape-dialog').classList.contains('has-plan'),
            bounds: document.querySelector('#lmScrapeDetail').getBoundingClientRect().toJSON(),
        })""")

    @staticmethod
    def release_execute(page, outcome, index=0):
        page.evaluate("""({outcome, index}) => {
            const result = outcome === 'error' ? {error: '执行请求失败'} : outcome === 'repreview'
                ? {status: 'requires_manual', repreview_required: true, preview: {reason: '原计划需重新确认'}}
                : {status: 'completed', moved: ['media.mp4'], deleted_junk: []};
            window.__lmFixture.executes[index](result, outcome === 'error' ? 503 : 200);
        }""", {'outcome': outcome, 'index': index})
        page.locator('.app-toast-message').last.wait_for()

    def assert_plan_unchanged(self, page, before):
        self.assertTrue(page.locator('#lmScrapeModal').is_visible())
        self.assertEqual(self.plan_snapshot(page), before)
        self.assertTrue(page.locator('#lmScrapeCloseBtn').evaluate('node => document.activeElement === node'))
        self.assertTrue(page.locator('.app-toast-message').last.evaluate("""node => {
            const rect = node.getBoundingClientRect();
            return node.closest('.app-toast').contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
        }"""), '真实 toast 必须显示在新刮削弹窗上方')

    def test_desktop_movie_searchbar_collapses_hidden_episode_column(self):
        page, unexpected, errors = self.make_page(1440)
        self.open_plan(page, 0)
        self.assertTrue(page.locator('#lmScrapeEpisodeFields').is_hidden())
        bar = page.locator('.lm-scrape-searchbar')
        box, search, auto = bar.bounding_box(), page.locator('#lmSearchBtn').bounding_box(), page.locator('#lmAutoPreviewBtn').bounding_box()
        assert box and search and auto
        padding = bar.evaluate('node => parseFloat(getComputedStyle(node).paddingRight)')
        # 隐藏的季集列曾把“自动匹配”拉到 300px 以上，并在搜索按钮右侧留出空列。
        self.assertAlmostEqual(box['x'] + box['width'] - padding, search['x'] + search['width'], delta=1)
        self.assertLessEqual(auto['width'], 140)
        self.assertEqual((unexpected, errors), ([], []))

    def test_late_execution_receipts_keep_new_plan_and_focus(self):
        for width in (1280, 390):
            for outcome in ('completed', 'error', 'repreview'):
                with self.subTest(width=width, outcome=outcome):
                    page, unexpected, errors = self.make_page(width)
                    self.open_plan(page, 0)
                    self.start_execute(page)
                    page.locator('#lmScrapeCloseBtn').click()
                    self.open_plan(page, 1)
                    page.locator('#lmScrapeCloseBtn').focus()
                    before = self.plan_snapshot(page)
                    self.release_execute(page, outcome)
                    if outcome != 'error':
                        page.wait_for_function("window.__lmFixture.calls.filter(c => c.path === '/api/local-media/items').length > 1")
                    self.assert_plan_unchanged(page, before)
                    toast = page.locator('.app-toast-message').last.inner_text()
                    self.assertIn('A.mp4', toast)
                    self.assertIn({'completed': '已移动 1 项', 'error': '整理失败', 'repreview': '原计划需重新确认'}[outcome], toast)
                    self.assertEqual(page.evaluate("window.__lmFixture.calls.filter(c => c.path === '/api/local-media/execute').map(c => c.body)"),
                                     [{'inspection_id': '/fixture/A.mp4', 'preview_digest': 'digest:/fixture/A.mp4:1'}])
                    self.assertEqual((unexpected, errors), ([], []))

    def test_old_receipt_cannot_release_another_plan_execution(self):
        for width in (1280, 390):
            for outcome in ('completed', 'error', 'repreview'):
                with self.subTest(width=width, outcome=outcome):
                    page, unexpected, errors = self.make_page(width)
                    self.open_plan(page, 0)
                    self.start_execute(page)
                    page.locator('#lmScrapeCloseBtn').click()
                    self.open_plan(page, 1)
                    self.start_execute(page, 2)
                    page.locator('#lmScrapeCloseBtn').focus()
                    before = self.plan_snapshot(page)
                    self.assertTrue(before['disabled'])
                    self.assertIn('整理中', before['label'])
                    self.release_execute(page, outcome)
                    self.assert_plan_unchanged(page, before)
                    self.assertEqual(page.evaluate('window.__lmFixture.executes.length'), 2)
                    self.release_execute(page, 'completed', 1)
                    page.locator('#lmScrapeModal').wait_for(state='hidden')
                    self.assertIn('B.mp4', page.locator('.app-toast-message').last.inner_text())
                    self.assertEqual(page.evaluate('window.__lmFixture.executes.length'), 2)
                    self.assertEqual((unexpected, errors), ([], []))

    def test_same_file_reopen_and_new_preview_keep_their_own_plan(self):
        for width in (1280, 390):
            for change in ('reopen', 'preview'):
                with self.subTest(width=width, change=change):
                    page, unexpected, errors = self.make_page(width)
                    self.open_plan(page, 0)
                    self.start_execute(page)
                    if change == 'reopen':
                        page.locator('#lmScrapeCloseBtn').click()
                        self.open_plan(page, 0)
                    else:
                        bounds = page.locator('#lmScrapeDetail').bounding_box()
                        page.evaluate('window.__lmFixture.holdPreview = true')
                        page.locator('#lmAutoPreviewBtn').click()
                        page.wait_for_function('window.__lmFixture.previews.length === 1')
                        self.assertIn('has-plan', page.locator('.lm-scrape-dialog').get_attribute('class') or '')
                        self.assertEqual(page.locator('#lmScrapeDetail').bounding_box(), bounds)
                        page.evaluate('window.__lmFixture.previews[0]()')
                        page.wait_for_function("!document.querySelector('#lmExecuteBtn').disabled")
                    page.locator('#lmScrapeCloseBtn').focus()
                    before = self.plan_snapshot(page)
                    self.assertIn('.2', before['target'])
                    self.assertIn('确认并开始整理', before['label'])
                    self.release_execute(page, 'completed')
                    self.assert_plan_unchanged(page, before)
                    self.assertEqual(page.evaluate('window.__lmFixture.executes.length'), 1)
                    self.assertEqual((unexpected, errors), ([], []))

    def test_success_refresh_delay_and_resource_failure_keep_new_plan(self):
        for width in (1280, 390):
            for failed in ([], ['sources'], ['sources', 'tasks', 'items']):
                with self.subTest(width=width, failed=failed):
                    page, unexpected, errors = self.make_page(width)
                    self.open_plan(page, 0)
                    self.start_execute(page)
                    page.evaluate('window.__lmFixture.holdRefresh = true')
                    self.release_execute(page, 'completed')
                    page.wait_for_function('window.__lmFixture.refreshes.length === 3')
                    page.locator('#lmScrapeModal').wait_for(state='hidden')
                    self.open_plan(page, 1)
                    page.locator('#lmScrapeCloseBtn').focus()
                    before = self.plan_snapshot(page)
                    page.evaluate("""failed => {
                        window.__lmFixture.failResources = failed;
                        window.__lmFixture.holdRefresh = false;
                        window.__lmFixture.refreshes.splice(0).forEach(release => release());
                    }""", failed)
                    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                    self.assert_plan_unchanged(page, before)
                    messages = page.locator('.app-toast-message').all_text_contents()
                    self.assertTrue(any('A.mp4' in text and '整理完成' in text and '已移动 1 项' in text for text in messages))
                    self.assertFalse(any('整理失败' in text for text in messages))
                    if failed:
                        self.assertIn('刷新失败', page.locator('#lmRefreshBtn').get_attribute('title') or '')
                    self.assertEqual(page.evaluate('window.__lmFixture.executes.length'), 1)
                    self.assertEqual((unexpected, errors), ([], []))

    def test_current_repreview_invalidates_plan_and_current_error_restores_retry(self):
        for width in (1280, 390):
            for outcome in ('repreview', 'error'):
                with self.subTest(width=width, outcome=outcome):
                    page, unexpected, errors = self.make_page(width)
                    self.open_plan(page, 0)
                    self.start_execute(page)
                    self.release_execute(page, outcome)
                    self.assertTrue(page.locator('#lmScrapeModal').is_visible())
                    self.assertEqual(page.locator('#lmExecuteBtn').is_disabled(), outcome == 'repreview')
                    self.assertIn('确认并开始整理', page.locator('#lmExecuteBtn').inner_text())
                    self.assertEqual('has-plan' in (page.locator('.lm-scrape-dialog').get_attribute('class') or ''), outcome != 'repreview')
                    self.assertEqual(page.evaluate('window.__lmFixture.executes.length'), 1)
                    self.assertEqual((unexpected, errors), ([], []))
