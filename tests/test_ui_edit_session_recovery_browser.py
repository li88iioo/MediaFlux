"""编辑/刮削会话恢复：原始 Jinja + Chromium，全请求 fake，不导入应用或访问 DB。

直接运行本文件即可；UI_SESSION_EVIDENCE_DIR 可选保存截图与请求证据。
响应体闸门模拟响应已读取但业务 continuation 尚未执行的竞态：即使 abort
也必须用会话代次忽略旧结果。它只控制 fake 传输，不替换产品事件处理器。
"""

from __future__ import annotations

import json
import mimetypes
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from jinja2 import Environment, FileSystemLoader

try:
    from .test_agent_kernel_browser import _chromium_executable, sync_playwright
except ImportError:
    from test_agent_kernel_browser import _chromium_executable, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTIONS = [
    dict(
        id=i,
        title=f"媒体 {i}",
        tmdb_id=100 + i,
        media_type="tv",
        enabled=True,
        sites=[f"site-{i}"],
        download_target="guangya",
        action="confirm",
        monitor_mode="selected",
        seasons=[1, 3],
    )
    for i in (1, 2)
]
SCRAPE = "/api/guangya/directory-scrape"
MEDIA = "/api/subscriptions/media"
PROXY = "/api/media-proxy"
PROXY_INSTANCES = [
    dict(id=i, name=f"反代 {i}", config_source="custom", server_type="jellyfin",
         upstream_url="http://jellyfin.invalid", listen_host="127.0.0.1",
         listen_port=18096 + i, enabled=True, status="running")
    for i in (1, 2)
]
# 先读取 fake JSON，再阻塞交付；这是 AbortController 无法撤回的已完成响应。
REPLY_GATE = """
(() => {
    const originalFetch = window.fetch.bind(window);
    window.uiReplyGates = {};
    window.uiHoldReplies = [];
    window.fetch = async (input, options = {}) => {
        const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
        const index = window.uiHoldReplies.findIndex(item => item.path === path);
        const hold = index < 0 ? null : window.uiHoldReplies.splice(index, 1)[0];
        const response = await originalFetch(input, options);
        if (!hold) return response;
        const payload = await response.json();
        let release;
        const wait = new Promise(resolve => { release = resolve; });
        const gate = {ready: true, delivered: false, aborted: Boolean(options.signal?.aborted), release};
        window.uiReplyGates[hold.key] = gate;
        options.signal?.addEventListener('abort', () => { gate.aborted = true; }, {once: true});
        return {
            ok: response.ok, status: response.status, headers: response.headers,
            json: async () => { await wait; gate.delivered = true; return payload; },
        };
    };
})();
"""


def inspection(name):
    return dict(
        inspection_id=f"inspection-{name}",
        directory={"id": name, "name": name},
        counts={"video": 1},
        suggested_query=name,
        media_type="movie",
        archive_target={"id": "archive", "name": "归档目录"},
    )


def candidate(tmdb_id):
    return dict(
        tmdb_id=tmdb_id, title=f"候选 {tmdb_id}", media_type="movie", score=0.95
    )


