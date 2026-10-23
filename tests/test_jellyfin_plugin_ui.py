"""真实 Chrome 执行插件配置页；API 响应为受控替身，不连接生产。"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.test_agent_kernel_browser import _chromium_executable, sync_playwright


PAGE = Path(__file__).resolve().parents[1] / "plugins/MediaFlux.Jellyfin/Configuration/configPage.html"
API = """() => {
    window.settings = {
        enabled: false, mf_url: 'http://mf.invalid', username: 'admin', password_set: true,
        task_timeout_seconds: 120, active_mappings: [{local:'/data/strm',server:'/media/strm'}],
        libraries: [], synced_at_utc: '2026-10-23T12:00:00Z', cache_current: true,
        last_delete_error: '', compiled_baseline: '12.2.0', source_checked_versions: '12.2 / 12.3'
    };
    window.requests = []; window.fail = false; window.hold = false;
    window.ApiClient = {
        getUrl: path => path,
        ajax: request => {
            const body = request.data ? JSON.parse(request.data) : null;
            requests.push({path:request.url,method:request.type,body});
            const respond = () => {
                if (window.fail) throw {responseText:JSON.stringify({message:'MF 暂时无法连接'})};
                if (request.url.endsWith('/Test')) return {message:'连接成功；未执行删除。'};
                if (body) {
                    const {password, ...saved} = body;
                    Object.assign(settings, saved);
                    settings.password_set = settings.password_set || !!password;
                }
                return structuredClone(settings);
            };
            return (window.hold ? new Promise(resolve => window.release = resolve) : Promise.resolve()).then(respond);
        }
    };
} """


@unittest.skipUnless(sync_playwright is not None, "未安装 Playwright")
class JellyfinPluginBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("未安装 Playwright")
        cls.playwright = sync_playwright().start()
        executable = _chromium_executable(cls.playwright)
        if not executable:
            cls.playwright.stop()
            raise unittest.SkipTest("未安装 Chrome/Chromium")
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1100, "height": 1100})
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.evaluate(API)
        self.page.set_content(PAGE.read_text(encoding="utf-8"))
        self.page.locator("#MediaFluxConfigPage").dispatch_event("pageshow")
        self.page.wait_for_function("!document.querySelector('#mfFields').disabled")

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def test_single_synced_scope_logo_and_mobile_layout(self):
        self.assertEqual(self.page.locator("h2").inner_text(), "MediaFlux · STRM 联动删除")
        self.assertEqual(self.page.locator("select, textarea").count(), 0)
        self.assertEqual(self.page.locator("#mfPassword").input_value(), "")
        self.assertIn("/media/strm", self.page.locator("#mfScope").inner_text())
        self.assertFalse(self.page.locator("#mfEnabled").is_checked())
        for width in (1100, 360):
            self.page.set_viewport_size({"width": width, "height": 1100})
            logo = self.page.get_by_role("img", name="MediaFlux").bounding_box()
            self.assertIsNotNone(logo)
            self.assertEqual((logo["width"], logo["height"]), (44, 44))
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))

    def test_save_enable_sync_and_connection_never_send_manual_mapping(self):
        self.page.locator("#mfPassword").fill("test-only-password")
        self.page.locator("#mfEnabled").check()
        self.page.get_by_role("button", name="保存配置", exact=True).click()
        self.page.wait_for_function("document.querySelector('#mfStatus').textContent.startsWith('配置已保存')")
        body = self.page.evaluate("requests.at(-1).body")
        self.assertEqual(set(body), {"enabled", "mf_url", "username", "password", "task_timeout_seconds"})
        self.assertTrue(body["enabled"])
        self.assertEqual(self.page.locator("#mfPassword").input_value(), "")
        self.page.get_by_role("button", name="保存配置", exact=True).click()
        self.page.wait_for_function("requests.length === 3 && !document.querySelector('#mfFields').disabled")
        self.assertIsNone(self.page.evaluate("requests.at(-1).body.password"))
        self.page.locator("#mfSync").click()
        self.page.wait_for_function("document.querySelector('#mfStatus').textContent.startsWith('MF 范围同步成功')")
        self.page.locator("#mfTest").click()
        self.page.wait_for_function("document.querySelector('#mfStatus').textContent.startsWith('连接成功')")
        self.assertEqual(self.page.evaluate("requests.map(r => r.path)"), [
            "MediaFlux/Settings", "MediaFlux/Settings", "MediaFlux/Settings", "MediaFlux/Sync", "MediaFlux/Test"
        ])

    def test_pending_sync_prevents_duplicate_submits_preserves_scope_and_buttons(self):
        before = self.page.locator("#mfSync").bounding_box()
        scope = self.page.locator("#mfScope").inner_text()
        self.page.evaluate("window.hold = true")
        self.page.locator("#mfSync").click()
        self.page.wait_for_function("typeof window.release === 'function'")
        self.assertTrue(self.page.locator("#mfSync").is_disabled())
        self.assertEqual(self.page.locator("#mfSync").bounding_box(), before)
        self.assertEqual(self.page.locator("#mfScope").inner_text(), scope)
        self.page.locator("#MediaFluxConfigForm").dispatch_event("submit")
        self.assertEqual(self.page.evaluate("requests.length"), 2)
        self.page.evaluate("window.fail = true; release()")
        self.page.wait_for_function("!document.querySelector('#mfFields').disabled")
        self.assertEqual(self.page.locator("#mfStatus").inner_text(), "MF 暂时无法连接")
        self.assertEqual(self.page.locator("#mfScope").inner_text(), scope)
        self.page.evaluate("window.hold = false; window.fail = false")
        self.page.locator("#mfSync").click()
        self.page.wait_for_function("document.querySelector('#mfStatus').textContent.startsWith('MF 范围同步成功')")

    def test_refresh_does_not_accumulate_event_handlers(self):
        self.page.locator("#MediaFluxConfigPage").dispatch_event("pageshow")
        self.page.wait_for_function("requests.length === 2 && !document.querySelector('#mfFields').disabled")
        self.page.locator("#mfTest").click()
        self.page.wait_for_function("document.querySelector('#mfStatus').textContent.startsWith('连接成功')")
        self.assertEqual(self.page.evaluate("requests.length"), 3)
