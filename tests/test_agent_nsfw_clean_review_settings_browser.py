"""真实设置模板/脚本的 NSFW 授权组件浏览器回归；不连接运行服务。"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 浏览器依赖可选，与现有浏览器测试一致
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
KEY = "AGENT_NSFW_CLEAN_REVIEW_ENABLED"
PARENT_KEY = "AGENT_RECOGNITION_REVIEW_ENABLED"
APP_SCRIPT = ROOT / "app/static/js/app.js"
SETTINGS_SCRIPT = ROOT / "app/static/js/settings.js"
TEMPLATE = (ROOT / "app/templates/settings.html").read_text("utf-8")
CONTENT = TEMPLATE.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
HARNESS = """<!doctype html><html lang="zh-CN" data-theme="light" data-settings-config="pending">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/main.css"><link rel="stylesheet" href="/static/css/settings-agent.css"></head>
<body class="settings-page"><main><div class="content">{content}</div></main></body></html>""".format(content=Environment(autoescape=True).from_string(CONTENT).render(
    app_version="test", resource_results_enabled=False,
))
MOCK_FETCH = r"""(config) => {
    window.__mediafluxInitialSettingsTarget = 'metadata';
    window.__settingsConfig = config;
    window.__settingsWrites = [];
    window.fetch = async (url, options = {}) => {
        if (String(url) !== '/api/config') throw new Error('未授权的浏览器测试请求: ' + url);
        if ((options.method || 'GET') === 'GET') {
            return new Promise(resolve => {
                window.__resolveSettingsConfig = () => resolve(new Response(JSON.stringify(window.__settingsConfig), {
                    status: 200, headers: {'Content-Type': 'application/json'},
                }));
            });
        }
        window.__settingsWrites.push(JSON.parse(options.body));
        return new Promise(resolve => {
            window.__resolveSettingsSave = () => resolve(new Response(JSON.stringify({success: true}), {
                status: 200, headers: {'Content-Type': 'application/json'},
            }));
        });
    };
}"""


@unittest.skipUnless(sync_playwright is not None, "未安装 Playwright")
class NsfwCleanReviewSettingsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert sync_playwright is not None
        cls.playwright = sync_playwright().start()
        candidates = [
            Path(os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or cls.playwright.chromium.executable_path),
            *sorted((Path.home() / ".cache/ms-playwright").glob("chromium-*/chrome-linux*/chrome"), reverse=True),
            Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"),
        ]
        browser_path = next((str(path) for path in candidates if path.is_file()), None)
        if not browser_path:
            cls.playwright.stop()
            raise unittest.SkipTest("未找到 Chrome/Chromium")
        try:
            cls.browser = cls.playwright.chromium.launch(
                headless=True, executable_path=browser_path, args=["--no-sandbox"],
            )
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def _page(self: unittest.TestCase, width, config, *, touch=False):
        page = getattr(self, "browser").new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce", has_touch=touch)
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
            "AGENT_LLM_ENABLED": "1",
            "AGENT_LLM_API_URL": "https://model.invalid/v1",
            "AGENT_LLM_MODEL": "fixture-model",
            "TMDB_MATCH_MODE": "strict",
            "ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT": "20",
            "AI_RECOGNITION_CONFIDENCE_THRESHOLD": "0.8",
            "AI_RECOGNITION_REQUESTS_PER_MINUTE": "6",
            "AI_RECOGNITION_DAILY_REQUEST_LIMIT": "100",
            "AI_RECOGNITION_MAX_CONCURRENCY": "2",
            "AI_RECOGNITION_CIRCUIT_BREAKER_SECONDS": "60",
            # /api/config 保证返回研究默认值；缺失会让空初值与后补的 10 不一致，
            # 被误当作未保存更改。仅同步 fixture，不放宽 NSFW 的精确 payload 断言。
            "AGENT_EPISODE_RESEARCH_ENABLED": "0",
            "AGENT_EPISODE_RESEARCH_DAILY_LIMIT": "10",
            **config,
        })
        page.add_script_tag(path=str(APP_SCRIPT))
        page.add_script_tag(path=str(SETTINGS_SCRIPT))
        page.wait_for_function("typeof window.__resolveSettingsConfig === 'function'")
        return page, errors

    @staticmethod
    def _resolve_config(page):
        page.evaluate("window.__resolveSettingsConfig()")
        page.wait_for_function("document.getElementById('settingsForm').getAttribute('aria-busy') === 'false'")
        page.evaluate("document.fonts.ready")
        page.locator(".metadata-nsfw-clean-review").wait_for(state="visible")

    @staticmethod
    def _rect(locator):
        return locator.evaluate("""element => {
            const rect = element.getBoundingClientRect();
            return {x: rect.x + scrollX, y: rect.y + scrollY, width: rect.width, height: rect.height};
        }""")

    def _assert_same_rect(self: unittest.TestCase, before, after):
        for key in ("x", "y", "width", "height"):
            self.assertAlmostEqual(before[key], after[key], delta=0.5, msg=f"{key}: {before} → {after}")

    def test_320_390_desktop_load_toggle_and_save_preserve_component_geometry(self):
        for width in (320, 390, 1280):
            with self.subTest(width=width):
                page, errors = self._page(width, {PARENT_KEY: "1"})
                option = page.locator(".metadata-nsfw-clean-review")
                child = page.locator(f'[data-key="{KEY}"]')
                parent = page.locator(f'[data-key="{PARENT_KEY}"]')
                before_load = self._rect(option)
                self.assertTrue(child.is_disabled())
                self._resolve_config(page)
                self.assertFalse(child.is_checked())
                self.assertFalse(child.is_disabled())
                self._assert_same_rect(before_load, self._rect(option))
                bounding_box_result = option.bounding_box()
                assert bounding_box_result is not None
                bounding_box_result_2 = option.bounding_box()
                assert bounding_box_result_2 is not None
                self.assertLessEqual(bounding_box_result["x"] + bounding_box_result_2["width"], width + 0.5)
                bounding_box_result_3 = option.locator("label.toggle").bounding_box()
                assert bounding_box_result_3 is not None
                self.assertGreaterEqual(bounding_box_result_3["width"], 44)
                bounding_box_result_4 = option.locator("label.toggle").bounding_box()
                assert bounding_box_result_4 is not None
                self.assertGreaterEqual(bounding_box_result_4["height"], 44)
                self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))

                parent.locator("..").click()
                self.assertTrue(child.is_disabled())
                self.assertTrue(option.is_visible())
                after_disable = self._rect(option)
                parent.locator("..").click()
                self.assertFalse(child.is_disabled())
                self._assert_same_rect(after_disable, self._rect(option))
                child.locator("..").click()
                self.assertTrue(child.is_checked())
                payload = page.evaluate("collectConfigFields(document.getElementById('settings-panel-metadata'))")
                self.assertEqual(payload, {KEY: "1"})

                button = page.locator('#settings-panel-metadata [data-save-settings]')
                button.scroll_into_view_if_needed()
                page.mouse.move(0, 0)
                rest_state = """() => {
                    const button = document.querySelector('#settings-panel-metadata [data-save-settings]');
                    const transform = getComputedStyle(button).transform;
                    return transform === 'none' || new DOMMatrixReadOnly(transform).isIdentity;
                }"""
                page.wait_for_function(rest_state)
                before_save = button.bounding_box()
                button.click()
                # 去掉既有 hover 的 1px transform，只比较加载/完成状态本身。
                page.mouse.move(0, 0)
                page.wait_for_function("typeof window.__resolveSettingsSave === 'function'")
                self.assertTrue(button.is_disabled())
                # mousemove 的返回不保证 hover 样式已经复位；等真实 transform
                # 回到静止态，不放宽几何容差，也不跳过 loading 状态断言。
                page.wait_for_function(rest_state)
                self._assert_same_rect(before_save, button.bounding_box())
                page.evaluate("window.__resolveSettingsSave()")
                page.wait_for_function("!document.querySelector('#settings-panel-metadata [data-save-settings]').disabled")
                self._assert_same_rect(before_save, button.bounding_box())
                self.assertEqual(page.evaluate("window.__settingsWrites"), [{KEY: "1"}])
                self.assertEqual(page.evaluate("collectConfigFields(document.getElementById('settings-panel-metadata'))"), {})
                self.assertEqual(errors, [])

                artifact_dir = os.getenv("MEDIAFLUX_SETTINGS_TEST_ARTIFACTS")
                if artifact_dir:
                    directory = Path(artifact_dir)
                    directory.mkdir(parents=True, exist_ok=True)
                    option.screenshot(path=str(directory / f"nsfw-settings-{width}.png"))
                    (directory / f"nsfw-settings-{width}.json").write_text(json.dumps({
                        "width": width, "before_load": before_load,
                        "option_rect": self._rect(option), "save_rect": before_save,
                        "page_errors": errors, "overflow": False,
                    }, ensure_ascii=False, indent=2), "utf-8")
                page.close()

    def test_parent_off_keeps_stored_child_authorization_visible_but_inactive(self):
        page, errors = self._page(390, {PARENT_KEY: "0", KEY: "1"})
        self._resolve_config(page)
        child = page.locator(f'[data-key="{KEY}"]')
        self.assertTrue(child.is_disabled())
        self.assertTrue(child.is_checked())
        self.assertTrue(page.locator(".metadata-nsfw-clean-review").is_visible())
        self.assertNotIn(KEY, page.evaluate("collectConfigFields(document.getElementById('settings-panel-metadata'))"))
        self.assertEqual(errors, [])

    def test_environment_boolean_aliases_match_runtime_permission_and_keyboard_focus(self):
        page, errors = self._page(390, {PARENT_KEY: " ON ", KEY: "y"})
        self._resolve_config(page)
        child = page.locator(f'[data-key="{KEY}"]')
        self.assertTrue(child.is_checked())
        self.assertFalse(child.is_disabled())
        child.focus()
        page.keyboard.press("Space")
        self.assertFalse(child.is_checked())
        self.assertEqual(child.locator("..").locator(".toggle-slider").evaluate(
            "element => getComputedStyle(element).outlineStyle"
        ), "solid")
        self.assertEqual(page.evaluate("collectConfigFields(document.getElementById('settings-panel-metadata'))"), {KEY: "0"})
        self.assertEqual(errors, [])

    def test_parent_toggle_cannot_unlock_environment_managed_child_permission(self):
        page, errors = self._page(390, {PARENT_KEY: "1", KEY: "0", "__managed_fields": [KEY]})
        self._resolve_config(page)
        child = page.locator(f'[data-key="{KEY}"]')
        parent = page.locator(f'[data-key="{PARENT_KEY}"]')
        self.assertTrue(child.is_disabled())
        parent.locator("..").click()
        parent.locator("..").click()
        self.assertTrue(child.is_disabled())
        self.assertFalse(child.is_checked())
        self.assertEqual(page.evaluate("collectConfigFields(document.getElementById('settings-panel-metadata'))"), {})
        self.assertEqual(errors, [])


    def test_episode_research_dependencies_preserve_saved_authorization_and_quota(self):
        enabled, limit = "AGENT_EPISODE_RESEARCH_ENABLED", "AGENT_EPISODE_RESEARCH_DAILY_LIMIT"
        for width in (390, 1280):
            with self.subTest(width=width):
                page, errors = self._page(width, {PARENT_KEY: "1", enabled: "1", limit: "17"})
                page.set_default_timeout(5000)
                toggle = page.locator(f'[data-key="{enabled}"]')
                quota = page.locator(f'[data-key="{limit}"]')
                self.assertTrue(toggle.is_disabled())
                self.assertTrue(quota.is_disabled())
                self._resolve_config(page)
                option = page.locator('.metadata-episode-research')
                before = self._rect(option)
                for key, panel in (("AGENT_LLM_ENABLED", "agent"), ("AGENT_LLM_API_URL", "agent"),
                                   ("AGENT_LLM_MODEL", "agent"), (PARENT_KEY, "metadata")):
                    page.locator(f'[data-settings-target="{panel}"]').click()
                    parent = page.locator(f'[data-key="{key}"]')
                    checkbox = parent.get_attribute('type') == 'checkbox'
                    saved = "" if checkbox else parent.input_value()
                    if checkbox:
                        parent.locator('..').click()
                    else:
                        parent.fill('')
                    page.locator('[data-settings-target="metadata"]').click()
                    self.assertTrue(toggle.is_disabled(), key)
                    self.assertTrue(quota.is_disabled(), key)
                    self.assertTrue(toggle.is_checked())
                    self.assertEqual(quota.input_value(), '17')
                    self._assert_same_rect(before, self._rect(option))
                    page.locator(f'[data-settings-target="{panel}"]').click()
                    if checkbox:
                        parent.locator('..').click()
                    else:
                        parent.fill(saved)
                    page.locator('[data-settings-target="metadata"]').click()
                    self.assertFalse(toggle.is_disabled(), key)
                    self.assertFalse(quota.is_disabled(), key)
                toggle.locator('..').click()
                self.assertTrue(quota.is_disabled())
                self.assertEqual(quota.input_value(), '17')
                toggle.locator('..').click()
                quota.fill('19')
                page.locator('#settings-panel-metadata [data-save-settings]').click()
                page.wait_for_function("typeof window.__resolveSettingsSave === 'function'")
                self.assertEqual(page.evaluate('window.__settingsWrites'), [{limit: '19'}])
                page.evaluate('window.__resolveSettingsSave()')
                page.wait_for_function("!document.querySelector('#settings-panel-metadata [data-save-settings]').disabled")
                self.assertEqual(page.evaluate("collectConfigFields(document.getElementById('settings-panel-metadata'))"), {})
                self.assertEqual(errors, [])
                page.close()
        for managed in (enabled, limit):
            page, errors = self._page(390, {PARENT_KEY: "1", enabled: "1", limit: "17", "__managed_fields": [managed]})
            self._resolve_config(page)
            parent = page.locator(f'[data-key="{PARENT_KEY}"]')
            parent.locator('..').click()
            parent.locator('..').click()
            self.assertTrue(page.locator(f'[data-key="{managed}"]').is_disabled())
            self.assertTrue(page.locator(f'[data-key="{enabled}"]').is_checked())
            self.assertEqual(page.locator(f'[data-key="{limit}"]').input_value(), '17')
            self.assertEqual(page.evaluate("collectConfigFields(document.getElementById('settings-panel-metadata'))"), {})
            self.assertEqual(errors, [])
            page.close()

if __name__ == "__main__":
    unittest.main()
