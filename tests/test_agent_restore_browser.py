"""刷新恢复不能先绘制新会话：真实Jinja、延迟defer脚本与API的离线浏览器回归。"""
from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from tests import test_agent_kernel_browser as browser_support
from tests import test_agent_ux_shell_browser as shell_support

SESSION = 'session_restore_browser_0001'
SESSION_KEY = 'mediaflux.agent.kernel.session.v1'
LAYOUT_KEY = 'mediaflux.agent.kernel.layout.v1'
SCOPE = 'b' * 64


def restored_candidate_view(ref: str, selection_ref: str) -> dict:
    return {
        'ref': ref,
        'expires_at': 4_102_444_800,
        'selection_ref': selection_ref,
        'recommended_positions': [],
        'target': 'guangya',
        'target_source': 'saved_preference',
        'targets': [
            {'value': 'qb', 'label': 'qBittorrent', 'available': True},
            {'value': 'guangya', 'label': '光鸭', 'available': True},
        ],
        'items': [
            {'position': 1, 'title': f'{ref} 第一版', 'site_name': '资源站 A', 'size_text': '1 GB', 'tags': {}, 'reasons': [], 'warnings': []},
            {'position': 2, 'title': f'{ref} 第二版', 'site_name': '资源站 B', 'size_text': '2 GB', 'tags': {}, 'reasons': [], 'warnings': []},
        ],
    }


