"""在真实 GCID 页面与 Chromium 中验证流程，云盘请求只由本地夹具处理。"""

from __future__ import annotations

import json
import mimetypes
import re
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from fastapi.testclient import TestClient

from app.modules.gcid_manifest import FORMAT_NAME, FORMAT_VERSION, _digest
from tests.support import InitializedWebTestCase
from tests.test_agent_kernel_browser import _chromium_executable

if TYPE_CHECKING:
    from playwright.sync_api import Page, Route

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 浏览器 CI 显式安装 Playwright
    sync_playwright = None


GCID_PAGE = "/guangya/more?view=gcid"
OTHER_PAGE = "/other-page"
API_ROOT = "/api/tools/gcid"
STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "static"
DIRECTORIES = [
    {"file_id": "source", "name": "导出目录", "is_dir": True},
    {"file_id": "target-a", "name": "目标甲", "is_dir": True},
    {"file_id": "target-b", "name": "目标乙", "is_dir": True},
]
OFFLINE_REASON = "请先登录光鸭账号，再选择导入目标目录"


def manifest(*files: dict) -> dict:
    result = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "generated_at": "2026-11-22T00:00:00+00:00",
        "source": {"provider": "guangya"},
        "file_count": len(files),
        "total_size": sum(file["size"] for file in files),
        "files": list(files),
    }
    result["integrity"] = {"algorithm": "sha256", "digest": _digest(result)}
    return result


