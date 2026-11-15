"""豆瓣 dbcl2 设置状态的浏览器回归；不连接豆瓣，也不使用真实 Cookie。"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - 浏览器依赖可选
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
APP_SCRIPT = ROOT / "app/static/js/app.js"
SETTINGS_SCRIPT = ROOT / "app/static/js/settings.js"
TEMPLATE = (ROOT / "app/templates/settings.html").read_text("utf-8")
CONTENT = TEMPLATE.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
HARNESS = """<!doctype html><html lang="zh-CN" data-theme="light" data-settings-config="pending">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/main.css"><link rel="stylesheet" href="/static/css/settings-agent.css"></head>
<body class="settings-page"><main><div class="content">{content}</div></main></body></html>""".format(
    content=Environment(autoescape=True).from_string(CONTENT).render(
        app_version="test", resource_results_enabled=False,
    ),
)


MOCK_FETCH = r"""(fixture) => {
    window.__settingsConfig = fixture.config || {};
    window.__doubanStatus = fixture.status || 'unknown';
    window.__doubanStatusError = Boolean(fixture.status_error);
    window.__doubanStatusCalls = [];
    window.__doubanStatusResolvers = [];
    window.__settingsWrites = [];
    window.fetch = async (url, options = {}) => {
        const path = String(url);
        if (path === '/api/config') {
            if ((options.method || 'GET') === 'GET') {
                return new Response(JSON.stringify(window.__settingsConfig), {
                    status: 200, headers: {'Content-Type': 'application/json'},
                });
            }
            window.__settingsWrites.push(JSON.parse(options.body));
            return new Response(JSON.stringify({success: true}), {
                status: 200, headers: {'Content-Type': 'application/json'},
            });
        }
        if (path === '/api/douban/dbcl2/status') {
            window.__doubanStatusCalls.push(path);
            if (fixture.status_deferred) {
                return new Promise(resolve => window.__doubanStatusResolvers.push(resolve));
            }
            if (window.__doubanStatusError) {
                return new Response(JSON.stringify({error: '状态暂时无法确认'}), {
                    status: 504, headers: {'Content-Type': 'application/json'},
                });
            }
            return new Response(JSON.stringify({status: window.__doubanStatus}), {
                status: 200, headers: {'Content-Type': 'application/json'},
            });
        }
        throw new Error('未授权的浏览器测试请求: ' + path);
    };
    window.__resolveDoubanStatus = (index, status) => {
        const resolve = window.__doubanStatusResolvers[index];
        if (!resolve) throw new Error('缺少延迟状态请求: ' + index);
        resolve(new Response(JSON.stringify({status}), {
            status: 200, headers: {'Content-Type': 'application/json'},
        }));
    };
}"""


@unittest.skipUnless(sync_playwright is not None, "未安装 Playwright")
class DoubanDbcl2SettingsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert sync_playwright is not None
        cls.playwright = cls.enterClassContext(sync_playwright())
        candidates = [
            Path(os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or cls.playwright.chromium.executable_path),
            *sorted((Path.home() / ".cache/ms-playwright").glob("chromium-*/chrome-linux*/chrome"), reverse=True),
            Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"),
        ]
        browser_path = next((str(path) for path in candidates if path.is_file()), None)
        if not browser_path:
            raise unittest.SkipTest("未找到 Chrome/Chromium")
        cls.browser = cls.playwright.chromium.launch(
            headless=True, executable_path=browser_path, args=["--no-sandbox"],
        )
        cls.addClassCleanup(cls.browser.close)

    def _page(self, width, *, status="unknown", status_error=False, status_deferred=False):
        page = self.browser.new_page(
            viewport={"width": width, "height": 900},
            reduced_motion="reduce",
            has_touch=width < 600,
        )
        self.addCleanup(page.close)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_request(route):
            path = urlparse(route.request.url).path
            if path == "/settings":
                route.fulfill(status=200, content_type="text/html", body=HARNESS)
            elif path.startswith("/static/"):
                file = (ROOT / "app" / path.lstrip("/")).resolve()
                if file.is_relative_to(ROOT / "app/static") and file.is_file():
                    route.fulfill(path=str(file))
                else:
                    route.abort()
            else:
                route.abort()

        page.route("**/*", route_request)
        page.goto("http://settings.test/settings")
        page.evaluate(MOCK_FETCH, {
            "status": status,
            "status_error": status_error,
            "status_deferred": status_deferred,
            "config": {
                "DISCOVERY_ENABLED": "1",
                "DISCOVERY_DOUBAN_ENABLED": "1",
                "DISCOVERY_RESOURCE_RESULTS_ENABLED": "1",
                "DOUBAN_CACHE_TTL_SECONDS": "21600",
                "DOUBAN_DBCL2": "",
            },
        })
        page.evaluate("window.__mediafluxInitialSettingsTarget = 'discovery'")
        page.add_script_tag(path=str(APP_SCRIPT))
        page.add_script_tag(path=str(SETTINGS_SCRIPT))
        page.wait_for_function("typeof window.__settingsConfig === 'object'")
        page.wait_for_function("document.getElementById('settingsForm').getAttribute('aria-busy') === 'false'")
        page.wait_for_function("window.__doubanStatusCalls.length === 1")
        return page, errors

    @staticmethod
    def _rect(locator):
        return locator.evaluate("""element => {
            const rect = element.getBoundingClientRect();
            return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
        }""")

    def test_status_labels_and_slot_remain_stable_on_mobile_and_desktop(self):
        for width in (320, 390, 1280):
            with self.subTest(width=width):
                page, errors = self._page(width, status="valid")
                status = page.locator("[data-douban-dbcl2-status]")
                cookie = page.locator('[data-key="DOUBAN_DBCL2"]')
                before = self._rect(status)

                self.assertEqual(status.text_content(), "· 有效")
                self.assertEqual(status.get_attribute("data-tone"), "valid")
                self.assertEqual(before["height"], self._rect(status)["height"])
                self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))

                cookie.fill("test-cookie-value")
                self.assertEqual(status.text_content(), "· 未确认")
                self.assertEqual(status.get_attribute("data-tone"), "unknown")
                self.assertAlmostEqual(before["width"], self._rect(status)["width"], delta=0.5)

                toggle = cookie.locator("..").locator(".secret-input-toggle")
                controls = (cookie, toggle, status, page.locator('#settings-panel-discovery [data-save-settings]'))
                cookie.evaluate("""input => {
                    window.__secretFormSubmits = 0;
                    input.form.addEventListener('submit', () => window.__secretFormSubmits++);
                }""")
                for key, input_type, pressed in (("Space", "text", "true"), ("Enter", "password", "false")):
                    # 进入键盘模式，验证显隐按钮的公共焦点样式及原生键盘激活。
                    page.keyboard.press("Tab")
                    toggle.focus()
                    expect(toggle).to_be_focused()
                    self.assertTrue(toggle.evaluate('button => button.matches(":focus-visible")'))
                    expect(toggle).not_to_have_css("outline-style", "none")
                    self.assertGreater(float(toggle.evaluate('el => getComputedStyle(el).outlineWidth').removesuffix('px')), 0)
                    geometry = [self._rect(control) for control in controls]
                    page.keyboard.press(key)
                    expect(cookie).to_have_attribute("type", input_type)
                    expect(toggle).to_have_attribute("aria-pressed", pressed)
                    expect(cookie).to_have_value("test-cookie-value")
                    self.assertEqual(page.evaluate("window.__secretFormSubmits"), 0)
                    self.assertEqual(page.evaluate("window.__settingsWrites"), [])
                    for control, rect in zip(controls, geometry):
                        for dimension, value in self._rect(control).items():
                            self.assertAlmostEqual(rect[dimension], value, delta=0.5)
                self.assertEqual(page.evaluate("window.__doubanStatusCalls.length"), 1)
                self.assertEqual(errors, [])

    def test_unconfigured_invalid_and_indeterminate_states_are_explicit(self):
        cases = (
            ("unconfigured", False, "· 未配置", "unknown"),
            ("invalid", False, "· 无效", "invalid"),
            ("unknown", True, "· 未确认", "unknown"),
        )
        for state, status_error, label, tone in cases:
            with self.subTest(state=state):
                page, errors = self._page(390, status=state, status_error=status_error)
                status = page.locator("[data-douban-dbcl2-status]")
                self.assertEqual(status.text_content(), label)
                self.assertEqual(status.get_attribute("data-tone"), tone)
                self.assertEqual(errors, [])

    def test_deferred_old_status_cannot_overwrite_edit_or_new_detection(self):
        page, errors = self._page(390, status="valid", status_deferred=True)
        status = page.locator("[data-douban-dbcl2-status]")
        cookie = page.locator('[data-key="DOUBAN_DBCL2"]')

        cookie.fill("edited-cookie")
        page.evaluate("window.__resolveDoubanStatus(0, 'valid')")
        page.wait_for_timeout(0)
        self.assertEqual(status.text_content(), "· 未确认")

        page.locator('#settings-panel-discovery [data-save-settings]').click()
        page.wait_for_function("window.__doubanStatusCalls.length === 2")

        page.evaluate("window.__resolveDoubanStatus(0, 'valid')")
        page.wait_for_timeout(0)
        self.assertEqual(status.text_content(), "· 未确认")

        page.evaluate("window.__resolveDoubanStatus(1, 'invalid')")
        page.wait_for_function("document.querySelector('[data-douban-dbcl2-status]').textContent === '· 无效'")
        self.assertEqual(status.get_attribute("data-tone"), "invalid")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
