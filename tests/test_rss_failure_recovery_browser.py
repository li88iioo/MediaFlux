"""真实 Chromium 执行 RSS 页面函数；静态资源 / API 都在本地内存，无外网。"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]


def extract(source, start, end):
    offset = source.index(start)
    return source[offset:source.index(end, offset)]


@unittest.skipIf(sync_playwright is None, "未安装 Playwright")
class RSSFailureRecoveryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert sync_playwright is not None
        cls.playwright = sync_playwright().start()
        bundled = Path(cls.playwright.chromium.executable_path)
        executable = str(bundled) if bundled.is_file() else next((p for name in ("google-chrome", "chromium", "chromium-browser") if (p := shutil.which(name))), None)
        if not executable:
            cls.playwright.stop()
            raise unittest.SkipTest("未找到 Chromium")
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True, args=["--no-sandbox"])
        source = (ROOT / "app/templates/rss.html").read_text()
        cls.script = "\n".join((
            "let entries=[],rssEntriesRequestId=0,rssSelectionRevision=0,rssEntriesController=null,rssEntriesLoaded=false;const selectedEntries=new Set();",
            "let rssEntryPage={offset:0,limit:100,total:0,has_more:false,filters:''};",
            "window.pending=[];window.calls=[];window.alerts=[];const api=(path,opts)=>{calls.push({path,opts});return new Promise(resolve=>pending.push(resolve));};",
            "const updateBatchbar=()=>{};const updateStats=()=>{};const toggleEntry=()=>{};const appAlert=async message=>alerts.push(message);",
            extract(source, "function lockRssListHeight", "\nfunction animateRssStats"),
            extract(source, "function escapeHtml", "\nfunction renderRssTarget"),
            extract(source, "async function rssApiJSON", "\nfunction setRssButtonBusy"),
            extract(source, "function setRssButtonBusy", "\nasync function updateStats"),
            extract(source, "function updateEntryPagination", "\nasync function batchRequest"),
            extract(source, "async function dlEntry", "\ndocument.getElementById('f_method')"),
            "window.finish=data=>pending.shift()({ok:true,json:async()=>data});",
            "window.finishPage=(items,offset=0,total=items.length)=>finish({items,offset,limit:100,total,has_more:offset+items.length<total});",
            "window.rows=[{id:1,title:'Episode S01E01',sub_name:'RSS',status:'downloaded',processed:true,delivery:{request_id:7,failed:true,retryable:true,error:'源已确定失败 <script>window.xss=true</script>'}},",
            "{id:2,title:'Episode S01E02',status:'downloaded',processed:true,delivery:{request_id:8,review_required:true,error:'结果未知'}},",
            "{id:3,title:'Episode S01E03',status:'skipped',processed:true,delivery:{request_id:9,failed:true,retryable:false}}];",
        ))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def page(self, width):
        context = self.browser.new_context(viewport={"width": width, "height": 960})
        self.addCleanup(context.close)
        context.route("**/*", lambda route: route.abort())
        if hasattr(context, "route_web_socket"):
            context.route_web_socket("**/*", lambda ws: ws.close())
        page = context.new_page()
        page.set_content('''<main style="padding:12px"><select id="filterSub"></select><select id="filterStatus"></select><input id="filterKeyword"><button id="refreshRssEntries" class="rss-btn">刷新</button><div class="rss-entry-list" id="entryList"></div></main>''')
        source = (ROOT / "app/templates/rss.html").read_text()
        page.locator('main').evaluate('(el,html)=>el.insertAdjacentHTML("beforeend",html)', extract(source, '<nav class="table-pagination" aria-label="RSS 条目分页">', '\n        </div>'))
        page.add_style_tag(content=(ROOT / "app/static/css/main.css").read_text())
        page.add_script_tag(content=(ROOT / "app/static/js/lucide.min.js").read_text())
        page.add_script_tag(content="window.renderLucideIcons=()=>lucide.createIcons();")
        page.add_script_tag(content=self.script)
        page.evaluate("async()=>{const done=loadEntries();finishPage(rows);await done;}")
        return page

    @staticmethod
    def boxes(page):
        return page.evaluate("""() => ['#entryList', '.rss-entry-card', '.rss-entry-actions', '.rss-entry-actions button'].map(selector=>{const el=document.querySelector(selector);if(!el)return [0,0,0,0];const r=el.getBoundingClientRect();return [r.x,r.y,r.width,r.height]})""")

    def assert_stable(self, actual, expected):
        for a, e in zip(actual, expected):
            for x, y in zip(a, e):
                self.assertAlmostEqual(x, y, delta=1)

    def test_pagination_filters_selection_and_late_response(self):
        for width in (360, 1440):
            with self.subTest(width=width):
                page = self.page(width)
                page.evaluate("async()=>{const done=loadEntries();finishPage(rows,0,201);await done;selectedEntries.add(1);}")
                page.locator('#rssEntryNext').click()
                self.assertIn('offset=100', page.evaluate('calls.at(-1).path'))
                self.assertTrue(page.locator('#rssEntryNext').is_disabled())
                self.assertEqual(page.locator('.rss-entry-card').count(), 3)
                page.evaluate("finishPage(rows.map(r=>({...r,id:r.id+100})),100,201)")
                page.wait_for_function('rssEntryPage.offset===100')
                self.assertEqual(page.evaluate('selectedEntries.size'), 0)
                self.assertEqual(page.locator('#rssEntryPageInfo').inner_text(), '显示 101–103 / 共 201 条')
                self.assertFalse(page.locator('#rssEntryPrev').is_disabled())
                # 翻页尚在途时改变筛选，旧请求晚返回不能覆盖新筛选结果。
                page.locator('#rssEntryNext').click()
                page.fill('#filterKeyword', 'another')
                page.evaluate('window.filterLoad=loadEntries();void 0;')
                self.assertIn('offset=0', page.evaluate('calls.at(-1).path'))
                self.assertIn('q=another', page.evaluate('calls.at(-1).path'))
                page.evaluate('finishPage(rows,200,201)')
                page.evaluate('async()=>{finishPage([],0,0);await filterLoad;}')
                self.assertEqual(page.locator('#rssEntryPageInfo').inner_text(), '显示 0–0 / 共 0 条')
                self.assertTrue(page.locator('#rssEntryPrev').is_disabled())
                self.assertTrue(page.locator('#rssEntryNext').is_disabled())
                self.assertEqual(page.locator('.rss-entry-card').count(), 0)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                page.close()

    def test_failure_retry_unknown_protection_and_refresh_busy_geometry(self):
        for width in (360, 768, 1440):
            with self.subTest(width=width):
                page = self.page(width)
                errors = []
                page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                cards = page.locator(".rss-entry-card")
                text_content = cards.nth(0).locator(".rss-status-badge").get_attribute("title")
                assert text_content is not None
                self.assertIn("下载失败 · 请求 #7", text_content)
                self.assertEqual(cards.nth(0).locator("button").inner_text(), "重试")
                self.assertEqual(cards.nth(1).locator("button").count(), 0)
                self.assertEqual(cards.nth(1).locator("a").get_attribute("href"), "/downloads")
                self.assertEqual(cards.nth(2).locator("button,a").count(), 0)
                self.assertFalse(page.evaluate("Boolean(window.xss)"))
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                before = self.boxes(page)
                for _ in range(3):
                    page.evaluate("window.refresh=loadEntries();void 0;")
                    page.wait_for_timeout(350)
                    self.assertEqual(cards.count(), 3)
                    self.assertEqual(page.locator(".subscription-skeleton").count(), 0)
                    self.assert_stable(self.boxes(page), before)
                    page.evaluate("async()=>{finishPage(rows);await refresh;}")
                    page.wait_for_timeout(50)
                    self.assert_stable(self.boxes(page), before)
                page.evaluate("window.submit=dlEntry(1,document.querySelector('.rss-entry-actions button'));void 0;")
                self.assertTrue(cards.nth(0).locator("button").is_disabled())
                self.assert_stable(self.boxes(page), before)
                self.assertEqual(page.evaluate("calls.at(-1).path"), "/api/rss/entries/1/download")
                self.assertEqual(page.evaluate("calls.at(-1).opts.method"), "POST")
                page.evaluate("""async()=>{
                    pending.shift()({ok:false,status:400,json:async()=>({ok:false,
                        error:'种子抓取连接失败，请等待 8 秒后重试（00:13:35）',
                        retry_reason:'cooldown',retry_after:8,retry_at:'2026-11-09 00:13:35'})});
                    await submit;
                }""")
                self.assertEqual(page.evaluate("alerts.at(-1).type"), "warning")
                self.assertEqual(page.evaluate("alerts.at(-1).title"), "稍后重试")
                self.assertIn("8 秒", page.evaluate("alerts.at(-1).message"))
                self.assertFalse(cards.nth(0).locator("button").is_disabled())
                self.assert_stable(self.boxes(page), before)
                page.evaluate("window.submit=dlEntry(1,document.querySelector('.rss-entry-actions button'));void 0;")
                page.evaluate("finish({result:{ok:true,existing:false,request_id:10}})")
                page.wait_for_function("pending.length===1")
                page.evaluate("async()=>{finishPage(rows);await submit;}")
                page.wait_for_timeout(50)
                self.assert_stable(self.boxes(page), before)
                self.assertEqual(page.evaluate("alerts.at(-1).title"), "下载已提交")
                # 请求恢复成功后从重试按钮变成受理回执，动作槽位也不能塌缩。
                page.evaluate("async()=>{rows[0].delivery={request_id:10,status:'submitted',failed:false,retryable:false};const done=loadEntries();finishPage(rows);await done;}")
                page.wait_for_timeout(50)
                self.assertEqual(cards.nth(0).locator("button").count(), 0)
                self.assert_stable(self.boxes(page)[:3], before[:3])
                self.assertEqual(errors, [])
                page.close()