@unittest.skipIf(sync_playwright is None, "未安装 Playwright")
class UiEditSessionRecoveryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert sync_playwright is not None
        cls.playwright = cls.enterClassContext(sync_playwright())
        executable = _chromium_executable(cls.playwright)
        if executable is None:
            raise unittest.SkipTest("未找到 Chromium")
        cls.browser = cls.playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
            ],
        )
        cls.addClassCleanup(cls.browser.close)
        env = Environment(
            loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True
        )
        cls.templates = {
            name: env.get_template(name + ".html").render(
                active=name,
                app_version="offline-session-test",
                discovery_enabled=False,
                agent_enabled=False,
                csrf_token=lambda: "fake-csrf",
                static_url=lambda value: "/static/" + value,
                url_for=lambda name: "/" + name.split(".")[-1],
            )
            for name in ("rss", "guangya", "media_proxy", "guangya_strm")
        }

    def setUp(self):
        self.requests, self.unexpected, self.errors = [], [], []
        self.replies, self.pending, self.hold_transport = {}, {}, {}
        self.context = None
        self.strm_client = None
        self.observations = {}

    def tearDown(self):
        if self.context:
            # held route 也需要结束协议回调；不能靠关闭context遗留取消中的处理器。
            for route in self.pending.values():
                route.abort()
            self.pending.clear()
            evidence = os.environ.get("UI_SESSION_EVIDENCE_DIR")
            if evidence:
                target = Path(evidence)
                target.mkdir(parents=True, exist_ok=True)
                self.page.screenshot(path=str(target / (self._testMethodName + ".png")))
                (target / (self._testMethodName + ".json")).write_text(
                    json.dumps(
                        {
                            "observations": self.observations,
                            "requests": self.requests,
                            "page_errors": self.errors,
                            "unexpected_requests": self.unexpected,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            self.context.close()
        self.assertEqual(self.errors, [], "页面不能有未处理脚本异常")
        self.assertEqual(self.unexpected, [], "所有请求必须完全离线且在 fake 边界内")

    def make_page(self, template, width=1280):
        self.context = self.browser.new_context(
            viewport={"width": width, "height": 900 if width > 600 else 844},
            service_workers="block",
            is_mobile=width < 600,
            has_touch=width < 600,
            reduced_motion="reduce",
        )
        self.context.add_init_script(REPLY_GATE)
        self.context.route("**/*", self.route_request)
        self.context.route_web_socket("**/*", lambda ws: ws.close())
        self.template = template
        self.page = self.context.new_page()
        self.page.set_default_timeout(2500)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto("http://offline.invalid/" + template)
        ready = {
            "rss": '[data-subscription-id="1"]',
            "guangya": '[data-media-id="A"]',
            "media_proxy": '[data-select-instance="1"]',
            "guangya_strm": '#saveStrmBtn:not(:disabled)',
        }[template]
        self.page.locator(ready).wait_for()
        return self.page

    def route_request(self, route):
        request = route.request
        url = urlsplit(request.url)
        path = unquote(url.path)
        if url.netloc == "offline.invalid":
            if request.method == "GET" and path == "/" + self.template:
                route.fulfill(
                    content_type="text/html", body=self.templates[self.template]
                )
                return
            if request.method == "GET" and path.startswith("/static/"):
                asset = (ROOT / "app" / path.lstrip("/")).resolve()
                if asset.is_relative_to(ROOT / "app/static") and asset.is_file():
                    route.fulfill(
                        body=asset.read_bytes(),
                        content_type=mimetypes.guess_type(str(asset))[0]
                        or "application/octet-stream",
                    )
                    return
            body = request.post_data_json if request.post_data else None
            entry = dict(method=request.method, path=path, query=url.query, body=body)
            self.requests.append(entry)
            key = (request.method, path)
            if key in self.hold_transport:
                self.pending[self.hold_transport.pop(key)] = route
                return
            if self.replies.get(key):
                status, payload = self.replies[key].pop(0)
                entry["status"] = status
                route.fulfill(status=status, json=payload)
                return
            if path.startswith("/api/strm/failures"):
                assert self.strm_client is not None
                response = self.strm_client.request(
                    request.method, path + ("?" + url.query if url.query else ""), json=body,
                )
                entry["status"] = response.status_code
                route.fulfill(status=response.status_code, json=response.json())
                return
            if path == "/api/config":
                route.fulfill(json={
                    "GY_STRM_SOURCE_DIRS": "[]",
                    "GY_STRM_BASE_URL": "http://fixture.invalid:1258",
                    "STRM_SCHEDULE_CRON": "0 4 * * *",
                })
                return
            if path == "/api/strm/index-diagnostics":
                route.fulfill(json={
                    "confirmed_test_artifact": 0, "metadata_queue": {},
                    "video": {}, "metadata": {},
                })
                return
            if path == PROXY + "/profiles":
                route.fulfill(json=[])
                return
            if path == PROXY and request.method == "GET":
                route.fulfill(json=PROXY_INSTANCES)
                return
            if path in (PROXY + "/1", PROXY + "/2") and request.method == "PUT":
                route.fulfill(json={"instance": PROXY_INSTANCES[int(path[-1]) - 1]})
                return
            if path in (PROXY + "/1/bindings", PROXY + "/2/bindings"):
                route.fulfill(json=[] if request.method == "GET" else {"ok": True})
                return
            if path == MEDIA and request.method == "GET":
                route.fulfill(json={"items": SUBSCRIPTIONS, "total": len(SUBSCRIPTIONS), "has_more": False, "next": None})
                return
            if path.startswith(MEDIA + "/") and request.method == "PUT":
                route.fulfill(json={"created": False})
                return
            if path == "/api/subscriptions/stats":
                route.fulfill(json={})
                return
            if path == "/api/subscriptions/watchlist":
                route.fulfill(json={"items": [], "total": 0, "has_more": False, "next": None})
                return
            if path == "/api/guangya/token/validate":
                route.fulfill(json={"has_access_token": True, "valid": True})
                return
            if path == "/api/guangya/capabilities":
                route.fulfill(json={})
                return
            if path == "/api/guangya/dirs":
                route.fulfill(
                    json=[
                        dict(file_id=name, name=name, is_dir=True)
                        for name in ("A", "B")
                    ]
                )
                return
            if path == SCRAPE + "/inspect":
                assert body is not None
                route.fulfill(
                    json=inspection(body.get("directory_id", body.get("file_id")))
                )
                return
            if path == SCRAPE + "/search":
                route.fulfill(json={"candidates": [candidate(101), candidate(202)]})
                return
            if path == SCRAPE + "/preview":
                assert body is not None
                route.fulfill(
                    json=dict(
                        preview_id=f"{body['inspection_id']}-{body['tmdb_id']}",
                        match=candidate(body["tmdb_id"]),
                        archive_target={"id": "archive", "name": "归档目录"},
                        plans=[
                            dict(
                                action="move",
                                original_name="movie.mkv",
                                new_name="Movie.mkv",
                                target_path="Archive/Movie",
                            )
                        ],
                    )
                )
                return
        self.unexpected.append(request.method + " " + request.url)
        route.abort()

    def hold_reply(self, path, key, payload=None, status=200, method="POST"):
        if payload is not None:
            self.replies.setdefault((method, path), []).append((status, payload))
        self.page.evaluate(
            "item => uiHoldReplies.push(item)", {"path": path, "key": key}
        )

    def wait_reply(self, key):
        self.page.wait_for_function("key => uiReplyGates[key]?.ready", arg=key)

    def release_reply(self, key):
        self.page.evaluate("key => uiReplyGates[key].release()", key)
        self.page.wait_for_function("key => uiReplyGates[key].delivered", arg=key)
        self.settle()

    def settle(self):
        self.page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )

    def make_strm_page(self, width, count=241):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app import database as db
        from app.routes import strm_api
        from tests.support import isolated_test_database

        self.enterContext(isolated_test_database())
        for number in range(count):
            db.record_strm_failure(
                source_id="fixture-a", source_name="隔离来源 A",
                file_id=f"fixture-{number}", parent_id="fixture-a",
                filename=f"fixture-{number}.mkv", action="generate",
                rel_dir="fixture", target_rel_path=f"fixture/{number}.strm", error="fixture failure",
            )
        self.enterContext(patch.object(strm_api, "require_api_login"))
        self.strm_selected_retry = self.enterContext(patch.object(
            strm_api, "retry_strm_failures",
            side_effect=lambda ids, trigger: {"ok": True, "resolved": 0, "failed": len(ids)},
        ))
        self.strm_all_retry = self.enterContext(patch.object(
            strm_api, "retry_all_strm_failures", return_value={"ok": True, "resolved": 0, "failed": 0},
        ))
        app = FastAPI()
        app.include_router(strm_api.router)
        self.strm_client = self.enterContext(TestClient(app))
        page = self.make_page("guangya_strm", width)
        page.locator("#strmTabButton_diagnostics").click()
        page.locator("#strmFailureList tr[data-id]").first.wait_for()
        self.settle()
        return page

    def strm_retry_pages(self, width):
        page = self.make_strm_page(width)
        pages = []
        for number, count in ((1, 100), (2, 100), (3, 41)):
            page.wait_for_function(
                "([number,count]) => document.querySelector('#strmFailurePageInfo').textContent.includes(`第 ${number} / 3 页`) && document.querySelectorAll('#strmFailureList tr[data-id]').length === count",
                arg=[number, count],
            )
            pages.append(page.locator("#strmFailureList tr[data-id]").evaluate_all(
                "rows => rows.map(row => Number(row.dataset.id))",
            ))
            if number < 3:
                page.locator("#strmFailureNext").click()
        self.assertEqual(len(set(sum(pages, []))), 241)
        self.assertTrue(page.locator("#strmFailureNext").is_disabled())
        self.assertTrue(all("page_size=100" in row["query"] for row in self.requests if row["path"] == "/api/strm/failures"))
        assert self.strm_client is not None
        self.assertEqual(self.strm_client.post("/api/strm/failures/retry", json={"ids": pages[0]}).status_code, 200)
        self.assertEqual(self.strm_client.post("/api/strm/failures/retry", json={"ids": pages[0] + [pages[1][0]]}).status_code, 400)
        self.strm_selected_retry.reset_mock()
        page.locator("#strmFailurePrev").click()
        page.wait_for_function("document.querySelector('#strmFailurePageInfo').textContent.includes('第 2 / 3 页')")
        page.locator("#strmFailurePrev").click()
        page.wait_for_function("document.querySelector('#strmFailurePageInfo').textContent.includes('第 1 / 3 页')")
        page.locator("#selectAllStrmFailures").check()
        self.hold_reply("/api/strm/failures/retry", "page-retry")
        page.locator("#retrySelectedStrmFailuresBtn").click()
        page.locator("#appConfirmSubmit").click()
        self.wait_reply("page-retry")
        self.strm_selected_retry.assert_called_once_with(pages[0], "web")
        self.assertEqual(self.requests[-1]["status"], 200)
        self.release_reply("page-retry")
        page.wait_for_function("!document.querySelector('#retryAllStrmFailuresBtn').disabled")
        self.strm_all_retry.assert_not_called()
        self.observations = {"width": width, "page_counts": list(map(len, pages)), "unique_ids": 241}

    def test_strm_retry_pages_are_complete_and_within_api_limit_desktop(self):
        self.strm_retry_pages(1280)

    def test_strm_retry_pages_are_complete_and_within_api_limit_mobile(self):
        self.strm_retry_pages(390)

    def strm_single_retry_selection(self, width):
        page = self.make_strm_page(width, count=3)
        ids = page.locator("#strmFailureList tr[data-id]").evaluate_all("rows => rows.map(row => Number(row.dataset.id))")
        first = page.locator(f'#strmFailureList tr[data-id="{ids[0]}"]')
        other = page.locator(f'#strmFailureList tr[data-id="{ids[1]}"] input')
        other.check()
        first.locator("[data-retry-single]").click()
        page.locator("#appConfirmSubmit").wait_for(state="visible")
        self.assertTrue(other.is_checked())
        self.assertFalse(first.locator("input").is_checked())
        page.locator("#appConfirmCancel").click()
        self.assertFalse(any(row["method"] == "POST" for row in self.requests))
        self.hold_reply("/api/strm/failures/retry", "single-retry")
        before = page.locator("#retrySelectedStrmFailuresBtn").bounding_box()
        first.locator("[data-retry-single]").click()
        page.locator("#appConfirmSubmit").click()
        self.wait_reply("single-retry")
        self.assertTrue(other.is_checked())
        first.locator("[data-retry-single]").click()
        self.strm_selected_retry.assert_called_once_with([ids[0]], "web")
        after = page.locator("#retrySelectedStrmFailuresBtn").bounding_box()
        assert before is not None and after is not None
        self.assertEqual((before["width"], before["height"]), (after["width"], after["height"]))
        self.release_reply("single-retry")
        page.wait_for_function("!document.querySelector('#retrySelectedStrmFailuresBtn').disabled")
        self.assertTrue(other.is_checked())
        self.replies[("POST", "/api/strm/failures/retry")] = [(503, {"error": "fixture retry unavailable"})]
        first.locator("[data-retry-single]").click()
        page.locator("#appConfirmSubmit").click()
        page.wait_for_function("document.querySelector('#strmFailureState').textContent.includes('fixture retry unavailable')")
        self.assertTrue(other.is_checked())
        self.assertFalse(other.is_disabled())
        self.assertFalse(first.locator("input").is_checked())
        self.assertEqual([row["body"] for row in self.requests if row["method"] == "POST"], [{"ids": [ids[0]]}] * 2)
        self.observations = {"width": width, "retried_id": ids[0], "retained_selection": ids[1]}

    def test_strm_single_retry_keeps_other_selection_desktop(self):
        self.strm_single_retry_selection(1280)

    def test_strm_single_retry_keeps_other_selection_mobile(self):
        self.strm_single_retry_selection(390)

    def strm_all_retry_snapshot(self, width):
        from app import database as db

        page = self.make_strm_page(width, count=3)
        for selector, value in (("#strmFailureSourceFilter", "fixture-a"), ("#strmFailureActionFilter", "generate")):
            page.locator(selector).select_option(value)
            page.wait_for_function("document.querySelector('#strmFailureList').getAttribute('aria-busy') === 'false'")
        # 另一写入者清空了台账，列表回应先到传输层、晚到 UI；确认期间会重建来源选项。
        with db.get_conn() as connection:
            connection.execute("DELETE FROM strm_failures")
        self.hold_reply("/api/strm/failures", "empty-list", method="GET")
        page.locator("#refreshStrmFailuresBtn").click()
        self.wait_reply("empty-list")
        page.locator("#retryAllStrmFailuresBtn").click()
        self.release_reply("empty-list")
        self.assertEqual(page.locator("#strmFailureSourceFilter").input_value(), "")
        self.hold_reply("/api/strm/failures/retry", "all-retry")
        page.locator("#appConfirmSubmit").click()
        self.wait_reply("all-retry")
        self.strm_all_retry.assert_called_once_with("fixture-a", "generate", "web")
        self.strm_selected_retry.assert_not_called()
        self.assertEqual(self.requests[-1]["body"], {"all": True, "source_id": "fixture-a", "action": "generate"})
        self.release_reply("all-retry")
        self.observations = {"width": width, "frozen_source_id": "fixture-a", "visible_source_id": ""}

    def test_strm_retry_all_uses_confirmed_filter_desktop(self):
        self.strm_all_retry_snapshot(1280)

    def test_strm_retry_all_uses_confirmed_filter_mobile(self):
        self.strm_all_retry_snapshot(390)

    def proxy_late_save(self, *, binding=False, status=200, new_pending=False, width=1280, same_object=False):
        page = self.make_page("media_proxy", width)
        modal, field, save, notice = (
            ("bindingModal", "bindingItemId", "saveBindingBtn", "bindingSaveState")
            if binding else ("proxyModal", "proxyName", "saveProxyBtn", "proxySaveState")
        )

        def edit(ident):
            if binding:
                page.locator("#proxyTabButton_instances").click()
                page.locator(f'[data-select-instance="{ident}"] strong').first.click()
                page.locator("#proxyTabButton_advanced").click()
                page.locator("#newBindingBtn").click()
                page.locator("#bindingGuangyaId").fill(f"cloud-{ident}")
            else:
                page.locator(f'[data-edit-instance="{ident}"]').click()
            page.locator(f"#{field}").fill(f"draft-{ident}")

        edit(1)
        path = PROXY + ("/1/bindings" if binding else "/1")
        method = "POST" if binding else "PUT"
        self.hold_reply(path, "old-save", payload={"error": "旧保存失败"} if status == 500 else None,
                        status=status, method=method)
        page.locator(f"#{save}").click()
        self.wait_reply("old-save")
        if status == 500:
            page.locator(f'#{modal} .media-config-footer [data-{"binding" if binding else "proxy"}-close]').click()
        else:
            page.keyboard.press("Escape")
        page.locator(f"#{modal}").wait_for(state="hidden")
        edit(1 if same_object else 2)
        page.locator(f"#{field}").fill("draft-2")
        if new_pending:
            self.assertTrue(page.locator(f"#{save}").is_enabled(), "新会话必须能保存")
            self.hold_reply(PROXY + ("/2/bindings" if binding else "/2"), "new-save", method=method)
            page.locator(f"#{save}").click()
            self.wait_reply("new-save")
        page.locator(f"#{modal}").evaluate("""async el => {
            await Promise.all(el.getAnimations({subtree:true})
                .filter(a => a.effect.getTiming().iterations !== Infinity)
                .map(a => a.finished.catch(() => {})));
        }""")
        before = page.locator(f"#{save}").bounding_box()
        self.release_reply("old-save")
        self.assertTrue(page.locator(f"#{modal}").is_visible(), "旧成功不能关闭新会话")
        self.assertEqual(page.locator(f"#{field}").input_value(), "draft-2")
        self.assertEqual(page.locator(f"#{notice}").inner_text(), "保存中..." if binding and new_pending
                         else "保存并热加载中..." if new_pending else "")
        self.assertEqual(page.locator(f"#{save}").is_disabled(), new_pending)
        self.assertEqual(page.locator(f"#{save}").bounding_box(), before)
        if new_pending:
            self.release_reply("new-save")
            page.locator(f"#{modal}").wait_for(state="hidden")
            if not binding:
                page.locator('[data-select-instance="2"].active').wait_for()
                self.assertTrue(page.locator("#newBindingBtn").is_enabled())
                self.assertIn("反代 2", page.locator("#bindingPanelHint").inner_text())
        saves = [r for r in self.requests if r["method"] == method]
        self.assertEqual(saves[0]["path"], path)
        self.assertEqual(saves[0]["body"]["media_item_id" if binding else "name"], "draft-1")

    def test_proxy_instance_late_success_keeps_new_draft(self):
        self.proxy_late_save()

    def test_proxy_instance_late_error_keeps_new_draft(self):
        self.proxy_late_save(status=500)

    def test_proxy_instance_old_completion_cannot_release_new_save(self):
        self.proxy_late_save(status=500, new_pending=True)

    def test_proxy_binding_late_success_keeps_new_draft_on_mobile(self):
        self.proxy_late_save(binding=True, width=390)

    def test_proxy_binding_late_error_keeps_new_draft(self):
        self.proxy_late_save(binding=True, status=500)

    def test_proxy_binding_old_completion_cannot_release_new_save(self):
        self.proxy_late_save(binding=True, new_pending=True)

    def test_proxy_instance_reopened_same_object_is_a_new_session(self):
        self.proxy_late_save(same_object=True)

    def test_proxy_binding_reopened_same_instance_is_a_new_session(self):
        self.proxy_late_save(binding=True, same_object=True)

    def proxy_current_failure_retry(self, *, binding=False):
        page = self.make_page("media_proxy")
        if binding:
            page.locator('[data-select-instance="1"] strong').first.click()
            page.locator("#proxyTabButton_advanced").click()
            page.locator("#newBindingBtn").click()
            page.locator("#bindingGuangyaId").fill("cloud-1")
        else:
            page.locator('[data-edit-instance="1"]').click()
        modal, field, save, notice, method, path = (
            ("bindingModal", "bindingItemId", "saveBindingBtn", "bindingSaveState", "POST", PROXY + "/1/bindings")
            if binding else ("proxyModal", "proxyName", "saveProxyBtn", "proxySaveState", "PUT", PROXY + "/1")
        )
        page.locator(f"#{field}").fill("retry-draft")
        self.hold_reply(path, "failure", payload={"error": "当前失败，可重试"}, status=500, method=method)
        page.locator(f"#{save}").click()
        self.wait_reply("failure")
        page.locator(f"#{save}").dispatch_event("click")
        self.assertEqual(len([r for r in self.requests if r["method"] == method]), 1)
        self.release_reply("failure")
        self.assertTrue(page.locator(f"#{save}").is_enabled())
        self.assertEqual(page.locator(f"#{field}").input_value(), "retry-draft")
        self.assertEqual(page.locator(f"#{notice}").inner_text(), "当前失败，可重试")
        page.locator(f"#{save}").click()
        page.locator(f"#{modal}").wait_for(state="hidden")
        self.assertEqual(len([r for r in self.requests if r["method"] == method]), 2)

    def test_proxy_instance_current_error_preserves_draft_and_can_retry(self):
        self.proxy_current_failure_retry()

    def test_proxy_binding_current_error_preserves_draft_and_can_retry(self):
        self.proxy_current_failure_retry(binding=True)

    def manual(self, name):
        self.page.locator(f'[data-media-id="{name}"] .gy-dir-action-btn').click()
        self.page.locator('[data-scrape-action="manual"]').click()

    def wait_manual(self, name):
        self.page.wait_for_function(
            "name => document.querySelector('#gyScrapeDirectory').textContent.startsWith(name + ' ·')",
            arg=name,
        )
        self.page.locator(".gy-scrape-candidate").first.wait_for()
        self.page.wait_for_function(
            "!document.querySelector('#gyScrapeSearchBtn').disabled"
        )
        self.settle()

    def edit(self, ident):
        self.page.locator(
            f'[data-subscription-id="{ident}"] [data-media-action="edit"]'
        ).click()
        self.page.locator("#ms_sites").fill(f"draft-{ident}")
        self.settle()

    def save_held(self, ident, key="old-save", payload=None, status=200):
        self.hold_reply(MEDIA + f"/{ident}", key, payload, status, "PUT")
        self.page.locator("#mediaSubSaveBtn").click()
        self.wait_reply(key)

    def close_editor(self):
        self.page.locator("[data-media-sub-close]").first.click()

    def assert_editor(self, ident, text=None):
        self.assertTrue(self.page.locator("#mediaSubModal").is_visible())
        self.assertEqual(
            self.page.locator("#ms_subscription_id").input_value(), str(ident)
        )
        self.assertEqual(
            self.page.locator("#ms_sites").input_value(), text or f"draft-{ident}"
        )
        self.assertEqual(self.page.locator("#mediaSubFormStatus").inner_text(), "")
        self.assertTrue(self.page.locator("#mediaMappingCandidates").is_hidden())
        self.assertTrue(self.page.locator("#appMessageModal").is_hidden())

    def test_manual_transport_is_cancelled_and_old_row_recovers(self):
        page = self.make_page("guangya")
        failed = []
        page.on("requestfailed", lambda req: failed.append(req.url))
        self.hold_transport[("POST", SCRAPE + "/inspect")] = "A"
        self.manual("A")
        for _ in range(50):
            if "A" in self.pending:
                break
            page.wait_for_timeout(20)
        self.assertIn("A", self.pending)
        self.manual("B")
        self.wait_manual("B")
        self.assertEqual(
            page.locator('[data-media-id="A"] .gy-dir-action-btn').get_attribute(
                "data-state"
            ),
            "idle",
        )
        self.assertTrue(
            any(url.endswith("/inspect") for url in failed), "被替换的只读检查应 abort"
        )
        self.assertTrue(page.locator("#appMessageModal").is_hidden())

    def test_manual_late_check_cannot_replace_new_directory(self):
        page = self.make_page("guangya")
        self.hold_reply(SCRAPE + "/inspect", "A")
        self.manual("A")
        self.wait_reply("A")
        self.manual("B")
        self.wait_manual("B")
        page.locator("#gyScrapeQuery").fill("B-未保存")
        before = page.locator(".gy-scrape-dialog").bounding_box()
        self.release_reply("A")
        self.assertEqual(page.locator("#gyScrapeQuery").input_value(), "B-未保存")
        self.assertEqual(
            page.locator("#gyScrapeDirectory").inner_text(), "B · 1 个视频"
        )
        self.assertTrue(
            page.locator("#gyScrapeQuery").evaluate(
                "el => el === document.activeElement"
            )
        )
        self.assertEqual(before, page.locator(".gy-scrape-dialog").bounding_box())
        page.locator(".gy-scrape-candidate").first.click()
        page.wait_for_function("!document.querySelector('#gyScrapeRunBtn').disabled")
        preview = [r for r in self.requests if r["path"] == SCRAPE + "/preview"][-1]
        self.assertEqual(preview["body"]["inspection_id"], "inspection-B")

    def test_manual_late_check_does_not_reopen_after_close_on_mobile(self):
        page = self.make_page("guangya", 390)
        self.hold_reply(SCRAPE + "/inspect", "A")
        self.manual("A")
        self.wait_reply("A")
        self.manual("B")
        self.wait_manual("B")
        page.locator("#gyScrapeCloseBtn").click()
        self.release_reply("A")
        self.assertTrue(page.locator("#gyScrapeModal").is_hidden())
        self.assertTrue(
            page.locator('[data-media-id="B"] .gy-dir-action-btn').evaluate(
                "el => el === document.activeElement"
            )
        )
        self.manual("B")
        self.wait_manual("B")
        self.assertEqual(
            page.locator("#gyScrapeDirectory").inner_text(), "B · 1 个视频"
        )

    def test_manual_same_directory_reopened_is_a_new_session(self):
        page = self.make_page("guangya")
        self.hold_reply(SCRAPE + "/inspect", "old-A")
        self.manual("A")
        self.wait_reply("old-A")
        self.manual("A")
        self.wait_manual("A")
        page.locator("#gyScrapeCloseBtn").click()
        self.manual("A")
        self.wait_manual("A")
        page.locator("#gyScrapeQuery").fill("A-新会话")
        self.release_reply("old-A")
        self.assertEqual(page.locator("#gyScrapeQuery").input_value(), "A-新会话")

    def test_manual_late_error_is_silent_but_current_error_can_retry(self):
        page = self.make_page("guangya")
        self.hold_reply(
            SCRAPE + "/inspect", "old-error", {"error": "旧 A 检查失败"}, 500
        )
        self.manual("A")
        self.wait_reply("old-error")
        self.manual("B")
        self.wait_manual("B")
        self.release_reply("old-error")
        self.assertTrue(page.locator("#appMessageModal").is_hidden())
        page.locator("#gyScrapeCloseBtn").click()
        self.replies[("POST", SCRAPE + "/inspect")] = [(500, {"error": "当前检查失败"})]
        self.manual("A")
        page.locator("#appMessageModal:not([hidden])").wait_for()
        self.assertIn("当前检查失败", page.locator("#appMessageText").inner_text())
        page.locator("#appMessageClose").click()
        self.manual("A")
        self.wait_manual("A")

    def test_media_late_success_cannot_close_new_editor_or_steal_focus(self):
        page = self.make_page("rss")
        self.edit(1)
        self.save_held(1)
        self.close_editor()
        self.edit(2)
        before = page.locator("#mediaSubModal [role=dialog]").bounding_box()
        self.release_reply("old-save")
        self.assert_editor(2)
        self.assertTrue(
            page.locator("#ms_sites").evaluate("el => el === document.activeElement")
        )
        self.assertEqual(
            before, page.locator("#mediaSubModal [role=dialog]").bounding_box()
        )

    def test_media_late_error_cannot_pollute_new_editor(self):
        self.make_page("rss")
        self.edit(1)
        self.save_held(1, payload={"error": "A 保存失败"}, status=500)
        self.close_editor()
        self.edit(2)
        self.release_reply("old-save")
        self.assert_editor(2)

    def test_media_late_mapping_error_cannot_fill_new_editor(self):
        self.make_page("rss")
        self.edit(1)
        self.save_held(
            1,
            payload={
                "error": "需要映射",
                "code": "mapping_required",
                "candidates": [candidate(999)],
            },
            status=409,
        )
        self.close_editor()
        self.edit(2)
        self.release_reply("old-save")
        self.assert_editor(2)

    def media_old_reply_while_new_save(self, status):
        page = self.make_page("rss")
        self.edit(1)
        self.save_held(
            1,
            payload={"error": "A 失败"} if status == 500 else {"created": False},
            status=status,
        )
        self.close_editor()
        self.edit(2)
        self.assertTrue(
            page.locator("#mediaSubSaveBtn").is_enabled(), "新会话必须能保存"
        )
        before = page.locator("#mediaSubSaveBtn").bounding_box()
        self.save_held(2, "new-save")
        self.release_reply("old-save")
        self.assert_editor(2)
        self.assertTrue(
            page.locator("#mediaSubSaveBtn").is_disabled(),
            "旧 finally 不能解除 B 的 busy",
        )
        self.assertEqual(before, page.locator("#mediaSubSaveBtn").bounding_box())
        self.release_reply("new-save")
        page.locator("#appMessageModal:not([hidden])").wait_for()
        self.assertTrue(page.locator("#mediaSubModal").is_hidden())
        self.assertIn("媒体订阅已更新", page.locator("#appMessageTitle").inner_text())

    def test_media_old_success_cannot_release_new_save_busy(self):
        self.media_old_reply_while_new_save(200)

    def test_media_old_failure_cannot_release_new_save_busy(self):
        self.media_old_reply_while_new_save(500)

    def test_media_same_object_reopened_is_a_new_session(self):
        page = self.make_page("rss", 390)
        self.edit(1)
        self.save_held(1)
        self.close_editor()
        self.edit(1)
        page.locator("#ms_sites").fill("same-object-new-draft")
        self.release_reply("old-save")
        self.assert_editor(1, "same-object-new-draft")

    def test_media_mobile_controls_keep_touch_size_and_busy_geometry(self):
        page = self.make_page("rss", 390)
        self.edit(1)
        controls = page.locator("#mediaSubModal .form-input, #mediaSubModal .form-select")
        metrics = controls.evaluate_all("""elements => elements.map(el => ({
            font: parseFloat(getComputedStyle(el).fontSize),
            height: el.getBoundingClientRect().height,
        }))""")
        self.assertTrue(metrics)
        self.assertTrue(all(item["font"] >= 16 and item["height"] >= 44 for item in metrics), metrics)
        close = page.locator("#mediaSubModal .rss-modal-header .icon-btn").bounding_box()
        assert close is not None
        self.assertGreaterEqual(close["width"], 44)
        self.assertGreaterEqual(close["height"], 44)
        save = page.locator("#mediaSubSaveBtn")
        save.scroll_into_view_if_needed()
        before = save.bounding_box()
        assert before is not None
        self.assertGreaterEqual(before["height"], 44)
        assert page.viewport_size is not None
        self.assertLessEqual(before["y"] + before["height"], page.viewport_size["height"])
        self.save_held(1, payload={"error": "当前保存失败"}, status=500)
        self.assertEqual(before, save.bounding_box())
        self.release_reply("old-save")
        self.assertTrue(save.is_enabled())
        self.assertEqual(before, save.bounding_box())
        self.save_held(1, "long-error", payload={"error": "较长的失败说明" * 60}, status=500)
        self.assertEqual(before, save.bounding_box())
        self.release_reply("long-error")
        self.assertEqual(before, save.bounding_box())
        self.assertTrue(page.locator("#mediaSubFormStatus").evaluate("el => el.scrollHeight > el.clientHeight"))
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))

    def test_media_desktop_control_metrics_are_not_enlarged_by_mobile_rules(self):
        page = self.make_page("rss", 1280)
        self.edit(1)
        self.assertEqual(page.locator("#ms_sites").evaluate("el => getComputedStyle(el).fontSize"), "14px")
        bounding_box_result = page.locator("#mediaSubSaveBtn").bounding_box()
        assert bounding_box_result is not None
        self.assertEqual(bounding_box_result["height"], 42)

    def test_media_current_failure_keeps_input_and_can_retry_successfully(self):
        page = self.make_page("rss")
        self.edit(1)
        self.save_held(1, payload={"error": "当前保存失败"}, status=500)
        self.release_reply("old-save")
        self.assertTrue(page.locator("#mediaSubModal").is_visible())
        self.assertEqual(page.locator("#ms_sites").input_value(), "draft-1")
        self.assertEqual(
            page.locator("#mediaSubFormStatus").inner_text(), "当前保存失败"
        )
        self.assertTrue(page.locator("#mediaSubSaveBtn").is_enabled())
        page.locator("#mediaSubSaveBtn").click()
        page.locator("#appMessageModal:not([hidden])").wait_for()
        self.assertIn("媒体订阅已更新", page.locator("#appMessageTitle").inner_text())
        self.assertTrue(page.locator("#mediaSubModal").is_hidden())

    def test_media_current_mapping_error_is_still_visible(self):
        page = self.make_page("rss")
        self.edit(1)
        self.save_held(
            1,
            payload={
                "error": "需要映射",
                "code": "mapping_required",
                "candidates": [candidate(999)],
            },
            status=409,
        )
        self.release_reply("old-save")
        self.assertTrue(page.locator("#mediaMappingCandidates").is_visible())
        self.assertIn(
            "请选择下方正确的 TMDB", page.locator("#mediaSubFormStatus").inner_text()
        )
        self.assertTrue(page.locator("#mediaSubSaveBtn").is_enabled())

    def test_subscription_center_automatically_loads_all_cursor_pages(self):
        media = [{**SUBSCRIPTIONS[0], "id": number, "tmdb_id": str(number)} for number in range(1, 202)]
        watch = [{"provider": "tmdb", "external_id": str(number), "media_type": "tv",
                  "title": f"收藏 {number}", "year": "2026", "poster_url": "", "subscription": None}
                 for number in range(1, 502)]
        self.replies[("GET", MEDIA)] = [
            (200, {"items": media[:200], "total": 201, "has_more": True,
                   "next": {"before_id": 2, "before_updated_at": "2026-10-21 10:00:00", "limit": 200}}),
            (200, {"items": media[200:], "total": 201, "has_more": False, "next": None}),
        ]
        self.replies[("GET", "/api/subscriptions/watchlist")] = [
            (200, {"items": watch[start:start + 200], "total": 501, "has_more": start < 400,
                   "next": {"before_number": 501 - start - 200, "limit": 200} if start < 400 else None})
            for start in (0, 200, 400)
        ]
        page = self.make_page("rss")
        self.assertEqual(page.locator('[data-subscription-id]').count(), 201)
        page.locator('[data-subscription-tab="watchlist"]').click()
        page.wait_for_function("document.querySelectorAll('.subscription-watch-card').length === 501")
        self.assertEqual(page.locator('#subscriptionWatchlistBadge').inner_text(), "501")
        self.assertTrue(any("before_id=2" in item["query"] for item in self.requests))
        self.assertEqual(sum("before_number=" in item["query"] for item in self.requests), 2)

    def test_failed_continuation_does_not_replace_current_list_with_a_partial_page(self):
        page = self.make_page("rss")
        self.replies[("GET", MEDIA)] = [
            (200, {"items": [{**SUBSCRIPTIONS[0], "id": 999}], "total": 2, "has_more": True,
                   "next": {"before_id": 999, "before_updated_at": "2026-10-21 10:00:00"}}),
            (503, {"error": "续页暂不可用"}),
        ]
        self.hold_reply(MEDIA, "list-refresh", method="GET")
        page.locator('#refreshMediaSubscriptions').click()
        self.wait_reply("list-refresh")
        self.assertEqual(page.locator('[data-subscription-id]').count(), len(SUBSCRIPTIONS))
        self.release_reply("list-refresh")
        page.wait_for_function("!document.getElementById('refreshMediaSubscriptions').disabled")
        self.assertEqual(page.locator('[data-subscription-id]').count(), len(SUBSCRIPTIONS))
        self.assertEqual(page.locator('[data-subscription-id="999"]').count(), 0)

    def test_media_success_waiting_for_refresh_cannot_alert_over_new_editor(self):
        page = self.make_page("rss")
        self.edit(1)
        self.hold_reply(MEDIA, "refresh", method="GET")
        page.locator("#mediaSubSaveBtn").click()
        self.wait_reply("refresh")
        self.assertTrue(page.locator("#mediaSubModal").is_hidden())
        self.edit(2)
        self.release_reply("refresh")
        self.assert_editor(2)
        self.assertTrue(
            page.locator("#ms_sites").evaluate("el => el === document.activeElement")
        )

    def prepare_manual_run(self, name):
        self.manual(name)
        self.wait_manual(name)
        self.page.locator(".gy-scrape-candidate").first.click()
        self.page.wait_for_function(
            "!document.querySelector('#gyScrapeRunBtn').disabled"
        )

    def auto(self, name):
        self.page.locator(f'[data-media-id="{name}"] .gy-dir-action-btn').click()
        self.page.locator('[data-scrape-action="auto"]').click()

    def test_manual_late_submission_error_cannot_release_new_submission(self):
        page = self.make_page("guangya")
        self.prepare_manual_run("A")
        self.hold_reply(SCRAPE + "/run", "old-run", {"error": "old failure"}, 500)
        page.locator("#gyScrapeRunBtn").click()
        self.wait_reply("old-run")
        page.locator("#gyScrapeCloseBtn").click()
        self.prepare_manual_run("B")
        self.hold_reply(SCRAPE + "/run", "new-run", {"error": "current failure"}, 500)
        page.locator("#gyScrapeRunBtn").click()
        self.wait_reply("new-run")
        self.release_reply("old-run")
        self.assertTrue(page.locator("#gyScrapeRunBtn").is_disabled())
        self.assertIn("提交中", page.locator("#gyScrapeRunBtn").inner_text())
        self.assertTrue(page.locator("#appMessageModal").is_hidden())
        self.release_reply("new-run")
        self.assertTrue(page.locator("#gyScrapeRunBtn").is_enabled())
        self.assertIn("current failure", page.locator("#appMessageText").inner_text())

    def test_auto_late_manual_fallback_cannot_replace_new_editor(self):
        page = self.make_page("guangya")
        self.hold_reply(
            SCRAPE + "/run",
            "auto-A",
            {
                "status": "requires_manual",
                "candidates": [candidate(101)],
                "suggested_query": "stale-A",
                "message": "old fallback",
            },
        )
        self.auto("A")
        page.get_by_role("button", name="确认并自动刮削", exact=True).click()
        self.wait_reply("auto-A")
        self.manual("B")
        self.wait_manual("B")
        page.locator("#gyScrapeQuery").fill("draft-B")
        self.release_reply("auto-A")
        self.assertEqual(page.locator("#gyScrapeQuery").input_value(), "draft-B")
        self.assertEqual(
            page.locator("#gyScrapeDirectory").inner_text(), "B · 1 个视频"
        )
        self.assertTrue(page.locator("#appMessageModal").is_hidden())
        page.locator("#gyScrapeCloseBtn").click()
        self.settle()
        self.assertTrue(page.locator("#gyScrapeModal").is_hidden())

    def test_auto_late_inspection_does_not_open_confirmation_over_new_editor(self):
        page = self.make_page("guangya", 390)
        self.hold_reply(SCRAPE + "/inspect", "auto-inspect")
        self.auto("A")
        self.wait_reply("auto-inspect")
        self.manual("B")
        self.wait_manual("B")
        self.release_reply("auto-inspect")
        self.assertTrue(page.locator("#appConfirmModal").is_hidden())
        self.assertEqual(
            page.locator("#gyScrapeDirectory").inner_text(), "B · 1 个视频"
        )
        self.assertFalse(any(r["path"] == SCRAPE + "/run" for r in self.requests))

    def test_auto_current_manual_fallback_still_opens(self):
        page = self.make_page("guangya")
        self.hold_reply(
            SCRAPE + "/run",
            "current-auto",
            {
                "status": "requires_manual",
                "candidates": [candidate(101)],
                "suggested_query": "review-A",
                "message": "select manually",
            },
        )
        self.auto("A")
        page.get_by_role("button", name="确认并自动刮削", exact=True).click()
        self.wait_reply("current-auto")
        self.release_reply("current-auto")
        self.wait_manual("A")
        self.assertEqual(page.locator("#gyScrapeQuery").input_value(), "review-A")
        self.assertIn(
            "select manually", page.locator("#gyScrapePlanSummary").inner_text()
        )


if __name__ == "__main__":
    unittest.main()
