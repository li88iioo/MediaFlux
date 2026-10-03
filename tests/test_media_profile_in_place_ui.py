from __future__ import annotations

import re
import shutil
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:  # pragma: no cover - 可选浏览器依赖
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_SCRIPT = ROOT / "app/static/js/discovery.js"
SUBSCRIPTIONS_SCRIPT = ROOT / "app/static/js/subscriptions.js"
GLOBAL_SEARCH_TEMPLATE = ROOT / "app/templates/global_search.html"
RSS_TEMPLATE = ROOT / "app/templates/rss.html"
PROFILE_HOST = ROOT / "app/templates/_media_profile_host.html"
PROFILE_DIALOG = ROOT / "app/templates/_media_profile_dialog.html"
MAIN_STYLES = ROOT / "app/static/css/main.css"


class MediaProfileInPlaceUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.discovery = DISCOVERY_SCRIPT.read_text(encoding="utf-8")
        self.subscriptions = SUBSCRIPTIONS_SCRIPT.read_text(encoding="utf-8")
        self.global_search = GLOBAL_SEARCH_TEMPLATE.read_text(encoding="utf-8")
        self.rss = RSS_TEMPLATE.read_text(encoding="utf-8")
        self.host = PROFILE_HOST.read_text(encoding="utf-8")
        self.dialog = PROFILE_DIALOG.read_text(encoding="utf-8")
        self.styles = MAIN_STYLES.read_text(encoding="utf-8")

    def test_search_and_subscription_pages_mount_the_shared_profile_host(self):
        for template in (self.global_search, self.rss):
            self.assertIn('{% include "_media_profile_host.html" %}', template)
            self.assertIn("static_url('js/discovery.js')", template)
        self.assertIn('data-discovery-profile-host="true"', self.host)
        self.assertIn('{% include "_media_profile_dialog.html" %}', self.host)
        self.assertIn('id="discovery-detail-dialog"', self.dialog)

    def test_profile_links_keep_navigation_fallback_but_open_in_place_for_plain_clicks(self):
        self.assertIn("data-media-profile-link", self.global_search)
        self.assertGreaterEqual(self.subscriptions.count("dataset.mediaProfileLink = ''"), 4)
        for contract in (
            "const profileOnly = root.dataset.discoveryProfileHost === 'true'",
            "function detailIdentityFromURL(value)",
            "a[data-media-profile-link]",
            "event.preventDefault()",
            "void openDetail(identity, link)",
            "if (profileOnly) return;",
            "if (!profileOnly) loadActive();",
        ):
            self.assertIn(contract, self.discovery)
        self.assertRegex(
            self.discovery,
            re.compile(
                r"event\.defaultPrevented.*?event\.button !== 0.*?event\.metaKey.*?event\.ctrlKey.*?event\.shiftKey.*?event\.altKey",
                re.S,
            ),
        )
        self.assertIn("link.target === '_blank'", self.discovery)
        self.assertIn("link.hasAttribute('download')", self.discovery)

    @unittest.skipIf(sync_playwright is None, "未安装 Playwright")
    def test_plain_click_opens_and_closes_profile_without_changing_current_page(self):
        browser_path = next(
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
        if not browser_path:
            self.skipTest("未找到可用的本机 Chrome/Chromium")

        script = self.discovery
        dialog = self.dialog
        styles = self.styles
        html = f"""
            <!doctype html>
            <html><head><meta name="viewport" content="width=device-width, initial-scale=1"><style>{styles}</style></head>
            <body>
                <a id="profileLink" data-media-profile-link href="/discovery?detail_provider=tmdb&detail_type=movie&detail_id=693134&return_query=test">查看媒体档案</a>
                <div data-discovery-root data-discovery-profile-host="true" data-resource-results-enabled="false">
                    <div hidden>
                        <div id="discovery-source-tabs"></div><div id="discovery-filter-region"></div>
                        <form id="discovery-search-form"><input id="discovery-search-query"><button id="discovery-search-submit"><span></span></button></form>
                        <span id="discovery-provider-status"></span><div id="discovery-sections"></div><div id="discovery-grid"></div><div id="discovery-stage"></div>
                        <button id="discovery-refresh"></button><div id="discovery-load-more-row"><button id="discovery-load-more"><span></span></button></div><div id="discovery-page-sentinel"></div>
                    </div>
                    <div id="discovery-live"></div>
                    {dialog}
                </div>
                <script>
                    window.renderLucideIcons = () => {{}};
                    window.fetch = async () => ({{
                        ok: true,
                        status: 200,
                        json: async () => ({{detail: {{provider: 'tmdb', media_type: 'movie', external_id: '693134', tmdb_id: 693134, title: '沙丘2', year: '2024', overview: '测试简介'}}}}),
                    }});
                </script>
                <script>{script}</script>
            </body></html>
        """

        playwright = sync_playwright().start()
        browser = None
        try:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=browser_path,
                args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 390, "height": 640})
            page.route(
                "http://mediaflux.test/search*",
                lambda route: route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=html,
                ),
            )
            page.route(
                "http://mediaflux.test/rss*",
                lambda route: route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                    body=html,
                ),
            )
            page.goto("http://mediaflux.test/search?q=test", wait_until="domcontentloaded")
            before = page.url
            page.locator("#profileLink").click()
            page.locator("#discovery-detail-dialog[open]").wait_for()
            page.get_by_text("沙丘2", exact=True).wait_for()
            self.assertEqual(page.url, before)
            bounds = page.locator("#discovery-detail-dialog").evaluate(
                "node => { const rect = node.getBoundingClientRect(); return {top: rect.top, bottom: rect.bottom, viewport: innerHeight}; }"
            )
            self.assertGreaterEqual(bounds["top"], -1)
            self.assertLessEqual(bounds["bottom"], bounds["viewport"] + 1)
            page.locator("[data-discovery-dialog-close]").click()
            page.wait_for_function("!document.querySelector('#discovery-detail-dialog').open")
            self.assertEqual(page.url, before)
            self.assertTrue(page.locator("#profileLink").evaluate("node => node === document.activeElement"))

            page.goto("http://mediaflux.test/rss#media", wait_until="domcontentloaded")
            page.locator("#profileLink").evaluate(
                "node => node.href = '/discovery?detail_provider=tmdb&detail_type=movie&detail_id=693134&return_to=/rss%23media'"
            )
            rss_before = page.url
            page.locator("#profileLink").click()
            page.locator("#discovery-detail-dialog[open]").wait_for()
            page.get_by_text("沙丘2", exact=True).wait_for()
            self.assertEqual(page.url, rss_before)
            page.locator("[data-discovery-dialog-close]").click()
            page.wait_for_function("!document.querySelector('#discovery-detail-dialog').open")
            self.assertEqual(page.url, rss_before)
        finally:
            if browser is not None:
                browser.close()
            playwright.stop()



