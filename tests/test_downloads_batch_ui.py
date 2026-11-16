"""下载实时任务多选与批量工具栏前端契约。"""
from __future__ import annotations

import re
import shutil
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app/templates/downloads.html"
BASE_TEMPLATE = ROOT / "app/templates/base.html"
STYLES = ROOT / "app/static/css/main.css"


class DownloadBatchUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = TEMPLATE.read_text(encoding="utf-8") + (ROOT / "app/static/js/downloads.js").read_text(encoding="utf-8")
        self.base_template = BASE_TEMPLATE.read_text(encoding="utf-8")
        self.styles = STYLES.read_text(encoding="utf-8")

    def test_task_toolbar_exposes_select_all_and_three_batch_actions(self):
        for contract in (
            'id="qbTaskToolbar"',
            'id="qbSelectAll"',
            'id="qbSelectionLabel"',
            'id="qbBulkResume"',
            'id="qbBulkPause"',
            'id="qbBulkDelete"',
        ):
            self.assertIn(contract, self.template)
        self.assertNotIn('id="qbClearSelection"', self.template)
        self.assertRegex(
            self.template,
            re.compile(r'id="qbBulkResume"[^>]+disabled.*?>.*?启动', re.S),
        )
        self.assertRegex(
            self.template,
            re.compile(r'id="qbBulkPause"[^>]+disabled.*?>.*?停止', re.S),
        )
        self.assertRegex(
            self.template,
            re.compile(r'id="qbBulkDelete"[^>]+disabled.*?>.*?删除', re.S),
        )

    def test_selection_is_hash_based_and_survives_background_refresh(self):
        for contract in (
            "const selectedQbHashes = new Set()",
            "let currentQbTasks = []",
            "function currentQbHashes()",
            "function syncQbSelectionControls()",
            "if(!available.has(hash))selectedQbHashes.delete(hash)",
            "data-qb-select",
            "selectAll.indeterminate",
            "let overviewRefreshQueued = false",
            "overviewRefreshQueued=true",
            "if(loading||qbActionBusy)return overviewPromise",
            "while(overviewRefreshQueued&&!qbActionBusy)",
            "const keepPreviousTasks=qb.error_code==='connection_failed'",
        ):
            self.assertIn(contract, self.template)
        self.assertNotIn("selectedQbHashes.clear();\n    const taskCount", self.template)
        self.assertIn("document.addEventListener('visibilitychange',syncOverviewPolling)", self.template)
        self.assertIn("if(document.hidden)", self.template)

    def test_background_reads_keep_previous_data_and_ignore_stale_pages(self):
        for contract in (
            "async function readApiResponse(response)",
            "if(!response.ok)throw new Error",
            "let downloadLogRequestSerial = 0",
            "let downloadIssueRequestSerial = 0",
            "if(requestSerial!==downloadLogRequestSerial)return false",
            "if(requestSerial!==downloadIssueRequestSerial)return false",
            "if(!hasLoadedDownloadLogs)",
            "if(!hasLoadedDownloadIssues)",
        ):
            self.assertIn(contract, self.template)
        self.assertNotIn("currentLogIds=[];document.getElementById('logList')", self.template)
        self.assertNotIn("currentIssueIds=[];document.getElementById('issueList')", self.template)
        self.assertNotIn("then(r=>r.json()).then(d=>", self.template)

    def test_batch_actions_send_arrays_and_delete_keeps_files(self):
        for contract in (
            "async function runQbAction(action, hashes, clearSelection=false)",
            "JSON.stringify({hashes:unique})",
            "qbBulkAction('resume')",
            "qbBulkAction('pause')",
            "qbBulkAction('delete')",
            "从 qBittorrent 移除任务，并停止对应未完成下载的本地跟踪；不删除已经下载的文件。",
            "selectedQbHashes.clear()",
        ):
            self.assertIn(contract, self.template)

    def test_delete_keeps_row_visible_until_server_confirms(self):
        single_delete = self.template.split("async function qbDelete(hash){", 1)[1].split("async function qbBulkAction", 1)[0]
        self.assertNotIn("slideOutAndCollapse", single_delete)
        self.assertIn("return runQbAction('delete',[hash],false)", single_delete)
        self.assertIn("cancelled:['已停止跟踪','paused']", self.template)

    def test_toolbar_has_stable_geometry_and_mobile_layout(self):
        for contract in (
            ".qb-task-toolbar { min-height: 42px",
            ".qb-task-title-layout",
            ".qb-task-row.is-selected td",
            ".qb-task-toolbar { display: flex; align-items: center; justify-content: space-between; flex-direction: row;",
            ".qb-bulk-actions { display: flex; align-items: center; justify-content: flex-end; gap: 4px;",
        ):
            self.assertIn(contract, self.styles)

    def test_qb_523_states_use_safe_and_stable_task_controls(self):
        for contract in (
            "const qbPausedStates = new Set(['pausedDL','pausedUP','stoppedDL','stoppedUP'])",
            "const qbStoppableStates = new Set(",
            "const qbTransientStates = new Set(['checkingDL','checkingUP','checkingResumeData','moving'])",
            "function qbTaskControl(state)",
            "return {action:'',label:'处理中',icon:'loader-circle',disabled:true}",
            "control.disabled?'disabled aria-disabled=\"true\"'",
            "checkingResumeData:'检查恢复数据'",
            "forcedMetaDL:'强制获取元数据'",
            "width:76px;height:26px",
        ):
            self.assertIn(contract, self.template)
        self.assertNotIn("const active=['downloading','forcedDL','metaDL'].includes(t.state)", self.template)
        self.assertNotIn("${active", self.template)
        self.assertGreaterEqual(self.template.count('title="${control.label}"'), 2)

    def test_issue_and_log_tabs_expose_stable_batch_selection_toolbars(self):
        for contract in (
            'id="issueBatchToolbar"',
            'id="issueSelectAll"',
            'id="issueSelectionLabel"',
            'id="issueBulkQb"',
            'id="issueBulkGuangya"',
            'id="issueBulkBoth"',
            'id="issueBulkClear"',
            'id="logBatchToolbar"',
            'id="logSelectAll"',
            'id="logSelectionLabel"',
            'id="logBulkClear"',
            'data-issue-select',
            'data-log-select',
            "const selectedIssueIds = new Set()",
            "const selectedLogIds = new Set()",
            "function syncIssueSelectionControls()",
            "function syncLogSelectionControls()",
        ):
            self.assertIn(contract, self.template)
        self.assertNotIn('id="issueClearSelection"', self.template)
        self.assertNotIn('id="logClearSelection"', self.template)
        for contract in (
            ".download-batch-toolbar { min-height: 42px",
            ".download-select-layout",
            ".download-batch-row.is-selected td",
            ".download-batch-actions .rss-btn { min-width: 68px; min-height: 30px",
            ".download-batch-toolbar { display: flex; align-items: center; justify-content: space-between; flex-direction: row;",
            ".download-batch-actions { width: auto; display: flex; align-items: center; justify-content: flex-end; gap: 4px;",
        ):
            self.assertIn(contract, self.styles)

    def test_download_logs_use_fixed_columns_and_safe_source_labels(self):
        for contract in (
            'class="dl-table download-log-table"',
            'class="download-log-source-col"',
            'function downloadLogSourceSvg(key)',
            'function downloadLogSourceHtml(source)',
            "label:'qBittorrent',tone:'is-qb'",
            "label:'光鸭',tone:'is-guangya'",
            '<circle cx="12" cy="12" r="10" fill="currentColor"/>',
            'fill="#ffd45c"',
            'class="source-label download-log-source-badge ${item.tone}"',
            'function downloadLogTarget(path)',
            "label:'种子直链',detail:'HTTPS · 已脱敏'",
            "label:'磁力链接',detail:'MAGNET · 已脱敏'",
            'class="download-log-target-cell desktop-only-cell"',
            'class="download-log-mobile-details"',
        ):
            self.assertIn(contract, self.template)
        for contract in (
            ".download-log-table { table-layout: fixed; }",
            ".download-log-table .download-log-title-cell { min-width: 0; max-width: none; }",
            ".download-log-source-badge { display: inline-flex;",
            ".download-log-source-icon svg { width: 24px; height: 24px; display: block; }",
            ".download-log-source-layout .download-row-check { width: 20px; height: 24px; min-height: 24px; display: flex; align-items: center;",
            ".download-log-source-badge.is-qb { color: var(--info); }",
            ".download-log-source-badge.is-guangya { color: var(--accent); }",
            ".download-log-target { display: grid;",
            ".download-log-mobile-details { display: none; }",
        ):
            self.assertIn(contract, self.styles)
        self.assertNotIn('title="${attr(r.path)}"', self.template)
        self.assertNotIn("${esc(r.path||'-')}</td>", self.template)
        self.assertRegex(
            self.template,
            re.compile(
                r'class="download-log-source-cell".*?data-log-select.*?downloadLogSourceHtml\(r\.source\)',
                re.S,
            ),
        )
        self.assertIn('class="status-pill ${st[1]} download-log-mobile-status"', self.template)
        self.assertIn(
            ".dl-tab-btn { min-width: 0; flex: 1 1 0;",
            self.styles,
        )

    def test_main_stylesheet_uses_content_hash_authority(self):
        self.assertIn("static_url('css/main.css')", self.base_template)
        self.assertNotRegex(self.base_template, r"css/main\.css[^\n]*\?v=20\d{6}[a-z]")

    def test_issue_batch_actions_support_qb_guangya_both_and_safe_cleanup(self):
        for contract in (
            "async function resubmitIssuesBatch(target)",
            "resubmitIssuesBatch('qb')",
            "resubmitIssuesBatch('guangya')",
            "resubmitIssuesBatch('both')",
            "'/api/downloads/issues/batch/resubmit'",
            "async function clearIssuesBatch()",
            "'/api/downloads/issues/batch/clear'",
            "不会删除下载任务、实际文件、原请求或下载日志",
            "不支持的记录会跳过",
        ):
            self.assertIn(contract, self.template)

    def test_log_batch_cleanup_is_explicitly_non_destructive_to_tasks_and_files(self):
        for contract in (
            "async function clearLogsBatch(allMatching=false)",
            "'/api/downloads/logs/batch/clear'",
            "不会停止 qB/光鸭任务",
            "不会删除已下载文件或下载请求",
            "currentLogIds.forEach",
        ):
            self.assertIn(contract, self.template)


