"""配置回填与保存：真实应用 + Chromium，全离线。

页面、静态资源和 /api 请求全部转发给进程内 TestClient 上的真实应用（真实安装、
user.env、路由、模板与 JS）；进程内对外 socket 连接被禁止。验证缺失默认、显式空值
和非标准真值经 GET → fillConfigFields 回填的结果，以及 Cron 清空后保存被拒绝、草稿
保留、配置文件不变。
"""
from __future__ import annotations

import socket
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app import config
from app.defaults import DEFAULT_BOOL_CONFIG_VALUES
from app.main import create_app
from tests.support import initialized_web_runtime

try:
    from .test_agent_kernel_browser import _chromium_executable, sync_playwright
    from .test_config_empty_values_api import login, write_raw
except ImportError:
    from test_agent_kernel_browser import _chromium_executable, sync_playwright
    from test_config_empty_values_api import login, write_raw

ORIGIN = "http://app.test"
ORGANIZE_DEFAULT_ON = frozenset(key for key, value in DEFAULT_BOOL_CONFIG_VALUES.items() if key.startswith("GY_ORGANIZE_") and value)
READY = {
    "/organize": "document.getElementById('saveOrganizeConfigBtn')?.getAttribute('aria-busy') === 'false'",
    "/organize-rules": "document.getElementById('saveOrganizeConfigBtn')?.getAttribute('aria-busy') === 'false'",
    "/guangya/strm": "!document.getElementById('saveStrmBtn')?.disabled",
    "/guangya/offline": "!document.getElementById('saveOfflineBtn')?.disabled",
    "/settings": "document.getElementById('settingsForm')?.getAttribute('aria-busy') === 'false'",
}


class ConfigBackfillBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert sync_playwright is not None
        cls.playwright = cls.enterClassContext(sync_playwright())
        executable = _chromium_executable(cls.playwright)
        if executable is None:
            raise unittest.SkipTest("未找到 Chromium")
        cls.browser = cls.playwright.chromium.launch(
            executable_path=executable, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking"],
        )
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.enterContext(initialized_web_runtime())
        real_connect = socket.socket.connect

        def offline_connect(sock, address):
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in {"127.0.0.1", "::1", "localhost"}:
                raise OSError("offline browser test: outbound connection blocked")
            return real_connect(sock, address)

        self.enterContext(patch.object(socket.socket, "connect", offline_connect))
        self.client = TestClient(create_app(), raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        login(self.client)  # 真实登录；页面脚本自带 CSRF 头，经代理原样转发
        self.config_posts: list[tuple[int, dict]] = []
        self.errors: list[str] = []
        self.unexpected: list[str] = []
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 900}, service_workers="block", reduced_motion="reduce",
        )
        self.addCleanup(self.context.close)
        self.context.route("**/*", self._proxy)

    def tearDown(self):
        self.assertEqual(self.errors, [], "页面不能有未处理脚本异常")
        self.assertEqual(self.unexpected, [], "浏览器只能访问本地应用")

    def _proxy(self, route):
        request = route.request
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            self.unexpected.append(request.url)
            route.abort()
            return
        headers = {
            key: value for key, value in request.headers.items()
            if key.lower() in {"content-type", "x-csrf-token", "accept"}
        }
        response = self.client.request(
            request.method, url.path + (f"?{url.query}" if url.query else ""),
            content=request.post_data_buffer, headers=headers, follow_redirects=False,
        )
        if request.method == "POST" and url.path == "/api/config":
            self.config_posts.append((response.status_code, request.post_data_json))
        route.fulfill(
            status=response.status_code,
            headers={"content-type": response.headers.get("content-type", "application/octet-stream")},
            body=response.content,
        )

    def _open(self, path: str):
        page = self.context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.goto(ORIGIN + path)
        page.wait_for_function(READY[path])
        return page

    @staticmethod
    def _write(**values: str) -> None:
        write_raw(**values)

    @staticmethod
    def _checked(page, key: str) -> bool:
        return page.locator(f'[data-key="{key}"]').first.is_checked()

    def test_organize_both_views_show_missing_defaults_explicit_empty_and_truthy_text(self):
        self._write(
            GY_ORGANIZE_EMBY_REFRESH="", GY_ORGANIZE_CLEAN_EMPTY="", GY_ORGANIZE_SCHEDULE_CRON="",
            GY_ORGANIZE_REGION_SPLIT=" y ", GY_ORGANIZE_NOTIFY_ENABLED="On",
        )
        rules = self._open("/organize-rules")
        self.assertFalse(self._checked(rules, "GY_ORGANIZE_EMBY_REFRESH"))
        self.assertFalse(self._checked(rules, "GY_ORGANIZE_CLEAN_EMPTY"))
        self.assertTrue(self._checked(rules, "GY_ORGANIZE_REGION_SPLIT"))
        self.assertTrue(self._checked(rules, "GY_ORGANIZE_NOTIFY_ENABLED"))
        # 缺失键由 GET 回填后端唯一默认（开启），页面不再自行补勾选。
        for key in ("GY_ORGANIZE_YEAR_SPLIT", "GY_ORGANIZE_LINK_STRM", "GY_ORGANIZE_STRM_DETAIL_NOTIFY"):
            self.assertTrue(self._checked(rules, key), key)
        self.assertEqual(rules.locator('[data-key="GY_ORGANIZE_SCHEDULE_CRON"]').input_value(), "")

        execute = self._open("/organize")
        self.assertFalse(self._checked(execute, "GY_ORGANIZE_CLEAN_EMPTY"))
        self.assertEqual(self.config_posts, [], "仅加载页面不能写配置")
        self.assertIn("GY_ORGANIZE_EMBY_REFRESH=''", config.ENV_FILE.read_text(encoding="utf-8"))

    def test_organize_rules_rejects_clearing_cron_and_keeps_draft(self):
        self._write(GY_ORGANIZE_SCHEDULE_ENABLED="1")  # Cron 输入只在启用定时后可编辑
        rules = self._open("/organize-rules")
        rules.locator('[data-tab-target="delivery"]').click()
        cron = rules.locator('[data-key="GY_ORGANIZE_SCHEDULE_CRON"]')
        self.assertEqual(cron.input_value(), "0 4 * * *")
        before = config.ENV_FILE.read_bytes()
        cron.fill("")
        rules.locator("#saveOrganizeConfigBtn").click()
        rules.wait_for_function("document.getElementById('organizeSaveState')?.classList.contains('is-error')")
        self.assertEqual([status for status, _ in self.config_posts], [400])
        submitted = self.config_posts[0][1]
        self.assertEqual(submitted["GY_ORGANIZE_SCHEDULE_CRON"], "")
        # 布尔开关已由 GET 回填且未改动，稀疏保存不能把它们带上。
        self.assertFalse([key for key in submitted if key in ORGANIZE_DEFAULT_ON], submitted)
        self.assertIn("GY_ORGANIZE_SCHEDULE_CRON", rules.locator("#organizeSaveState").inner_text())
        self.assertEqual(cron.input_value(), "", "失败后保留草稿")
        self.assertEqual(config.ENV_FILE.read_bytes(), before)
        cron.fill("30 5 * * *")
        rules.locator("#saveOrganizeConfigBtn").click()
        rules.wait_for_function("document.getElementById('organizeSaveState')?.classList.contains('is-success')")
        self.assertEqual(self.config_posts[-1][0], 200)
        self.assertEqual(self.config_posts[-1][1]["GY_ORGANIZE_SCHEDULE_CRON"], "30 5 * * *")
        self.assertEqual(config.get("GY_ORGANIZE_SCHEDULE_CRON"), "30 5 * * *")

    def test_strm_page_keeps_explicit_empty_and_rejects_clearing_cron(self):
        self._write(STRM_NOTIFY_ENABLED="", STRM_SCHEDULE_CRON="")
        page = self._open("/guangya/strm")
        self.assertFalse(self._checked(page, "STRM_NOTIFY_ENABLED"))
        cron = page.locator('[data-key="STRM_SCHEDULE_CRON"]')
        self.assertEqual(cron.input_value(), "", "历史空 Cron 保持可见，不自动补值")
        self.assertEqual(self.config_posts, [])

        self._write(STRM_SCHEDULE_CRON="0 4 * * *", STRM_SCHEDULE_ENABLED="1")
        page = self._open("/guangya/strm")
        page.locator("#strmTabButton_schedule").click()
        before = config.ENV_FILE.read_bytes()
        page.locator('[data-key="STRM_SCHEDULE_CRON"]').fill("   ")
        page.locator("#saveStrmBtn").click()
        page.wait_for_function("document.getElementById('strmSaveState')?.classList.contains('is-error')")
        self.assertEqual([status for status, _ in self.config_posts], [400])
        self.assertEqual(page.locator('[data-key="STRM_SCHEDULE_CRON"]').input_value(), "   ")
        self.assertEqual(config.ENV_FILE.read_bytes(), before)

    def test_offline_and_settings_use_the_shared_truth_values(self):
        self._write(
            OFFLINE_MAGNET_ENABLED="", AGENT_STREAM_DISPLAY_ENABLED=" YES ",
            WEB_READ_ENABLED="", WEB_SEARCH_ENABLED="y",
        )
        offline = self._open("/guangya/offline")
        self.assertFalse(self._checked(offline, "OFFLINE_MAGNET_ENABLED"), "显式空值不能被页面重新勾选")
        self.assertTrue(self._checked(offline, "OFFLINE_ED2K_ENABLED"), "缺失键由 GET 回填默认开启")
        settings = self._open("/settings")
        self.assertTrue(self._checked(settings, "AGENT_STREAM_DISPLAY_ENABLED"))
        self.assertFalse(self._checked(settings, "WEB_READ_ENABLED"))
        self.assertTrue(self._checked(settings, "WEB_SEARCH_ENABLED"))
        self.assertEqual(self.config_posts, [])


if __name__ == "__main__":
    unittest.main()