@unittest.skipIf(sync_playwright is None, "未安装 Playwright")
class ResourceConfirmLayerBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from jinja2 import Environment, FileSystemLoader
        cls.playwright = sync_playwright().start()
        executable = next((path for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser') if (path := shutil.which(name))), None)
        if not executable:
            cls.playwright.stop()
            raise unittest.SkipTest('未找到 Chromium')
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True, args=['--no-sandbox'])
        env = Environment(loader=FileSystemLoader(ROOT / 'app/templates'), autoescape=True)
        cls.html = env.from_string('''{% extends "base.html" %}{% block content %}
            <a id="openProfile" data-media-profile-link href="/discovery?detail_provider=tmdb&detail_type=tv&detail_id=123">打开资源</a>
            {% include "_media_profile_host.html" %}{% endblock %}
            {% block scripts %}<script src="{{ static_url('js/discovery.js') }}"></script>{% endblock %}''').render(
                active='global_search', app_version='offline-test', resource_results_enabled=True,
                discovery_enabled=True, agent_enabled=False, csrf_token=lambda: 'offline-csrf',
                static_url=lambda path: '/static/' + path, url_for=lambda name, **kwargs: '/' + name.split('.')[-1],
            )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def page(self, width=390, *, native=True):
        import mimetypes
        from urllib.parse import unquote, urlsplit
        context = self.browser.new_context(viewport={'width': width, 'height': 844}, reduced_motion='reduce')
        self.addCleanup(context.close)
        if not native:
            context.add_init_script('HTMLDialogElement.prototype.showModal = undefined')
        page = context.new_page()
        page.set_default_timeout(4000)
        writes, errors, unexpected = [], [], []
        resources = [{'result_id': f'resource-{i}', 'site_id': 'btbtla', 'site_name': 'BTBtla',
                      'title': f'离线测试资源 {i}', 'download_state': 'resolvable', 'download_kinds': ['magnet']} for i in range(5)]
        def route_request(route):
            path = unquote(urlsplit(route.request.url).path)
            if path == '/test':
                route.fulfill(status=200, content_type='text/html', body=self.html)
            elif path.startswith('/static/'):
                asset = (ROOT / 'app' / path.lstrip('/')).resolve()
                if asset.is_relative_to(ROOT / 'app/static') and asset.is_file():
                    route.fulfill(status=200, content_type=mimetypes.guess_type(str(asset))[0] or 'application/octet-stream', body=asset.read_bytes())
                else:
                    route.abort()
            elif path.startswith('/api/discovery/detail/'):
                route.fulfill(json={'detail': {'provider': 'tmdb', 'media_type': 'tv', 'external_id': '123', 'tmdb_id': 123, 'title': '离线测试剧集', 'year': 2026}})
            elif path == '/api/indexers/search':
                route.fulfill(json={'items': resources, 'site_statuses': [{'site_id': 'btbtla', 'site_name': 'BTBtla', 'status': 'success'}]})
            elif path == '/api/indexers/download/batch':
                writes.append(route.request.post_data_json)
                route.fulfill(json={'ok': True, 'items': [{'result_id': item['result_id'], 'ok': True, 'status': 'success', 'succeeded': ['guangya']} for item in resources]})
            else:
                unexpected.append(route.request.url)
                route.abort()
        context.route('**/*', route_request)
        context.route_web_socket('**/*', lambda ws: ws.close())
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://testserver/test')
        page.locator('#openProfile').click()
        expect(page.locator('[data-resource-result-id]')).to_have_count(5)
        page.locator('[data-resource-select-all]').click()
        expect(page.locator('[data-resource-batch-target="guangya"]')).to_be_enabled()
        self.addCleanup(lambda: self.assertEqual(errors, []))
        self.addCleanup(lambda: self.assertEqual(unexpected, []))
        return page, writes

    def assert_frontmost(self, page, modal_id):
        self.assertTrue(page.locator('#' + modal_id).evaluate('el => el.matches(":modal")'))
        self.assertTrue(page.locator('#' + modal_id).evaluate('''el => {
            const card=el.querySelector('.card'); const r=card.getBoundingClientRect();
            return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
        }'''))

    def test_five_resource_confirmation_cancel_escape_and_single_submission(self):
        for width in (1280, 390, 320):
            with self.subTest(width=width):
                page, writes = self.page(width)
                trigger = page.locator('[data-resource-batch-target="guangya"]')
                trigger.click()
                self.assert_frontmost(page, 'appConfirmModal')
                self.assertEqual(page.locator('#appConfirmTitle').inner_text(), '提交 5 条资源？')
                self.assertEqual(page.locator('#appConfirmMessage').inner_text(), '目标：光鸭')
                card = page.locator('.app-confirm-dialog').bounding_box()
                self.assertGreaterEqual(card['x'], 0)
                self.assertLessEqual(card['x'] + card['width'], width)
                self.assertGreaterEqual(card['y'], 0)
                self.assertLessEqual(card['y'] + card['height'], 844)
                self.assertEqual(writes, [])
                page.locator('#appConfirmCancel').click()
                self.assertTrue(page.locator('#appConfirmModal').is_hidden())
                self.assertTrue(page.locator('#discovery-detail-dialog').evaluate('el => el.open'))
                self.assertTrue(trigger.evaluate('el => el === document.activeElement'))
                self.assertEqual(page.locator('[data-resource-result-id] input:checked').count(), 5)
                trigger.click()
                page.keyboard.press('Escape')
                self.assertTrue(page.locator('#appConfirmModal').is_hidden())
                self.assertTrue(page.locator('#discovery-detail-dialog').evaluate('el => el.open'))
                self.assertEqual(writes, [])
                trigger.click()
                page.locator('#appConfirmSubmit').click()
                expect(page.locator('[data-resource-batch-summary]')).to_contain_text('成功 5')
                expect(trigger).to_be_disabled()
                expect(page.locator('[data-resource-batch-target="qb"]')).to_be_enabled()
                self.assertEqual(page.locator('[data-resource-result-id] input:checked').count(), 5)
                self.assertEqual(len(writes), 1)
                self.assertEqual(writes[0]['result_ids'], [f'resource-{i}' for i in range(5)])
                self.assertEqual(writes[0]['target'], 'guangya')
                self.assertTrue(page.locator('#discovery-detail-dialog').evaluate('el => el.open'))
                page.close()

    def test_close_during_pending_download_releases_panel_and_keeps_late_receipt(self):
        page, writes = self.page()
        pending_routes = []

        def hold_download(route):
            writes.append(route.request.post_data_json)
            pending_routes.append(route)

        page.route('**/api/indexers/download/batch', hold_download)
        page.locator('[data-resource-batch-target="guangya"]').click()
        with page.expect_request('**/api/indexers/download/batch'):
            page.locator('#appConfirmSubmit').click()
        page.wait_for_function("document.querySelector('#appConfirmModal').hidden")
        self.assertEqual(len(pending_routes), 1)
        self.assertEqual(len(writes), 1)
        self.assertTrue(page.locator('#appMessageModal').is_hidden())

        page.keyboard.press('Escape')
        page.wait_for_function("""() => {
            const dialog = document.querySelector('#discovery-detail-dialog');
            return !dialog.open && document.querySelector('#discovery-detail-body').childElementCount === 0;
        }""")
        self.assertEqual(page.locator('#discovery-detail-body [data-resource-result-id]').count(), 0)
        self.assertTrue(page.locator('#appMessageModal').is_hidden())

        pending_routes[0].fulfill(json={
            'ok': True,
            'items': [
                {'result_id': f'resource-{index}', 'ok': True, 'status': 'success', 'succeeded': ['guangya']}
                for index in range(5)
            ],
        })
        page.locator('#appMessageModal[open]').wait_for()
        self.assertEqual(page.locator('#appMessageTitle').inner_text(), '批量提交完成')
        self.assertIn('成功 5', page.locator('#appMessageText').inner_text())
        self.assertFalse(page.locator('#discovery-detail-dialog').evaluate('el => el.open'))
        self.assertEqual(page.locator('#discovery-detail-body').evaluate('el => el.childElementCount'), 0)
        page.locator('#appMessageClose').click()

    def test_queued_receipt_opens_global_alert_from_native_close_event(self):
        page, writes = self.page()
        pending_routes = []

        def hold_download(route):
            writes.append(route.request.post_data_json)
            pending_routes.append(route)

        page.route('**/api/indexers/download/batch', hold_download)
        page.evaluate("""() => {
            window.__resourceAlerts = [];
            const appAlert = window.appAlert;
            window.appAlert = options => {
                window.__resourceAlerts.push(options);
                return appAlert(options);
            };
        }""")
        page.locator('[data-resource-batch-target="guangya"]').click()
        with page.expect_request('**/api/indexers/download/batch'):
            page.locator('#appConfirmSubmit').click()
        self.assertEqual(len(pending_routes), 1)

        page.evaluate("""() => {
            const link = document.querySelector('#openProfile');
            document.querySelector('[data-discovery-dialog-close]').click();
            link.href = '/discovery?detail_provider=tmdb&detail_type=tv&detail_id=456';
            link.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, button: 0}));
        }""")
        page.locator('[data-resource-result-id]').first.wait_for()
        pending_routes[0].fulfill(json={
            'ok': True,
            'items': [
                {'result_id': f'resource-{index}', 'ok': True, 'status': 'success', 'succeeded': ['guangya']}
                for index in range(5)
            ],
        })
        page.wait_for_load_state('networkidle')
        self.assertEqual(page.evaluate('window.__resourceAlerts.length'), 0)
        self.assertTrue(page.locator('#discovery-detail-dialog').evaluate('el => el.open'))
        self.assertTrue(page.locator('#appMessageModal').is_hidden())

        page.keyboard.press('Escape')
        page.locator('#appMessageModal[open]').wait_for()
        self.assertEqual(page.evaluate('window.__resourceAlerts.length'), 1)
        self.assertEqual(page.locator('#appMessageTitle').inner_text(), '批量提交完成')
        self.assertIn('成功 5', page.locator('#appMessageText').inner_text())
        self.assertEqual(len(writes), 1)
        page.locator('#appMessageClose').click()

    def test_resubmit_receipt_survives_closing_the_resource_panel(self):
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds):
                page, writes = self.page()
                pending = []

                def duplicate(route):
                    writes.append(route.request.post_data_json)
                    route.fulfill(status=409, json={
                        'ok': False, 'duplicate': True, 'request_id': 71,
                        'existing_status': 'failed', 'can_resubmit': True,
                        'resubmit_target': 'guangya', 'error': '已有历史任务',
                    })

                def hold_resubmit(route):
                    writes.append(route.request.post_data_json)
                    pending.append(route)

                page.route('**/api/indexers/download', duplicate)
                page.route('**/api/indexers/download/resubmit', hold_resubmit)
                page.locator('[data-resource-submit-target="guangya"]').first.click()
                page.get_by_role('button', name='重新提交', exact=True).click()
                self.assertEqual(len(pending), 1)
                self.assertEqual(writes[-1], {'request_id': 71, 'target': 'guangya'})
                page.keyboard.press('Escape')
                page.wait_for_function("document.querySelector('#discovery-detail-body').childElementCount === 0")
                if succeeds:
                    pending[0].fulfill(json={'ok': True, 'request_id': 88, 'succeeded': ['guangya']})
                else:
                    pending[0].fulfill(status=400, json={'ok': False, 'error': '测试后端拒绝提交'})
                page.locator('#appMessageModal[open]').wait_for()
                self.assertEqual(page.locator('#appMessageTitle').inner_text(), '已重新提交' if succeeds else '重新提交失败')
                self.assertIn('88' if succeeds else '测试后端拒绝提交', page.locator('#appMessageText').inner_text())
                self.assertEqual(len(writes), 2)
                self.assertFalse(page.locator('#discovery-detail-dialog').evaluate('el => el.open'))
                self.assertEqual(page.locator('#discovery-detail-body').evaluate('el => el.childElementCount'), 0)
                page.locator('#appMessageClose').click()
                page.close()

    def test_message_stacks_above_confirmation_without_closing_parent_dialog(self):
        page, writes = self.page()
        page.locator('[data-resource-batch-target="guangya"]').click()
        page.evaluate('''() => { window.appAlert({message:'嵌套消息'}); }''')
        self.assert_frontmost(page, 'appMessageModal')
        page.keyboard.press('Escape')
        self.assertTrue(page.locator('#appMessageModal').is_hidden())
        self.assert_frontmost(page, 'appConfirmModal')
        page.keyboard.press('Escape')
        self.assertTrue(page.locator('#appConfirmModal').is_hidden())
        self.assertTrue(page.locator('#discovery-detail-dialog').evaluate('el => el.open'))
        self.assertEqual(writes, [])

    def test_native_cancel_busy_guard_and_late_close_do_not_lose_active_confirmation(self):
        page, writes = self.page()
        page.evaluate('''() => {
            window.confirmed=null;
            window.appConfirm({onConfirm:()=>new Promise(resolve=>window.finishEffect=resolve)}).then(value=>window.confirmed=value);
        }''')
        page.locator('#appConfirmSubmit').click()
        page.keyboard.press('Escape')
        page.locator('#appConfirmModal').dispatch_event('cancel', {'cancelable': True})
        self.assert_frontmost(page, 'appConfirmModal')
        self.assertIsNone(page.evaluate('window.confirmed'))
        page.evaluate('window.finishEffect(true)')
        page.wait_for_function('window.confirmed === true')
        page.evaluate('''() => {
            window.appConfirm({title:'旧确认'});
            document.getElementById('appConfirmModal').close();
            window.appConfirm({title:'新确认'});
        }''')
        page.wait_for_timeout(50)
        self.assert_frontmost(page, 'appConfirmModal')
        self.assertEqual(page.locator('#appConfirmTitle').inner_text(), '新确认')
        page.locator('#appConfirmCancel').click()
        self.assertEqual(writes, [])

    def test_non_native_browser_retains_shared_overlay_and_cancellation(self):
        page, writes = self.page(native=False)
        page.locator('[data-resource-batch-target="guangya"]').click()
        self.assertTrue(page.locator('#appConfirmModal').evaluate('''el => {
            const r=el.querySelector('.card').getBoundingClientRect();
            return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
        }'''))
        page.locator('#appConfirmCancel').click()
        self.assertTrue(page.locator('#appConfirmModal').is_hidden())
        self.assertTrue(page.locator('#discovery-detail-dialog').evaluate('el => el.open'))
        self.assertEqual(writes, [])

    def test_external_native_close_settles_confirmation_and_backdrop_preserves_selection(self):
        page, writes = self.page()
        page.evaluate('''() => {
            window.confirmed=null;
            window.appConfirm({title:'外部关闭'}).then(value=>window.confirmed=value);
            document.getElementById('appConfirmModal').close();
        }''')
        page.wait_for_function('window.confirmed === false')
        trigger=page.locator('[data-resource-batch-target="guangya"]')
        trigger.click()
        page.mouse.click(3, 3)
        self.assertTrue(page.locator('#appConfirmModal').is_hidden())
        self.assertTrue(page.locator('#discovery-detail-dialog').evaluate('el => el.open'))
        self.assertEqual(page.locator('[data-resource-result-id] input:checked').count(), 5)
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