class DownloadLogCleanupBrowserTests(unittest.TestCase):
    def test_active_lists_refresh_without_losing_pending_pages_or_previous_rows(self):
        """完整下载模板、样式及脚本；所有请求在真实 Chromium 内隔离拦截。"""
        from urllib.parse import parse_qs, urlsplit
        try:
            from playwright.sync_api import sync_playwright, expect
        except ImportError:
            self.skipTest("需要 Playwright 执行真实浏览器验证")

        template = TEMPLATE.read_text(encoding="utf-8")
        content = template.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
        html = '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        html += "<style>" + STYLES.read_text(encoding="utf-8") + "</style>" + content
        script = (ROOT / "app/static/js/downloads.js").read_text(encoding="utf-8")

        def result(kind, title=None, page=1, total=None):
            row = ({"id": 41, "title": title, "status": "failed", "stages": []}
                   if kind == "issues" else
                   {"id": 51, "title": title, "source": "qb", "status": "failed"})
            count = (1 if title else 0) if total is None else total
            return {"items": [row] if title else [], "total": count, "page": page,
                    "pages": (count + 19) // 20, "through_id": 51}

        with sync_playwright() as pw:
            executable = Path(pw.chromium.executable_path)
            system_browser = next((p for name in ("google-chrome", "chromium", "chromium-browser")
                                   if (p := shutil.which(name))), None)
            if not executable.is_file() and not system_browser:
                self.skipTest("未找到 Chromium")
            browser = pw.chromium.launch(
                executable_path=str(executable) if executable.is_file() else system_browser,
                headless=True, args=["--no-sandbox"],
            )
            for width in (360, 1440):
                with self.subTest(width=width):
                    page = browser.new_page(viewport={"width": width, "height": 960})
                    errors, calls, held = [], [], []
                    state = {kind: result(kind) for kind in ("issues", "logs")}
                    batch_receipt = {}
                    batch_posts = []
                    hold = set()
                    failed = set()
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def route_request(route):
                        path = urlsplit(route.request.url).path
                        kind = path.rsplit("/", 1)[-1]
                        if path == "/downloads":
                            route.fulfill(body=html, content_type="text/html; charset=utf-8")
                        elif path == "/api/downloads/issues/batch/resubmit":
                            batch_posts.append(route.request.post_data_json)
                            route.fulfill(json=batch_receipt)
                        elif kind in state:
                            calls.append((kind, parse_qs(urlsplit(route.request.url).query)))
                            if kind in hold:
                                held.append(route)
                            elif kind in failed:
                                route.fulfill(status=503, json={"error": "隔离的读取失败"})
                            else:
                                route.fulfill(json=state[kind])
                        elif path == "/api/downloads/overview":
                            route.fulfill(json={"qb": {"tasks": []}})
                        else:
                            route.abort()

                    page.route("**/*", route_request)
                    page.clock.install()
                    page.goto("http://fixture.test/downloads?view=issues")
                    page.add_script_tag(content=script)
                    page.evaluate("() => {window.readErrors=[]; window.appAlert=value=>window.readErrors.push(value);}")
                    expect(page.locator("#issueList")).to_contain_text("当前没有")
                    for kind, tab, body, badge in (
                        ("issues", "#tabIssuesBtn", "#issueList", "#tabIssuesBadge"),
                        ("logs", "#tabLogsBtn", "#logList", "#tabLogsBadge"),
                    ):
                        # 另一入口在空结果之后产生新记录，重新激活面板必须读取。
                        state[kind] = result(kind, "重新激活后新增")
                        page.locator("#tabTasksBtn").click()
                        page.locator(tab).click()
                        expect(page.locator(body)).to_contain_text("重新激活后新增")
                        expect(page.locator(badge)).to_have_text("1")
                        state[kind] = result(kind, "手动刷新后更新")
                        page.locator("#refreshBtn").click()
                        expect(page.locator(body)).to_contain_text("手动刷新后更新")
                        page.wait_for_function("!loading")

                        # 慢响应期间保留节点、勾选与工具栏尺寸；失败也不覆盖已有行。
                        checkbox = page.locator(body + ' input[type="checkbox"]')
                        checkbox.check()
                        page.evaluate("selector=>window.previousRow=document.querySelector(selector).firstElementChild", body)
                        toolbar = page.locator("#issueBatchToolbar" if kind == "issues" else "#logBatchToolbar")
                        before = toolbar.bounding_box()
                        hold.add(kind)
                        with page.expect_request(lambda request: urlsplit(request.url).path == "/api/downloads/" + kind):
                            page.locator("#refreshBtn").click()
                        self.assertEqual(len(held), 1)
                        self.assertTrue(page.evaluate("selector=>window.previousRow===document.querySelector(selector).firstElementChild", body))
                        expect(checkbox).to_be_checked()
                        self.assertEqual(toolbar.bounding_box(), before)
                        held.pop().fulfill(json=state[kind])
                        hold.clear()
                        page.wait_for_function("!loading")
                        failed.add(kind)
                        page.evaluate("document.getElementById('lastRefresh').textContent='上次有效刷新'")
                        page.locator("#refreshBtn").click()
                        page.wait_for_function("!loading")
                        expect(page.locator(body)).to_contain_text("手动刷新后更新")
                        expect(checkbox).to_be_checked()
                        expect(page.locator("#lastRefresh")).to_have_text("上次有效刷新")
                        failed.clear()

                        # 定时轮询只刷新可见列表；刷新中不把用户请求的第 2 页拉回第 1 页。
                        state[kind] = result(kind, "第一页", total=21)
                        page.locator("#refreshBtn").click()
                        expect(page.locator(body)).to_contain_text("第一页")
                        page.wait_for_function("!loading")
                        hold.add(kind)
                        next_button = "#downloadIssueNext" if kind == "issues" else "#downloadLogNext"
                        with page.expect_request(lambda request: urlsplit(request.url).path == "/api/downloads/" + kind):
                            page.locator(next_button).click()
                        other = "logs" if kind == "issues" else "issues"
                        other_calls = sum(item[0] == other for item in calls)
                        with page.expect_request(lambda request: urlsplit(request.url).path == "/api/downloads/" + kind):
                            page.clock.fast_forward(10_001)
                        self.assertEqual([item[1]["page"] for item in calls if item[0] == kind][-2:], [["2"], ["2"]])
                        self.assertEqual(sum(item[0] == other for item in calls), other_calls)
                        self.assertEqual(len(held), 2)
                        held.pop().fulfill(json=result(kind, "最新第二页", page=2, total=21))
                        expect(page.locator(body)).to_contain_text("最新第二页")
                        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/downloads/" + kind) as stale:
                            held.pop().fulfill(json=result(kind, "过期响应", page=2, total=21))
                        stale.value.finished()
                        hold.clear()
                        page.wait_for_function("!loading")
                        expect(page.locator(body)).not_to_contain_text("过期响应")
                        page_info = "#downloadIssuePageInfo" if kind == "issues" else "#downloadLogPageInfo"
                        expect(page.locator(page_info)).to_contain_text("第 2 / 2 页")
                        if kind == "issues":
                            # 重复/失败回执保留选择，混合受理仅取消已受理项的勾选。
                            state[kind] = {**result(kind, total=4), "page": 1, "items": [
                                {"id": id, "title": f"批量请求{id}", "status": "failed", "stages": []}
                                for id in range(41, 45)
                            ]}
                            page.locator("#refreshBtn").click()
                            expect(page.locator("#issueList [data-issue-select]")).to_have_count(4)
                            page.wait_for_function("!loading")
                            page.locator("#issueSelectAll").check()
                            page.evaluate("() => {window.previousAlert=appAlert;window.batchAlerts=[];window.appAlert=value=>window.batchAlerts.push(value);window.appConfirm=async()=>true;}")
                            receipts = (
                                {"ok": False, "skipped": 4, "items": [
                                    {"source_request_id": id, "outcome": "skipped", "duplicate": True,
                                     "error": "同一资源已有下载任务正在处理"} for id in range(41, 45)
                                ]},
                                {"ok": False, "error": "凭据已过期"},
                                {"ok": False, "failed": 4, "items": [
                                    {"source_request_id": id, "outcome": "failed", "error": "明确拒绝"}
                                    for id in range(41, 45)
                                ]},
                                {"ok": False, "succeeded": 1, "partial": 1, "failed": 1, "skipped": 1,
                                 "items": [{"source_request_id": id, "outcome": outcome}
                                           for id, outcome in zip(range(41, 45), ("succeeded", "partial", "failed", "skipped"))]},
                            )
                            for index, batch_receipt in enumerate(receipts):
                                if index == 3:
                                    state[kind]["items"][2]["retry_targets"] = {
                                        "qb": {"enabled": False, "reason": "结果待核对，勿重复提交"},
                                    }
                                page.locator("#issueBulkQb").click()
                                page.wait_for_function("!issueBatchBusy")
                                expect(page.locator("#issueSelectionLabel")).to_have_text(f"已选择 {2 if index == 3 else 4} 项")
                                self.assertEqual(len(batch_posts), index + 1)
                                for id in range(41, 45):
                                    check = page.locator(f'#issueList [data-issue-select][value="{id}"]')
                                    if index == 3 and id < 43:
                                        expect(check).not_to_be_checked()
                                    else:
                                        expect(check).to_be_checked()
                            expect(page.locator('[data-issue-id="43"] [data-issue-resubmit="qb"]')).to_be_disabled()
                            self.assertEqual(page.evaluate("window.batchAlerts.map(item=>item.type)"),
                                             ["warning", "error", "error", "warning"])
                            page.evaluate("() => {window.appAlert=window.previousAlert;}")
                    read_errors = page.evaluate("window.readErrors")
                    self.assertEqual(len(read_errors), 2, read_errors)
                    # 后台标签停止轮询，恢复可见后立即核对当前日志页。
                    page.evaluate("() => {Object.defineProperty(document,'hidden',{configurable:true,value:true}); document.dispatchEvent(new Event('visibilitychange'));}")
                    before = len(calls)
                    page.clock.fast_forward(20_001)
                    self.assertEqual(len(calls), before)
                    state["logs"] = result("logs", "恢复可见后更新", page=2, total=21)
                    page.evaluate("() => {Object.defineProperty(document,'hidden',{configurable:true,value:false}); document.dispatchEvent(new Event('visibilitychange'));}")
                    expect(page.locator("#logList")).to_contain_text("恢复可见后更新")
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    self.assertEqual(errors, [])
                    page.close()
            browser.close()

    def test_history_clear_preserves_other_logs_and_local_retry_remains_accessible(self):
        """真实页面和 Chromium；API 替身只保存日志，不调用下载器。"""
        from urllib.parse import urlsplit
        from fastapi.testclient import TestClient
        try:
            from playwright.sync_api import sync_playwright, expect
        except ImportError:
            self.skipTest('需要 Playwright 执行真实浏览器验证')
        from app.config import web_credentials
        from app.main import create_app
        from tests.support import initialized_web_runtime

        with initialized_web_runtime(), TestClient(create_app()) as client, sync_playwright() as pw:
            executable = Path(pw.chromium.executable_path)
            system_browser = next((p for name in ('google-chrome', 'chromium', 'chromium-browser') if (p := shutil.which(name))), None)
            if not executable.is_file() and not system_browser:
                self.skipTest('未找到 Chromium')
            browser = pw.chromium.launch(
                executable_path=str(executable) if executable.is_file() else system_browser,
                headless=True, args=['--no-sandbox'],
            )
            login = client.get('/login')
            token = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', login.text)
            assert token
            username, password = web_credentials()
            client.post('/login', data={'csrf_token': token[1], 'username': username, 'password': password})
            html = client.get('/downloads?view=logs').text
            for width in (360, 1440):
                with self.subTest(width=width):
                    page = browser.new_page(viewport={'width': width, 'height': 960})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    rows: list[dict] = [{'id': i, 'source': 'guangya', 'title': f'剧集{i}.mp4',
                             'status': 'failed', 'path': '', 'created_at': '2026-11-09 00:00:00'}
                            for i in range(1, 54)]
                    rows.append({**rows[0], 'id': 54, 'source': 'qb', 'title': '保留的日志',
                                 'status': 'success', 'request_id': 94, 'retry_local_import': True,
                                 'postprocess_issues': [{'key': 'local_import', 'label': '本地入库',
                                                        'error': '路径映射错误 <img src=x onerror=alert(1)>'}]})
                    posted = []
                    local_retries = []

                    def route_request(route):
                        path = urlsplit(route.request.url).path
                        if path == '/downloads':
                            route.fulfill(body=html, content_type='text/html')
                        elif path.startswith('/static/'):
                            response = client.get(path)
                            route.fulfill(status=response.status_code, body=response.content,
                                          content_type=response.headers.get('content-type', 'text/plain'))
                        elif path == '/api/downloads/logs':
                            from urllib.parse import parse_qs
                            query = parse_qs(urlsplit(route.request.url).query)
                            source = query.get('source', [''])[0]
                            matched = [r for r in rows if not source or r['source'] == source]
                            number = min(int(query.get('page', ['1'])[0]), max(1, (len(matched)+19)//20))
                            route.fulfill(json={'items': matched[(number-1)*20:number*20],
                                                'total': len(matched), 'pages': (len(matched)+19)//20,
                                                'page': number, 'through_id': max((r['id'] for r in matched), default=0)})
                        elif path == '/api/downloads/logs/batch/clear':
                            payload = route.request.post_data_json
                            posted.append(payload)
                            ids = payload.get('log_ids', [])
                            selected = [r for r in rows if r['id'] in ids or (
                                payload.get('scope') == 'filtered' and r['id'] <= payload['through_id']
                                and (not payload['source'] or r['source'] == payload['source']))]
                            rows[:] = [r for r in rows if r not in selected]
                            route.fulfill(json={'ok': True, 'deleted': len(selected), 'missing': 0})
                        elif path == '/api/downloads/issues/94/retry-local-import':
                            local_retries.append(path)
                            rows[-1]['retry_local_import'] = False
                            rows[-1]['postprocess_issues'] = []
                            route.fulfill(json={'ok': True, 'request_id': 94})
                        elif path.startswith('/api/'):
                            route.fulfill(json={})
                        else:
                            route.fulfill(status=404, body='')

                    page.route('**/*', route_request)
                    page.goto('http://fixture.test/downloads?view=logs')
                    expect(page.locator('#logClearAll')).to_be_enabled()
                    page.evaluate('window.appAlert=async()=>{}')
                    page.select_option('#logSource', 'guangya')
                    expect(page.locator('#downloadLogPageInfo')).to_contain_text('共 53 条')
                    expect(page.locator('#logClearAll')).to_have_text('清空筛选结果')
                    page.locator('#downloadLogNext').click()
                    expect(page.locator('#downloadLogPageInfo')).to_contain_text('第 2')
                    page.locator('#logClearAll').scroll_into_view_if_needed()
                    before = page.locator('#logBatchToolbar').bounding_box()
                    page.locator('#logClearAll').click()
                    expect(page.locator('#appConfirmTitle')).to_have_text('清空 53 条下载日志')
                    expect(page.locator('#logClearAll')).to_be_disabled()
                    self.assertEqual(page.locator('#logBatchToolbar').bounding_box(), before)
                    page.locator('#appConfirmCancel').click()
                    expect(page.locator('#logClearAll')).to_be_enabled()
                    self.assertEqual(posted, [])
                    page.locator('#logClearAll').click()
                    rows.append({**rows[0], 'id': 55, 'title': '确认期间新日志'})
                    page.locator('#appConfirmSubmit').click()
                    expect(page.locator('#downloadLogPageInfo')).to_contain_text('共 1 条')
                    self.assertEqual(posted[0], {'scope': 'filtered', 'source': 'guangya',
                                                'status': '', 'keyword': '', 'through_id': 53})
                    self.assertEqual({r['id'] for r in rows}, {54, 55})
                    page.locator('#logSelectAll').check()
                    page.locator('#logBulkClear').click()
                    expect(page.locator('#appConfirmTitle')).to_have_text('删除 1 条下载日志')
                    page.locator('#appConfirmSubmit').click()
                    expect(page.locator('#downloadLogPageInfo')).to_have_text('共 0 条')
                    self.assertEqual(posted[1], {'log_ids': [55]})
                    expect(page.locator('#logClearAll')).to_be_disabled()
                    page.select_option('#logSource', 'qb')
                    expect(page.locator('#logList')).to_contain_text('本地入库：路径映射错误')
                    self.assertEqual(page.locator('#logList img').count(), 0)
                    retry = page.locator('#logList [data-issue-local-retry]')
                    expect(retry).to_be_enabled()
                    retry.click()
                    expect(page.locator('#appConfirmTitle')).to_have_text('重试本地整理')
                    page.locator('#appConfirmCancel').click()
                    self.assertEqual(local_retries, [])
                    retry.click()
                    page.locator('#appConfirmSubmit').click()
                    expect(page.locator('#logList [data-issue-local-retry]')).to_have_count(0)
                    self.assertEqual(local_retries, ['/api/downloads/issues/94/retry-local-import'])
                    self.assertEqual(len(posted), 2)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    self.assertEqual(errors, [])
                    page.close()
            browser.close()
