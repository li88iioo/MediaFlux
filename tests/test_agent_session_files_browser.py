"""通过真实 Chromium 和本地 ASGI 服务器验证 Agent 种子入口。"""
from __future__ import annotations

import socket
import threading
import time
import unittest
from contextlib import contextmanager
from urllib.parse import urlsplit

import uvicorn

from app import config
from tests import test_agent_kernel_browser as browser_support
from tests.support import initialized_web_runtime


@contextmanager
def _local_app():
    from app.main import create_app

    with initialized_web_runtime(), socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(create_app(), lifespan="off", log_level="error"))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                assert worker.is_alive() and time.monotonic() < deadline, "本地 Web 测试服务启动失败"
                time.sleep(0.01)
            yield origin
        finally:
            server.should_exit = True
            worker.join(timeout=5)
            assert not worker.is_alive(), "本地 Web 测试服务未停止"


@unittest.skipIf(browser_support.sync_playwright is None, "系统环境未安装 Playwright")
class AgentSessionFilesBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sync_playwright = browser_support.sync_playwright
        assert sync_playwright is not None
        playwright = cls.enterClassContext(sync_playwright())
        cls.browser = cls.enterClassContext(playwright.chromium.launch(
            executable_path=browser_support._chromium_executable(playwright),
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking"],
        ))

    def setUp(self):
        origin = self.enterContext(_local_app())
        context = self.enterContext(self.browser.new_context(
            viewport={"width": 390, "height": 844},
            reduced_motion="reduce", service_workers="block",
        ))
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
        self.page = page = context.new_page()
        page.set_default_timeout(10_000)
        self.posts = []
        self.errors = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("request", lambda request: self.posts.append(urlsplit(request.url).path) if request.method == "POST" else None)
        page.goto(origin + "/login")
        username, password = config.web_credentials()
        page.locator("#login-username").fill(username)
        page.locator("#login-password").fill(password)
        page.get_by_role("button", name="验证凭证进入系统").click()
        page.wait_for_load_state("load")
        self.posts.clear()
        page.goto(origin + "/agent")
        self.attach = page.get_by_role("button", name="添加种子文件", exact=True)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def choose(self, name, content, mime_type):
        with self.page.expect_file_chooser() as chooser:
            self.attach.click()
        chooser.value.set_files({"name": name, "mimeType": mime_type, "buffer": content})

    def test_torrent_uses_direct_preview_then_agent_selection(self):
        page = self.page
        name = "测试种子.TORRENT"
        content = b"d4:infod6:lengthi10485760e4:name9:Movie.mkvee"
        with page.expect_response("**/api/downloads/direct/preview") as preview:
            self.choose(name, content, "application/octet-stream")
        assert preview.value.ok, preview.value.text()
        card = page.locator(".agent-download-card")
        card.get_by_text("Movie.mkv", exact=True).wait_for()
        assert card.get_by_role("button", name="提交下载").is_visible()
        assert self.posts == ["/api/downloads/direct/preview"]

        session_id = page.evaluate("localStorage.getItem('mediaflux.agent.kernel.session.v1')")
        upload_path = f"/api/agent/sessions/{session_id}/torrent"
        choose_files = card.get_by_role("button", name="用 Agent 选文件")
        prompt = page.locator("#agentPrompt")
        prompt.fill("正在编辑的草稿")
        with page.expect_response(f"**{upload_path}") as upload:
            choose_files.click()
        assert upload.value.status == 201, upload.value.text()
        file = upload.value.json()["file"]
        assert file["name"] == name and file["size"] == len(content)
        page.get_by_text("已保留输入框中的草稿，请先发送或清空后再选择", exact=True).wait_for()
        assert prompt.input_value() == "正在编辑的草稿"
        prompt.fill("")
        choose_files.click()
        card.wait_for(state="detached")
        draft = prompt.input_value()
        assert name in draft and file["id"] in draft
        assert self.posts == ["/api/downloads/direct/preview", upload_path]

    def test_non_torrent_extensions_are_rejected_by_the_picker(self):
        page = self.page
        page.set_viewport_size({"width": 320, "height": 700})
        composer = page.locator("#agentComposer")
        before = composer.bounding_box()
        assert composer.get_by_role("button").count() == 1
        assert composer.locator("details").count() == 0
        picker = composer.locator("input[type=file]")
        assert picker.get_attribute("accept") == ".torrent"
        prompt = page.locator("#agentPrompt")
        prompt.fill("选择剧集")
        attach_box = self.attach.bounding_box()
        send_box = page.get_by_role("button", name="发送", exact=True).bounding_box()
        assert attach_box is not None and send_box is not None
        self.assertAlmostEqual(
            attach_box["y"] + attach_box["height"] / 2,
            send_box["y"] + send_box["height"] / 2,
            delta=0.5,
        )

        for name, payload, mime_type in (
            ("素材.mp4", b"video", "application/x-bittorrent"),
            ("GCID 清单.json", b'{"format":"mediaflux.gcid-manifest","version":2}', "application/json"),
        ):
            with self.subTest(name=name):
                self.choose(name, payload, mime_type)
                toast = page.get_by_text("请选择 .torrent 种子文件。", exact=True)
                toast.wait_for()
                toast.click()
                toast.wait_for(state="hidden")
                assert self.posts == []
                assert page.locator(".agent-download-card").count() == 0
        assert prompt.input_value() == "选择剧集"
        assert composer.bounding_box() == before
        assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
