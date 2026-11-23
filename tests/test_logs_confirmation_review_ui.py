"""真实 Chromium/DOM 验证光鸭批量审阅；复用日志浏览器环境，接口完全隔离。"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from tests import test_logs_batch_correction_ui as support


def groups(count=25) -> list[dict]:
    return [{
        "confirmation_id": f"group-{number:03d}", "batch_id": "source-batch", "task_id": "task-a",
        "source": "光鸭来源 A", "directory": f"/incoming/group-{number}",
        "identity": f"媒体组 {number:03d}", "reason": "识别歧义", "status": "pending",
        "expires_at": "2099-12-01T00:00:00Z", "review_status": "waiting", "can_discover_candidates": False,
        "suggestion": ({"candidate_index": 1, "summary": "年份及文件证据一致", "confidence": .91,
                        "reason_code": "file_evidence", "source": "agent"} if number == 1 else None),
        "selected_index": None, "error": "", "review_summary": "", "downstream": {},
        "files": [{"name": f"Group{number}.S01E02.mkv", "season": 1, "episode": 2}],
        "candidates": [{"index": index, "title": f"作品{number}候选{'A' if index == 0 else 'B'}",
                        "year": 2023 + index, "provider": "tmdb", "external_id": str(number * 10 + index),
                        "tmdb_id": str(number * 10 + index), "media_type": "tv",
                        "evidence": {"release_date": "2024-04-08", "actors": ["演员甲", "演员乙"],
                                     "maker": "制作商甲", "series": "系列甲"}}
                       for index in range(2)],
    } for number in range(1, count + 1)]


def without_candidates(group, *log_ids):
    return {
        **group, "candidates": [], "suggestion": None, "log_ids": list(log_ids),
        "files": [{**group["files"][0], "file_id": f"f{log_id}"} for log_id in log_ids],
    }


CONFIRMATION_API = r"""
(() => {
    const fallback=window.fetch;
    const endpoint='/api/logs/organize/confirmations';
    const reply=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
    window.__confirmationCalls=[];
    window.__heldConfirmationLists=[];
    window.__heldConfirmationReviews=[];
    window.__heldConfirmationPreviews=[];
    window.__heldConfirmationSubmits=[];
    window.fetch=async(input,options={})=>{
        const url=new URL(typeof input==='string'?input:input.url,location.href);
        if(!url.pathname.startsWith(endpoint))return fallback(input,options);
        const method=String(options.method||'GET').toUpperCase();
        const body=options.body?JSON.parse(options.body):{};
        const config=window.__confirmationConfig;
        const suffix=url.pathname.slice(endpoint.length);
        window.__confirmationCalls.push({suffix,method,body,url:String(url),params:Object.fromEntries(url.searchParams)});
        if((!suffix&&method==='GET')||suffix==='/query'){
            const params=suffix==='/query'?body:Object.fromEntries(url.searchParams);
            if(config.failQuery&&suffix==='/query')return reply({error:'冻结状态读取失败'},503);
            const matched=config.items.filter(item=>{
                if(params.confirmation_ids&&!params.confirmation_ids.includes(item.confirmation_id))return false;
                if(params.status!=='all'&&item.status!==(params.status||'pending'))return false;
                return ['task_id','source'].every(key=>!params[key]||item[key]===params[key])&&
                    (!params.reason||item.reason.includes(params.reason))&&
                    (!params.q||(item.identity+' '+item.directory).includes(params.q));
            });
            const truncated=matched.length>1000, rows=matched.slice(0,1000);
            const page=Number(params.page)||1, size=Math.min(100,Number(params.page_size)||20);
            const items=matched.slice((page-1)*size,page*size).map(item=>{
                if(params.include_correction_logs===true||item.status!=='pending')return item;
                const summary={...item};delete summary.log_ids;return summary;
            });
            const data={items,total:matched.length,pages:Math.ceil(matched.length/size),page,
                confirmation_ids:rows.map(item=>item.confirmation_id),counts:matched.reduce((counts,item)=>{counts[item.status]=(counts[item.status]||0)+1;return counts;},{}),
                selection_truncated:truncated,review_enabled:config.reviewEnabled!==false};
            const response=config.failList&&!suffix?reply({error:'列表暂时不可用'},503):reply(data);
            if(config.holdList&&!suffix)return new Promise(resolve=>window.__heldConfirmationLists.push(()=>resolve(response)));
            return response;
        }
        if(suffix==='/review'){
            body.confirmation_ids.forEach(token=>{const item=config.items.find(item=>item.confirmation_id===token);item.review_status='pending';});
            const response=reply({requested:body.confirmation_ids.length,unchanged:0,unavailable:0});
            if(config.holdReview)return new Promise(resolve=>window.__heldConfirmationReviews.push(()=>resolve(response)));
            return response;
        }
        if(suffix==='/skip'){
            const items=body.confirmation_ids.map(token=>{
                const item=config.items.find(item=>item.confirmation_id===token);
                if(!item||item.status!=='pending'||config.skipFailTokens?.includes(token))return {confirmation_id:token,skipped:false,error:'确认操作已过期，请重新执行整理'};
                item.status='cancelled';
                return {confirmation_id:token,skipped:true};
            });
            const skipped=items.filter(item=>item.skipped).length;
            return reply({items,skipped,failed:items.length-skipped});
        }
        if(suffix==='/selection'){
            if(config.selectionError)return reply({error:'人工选择保存失败'},422);
            const item=config.items.find(item=>item.confirmation_id===body.confirmation_id);
            item.suggestion={candidate_index:body.candidate_index,source:'human',summary:'人工核对证据后选择'};
            item.review_status='approved';
            return reply({success:true});
        }
        if(suffix==='/preview'){
            const entries=body.entries.map(entry=>config.reverseEntries?{candidate_index:entry.candidate_index,confirmation_id:entry.confirmation_id}:entry);
            if(config.reverseEntries)entries.reverse();
            const data={entries,digest:'server-digest-'+body.entries.map(entry=>entry.confirmation_id+':'+entry.candidate_index).join('|'),
                items:body.entries.map(entry=>{
                    const item=config.items.find(item=>item.confirmation_id===entry.confirmation_id);
                    return {...item,candidate:item.candidates.find(candidate=>candidate.index===entry.candidate_index),
                        episode_mappings:config.episodeMappings?.[item.confirmation_id]||[],target:'TV/媒体目录 '+item.confirmation_id};
                }),note:'仅预览媒体目录；分类层级及文件名由现有整理规则生成，执行前仍会核验源文件。'};
            if(config.previewError)return reply({error:config.previewError},422);
            const response=reply(data);
            if(config.holdPreview)return new Promise(resolve=>window.__heldConfirmationPreviews.push(()=>resolve(response)));
            return response;
        }
        if(suffix==='/submit'){
            if(config.submitError)return reply({error:'连接中断'},503);
            const items=body.entries.map(entry=>{
                const item=config.items.find(item=>item.confirmation_id===entry.confirmation_id);
                item.status=config.rejectedTokens?.includes(entry.confirmation_id)?'failed':'queued';
                item.selected_index=entry.candidate_index;item.error=item.status==='failed'?'源文件已变更':'';
                return {confirmation_id:item.confirmation_id,status:item.status,error:item.error};
            });
            const failed=items.filter(item=>item.status==='failed').length;
            const response=reply({items,accepted:items.length-failed,failed});
            if(config.holdSubmit)return new Promise(resolve=>window.__heldConfirmationSubmits.push(()=>resolve(response)));
            return response;
        }
        return reply({error:'未知确认接口'},404);
    };
})();
"""


class LogsConfirmationReviewBrowserTests(support.LogsBrowserCase):
    def setUp(self):
        self.config = {"items": groups(), "reviewEnabled": True}
        super().setUp()

    def _serve_local_page(self, route):
        path = urlsplit(route.request.url).path
        if path == "/logs":
            config = json.dumps(self.config, ensure_ascii=False).replace("<", "\\u003c")
            additions = '<link rel="stylesheet" href="/static/css/pages/logs.css">'
            additions += '<script>window.__confirmationConfig=' + config + ';' + CONFIRMATION_API + '</script>'
            html = self.html.replace('</head>', additions + '</head>')
            # 使用真实批量审阅、纠正弹窗和二次确认。
            base = (support.ROOT / 'app/templates/base.html').read_text(encoding='utf-8')
            confirm = base.index('<dialog class="app-modal app-confirm-modal"')
            confirm_end = base.index('</dialog>', confirm) + len('</dialog>')
            html = html.replace('<script src="/static/js/logs.js">',
                                base[confirm:confirm_end] + '<script src="/static/js/app.js"></script><script src="/static/js/logs.js">')
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        elif path in {"/static/css/pages/logs.css", "/static/js/app.js"}:
            route.fulfill(status=200, content_type="text/css" if path.endswith('.css') else "text/javascript",
                          body=(support.ROOT / path.lstrip('/').replace('static/', 'app/static/', 1)).read_text(encoding='utf-8'))
        else:
            super()._serve_local_page(route)

    def idle(self):
        self.page.wait_for_function("() => !document.getElementById('confirmationReviewRefresh').disabled")

    def open(self, items=None):
        if items is not None:
            self.page.evaluate("items=>window.__confirmationConfig.items=items", items)
        self.page.locator('#organizeConfirmationReviewBtn').click()
        self.page.locator('#confirmationReviewModal').wait_for(state='visible')
        self.idle()

    def calls(self, suffix=None):
        calls = self.page.evaluate('window.__confirmationCalls')
        return calls if suffix is None else [call for call in calls if call['suffix'] == suffix]

    def organize_calls(self, suffix):
        return self.page.evaluate('path=>window.__apiCalls.filter(call=>call.path===path)', '/api/logs/organize' + suffix)

    def select_all(self):
        self.page.locator('#confirmationReviewSelectMatching').click()

    def refresh(self):
        self.page.locator('#confirmationReviewRefresh').click()
        self.idle()

    def test_one_zero_candidate_group_uses_all_logs_and_syncs_completed_group_on_return(self):
        first, other = groups(2)
        self.open([without_candidates(first, 10, 11), other])
        trigger = self.page.locator('#confirmationReviewCorrect')
        self.assertTrue(trigger.is_hidden())
        detail = self.page.locator('#confirmationReviewDetail')
        self.assertIn('点击「纠正识别」重新查找作品', detail.inner_text())
        self.assertEqual(detail.locator('.confirmation-review-suggestion').count(), 0)
        self.page.get_by_role('checkbox', name='选择待确认媒体组 媒体组 001', exact=True).check()
        self.assertTrue(self.page.locator('#confirmationReviewAgent').is_disabled())
        trigger.click()
        modal = self.page.get_by_role('dialog', name='批量纠正识别')
        modal.wait_for()
        self.assertIs(self.calls('/query')[-1]['body']['include_correction_logs'], True)
        for log_id in (10, 11):
            detail_call, = self.organize_calls(f'/{log_id}')
            self.assertEqual(detail_call['confirmationId'], 'group-001')
        self.assertTrue(self.page.locator('#confirmationReviewModal').evaluate('node => node.inert'))
        self.assertIn('2 条日志', modal.locator('#organizeDetailSubtitle').inner_text())
        query = modal.locator('#organizeTmdbQuery')
        query.fill('实际作品')
        query.press('Enter')
        modal.get_by_role('button', name='候选 A', exact=False).click()
        execute = modal.locator('#organizeBatchExecuteBtn')
        self.page.wait_for_function("!document.querySelector('#organizeBatchExecuteBtn').disabled")
        search, = self.organize_calls('/10/recognition/search')
        self.assertEqual(search['body']['confirmation_id'], 'group-001')
        preview = self.organize_calls('/batch/preview')[0]['body']
        self.assertEqual([(entry['log_id'], entry['expected_version'], entry['confirmation_id']) for entry in preview['entries']],
                         [(10, 4, 'group-001'), (11, 7, 'group-001')])
        self.assertEqual(preview['candidate']['tmdb_id'], '111')
        execute.click()
        self.page.locator('#appConfirmModal').wait_for()
        self.assertFalse(self.page.evaluate("__apiCalls.some(call => call.path === '/api/logs/organize/batch')"))
        self.page.locator('#appConfirmSubmit').click()
        self.page.wait_for_function("document.querySelector('#organizeOperationState').textContent.includes('逐条结果已返回')")
        submit, = self.organize_calls('/batch')
        self.assertEqual(submit['body']['entries'], preview['entries'])
        self.page.evaluate("window.__confirmationConfig.items[0].status='completed'")
        modal.locator('#organizeBatchCancelBtn').click()
        self.idle()
        self.assertFalse(self.page.locator('#confirmationReviewModal').evaluate('node => node.inert'))
        self.assertIn('1 组已完成', self.page.locator('#confirmationReviewState').inner_text())
        self.assertIn('已选 0 / 1000', self.page.locator('#confirmationReviewSelectionCount').inner_text())
        self.assertNotIn('媒体组 001', self.page.locator('#confirmationReviewList').inner_text())
        self.assertNotIn('include_correction_logs', self.calls('/query')[-1]['body'])
        for suffix in ('/selection', '/submit', '/skip'):
            self.assertEqual(self.calls(suffix), [])

    def test_correct_cross_page_groups_with_candidates_preserves_filter_and_shows_mixed_work_errors(self):
        items = groups(21)
        items[0]['log_ids'] = [10]
        items[-1]['log_ids'] = [11]
        self.page.evaluate("preview=>window.__testConfig.previewById['8801']=preview", support._preview('8801', errors=True))
        self.open(items)
        field = self.page.locator('#confirmationReviewQuery')
        field.fill('媒体组')
        field.press('Enter')
        self.idle()
        self.page.get_by_role('checkbox', name='选择待确认媒体组 媒体组 001', exact=True).check()
        self.page.locator('#confirmationReviewNext').click()
        self.idle()
        self.page.get_by_role('checkbox', name='选择待确认媒体组 媒体组 021', exact=True).check()
        trigger = self.page.locator('#confirmationReviewCorrect')
        trigger.click()
        modal = self.page.get_by_role('dialog', name='批量纠正识别')
        modal.wait_for()
        self.assertEqual(self.calls('/query')[-1]['body']['confirmation_ids'], ['group-001', 'group-021'])
        query = modal.locator('#organizeTmdbQuery')
        query.fill('8801')
        query.press('Enter')
        self.page.wait_for_function("document.querySelector('#organizeBatchPreview').textContent.includes('来源标题不匹配')")
        preview, = self.organize_calls('/batch/preview')
        self.assertEqual([(entry['log_id'], entry['confirmation_id']) for entry in preview['body']['entries']],
                         [(10, 'group-001'), (11, 'group-021')])
        self.assertTrue(modal.locator('#organizeBatchExecuteBtn').is_disabled())
        self.assertEqual(modal.locator('.organize-batch-preview-error').count(), 2)
        self.page.keyboard.press('Escape')
        self.idle()
        self.assertEqual(field.input_value(), '媒体组')
        self.assertIn('2 / 2 页', self.page.locator('#confirmationReviewPageInfo').inner_text())
        self.assertIn('已选 2 / 1000', self.page.locator('#confirmationReviewSelectionCount').inner_text())
        self.page.wait_for_function("document.activeElement.id==='confirmationReviewCorrect'")
        self.assertFalse(self.page.evaluate("__apiCalls.some(call => call.path === '/api/logs/organize/batch')"))

    def test_single_file_uses_single_log_preview_and_explicit_id_in_narrow_layout(self):
        self.page.set_viewport_size({'width': 320, 'height': 720})
        self.open([without_candidates(groups(1)[0], 10)])
        self.select_all()
        correction = self.page.locator('#confirmationReviewCorrect')
        self.assertEqual(self.page.locator('.confirmation-review-actions > button').count(), 4)
        self.assertTrue(self.page.locator('.confirmation-review-dialog').evaluate('node=>node.scrollWidth<=node.clientWidth'))
        bounds = correction.bounding_box()
        assert bounds is not None
        self.assertGreaterEqual(bounds['height'], 44)
        self.assertLessEqual(bounds['x'] + bounds['width'], 320)
        correction.click()
        modal = self.page.get_by_role('dialog', name='整理日志 #10')
        modal.wait_for()
        self.assertEqual(self.organize_calls('/10')[0]['confirmationId'], 'group-001')
        self.assertEqual(modal.locator('#organizeTmdbType').input_value(), 'tv')
        query = modal.locator('#organizeTmdbQuery')
        query.fill('8801')
        query.press('Enter')
        modal.locator('#organizeNamingPreview').wait_for()
        self.assertIn('Correct.mkv', modal.locator('#organizeNamingPreview').inner_text())
        preview, = self.organize_calls('/10/reorganize/preview')
        self.assertEqual(preview['body']['confirmation_id'], 'group-001')
        modal.locator('#organizeReorganizeBtn').click()
        self.page.locator('#appConfirmModal').wait_for()
        self.assertFalse(self.page.evaluate("__apiCalls.some(call=>call.path.endsWith('/reorganize'))"))
        self.page.locator('#appConfirmSubmit').click()
        self.page.wait_for_function("document.querySelector('#organizeOperationState').textContent.includes('云端操作已完成')")
        post, = self.organize_calls('/10/reorganize')
        self.assertEqual((post['body']['tmdb_id'], post['body']['preview_digest']), ('8801', 'single-digest-8801'))
        self.assertEqual(post['body']['confirmation_id'], 'group-001')
        self.assertFalse(self.page.evaluate("__apiCalls.some(call=>call.path.includes('/organize/batch'))"))

    def test_partial_correction_receipt_opens_only_failed_log_for_retry(self):
        log_ids = list(range(10, 18))
        preview = support._preview('8801')
        preview['items'] = [{**preview['items'][0], 'log_id': log_id, 'episode': index,
                             'file_name': f'Correct - S01E{index:02d}.mkv'}
                            for index, log_id in enumerate(log_ids, 1)]
        result = {'requested': 8, 'completed': [{'log_id': log_id} for log_id in log_ids[:-1]],
                  'failed': [{'log_id': 17, 'error': '改名失败'}]}
        self.page.evaluate("""({preview,result})=>Object.assign(window.__testConfig,{
            previewById:{'8801':preview},taskStatus:'partial',taskResult:result,singleTaskStatus:'completed',
        })""", {'preview': preview, 'result': result})
        self.open([without_candidates(groups(1)[0], *log_ids)])
        self.select_all()
        self.page.locator('#confirmationReviewCorrect').click()
        batch = self.page.get_by_role('dialog', name='批量纠正识别')
        query = batch.locator('#organizeTmdbQuery')
        query.fill('8801')
        query.press('Enter')
        execute = batch.locator('#organizeBatchExecuteBtn')
        execute.click()
        self.page.locator('#appConfirmSubmit').click()
        failure = batch.get_by_role('button', name='打开失败的整理日志 #17')
        failure.wait_for()
        self.assertTrue(execute.is_disabled())
        self.assertIn('打开整理日志单独重试', batch.locator('#organizeBatchPreview').inner_text())
        self.assertEqual(batch.locator('.organize-batch-preview-result.is-success').count(), 7)
        self.assertEqual(batch.get_by_role('button', name='打开失败的整理日志').count(), 1)
        self.page.evaluate('window.__confirmationConfig.items[0].log_ids=[]')
        failure.click()
        single = self.page.get_by_role('dialog', name='整理日志 #17')
        query = single.locator('#organizeTmdbQuery')
        query.fill('8801')
        query.press('Enter')
        single.locator('#organizeNamingPreview').wait_for()
        self.assertEqual([call['confirmationId'] for call in self.organize_calls('/17')], ['group-001', None])
        self.assertNotIn('confirmation_id', self.organize_calls('/17/reorganize/preview')[0]['body'])
        single.locator('#organizeReorganizeBtn').click()
        self.assertEqual(self.organize_calls('/17/reorganize'), [])
        self.page.locator('#appConfirmSubmit').click()
        self.page.wait_for_function("document.querySelector('#organizeOperationState').textContent.includes('云端操作已完成')")
        self.assertEqual(len(self.organize_calls('/batch')), 1)
        self.assertNotIn('confirmation_id', self.organize_calls('/17/reorganize')[0]['body'])
        retry_paths = self.page.evaluate("window.__apiCalls.filter(call=>call.path.endsWith('/reorganize')).map(call=>call.path)")
        self.assertEqual(retry_paths, ['/api/logs/organize/17/reorganize'])

    def test_incomplete_and_oversize_selections_block_correction_before_reading_logs(self):
        first, second = groups(2)
        missing = {**without_candidates(second, 11, 12), "log_ids": [11]}
        large = {**without_candidates(second, *range(11, 61)), "log_ids": []}
        self.open([without_candidates(first, 10), missing])
        self.select_all()
        trigger = self.page.locator('#confirmationReviewCorrect')
        trigger.click()
        self.idle()
        status = self.page.locator('#confirmationReviewState').inner_text()
        self.assertIn('「媒体组 002」缺少完整的关联日志，无法整组纠正', status)
        self.assertIn('按回执中的日志编号在整理日志里单独重试失败项', status)
        self.page.evaluate("item=>{window.__confirmationConfig.items[0].log_ids=[];window.__confirmationConfig.items[1]=item;}", large)
        trigger.click()
        self.idle()
        self.assertIn('51 个视频，单次纠正最多 50 条日志', self.page.locator('#confirmationReviewState').inner_text())
        self.assertTrue(self.page.locator('#organizeDetailModal').is_hidden())
        self.assertFalse(self.page.evaluate("__apiCalls.some(call=>/\\/organize\\/\\d+$/.test(call.path))"))

    def test_agent_rechecks_unknown_cross_page_choices_for_mixed_candidate_eligibility(self):
        items = groups(21)
        items = [without_candidates(item, index) for index, item in enumerate(items, 100)]
        self.open(items)
        agent = self.page.locator('#confirmationReviewAgent')
        self.page.locator('#confirmationReviewSelectPage').check()
        self.assertTrue(agent.is_disabled())
        self.select_all()
        self.assertTrue(agent.is_enabled())
        agent.click()
        self.idle()
        self.assertTrue(agent.is_disabled())
        self.assertEqual(self.calls('/review'), [])
        self.assertIn('无法由 Agent 搜索候选', self.page.locator('#confirmationReviewState').inner_text())

        # 跨页快照包含已有候选、允许搜索的空候选和已经离开待确认状态的项。
        more = groups(24)[21:]
        more[1] = {**without_candidates(more[1], 122), 'can_discover_candidates': True}
        more[2] = {**without_candidates(more[2], 123), 'can_discover_candidates': True}
        self.page.evaluate('items=>window.__confirmationConfig.items.push(...items)', more)
        self.refresh()
        self.select_all()
        self.page.evaluate("window.__confirmationConfig.items[23].status='queued'")
        agent.click()
        self.idle()
        self.assertEqual([call['body'] for call in self.calls('/review')], [{'confirmation_ids': ['group-022', 'group-023']}])
        status = self.page.locator('#confirmationReviewState').inner_text()
        self.assertIn('其中 1 组将搜索候选', status)
        self.assertIn('21 组无候选且不具备搜索资格', status)
        self.assertTrue(self.calls('/query'))
        self.assertTrue(all('include_correction_logs' not in call['body'] for call in self.calls('/query')))

    def test_agent_candidate_search_reports_progress_and_results_then_previews_verified_candidate(self):
        groups_with_candidates = groups(3)
        items = [{**without_candidates(item, index), 'can_discover_candidates': True}
                 for index, item in enumerate(groups_with_candidates, 10)]
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.clock.install()
        self.page.evaluate('window.__confirmationConfig.holdReview=true')
        self.open(items)
        self.select_all()
        agent = self.page.locator('#confirmationReviewAgent')
        detail = self.page.locator('#confirmationReviewDetail')
        correction = self.page.locator('#confirmationReviewCorrect')
        self.assertTrue(agent.is_enabled())
        self.assertTrue(correction.is_enabled())
        self.assertIn('「Agent 只读复核」搜索候选', detail.inner_text())
        agent.click()
        self.page.wait_for_function('window.__heldConfirmationReviews.length===1')
        self.assertTrue(agent.is_disabled())
        self.assertIn('3 组将搜索候选', self.page.locator('#confirmationReviewState').inner_text())
        self.page.evaluate('window.__heldConfirmationReviews.shift()()')
        self.idle()
        self.assertEqual([call['body'] for call in self.calls('/review')], [
            {'confirmation_ids': ['group-001', 'group-002', 'group-003']},
        ])
        detail.get_by_text('候选搜索排队中', exact=True).wait_for()
        self.page.evaluate("window.__confirmationConfig.items[0].review_status='running'")
        self.page.clock.fast_forward(4100)
        detail.get_by_text('Agent 正在搜索候选', exact=True).wait_for()

        discovered = groups_with_candidates[0]['candidates']
        discovered[1]['title'] = '新找到的作品'
        self.page.evaluate("""candidates=>{
            const [found,empty,failed]=window.__confirmationConfig.items;
            Object.assign(found,{review_status:'approved',can_discover_candidates:false,candidates,
                suggestion:{candidate_index:1,summary:'作品与文件季集证据一致',source:'agent',confidence:0.91}});
            Object.assign(empty,{review_status:'abstained',review_summary:'TMDB 搜索未命中匹配的作品'});
            Object.assign(failed,{review_status:'failed',review_summary:'TMDB 搜索超时，可稍后重试'});
        }""", discovered)
        self.page.clock.fast_forward(4100)
        detail.get_by_text('Agent 候选建议（只读）', exact=True).wait_for()
        self.assertTrue(detail.get_by_role('radio', name='选择候选 新找到的作品 (2024)').is_checked())
        self.page.get_by_role('button', name='审阅 媒体组 002', exact=True).click()
        self.assertIn('未找到可用候选', detail.inner_text())
        self.assertIn('TMDB 搜索未命中匹配的作品', detail.inner_text())
        self.page.get_by_role('button', name='审阅 媒体组 003', exact=True).click()
        self.assertIn('复核失败', detail.inner_text())
        self.assertIn('TMDB 搜索超时，可稍后重试', detail.inner_text())
        self.assertTrue(correction.is_enabled())
        self.assertTrue(self.page.locator('.confirmation-review-dialog').evaluate('node=>node.scrollWidth<=node.clientWidth'))
        self.assertTrue(self.page.locator('#confirmationReviewSubmit').is_disabled())
        self.assertEqual(self.calls('/preview'), [])
        self.page.locator('#confirmationReviewPrepare').click()
        self.idle()
        self.assertEqual([call['body'] for call in self.calls('/preview')], [
            {'entries': [{'confirmation_id': 'group-001', 'candidate_index': 1}]},
        ])
        self.assertIn('新找到的作品', self.page.locator('#confirmationReviewPreview').inner_text())
        for suffix in ('/selection', '/submit', '/skip'):
            self.assertEqual(self.calls(suffix), [])

    def test_filters_and_cross_page_selection_use_frozen_post_queries(self):
        self.open()
        for field, value in [('Task', 'task-a'), ('Source', '光鸭来源 A'), ('Reason', '歧'), ('Query', '媒体组')]:
            self.page.locator('#confirmationReview' + field).fill(value)
        self.page.locator('#confirmationReviewFilters button').click()
        self.idle()
        self.assertEqual({key: self.calls('')[-1]['params'][key] for key in ('task_id', 'source', 'reason', 'q')}, {
            'task_id': 'task-a', 'source': '光鸭来源 A', 'reason': '歧', 'q': '媒体组',
        })
        self.page.get_by_role('checkbox', name='选择待确认媒体组 媒体组 001', exact=True).check()
        self.page.locator('#confirmationReviewNext').click()
        self.idle()
        self.page.get_by_role('checkbox', name='选择待确认媒体组 媒体组 021', exact=True).check()
        self.page.locator('#confirmationReviewPrev').click()
        self.idle()
        self.assertTrue(self.page.get_by_role('checkbox', name='选择待确认媒体组 媒体组 001', exact=True).is_checked())
        self.page.select_option('#confirmationReviewScope', 'selected')
        self.idle()
        self.assertEqual(self.page.locator('.confirmation-review-row').count(), 2)
        self.assertEqual(set(self.calls('/query')[-1]['body']['confirmation_ids']), {'group-001', 'group-021'})
        self.assertFalse(any('confirmation_ids=' in call['url'] for call in self.calls()))
        saved = self.page.evaluate("JSON.parse(sessionStorage.getItem('mediaflux.logs.confirmation-review'))")
        self.assertEqual(set(saved), {'selected', 'batch'})
        self.assertEqual(set(saved['selected']), {'group-001', 'group-021'})

    def test_select_all_25_groups_persists_across_pages_and_exact_source_filter(self):
        self.open()
        self.select_all()
        self.assertIn('25 / 1000', self.page.locator('#confirmationReviewSelectionCount').inner_text())
        self.page.locator('#confirmationReviewNext').click()
        self.idle()
        self.assertEqual(self.page.locator('.confirmation-review-row input:checked').count(), 5)
        self.page.locator('#confirmationReviewSource').fill('光鸭来源')
        self.page.locator('#confirmationReviewFilters button').click()
        self.idle()
        self.assertEqual(self.page.locator('.confirmation-review-row').count(), 0)
        self.assertIn('25 / 1000', self.page.locator('#confirmationReviewSelectionCount').inner_text())
        self.page.select_option('#confirmationReviewScope', 'selected')
        self.idle()
        self.assertEqual(len(self.calls('/query')[-1]['body']['confirmation_ids']), 25)
        self.assertEqual(self.page.locator('.confirmation-review-row input:checked').count(), 20)
        self.page.locator('#confirmationReviewSelectPage').uncheck()
        self.idle()
        self.assertIn('5 / 1000', self.page.locator('#confirmationReviewSelectionCount').inner_text())

    def test_select_matching_caps_at_1000_without_expanding_log_selection(self):
        self.open(groups(1002))
        self.assertIn('1002', self.page.locator('#confirmationReviewCounts').inner_text())
        self.select_all()
        self.assertIn('1000 / 1000', self.page.locator('#confirmationReviewSelectionCount').inner_text())
        self.assertIn('上限', self.page.locator('#confirmationReviewState').inner_text())
        self.page.select_option('#confirmationReviewScope', 'selected')
        self.idle()
        call = self.calls('/query')[-1]
        self.assertEqual(len(call['body']['confirmation_ids']), 1000)
        self.assertLess(len(call['url']), 100)
        self.page.locator('[aria-label="关闭批量审阅"]').click()
        self.page.locator('#organizeSelectAll').check()
        self.assertIn('2 条光鸭记录', self.page.locator('#organizeBatchCount').inner_text())
        self.assertEqual(self.page.locator('.organize-row-select:checked').count(), 2)

    def test_agent_is_read_only_and_poll_preserves_unsaved_choice_and_evidence(self):
        self.open(groups(3))
        self.select_all()
        self.page.locator('#confirmationReviewAgent').click()
        self.idle()
        self.assertEqual(len(self.calls('/review')), 1)
        self.assertEqual(self.calls('/review')[0]['body']['confirmation_ids'], ['group-001', 'group-002', 'group-003'])
        self.assertIn('复核排队中', self.page.locator('#confirmationReviewList').inner_text())
        self.page.locator('#confirmationReviewAgent').click()
        self.idle()
        self.assertEqual(len(self.calls('/review')), 1)
        self.page.locator('#confirmationReviewDetail input[value="0"]').check()
        self.page.evaluate("""() => {
            const items=window.__confirmationConfig.items;
            items[0].suggestion={candidate_index:1,summary:'新的只读建议',source:'agent',confidence:.8};
            items[0].review_status='approved';
            items[1].review_status='abstained';items[1].review_summary='演员与发行日期冲突，建议人工核对';
        }""")
        self.page.clock.install()
        before = len(self.calls(''))
        self.page.clock.fast_forward(4100)
        self.page.wait_for_function('(before)=>window.__confirmationCalls.filter(call=>!call.suffix).length>before', arg=before)
        self.idle()
        self.assertTrue(self.page.locator('#confirmationReviewDetail input[value="0"]').is_checked())
        detail = self.page.locator('#confirmationReviewDetail').inner_text()
        self.assertIn('发行日期：2024-04-08', detail)
        self.assertIn('演员：演员甲、演员乙', detail)
        self.assertIn('制作商：制作商甲', detail)
        self.page.get_by_role('button', name='审阅 媒体组 002', exact=True).click()
        self.assertIn('演员与发行日期冲突', self.page.locator('#confirmationReviewDetail').inner_text())
        self.assertEqual(self.page.locator('#confirmationReviewDetail input:checked').count(), 0)
        self.assertFalse(any(call['suffix'] in {'/selection', '/preview', '/submit'} for call in self.calls()))

    def test_manual_continuous_selection_and_distinct_preview_submit_then_restore(self):
        self.open(groups(3))
        self.select_all()
        self.page.get_by_role('button', name='审阅 媒体组 002', exact=True).click()
        self.page.locator('#confirmationReviewDetail input[value="0"]').check()
        self.page.get_by_role('button', name='保存并下一条', exact=True).click()
        self.idle()
        self.assertEqual(self.calls('/selection')[0]['body'], {'confirmation_id': 'group-002', 'candidate_index': 0})
        self.assertIn('媒体组 003', self.page.locator('#confirmationReviewDetail h3').inner_text())
        self.page.locator('#confirmationReviewDetail input[value="1"]').check()
        self.page.evaluate("Object.assign(window.__confirmationConfig,{reverseEntries:true,rejectedTokens:['group-002']})")
        self.page.locator('#confirmationReviewPrepare').click()
        self.idle()
        entries = [{'confirmation_id': 'group-001', 'candidate_index': 1}, {'confirmation_id': 'group-002', 'candidate_index': 0}]
        self.assertEqual(self.calls('/preview')[0]['body'], {'entries': entries})
        preview = self.page.locator('#confirmationReviewPreview').inner_text()
        self.assertIn('作品1候选B', preview)
        self.assertIn('作品2候选A', preview)
        self.assertIn('1 组不提交', preview)
        self.assertIn('分类层级及文件名由现有整理规则生成', preview)
        self.page.evaluate('window.__confirmationConfig.holdSubmit=true')
        submit = self.page.locator('#confirmationReviewSubmit')
        size = submit.bounding_box()
        submit.click()
        self.page.wait_for_function('window.__heldConfirmationSubmits.length===1')
        self.assertTrue(submit.is_disabled())
        self.assertEqual(submit.bounding_box(), size)
        self.page.evaluate('window.__heldConfirmationSubmits.shift()()')
        self.idle()
        self.assertEqual(len(self.calls('/submit')), 1)
        self.assertEqual(self.calls('/submit')[0]['body']['entries'], entries)
        self.page.wait_for_function("document.getElementById('confirmationReviewScope').value==='batch'")
        self.page.wait_for_function("document.getElementById('confirmationReviewList').textContent.includes('失败')")
        self.config['items'] = self.page.evaluate('window.__confirmationConfig.items')
        self.config['items'][0].update(status='completed', downstream={'strm': '已完成', 'media_refresh': '已刷新', 'partial': False})
        self.page.reload(wait_until='domcontentloaded')
        self.page.locator('#confirmationReviewModal').wait_for(state='visible')
        self.idle()
        self.assertEqual(self.page.locator('#confirmationReviewScope').input_value(), 'batch')
        self.page.get_by_role('button', name='审阅 媒体组 001', exact=True).click()
        self.assertIn('STRM：已完成', self.page.locator('#confirmationReviewDetail').inner_text())
        self.assertIn('媒体库刷新：已刷新', self.page.locator('#confirmationReviewDetail').inner_text())
        self.assertTrue(self.page.locator('#confirmationReviewSubmit').is_disabled())
        self.assertEqual(self.calls('/submit'), [])
        self.page.locator('#confirmationReviewPrepare').click()
        self.idle()
        self.assertEqual(self.calls('/preview'), [])
        self.assertIn('没有可确认项', self.page.locator('#confirmationReviewState').inner_text())

    def test_late_preview_cannot_enable_confirmation_after_close_reopen(self):
        self.open(groups(1))
        self.select_all()
        self.page.evaluate('window.__confirmationConfig.holdPreview=true')
        self.page.locator('#confirmationReviewPrepare').click()
        self.page.wait_for_function('window.__heldConfirmationPreviews.length===1')
        self.page.locator('[aria-label="关闭批量审阅"]').click()
        self.page.locator('#organizeConfirmationReviewBtn').click()
        self.page.evaluate('window.__heldConfirmationPreviews.shift()()')
        self.idle()
        self.assertTrue(self.page.locator('#confirmationReviewPreview').is_hidden())
        self.assertTrue(self.page.locator('#confirmationReviewSubmit').is_disabled())
        self.assertEqual(self.calls('/submit'), [])

    def test_preview_displays_episode_mapping_beside_original_filename(self):
        self.open(groups(1))
        self.select_all()
        self.page.evaluate("""window.__confirmationConfig.episodeMappings={'group-001':[
            {file_index:0,source_season:1,source_episode:2,target_season:2,target_episode:7}
        ]}""")
        self.page.locator('#confirmationReviewPrepare').click()
        self.idle()
        mapping = self.page.locator('.confirmation-review-episode-map').inner_text()
        self.assertEqual(mapping, 'Group1.S01E02.mkv：S01E02 → S02E07')
        self.assertEqual(self.calls('/submit'), [])
        self.assertFalse(self.page.locator('#confirmationReviewSubmit').is_disabled())

    def test_slow_failed_refresh_keeps_nodes_scroll_selection_and_button_dimensions(self):
        self.open()
        self.page.get_by_role('checkbox', name='选择待确认媒体组 媒体组 001', exact=True).check()
        self.page.evaluate("""() => {
            const list=document.getElementById('confirmationReviewList');list.scrollTop=250;
            window.__originalReviewRow=list.firstElementChild;
            window.__originalReviewChoice=document.querySelector('#confirmationReviewDetail input[value="1"]');
            window.__confirmationConfig.holdList=true;window.__confirmationConfig.failList=true;
        }""")
        refresh = self.page.locator('#confirmationReviewRefresh')
        size = refresh.bounding_box()
        refresh.click()
        self.page.wait_for_function('window.__heldConfirmationLists.length===1')
        self.assertEqual(refresh.bounding_box(), size)
        self.assertTrue(self.page.evaluate("window.__originalReviewRow===document.getElementById('confirmationReviewList').firstElementChild"))
        self.assertTrue(self.page.locator('[data-token="group-001"] input').is_checked())
        self.page.evaluate('window.__heldConfirmationLists.shift()()')
        self.idle()
        self.assertIn('列表暂时不可用', self.page.locator('#confirmationReviewState').inner_text())
        self.assertEqual(self.page.locator('#confirmationReviewList').evaluate('node=>node.scrollTop'), 250)
        self.assertTrue(self.page.evaluate("window.__originalReviewChoice===document.querySelector('#confirmationReviewDetail input[value=\"1\"]')"))

    def test_submit_rechecks_completed_status_and_never_reuses_preview(self):
        self.open(groups(1))
        self.select_all()
        self.page.locator('#confirmationReviewPrepare').click()
        self.idle()
        self.page.evaluate("window.__confirmationConfig.items[0].status='completed'")
        self.page.locator('#confirmationReviewSubmit').click()
        self.idle()
        self.assertEqual(self.calls('/submit'), [])
        self.assertTrue(self.page.locator('#confirmationReviewSubmit').is_disabled())
        self.assertIn('请重新预览', self.page.locator('#confirmationReviewState').inner_text())

    def test_query_failure_and_manual_save_failure_never_authorize_submit(self):
        self.open(groups(2))
        self.select_all()
        self.page.get_by_role('button', name='审阅 媒体组 002', exact=True).click()
        self.page.locator('#confirmationReviewDetail input[value="0"]').check()
        self.page.evaluate('window.__confirmationConfig.selectionError=true')
        self.page.get_by_role('button', name='保存并下一条', exact=True).click()
        self.idle()
        self.assertIn('人工选择保存失败', self.page.locator('#confirmationReviewState').inner_text())
        self.assertTrue(self.page.locator('#confirmationReviewDetail input[value="0"]').is_checked())
        self.page.evaluate('window.__confirmationConfig.failQuery=true')
        self.page.locator('#confirmationReviewPrepare').click()
        self.idle()
        self.assertIn('冻结状态读取失败', self.page.locator('#confirmationReviewState').inner_text())
        self.assertTrue(self.page.locator('#confirmationReviewSubmit').is_disabled())
        self.assertEqual(self.calls('/preview'), [])
        self.assertEqual(self.calls('/submit'), [])

    def test_stored_candidate_and_digest_never_authorize_execution(self):
        self.config['items'] = groups(1)
        self.config['items'][0]['suggestion'] = None
        self.page.evaluate("""() => sessionStorage.setItem('mediaflux.logs.confirmation-review', JSON.stringify({
            selected:['group-001'],batch:[],candidate_index:0,
            preview:{entries:[{confirmation_id:'group-001',candidate_index:0}],digest:'forged-browser-digest'}
        }))""")
        self.page.reload(wait_until='domcontentloaded')
        self.page.locator('#confirmationReviewModal').wait_for(state='visible')
        self.idle()
        self.assertEqual(self.page.locator('#confirmationReviewDetail input:checked').count(), 0)
        self.page.locator('#confirmationReviewPrepare').click()
        self.idle()
        self.assertIn('没有可确认项', self.page.locator('#confirmationReviewState').inner_text())
        self.assertTrue(self.page.locator('#confirmationReviewSubmit').is_disabled())
        self.assertFalse(any(call['suffix'] in {'/selection', '/review', '/preview', '/submit'} for call in self.calls()))

    def test_uncertain_submit_restores_server_batch_without_retry(self):
        self.open(groups(1))
        self.select_all()
        self.page.locator('#confirmationReviewPrepare').click()
        self.idle()
        self.page.evaluate('window.__confirmationConfig.submitError=true')
        self.page.locator('#confirmationReviewSubmit').click()
        self.idle()
        self.assertIn('提交状态待核实', self.page.locator('#confirmationReviewState').inner_text())
        self.assertEqual(len(self.calls('/submit')), 1)
        self.config['items'] = groups(1)
        self.config['items'][0].update(status='completed', selected_index=1)
        self.page.reload(wait_until='domcontentloaded')
        self.page.locator('#confirmationReviewModal').wait_for(state='visible')
        self.idle()
        self.assertIn('已完成', self.page.locator('#confirmationReviewList').inner_text())
        self.assertEqual(self.calls('/submit'), [])

    def test_mobile_fullscreen_keyboard_focus_and_disabled_agent(self):
        self.page.close()
        self.page = self.browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.add_init_script(script=support.API_FIXTURE)
        self.page.route('**/*', self._serve_local_page)
        self.page.goto('http://mediaflux.test/logs', wait_until='domcontentloaded')
        self.page.evaluate('window.__confirmationConfig.reviewEnabled=false')
        self.page.evaluate('items=>window.__confirmationConfig.items=items', groups(2))
        self.page.locator('#organizeConfirmationReviewBtn').tap()
        self.idle()
        self.page.locator('#confirmationReviewSelectMatching').tap()
        self.assertTrue(self.page.locator('#confirmationReviewAgent').is_disabled())
        dialog = self.page.locator('.confirmation-review-dialog')
        box = dialog.bounding_box()
        assert box is not None
        self.assertEqual((box['x'], box['y'], box['width'], box['height']), (0, 0, 390, 844))
        self.assertFalse(dialog.evaluate('node=>node.scrollWidth>node.clientWidth'))
        submit_box = self.page.locator('#confirmationReviewSubmit').bounding_box()
        assert submit_box is not None
        self.assertGreaterEqual(submit_box['height'], 44)
        field = self.page.locator('#confirmationReviewQuery')
        self.assertGreaterEqual(field.evaluate('node=>parseFloat(getComputedStyle(node).fontSize)'), 16)
        field.focus()
        self.assertEqual(dialog.bounding_box(), box)
        self.page.get_by_role('button', name='审阅 媒体组 002', exact=True).tap()
        self.page.locator('#confirmationReviewDetail input[value="0"]').tap()
        self.assertTrue(self.page.locator('#confirmationReviewDetail input[value="0"]').is_checked())
        self.assertFalse(self.page.locator('.confirmation-review-workspace').evaluate('node=>node.scrollWidth>node.clientWidth'))
        self.page.keyboard.press('Escape')
        self.page.locator('#confirmationReviewModal').wait_for(state='hidden')
        self.page.wait_for_function("document.activeElement.id==='organizeConfirmationReviewBtn'")

    def test_skip_one_group_ends_it_and_moves_to_the_next(self):
        self.open()
        detail = self.page.locator('#confirmationReviewDetail')
        self.assertIn('媒体组 001', detail.inner_text())
        detail.get_by_role('button', name='跳过此组').click()
        self.page.wait_for_function("() => document.getElementById('confirmationReviewState').textContent.startsWith('已跳过 1 组')")
        self.idle()
        self.assertEqual([call['body'] for call in self.calls('/skip')], [{'confirmation_ids': ['group-001']}])
        self.assertIn('文件保持原位', self.page.locator('#confirmationReviewState').inner_text())
        self.assertNotIn('媒体组 001', self.page.locator('#confirmationReviewList').inner_text())
        self.assertIn('媒体组 002', detail.inner_text())

    def test_batch_skip_confirms_first_and_reports_groups_that_could_not_be_skipped(self):
        self.page.evaluate("""() => { window.__confirmCalls=[]; window.__confirmAnswer=false;
            window.appConfirm=async options=>{window.__confirmCalls.push(options);return window.__confirmAnswer;}; }""")
        self.page.evaluate("tokens=>window.__confirmationConfig.skipFailTokens=tokens", ['group-002'])
        self.open()
        skip = self.page.locator('#confirmationReviewSkip')
        self.assertTrue(skip.is_disabled())
        self.page.locator('#confirmationReviewSelectPage').check()
        self.assertTrue(skip.is_enabled())
        skip.click()
        self.idle()
        self.assertEqual(self.calls('/skip'), [])
        self.assertIn('文件保持原位', self.page.evaluate('window.__confirmCalls[0].message'))
        self.page.evaluate('window.__confirmAnswer=true')
        skip.click()
        self.page.wait_for_function("() => document.getElementById('confirmationReviewState').textContent.startsWith('已跳过')")
        self.idle()
        requested = self.calls('/skip')[0]['body']['confirmation_ids']
        self.assertEqual(len(requested), 20)
        state = self.page.locator('#confirmationReviewState')
        self.assertIn('已跳过 19 组', state.inner_text())
        self.assertIn('1 组未跳过', state.inner_text())
        self.assertIn('is-error', state.get_attribute('class') or '')
        # 失败的组仍保持勾选，成功跳过的组离开待确认队列与选择。
        self.assertIn('已选 1 / 1000 组', self.page.locator('#confirmationReviewSelectionCount').inner_text())
        self.assertIn('媒体组 002', self.page.locator('#confirmationReviewList').inner_text())

    def test_skipping_last_group_keeps_receipt_clears_detail_and_refreshes_logs(self):
        self.open(groups(1))
        before = len([call for call in self.page.evaluate('window.__apiCalls') if call['path'] == '/api/logs/organize/timeline'])
        self.page.locator('#confirmationReviewDetail [data-review-action="skip"]').click()
        self.page.wait_for_function("() => document.getElementById('confirmationReviewList').textContent.includes('没有匹配')")
        self.idle()
        self.assertIn('已跳过 1 组', self.page.locator('#confirmationReviewState').inner_text())
        self.assertEqual(self.page.locator('#confirmationReviewDetail [data-review-action="skip"]').count(), 0)
        self.assertGreater(len([call for call in self.page.evaluate('window.__apiCalls') if call['path'] == '/api/logs/organize/timeline']), before)

    def test_skipping_only_group_on_last_page_returns_to_existing_page(self):
        self.open(groups(21))
        self.page.locator('#confirmationReviewNext').click()
        self.idle()
        self.page.locator('#confirmationReviewDetail [data-review-action="skip"]').click()
        self.page.wait_for_function("() => document.getElementById('confirmationReviewPageInfo').textContent.includes('1 / 1')")
        self.idle()
        self.assertEqual(self.page.locator('.confirmation-review-row').count(), 20)
        self.assertIn('已跳过 1 组', self.page.locator('#confirmationReviewState').inner_text())

    def test_skip_success_remains_visible_when_status_readback_fails(self):
        self.open(groups(1))
        self.page.evaluate('window.__confirmationConfig.failQuery=true')
        self.page.locator('#confirmationReviewDetail [data-review-action="skip"]').click()
        self.page.wait_for_function("() => document.getElementById('confirmationReviewState').textContent.includes('状态回读失败')")
        self.idle()
        message = self.page.locator('#confirmationReviewState').inner_text()
        self.assertIn('已跳过 1 组', message)
        self.assertIn('刷新核对', message)
        self.assertEqual(len(self.calls('/skip')), 1)

    def test_batch_skip_does_not_send_already_queued_selection(self):
        self.open(groups(2))
        self.select_all()
        self.page.evaluate("window.__confirmationConfig.items[0].status='queued'")
        self.page.evaluate("window.appConfirm=async()=>true")
        self.page.locator('#confirmationReviewSkip').click()
        self.page.wait_for_function("() => document.getElementById('confirmationReviewState').textContent.startsWith('已跳过')")
        self.idle()
        self.assertEqual(self.calls('/skip')[0]['body'], {'confirmation_ids': ['group-002']})
        self.assertIn('已选 1 / 1000', self.page.locator('#confirmationReviewSelectionCount').inner_text())
        self.assertEqual(self.page.evaluate('window.__confirmationConfig.items[0].status'), 'queued')

    def test_narrow_mobile_skip_controls_fit_and_are_reachable(self):
        self.open(groups(1))
        for width in (320, 390, 768, 1024, 1280):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 740})
                for selector in ('.confirmation-review-dialog', '.confirmation-review-workspace',
                                 '.confirmation-review-queue-head', '#confirmationReviewDetail'):
                    self.assertTrue(self.page.locator(selector).evaluate('node=>node.scrollWidth<=node.clientWidth'), selector)
                for button in self.page.locator('.confirmation-review-savebar button').all():
                    box = button.bounding_box()
                    self.assertIsNotNone(box)
                    self.assertGreaterEqual(box['height'], 44 if width <= 640 else 40)
                    self.assertGreaterEqual(box['x'], 0)
                    self.assertLessEqual(box['x'] + box['width'], width)
