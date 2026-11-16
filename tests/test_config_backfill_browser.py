"""配置回填与保存：真实应用 + Chromium，全离线。

页面、静态资源和 /api 请求全部转发给进程内 TestClient 上的真实应用（真实安装、
user.env、路由、模板与 JS）；进程内对外 socket 连接被禁止。验证缺失默认、显式空值
和非标准真值经 GET → fillConfigFields 回填的结果，以及 Cron 清空后保存被拒绝、草稿
保留、配置文件不变。
"""
from __future__ import annotations

import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app import config
from app.defaults import DEFAULT_BOOL_CONFIG_VALUES
from app.main import create_app
from tests.support import FakeTelegramBot, initialized_web_runtime

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
    "/rss": "typeof rssSubsLoaded !== 'undefined' && rssSubsLoaded === true",
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
        page.wait_for_function(READY[path.split("#")[0]])
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
            GY_ORGANIZE_REGION_SPLIT=" y ",
        )
        rules = self._open("/organize-rules")
        self.assertFalse(self._checked(rules, "GY_ORGANIZE_EMBY_REFRESH"))
        self.assertFalse(self._checked(rules, "GY_ORGANIZE_CLEAN_EMPTY"))
        self.assertTrue(self._checked(rules, "GY_ORGANIZE_REGION_SPLIT"))
        # 缺失键由 GET 回填后端唯一默认（开启），页面不再自行补勾选。
        for key in ("GY_ORGANIZE_YEAR_SPLIT", "GY_ORGANIZE_LINK_STRM"):
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
        self._write(STRM_SCHEDULE_CRON="")
        page = self._open("/guangya/strm")
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
            GY_ORGANIZE_NOTIFY_ENABLED="On", STRM_NOTIFY_ENABLED="",
        )
        offline = self._open("/guangya/offline")
        self.assertFalse(self._checked(offline, "OFFLINE_MAGNET_ENABLED"), "显式空值不能被页面重新勾选")
        self.assertTrue(self._checked(offline, "OFFLINE_ED2K_ENABLED"), "缺失键由 GET 回填默认开启")
        settings = self._open("/settings")
        self.assertTrue(self._checked(settings, "AGENT_STREAM_DISPLAY_ENABLED"))
        self.assertFalse(self._checked(settings, "WEB_READ_ENABLED"))
        self.assertTrue(self._checked(settings, "WEB_SEARCH_ENABLED"))
        self.assertTrue(self._checked(settings, "GY_ORGANIZE_NOTIFY_ENABLED"))
        self.assertTrue(self._checked(settings, "GY_ORGANIZE_STRM_DETAIL_NOTIFY"), "缺失通知键使用后端默认")
        self.assertFalse(self._checked(settings, "STRM_NOTIFY_ENABLED"), "通知移至设置页，显式空仍为关闭")
        self.assertEqual(self.config_posts, [])

    def test_rss_inherited_guangya_directory_matches_offline_default(self):
        # 与离线转存页一致：未保存或保存为 "0" 都显示未配置；已配置显示其名称。
        for values, expected in (
            ({}, ("继承全局目录", "未配置")),
            ({"OFFLINE_TARGET_DIR": "1939382533671788571", "OFFLINE_TARGET_DIR_NAME": "离线"},
             ("继承：离线", "离线转存默认目录")),
            ({"OFFLINE_TARGET_DIR": "0"}, ("继承全局目录", "未配置")),
        ):
            with self.subTest(values=values):
                if values:
                    self._write(**values)
                page = self._open("/rss#rss")
                page.locator(".subscription-rss-create").first.click()
                target = page.locator("#rssGyTargetValue")
                target.wait_for(state="visible")
                self.assertEqual(
                    (target.locator("strong").inner_text(), target.locator("span").inner_text()), expected,
                )
                page.close()
        self.assertEqual(self.config_posts, [])

    def test_media_agent_switch_greys_dependents_and_keeps_their_values(self):
        self._write(
            AGENT_LLM_ENABLED="0", AGENT_LIBRARY_PATROL_ENABLED="1", AGENT_RECOGNITION_REVIEW_ENABLED="1",
            AI_RECOGNITION_ENABLED="1", TG_AGENT_ENABLED="1", TG_NOTIFICATION_ENABLED="1",
            AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED="1",
            AGENT_LLM_API_URL="https://model.invalid/v1", AGENT_LLM_MODEL="fixture-model",
        )
        page = self._open("/settings#agent")
        dependents = ("AGENT_LIBRARY_PATROL_ENABLED", "AGENT_RECOGNITION_REVIEW_ENABLED", "AI_RECOGNITION_ENABLED",
                      "TG_AGENT_ENABLED", "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED")

        def state(key):
            field = page.locator(f'[data-key="{key}"]').first
            return field.is_checked(), field.is_disabled()

        # 总开关关闭：跨分区的依赖项都变灰但保留已存值；连接配置、Tavily 与总开关本身仍可编辑。
        for key in dependents:
            self.assertEqual(state(key), (True, True), key)
        for key in ("AGENT_LLM_API_URL", "TAVILY_API_KEY", "AGENT_LLM_ENABLED"):
            self.assertFalse(page.locator(f'[data-key="{key}"]').first.is_disabled(), key)

        page.locator('label.toggle:has([data-key="AGENT_LLM_ENABLED"])').click()
        for key in dependents:
            self.assertEqual(state(key), (True, False), key)
        # 清空模型名后，依赖模型连接的复核与 AI 回退重新变灰，其余 Agent 功能不受影响。
        page.locator('[data-key="AGENT_LLM_MODEL"]').fill("")
        self.assertEqual(state("AGENT_RECOGNITION_REVIEW_ENABLED"), (True, True))
        self.assertEqual(state("AI_RECOGNITION_ENABLED"), (True, True))
        self.assertEqual(state("AGENT_LIBRARY_PATROL_ENABLED"), (True, False))
        page.locator('[data-key="AGENT_LLM_MODEL"]').fill("fixture-model")

        page.locator("#settings-panel-agent [data-save-settings]").click()
        page.wait_for_function(
            "document.querySelector('#settings-panel-agent [data-settings-state]')?.classList.contains('is-success')"
        )
        self.assertEqual(self.config_posts, [(200, {"AGENT_LLM_ENABLED": "1"})])
        self.assertEqual(config.get("AGENT_LIBRARY_PATROL_ENABLED"), "1")

    def test_shared_telegram_allowlist_can_be_saved_and_revoked_with_agent_off(self):
        from app.bot.handlers import _reject_unauthorized_group_write, _reject_unauthorized_resource_search

        key = "TG_AGENT_ALLOWED_USER_IDS"
        message = SimpleNamespace(chat=SimpleNamespace(id=-100), from_user=SimpleNamespace(id=9))
        for width in (1280, 390):
            with self.subTest(width=width):
                self._write(AGENT_LLM_ENABLED="0", TG_AGENT_ENABLED="0", TG_CHAT_ID="-100", TG_AGENT_ALLOWED_USER_IDS="")
                page = self._open("/settings#telegram")
                page.set_viewport_size({"width": width, "height": 900})
                page.locator('[data-settings-target="telegram"]').click()
                field = page.locator(f'[data-key="{key}"]')
                field.wait_for(state="visible")
                self.assertTrue(field.is_visible())
                self.assertFalse(field.is_disabled())
                for value, rejected in (("9", False), ("", True)):
                    field.fill(value)
                    before = len(self.config_posts)
                    page.locator('#settings-panel-telegram [data-save-settings]').click()
                    page.wait_for_function("document.querySelector('#settings-panel-telegram [data-settings-state]')?.classList.contains('is-success')")
                    self.assertEqual(self.config_posts[before:], [(200, {key: value})])
                    self.assertEqual(config.get(key), value)
                    self.assertEqual(config.get("AGENT_LLM_ENABLED"), "0")
                    self.assertEqual(config.get("TG_AGENT_ENABLED"), "0")
                    self.assertEqual(_reject_unauthorized_resource_search(FakeTelegramBot(), message), rejected, f"effective chat={config.get('TG_CHAT_ID')} allowlist={config.get(key)}")
                    self.assertEqual(_reject_unauthorized_group_write(FakeTelegramBot(), message), rejected)
                    page.reload()
                    page.wait_for_function(READY["/settings"])
                    self.assertEqual(field.input_value(), value)
                    self.assertFalse(field.is_disabled())
                page.close()
        with patch.object(config, "has_external_override", side_effect=lambda field: field == key):
            page = self._open("/settings#telegram")
        self.assertTrue(page.locator(f'[data-key="{key}"]').is_disabled(), "移除父依赖不能解除环境托管")
        self.assertEqual(page.evaluate("collectConfigFields(document.getElementById('settings-panel-telegram'))"), {})

if __name__ == "__main__":
    unittest.main()