class AgentRestoreBrowserTests(shell_support.AgentShellBrowserCase):
    def restore_page(self: shell_support.AgentShellBrowserCase, *, viewport=None, stored=True, hint=None, history=True, delay=250, hold_script=False, fail=False, missing=False, ignore_abort=False, color_scheme: Literal["dark", "light", "no-preference", "null"] = "light", block_storage=False, session_details=None, session_list=None):
        context = self.browser.new_context(viewport=viewport or {'width': 1280, 'height': 800}, color_scheme=color_scheme)
        self.addCleanup(context.close)
        pending = []
        errors = []
        unexpected = []

        def route(request):
            url = urlsplit(request.request.url)
            path = unquote(url.path)
            if url.netloc == 'testserver' and path == '/agent':
                request.fulfill(status=200, content_type='text/html', body=self.html)
                return
            if url.netloc == 'testserver' and path.startswith('/static/'):
                asset = (self.static_root / path.removeprefix('/static/')).resolve()
                if asset.is_relative_to(self.static_root) and asset.is_file():
                    if hold_script and path == '/static/js/agent.js':
                        pending.append((request, asset))
                        return
                    request.fulfill(status=200, content_type=mimetypes.guess_type(str(asset))[0] or 'application/octet-stream', body=asset.read_bytes())
                    return
            unexpected.append(request.request.url)
            request.abort()

        context.route('**/*', route)
        context.route_web_socket('**/*', lambda ws: ws.close())
        config = {
            'sessions': {'draft_scope': SCOPE, 'sessions': session_list if session_list is not None else [
                {'session_id': SESSION, 'title': '已有会话', 'message_count': 2},
            ] if history else []},
            'sessionDetails': session_details if session_details is not None else {SESSION: {'messages': [
                {'role': 'user', 'content': '查询媒体任务'},
                {'role': 'assistant', 'content': '这是刷新前已有的历史回复。'},
            ] if history else []}},
            'delay': delay, 'failHistory': fail, 'ignoreAbort': ignore_abort,
        }
        if missing:
            config['sessionDetails'] = {}
        seed = {'blockStorage': block_storage, 'stored': stored, 'hint': hint, 'session': SESSION, 'scope': SCOPE, 'key': SESSION_KEY, 'layoutKey': LAYOUT_KEY}
        script = f"""
        (() => {{
          const seed = {json.dumps(seed)};
          if (!sessionStorage.getItem('restore-fixture-seeded')) {{
            if (seed.stored) {{ localStorage.setItem(seed.key, seed.session); localStorage.setItem(seed.key+'.'+seed.scope, seed.session); }}
            if (seed.hint) localStorage.setItem(seed.layoutKey, JSON.stringify({{session_id:seed.session, mode:seed.hint}}));
            sessionStorage.setItem('restore-fixture-seeded','1');
          }}
          ({browser_support.MOCK_FETCH.replace('window.renderLucideIcons = () => {};', '')})({json.dumps(config, ensure_ascii=False)});
          if (seed.blockStorage) {{ Storage.prototype.getItem = Storage.prototype.setItem = () => {{throw new DOMException('Blocked','SecurityError');}}; }}
          const fetch = window.fetch;
          window.__restoreRequests = [];
          window.fetch = async (url, options={{}}) => {{
            if (String(url).includes('/api/agent/sessions')) {{
              window.__restoreRequests.push(String(url));
              if (window.__kernelConfig.failHistory) return new Response(JSON.stringify({{error:'历史服务暂不可用'}}),{{status:503}});
              await new Promise((resolve,reject) => {{
                const timer = setTimeout(resolve, window.__kernelConfig.delay);
                const abort = () => {{clearTimeout(timer); reject(new DOMException('Aborted','AbortError'));}};
                if (!window.__kernelConfig.ignoreAbort) {{if (options.signal?.aborted) abort(); else options.signal?.addEventListener('abort',abort,{{once:true}});}}
              }});
            }}
            return fetch(url, options);
          }};
          window.__restoreFrames = [];
          function frame() {{
            const node=document.querySelector('.agent-console'), composer=document.querySelector('#agentComposer');
            if(node && composer) {{const rect=composer.getBoundingClientRect();window.__restoreFrames.push({{empty:node.classList.contains('is-empty'), y:rect.y, h:rect.height}});}}
            if(window.__restoreFrames.length<240) requestAnimationFrame(frame);
          }}
          requestAnimationFrame(frame);
        }})();
        """
        context.add_init_script(script)
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto('http://testserver/agent', wait_until='commit')
        page.wait_for_selector('#agentPrompt', state='attached')
        return page, pending, errors, unexpected

    def release_script(self, pending):
        for request, asset in pending[:]:
            request.fulfill(status=200, content_type='text/javascript', body=asset.read_bytes())
            pending.remove((request, asset))

    def test_saved_conversation_is_not_painted_as_empty_before_deferred_script(self):
        page, pending, errors, unexpected = self.restore_page(hold_script=True)
        page.wait_for_function('window.__restoreFrames.length >= 3')
        self.assertFalse(page.locator('.agent-console').evaluate("n => n.classList.contains('is-empty')"), '历史刷新首绘不能呈现新会话空态')
        self.assertFalse(page.locator('#agentEmptyIntro').is_visible())
        self.assertFalse(page.locator('.agent-start-panel').is_visible())
        self.assertTrue(page.locator('#agentSend').is_disabled())
        before = page.locator('#agentComposer').bounding_box()
        assert before is not None
        self.release_script(pending)
        page.wait_for_function("document.querySelector('#agentTranscript').textContent.includes('历史回复')")
        after = page.locator('#agentComposer').bounding_box()
        assert after is not None
        self.assertLessEqual(abs(before['y'] - after['y']), 0.5)
        self.assertEqual(before['height'], after['height'])
        self.assertFalse(any(frame['empty'] for frame in page.evaluate('window.__restoreFrames')))
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_pending_candidate_approval_mounts_to_matching_ref_not_first_history_card(self):
        first = restored_candidate_view('ref_restore_candidate_first', 'ref_restore_selection_first')
        latest = restored_candidate_view('ref_restore_candidate_latest', 'ref_restore_selection_latest')
        approval = {
            'plan_id': 'plan_restore_candidate_00001',
            'tool_name': 'ingest.submit',
            'effect': 'WRITE',
            'preview': {'data': {'source_type': 'resource_candidates', 'count': 1, 'target': 'guangya'}},
            'result': {},
            'confirmation': {},
            'expires_at': '2026-09-20T12:00:00+00:00',
        }
        details = {
            SESSION: {
                'messages': [
                    {'role': 'assistant', 'content': '第一轮候选', 'candidate_view': first},
                    {'role': 'assistant', 'content': '最新候选', 'candidate_view': latest},
                ],
                'candidate_view': latest,
                'pending_approval': approval,
            },
        }
        page, _, errors, unexpected = self.restore_page(
            session_details=details,
            session_list=[{'session_id': SESSION, 'title': '候选恢复', 'message_count': 2}],
        )
        page.wait_for_selector(f'.agent-candidates[data-candidate-view="{latest["ref"]}"] [data-effect-confirm]')
        self.assertEqual(page.locator('.agent-candidates').count(), 2)
        self.assertEqual(page.locator(f'.agent-candidates[data-candidate-view="{first["ref"]}"] [data-effect-confirm]').count(), 0)
        self.assertEqual(page.locator(f'.agent-candidates[data-candidate-view="{latest["ref"]}"] [data-effect-confirm]').count(), 1)
        self.assertTrue(page.locator(f'.agent-candidates[data-candidate-view="{first["ref"]}"] [data-candidate-position]').first.is_disabled())
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_missing_current_candidate_view_keeps_history_readonly_and_detaches_approval(self):
        first = restored_candidate_view('ref_restore_candidate_missing_first', 'ref_restore_selection_missing_first')
        latest = restored_candidate_view('ref_restore_candidate_missing_latest', 'ref_restore_selection_missing_latest')
        approval = {
            'plan_id': 'plan_restore_candidate_missing_00001',
            'tool_name': 'ingest.submit',
            'effect': 'WRITE',
            'preview': {'data': {'source_type': 'resource_candidates', 'count': 1, 'target': 'guangya'}},
            'result': {},
            'confirmation': {},
            'expires_at': '2026-09-20T12:00:00+00:00',
        }
        details = {
            SESSION: {
                'messages': [
                    {'role': 'assistant', 'content': '第一轮历史候选', 'candidate_view': first},
                    {'role': 'assistant', 'content': '第二轮历史候选', 'candidate_view': latest},
                ],
                'candidate_view': None,
                'pending_approval': approval,
            },
        }
        page, _, errors, unexpected = self.restore_page(
            session_details=details,
            session_list=[{'session_id': SESSION, 'title': '缺失当前候选', 'message_count': 2}],
        )
        page.wait_for_selector('.agent-candidates')
        self.assertEqual(page.locator('.agent-candidates').count(), 2)
        self.assertEqual(page.locator('.agent-candidates .agent-confirmation-card').count(), 0)
        self.assertEqual(page.locator('.agent-confirmation-card').count(), 1)
        self.assertEqual(page.locator('.agent-candidates [data-candidate-position]:not([disabled])').count(), 0)
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def assert_centered(self, page):
        composer = page.locator('#agentComposer').bounding_box()
        assert composer is not None
        workspace = page.locator('.agent-workbench').bounding_box()
        assert workspace is not None
        self.assertLessEqual(abs(composer['y'] + composer['height'] / 2 - workspace['y'] - workspace['height'] / 2), 0.5)

    def screenshot(self, page, name):
        directory = os.getenv('AGENT_RESTORE_SCREENSHOTS')
        if directory:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path / f'{name}.png'))

    def test_repeated_real_reload_retains_layout_in_desktop_mobile_and_dark_mode(self):
        for viewport, color in (({'width': 1280, 'height': 800}, 'light'), ({'width': 390, 'height': 844}, 'dark')):
            with self.subTest(viewport=viewport, color=color):
                page, pending, errors, unexpected = self.restore_page(viewport=viewport, hold_script=True, color_scheme=color)
                for iteration in range(3):
                    if iteration:
                        page.reload(wait_until='commit')
                        page.wait_for_selector('#agentPrompt', state='attached')
                    page.wait_for_function('window.__restoreFrames.length >= 3')
                    self.assertFalse(page.locator('#agentEmptyIntro').is_visible())
                    self.assertFalse(page.locator('.agent-start-panel').is_visible())
                    before = page.locator('#agentComposer').bounding_box()
                    assert before is not None
                    self.screenshot(page, f'{color}-{iteration}-before-main-js')
                    self.release_script(pending)
                    page.wait_for_function("document.querySelector('#agentTranscript').textContent.includes('历史回复')")
                    after = page.locator('#agentComposer').bounding_box()
                    assert after is not None
                    self.assertLessEqual(abs(before['y'] - after['y']), 0.5)
                    self.assertEqual(before['height'], after['height'])
                    self.assertFalse(any(frame['empty'] for frame in page.evaluate('window.__restoreFrames')))
                    self.assertFalse(page.locator('#agentRestoreNotice').is_visible())
                    self.screenshot(page, f'{color}-{iteration}-restored')
                self.assertEqual(errors, [])
                self.assertEqual(unexpected, [])
                layout = page.evaluate(f"JSON.parse(localStorage.getItem('{LAYOUT_KEY}'))")
                self.assertEqual(layout, {'session_id': SESSION, 'mode': 'conversation'})

    def test_first_visit_and_known_empty_session_stay_centered(self):
        for stored in (False, True):
            with self.subTest(stored=stored):
                page, pending, errors, unexpected = self.restore_page(stored=stored, hint='empty' if stored else None, history=False, hold_script=True)
                page.wait_for_function('window.__restoreFrames.length >= 3')
                self.assert_centered(page)
                self.assertTrue(page.locator('#agentSend').is_disabled())
                page.locator('#agentPrompt').fill('等待恢复时写下的草稿')
                before = page.locator('#agentComposer').bounding_box()
                assert before is not None
                self.release_script(pending)
                page.wait_for_function("document.querySelector('#agentSessionList').getAttribute('aria-busy') === 'false'")
                page.wait_for_timeout(350)
                self.assert_centered(page)
                bounds = page.locator('#agentComposer').bounding_box()
                assert bounds is not None
                self.assertEqual(before['y'], bounds['y'])
                self.assertEqual(page.locator('#agentPrompt').input_value(), '等待恢复时写下的草稿')
                self.assertTrue(all(frame['empty'] for frame in page.evaluate('window.__restoreFrames')))
                self.assertEqual(errors, [])
                self.assertEqual(unexpected, [])

    def test_failed_restore_can_retry_without_losing_editing_or_replaying_queries(self):
        page, _, errors, unexpected = self.restore_page(fail=True)
        page.locator('#agentRestoreRetry').wait_for(state='visible')
        self.assertFalse(page.locator('#agentEmptyIntro').is_visible())
        page.locator('#agentPrompt').fill('恢复失败期间继续编辑')
        self.assertTrue(page.locator('#agentSend').is_disabled())
        page.locator('#agentPrompt').press('Enter')
        self.assertFalse(any(c['url'] == '/api/agent/query' for c in page.evaluate('window.__kernelCalls')))
        before = page.locator('#agentComposer').bounding_box()
        assert before is not None
        self.screenshot(page, 'restore-failed')
        page.evaluate('window.__kernelConfig.failHistory = false')
        page.locator('#agentRestoreRetry').click()
        page.wait_for_function("document.querySelector('#agentTranscript').textContent.includes('历史回复')")
        self.assertEqual(page.locator('#agentPrompt').input_value(), '恢复失败期间继续编辑')
        bounds = page.locator('#agentComposer').bounding_box()
        assert bounds is not None
        self.assertEqual(before['y'], bounds['y'])
        self.assertFalse(page.locator('#agentSend').is_disabled())
        self.assertFalse(page.locator('#agentRestoreNotice').is_visible())
        self.assertFalse(any(c['method'] != 'GET' for c in page.evaluate('window.__kernelCalls')))
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_missing_old_session_offers_explicit_new_session_without_deleting_history(self):
        page, _, errors, unexpected = self.restore_page(missing=True)
        page.locator('#agentRestoreNew').wait_for(state='visible')
        page.locator('#agentPrompt').fill('不要丢掉的草稿')
        page.locator('#agentRestoreNew').click()
        self.assert_centered(page)
        self.assertFalse(page.locator('#agentRestoreNotice').is_visible())
        self.assertFalse(page.locator('.agent-console').evaluate("n => n.classList.contains('is-restoring')"))
        self.assertEqual(page.locator('#agentPrompt').input_value(), '')
        drafts = page.evaluate(f"JSON.parse(sessionStorage.getItem('mediaflux.agent.drafts.v1.{SCOPE}'))")
        self.assertEqual(drafts[SESSION]['text'], '不要丢掉的草稿')
        self.assertFalse(any(c['method'] != 'GET' for c in page.evaluate('window.__kernelCalls')))
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_late_restore_cannot_overwrite_new_session_even_if_abort_is_ignored(self):
        page, _, errors, unexpected = self.restore_page(delay=350, ignore_abort=True)
        page.wait_for_function("window.__restoreRequests.some(path => path.endsWith('/' + 'session_restore_browser_0001'))")
        page.locator('#agentNewSession').click()
        page.locator('#agentPrompt').fill('新会话自己的草稿')
        page.wait_for_timeout(800)
        self.assert_centered(page)
        self.assertNotIn('历史回复', page.locator('#agentTranscript').inner_text())
        self.assertEqual(page.locator('#agentPrompt').input_value(), '新会话自己的草稿')
        self.assertFalse(page.locator('#agentRestoreNotice').is_visible())
        self.assertFalse(page.locator('#agentSend').is_disabled())
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_restore_timeout_is_bounded_and_has_a_new_session_escape(self):
        page, pending, errors, unexpected = self.restore_page(delay=60_000, hold_script=True)
        page.clock.install()
        self.release_script(pending)
        page.wait_for_function('window.__restoreRequests.length > 0')
        page.clock.fast_forward(12_100)
        page.locator('#agentRestoreRetry').wait_for(state='visible')
        self.assertIn('超时', page.locator('#agentRestoreText').inner_text())
        page.locator('#agentRestoreNew').click()
        self.assert_centered(page)
        self.assertFalse(page.locator('#agentRestoreNotice').is_visible())
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_layout_hint_never_restores_other_accounts_content(self):
        page, pending, errors, unexpected = self.restore_page(hold_script=True)
        other_scope, other_id = 'c' * 64, 'session_other_account_0001'
        page.evaluate('''({scope,id,key,layout}) => {
            localStorage.setItem(key+'.'+scope,id);
            localStorage.setItem(layout, JSON.stringify({session_id:'session_restore_browser_0001',mode:'conversation',text:'不可信旧账号缓存正文'}));
            window.__kernelConfig.sessions={draft_scope:scope,sessions:[{session_id:id,title:'当前账号',message_count:1}]};
            window.__kernelConfig.sessionDetails={[id]:{messages:[{role:'assistant',content:'当前账号自己的回复'}]}};
        }''', {'scope': other_scope, 'id': other_id, 'key': SESSION_KEY, 'layout': LAYOUT_KEY})
        self.release_script(pending)
        page.wait_for_function("document.querySelector('#agentTranscript').textContent.includes('当前账号自己的回复')")
        self.assertNotIn('不可信旧账号缓存正文', page.locator('body').inner_text())
        self.assertNotIn('刷新前已有', page.locator('#agentTranscript').inner_text())
        details = [c['url'] for c in page.evaluate('window.__kernelCalls') if c['url'].startswith('/api/agent/sessions/')]
        self.assertEqual(details, [f'/api/agent/sessions/{other_id}'])
        self.assertEqual(page.evaluate(f"JSON.parse(localStorage.getItem('{LAYOUT_KEY}'))"), {'session_id': other_id, 'mode': 'conversation'})
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_blocked_storage_keeps_the_new_session_usable(self):
        page, pending, errors, unexpected = self.restore_page(block_storage=True, hold_script=True)
        page.wait_for_function('window.__restoreFrames.length >= 3')
        self.assert_centered(page)
        self.release_script(pending)
        page.locator('#collapseSidebar').click()
        self.assertEqual(page.locator('html').get_attribute('data-sidebar'), 'collapsed')
        page.set_viewport_size({'width': 1200, 'height': 800})
        self.assertEqual(page.locator('html').get_attribute('data-sidebar'), 'collapsed')
        page.locator('#agentPrompt').fill('不依赖浏览器存储')
        page.wait_for_function("!document.querySelector('#agentSend').disabled")
        self.assertFalse(page.locator('#agentRestoreNotice').is_visible())
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_invalid_success_payload_is_not_treated_as_an_empty_history(self):
        page, pending, errors, unexpected = self.restore_page(hold_script=True)
        page.evaluate("window.fetch = async () => new Response('<html>登录或代理错误页</html>', {status:200})")
        self.release_script(pending)
        page.locator('#agentRestoreRetry').wait_for(state='visible')
        self.assertFalse(page.locator('#agentEmptyIntro').is_visible())
        self.assertTrue(page.locator('#agentSend').is_disabled())
        self.assertNotIn('登录或代理错误页', page.locator('body').inner_text())
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_opening_history_observes_startup_request_instead_of_cancelling_it(self):
        page, _, errors, unexpected = self.restore_page(delay=600)
        page.wait_for_function("window.__restoreRequests.includes('/api/agent/sessions')")
        browser_support.open_sessions(page)
        page.wait_for_function("document.querySelector('#agentTranscript').textContent.includes('历史回复')")
        page.keyboard.press('Escape')
        page.locator('#agentPrompt').fill('抽屉关闭后可以继续发送')
        self.assertFalse(page.locator('#agentRestoreNotice').is_visible())
        self.assertFalse(page.locator('#agentSend').is_disabled())
        calls = [c for c in page.evaluate('window.__kernelCalls') if c['url'] == '/api/agent/sessions']
        self.assertEqual(len(calls), 1)
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_account_transition_stores_matching_empty_hint_before_real_reload(self):
        page, pending, errors, unexpected = self.restore_page(hold_script=True, delay=100)
        self.release_script(pending)
        page.wait_for_function("document.querySelector('#agentTranscript').textContent.includes('历史回复')")
        other_scope = 'c' * 64
        page.evaluate("scope => {window.__kernelConfig.sessions={draft_scope:scope,sessions:[]};window.__kernelConfig.sessionDetails={};}", other_scope)
        browser_support.open_sessions(page)
        page.wait_for_function("document.querySelector('#agentSessionCount').textContent === '0 条'")
        new_id = page.evaluate(f"localStorage.getItem('{SESSION_KEY}')")
        self.assertNotEqual(new_id, SESSION)
        self.assertEqual(page.evaluate(f"JSON.parse(localStorage.getItem('{LAYOUT_KEY}'))"), {'session_id': new_id, 'mode': 'empty'})
        page.reload(wait_until='commit')
        page.wait_for_selector('#agentPrompt', state='attached')
        page.wait_for_function('window.__restoreFrames.length >= 3')
        self.assert_centered(page)
        before = page.locator('#agentComposer').bounding_box()
        assert before is not None
        page.evaluate("({scope,id}) => {window.__kernelConfig.sessions={draft_scope:scope,sessions:[]};window.__kernelConfig.sessionDetails={[id]:{messages:[]}};}", {'scope': other_scope, 'id': new_id})
        self.release_script(pending)
        page.wait_for_function("document.querySelector('#agentSessionList').getAttribute('aria-busy') === 'false'")
        page.wait_for_timeout(160)
        self.assertEqual(before, page.locator('#agentComposer').bounding_box())
        self.assertTrue(all(frame['empty'] for frame in page.evaluate('window.__restoreFrames')))
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_short_viewport_error_actions_never_cover_the_composer(self):
        page, _, errors, unexpected = self.restore_page(viewport={'width': 390, 'height': 300}, fail=True)
        page.locator('#agentRestoreNew').wait_for(state='visible')
        notice = page.locator('#agentRestoreNotice').bounding_box()
        assert notice is not None
        composer = page.locator('#agentComposer').bounding_box()
        assert composer is not None
        self.assertLessEqual(notice['y'] + notice['height'] + 4, composer['y'])
        page.locator('#agentRestoreNew').focus()
        button = page.locator('#agentRestoreNew').bounding_box()
        assert button is not None
        self.assertLessEqual(button['y'] + button['height'], notice['y'] + notice['height'] + 0.5)
        page.locator('#agentPrompt').click(position={'x': 20, 'y': 8})
        self.assertEqual(page.evaluate('document.activeElement.id'), 'agentPrompt')
        self.screenshot(page, 'short-390x300-restore-error')
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])

    def test_keyboard_retry_moves_focus_to_editing_and_preserves_selection(self):
        page, _, errors, unexpected = self.restore_page(fail=True, delay=300)
        page.locator('#agentRestoreRetry').wait_for(state='visible')
        page.evaluate('window.__kernelConfig.failHistory = false')
        page.locator('#agentRestoreRetry').focus()
        page.keyboard.press('Enter')
        self.assertEqual(page.evaluate('document.activeElement.id'), 'agentPrompt')
        page.locator('#agentPrompt').fill('my early draft')
        page.locator('#agentPrompt').evaluate('node => node.setSelectionRange(3,7)')
        page.wait_for_function("document.querySelector('#agentTranscript').textContent.includes('历史回复')")
        self.assertEqual(page.evaluate('document.activeElement.id'), 'agentPrompt')
        self.assertEqual(page.locator('#agentPrompt').evaluate('node => [node.selectionStart,node.selectionEnd]'), [3, 7])
        self.assertEqual(errors, [])
        self.assertEqual(unexpected, [])