@unittest.skipIf(sync_playwright is None, "浏览器测试环境未安装 Playwright")
class GCIDToolsBrowserTests(InitializedWebTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from app.main import create_app

        with (
            patch("socket.socket.connect", side_effect=AssertionError("测试禁止网络连接")),
            patch("socket.create_connection", side_effect=AssertionError("测试禁止网络连接")),
            patch("socket.getaddrinfo", side_effect=AssertionError("测试禁止 DNS 请求")),
            TestClient(create_app()) as client,
        ):
            login = client.get("/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text)
            assert csrf is not None
            response = client.post("/login", data={
                "csrf_token": csrf[1], "username": "admin", "password": "123456",
            }, follow_redirects=False)
            assert response.status_code == 302
            response = client.get(GCID_PAGE)
            assert response.status_code == 200
            cls.html = response.text

        assert sync_playwright is not None
        playwright = cls.enterClassContext(sync_playwright())
        executable = _chromium_executable(playwright)
        options: dict = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if executable:
            options["executable_path"] = executable
        cls.browser = playwright.chromium.launch(**options)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self) -> None:
        self.calls: dict[str, list[dict]] = {}
        self.errors: list[str] = []
        self.unexpected: list[str] = []
        self.history: list[dict] = []
        self.hold_gcid_requests: set[str] = set()
        self.held_gcid_requests: dict[str, Route] = {}
        self.fail_history_once = False
        self.held_preview: tuple[Route, dict] | None = None
        self.hold_preview = False
        self.export_task: dict | None = None
        self.export_manifest: dict | None = None
        self.export_revision = 0
        self.fail_export_poll = False
        self.lose_export_create = False
        self.fail_export_cleanup = False
        self.logged_in = True
        self.fail_run_once = False
        self.fail_capabilities = False
        self.addCleanup(lambda: self.assertEqual(self.errors, []))
        self.addCleanup(lambda: self.assertEqual(self.unexpected, []))

    def open_page(self, *, logged_in: bool = True, width: int = 1280, ignore_preview_abort: bool = False) -> Page:
        self.logged_in = logged_in
        context = self.browser.new_context(
            accept_downloads=True, viewport={"width": width, "height": 900}, service_workers="block",
        )
        self.addCleanup(context.close)
        context.route_web_socket("**/*", lambda socket: socket.close())
        context.route("**/*", self.serve)
        if ignore_preview_abort:
            # 模拟已经发出的请求仍返回，使乱序测试不依赖网络层及时取消。
            context.add_init_script("""
                const originalFetch = window.fetch.bind(window);
                window.fetch = (url, options) => {
                    if (url === '/api/tools/gcid/import/preview') {
                        const {signal, ...rest} = options;
                        return originalFetch(url, rest);
                    }
                    return originalFetch(url, options);
                };
            """)
        page = context.new_page()
        page.set_default_timeout(4000)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.goto("http://testserver" + GCID_PAGE)
        badge = "状态检查失败" if self.fail_capabilities else "可导入" if logged_in else "导入不可用"
        page.locator("#gcidImportCapability").get_by_text(badge, exact=True).wait_for()
        return page

    def serve(self, route: Route) -> None:
        request = route.request
        url = urlsplit(request.url)
        if url.netloc == "testserver":
            if url.path == "/guangya/more":
                route.fulfill(content_type="text/html; charset=utf-8", body=self.html)
                return
            if url.path == OTHER_PAGE:
                route.fulfill(
                    content_type="text/html; charset=utf-8",
                    body=f'<title>另一页</title><a href="{GCID_PAGE}">返回 GCID 清单</a>',
                )
                return
            if url.path.startswith("/static/"):
                asset = (STATIC_ROOT / unquote(url.path.removeprefix("/static/"))).resolve()
                if asset.is_relative_to(STATIC_ROOT) and asset.is_file():
                    route.fulfill(
                        content_type=mimetypes.guess_type(str(asset))[0] or "application/octet-stream",
                        body=asset.read_bytes(),
                    )
                    return
            if url.path == "/api/guangya/dirs":
                route.fulfill(json=DIRECTORIES)
                return
            if url.path.startswith(API_ROOT + "/"):
                endpoint = url.path.removeprefix(API_ROOT)
                method = request.method
                body = request.post_data_json if method == "POST" else {}
                assert isinstance(body, dict)
                is_export = endpoint == "/export" or endpoint.startswith("/export/")
                key = f"{method} {endpoint}" if is_export or method == "DELETE" else endpoint
                self.calls.setdefault(key, []).append(body)
                if is_export:
                    self.serve_export(route, endpoint, body)
                else:
                    self.serve_gcid(route, endpoint, body)
                return
        self.unexpected.append(request.url)
        route.abort()

    def serve_gcid(self, route: Route, endpoint: str, body: dict) -> None:
        if endpoint in self.hold_gcid_requests:
            self.hold_gcid_requests.remove(endpoint)
            self.held_gcid_requests[endpoint] = route
            return
        if endpoint == "/capabilities":
            if self.fail_capabilities:
                route.fulfill(status=502, content_type="text/html", body="<h1>Bad gateway</h1>")
                return
            route.fulfill(json={
                "export": self.logged_in, "validate": True, "cloud_only_import": self.logged_in,
                "reason": "" if self.logged_in else OFFLINE_REASON,
            })
        elif endpoint == "/validate":
            route.fulfill(json={"valid": True, "file_count": body["file_count"], "total_size": body["total_size"]})
        elif endpoint == "/import/preview":
            payload = body["manifest"]
            result = {
                "preview_id": f"preview-{len(self.calls[endpoint])}",
                "target_dir_id": body["target_dir_id"],
                "manifest_digest": payload["integrity"]["digest"],
                "file_count": payload["file_count"],
                "total_size": payload["total_size"],
                "missing_md5_count": sum(not file.get("md5") for file in payload["files"]),
                "tree": [{"type": "file", "name": file["path"], "size": file["size"]} for file in payload["files"]],
            }
            if self.hold_preview:
                self.hold_preview = False
                self.held_preview = (route, result)
                return
            route.fulfill(json=result)
        elif endpoint == "/import/run":
            if self.fail_run_once:
                self.fail_run_once = False
                route.abort("failed")
                return
            task = {
                "id": 1, "status": "success", "file_count": 1, "total_size": 42,
                "success_count": 1, "failed_count": 0, "can_retry": False,
            }
            self.history[:] = [task]
            route.fulfill(json={"task": task, "replayed": len(self.calls[endpoint]) > 1})
        elif endpoint == "/import/tasks":
            if route.request.method == "DELETE":
                assert route.request.headers.get("x-csrf-token")
                finished = {"success", "partial_success", "failed"}
                kept = [task for task in self.history if task["status"] not in finished]
                deleted = len(self.history) - len(kept)
                self.history[:] = kept
                route.fulfill(json={"deleted": deleted, "retained": len(kept)})
            elif self.fail_history_once:
                self.fail_history_once = False
                route.fulfill(status=502, content_type="text/html", body="<h1>Bad gateway</h1>")
            else:
                route.fulfill(json={"tasks": self.history})
        else:
            self.unexpected.append(route.request.url)
            route.abort()

    def serve_export(self, route: Route, endpoint: str, body: dict) -> None:
        method = route.request.method
        task = self.export_task
        if endpoint == "/export" and method == "GET":
            if self.fail_export_poll:
                self.fail_export_poll = False
                route.fulfill(status=502, content_type="text/html", body="<h1>Bad gateway</h1>")
            else:
                route.fulfill(json={"task": task})
            return
        if endpoint == "/export" and method == "POST":
            if task and task["status"] != "failed":
                if task["source_dir_id"] == body["source_dir_id"]:
                    route.fulfill(status=202, json={"task": task})
                else:
                    route.fulfill(status=409, json={"error": "已有导出任务，请先下载当前清单"})
                return
            assert self.logged_in
            self.export_revision += 1
            self.export_task = {
                "id": f"export-{self.export_revision}", "status": "running", **body,
                "directory_count": 0, "file_count": 0, "error": "",
            }
            if self.lose_export_create:
                self.lose_export_create = False
                route.abort("failed")
            else:
                route.fulfill(status=202, json={"task": self.export_task})
            return
        parts = endpoint.split("/")
        assert len(parts) in {3, 4}
        job_id = parts[2]
        if method == "GET" and parts[3:] == ["download"]:
            if not task or job_id != task["id"]:
                route.fulfill(status=404, json={"error": "导出文件已清理或已过期"})
            else:
                assert task["status"] == "ready" and self.export_manifest is not None
                route.fulfill(
                    headers={"Content-Disposition": "attachment; filename=legacy.json; filename*=UTF-8''%E6%B8%85%E5%8D%95.json"},
                    json=self.export_manifest,
                )
            return
        assert method == "DELETE" and len(parts) == 3
        assert route.request.headers.get("x-csrf-token")
        if self.fail_export_cleanup:
            self.fail_export_cleanup = False
            route.fulfill(status=503, json={"error": "清理暂不可用"})
            return
        if task and task["id"] == job_id:
            assert task["status"] == "ready"
            self.export_task = None
        route.fulfill(json={"ok": True})

    @staticmethod
    def pick_directory(page: Page, trigger: str, modal: str, directory: str) -> None:
        page.locator("#" + trigger).click()
        row = page.locator(f"#{modal} .settings-dir-item").filter(has_text=directory)
        row.get_by_role("button", name="选择", exact=True).click()

    @staticmethod
    def choose_file(page: Page, input_id: str, payload: dict) -> None:
        page.locator("#" + input_id).set_input_files({
            "name": "test.json",
            "mimeType": "application/json",
            "buffer": json.dumps(payload).encode(),
        })

    def test_logged_out_cloud_shows_login_prompt_and_keeps_validation_available(self) -> None:
        page = self.open_page(logged_in=False)
        self.assertTrue(page.locator("#gcidChooseDir").is_disabled())
        self.assertTrue(page.locator("#gcidImportChooseTarget").is_disabled())
        self.assertEqual(page.locator("#gcidImportStatus").inner_text(), OFFLINE_REASON)
        page.locator("#gcidExportMessage").get_by_text(OFFLINE_REASON, exact=True).wait_for()
        self.choose_file(page, "gcidFile", manifest({"path": "可校验.mkv", "size": 42, "gcid": "file-gcid"}))
        page.locator("#gcidValidateBtn").click()
        page.get_by_text("校验通过 · 1 个文件 · 42 B").wait_for()
        self.assertNotIn("POST /export", self.calls)
        self.assertNotIn("/import/run", self.calls)

    def test_capability_request_failure_leaves_actions_disabled_with_feedback(self) -> None:
        self.fail_capabilities = True
        page = self.open_page()
        self.assertTrue(page.locator("#gcidChooseDir").is_disabled())
        self.assertTrue(page.locator("#gcidImportChooseTarget").is_disabled())
        self.assertIn("HTTP 502", page.locator("#gcidImportStatus").inner_text())
        page.locator("#gcidExportMessage").get_by_text("HTTP 502", exact=False).wait_for()

    def test_export_progress_and_poll_failure_keep_layout_and_allow_restart(self) -> None:
        page = self.open_page(width=390)
        page.clock.install()
        self.pick_directory(page, "gcidChooseDir", "gcidDirModal", "导出目录")
        button = page.locator("#gcidExportBtn")
        button.scroll_into_view_if_needed()

        def measure_layout() -> dict[str, dict[str, float]]:
            page.mouse.move(0, 0)
            return page.evaluate("""async () => {
                const finite = document.getAnimations().filter(animation =>
                    Number.isFinite(animation.effect?.getComputedTiming().endTime));
                await Promise.allSettled(finite.map(animation => animation.finished));
                const boxes = {};
                for (const id of ['gcidExportBtn', 'gcidValidateBtn']) {
                    const rect = document.getElementById(id).getBoundingClientRect();
                    boxes[id] = {
                        x: rect.x + window.scrollX, y: rect.y + window.scrollY,
                        width: rect.width, height: rect.height,
                    };
                }
                return boxes;
            }""")

        idle_boxes = measure_layout()

        def assert_stable_layout(stage: str) -> None:
            for control, current in measure_layout().items():
                for measure in ("x", "y", "width", "height"):
                    self.assertAlmostEqual(
                        current[measure], idle_boxes[control][measure], delta=0.5,
                        msg=f"{stage} · {control}.{measure}",
                    )

        button.click()
        message = page.locator("#gcidExportMessage")
        message.get_by_text("正在扫描 · 0 个目录 · 0 个文件").wait_for()
        self.assertTrue(button.is_disabled())
        self.assertTrue(page.locator("#gcidChooseDir").is_disabled())
        task = self.export_task
        assert task is not None
        task.update(directory_count=3, file_count=12)
        page.clock.fast_forward(2000)
        message.get_by_text("正在扫描 · 3 个目录 · 12 个文件").wait_for()
        assert_stable_layout("长时间扫描")
        self.assertEqual(button.get_attribute("aria-busy"), "true")

        self.fail_export_poll = True
        page.clock.fast_forward(2000)
        message.get_by_text("导出状态暂未更新", exact=False).wait_for()
        self.assertIn("12 个文件", message.inner_text())
        self.assertTrue(button.is_disabled())
        task.update(status="failed", error="光鸭目录读取失败。" * 20)
        page.clock.fast_forward(5000)
        message.get_by_text("生成失败", exact=False).wait_for()
        self.assertIn("光鸭目录读取失败", message.inner_text())
        self.assertTrue(button.is_enabled())
        assert_stable_layout("导出失败")
        self.assertNotIn("GET /export/export-1/download", self.calls)

        self.pick_directory(page, "gcidChooseDir", "gcidDirModal", "目标乙")
        button.click()
        message.get_by_text("正在扫描 · 0 个目录 · 0 个文件").wait_for()
        self.assertEqual(self.calls["POST /export"], [
            {"source_dir_id": "source", "source_name": "导出目录"},
            {"source_dir_id": "target-b", "source_name": "目标乙"},
        ])
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)

    def test_export_is_restored_after_leaving_page_and_ready_download_works_offline(self) -> None:
        page = self.open_page()
        self.export_manifest = manifest({
            "path": "影片.mkv", "size": (1 << 53) + 1, "gcid": "file-gcid", "md5": "a" * 32,
        })
        self.pick_directory(page, "gcidChooseDir", "gcidDirModal", "导出目录")
        page.get_by_role("button", name="生成清单", exact=True).click()
        message = page.locator("#gcidExportMessage")
        message.get_by_text("正在扫描 · 0 个目录 · 0 个文件").wait_for()
        page.goto("http://testserver" + OTHER_PAGE)
        self.assertEqual(page.title(), "另一页")
        self.assertEqual(page.locator("#gcidExportBtn").count(), 0)
        task = self.export_task
        assert task is not None
        task.update(status="ready", directory_count=2, file_count=1)
        self.logged_in = False
        page.get_by_role("link", name="返回 GCID 清单").click()
        page.locator("#gcidImportCapability").get_by_text("导入不可用", exact=True).wait_for()
        message.get_by_text("清单已就绪 · 2 个目录 · 1 个文件").wait_for()
        self.assertIn("导出目录", page.locator("#gcidSourceValue").inner_text())
        self.assertTrue(page.locator("#gcidChooseDir").is_disabled())
        self.assertNotIn("GET /export/export-1/download", self.calls)
        self.assertNotIn("DELETE /export/export-1", self.calls)

        with page.expect_download() as download:
            page.get_by_role("button", name="下载 JSON", exact=True).click()
        self.assertEqual(download.value.suggested_filename, "清单.json")
        self.assertEqual(json.loads(Path(download.value.path()).read_bytes()), self.export_manifest)
        message.get_by_text("下载已开始。", exact=True).wait_for()
        self.assertIsNone(self.export_task)
        self.assertEqual(len(self.calls["POST /export"]), 1)
        self.assertEqual(len(self.calls["DELETE /export/export-1"]), 1)
        self.assertTrue(page.get_by_role("button", name="生成清单", exact=True).is_disabled())

    def test_interrupted_blob_keeps_result_and_cleanup_can_retry_without_another_download(self) -> None:
        self.export_manifest = manifest({"path": "影片.mkv", "size": 42, "gcid": "file-gcid"})
        self.export_task = {
            "id": "export-1", "status": "ready", "source_dir_id": "source", "source_name": "导出目录",
            "directory_count": 1, "file_count": 1, "error": "",
        }
        page = self.open_page()
        page.evaluate("""() => {
            const readBlob = Response.prototype.blob;
            Response.prototype.blob = function () {
                if (this.url.endsWith('/export/export-1/download') && !window.interruptExportBlob) {
                    return new Promise((_resolve, reject) => {
                        window.interruptExportBlob = () => reject(new TypeError('interrupted transfer'));
                    });
                }
                return readBlob.call(this);
            };
        }""")
        downloads: list[str] = []
        page.on("download", lambda download: downloads.append(download.suggested_filename))
        button = page.locator("#gcidExportBtn")
        button.get_by_text("下载 JSON", exact=True).click()
        page.wait_for_function("typeof window.interruptExportBlob === 'function'")
        self.assertNotIn("DELETE /export/export-1", self.calls)
        self.assertEqual(downloads, [])
        page.evaluate("window.interruptExportBlob()")
        page.get_by_text("清单下载失败", exact=False).wait_for()
        self.assertIsNotNone(self.export_task)
        self.assertNotIn("DELETE /export/export-1", self.calls)

        self.fail_export_cleanup = True
        with page.expect_download() as download:
            button.get_by_text("下载 JSON", exact=True).click()
        self.assertEqual(json.loads(Path(download.value.path()).read_bytes()), self.export_manifest)
        page.get_by_text("下载已开始，清理失败", exact=False).wait_for()
        self.assertIsNotNone(self.export_task)
        button.get_by_text("重试清理", exact=True).click()
        page.get_by_text("下载已开始。", exact=True).wait_for()
        self.assertIsNone(self.export_task)
        self.assertEqual(len(self.calls["GET /export/export-1/download"]), 2)
        self.assertEqual(len(self.calls["DELETE /export/export-1"]), 2)
        self.assertEqual(downloads, ["清单.json"])

    def test_lost_create_response_and_other_tab_conflict_restore_the_same_slot(self) -> None:
        first = self.open_page()
        self.pick_directory(first, "gcidChooseDir", "gcidDirModal", "导出目录")
        second = first.context.new_page()
        second.set_default_timeout(4000)
        second.goto("http://testserver" + GCID_PAGE)
        self.pick_directory(second, "gcidChooseDir", "gcidDirModal", "目标乙")
        self.lose_export_create = True
        # 在后台标签页提交，保留当前标签页尚未刷新的空槽快照。
        first.locator("#gcidExportBtn").evaluate("button => button.click()")
        first.get_by_text("正在扫描 · 0 个目录 · 0 个文件").wait_for()
        second.locator("#gcidExportBtn").click()
        second.get_by_text("正在扫描 · 0 个目录 · 0 个文件").wait_for()
        self.assertIn("导出目录", second.locator("#gcidSourceValue").inner_text())
        self.assertTrue(second.locator("#gcidChooseDir").is_disabled())
        self.assertEqual(self.export_revision, 1)
        self.assertEqual(self.calls["POST /export"], [
            {"source_dir_id": "source", "source_name": "导出目录"},
            {"source_dir_id": "target-b", "source_name": "目标乙"},
        ])

    def test_root_directory_selection_sends_string_zero_for_export_and_import(self) -> None:
        page = self.open_page()
        page.clock.install()
        self.export_manifest = manifest({"path": "影片.mkv", "size": 42, "gcid": "file-gcid", "md5": "a" * 32})
        page.locator("#gcidChooseDir").click()
        page.locator("#gcidDirModal").get_by_role("button", name="选择当前目录", exact=True).click()
        page.get_by_role("button", name="生成清单", exact=True).click()
        page.get_by_text("正在扫描 · 0 个目录 · 0 个文件").wait_for()
        task = self.export_task
        assert task is not None
        task.update(status="ready", directory_count=1, file_count=1)
        page.clock.fast_forward(2000)
        with page.expect_download() as download:
            page.get_by_role("button", name="下载 JSON", exact=True).click()
        self.assertEqual(self.calls["POST /export"], [{"source_dir_id": "0", "source_name": "根目录"}])

        page.locator("#gcidImportFile").set_input_files(download.value.path())
        page.locator("#gcidImportChooseTarget").click()
        page.locator("#gcidImportTargetModal").get_by_role("button", name="选择当前目录", exact=True).click()
        page.locator("#gcidImportPreviewBtn").click()
        page.locator("#gcidImportConfirm").fill("GCID")
        page.locator("#gcidImportRunBtn").click()
        page.locator("#gcidImportProgress").get_by_text("导入成功").wait_for()
        self.assertEqual(self.calls["/import/preview"], [{"manifest": self.export_manifest, "target_dir_id": "0"}])
        self.assertEqual(self.calls["/import/run"][0]["target_dir_id"], "0")

    def test_late_preview_and_changed_file_cannot_run_an_old_confirmation(self) -> None:
        page = self.open_page(ignore_preview_abort=True)
        legacy = manifest({"path": "旧附件.mkv", "size": 42, "gcid": "old-gcid"})
        modern = manifest({"path": "新附件.mkv", "size": 42, "gcid": "new-gcid", "md5": "a" * 32})
        self.choose_file(page, "gcidImportFile", legacy)
        self.pick_directory(page, "gcidImportChooseTarget", "gcidImportTargetModal", "目标甲")
        preview = page.locator("#gcidImportPreviewBtn")
        confirm = page.locator("#gcidImportConfirm")
        run = page.locator("#gcidImportRunBtn")
        self.hold_preview = True
        with page.expect_request("**/gcid/import/preview"):
            preview.click()
        self.pick_directory(page, "gcidImportChooseTarget", "gcidImportTargetModal", "目标乙")
        self.assertTrue(confirm.is_disabled())
        preview.click()
        page.locator("#gcidImportStatus").get_by_text(
            "1 个文件未带 MD5。GCID 未命中时，无法通过此清单恢复。",
        ).wait_for()
        held = self.held_preview
        assert held is not None
        with page.expect_response("**/gcid/import/preview"):
            held[0].fulfill(json=held[1])
        self.assertEqual(self.calls["/import/preview"], [
            {"manifest": legacy, "target_dir_id": "target-a"},
            {"manifest": legacy, "target_dir_id": "target-b"},
        ])
        confirm.fill("GCID")
        self.assertTrue(run.is_enabled())
        self.choose_file(page, "gcidImportFile", modern)
        self.assertEqual(confirm.input_value(), "")
        self.assertTrue(run.is_disabled())
        self.assertNotIn("/import/run", self.calls)

        preview.click()
        page.locator("#gcidImportTree").get_by_text("新附件.mkv").wait_for()
        self.assertEqual(page.locator("#gcidImportStatus").inner_text(), "请核对预览并输入 GCID")
        confirm.fill("GCID")
        run.click()
        page.locator("#gcidImportProgress").get_by_text("导入成功").wait_for()
        page.locator("#gcidImportHistory").get_by_text("TASK 0001").wait_for()
        task, = self.calls["/import/run"]
        self.assertTrue(task.pop("operation_token"))
        self.assertEqual(task, {
            "preview_id": "preview-3", "target_dir_id": "target-b",
            "manifest_digest": modern["integrity"]["digest"], "confirm": "GCID",
        })
        self.assertTrue(run.is_disabled())

    def test_empty_manifest_explains_why_run_is_disabled(self) -> None:
        page = self.open_page()
        self.choose_file(page, "gcidImportFile", manifest())
        self.pick_directory(page, "gcidImportChooseTarget", "gcidImportTargetModal", "目标甲")
        page.locator("#gcidImportPreviewBtn").click()
        page.get_by_text("清单没有文件，无法执行导入").wait_for()
        self.assertTrue(page.locator("#gcidImportConfirm").is_disabled())
        self.assertTrue(page.locator("#gcidImportRunBtn").is_disabled())
        self.assertNotIn("/import/run", self.calls)

    def test_unknown_run_outcome_resubmits_the_same_binding(self) -> None:
        page = self.open_page()
        self.choose_file(page, "gcidImportFile", manifest({"path": "影片.mkv", "size": 42, "gcid": "file-gcid"}))
        self.pick_directory(page, "gcidImportChooseTarget", "gcidImportTargetModal", "目标甲")
        page.locator("#gcidImportPreviewBtn").click()
        page.locator("#gcidImportConfirm").fill("GCID")
        self.fail_run_once = True
        run = page.locator("#gcidImportRunBtn")
        run.click()
        page.get_by_text("导入结果待确认").wait_for()
        self.assertTrue(page.locator("#gcidImportPreviewBtn").is_disabled())
        run.click()
        page.locator("#gcidImportProgress").get_by_text("导入成功").wait_for()
        initial, retried = self.calls["/import/run"]
        self.assertEqual(initial, retried)

    def test_pending_import_polls_empty_history_and_keeps_progress_after_http_errors(self) -> None:
        page = self.open_page()
        history = page.locator("#gcidImportHistory")
        history.get_by_text("暂无导入任务").wait_for()
        page.clock.install()
        payload = manifest(*[
            {"path": f"{index}.mkv", "size": 42, "gcid": f"gcid-{index}"} for index in range(3)
        ])
        self.choose_file(page, "gcidImportFile", payload)
        self.pick_directory(page, "gcidImportChooseTarget", "gcidImportTargetModal", "目标甲")
        page.locator("#gcidImportPreviewBtn").click()
        page.locator("#gcidImportConfirm").fill("GCID")
        self.hold_gcid_requests.add("/import/run")
        run = page.locator("#gcidImportRunBtn")
        with page.expect_response("**/gcid/import/tasks"), page.expect_request("**/gcid/import/run"):
            run.click()
        page.locator("#gcidHistoryRefreshState").get_by_text("已更新 · 0 条").wait_for()
        self.assertTrue(run.is_disabled())

        task = {
            "id": 18, "status": "running", "manifest_digest": payload["integrity"]["digest"],
            "target_dir_id": "target-a", "file_count": 3, "total_size": 126,
            "success_count": 1, "failed_count": 1, "can_retry": False,
            "failed_samples": [{"path": "1.mkv", "error": "清单缺少 MD5，光鸭仅按 GCID 未命中"}],
        }
        self.history[:] = [task]
        page.clock.fast_forward(5000)
        card = history.locator('[data-status="running"]')
        card.get_by_text("TASK 0018").wait_for()
        card.get_by_text("1.mkv · 清单缺少 MD5，光鸭仅按 GCID 未命中").wait_for()
        succeeded = card.locator(".gcid-history-stats > div").filter(has_text="成功").locator("strong")
        failed = card.locator(".gcid-history-stats > div").filter(has_text="失败").locator("strong")
        self.assertEqual((succeeded.inner_text(), failed.inner_text()), ("1", "1"))

        self.fail_history_once = True
        page.clock.fast_forward(5000)
        page.locator("#gcidHistoryRefreshState").get_by_text("刷新失败").wait_for()
        self.assertEqual((succeeded.inner_text(), failed.inner_text()), ("1", "1"))
        task["success_count"] = 2
        page.clock.fast_forward(5000)
        succeeded.get_by_text("2", exact=True).wait_for()
        self.assertTrue(run.is_disabled())

        self.held_gcid_requests["/import/run"].fulfill(
            status=504, content_type="text/html", body="<h1>Gateway timeout</h1>",
        )
        page.locator("#gcidImportProgress").get_by_text("导入结果待确认").wait_for()
        page.locator("#gcidHistoryRefreshState").get_by_text("已更新 · 1 条").wait_for()
        task.update(status="partial_success", can_retry=True)
        page.clock.fast_forward(5000)
        completed = history.locator('[data-status="partial_success"]')
        completed.get_by_role("button", name="重试失败项").wait_for()
        self.assertEqual(len(self.calls["/import/run"]), 1)

    def test_pending_retry_refreshes_history_again_after_a_poll_timeout(self) -> None:
        task = {
            "id": 7, "status": "partial_success", "file_count": 3, "total_size": 126,
            "success_count": 1, "failed_count": 2, "can_retry": True,
        }
        self.history[:] = [task]
        page = self.open_page()
        retry = page.locator("#gcidImportHistory").get_by_role("button", name="重试失败项")
        retry.wait_for()
        page.clock.install()
        retry_endpoint = "/import/7/retry"
        self.hold_gcid_requests.add(retry_endpoint)
        with page.expect_response("**/gcid/import/tasks"), page.expect_request("**/gcid/import/7/retry"):
            retry.click()
        page.locator("#gcidHistoryRefreshState").get_by_text("已更新 · 1 条").wait_for()
        self.assertTrue(retry.is_disabled())

        self.hold_gcid_requests.add("/import/tasks")
        with page.expect_request("**/gcid/import/tasks"):
            page.clock.fast_forward(5000)
        page.locator("#gcidHistoryRefreshState").get_by_text("刷新中…").wait_for()
        page.clock.fast_forward(10000)
        page.locator("#gcidHistoryRefreshState").get_by_text("刷新超时").wait_for()
        self.assertTrue(retry.is_disabled())

        task.update(status="running", success_count=2, failed_count=0, can_retry=False)
        page.clock.fast_forward(5000)
        card = page.locator('#gcidImportHistory [data-status="running"]')
        succeeded = card.locator(".gcid-history-stats > div").filter(has_text="成功").locator("strong")
        succeeded.get_by_text("2", exact=True).wait_for()
        task.update(status="success", success_count=3)
        page.clock.fast_forward(5000)
        page.locator('#gcidImportHistory [data-status="success"]').get_by_text("TASK 0007").wait_for()
        self.held_gcid_requests[retry_endpoint].fulfill(json={"task": task, "replayed": False})
        page.locator("#gcidImportProgress").get_by_text("成功", exact=True).wait_for()
        self.assertEqual(len(self.calls[retry_endpoint]), 1)

    def test_interrupted_history_stops_polling_without_retry_or_cleanup(self) -> None:
        task = {
            "id": 24, "status": "running", "file_count": 3, "total_size": 3,
            "success_count": 1, "failed_count": 0, "can_retry": False,
        }
        self.history[:] = [task]
        page = self.open_page()
        page.clock.install()
        page.reload()
        history = page.locator("#gcidImportHistory")
        history.locator('[data-status="running"]').wait_for()
        task.update(status="interrupted", unknown_count=1, pending_count=1, error="请核对原光鸭账号的目标目录")
        page.clock.fast_forward(5000)
        interrupted = history.locator('[data-status="interrupted"]')
        interrupted.get_by_text("结果未知 1 · 尚未提交 1", exact=False).wait_for()
        self.assertEqual(interrupted.get_by_role("button", name="重试失败项").count(), 0)
        reads = len(self.calls["/import/tasks"])
        page.clock.run_for(15_000)
        self.assertEqual(len(self.calls["/import/tasks"]), reads)

        page.locator("#gcidImportClear").click()
        modal = page.get_by_role("alertdialog", name="清除 GCID 导入记录")
        with page.expect_response(lambda response: response.request.method == "DELETE"):
            modal.locator("#appConfirmSubmit").click()
        interrupted.wait_for()
        self.assertEqual(self.history, [task])

    def test_clear_import_history_preserves_running_tasks_on_narrow_screen(self) -> None:
        common = {"file_count": 3, "total_size": 126, "can_retry": False}
        running = {**common, "id": 23, "status": "running", "success_count": 1, "failed_count": 0}
        self.history[:] = [
            running,
            {**common, "id": 22, "status": "failed", "success_count": 0, "failed_count": 3, "can_retry": True},
            {**common, "id": 21, "status": "success", "success_count": 3, "failed_count": 0},
        ]
        page = self.open_page(width=320)
        history = page.locator("#gcidImportHistory")
        header = page.locator('section[aria-labelledby="gcidHistoryTitle"] > header')
        clear = header.locator("#gcidImportClear")
        modal = page.get_by_role("alertdialog", name="清除 GCID 导入记录")
        finished = history.get_by_text("TASK 0021", exact=True)
        failed = history.get_by_text("TASK 0022", exact=True)
        active = history.locator('[data-status="running"]')
        delete_key = "DELETE /import/tasks"
        endpoint = API_ROOT + "/import/tasks"
        finished.wait_for()
        failed.wait_for()
        active.get_by_text("TASK 0023", exact=True).wait_for()
        self.assertTrue(header.evaluate("header => header.scrollWidth <= header.clientWidth"))
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))

        clear.click()
        modal.wait_for(state="visible")
        self.assertNotIn(delete_key, self.calls)
        modal.locator("#appConfirmCancel").click()
        modal.wait_for(state="hidden")
        self.assertNotIn(delete_key, self.calls)
        self.assertEqual(history.locator(".gcid-history-task").count(), 3)

        clear.click()
        modal.wait_for(state="visible")
        with page.expect_response(
            lambda response: response.request.method == "DELETE" and urlsplit(response.url).path == endpoint,
        ) as cleared:
            modal.locator("#appConfirmSubmit").click()
        self.assertEqual(cleared.value.status, 200)
        self.assertEqual(cleared.value.json(), {"deleted": 2, "retained": 1})
        modal.wait_for(state="hidden")
        page.locator("#appToastContainer").get_by_text("已清除 2 条记录，保留 1 条", exact=True).wait_for()
        finished.wait_for(state="detached")
        failed.wait_for(state="detached")
        active.get_by_text("TASK 0023", exact=True).wait_for()
        self.assertEqual(history.locator(".gcid-history-task").count(), 1)

        def is_history_read(response) -> bool:
            return response.request.method == "GET" and urlsplit(response.url).path == endpoint

        running["success_count"] = 2
        succeeded = active.locator(".gcid-history-stats > div").filter(has_text="成功").locator("strong")
        with page.expect_response(is_history_read) as refreshed:
            header.locator("#gcidImportRefresh").click()
        self.assertEqual(refreshed.value.json(), {"tasks": [running]})
        succeeded.get_by_text("2", exact=True).wait_for()
        self.assertEqual(history.locator(".gcid-history-task").count(), 1)
        with page.expect_response(is_history_read) as reloaded:
            page.reload()
        self.assertEqual(reloaded.value.json(), {"tasks": [running]})
        active.get_by_text("TASK 0023", exact=True).wait_for()
        succeeded.get_by_text("2", exact=True).wait_for()
        self.assertEqual(history.locator(".gcid-history-task").count(), 1)
        self.assertEqual(self.calls[delete_key], [{}])
        self.assertTrue(header.evaluate("header => header.scrollWidth <= header.clientWidth"))
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
