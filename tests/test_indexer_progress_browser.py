from __future__ import annotations

import base64
import json
import shutil
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional browser dependency
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "app/static/js/discovery.js"
PROFILE_DIALOG = ROOT / "app/templates/_media_profile_dialog.html"
STYLES = ROOT / "app/static/css/main.css"


@unittest.skipIf(sync_playwright is None, "未安装 Playwright")
class IndexerProgressBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        executable = next(
            (
                path
                for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
                if (path := shutil.which(name))
            ),
            None,
        )
        if not executable:
            cls.playwright.stop()
            raise unittest.SkipTest("未找到可用的本机 Chrome/Chromium")
        cls.browser = cls.playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--no-sandbox"],
        )
        cls.script = SCRIPT.read_text(encoding="utf-8")
        cls.dialog = PROFILE_DIALOG.read_text(encoding="utf-8")
        cls.styles = STYLES.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        cls.playwright.stop()

    def open_profile(self, *, json_payload=None, search_content_type=None, resource_focus=False):
        html = f"""<!doctype html>
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1">
        <style>{self.styles}</style></head><body>
        <a id="profileLink" data-media-profile-link
           href="/discovery?detail_provider=tmdb&amp;detail_type=movie&amp;detail_id=123{'&amp;resource_focus=1' if resource_focus else ''}">打开条目 123</a>
        <a id="profileLink2" data-media-profile-link
           href="/discovery?detail_provider=tmdb&amp;detail_type=movie&amp;detail_id=456">打开条目 456</a>
        <div data-discovery-root data-discovery-profile-host="true">
            <div hidden>
                <div id="discovery-source-tabs"></div><div id="discovery-filter-region"></div>
                <form id="discovery-search-form"><input id="discovery-search-query"><button id="discovery-search-submit"></button></form>
                <span id="discovery-provider-status"></span><div id="discovery-sections"></div>
                <div id="discovery-grid"></div><div id="discovery-stage"></div>
                <button id="discovery-refresh"></button>
                <div id="discovery-load-more-row"><button id="discovery-load-more"></button></div>
                <div id="discovery-page-sentinel"></div>
            </div>
            <div id="discovery-live"></div>
            {self.dialog}
        </div></body></html>"""
        context = self.browser.new_context(viewport={"width": 1100, "height": 850})
        self.addCleanup(context.close)
        page = context.new_page()
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.route(
            "http://mediaflux.test/**",
            lambda route: route.fulfill(
                status=200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                body=html,
            ),
        )
        page.goto("http://mediaflux.test/discovery", wait_until="domcontentloaded")
        page.evaluate(
            """fixtures => {
                window.__resourceSearchRequests = [];
                window.__streamControllers = [];
                window.__abortedSearches = [];
                window.__readerCancelCalls = [];
                const nativeGetReader = ReadableStream.prototype.getReader;
                ReadableStream.prototype.getReader = function(...args) {
                    const requestIndex = window.__resourceSearchRequests.length - 1;
                    const reader = nativeGetReader.apply(this, args);
                    const nativeCancel = reader.cancel.bind(reader);
                    reader.cancel = reason => {
                        window.__readerCancelCalls.push(requestIndex);
                        return nativeCancel(reason);
                    };
                    return reader;
                };
                window.__searchMode = fixtures.jsonPayload ? 'json' : 'stream';
                window.__detail = id => ({detail: {
                    provider: 'tmdb', media_type: 'movie', external_id: id,
                    tmdb_id: Number(id), title: `测试作品 ${id}`, year: '2024'
                }});
                window.renderLucideIcons = () => {};
                window.fetch = async (input, options = {}) => {
                    const url = String(input);
                    if (url.startsWith('/api/discovery/detail/')) {
                        const id = url.split('/').pop();
                        return new Response(JSON.stringify(window.__detail(id)), {
                            headers: {'Content-Type': 'application/json'}
                        });
                    }
                    if (url === '/api/indexers/search') {
                        const index = window.__resourceSearchRequests.length;
                        window.__resourceSearchRequests.push({
                            url, method: options.method, headers: options.headers,
                            body: options.body, signal: options.signal
                        });
                        if (window.__searchMode === 'json') {
                            return new Response(JSON.stringify(fixtures.jsonPayload), {
                                headers: {'Content-Type': fixtures.searchContentType || 'application/json; charset=utf-8'}
                            });
                        }
                        return new Response(new ReadableStream({
                            start(controller) {
                                window.__streamControllers[index] = controller;
                                options.signal?.addEventListener('abort', () => {
                                    window.__abortedSearches.push(index);
                                    try { controller.error(new DOMException('Aborted', 'AbortError')); } catch (_) {}
                                }, {once: true});
                            }
                        }), {headers: {'Content-Type': 'text/event-stream; charset=utf-8'}});
                    }
                    if (url === '/api/indexers/download') {
                        return new Response(JSON.stringify({ok: false, error: '模拟提交失败状态'}), {
                            headers: {'Content-Type': 'application/json'}
                        });
                    }
                    return new Response('{}', {headers: {'Content-Type': 'application/json'}});
                };
                window.__pushChunks = (index, chunks) => {
                    const controller = window.__streamControllers[index];
                    chunks.forEach(chunk => controller.enqueue(Uint8Array.from(atob(chunk), char => char.charCodeAt(0))));
                };
                window.__finishStream = index => window.__streamControllers[index].close();
            }""",
            {"jsonPayload": json_payload, "searchContentType": search_content_type},
        )
        page.add_script_tag(content=self.script)
        page.locator("#profileLink").click()
        page.wait_for_function("window.__resourceSearchRequests.length === 1")
        self.assertEqual(page_errors, [])
        request = page.evaluate("window.__resourceSearchRequests[0]")
        self.assertEqual(request["url"], "/api/indexers/search")
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["headers"]["Accept"], "text/event-stream")
        self.assertEqual(json.loads(request["body"])["title"], "测试作品 123")
        return page

    @staticmethod
    def progress_payload(items, site_statuses, *, complete=False):
        return {"items": items, "site_statuses": site_statuses, "complete": complete}

    def push_event(self, page, index, event, payload, *, split_utf8=False):
        frame = f"event:{event}\r\ndata:{json.dumps(payload, ensure_ascii=False)}\r\n\r\n".encode("utf-8")
        if split_utf8:
            marker = "结果".encode("utf-8")
            offset = frame.index(marker) + 1
            chunks = [frame[:offset], frame[offset:offset + 1], frame[offset + 1:]]
        else:
            midpoint = max(1, len(frame) // 2)
            chunks = [frame[:midpoint], frame[midpoint:]]
        encoded = [base64.b64encode(chunk).decode("ascii") for chunk in chunks]
        page.evaluate("args => window.__pushChunks(args.index, args.chunks)", {"index": index, "chunks": encoded})

    def test_progress_updates_in_place_and_diagnostics_stay_quiet(self):
        page = self.open_profile()
        first_items = [
            {"result_id": "r1", "site_id": "alpha", "site_name": "Alpha", "title": "首批结果一", "published_at": "2024-01-01T00:00:00Z", "source_url": "https://alpha.test/1"},
            {"result_id": "r2", "site_id": "alpha", "site_name": "Alpha", "title": "首批结果二", "published_at": "2023-01-01T00:00:00Z", "source_url": "https://alpha.test/2"},
        ]
        self.push_event(page, 0, "progress", self.progress_payload(first_items, [
            {"site_id": "alpha", "site_name": "Alpha", "status": "success"},
            {"site_id": "beta", "site_name": "Beta", "status": "searching"},
        ]), split_utf8=True)
        page.locator("[data-resource-result-id='r1']").wait_for()
        page.locator("[data-resource-result-id='r2']").wait_for()
        self.assertEqual(
            page.locator("[data-resource-result-id]").evaluate_all("rows => rows.map(row => row.dataset.resourceResultId)"),
            ["r1", "r2"],
        )
        self.assertIn("正在搜索", page.locator(".discovery-resource-site-status[aria-live='polite']").inner_text())
        self.assertTrue(page.locator("[data-resource-result-id='r1'] .discovery-resource-select").is_visible())
        self.assertEqual(page.locator(".discovery-resource-site-details.is-visible").count(), 0)
        page.evaluate("window.__firstResourceRow = document.querySelector('[data-resource-result-id=\"r1\"]')")
        page.locator("[data-resource-result-id='r1'] .discovery-resource-select").check()
        page.locator("[data-resource-result-id='r1'] [data-resource-submit-target='qb']").click()
        page.locator("[data-resource-result-id='r1'] .discovery-resource-item-status").get_by_text("模拟提交失败状态").wait_for()

        second_items = [
            {**first_items[0], "title": "首批结果一（已更新）", "published_at": "2031-01-01T00:00:00Z"},
            {**first_items[1], "title": "首批结果二（已更新）", "published_at": "2030-01-01T00:00:00Z"},
            {"result_id": "r3", "site_id": "gamma", "site_name": "Gamma", "title": "后续新增结果", "published_at": "2040-01-01T00:00:00Z", "source_url": "https://gamma.test/3"},
        ]
        self.push_event(page, 0, "progress", self.progress_payload(second_items, [
            {"site_id": "alpha", "site_name": "Alpha", "status": "success"},
            {"site_id": "beta", "site_name": "Beta", "status": "error", "diagnostics": [
                {"site_id": "beta", "site_name": "Beta", "status": "error", "count": 0, "cached": True, "code": "timeout", "message": "响应超时"}
            ]},
            {"site_id": "gamma", "site_name": "Gamma", "status": "success"},
        ]))
        page.locator("[data-resource-result-id='r3']").wait_for()
        self.assertEqual(
            page.locator("[data-resource-result-id]").evaluate_all("rows => rows.map(row => row.dataset.resourceResultId)"),
            ["r1", "r2", "r3"],
        )
        self.assertTrue(page.evaluate("window.__firstResourceRow === document.querySelector('[data-resource-result-id=\"r1\"]')"))
        self.assertTrue(page.locator("[data-resource-result-id='r1'] .discovery-resource-select").is_checked())
        self.assertIn("模拟提交失败状态", page.locator("[data-resource-result-id='r1'] .discovery-resource-item-status").inner_text())
        self.assertEqual(page.locator("[data-resource-result-id='r1'] h4").inner_text(), "首批结果一（已更新）")
        self.assertEqual(page.locator(".discovery-resource-site-details.is-visible").count(), 0)

        page.locator("[data-resource-site-filter='beta']").focus()
        diagnostics = page.locator(".discovery-resource-site-details.is-visible")
        diagnostics.wait_for()
        self.assertIn("响应超时", diagnostics.inner_text())
        self.assertIn("timeout", diagnostics.inner_text())
        self.assertIn("0 条", diagnostics.inner_text())
        self.assertIn("来自缓存", diagnostics.inner_text())

        self.push_event(page, 0, "complete", self.progress_payload(second_items, [
            {"site_id": "alpha", "site_name": "Alpha", "status": "success"},
            {"site_id": "beta", "site_name": "Beta", "status": "error", "diagnostics": [
                {"site_id": "beta", "site_name": "Beta", "status": "error", "count": 0, "cached": True, "code": "timeout", "message": "响应超时"}
            ]},
            {"site_id": "gamma", "site_name": "Gamma", "status": "success"},
        ], complete=True))
        page.wait_for_function("window.__readerCancelCalls.includes(0)")
        summary = page.locator(".discovery-resource-site-status[aria-live='polite']")
        page.wait_for_function("() => [...document.querySelectorAll('.discovery-resource-site-status[aria-live=\"polite\"]')].some(el => el.innerText.includes('搜索完成'))")
        self.assertEqual(summary.evaluate("el => el.firstChild.textContent"), "检索")
        self.assertIn("搜索完成", summary.get_attribute("aria-label"))
        self.assertIn("搜索完成", summary.get_attribute("title"))
        self.assertTrue(summary.evaluate("el => el.classList.contains('is-success')"))
        self.assertEqual(page.locator("[data-resource-result-id]").count(), 3)

    def test_resource_focus_is_not_stolen_when_background_search_finishes(self):
        page = self.open_profile(resource_focus=True)
        page.wait_for_timeout(60)
        first = {"result_id": "focused", "site_id": "alpha", "title": "Demo", "download_state": "ready", "download_kinds": ["magnet"]}
        self.push_event(page, 0, "progress", self.progress_payload([first], [{"site_id": "alpha", "status": "success"}]))
        checkbox = page.locator("[data-resource-result-id='focused'] .discovery-resource-select")
        checkbox.wait_for()
        checkbox.check()
        checkbox.focus()
        page.evaluate("window.__focusedCheckbox = document.activeElement")
        self.push_event(page, 0, "complete", self.progress_payload([first], [{"site_id": "alpha", "status": "success"}], complete=True))
        page.wait_for_timeout(120)
        self.assertTrue(page.evaluate("document.activeElement === window.__focusedCheckbox"))
        self.assertTrue(checkbox.is_checked())

    def test_late_correct_match_sorts_before_noise_without_losing_selection_or_focus(self):
        page = self.open_profile()
        self.assertEqual(page.locator("[data-resource-sort]").input_value(), "relevance_desc")
        statuses = [{"site_id": "alpha", "site_name": "Alpha", "status": "success"}]
        noise = [{"result_id": f"noise-{i}", "site_id": "alpha", "title": f"无关作品{i}",
                  "match_priority": 3, "relevance_score": 95, "published_at": "2026-10-05T00:00:00Z"}
                 for i in range(10)]
        self.push_event(page, 0, "progress", self.progress_payload(noise, statuses))
        selected = page.locator("[data-resource-result-id='noise-0'] .discovery-resource-select")
        selected.wait_for()
        selected.check()
        selected.focus()
        page.evaluate("window.__selectedRow = document.activeElement.closest('[data-resource-result-id]'); window.__selectedTop = window.__selectedRow.getBoundingClientRect().top")
        correct = {"result_id": "correct", "site_id": "alpha", "title": "起义.2026.1080p", "match_priority": 0,
                   "relevance_score": 83, "published_at": "2026-01-01T00:00:00Z"}
        self.push_event(page, 0, "complete", self.progress_payload([*noise, correct], statuses, complete=True))
        page.wait_for_function("window.__readerCancelCalls.includes(0)")
        self.assertEqual(page.locator("[data-resource-result-id]").first.get_attribute("data-resource-result-id"), "correct")
        self.assertTrue(selected.is_checked())
        self.assertTrue(page.evaluate("document.activeElement.closest('[data-resource-result-id]') === window.__selectedRow"))
        self.assertLessEqual(abs(page.evaluate("window.__selectedRow.getBoundingClientRect().top - window.__selectedTop")), 1)
        page.locator("[data-resource-sort]").select_option("published_desc")
        page.wait_for_function("window.__resourceSearchRequests.length === 2")
        self.push_event(page, 1, "complete", self.progress_payload([*noise, correct], statuses, complete=True))
        page.wait_for_function("window.__readerCancelCalls.includes(1)")
        self.assertEqual(page.locator("[data-resource-result-id]").first.get_attribute("data-resource-result-id"), "correct")
        self.assertEqual(page.locator("[data-resource-result-id]").count(), 11)

    def test_resort_transfers_selection_only_to_the_same_resource_new_reference(self):
        page = self.open_profile()
        statuses = [{"site_id": "alpha", "status": "success"}]
        first = {"result_id": "old", "resource_key": "hash-a", "site_id": "alpha", "title": "相同标题", "download_state": "ready"}
        self.push_event(page, 0, "complete", self.progress_payload([first], statuses, complete=True))
        page.wait_for_function("window.__readerCancelCalls.includes(0)")
        page.locator("[data-resource-result-id='old'] .discovery-resource-select").check()
        page.locator("[data-resource-sort]").select_option("size_desc")
        page.wait_for_function("window.__resourceSearchRequests.length === 2")
        fresh = [{**first, "result_id": "new-b", "resource_key": "hash-b"}, {**first, "result_id": "new-a"}]
        self.push_event(page, 1, "complete", self.progress_payload(fresh, statuses, complete=True))
        page.wait_for_function("window.__readerCancelCalls.includes(1)")
        self.assertTrue(page.locator("[data-resource-result-id='new-a'] .discovery-resource-select").is_checked())
        self.assertFalse(page.locator("[data-resource-result-id='new-b'] .discovery-resource-select").is_checked())
        self.assertIn("已选 1 条", page.locator("[data-resource-batch-summary]").inner_text())
        self.assertFalse(page.locator("[data-resource-batch-target='qb']").is_disabled())
        # 等待下一轮排序期间取消勾选，后到的同资源不可再自动恢复选择。
        page.locator("[data-resource-sort]").select_option("published_desc")
        page.wait_for_function("window.__resourceSearchRequests.length === 3")
        page.locator("[data-resource-result-id='new-a'] .discovery-resource-select").uncheck()
        self.push_event(page, 2, "complete", self.progress_payload([{**first, "result_id": "third-a"}], statuses, complete=True))
        page.wait_for_function("window.__readerCancelCalls.includes(2)")
        self.assertFalse(page.locator("[data-resource-result-id='third-a'] .discovery-resource-select").is_checked())
        self.assertIn("已选 0 条", page.locator("[data-resource-batch-summary]").inner_text())

    def test_source_order_uses_server_snapshot_order_instead_of_arrival_or_score(self):
        page = self.open_profile()
        page.locator("[data-resource-sort]").select_option("source_order")
        page.wait_for_function("window.__resourceSearchRequests.length === 2")
        statuses = [{"site_id": "alpha", "status": "success"}]
        early = {"result_id": "early", "site_id": "alpha", "title": "先到且高分", "relevance_score": 99, "match_priority": 0}
        later = {"result_id": "later", "site_id": "alpha", "title": "源站本来排在前面", "relevance_score": 1, "match_priority": 3}
        self.push_event(page, 1, "progress", self.progress_payload([early], statuses))
        page.locator("[data-resource-result-id='early']").wait_for()
        self.push_event(page, 1, "complete", self.progress_payload([later, early], statuses, complete=True))
        page.wait_for_function("window.__readerCancelCalls.includes(1)")
        self.assertEqual(page.locator("[data-resource-result-id]").evaluate_all("rows => rows.map(row => row.dataset.resourceResultId)"), ["later", "early"])

    def test_completion_count_includes_retained_earlier_candidates(self):
        page = self.open_profile()
        statuses = [{"site_id": "alpha", "site_name": "Alpha", "status": "success"}]
        self.push_event(page, 0, "progress", self.progress_payload([
            {"result_id": "early", "site_id": "alpha", "title": "先返回的候选"},
        ], statuses))
        page.locator("[data-resource-result-id='early']").wait_for()
        self.push_event(page, 0, "complete", self.progress_payload([
            {"result_id": "later", "site_id": "alpha", "title": "最终排序截断后的候选"},
        ], statuses, complete=True))
        page.wait_for_function("window.__readerCancelCalls.includes(0)")
        self.assertEqual(page.locator("[data-resource-result-id]").count(), 2)
        self.assertIn("本轮 2 条结果", page.locator(".discovery-resource-site-status[aria-live='polite']").get_attribute("aria-label"))

    def test_candidate_cap_is_information_not_failure_or_hidden_load_more(self):
        page = self.open_profile()
        payload = self.progress_payload([
            {"result_id": "r1", "site_id": "alpha", "title": "Demo"},
        ], [{"site_id": "alpha", "site_name": "Alpha", "status": "success", "message": "达到候选上限，可细化条件"}], complete=True)
        payload["truncated"] = True
        payload["has_more"] = False
        self.push_event(page, 0, "complete", payload)
        page.wait_for_function("window.__readerCancelCalls.includes(0)")
        progress = page.locator(".discovery-resource-site-status[aria-live='polite']")
        self.assertIn("候选上限", progress.get_attribute("aria-label"))
        self.assertNotIn("失败", progress.get_attribute("aria-label"))
        self.assertEqual(page.locator("[data-resource-result-id]").count(), 1)

    def test_source_diagnostic_focus_survives_progress_and_completion(self):
        page = self.open_profile()
        items = [{"result_id": "r1", "site_id": "alpha", "title": "资源"}]
        statuses = [
            {"site_id": "alpha", "site_name": "Alpha", "status": "success"},
            {"site_id": "beta", "site_name": "Beta", "status": "error", "message": "响应超时", "retryable": True},
        ]
        self.push_event(page, 0, "progress", self.progress_payload(items, statuses))
        page.locator("[data-resource-result-id='r1']").wait_for()
        page.locator("[data-resource-site-filter='beta']").focus()
        self.push_event(page, 0, "complete", self.progress_payload(items, statuses, complete=True))
        page.wait_for_function("window.__readerCancelCalls.includes(0)")
        self.assertEqual(page.evaluate("document.activeElement.getAttribute('data-resource-site-filter')"), "beta")
        self.assertTrue(page.locator("[data-resource-site-retry='beta']").is_visible())

    def test_close_cancels_old_search_and_stale_snapshot_cannot_cross_detail(self):
        page = self.open_profile()
        self.push_event(page, 0, "progress", self.progress_payload([
            {"result_id": "stale", "site_id": "alpha", "site_name": "Alpha", "title": "旧作品迟到结果"}
        ], [{"site_id": "alpha", "site_name": "Alpha", "status": "success"}]))
        page.locator("[data-discovery-dialog-close]").first.click()
        page.wait_for_function("window.__abortedSearches.includes(0)")
        page.locator("#profileLink2").click()
        page.wait_for_function("window.__resourceSearchRequests.length === 2")
        page.wait_for_timeout(350)
        self.assertEqual(page.locator("[data-resource-result-id='stale']").count(), 0)
        self.push_event(page, 1, "progress", self.progress_payload([
            {"result_id": "fresh", "site_id": "beta", "site_name": "Beta", "title": "新作品结果"}
        ], [{"site_id": "beta", "site_name": "Beta", "status": "success"}]))
        page.locator("[data-resource-result-id='fresh']").wait_for()
        self.assertEqual(page.locator("[data-resource-result-id]").evaluate_all("rows => rows.map(row => row.dataset.resourceResultId)"), ["fresh"])

    def test_disconnect_keeps_delivered_results_and_never_reports_complete(self):
        page = self.open_profile()
        self.push_event(page, 0, "progress", self.progress_payload([
            {"result_id": "kept", "site_id": "alpha", "site_name": "Alpha", "title": "已到达结果"}
        ], [{"site_id": "alpha", "site_name": "Alpha", "status": "success"}]))
        page.locator("[data-resource-result-id='kept']").wait_for()
        page.evaluate("window.__finishStream(0)")
        page.wait_for_function("window.__readerCancelCalls.includes(0)")
        notice = page.locator("[data-resource-notice]")
        notice.wait_for(state="visible")
        self.assertIn("本次搜索未完成", notice.inner_text())
        self.assertTrue(page.locator("[data-resource-result-id='kept']").is_visible())
        progress = page.locator(".discovery-resource-site-status[aria-live='polite']")
        self.assertIn("本次搜索未完成", progress.inner_text())
        self.assertNotIn("搜索完成", progress.inner_text())

    def test_empty_progress_snapshot_keeps_skeleton_until_delayed_first_results(self):
        page = self.open_profile()
        self.push_event(page, 0, "progress", self.progress_payload([], [
            {"site_id": "alpha", "site_name": "Alpha", "status": "searching"},
        ]))
        progress = page.locator(".discovery-resource-site-status[aria-live='polite']")
        page.wait_for_function("el => el?.getAttribute('aria-label')?.includes('正在搜索站点')", arg=progress.element_handle())
        skeletons = page.locator("[data-discovery-resource-list] .discovery-resource-row.is-skeleton")
        self.assertEqual(skeletons.count(), 4)
        self.assertEqual(page.locator("[data-resource-search-empty]").count(), 0)
        page.wait_for_timeout(120)
        self.assertEqual(skeletons.count(), 4)
        self.assertEqual(page.locator("[data-resource-result-id]").count(), 0)

        self.push_event(page, 0, "progress", self.progress_payload([
            {"result_id": "late-result", "site_id": "alpha", "site_name": "Alpha", "title": "稍后到达"},
        ], [{"site_id": "alpha", "site_name": "Alpha", "status": "success"}]))
        page.locator("[data-resource-result-id='late-result']").wait_for()
        self.assertEqual(skeletons.count(), 0)
        self.assertEqual(page.locator("[data-resource-search-empty]").count(), 0)
        self.assertEqual(page.locator("[data-resource-result-id]").count(), 1)

    def test_progress_chip_keeps_filter_positions_and_row_focus_stable(self):
        page = self.open_profile()
        first = [{"result_id": "stable", "site_id": "alpha", "site_name": "Alpha", "title": "稳定首批结果"}]
        self.push_event(page, 0, "progress", self.progress_payload(first, [
            {"site_id": "alpha", "site_name": "Alpha", "status": "success"},
            {"site_id": "beta", "site_name": "Beta", "status": "searching"},
        ]))
        row = page.locator("[data-resource-result-id='stable']")
        row.wait_for()
        checkbox = row.locator(".discovery-resource-select")
        checkbox.focus()
        page.evaluate("""() => {
            const row = document.querySelector('[data-resource-result-id="stable"]');
            const all = document.querySelector('[data-resource-site-filter=""]');
            const rect = row.getBoundingClientRect();
            window.__stableStreamNodes = {
                row, checkbox: row.querySelector('.discovery-resource-select'),
                rowX: rect.x, rowY: rect.y,
                allX: all.getBoundingClientRect().x,
            };
        }""")

        self.push_event(page, 0, "progress", self.progress_payload([
            *first,
            {"result_id": "later", "site_id": "beta", "site_name": "Beta", "title": "后续新增结果"},
        ], [
            {"site_id": "alpha", "site_name": "Alpha", "status": "success"},
            {"site_id": "beta", "site_name": "Beta", "status": "success"},
        ]))
        page.locator("[data-resource-result-id='later']").wait_for()
        self.assertTrue(page.evaluate("""() => {
            const saved = window.__stableStreamNodes;
            const row = document.querySelector('[data-resource-result-id="stable"]');
            const rect = row.getBoundingClientRect();
            const all = document.querySelector('[data-resource-site-filter=""]');
            return saved.row === row
                && saved.checkbox === row.querySelector('.discovery-resource-select')
                && document.activeElement === saved.checkbox
                && Math.abs(saved.rowX - rect.x) < 0.5
                && Math.abs(saved.rowY - rect.y) < 0.5
                && Math.abs(saved.allX - all.getBoundingClientRect().x) < 0.5;
        }"""))
        progress = page.locator(".discovery-resource-site-status[aria-live='polite']")
        self.assertEqual(progress.evaluate("el => el.firstChild.textContent"), "检索")
        self.assertIn("已找到 2 条", progress.get_attribute("aria-label"))
        self.assertIn("已找到 2 条", progress.get_attribute("title"))

    def test_same_context_empty_partial_refresh_preserves_selected_old_result(self):
        page = self.open_profile()
        initial = [{"result_id": "old", "site_id": "alpha", "site_name": "Alpha", "title": "旧结果"}]
        self.push_event(page, 0, "progress", self.progress_payload(initial, [
            {"site_id": "alpha", "site_name": "Alpha", "status": "success"},
        ]))
        row = page.locator("[data-resource-result-id='old']")
        row.wait_for()
        checkbox = row.locator(".discovery-resource-select")
        checkbox.check()
        self.push_event(page, 0, "complete", self.progress_payload(initial, [
            {"site_id": "alpha", "site_name": "Alpha", "status": "success"},
        ], complete=True))
        page.wait_for_timeout(50)
        page.locator("[data-resource-sort]").select_option("seeders_desc")
        page.wait_for_function("window.__resourceSearchRequests.length === 2")
        previous_row = row.element_handle()

        self.push_event(page, 1, "progress", self.progress_payload([], [
            {"site_id": "alpha", "site_name": "Alpha", "status": "searching"},
        ]))
        self.assertTrue(row.is_visible())
        self.assertTrue(checkbox.is_checked())
        self.assertEqual(page.locator("[data-resource-search-empty]").count(), 0)

        self.push_event(page, 1, "complete", self.progress_payload([], [
            {"site_id": "alpha", "site_name": "Alpha", "status": "empty"},
            {"site_id": "beta", "site_name": "Beta", "status": "error", "message": "超时"},
        ], complete=True))
        notice = page.locator("[data-resource-notice]")
        notice.wait_for(state="visible")
        self.assertIn("本轮检索不完整，未取得新结果", notice.inner_text())
        self.assertIn("已保留 1 条现有结果", notice.inner_text())
        self.assertTrue(page.evaluate("row => row === document.querySelector('[data-resource-result-id=old]')", previous_row))
        self.assertTrue(checkbox.is_checked())
        summary = page.locator(".discovery-resource-site-status[aria-live='polite']")
        self.assertNotIn("本轮 1 条结果", summary.get_attribute("aria-label"))

    def test_sse_terminal_error_is_not_a_site_snapshot_and_keeps_results(self):
        page = self.open_profile()
        self.push_event(page, 0, "progress", self.progress_payload([
            {"result_id": "kept", "site_id": "alpha", "site_name": "Alpha", "title": "已到达结果"},
        ], [{"site_id": "alpha", "site_name": "Alpha", "status": "success"}]))
        row = page.locator("[data-resource-result-id='kept']")
        row.wait_for()
        checkbox = row.locator(".discovery-resource-select")
        checkbox.check()
        page.evaluate("window.__keptRow = document.querySelector('[data-resource-result-id=kept]')")
        self.push_event(page, 0, "complete", self.progress_payload([
            {"result_id": "kept", "site_id": "alpha", "site_name": "Alpha", "title": "已到达结果"},
        ], [{"site_id": "alpha", "site_name": "Alpha", "status": "success"}], complete=True))
        page.wait_for_timeout(50)
        page.locator("[data-resource-sort]").select_option("seeders_desc")
        page.wait_for_function("window.__resourceSearchRequests.length === 2")
        self.push_event(page, 1, "error", {"error": "服务端终止", "code": "service_failed"})
        page.wait_for_function("window.__readerCancelCalls.includes(1)")

        notice = page.locator("[data-resource-notice]")
        notice.wait_for(state="visible")
        self.assertIn("服务端终止", notice.inner_text())
        self.assertIn("service_failed", notice.inner_text())
        self.assertIn("已保留 1 条已显示结果", notice.inner_text())
        self.assertTrue(page.evaluate("() => window.__keptRow === document.querySelector('[data-resource-result-id=kept]')"))
        self.assertTrue(checkbox.is_checked())
        self.assertNotIn("服务端终止", page.locator("[data-resource-site-filter='alpha']").get_attribute("aria-label"))
        self.assertNotIn("搜索完成", page.locator(".discovery-resource-site-status[aria-live='polite']").get_attribute("aria-label"))

    def test_non_json_content_type_is_not_parsed_as_json(self):
        payload = {
            "items": [{"result_id": "must-not-appear", "site_id": "alpha", "site_name": "Alpha", "title": "不能被当 JSON"}],
            "site_statuses": [{"site_id": "alpha", "site_name": "Alpha", "status": "success"}],
        }
        page = self.open_profile(json_payload=payload, search_content_type="text/plain; charset=utf-8")
        page.locator(".discovery-resource-state.is-error").wait_for()
        self.assertEqual(page.locator("[data-resource-result-id='must-not-appear']").count(), 0)

    def test_empty_search_summary_distinguishes_searching_partial_and_failure(self):
        cases = (
            (
                [{"site_id": "alpha", "site_name": "Alpha", "status": "searching"}],
                "正在搜索站点",
                "is-pending",
            ),
            (
                [
                    {"site_id": "alpha", "site_name": "Alpha", "status": "empty"},
                    {"site_id": "beta", "site_name": "Beta", "status": "error"},
                ],
                "部分站点未成功",
                "is-pending",
            ),
            (
                [
                    {"site_id": "alpha", "site_name": "Alpha", "status": "error"},
                    {"site_id": "beta", "site_name": "Beta", "status": "error"},
                ],
                "综合搜索失败",
                "is-error",
            ),
            (
                [
                    {"site_id": "sukebei", "site_name": "Sukebei", "status": "disabled"},
                    {"site_id": "btbtla", "site_name": "综合", "status": "error"},
                ],
                "综合搜索失败",
                "is-error",
            ),
        )
        for statuses, expected_text, expected_class in cases:
            with self.subTest(expected_text=expected_text):
                page = self.open_profile(json_payload={"items": [], "site_statuses": statuses})
                page.wait_for_function(
                    "text => [...document.querySelectorAll('.discovery-resource-site-status[aria-live=\"polite\"]')].some(el => el.innerText.includes(text))",
                    arg=expected_text,
                )
                summary = page.locator(".discovery-resource-site-status[aria-live='polite']")
                self.assertIn(expected_text, summary.inner_text())
                self.assertTrue(summary.evaluate("(el, className) => el.classList.contains(className)", expected_class))

    def test_json_response_remains_a_single_complete_snapshot(self):
        payload = {
            "items": [{"result_id": "json-result", "site_id": "alpha", "site_name": "Alpha", "title": "JSON 兼容结果"}],
            "site_statuses": [{"site_id": "alpha", "site_name": "Alpha", "status": "success"}],
        }
        page = self.open_profile(json_payload=payload)
        page.locator("[data-resource-result-id='json-result']").wait_for()
        summary = page.locator(".discovery-resource-site-status[aria-live='polite']")
        self.assertIn("搜索完成", summary.inner_text())
        self.assertTrue(summary.evaluate("el => el.classList.contains('is-success')"))
        self.assertEqual(page.locator("[data-resource-result-id]").count(), 1)


if __name__ == "__main__":
    unittest.main()
