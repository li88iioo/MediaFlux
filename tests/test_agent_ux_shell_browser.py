"""真实 Jinja/base 外壳的 Agent 布局回归；所有浏览器请求只由本地夹具处理。"""
from __future__ import annotations

import json
import mimetypes
import os
import unittest
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Browser
from urllib.parse import unquote, urlsplit

from tests import test_agent_kernel_browser as browser_support
from tests import test_agent_page as page_support
from tests.support import InitializedWebTestCase


@unittest.skipIf(browser_support.sync_playwright is None, '系统环境未安装 Playwright')
class AgentShellBrowserCase(InitializedWebTestCase):
    browser: Browser
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from app import database as db

        db.init_db()
        case = page_support.AgentPageTests('test_authenticated_agent_renders_stable_semantic_shell')
        try:
            case.setUp()
            try:
                case._login()
                response = case.client.get('/agent', follow_redirects=False)
                if response.status_code != 200:
                    raise AssertionError(f'Agent 页面夹具加载失败: {response.status_code}')
                cls.html = response.text
            finally:
                case.tearDown()
        finally:
            if not case.doCleanups():
                raise AssertionError('页面夹具注册清理失败')
        browser_support.AgentKernelBrowserTests.setUpClass.__func__(cls)
        cls.static_root = Path(os.getenv('MEDIAFLUX_BROWSER_STATIC_ROOT') or browser_support.ROOT / 'app/static').resolve()

    @classmethod
    def tearDownClass(cls):
        try:
            browser_support.AgentKernelBrowserTests.tearDownClass.__func__(cls)
        finally:
            super().tearDownClass()

    def make_shell(self, viewport, *, history):
        context = self.browser.new_context(viewport=viewport)
        self.addCleanup(context.close)
        unexpected = []
        errors = []
        assets = {}

        def route(request):
            url = urlsplit(request.request.url)
            path = unquote(url.path)
            if url.netloc == 'testserver' and path == '/agent':
                request.fulfill(status=200, content_type='text/html', body=self.html)
                return
            if url.netloc == 'testserver' and path.startswith('/static/'):
                asset = (self.static_root / path.removeprefix('/static/')).resolve()
                if asset.is_relative_to(self.static_root) and asset.is_file():
                    assets.setdefault(path, asset.read_bytes())
                    request.fulfill(status=200, content_type=mimetypes.guess_type(str(asset))[0] or 'application/octet-stream', body=assets[path])
                    return
            unexpected.append(request.request.url)
            request.abort()

        context.route('**/*', route)
        context.route_web_socket('**/*', lambda websocket: websocket.close())
        config = {
            'sessions': {'draft_scope': 'a' * 64, 'sessions': [
                {'session_id': 'session_shell_0000000001', 'title': '媒体库排障', 'message_count': 3},
            ] if history else []},
            'nextActionsDelayMs': 250,
            'nextActions': {'snapshot_status': 'attention', 'actions': [
                {'id': f'action-{i}', 'title': f'查看待办 {i}', 'description': '仅查看本地记录', 'prompt': f'查看待办 {i}'} for i in range(3)
            ]},
        }
        mock = browser_support.MOCK_FETCH.replace('window.renderLucideIcons = () => {};', '')
        context.add_init_script(f'({mock})({json.dumps(config, ensure_ascii=False)});')
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://testserver/agent')
        page.wait_for_function("document.querySelector('#agentSessionList').getAttribute('aria-busy') === 'false'")
        return page, unexpected, errors



