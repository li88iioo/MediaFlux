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
        "expires_at": "2099-12-01T00:00:00Z", "review_status": "waiting",
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


CONFIRMATION_API = r"""
(() => {
    const fallback=window.fetch;
    const endpoint='/api/logs/organize/confirmations';
    const reply=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
    window.__confirmationCalls=[];
    window.__heldConfirmationLists=[];
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
            const data={items:matched.slice((page-1)*size,page*size),total:matched.length,pages:Math.ceil(matched.length/size),page,
                confirmation_ids:rows.map(item=>item.confirmation_id),counts:matched.reduce((counts,item)=>{counts[item.status]=(counts[item.status]||0)+1;return counts;},{}),
                selection_truncated:truncated,review_enabled:config.reviewEnabled!==false};
            const response=config.failList&&!suffix?reply({error:'列表暂时不可用'},503):reply(data);
            if(config.holdList&&!suffix)return new Promise(resolve=>window.__heldConfirmationLists.push(()=>resolve(response)));
            return response;
        }
        if(suffix==='/review'){
            body.confirmation_ids.forEach(token=>{const item=config.items.find(item=>item.confirmation_id===token);item.review_status='pending';});
            return reply({requested:body.confirmation_ids.length,unchanged:0});
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
            # 使用生产弹窗管理器验证焦点、Esc、滚动锁；不修改共享夹具。
            html = html.replace('<script src="/static/js/logs.js">', '<script src="/static/js/app.js"></script><script src="/static/js/logs.js">')
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

    def select_all(self):
        self.page.locator('#confirmationReviewSelectMatching').click()

    def refresh(self):
        self.page.locator('#confirmationReviewRefresh').click()
        self.idle()

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
