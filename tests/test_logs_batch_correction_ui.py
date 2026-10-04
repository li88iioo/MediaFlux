from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from urllib.parse import urlsplit

try:
    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional browser dependency
    Environment = None
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
BASE_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="{{ static_url('css/main.css') }}"><title>{% block title %}{% endblock %}</title></head>
<body><main><div class="content">{% block content %}{% endblock %}</div></main>
<script>
window.renderLucideIcons=()=>{};
window.appConfirm=async options=>{window.__confirmCalls.push(options);return window.__confirmAnswer;};
window.createAppModal=(modal,options={})=>{
  const close=()=>{modal.hidden=true;};
  const requestClose=reason=>options.onRequestClose?options.onRequestClose({reason,close}):close();
  modal.querySelectorAll('[data-modal-close]').forEach(button=>button.addEventListener('click',()=>requestClose('control')));
  modal.addEventListener('click',event=>{if(event.target===modal)requestClose('backdrop');});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!modal.hidden)requestClose('escape');});
  return {open(){modal.hidden=false;},close};
};
</script>{% block scripts %}{% endblock %}</body></html>"""

ROWS = [
    {
        "id": 10, "record_key": "guangya:10", "version": 4,
        "media_type": "unknown", "tmdb_id": "wrong-old-id",
        "origin": "guangya", "origin_label": "光鸭整理", "source_label": "媒体源 A",
        "original_path": "/incoming/series", "original_name": "Series.S01E02.mkv",
        "status": "manual", "actions": {"batch": True, "detail": True},
    },
    {
        "id": 11, "record_key": "guangya:11", "version": 7,
        "media_type": "movie", "tmdb_id": "",
        "origin": "guangya", "origin_label": "光鸭整理", "source_label": "媒体源 B",
        "original_path": "/incoming/series", "original_name": "Series.S02E07.mkv",
        "status": "failed", "actions": {"batch": True, "detail": True},
    },
]

CANDIDATES = [
    {"tmdb_id": "111", "external_id": "111", "provider": "tmdb", "media_type": "tv", "title": "候选 A", "year": 2023, "score": 0.91},
    {"tmdb_id": "222", "external_id": "222", "provider": "tmdb", "media_type": "tv", "title": "候选 B", "year": 2024, "score": 0.89},
]


def _preview(candidate_id: str, *, errors: bool = False) -> dict:
    digest = f"digest-{candidate_id}"
    items = []
    for row, season, episode in zip(ROWS, (1, 2), (2, 7)):
        item = {"log_id": row["id"], "original_name": row["original_name"]}
        if errors:
            item["error"] = f"与日志 #{ROWS[1 if row['id'] == 10 else 0]['id']} 的来源标题不匹配"
        else:
            item.update({
                "target_path": f"Series/Correct/Season {season}",
                "file_name": f"Correct - S{season:02d}E{episode:02d}.mkv",
                # 故意与 target_path 不同，确认 UI 使用正式字段而非 media_dir。
                "media_dir": "Series/Correct",
                "season": season,
                "episode": episode,
                "match": {"tmdb_id": candidate_id, "title": "正确作品", "year": "2024", "media_type": "tv"},
            })
        items.append(item)
    return {
        "items": items,
        "errors": ([{"log_id": row["id"], "error": items[index]["error"]} for index, row in enumerate(ROWS)] if errors else []),
        "can_execute": not errors,
        "preview_digest": "" if errors else digest,
    }


API_FIXTURE = r"""
window.__apiCalls=[];
window.__confirmCalls=[];
window.__confirmAnswer=true;
window.__pendingPreviews=[];
window.__pendingDetails=[];
window.__pendingTaskStatuses=[];
window.__testConfig=window.__testConfig||{};
const jsonResponse=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
const detailFor=id=>{
  const nsfw=Boolean(window.__testConfig.nsfw);
  return {
    id:Number(id),version:id==='10'?4:7,status:'manual',title:'旧识别标题',year:2020,
    media_type:'unknown',provider:nsfw?'metatube':'tmdb',tmdb_id:'wrong-old-id',
    original_name:'Series.S01E02.mkv',original_path:'/incoming/series',
    safety_notice:nsfw?'NSFW 专用来源 · 仅 MetaTube / 清洗标题':'' ,
    recognition:nsfw
      ? {provider:'metatube',label:'MetaTube',nsfw_only:true,query_placeholder:'输入番号'}
      : {provider:'tmdb',label:'TMDB',nsfw_only:false,query_placeholder:'输入片名或剧名'},
    release_parse:{media_type:'tv',effective_position:{season:1,episode:2}},
    allowed_actions:{search:true,preview:true,reorganize:true,return_to_source:true,revert:true,delete:true},
    items:[{file_id:'f'+id,role:'video',current_name:'Series.S01E02.mkv',original_name:'Series.S01E02.mkv',current_parent_id:'p',status:'success'}],
    operations:[],delete_audits:[],
  };
};
window.__releasePreview=id=>{
  const index=window.__pendingPreviews.findIndex(entry=>entry.id===String(id));
  if(index<0)return false;
  const [entry]=window.__pendingPreviews.splice(index,1);
  entry.resolve(jsonResponse(window.__testConfig.previewById?.[entry.id]||window.__testConfig.preview||{}));
  return true;
};
window.__releaseTaskStatus=(taskId,task)=>{
  const index=window.__pendingTaskStatuses.findIndex(entry=>entry.taskId===String(taskId));
  if(index<0)return false;
  const [entry]=window.__pendingTaskStatuses.splice(index,1);
  entry.resolve(jsonResponse({id:String(taskId),...task}));
  return true;
};
window.fetch=async(input,options={})=>{
  const url=new URL(typeof input==='string'?input:input.url,location.href);
  const method=String(options.method||'GET').toUpperCase();
  let body={};
  try{body=options.body?JSON.parse(options.body):{};}catch{}
  const taskId=url.searchParams.get('task_id');
  window.__apiCalls.push({path:url.pathname,method,body,taskId});
  const config=window.__testConfig;
  if(url.pathname==='/api/logs/overview')return jsonResponse({timeline:{success:0,failed:0,skipped:0,reverted:0}});
  if(url.pathname==='/api/logs/organize/timeline')return jsonResponse({items:config.rows||[],page:1,pages:1,total:(config.rows||[]).length});
  const detail=url.pathname.match(/^\/api\/logs\/organize\/(\d+)$/);
  if(detail&&method==='GET'){
    if((config.holdDetailIds||[]).includes(detail[1]))return new Promise(resolve=>window.__pendingDetails.push(()=>resolve(jsonResponse(detailFor(detail[1])))));
    return jsonResponse(detailFor(detail[1]));
  }
  if(url.pathname.endsWith('/recognition/search')&&method==='POST'){
    const candidates=config.nsfw
      ? [{external_id:'ABC-123',provider:'metatube',media_type:'movie',title:'成人候选',year:2024,score:0.9}]
      : (config.candidates||[]);
    return jsonResponse({candidates});
  }
  if(url.pathname==='/api/logs/organize/batch/preview'&&method==='POST'){
    const id=String(body.candidate?.tmdb_id||body.candidate?.external_id||'');
    if((config.holdPreviewIds||[]).includes(id))return new Promise(resolve=>window.__pendingPreviews.push({id,resolve}));
    const result=config.previewById?.[id]||config.preview||{};
    return jsonResponse(result);
  }
  if(url.pathname==='/api/logs/organize/batch'&&method==='POST')return jsonResponse({task_id:'batch-task'});
  if(url.pathname==='/api/guangya/organize/status'&&method==='GET'){
    if(!taskId)return jsonResponse({error:'task_id is required'},400);
    if((config.holdTaskIds||[]).includes(taskId))return new Promise(resolve=>window.__pendingTaskStatuses.push({taskId,resolve}));
    const single=taskId==='single-task';
    const status=single?(config.singleTaskStatus||config.taskStatus||'completed'):(config.taskStatus||'completed');
    const result=single?(config.singleTaskResult||{success:true,warnings:[]}):(config.taskResult||{success:true,action:'reorganize',requested:2,completed:[],failed:[],warnings:[]});
    return jsonResponse({id:taskId,status,message:single?(config.singleTaskMessage||''):(config.taskMessage||''),error:single?(config.singleTaskError||''):(config.taskError||''),result});
  }
  if(/\/reorganize\/preview$/.test(url.pathname)&&method==='POST')return jsonResponse(config.singlePreview||{target_path:'Series/Correct',media_dir:'Series',file_name:'Correct.mkv',items:[]});
  if(/\/reorganize$/.test(url.pathname)&&method==='POST'){window.__activeTask='single-task';return jsonResponse({task_id:'single-task'});}
  return jsonResponse({error:'unmatched test API fixture: '+method+' '+url.pathname},404);
};
"""


@unittest.skipIf(sync_playwright is None or Environment is None, "未安装 Playwright/Jinja2")
class LogsBatchCorrectionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        executable = next((shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser") if shutil.which(name)), None)
        options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if executable:
            options["executable_path"] = executable
        try:
            cls.browser = cls.playwright.chromium.launch(**options)
        except Exception as error:
            cls.playwright.stop()
            raise unittest.SkipTest(f"无法启动隔离 Chromium：{error}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        loader = ChoiceLoader([
            DictLoader({"base.html": BASE_TEMPLATE}),
            FileSystemLoader(str(ROOT / "app/templates")),
        ])
        environment = Environment(loader=loader, autoescape=True)
        self.html = environment.get_template("logs.html").render(static_url=lambda path: "/static/" + path)
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        preview = _preview("8801")
        default_task = {
            "success": True, "action": "reorganize", "requested": 2,
            "completed":[{"log_id":10,"result":{"file_name":"done-10.mkv"}},{"log_id":11,"result":{"file_name":"done-11.mkv"}}],
            "failed":[],"warnings":[],
        }
        config = {
            "rows": ROWS, "candidates": CANDIDATES, "preview": preview,
            "previewById": {"8801": preview, "111": _preview("111"), "222": _preview("222")},
            "taskResult": default_task,
        }
        self.page.add_init_script(script="window.__testConfig=" + json.dumps(config, ensure_ascii=False) + ";\n" + API_FIXTURE)
        self.page.route("**/*", self._serve_local_page)
        self.page.goto("http://mediaflux.test/logs", wait_until="domcontentloaded")
        self.page.wait_for_function("() => window.__apiCalls?.some(call => call.path === '/api/logs/organize/timeline')")

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])

    def _serve_local_page(self, route):
        path = urlsplit(route.request.url).path
        if path == "/logs":
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=self.html)
        elif path == "/static/js/logs.js":
            route.fulfill(status=200, content_type="text/javascript; charset=utf-8", body=(ROOT / "app/static/js/logs.js").read_text(encoding="utf-8"))
        elif path == "/static/css/main.css":
            route.fulfill(status=200, content_type="text/css; charset=utf-8", body=(ROOT / "app/static/css/main.css").read_text(encoding="utf-8"))
        else:
            route.abort()

    def select_two_and_open(self):
        self.page.locator(".organize-row-select").nth(0).check()
        self.page.locator(".organize-row-select").nth(1).check()
        self.page.locator("#organizeBatchRenameBtn").click()
        self.page.locator("#organizeDetailModal").wait_for(state="visible")
        self.page.wait_for_function("() => document.getElementById('organizeCorrectionPanel') && !document.getElementById('organizeCorrectionPanel').hidden")

    def calls(self):
        return self.page.evaluate("() => window.__apiCalls")

    def batch_previews(self):
        return [call for call in self.calls() if call["path"] == "/api/logs/organize/batch/preview"]

    def execute_calls(self):
        return [call for call in self.calls() if call["path"] == "/api/logs/organize/batch" and call["method"] == "POST"]

    def prepare_numeric_preview(self, tmdb_id="8801"):
        self.select_two_and_open()
        self.page.locator("#organizeTmdbQuery").fill(tmdb_id)
        self.page.locator("#organizeTmdbSearchBtn").click()
        self.page.locator("#organizeBatchExecuteBtn:not([disabled])").wait_for()

    def prepare_single_preview(self):
        self.page.locator(".detail-btn").first.click()
        self.page.locator("#organizeDetailModal").wait_for(state="visible")
        self.page.locator("#organizeTmdbQuery").fill("正确作品")
        self.page.locator("#organizeTmdbSearchBtn").click()
        self.page.locator(".organize-candidate").first.click()
        self.page.locator("#organizeNamingPreview:not([hidden])").wait_for()

    def test_unknown_type_uses_release_parse_tv_and_submits_frozen_flat_preview(self):
        self.prepare_numeric_preview()
        self.assertEqual(self.page.locator("#organizeTmdbType").input_value(), "tv")
        self.assertTrue(self.page.locator("#organizeEpisodeOverrideField").evaluate("el => el.hidden"))
        self.assertEqual(self.page.locator("#organizeSeasonOverride").input_value(), "")
        self.assertIn("来源 · TMDB", self.page.locator("#organizeBatchSourceScope").inner_text())
        self.assertIn("每条日志仍由服务端", self.page.locator("#organizeBatchSourceScope").get_attribute("title"))
        self.assertIn("Series/Correct/Season 1/Correct - S01E02.mkv", self.page.locator('[data-log-id="10"]').inner_text())
        self.assertIn("各自季集：S01E02", self.page.locator('[data-log-id="10"]').inner_text())
        preview = self.batch_previews()[0]["body"]
        self.assertEqual(preview["action"], "reorganize")
        self.assertEqual([entry["log_id"] for entry in preview["entries"]], [10, 11])
        self.assertEqual([entry["expected_version"] for entry in preview["entries"]], [4, 7])
        self.assertTrue(all(entry["operation_token"] for entry in preview["entries"]))
        self.assertEqual(preview["candidate"]["tmdb_id"], "8801")
        self.assertNotIn("episode", preview["candidate"])
        self.assertNotIn("season", preview["candidate"])
        self.page.locator("#organizeBatchExecuteBtn").click()
        self.page.wait_for_function("() => window.__apiCalls.some(call => call.path === '/api/guangya/organize/status') && document.getElementById('organizeOperationState').textContent.includes('逐条结果已返回')")
        execute = self.execute_calls()[0]["body"]
        self.assertEqual(execute["entries"], preview["entries"])
        self.assertEqual(execute["candidate"], preview["candidate"])
        self.assertEqual(execute["preview_digest"], "digest-8801")
        self.assertIn("已完成", self.page.locator('[data-log-id="10"]').inner_text())
        self.assertNotIn("/reorganize/preview", [call["path"] for call in self.calls()])

    def test_mixed_source_titles_return_per_row_errors_and_disable_execute(self):
        self.page.evaluate("preview => window.__testConfig.previewById['111']=preview", _preview("111", errors=True))
        self.select_two_and_open()
        self.page.locator("#organizeTmdbQuery").fill("正确作品")
        self.page.locator("#organizeTmdbSearchBtn").click()
        self.page.locator(".organize-candidate").first.click()
        self.page.locator("#organizeBatchPreview .organize-batch-preview-error").first.wait_for()
        self.assertEqual(self.page.locator("#organizeBatchPreview .organize-batch-preview-error").count(), 2)
        self.assertTrue(self.page.locator("#organizeBatchExecuteBtn").is_disabled())
        self.assertTrue(all("与日志 #" in text for text in self.page.locator(".organize-batch-preview-error").all_text_contents()))
        self.assertEqual(self.execute_calls(), [])
        preview = self.batch_previews()[0]["body"]
        self.assertEqual(preview["candidate"]["tmdb_id"], "111")
        self.assertNotIn("wrong-old-id", json.dumps(preview, ensure_ascii=False))

    def test_candidate_switch_ignores_late_response_and_executes_only_latest_digest(self):
        self.page.evaluate("() => window.__testConfig.holdPreviewIds=['111']")
        self.select_two_and_open()
        self.page.locator("#organizeTmdbQuery").fill("剧名")
        self.page.locator("#organizeTmdbSearchBtn").click()
        self.page.locator(".organize-candidate").nth(0).click()
        self.page.wait_for_function("() => window.__pendingPreviews.length === 1")
        self.page.locator(".organize-candidate").nth(1).click()
        self.page.locator("#organizeBatchExecuteBtn:not([disabled])").wait_for()
        self.assertIn("候选 B", self.page.locator('.organize-candidate.selected').inner_text())
        self.assertIn("Series/Correct/Season 1/Correct - S01E02.mkv", self.page.locator('[data-log-id="10"]').inner_text())
        self.page.evaluate("() => window.__releasePreview('111')")
        self.page.wait_for_timeout(50)
        self.assertTrue(self.page.locator("#organizeBatchExecuteBtn").is_enabled())
        self.page.locator("#organizeBatchExecuteBtn").click()
        self.page.wait_for_function("() => window.__apiCalls.some(call => call.path === '/api/guangya/organize/status')")
        execute = self.execute_calls()[0]["body"]
        self.assertEqual(execute["candidate"]["tmdb_id"], "222")
        self.assertEqual(execute["preview_digest"], "digest-222")
        self.assertEqual([call["body"]["candidate"]["tmdb_id"] for call in self.batch_previews()], ["111", "222"])

    def test_cancel_discards_late_preview_without_execute_or_download(self):
        self.page.evaluate("() => window.__testConfig.holdPreviewIds=['8801']")
        self.select_two_and_open()
        self.page.locator("#organizeTmdbQuery").fill("8801")
        self.page.locator("#organizeTmdbSearchBtn").click()
        self.page.wait_for_function("() => window.__pendingPreviews.length === 1")
        self.page.locator("#organizeBatchCancelBtn").click()
        self.page.locator("#organizeDetailModal").wait_for(state="hidden")
        self.page.evaluate("() => window.__releasePreview('8801')")
        self.page.wait_for_timeout(50)
        self.assertTrue(self.page.locator("#organizeBatchPreview").evaluate("el => el.hidden"))
        calls = self.calls()
        self.assertEqual(self.execute_calls(), [])
        self.assertFalse(any("download" in call["path"].lower() for call in calls))
        self.assertFalse(any(call["path"].endswith("/reorganize") or call["path"].endswith("/reorganize/preview") for call in calls))
        # 唯一 POST 是明确无副作用的整批预览，没有云盘写入/执行请求。
        self.assertEqual([call["path"] for call in calls if call["method"] == "POST"], ["/api/logs/organize/batch/preview"])

    def test_partial_task_result_uses_completed_and_failed_contract(self):
        self.page.evaluate("() => window.__testConfig.taskResult={success:false,action:'reorganize',requested:2,completed:[{log_id:10,result:{file_name:'done.mkv'}}],failed:[{log_id:11,error:'目标已存在'}],warnings:[]}")
        self.prepare_numeric_preview()
        self.page.evaluate("() => window.__testConfig.taskStatus='partial'")
        self.page.locator("#organizeBatchExecuteBtn").click()
        self.page.wait_for_function("() => document.getElementById('organizeOperationState').textContent.includes('有 1 条失败')")
        self.assertIn("已完成", self.page.locator('[data-log-id="10"]').inner_text())
        failed = self.page.locator('[data-log-id="11"]').inner_text()
        self.assertIn("执行失败", failed)
        self.assertIn("目标已存在", failed)
        self.assertTrue(self.page.locator("#organizeBatchExecuteBtn").is_disabled())
        self.assertEqual(self.execute_calls()[0]["body"]["preview_digest"], "digest-8801")
        status_calls = [call for call in self.calls() if call["path"] == "/api/guangya/organize/status"]
        self.assertEqual([call["taskId"] for call in status_calls], ["batch-task"])

    def test_all_failed_batch_keeps_each_row_error(self):
        self.page.evaluate("""() => {
          window.__testConfig.taskStatus='failed';
          window.__testConfig.taskResult={success:false,action:'reorganize',requested:2,completed:[],failed:[
            {log_id:10,error:'第十条冲突'}, {log_id:11,error:'第十一条权限不足'}
          ],warnings:[]};
        }""")
        self.prepare_numeric_preview()
        self.page.locator("#organizeBatchExecuteBtn").click()
        self.page.wait_for_function("() => document.getElementById('organizeOperationState').textContent.includes('有 2 条失败')")
        self.assertIn("第十条冲突", self.page.locator('[data-log-id="10"]').inner_text())
        self.assertIn("第十一条权限不足", self.page.locator('[data-log-id="11"]').inner_text())
        status_calls = [call for call in self.calls() if call["path"] == "/api/guangya/organize/status"]
        self.assertEqual([call["taskId"] for call in status_calls], ["batch-task"])

    def test_single_completed_task_with_failed_receipt_is_not_success(self):
        self.page.evaluate("() => window.__testConfig.singleTaskResult={success:false,error:'单条写入失败'}")
        self.prepare_single_preview()
        self.page.locator("#organizeReorganizeBtn").click()
        self.page.wait_for_function("() => document.getElementById('organizeOperationState').textContent.includes('单条写入失败')")
        self.assertIn("is-error", self.page.locator("#organizeOperationState").get_attribute("class"))
        self.assertNotIn("云端操作已完成", self.page.locator("#organizeOperationState").inner_text())
        status_calls = [call for call in self.calls() if call["path"] == "/api/guangya/organize/status"]
        self.assertEqual([call["taskId"] for call in status_calls], ["single-task"])

    def test_stopped_cancelled_and_manual_review_are_terminal_errors(self):
        self.prepare_single_preview()
        cases = [
            ("stopped", "后台操作已停止"),
            ("cancelled", "后台操作已取消"),
            ("manual_review", "后台操作需要人工复核"),
        ]
        for status, expected in cases:
            self.page.evaluate("([status,message]) => { window.__testConfig.singleTaskStatus=status; window.__testConfig.singleTaskMessage=message; }", [status, expected])
            before = len([call for call in self.calls() if call["path"] == "/api/guangya/organize/status" and call["taskId"] == "single-task"])
            self.page.locator("#organizeReorganizeBtn").click()
            self.page.wait_for_function("before => window.__apiCalls.filter(call => call.path === '/api/guangya/organize/status' && call.taskId === 'single-task').length > before", arg=before)
            self.page.wait_for_function("expected => document.getElementById('organizeOperationState').textContent.includes(expected)", arg=expected)
            count = len([call for call in self.calls() if call["path"] == "/api/guangya/organize/status" and call["taskId"] == "single-task"])
            self.page.wait_for_timeout(1100)
            self.assertEqual(len([call for call in self.calls() if call["path"] == "/api/guangya/organize/status" and call["taskId"] == "single-task"]), count)
            self.assertIn("is-error", self.page.locator("#organizeOperationState").get_attribute("class"))

    def test_late_batch_task_error_does_not_touch_new_detail(self):
        self.page.evaluate("() => window.__testConfig.holdTaskIds=['batch-task']")
        self.prepare_numeric_preview()
        self.page.locator("#organizeBatchExecuteBtn").click()
        self.page.wait_for_function("() => window.__pendingTaskStatuses.length===1")
        self.page.keyboard.press("Escape")
        self.page.locator(".detail-btn").nth(1).click()
        self.page.wait_for_function("() => window.__apiCalls.some(call => call.path === '/api/logs/organize/11')")
        self.page.wait_for_function("() => document.getElementById('organizeTmdbQuery').value==='旧识别标题'")
        self.assertEqual(self.page.locator("#organizeOperationState").inner_text(), "")
        self.page.evaluate("() => window.__releaseTaskStatus('batch-task',{status:'failed',error:'旧弹窗任务失败',result:{success:false,failed:[{log_id:10,error:'旧弹窗任务失败'}]}})")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.locator("#organizeOperationState").inner_text(), "")
        self.assertNotIn("is-error", self.page.locator("#organizeOperationState").get_attribute("class"))

    def test_nsfw_source_is_visible_and_numeric_search_does_not_become_tmdb_id(self):
        self.page.evaluate("() => window.__testConfig.nsfw=true")
        self.select_two_and_open()
        self.assertFalse(self.page.locator("#organizeSafetyNotice").evaluate("el => el.hidden"))
        self.assertIn("NSFW", self.page.locator("#organizeSafetyNotice").inner_text())
        self.assertEqual(self.page.locator("#organizeBatchSourceScope").inner_text(), "NSFW · MetaTube")
        self.page.locator("#organizeTmdbQuery").fill("12345")
        self.page.locator("#organizeTmdbSearchBtn").click()
        self.page.locator(".organize-candidate").click()
        preview = self.batch_previews()[0]["body"]
        self.assertEqual(preview["candidate"]["provider"], "metatube")
        self.assertEqual(preview["candidate"]["external_id"], "ABC-123")
        self.assertNotIn("tmdb_id", preview["candidate"])
        search = next(call for call in self.calls() if call["path"].endswith("/recognition/search"))
        self.assertEqual(search["body"]["query"], "12345")

    def test_explicit_id_is_not_zero_confidence_and_post_action_warnings_remain_visible(self):
        self.prepare_numeric_preview()
        self.assertIn("指定 ID", self.page.locator('.organize-candidate.selected').inner_text())
        self.assertNotIn("0%", self.page.locator('.organize-candidate.selected').inner_text())
        self.page.evaluate("() => window.__testConfig.taskResult.warnings=['STRM 同步暂未启动']")
        self.page.locator("#organizeBatchExecuteBtn").click()
        self.page.wait_for_function("() => document.querySelector('#organizeOperationState').textContent.includes('联动警告')")
        self.assertIn("STRM 同步暂未启动", self.page.locator('#organizeBatchPreview').inner_text())
        self.assertEqual(self.page.locator('.organize-batch-preview-result.is-success').count(), 2)
        self.assertEqual(self.page.locator('#organizeBatchCancelBtn').inner_text(), '关闭')

    def test_late_single_detail_cannot_replace_a_new_batch_dialog(self):
        self.page.evaluate("() => window.__testConfig.holdDetailIds=['10']")
        self.page.locator('.detail-btn').first.click()
        self.page.wait_for_function("() => window.__pendingDetails.length===1")
        self.page.keyboard.press('Escape')
        self.page.evaluate("() => window.__testConfig.holdDetailIds=[]")
        self.select_two_and_open()
        self.page.evaluate("() => window.__pendingDetails.shift()()")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.locator('#organizeDetailTitle').inner_text(), '批量纠正识别')
        self.assertFalse(self.page.locator('#organizeBatchActions').evaluate('e=>e.hidden'))
        self.assertTrue(self.page.locator('#organizeEpisodeOverrideField').evaluate('e=>e.hidden'))
        self.assertEqual(self.execute_calls(), [])

    def test_single_correction_keeps_single_preview_and_action_routes(self):
        self.prepare_single_preview()
        single_preview = [call for call in self.calls() if call["path"].endswith("/reorganize/preview")]
        self.assertEqual(len(single_preview), 1)
        self.page.locator("#organizeReorganizeBtn").click()
        self.page.wait_for_function("() => window.__apiCalls.some(call => call.path === '/api/logs/organize/10/reorganize' && call.method === 'POST')")
        action = next(call for call in self.calls() if call["path"] == "/api/logs/organize/10/reorganize")
        self.assertIn("expected_version", action["body"])
        self.assertFalse(self.batch_previews())


if __name__ == "__main__":
    unittest.main()
