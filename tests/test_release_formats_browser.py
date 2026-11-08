"""移除手动教学界面，保留 Agent 自然语言→真实预览→人工确认→解析器复用。"""
from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from pathlib import Path

from tests.support import InitializedWebTestCase

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional browser contract
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]

def _chromium_executable(playwright) -> str | None:
    candidates = []
    configured = (os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path(playwright.chromium.executable_path))
    cache_root = Path.home() / ".cache" / "ms-playwright"
    candidates.extend(sorted(cache_root.glob("chromium-*/chrome-linux*/chrome"), reverse=True))
    candidates.extend((Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium")))
    return next((str(path) for path in candidates if path.is_file()), None)


@unittest.skipIf(sync_playwright is None, "系统环境未安装 Playwright")
class ReleaseFormatsBrowserTests(InitializedWebTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        assert sync_playwright is not None
        cls.playwright = sync_playwright().start()
        executable_path = _chromium_executable(cls.playwright)
        options: dict = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if executable_path:
            options["executable_path"] = executable_path
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        super().tearDownClass()

    @contextmanager
    def real_app_page(self, *, touch: bool = False):
        from fastapi.testclient import TestClient

        from app.main import create_app
        from app.modules.recognition import formats
        from tests.support import isolated_test_database
        from tests.test_release_formats import ReleaseFormatApiTests

        errors = []
        with isolated_test_database(), TestClient(create_app()) as client:
            formats.invalidate_cache()
            login = client.get("/login")
            response = client.post("/login", data={
                "csrf_token": ReleaseFormatApiTests.csrf(login), "username": "admin", "password": "123456",
            }, follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            page = self.browser.new_page(viewport={"width": 1280, "height": 900}, has_touch=touch)
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                request = route.request
                if not request.url.startswith("http://testserver/"):
                    route.abort()
                    return
                response = client.request(request.method, request.url, content=request.post_data_buffer,
                                          headers=request.headers)
                headers = {key: value for key, value in response.headers.items()
                           if key not in {"content-encoding", "content-length"}}
                route.fulfill(status=response.status_code, headers=headers, body=response.content)

            page.route("**/*", serve)
            try:
                yield page, client, errors
            finally:
                page.close()
                formats.invalidate_cache()


    def test_rules_page_removes_teaching_ui_and_keeps_other_recognition_tools(self):
        tools = (
            ("openPreprocessRulesBtn", "preprocessRulesModal"),
            ("openTmdbRegexRulesBtn", "tmdbRegexRulesModal"),
            ("openRecognitionKnowledgeBtn", "recognitionKnowledgeModal"),
        )
        self.assertFalse((ROOT / "app/static/js/release-formats.js").exists())
        with self.real_app_page() as (page, _client, errors):
            for width in (1440, 768, 390, 320):
                with self.subTest(width=width):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.goto("http://testserver/organize-rules")
                    page.wait_for_function("() => !document.getElementById('saveOrganizeConfigBtn').disabled")
                    self.assertEqual(page.locator(".organize-recognition-rule-grid > .tmdb-regex-launch").count(), 3)
                    self.assertEqual(page.locator("#releaseFormatsModal, #openReleaseFormatsBtn, a[href*='release-format-teaching'], script[src*='release-formats.js']").count(), 0)
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    if width == 1440:
                        boxes = []
                        for button, _ in tools:
                            box = page.locator(f"#{button}").bounding_box()
                            assert box is not None
                            boxes.append(box)
                        self.assertLess(max(box["y"] for box in boxes) - min(box["y"] for box in boxes), 1)
                    for button, modal in tools:
                        page.locator(f"#{button}").click()
                        page.locator(f"#{modal}").wait_for(state="visible")
                        page.locator(f"#{modal} [data-modal-close]").click()
                        page.locator(f"#{modal}").wait_for(state="hidden")
                    evidence_dir = os.environ.get("MEDIAFLUX_BROWSER_EVIDENCE_DIR")
                    if evidence_dir:
                        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
                        page.locator(".organize-recognition-rule-grid").screenshot(path=str(Path(evidence_dir) / f"recognition-tools-{width}.png"))
            self.assertEqual(errors, [])

    def test_agent_natural_request_previews_and_confirms_through_real_app(self):
        from unittest.mock import patch

        from app.agent.kernel.bootstrap import build_agent_kernel_runtime
        from app.modules.recognition import formats
        from app.modules.scraper import _parse_release_core
        from tests.test_agent_release_format_kernel import TeachingModel
        from tests.test_release_formats import PARENT, TEMPLATE, filename

        message = (
            f"这些文件集号识别错了，教会你这个发布格式以后复用。父目录是{PARENT}。"
            f"{filename(13)} 是星海航行第13集；{filename(14)} 是第14集。r2是修订版。"
            f"另请批量核对{filename(15)}和unrelated.mkv。请先批量预览，再让我确认保存。"
        )
        with self.real_app_page() as (page, _client, errors):
            model = TeachingModel()
            runtime = build_agent_kernel_runtime(model=model)
            with patch("app.routes.agent_api.get_agent_kernel_runtime", return_value=runtime):
                page.goto("http://testserver/agent")
                page.locator("#agentPrompt").wait_for(state="visible")
                self.assertEqual(page.locator("#agentPrompt").input_value(), "")
                self.assertEqual(model.requests, [])
                self.assertEqual(formats.list_rules(), [])
                page.locator("#agentPrompt").fill(message)
                page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
                card = page.locator(".agent-confirmation-card")
                card.wait_for()
                self.assertIn("第13集", card.inner_text())
                self.assertIn("星海航行", card.inner_text())
                self.assertNotIn(TEMPLATE, card.inner_text())
                self.assertNotIn(PARENT, card.inner_text())
                self.assertEqual(formats.list_rules(), [])
                page.set_viewport_size({"width": 390, "height": 844})
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                card.locator("[data-effect-confirm]").click()
                page.wait_for_function("() => document.querySelector('.agent-result-card')?.innerText.includes('已保存')")
                self.assertEqual(len(formats.list_rules()), 1)
                self.assertEqual(_parse_release_core(filename(15), PARENT).context.episode, 15)
                self.assertEqual(len(model.requests), 2)
                self.assertEqual(errors, [])