class AgentUXShellBrowserTests(AgentShellBrowserCase):
    def test_real_shell_keeps_composer_centered_and_auxiliary_content_separate(self):
        for viewport in ({'width': 1280, 'height': 800}, {'width': 390, 'height': 844}, {'width': 320, 'height': 360}):
            for history in (False, True):
                with self.subTest(viewport=viewport, history=history):
                    page, unexpected, errors = self.make_shell(viewport, history=history)
                    before = page.locator('#agentComposer').bounding_box()
                    assert before is not None
                    page.wait_for_function("document.querySelector('#agentStartActions').getAttribute('aria-busy') === 'false'")
                    page.evaluate('async () => { await document.fonts.ready; }')
                    after = page.locator('#agentComposer').bounding_box()
                    assert after is not None
                    workspace = page.locator('.agent-workbench').bounding_box()
                    assert workspace is not None
                    console = page.locator('.agent-console').bounding_box()
                    assert console is not None
                    self.assertLessEqual(abs(console['height'] - workspace['height']), 0.5)
                    self.assertLessEqual(abs(after['y'] + after['height'] / 2 - workspace['y'] - workspace['height'] / 2), 0.5)
                    self.assertLessEqual(abs(before['y'] - after['y']), 0.5)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), viewport['width'])
                    self.assertEqual(page.locator('#agentResponseStatus').evaluate('(node) => getComputedStyle(node).position'), 'absolute')
                    if history:
                        resume = page.locator('#agentResumeLatestSession').bounding_box()
                        assert resume is not None
                        status = page.locator('#agentStartActionsStatus').bounding_box()
                        assert status is not None
                        self.assertLessEqual(resume['y'] + resume['height'] + 4, status['y'])
                    page.locator('#toggleAgentRail').click()
                    self.assertEqual(page.locator('#agent-session-heading').inner_text(), '历史会话')
                    self.assertTrue(page.locator('#agent-session-heading').evaluate('(node) => document.activeElement === node'))
                    self.assertEqual(page.locator('#agentSessionCount').inner_text(), f'{1 if history else 0} 条')
                    self.assertEqual(unexpected, [])
                    self.assertEqual(errors, [])


