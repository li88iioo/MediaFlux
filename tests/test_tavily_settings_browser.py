"""真实设置模板：Tavily 草稿、已存凭据、清空及请求竞态的浏览器验收。"""
import unittest

from tests import test_agent_nsfw_clean_review_settings_browser as fixture


@unittest.skipUnless(fixture.sync_playwright is not None, "未安装 Playwright")
class TavilySettingsBrowserTests(unittest.TestCase):
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

    def _open(self, width):
        page, errors = self._page(width, {"TAVILY_API_KEY": "********", "TAVILY_TIMEOUT_SECONDS": "10"})
        self.assertTrue(page.locator('#testTavilyBtn').is_disabled())
        self._resolve_config(page)
        page.add_script_tag(path=str(fixture.ROOT / 'app/static/js/lucide.min.js'))
        page.evaluate('window.renderLucideIcons(document)')
        page.locator('[data-settings-target="agent"]').click()
        page.evaluate("""() => {
            const original = window.fetch;
            window.__tavilyCalls = [];
            window.__tavilyReplies = [];
            window.fetch = (url, options) => {
                if (url !== '/api/tools/tavily/test') return original(url, options);
                window.__tavilyCalls.push(JSON.parse(options.body));
                // 故意不响应 abort：模拟已经在途的旧响应，必须由草稿门丢弃。
                return new Promise(resolve => window.__tavilyReplies.push((status, data) =>
                    resolve(new Response(JSON.stringify(data), {status, headers:{'Content-Type':'application/json'}}))));
            };
        }""")
        return page, errors

    @staticmethod
    def _reply(page, index=0, *, status=200, data=None):
        page.evaluate("([index, status, data]) => window.__tavilyReplies[index](status, data)",
                      [index, status, data or {"ok": True, "message": "Tavily 连接正常，密钥有效", "elapsed_ms": 12}])

    def test_saved_key_test_has_stable_controls_on_desktop_and_mobile(self):
        for width in (320, 390, 1280):
            with self.subTest(width=width):
                page, errors = self._open(width)
                button = page.locator('#testTavilyBtn')
                key = page.locator('#tavilyApiKeyInput')
                state = page.locator('#tavilyConnectionState')
                button.scroll_into_view_if_needed()
                before = self._rect(button)
                key_rect = self._rect(key)
                self.assertGreater(key_rect['width'], 80)
                self.assertLessEqual(key_rect['x'] + key_rect['width'], before['x'])
                button.click()
                page.mouse.move(0, 0)
                self.assertTrue(button.is_disabled())
                self.assertEqual(button.get_attribute('aria-busy'), 'true')
                self._assert_same_rect(before, self._rect(button))
                self.assertEqual(page.evaluate('window.__tavilyCalls'), [{"api_key": "********", "timeout_seconds": "10"}])
                self._reply(page)
                page.wait_for_function("document.querySelector('#tavilyConnectionState').dataset.tone==='success'")
                self.assertIn('12 ms', state.inner_text())
                self.assertFalse(button.is_disabled())
                self._assert_same_rect(before, self._rect(button))
                self.assertEqual(page.evaluate('window.__settingsWrites'), [])
                self.assertEqual(key.input_value(), '')
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                self.assertEqual(errors, [])

    def test_local_read_controls_are_independent_responsive_and_save_only_their_draft(self):
        from app.routes.api import _UI_DEFAULTS

        for width in (320, 390, 1280):
            with self.subTest(width=width):
                page, errors = self._page(width, _UI_DEFAULTS)
                self._resolve_config(page)
                page.locator('[data-settings-target="agent"]').click()
                search = page.locator('[data-key="WEB_SEARCH_ENABLED"]')
                read = page.locator('[data-key="WEB_READ_ENABLED"]')
                self.assertFalse(search.is_checked())
                self.assertTrue(read.is_checked())
                self.assertFalse(read.is_disabled())
                option = read.locator('..').locator('..')
                option.scroll_into_view_if_needed()
                before = self._rect(option)
                read.locator('..').click()
                self.assertFalse(read.is_checked())
                self.assertFalse(search.is_checked())
                self._assert_same_rect(before, self._rect(option))
                page.locator('[data-key="WEB_READ_TIMEOUT_SECONDS"]').fill('15')
                page.locator('[data-key="WEB_READ_CACHE_TTL_SECONDS"]').fill('120')
                page.locator('#settings-panel-agent [data-save-settings]').click()
                page.wait_for_function('window.__settingsWrites.length === 1')
                self.assertEqual(page.evaluate('window.__settingsWrites[0]'), {
                    "WEB_READ_ENABLED": "0", "WEB_READ_TIMEOUT_SECONDS": "15", "WEB_READ_CACHE_TTL_SECONDS": "120",
                })
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                self.assertEqual(errors, [])

    def test_new_key_clear_and_stale_response_do_not_test_or_display_the_wrong_key(self):
        page, errors = self._open(1280)
        button = page.locator('#testTavilyBtn')
        key = page.locator('#tavilyApiKeyInput')
        state = page.locator('#tavilyConnectionState')
        key.locator('..').locator('.secret-input-clear').click()
        button.click()
        self.assertEqual(page.evaluate('window.__tavilyCalls[0].api_key'), '')
        self._reply(page, status=400, data={"error": "请先填写 Tavily API Key"})
        page.wait_for_function("document.querySelector('#tavilyConnectionState').dataset.tone==='error'")
        self.assertIn('请先填写', state.inner_text())
        key.fill('draft-one')
        button.click()
        key.fill('draft-two')
        self.assertFalse(button.is_disabled())
        button.click()
        self._reply(page, 1)
        self.assertEqual(state.get_attribute('data-tone'), 'testing')
        self.assertTrue(button.is_disabled())
        self._reply(page, 2, status=504, data={"error": "Tavily 响应超时，请检查网络"})
        page.wait_for_function("document.querySelector('#tavilyConnectionState').dataset.tone==='error'")
        self.assertIn('响应超时', state.inner_text())
        self.assertFalse(button.is_disabled())
        self.assertEqual(page.evaluate('window.__tavilyCalls.map(x => x.api_key)'), ['', 'draft-one', 'draft-two'])
        self.assertEqual(page.evaluate('window.__settingsWrites'), [])
        self.assertEqual(errors, [])