class AgentSessionStopBrowserTests(AgentShellBrowserCase):
    def session_stop_shell(self, viewport, snapshot=None):
        page, unexpected, errors = self.make_shell(viewport, history=True)
        page.evaluate("""snapshot => {
            const id = 'session_shell_0000000001';
            window.__kernelConfig.sessionDetails = {[id]: {
                messages: [{role: 'assistant', content: '已保存的任务结果。'}],
                active_turn: {request_id: 'shell-request', turn_id: 'shell-turn', generation: 1, status: 'running', protected: false},
                ...snapshot,
            }};
            window.__sessionStopCalls = [];
            window.__stopResult = {ok: true, status: 'stopped', stopped: true, model_turn: 'not_running'};
            window.__stopHttpStatus = 200;
            const original = window.fetch;
            window.fetch = async (url, options = {}) => {
                if (new URL(String(url), location.href).pathname !== '/api/agent/session/stop') return original(url, options);
                const body = JSON.parse(options.body);
                window.__sessionStopCalls.push(body);
                return new Promise(resolve => {
                    window.__completeSessionStop = () => {
                        Object.assign(window.__kernelConfig.sessionDetails[body.session_id], window.__stopSnapshot || {});
                        resolve(new Response(JSON.stringify(window.__stopResult), {
                            status: window.__stopHttpStatus, headers: {'Content-Type': 'application/json'},
                        }));
                    };
                });
            };
        }""", snapshot or {})
        page.locator('#agentResumeLatestSession').click()
        page.locator('.agent-message-assistant').first.wait_for()
        page.evaluate('async () => { await document.fonts.ready; }')
        self.assertEqual(page.locator('#agentStopSession').count(), 1)
        self.assertEqual(page.locator('#agentStop').count(), 0)
        return page, unexpected, errors

    def test_send_stop_share_rightmost_slot_without_layout_shift(self):
        for width, height in ((320, 667), (390, 844), (1280, 800)):
            with self.subTest(width=width):
                page, unexpected, errors = self.session_stop_shell({'width': width, 'height': height})
                stop = page.locator('#agentStopSession')
                send = page.locator('#agentSend')
                self.assertTrue(stop.is_visible())
                self.assertTrue(send.is_hidden())
                self.assertEqual(page.locator('.agent-composer-actions-right > button').count(), 2)
                self.assertEqual(stop.locator('..').get_attribute('class'), 'agent-submit-slot')
                self.assertEqual(stop.locator('svg').get_attribute('data-lucide'), 'square')
                expected_color = stop.evaluate("node => { const probe = document.createElement('span'); probe.style.color = 'var(--danger)'; node.append(probe); const color = getComputedStyle(probe).color; probe.remove(); return color; }")
                self.assertEqual(stop.evaluate('(node) => getComputedStyle(node).color'), expected_color)
                if output := os.getenv('MEDIAFLUX_UI_SCREENSHOTS'):
                    page.locator('#agentComposer').screenshot(path=str(Path(output) / f'stop-{width}.png'))
                page.locator('#agentPrompt').fill('未发送的草稿')
                page.mouse.move(0, 0)
                bounds = [page.locator(selector).bounding_box() for selector in ('#agentComposer', '.agent-submit-slot')]
                stop.focus()
                page.keyboard.press('Enter')
                page.wait_for_function("document.querySelector('#agentStopSession').disabled")
                stop.evaluate("node => node.dispatchEvent(new MouseEvent('click', {bubbles: true}))")
                page.locator('#agentPrompt').press('Enter')
                self.assertEqual(page.evaluate('window.__sessionStopCalls'), [{'session_id': 'session_shell_0000000001'}])
                page.evaluate("""() => {
                    window.__stopSnapshot = {active_turn: null, pending_effect_count: 0,
                        last_turn: {request_id: 'shell-request', turn_id: 'shell-turn', status: 'cancelled', message: '已停止'}};
                    window.__completeSessionStop();
                }""")
                stop.wait_for(state='hidden')
                self.assertTrue(send.is_visible())
                self.assertTrue(send.is_enabled())
                self.assertEqual(page.locator('#agentPrompt').input_value(), '未发送的草稿')
                page.mouse.move(0, 0)
                page.evaluate('() => Promise.all(document.getAnimations().map(a => a.finished.catch(() => {})))')
                for selector, before in zip(('#agentComposer', '.agent-submit-slot'), bounds):
                    assert before is not None
                    after = page.locator(selector).bounding_box()
                    assert after is not None
                    for key in ('x', 'y', 'width', 'height'):
                        self.assertAlmostEqual(before[key], after[key], delta=0.5)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                self.assertNotIn('/api/agent/query', [call['url'] for call in page.evaluate('window.__kernelCalls')])
                self.assertEqual(unexpected, [])
                self.assertEqual(errors, [])

    def test_idle_background_wait_keeps_send_and_hides_stop(self):
        page, unexpected, errors = self.session_stop_shell({'width': 390, 'height': 844}, {
            'active_turn': None, 'pending_effect_count': 1,
            'last_turn': {'request_id': 'background', 'turn_id': 'background-turn', 'status': 'waiting'},
        })
        self.assertTrue(page.locator('#agentStopSession').is_hidden())
        self.assertTrue(page.locator('#agentSend').is_visible())
        page.locator('#agentPrompt').fill('继续问一个问题')
        self.assertTrue(page.locator('#agentSend').is_enabled())
        self.assertEqual(unexpected, [])
        self.assertEqual(errors, [])

    def test_session_stop_reports_running_uncancellable_and_unconfirmed_results(self):
        page, unexpected, errors = self.session_stop_shell({'width': 390, 'height': 844})
        original_messages = page.locator('.agent-message-assistant').count()
        self.assertEqual(original_messages, 2)  # 已保存回复 + 正在运行的回复
        for status, expected in (
            ('stopping', '仍在'), ('partially_stopping', '不可取消'),
            ('partial', '不可取消'), ('critical_pending', '不可中断'),
            ('superseded', '无法确认'), ('stop_unconfirmed', '尚未确认'),
            ('unexpected_status', '尚未确认'),
        ):
            with self.subTest(status=status):
                page.evaluate("status => { window.__stopResult = {ok: true, status, stopped: false, uncancellable: []}; }", status)
                page.get_by_role('button', name='停止任务', exact=True).click()
                page.evaluate('window.__completeSessionStop()')
                page.wait_for_function("document.querySelector('#agentStopSession').getAttribute('aria-busy') === 'false'")
                text = page.locator('.app-toast-message').last.inner_text()
                self.assertIn(expected, text)
                self.assertNotIn('已撤回', text)
                self.assertNotIn('全部已停止', text)
                self.assertEqual(page.locator('.agent-message-assistant').count(), original_messages)
        self.assertEqual(unexpected, [])
        self.assertEqual(errors, [])

    def test_session_stop_error_allows_retry_and_ignores_response_after_session_switch(self):
        page, unexpected, errors = self.session_stop_shell({'width': 1280, 'height': 800})
        button = page.get_by_role('button', name='停止任务', exact=True)
        page.evaluate("() => { window.__stopHttpStatus = 503; window.__stopResult = {error: '本地停止入口暂不可用'}; }")
        button.click()
        page.evaluate('window.__completeSessionStop()')
        page.wait_for_function("document.querySelector('#agentStopSession').getAttribute('aria-busy') === 'false'")
        self.assertTrue(button.is_enabled())
        self.assertIn('尚未确认', page.locator('.app-toast-message').last.inner_text())
        page.evaluate("() => { window.__stopHttpStatus = 200; window.__stopResult = {ok: true, status: 'stopped', stopped: true}; }")
        button.click()
        page.locator('#agentNewSession').click()
        page.locator('#agentPrompt').fill('新会话草稿')
        page.evaluate('window.__completeSessionStop()')
        page.wait_for_function("document.querySelector('#agentStopSession').getAttribute('aria-busy') === 'false'")
        self.assertEqual(page.locator('#agentPrompt').input_value(), '新会话草稿')
        self.assertEqual(page.locator('.agent-message').count(), 0)
        self.assertNotIn('会话任务已停止', page.locator('#appToastContainer').inner_text())
        self.assertEqual(len(page.evaluate('window.__sessionStopCalls')), 2)
        self.assertEqual(unexpected, [])
        self.assertEqual(errors, [])

    def test_session_stop_remains_reachable_during_protected_turn(self):
        active = {'request_id': 'protected-request', 'turn_id': 'protected-turn',
                  'generation': 3, 'status': 'running', 'protected': True}
        page, unexpected, errors = self.session_stop_shell({'width': 390, 'height': 844}, {'active_turn': active})
        self.assertTrue(page.locator('#agentSend').is_disabled())
        page.evaluate("() => { window.__stopResult = {ok: true, status: 'critical_pending', stopped: false, model_turn: 'critical_pending'}; }")
        page.get_by_role('button', name='停止任务', exact=True).click()
        page.evaluate('window.__completeSessionStop()')
        page.wait_for_function("document.querySelector('#agentStopSession').getAttribute('aria-busy') === 'false'")
        self.assertIn('不可中断', page.locator('.app-toast-message').last.inner_text())
        self.assertTrue(page.locator('#agentSend').is_disabled())
        self.assertEqual(page.evaluate('window.__sessionStopCalls'), [{'session_id': 'session_shell_0000000001'}])
        self.assertEqual(unexpected, [])
        self.assertEqual(errors, [])

    def test_session_stop_waits_for_foreground_terminal_without_wildcard_query_cancel(self):
        page, unexpected, errors = self.session_stop_shell({'width': 390, 'height': 844}, {'active_turn': None})
        page.evaluate("() => { window.__kernelConfig.queryEvents = []; window.__kernelConfig.holdQueryOpen = true; }")
        page.locator('#agentPrompt').fill('继续只读检查')
        page.locator('#agentSend').click()
        page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'true'")
        page.evaluate("""() => {
            const call = window.__kernelCalls.findLast(call => call.url === '/api/agent/query');
            const request = JSON.parse(call.body);
            window.__foregroundRequest = request;
            window.__kernelConfig.sessionDetails[request.session_id] = {
                messages: [{role: 'assistant', content: '已保存的任务结果。'}, {role: 'user', content: request.message}],
                active_turn: {request_id: request.request_id, turn_id: `turn-${request.request_id}`,
                    generation: 2, status: 'cancelling', protected: false},
            };
            window.__stopResult = {ok: true, status: 'stopping', stopped: false, model_turn: 'stop_requested'};
        }""")
        page.get_by_role('button', name='停止任务', exact=True).click()
        page.evaluate('window.__completeSessionStop()')
        page.wait_for_function("document.querySelector('#agentStopSession').getAttribute('aria-busy') === 'false'")
        self.assertIn('仍在', page.locator('.app-toast-message').last.inner_text())
        self.assertEqual(page.locator('.agent-cancelled').count(), 0)
        self.assertTrue(page.locator('#agentSend').is_disabled())
        page.evaluate("""() => {
            const request = window.__foregroundRequest;
            Object.assign(window.__kernelConfig.sessionDetails[request.session_id], {
                active_turn: null,
                last_turn: {request_id: request.request_id, turn_id: `turn-${request.request_id}`,
                    status: 'cancelled', message: '本轮已取消'},
            });
        }""")
        page.locator('.agent-cancelled').wait_for()
        self.assertEqual(page.locator('.agent-message-user').count(), 1)
        self.assertEqual(page.locator('.agent-message-assistant').count(), 2)
        paths = [call['url'] for call in page.evaluate('window.__kernelCalls')]
        self.assertNotIn('/api/agent/query/cancel', paths)
        self.assertEqual(unexpected, [])
        self.assertEqual(errors, [])
