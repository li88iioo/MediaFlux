"""Agent体验的真实浏览器回归：等待、草稿、稳定位置与安全候选交互。"""
from __future__ import annotations

import json
import os
import re
import time
import unittest
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Browser

from tests import test_agent_kernel_browser as harness

SCOPE = 'a' * 64
SESSION_A = 'session_agent_ux_000000001'
SESSION_B = 'session_agent_ux_000000002'
SUBMITTED_276_RESULT = {
    'ok': True, 'status': 'submitted', 'summary': '下载请求 #276 已提交',
    'data': {'target': 'guangya'},
}
SUBMITTED_276_RECEIPT = (
    '📤 下载请求 #276 已提交\n'
    '- 状态：请求已提交，后台任务尚未完成；可以继续查询进度。\n'
    '- 目标：光鸭云盘'
)


def candidate_view(*, expires_at: float | None = None) -> dict:
    return {
        'ref': 'ref_resource_candidates_0000001',
        'expires_at': expires_at if expires_at is not None else time.time() + 900,
        'selection_ref': 'ref_selection_batch_0000000001',
        'recommended_positions': [1], 'target': 'guangya', 'target_source': 'saved_preference',
        'targets': [{'value': 'qb', 'label': 'qBittorrent', 'available': True}, {'value': 'guangya', 'label': '光鸭', 'available': True}, {'value': 'both', 'label': '两个目标', 'available': True}],
        'items': [
            {'position': 1, 'title': '第一集 1080p <img src=x onerror=window.__ux_xss=1>',
             'site_name': '资源站 A', 'size_text': '1.2 GB', 'tags': {'resolution': '1080p'},
             'reasons': ['明确匹配 S01E01'], 'warnings': ['字幕需要人工核对']},
            {'position': 2, 'title': '第一集 2160p', 'site_name': '资源站 B',
             'tags': {'resolution': '2160p', 'audio': 'AAC'}, 'reasons': ['存在备选版本'],
             'warnings': []},
        ],
    }


def events_for_candidates(view: dict) -> list[dict]:
    return [
        harness._event(1, 'turn.started'),
        harness._event(2, 'tool.completed', {'tool': 'resource.search', 'call_id': 'search1', 'result': {'candidate_view': view}}),
        harness._event(3, 'turn.completed', {'status': 'success', 'answer': '已找到候选，请先核对版本。'}),
    ]


def candidate_view_variant(ref: str, selection_ref: str, *, recommended_positions: list[int] | None = None) -> dict:
    view = candidate_view()
    view['ref'] = ref
    view['selection_ref'] = selection_ref
    view['recommended_positions'] = [] if recommended_positions is None else recommended_positions
    return view


def session_snapshot(session_id: str, *, messages: list[dict] | None = None,
                     active_turn: dict | None = None, last_turn: dict | None = None,
                     pending_approval: dict | None = None, candidate: dict | None = None,
                     generation: int = 1, pending_effect_count: int = 0) -> dict:
    return {
        'session_id': session_id,
        'generation': generation,
        'messages': list(messages or []),
        'pending_effect_count': pending_effect_count,
        'pending_approval': pending_approval,
        'candidate_view': candidate,
        'active_turn': active_turn,
        'last_turn': last_turn,
        'draft_scope': SCOPE,
    }


def active_turn(request_id: str, turn_id: str, *, status: str = 'running',
                protected: bool = False, detail: str = '后台仍在处理') -> dict:
    return {
        'request_id': request_id,
        'turn_id': turn_id,
        'generation': 1,
        'protected': protected,
        'status': status,
        'detail': detail,
    }


@unittest.skipIf(harness.sync_playwright is None, '系统环境未安装 Playwright')
class AgentUXBrowserTests(unittest.TestCase):
    browser: Browser
    source: str
    styles: str
    @classmethod
    def setUpClass(cls):
        harness.AgentKernelBrowserTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        harness.AgentKernelBrowserTests.tearDownClass.__func__(cls)

    make_page = harness.AgentKernelBrowserTests.make_page

    def page(self, config: dict | None = None, **kwargs):
        value = dict(config or {})
        value.setdefault('sessions', {'sessions': [], 'draft_scope': SCOPE})
        page = self.make_page(value, **kwargs)
        page.wait_for_function("() => document.querySelector('#agentSessionList').getAttribute('aria-busy') === 'false'")
        return page

    def reload_ui(self, page, config: dict):
        page.reload()
        page.add_style_tag(content=self.styles)
        page.evaluate(harness.MOCK_FETCH, config)
        page.add_script_tag(content=self.source)
        page.wait_for_function("() => document.querySelector('#agentSessionList').getAttribute('aria-busy') === 'false'")

    def progress_page(self, *, width: int = 1280):
        page = self.browser.new_page(viewport={'width': width, 'height': 844})
        self.addCleanup(page.close)
        # 在应用加载前安装时钟，发起请求前暂停，计时只由测试显式推进。
        page.clock.install(time='2026-11-07T00:00:00Z')
        page.route('http://mediaflux.test/agent', lambda route: route.fulfill(
            status=200, content_type='text/html', body=harness.HTML,
        ))
        page.goto('http://mediaflux.test/agent')
        page.add_style_tag(content=self.styles)
        page.evaluate(harness.MOCK_FETCH, {'sessions': {'sessions': [], 'draft_scope': SCOPE}})
        page.add_script_tag(content=self.source)
        page.wait_for_function("() => document.querySelector('#agentSessionList').getAttribute('aria-busy') === 'false'")
        page.add_script_tag(path=str(harness.ROOT / 'app/static/js/lucide.min.js'))
        page.evaluate(r"""() => {
            window.renderLucideIcons = () => window.lucide.createIcons();
            const original = window.fetch;
            window.fetch = (url, options = {}) => {
                if (String(url) !== '/api/agent/query') return original(url, options);
                window.__kernelCalls.push({url: String(url), method: options.method || 'GET', body: String(options.body || '')});
                const request = JSON.parse(options.body);
                let sequence = 0;
                return new Response(new ReadableStream({start(controller) {
                    window.emitProgress = (type, payload) => controller.enqueue(new TextEncoder().encode(JSON.stringify({
                        event_id: `progress-${++sequence}`, sequence, type, payload,
                        session_id: request.session_id, request_id: request.request_id, turn_id: 'progress-turn',
                    }) + '\n'));
                }}), {headers: {'Content-Type': 'application/x-ndjson'}});
            };
        }""")
        page.clock.pause_at('2026-11-07T01:00:00Z')
        return page

    def emit_progress(self, page, kind: str, payload: dict | None = None):
        page.evaluate('([kind, payload]) => window.emitProgress(kind, payload)', [kind, payload or {}])
        page.clock.run_for(100)

    def elapsed_seconds(self, page) -> float:
        elapsed = page.locator('.agent-stream-head time.agent-stream-elapsed')
        self.assertEqual(elapsed.count(), 1)
        self.assertTrue(elapsed.is_visible())
        match = re.fullmatch(r'·\s*(\d+)秒', elapsed.inner_text())
        self.assertIsNotNone(match, elapsed.inner_text())
        assert match is not None
        return float(match[1])

    def snapshot(self, page, name: str):
        output = os.getenv('MEDIAFLUX_BROWSER_EVIDENCE_DIR')
        if output:
            root = Path(output)
            root.mkdir(parents=True, exist_ok=True)
            page.add_script_tag(path=str(harness.ROOT / 'app/static/js/lucide.min.js'))
            page.evaluate('() => window.lucide?.createIcons()')
            page.evaluate("""async () => { await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {}))); }""")
            page.screenshot(path=str(root / f'{name}.png'))

    def test_next_actions_are_bounded_readonly_and_do_not_move_composer(self):
        actions = [{'id': f'item-{i}', 'title': f'检查待办 {i}', 'description': '只读本地快照', 'prompt': f'查看待办 {i}'} for i in range(5)]
        for viewport in ({'width': 1280, 'height': 800}, {'width': 390, 'height': 844}):
            with self.subTest(viewport=viewport):
                page = self.page({'nextActions': {'actions': actions, 'snapshot_status': 'attention'}, 'nextActionsDelayMs': 400}, viewport=viewport)
                before = page.locator('#agentComposer').bounding_box()
                page.wait_for_function("() => document.querySelectorAll('[data-agent-draft]').length === 3")
                after = page.locator('#agentComposer').bounding_box()
                assert after is not None
                assert before is not None
                self.assertLessEqual(abs(before['y'] - after['y']), 0.5)
                self.assertLessEqual(abs(before['height'] - after['height']), 0.5)
                console = page.locator('.agent-workbench').bounding_box()
                assert console is not None
                self.assertLessEqual(abs(after['y'] + after['height'] / 2 - console['y'] - console['height'] / 2), 0.5)
                self.assertTrue(page.locator('#agentStartActions').get_attribute('aria-busy') == 'false')
                page.locator('[data-agent-draft]').first.click()
                self.assertEqual(page.locator('#agentPrompt').input_value(), '查看待办 0')
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 0)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), viewport['width'])
                targets = page.locator('.agent-start-action').evaluate_all('(items) => items.map(item => item.getBoundingClientRect().height)')
                self.assertTrue(all(height >= 44 for height in targets))
                self.snapshot(page, f'start-{viewport["width"]}')

    def test_empty_resume_has_its_own_slot_and_history_focus_matches_rounded_style(self):
        for viewport in ({'width': 1280, 'height': 800}, {'width': 390, 'height': 844}, {'width': 390, 'height': 430}):
            with self.subTest(viewport=viewport):
                page = self.page({'sessions': {'draft_scope': SCOPE, 'sessions': [
                    {'session_id': SESSION_A, 'title': '媒体库排障', 'updated_at': 20, 'message_count': 16},
                ]}, 'sessionDetails': {SESSION_A: {'messages': []}}}, viewport=viewport)
                resume = page.locator('#agentStartResume #agentResumeLatestSession')
                page.wait_for_function("() => !document.querySelector('#agentResumeLatestSession').disabled")
                resume_box = resume.bounding_box()
                status_box = page.locator('#agentStartActionsStatus').bounding_box()
                composer_box = page.locator('#agentComposer').bounding_box()
                assert (resume_box) is not None
                console = page.locator('.agent-workbench').bounding_box()
                assert composer_box is not None
                assert console is not None
                self.assertLessEqual(abs(composer_box['y'] + composer_box['height'] / 2 - console['y'] - console['height'] / 2), 0.5)
                self.assertGreaterEqual(resume_box['y'], composer_box['y'] + composer_box['height'])
                assert status_box is not None
                self.assertLessEqual(resume_box['y'] + resume_box['height'] + 4, status_box['y'])
                self.assertLessEqual(composer_box['y'] + composer_box['height'], viewport['height'])
                self.snapshot(page, f'feedback-empty-with-history-{viewport["width"]}-{viewport["height"]}')
                page.locator('#toggleAgentRail').click()
                self.assertEqual(page.locator('#agentSessionCount').inner_text(), '1 条')
                self.assertEqual(page.locator('#agent-session-heading').inner_text(), '历史会话')
                self.assertEqual(page.locator('.agent-history-drawer .agent-kicker').count(), 0)
                self.assertTrue(page.locator('#agent-session-heading').evaluate('(node) => document.activeElement === node'))
                count = page.locator('#agentSessionCount').evaluate("node => {const style = getComputedStyle(node); return {border: style.borderTopWidth, background: style.backgroundColor, font: style.fontFamily};}")
                self.assertEqual(count['border'], '0px')
                self.assertEqual(count['background'], 'rgba(0, 0, 0, 0)')
                self.assertNotIn('mono', count['font'].lower())
                page.locator('#agentSessionSearch').focus()
                self.assertEqual(page.locator('#agentSessionSearch').evaluate('(node) => getComputedStyle(node).outlineStyle'), 'none')
                search_style = page.locator('.agent-session-search').evaluate("node => {const style = getComputedStyle(node); return {radius: style.borderRadius, shadow: style.boxShadow};}")
                self.assertEqual(search_style['radius'], '12px')
                self.assertNotEqual(search_style['shadow'], 'none')
                self.snapshot(page, f'feedback-history-search-{viewport["width"]}-{viewport["height"]}')
                page.locator('[data-session-rename]').click()
                editor = page.locator('.agent-session-editor input')
                style = editor.evaluate("node => {const style = getComputedStyle(node); return {outline: style.outlineStyle, radius: style.borderRadius, border: style.borderTopWidth};}")
                self.assertEqual(style, {'outline': 'none', 'radius': '10px', 'border': '1px'})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), viewport['width'])
                self.snapshot(page, f'feedback-history-rename-{viewport["width"]}-{viewport["height"]}')

    def test_next_actions_failure_leaves_chat_usable(self):
        page = self.page({'nextActionsStatus': 503})
        page.wait_for_function("() => document.querySelector('#agentStartActionsStatus').textContent.includes('暂时不可用')")
        self.assertTrue(page.locator('#agentPrompt').is_enabled())
        page.locator('#agentPrompt').fill('仍然可以提问')
        self.assertTrue(page.locator('#agentSend').is_enabled())

    def test_busy_composer_keeps_next_draft_without_queuing_or_retrying(self):
        page = self.page({'queryEvents': [harness._event(1, 'turn.started')], 'holdQueryOpen': True})
        page.locator('#agentPrompt').fill('检查下载状态')
        page.locator('#agentSend').click()
        page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'true'")
        self.assertTrue(page.locator('#agentPrompt').is_enabled())
        page.locator('#agentPrompt').fill('另外只看失败任务')
        page.locator('#agentPrompt').press('Enter')
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 1)
        page.locator('#agentStopSession').click()
        page.wait_for_selector('.agent-retry-draft')
        self.assertEqual(page.locator('#agentPrompt').input_value(), '另外只看失败任务')
        page.locator('.agent-retry-draft').click()
        self.assertEqual(page.locator('#agentPrompt').input_value(), '另外只看失败任务')
        page.locator('#agentPrompt').fill('')
        page.locator('.agent-retry-draft').click()
        self.assertEqual(page.locator('#agentPrompt').input_value(), '检查下载状态')
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 1)

    def test_drafts_survive_reload_but_are_account_scoped_and_expire(self):
        page = self.page()
        page.locator('#agentPrompt').fill('尚未发送的工作区问题')
        self.reload_ui(page, {'sessions': {'sessions': [], 'draft_scope': SCOPE}})
        self.assertEqual(page.locator('#agentPrompt').input_value(), '尚未发送的工作区问题')
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 0)
        self.reload_ui(page, {'sessions': {'sessions': [], 'draft_scope': 'b' * 64}})
        self.assertEqual(page.locator('#agentPrompt').input_value(), '')
        page.evaluate("""scope => {
            const key = 'mediaflux.agent.drafts.v1.' + scope;
            const drafts = JSON.parse(sessionStorage.getItem(key));
            for (const draft of Object.values(drafts)) draft.updated_at = Date.now() - 7 * 60 * 60 * 1000;
            sessionStorage.setItem(key, JSON.stringify(drafts));
        }""", SCOPE)
        self.reload_ui(page, {'sessions': {'sessions': [], 'draft_scope': SCOPE}})
        self.assertEqual(page.locator('#agentPrompt').input_value(), '')
        self.assertEqual(page.evaluate("scope => Object.keys(JSON.parse(sessionStorage.getItem('mediaflux.agent.drafts.v1.' + scope))).length", SCOPE), 0)

    def test_live_account_scope_change_does_not_reuse_memory_draft(self):
        page = self.page()
        page.locator('#agentPrompt').fill('旧主体的草稿')
        page.evaluate("() => {window.__kernelConfig.sessions.draft_scope = 'b'.repeat(64);}")
        page.locator('#toggleAgentRail').click()
        page.wait_for_function("() => document.querySelector('#agentPrompt').value === ''")
        stored = page.evaluate("() => sessionStorage.getItem('mediaflux.agent.drafts.v1.' + 'b'.repeat(64))")
        self.assertNotIn('旧主体的草稿', stored or '')

    def test_history_refresh_preserves_focused_rename_editor(self):
        page = self.page({'sessions': {'draft_scope': SCOPE, 'sessions': [
            {'session_id': SESSION_A, 'title': '准备改名', 'updated_at': 20},
            {'session_id': SESSION_B, 'title': '其他会话', 'updated_at': 10},
        ]}})
        page.locator('#toggleAgentRail').click()
        page.locator(f'[data-session-rename="{SESSION_A}"]').click()
        editor = page.locator('.agent-session-editor input')
        editor.fill('还没有保存的新名称')
        page.locator('#agentSessionSearch').fill('准备')
        editor.focus()
        page.locator('#agentSessionSearch').evaluate("node => node.dispatchEvent(new Event('input', {bubbles: true}))")
        self.assertTrue(editor.evaluate('(node) => document.activeElement === node'))
        self.assertEqual(editor.input_value(), '还没有保存的新名称')

    def test_suspected_credentials_are_not_written_to_session_storage(self):
        page = self.page()
        page.locator('#agentPrompt').fill('api_key=fixture-not-a-real-secret')
        stored = page.evaluate("() => Object.keys(sessionStorage).map(key => sessionStorage.getItem(key)).join(' ')")
        self.assertNotIn('fixture-not-a-real-secret', stored)

    def test_drafts_stay_with_each_session_and_delete_clears_only_that_session(self):
        config = {
            'sessions': {'draft_scope': SCOPE, 'sessions': [
                {'session_id': SESSION_A, 'title': 'A 会话', 'updated_at': 20},
                {'session_id': SESSION_B, 'title': 'B 会话', 'updated_at': 10},
            ]},
            'sessionDetails': {SESSION_A: {'messages': []}, SESSION_B: {'messages': []}},
        }
        page = self.page(config, stored_session=SESSION_A)
        page.wait_for_function("id => window.__kernelCalls.some(call => call.url.endsWith(id))", arg=SESSION_A)
        page.locator('#agentPrompt').fill('A 的草稿')
        page.locator('#toggleAgentRail').click()
        page.locator(f'[data-session-open="{SESSION_B}"]').click()
        page.wait_for_function("() => !document.querySelector('#agentHistoryRail').open")
        self.assertEqual(page.locator('#agentPrompt').input_value(), '')
        page.locator('#agentPrompt').fill('B 的草稿')
        page.locator('#toggleAgentRail').click()
        page.locator(f'[data-session-open="{SESSION_A}"]').click()
        page.wait_for_function("() => !document.querySelector('#agentHistoryRail').open")
        self.assertEqual(page.locator('#agentPrompt').input_value(), 'A 的草稿')
        page.locator('#toggleAgentRail').click()
        page.locator(f'[data-session-delete="{SESSION_A}"]').click()
        page.wait_for_function("() => !document.querySelector('#agentHistoryRail').open")
        drafts = page.evaluate("scope => JSON.parse(sessionStorage.getItem('mediaflux.agent.drafts.v1.' + scope))", SCOPE)
        self.assertNotIn(SESSION_A, drafts)
        self.assertEqual(drafts[SESSION_B]['text'], 'B 的草稿')

    def test_completion_and_error_do_not_pull_reader_to_bottom(self):
        for outcome in ('turn.completed', 'turn.failed'):
            with self.subTest(outcome=outcome):
                long_answer = '\n\n'.join(f'第 {i} 条查询记录，需要保留阅读位置。' for i in range(80))
                events = [harness._event(1, 'turn.started'), harness._event(2, 'model.delta', {'delta': long_answer}),
                          harness._event(3, outcome, {'status': 'success' if outcome == 'turn.completed' else 'failed', 'answer': long_answer, 'message': '查询暂不可用'})]
                page = self.page({'queryEvents': events, 'queryDelayMs': 500,
                                  'sessionDetails': {SESSION_A: {'messages': [{'role': 'assistant', 'content': long_answer}]}}},
                                 stored_session=SESSION_A)
                page.wait_for_selector('.agent-narrative')
                page.locator('#agentPrompt').fill('读取长记录')
                page.locator('#agentSend').click()
                page.wait_for_function("() => document.querySelector('#agentTranscript').scrollHeight > 2000")
                page.evaluate("() => { const node = document.querySelector('#agentTranscript'); node.scrollTop = 0; node.dispatchEvent(new Event('scroll')); }")
                page.wait_for_function("() => document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
                self.assertLess(page.locator('#agentTranscript').evaluate('(node) => node.scrollTop'), 10)
                self.assertTrue(page.locator('#agentNewReplies').is_visible())
                page.locator('#agentNewReplies').click()
                page.wait_for_function("() => {const node = document.querySelector('#agentTranscript'); return node.scrollHeight - node.clientHeight - node.scrollTop < 3;}")
                self.assertTrue(page.locator('#agentNewReplies').is_hidden())

    def test_history_filter_rename_and_pin_keep_existing_conversation(self):
        page = self.page({'sessions': {'draft_scope': SCOPE, 'sessions': [
            {'session_id': SESSION_A, 'title': '旧排障记录', 'updated_at': 10, 'pinned': False},
            {'session_id': SESSION_B, 'title': '最近选片', 'updated_at': 20, 'pinned': False},
        ]}})
        page.locator('#agentPrompt').fill('正在编辑的草稿')
        page.locator('#toggleAgentRail').click()
        page.locator('#agentSessionSearch').fill('排障')
        self.assertEqual(page.locator('.agent-session-item:visible').count(), 1)
        page.locator(f'[data-session-rename="{SESSION_A}"]').click()
        editor = page.locator('.agent-session-editor')
        editor.locator('input').fill('我的排障手册')
        editor.locator('button[type=submit]').click()
        page.wait_for_selector('.agent-session-editor', state='detached')
        self.assertEqual(page.locator('.agent-session-open:visible strong').inner_text(), '我的排障手册')
        page.locator('#agentSessionSearch').fill('')
        page.locator(f'[data-session-pin="{SESSION_A}"]').click()
        page.wait_for_function("id => document.querySelector('.agent-session-item').dataset.sessionId === id", arg=SESSION_A)
        self.assertEqual(page.locator(f'[data-session-pin="{SESSION_A}"]').get_attribute('aria-pressed'), 'true')
        self.assertEqual(page.locator('#agentPrompt').input_value(), '正在编辑的草稿')
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 0)
        page.locator('#agentSessionSearch').fill('不存在的名字')
        self.assertIn('没有匹配', page.locator('.agent-session-empty').inner_text())
        self.snapshot(page, 'history-filter')

    def test_history_update_failure_keeps_editor_and_reports_error(self):
        page = self.page({'patchStatus': 409, 'sessions': {'sessions': [{'session_id': SESSION_A, 'title': '原会话'}]}})
        page.locator('#toggleAgentRail').click()
        page.locator('[data-session-rename]').click()
        page.locator('.agent-session-editor input').fill('新名称')
        page.keyboard.press('Enter')
        page.wait_for_function("() => document.querySelector('#agentSessionStatus').textContent.includes('更新失败')")
        self.assertEqual(page.locator('.agent-session-editor input').input_value(), '新名称')
        self.assertTrue(page.locator('.agent-session-editor input').evaluate('(node) => document.activeElement === node'))
        page.keyboard.press('Escape')
        self.assertTrue(page.locator('#agentHistoryRail').is_visible())
        self.assertEqual(page.locator('.agent-session-open strong').inner_text(), '原会话')

    def test_candidate_comparison_uses_safe_fields_and_only_selects_for_preview(self):
        view = candidate_view()
        coverage_notice = '部分来源未返回或尚有未读取页；结果仅覆盖已返回范围。<img src=x onerror=window.__coverage_xss=1>'
        view['coverage_notice'] = coverage_notice
        page = self.page({'queryEvents': events_for_candidates(view)})
        page.locator('#agentPrompt').fill('找第一集的版本')
        page.locator('#agentSend').click()
        page.wait_for_selector('.agent-candidate-select:not([disabled])')
        self.assertEqual(page.locator('.agent-candidate-row').count(), 2)
        self.assertFalse(page.locator('.agent-candidates-more').evaluate('(node) => node.open'))
        notice = page.locator('[data-candidate-coverage-notice]')
        self.assertEqual(notice.count(), 1)
        self.assertEqual(notice.inner_text(), coverage_notice)
        self.assertIn('确认一次', page.locator('.agent-candidates-note:not([data-candidate-coverage-notice])').inner_text())
        self.assertEqual(page.locator('.agent-candidates img').count(), 0)
        self.assertFalse(page.evaluate('Boolean(window.__ux_xss)'))
        self.assertFalse(page.evaluate('Boolean(window.__coverage_xss)'))
        page.evaluate("window.__coverageNotice = document.querySelector('[data-candidate-coverage-notice]')")
        page.locator('.agent-candidates-more > summary').click()
        self.assertIn('1080p', page.locator('.agent-candidate-info').first.inner_text())
        page.locator('.agent-candidate-detail > summary').first.click()
        self.assertIn('字幕需要人工核对', page.locator('.agent-candidate-detail').first.inner_text())
        page.locator('[data-candidate-position="2"]').check()
        page.locator('.agent-candidate-target select').select_option('both')
        self.assertEqual(notice.inner_text(), coverage_notice)
        self.assertTrue(notice.evaluate('(node) => node === window.__coverageNotice'))
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 1)
        page.locator('#agentPrompt').fill('下一句草稿保持原样')
        messages_before = page.locator('.agent-message').count()
        page.evaluate("() => {window.__kernelConfig.queryEvents = [{type: 'turn.started', event_id: 'new-selection', sequence: 1, payload: {}}]; window.__kernelConfig.holdQueryOpen = true;}")
        before = page.locator('.agent-candidate-select').bounding_box()
        page.locator('.agent-candidate-select').click()
        page.wait_for_selector('.agent-candidate-select[aria-busy="true"]')
        last = page.evaluate("JSON.parse(window.__kernelCalls.filter(call => call.url === '/api/agent/query').at(-1).body)")
        self.assertEqual(last['selection'], {'ref': view['selection_ref'], 'positions': [1, 2], 'target': 'both'})
        self.assertEqual(page.locator('#agentPrompt').input_value(), '下一句草稿保持原样')
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 0)
        self.assertTrue(page.locator('[data-candidate-position="1"]').is_disabled())
        self.assertEqual(page.locator('.agent-message').count(), messages_before)
        after = page.locator('.agent-candidate-select').bounding_box()
        assert after is not None
        assert before is not None
        self.assertEqual((before['width'], before['height']), (after['width'], after['height']))
        self.assertEqual(notice.inner_text(), coverage_notice)
        self.snapshot(page, 'candidates-desktop')

    def test_candidate_coverage_notice_is_absent_when_empty(self):
        for coverage_notice in (None, '', ' \n '):
            with self.subTest(coverage_notice=coverage_notice):
                view = candidate_view()
                if coverage_notice is not None:
                    view['coverage_notice'] = coverage_notice
                page = self.page({'queryEvents': events_for_candidates(view)})
                page.locator('#agentPrompt').fill('搜索资源')
                page.locator('#agentSend').click()
                page.wait_for_selector('.agent-candidate-select:not([disabled])')
                self.assertEqual(page.locator('[data-candidate-coverage-notice]').count(), 0)
                self.assertEqual(page.locator('.agent-candidates-note').count(), 1)
                self.assertIn('确认一次', page.locator('.agent-candidates-note').inner_text())

    def test_same_turn_search_invalidation_removes_previous_cards(self):
        for signal in ('progress', 'empty_result'):
            with self.subTest(signal=signal):
                events = events_for_candidates(candidate_view())[:2]
                events.append(harness._event(3, 'tool.progress' if signal == 'progress' else 'tool.completed',
                    {'candidate_view': None} if signal == 'progress' else {'result': {'candidate_view': None}}))
                events.append(harness._event(4, 'turn.completed', {'status': 'success', 'answer': '新搜索没有可选择的候选。'}))
                page = self.page({'queryEvents': events})
                page.locator('#agentPrompt').fill('搜索后调整条件')
                page.locator('#agentSend').click()
                page.wait_for_selector('.agent-narrative')
                self.assertEqual(page.locator('.agent-candidates').count(), 0)

    def test_same_turn_candidate_views_latest_wins_in_one_group(self):
        first = candidate_view_variant('ref_resource_candidates_same_turn_a', 'ref_selection_same_turn_a')
        latest = candidate_view_variant('ref_resource_candidates_same_turn_b', 'ref_selection_same_turn_b')
        events = [
            harness._event(1, 'turn.started'),
            harness._event(2, 'tool.completed', {'tool': 'resource.search', 'call_id': 'search-a', 'result': {'candidate_view': first}}),
            harness._event(3, 'tool.completed', {'tool': 'resource.search', 'call_id': 'search-b', 'result': {'candidate_view': latest}}),
            harness._event(4, 'turn.completed', {'status': 'success', 'answer': '本轮最终只保留最新搜索候选。'}),
        ]
        page = self.page({'queryEvents': events})
        page.locator('#agentPrompt').fill('同一轮连续搜索')
        page.locator('#agentSend').click()
        page.wait_for_selector('.agent-narrative')
        self.assertEqual(page.locator('.agent-candidates').count(), 1)
        group = page.locator('.agent-candidates').first
        self.assertEqual(group.get_attribute('data-candidate-view'), latest['ref'])
        self.assertFalse(group.locator('.agent-candidates-note').inner_text().startswith('此批候选仅供回看'))
        self.assertFalse(group.locator('[data-candidate-position]').first.is_disabled())

    def test_same_turn_candidate_view_null_removes_current_group(self):
        view = candidate_view_variant('ref_resource_candidates_same_turn_null', 'ref_selection_same_turn_null')
        events = [
            harness._event(1, 'turn.started'),
            harness._event(2, 'tool.completed', {'tool': 'resource.search', 'call_id': 'search-a', 'result': {'candidate_view': view}}),
            harness._event(3, 'tool.progress', {'tool': 'resource.search', 'candidate_view': None}),
            harness._event(4, 'turn.completed', {'status': 'success', 'answer': '本轮候选已清空。'}),
        ]
        page = self.page({'queryEvents': events})
        page.locator('#agentPrompt').fill('清空本轮候选')
        page.locator('#agentSend').click()
        page.wait_for_selector('.agent-narrative')
        self.assertEqual(page.locator('.agent-candidates').count(), 0)
        self.assertIn('本轮候选已清空', page.locator('.agent-narrative').inner_text())

    def test_previous_turn_candidate_is_readonly_while_latest_turn_stays_selectable(self):
        first = candidate_view_variant('ref_resource_candidates_previous_turn', 'ref_selection_previous_turn')
        latest = candidate_view_variant('ref_resource_candidates_latest_turn', 'ref_selection_latest_turn')
        page = self.page({'queryEvents': events_for_candidates(first)})
        page.locator('#agentPrompt').fill('第一轮搜索')
        page.locator('#agentSend').click()
        page.wait_for_selector('.agent-candidates')

        page.evaluate('(events) => { window.__kernelConfig.queryEvents = events; }', events_for_candidates(latest))
        page.locator('#agentPrompt').fill('第二轮搜索')
        page.locator('#agentSend').click()
        page.wait_for_function("document.querySelectorAll('.agent-candidates').length === 2")

        groups = page.locator('.agent-candidates')
        self.assertEqual(groups.nth(0).get_attribute('data-candidate-view'), first['ref'])
        self.assertEqual(groups.nth(1).get_attribute('data-candidate-view'), latest['ref'])
        self.assertTrue(groups.nth(0).locator('[data-candidate-position]').first.is_disabled())
        self.assertFalse(groups.nth(1).locator('[data-candidate-position]').first.is_disabled())

    def test_rejected_selection_keeps_existing_approval_and_never_offers_unbound_replay(self):
        view = candidate_view()
        approval = {'plan_id': 'plan-already-pending-00001', 'tool_name': 'ingest.submit', 'effect': 'WRITE',
                    'preview': {'summary': '已有待确认计划'}, 'result': {}, 'expires_at': time.time() + 900}
        page = self.page({'sessionDetails': {SESSION_A: {'messages': [{'role': 'assistant', 'content': '本次搜索结果', 'candidate_view': view}], 'candidate_view': view, 'pending_approval': approval}},
                          'queryEvents': [harness._event(1, 'turn.failed', {'code': 'selection_invalid', 'message': '候选已更新，请重新搜索后选择'})]},
                         stored_session=SESSION_A)
        page.wait_for_selector('.agent-candidate-select:not([disabled])')
        page.locator('.agent-candidate-select').first.click()
        page.wait_for_function("() => document.querySelector('.agent-candidate-output').textContent.includes('候选已更新')")
        self.assertTrue(page.locator('[data-effect-confirm]').is_enabled())
        self.assertEqual(page.locator('.agent-confirmation-card.is-expired').count(), 0)
        self.assertEqual(page.locator('.agent-retry-draft').count(), 0)
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 0)

    def test_expired_candidates_are_readonly_and_mobile_stays_inside_viewport(self):
        view = candidate_view(expires_at=time.time() - 5)
        coverage_notice = '部分来源未返回或尚有未读取页；结果仅覆盖已返回范围。'
        view['coverage_notice'] = coverage_notice
        page = self.page({'queryEvents': events_for_candidates(view)}, viewport={'width': 390, 'height': 844})
        page.locator('#agentPrompt').fill('显示旧的搜索结果')
        page.locator('#agentSend').click()
        page.wait_for_selector('.agent-candidate-row', state='attached')
        page.wait_for_function("() => document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
        self.assertTrue(page.locator('.agent-candidate-select').first.is_disabled())
        self.assertEqual(page.locator('[data-candidate-coverage-notice]').inner_text(), coverage_notice)
        self.assertIn('仅供回看', page.locator('.agent-candidates-note:not([data-candidate-coverage-notice])').inner_text())
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
        self.snapshot(page, 'candidates-mobile')

    def test_running_preview_does_not_claim_pending_approval_was_confirmed(self):
        payload = session_snapshot(SESSION_A)
        payload.update(
            messages=[{'role': 'user', 'content': '准备计划'}],
            active_turn={'request_id': 'preview-request', 'turn_id': 'preview-turn', 'generation': 1,
                         'protected': False, 'status': 'running', 'detail': '正在完成预览'},
            pending_approval={'plan_id': 'plan_preview_not_confirmed_001', 'effect': 'WRITE',
                              'preview': {'summary': '待确认操作'}, 'confirmation': {}},
        )
        page = self.page({'sessions': {'draft_scope': SCOPE, 'sessions': []}, 'sessionDetails': {SESSION_A: payload}}, stored_session=SESSION_A)
        page.locator('.agent-confirmation-card').wait_for()
        self.assertIn('尚未执行', page.locator('.agent-confirmation-preflight').inner_text())
        self.assertNotIn('已确认，正在等待', page.locator('.agent-confirmation-card').inner_text())
        self.assertTrue(page.locator('[data-effect-confirm]').is_disabled())
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.method === 'POST').length"), 0)

    def test_running_restore_polls_without_rebuilding_nodes_or_resubmitting(self):
        current = {
            'request_id': 'active-request', 'turn_id': 'active-turn', 'generation': 2,
            'protected': False, 'status': 'running', 'detail': '正在执行后台测试',
        }
        payload = session_snapshot(SESSION_A)
        payload.update(generation=2, messages=[{'role': 'user', 'content': '原请求'}], active_turn=current)
        page = self.page({'sessions': {'draft_scope': SCOPE, 'sessions': []}, 'sessionDetails': {SESSION_A: payload}}, stored_session=SESSION_A)
        page.get_by_text('正在执行后台测试', exact=True).wait_for()
        page.evaluate("window.__resumeNode = document.querySelector('.agent-message-assistant')")
        page.locator('#agentPrompt').fill('保留未发送草稿')
        before = page.locator('#agentComposer').bounding_box()
        page.wait_for_timeout(3700)
        self.assertTrue(page.evaluate("window.__resumeNode === document.querySelector('.agent-message-assistant')"))
        assert before is not None
        bounding_box_result = page.locator('#agentComposer').bounding_box()
        assert bounding_box_result is not None
        self.assertLessEqual(abs(bounding_box_result['y'] - before['y']), 0.5)
        page.evaluate("""({id, turn}) => { window.__kernelConfig.sessionDetails[id] = {
            generation: 2, messages: [{role: 'user', content: '原请求'}, {role: 'assistant', content: '恢复后仅一次完成'}],
            active_turn: null, last_turn: {...turn, status: 'completed', message: '完成'},
        }; }""", {'id': SESSION_A, 'turn': current})
        page.get_by_text('恢复后仅一次完成', exact=True).wait_for(timeout=10000)
        self.assertEqual(page.locator('.agent-message-assistant').count(), 1)
        self.assertEqual(page.locator('#agentPrompt').input_value(), '保留未发送草稿')
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.method === 'POST').length"), 0)

    def test_running_journal_preserves_body_without_replaying_snapshots_or_resubmitting(self):
        for enabled, width in ((True, 1280), (False, 390)):
            with self.subTest(stream_display=enabled, width=width):
                current = active_turn('journal-request', 'journal-turn', detail='正在整理回复')
                rows = [
                    ('turn.started', {'stream_display_enabled': enabled}),
                    ('model.started', {'round': 1}),
                    ('model.tool_call', {'call_id': 'original-call', 'label': '工作区代码执行'}),
                    ('tool.completed', {'call_id': 'original-call', 'label': '工作区代码执行',
                                        'result': {'summary': '命令已退出（退出码 0）'}}),
                    ('model.started', {'round': 2}),
                    ('model.delta', {'round': 2, 'delta': '已收到 marker'}),
                    ('tool.progress', {'activity': {'id': 'sdk-journal', 'kind': 'tool', 'label': '查询媒体库',
                        'method': 'library.search', 'status': 'running', 'summary': '正在核对媒体记录。'}}),
                    ('model.reasoning_summary.delta', {'round': 2, 'summary_id': 'journal', 'delta': '公开摘要 marker'}),
                ]
                current['events'] = [{**harness._event(index, kind, data),
                    'request_id': current['request_id'], 'turn_id': current['turn_id']}
                    for index, (kind, data) in enumerate(rows, 1)]
                payload = session_snapshot(SESSION_A, messages=[{'role': 'user', 'content': '原用户请求'}], active_turn=current)
                page = self.page({'sessionDetails': {SESSION_A: payload}}, stored_session=SESSION_A,
                                 viewport={'width': width, 'height': 844})
                page.locator('.agent-stream-head').wait_for()
                self.assertEqual(page.locator('.agent-stream-text').inner_text(), '已收到 marker' if enabled else '')
                self.assertNotIn('命令已退出', page.locator('#agentTranscript').inner_text())
                self.assertNotIn('退出码', page.locator('#agentTranscript').inner_text())
                toggle = page.locator('.agent-process-toggle')
                self.assertEqual(toggle.get_attribute('aria-expanded'), 'false')
                toggle.click()
                self.assertEqual(page.locator('.agent-process-item').count(), 2)
                page.evaluate("window.__activeCard = document.querySelector('.agent-result-card')")
                before = page.locator('.agent-result-card').bounding_box()
                self.assertIsNotNone(before)
                page.wait_for_function("id => window.__kernelCalls.filter(c => c.url === '/api/agent/sessions/'+id).length >= 2", arg=SESSION_A)
                self.assertTrue(page.evaluate("window.__activeCard === document.querySelector('.agent-result-card')"))
                self.assertEqual(toggle.get_attribute('aria-expanded'), 'true')
                self.assertEqual(page.locator('.agent-process-item[data-kind="reasoning"] .agent-activity-summary').inner_text(), '公开摘要 marker')
                self.assertEqual(page.locator('.agent-stream-text').inner_text(), '已收到 marker' if enabled else '')
                after = page.locator('.agent-result-card').bounding_box()
                assert before is not None and after is not None
                self.assertLessEqual(abs(after['height'] - before['height']), 0.5)
                page.evaluate("""id => {
                    const current = window.__kernelConfig.sessionDetails[id].active_turn;
                    current.events.push({...current.events[3], event_id:'late-observation', sequence:3,
                        payload:{call_id:'late-call',label:'网络查询',result:{summary:'已完成的迟到观察'}}});
                    current.events.push({...current.events[5], event_id:'new-text', sequence:8,
                        payload:{round:2,delta:' 继续'}});
                    current.events.push({...current.events[5], event_id:'foreign-text', sequence:9,
                        turn_id:'another-turn',payload:{round:2,delta:'不应串入的文字'}});
                }""", SESSION_A)
                reads = page.evaluate("id => window.__kernelCalls.filter(c => c.url === '/api/agent/sessions/'+id).length", SESSION_A)
                page.wait_for_function("({id, reads}) => window.__kernelCalls.filter(c => c.url === '/api/agent/sessions/'+id).length > reads",
                                       arg={'id': SESSION_A, 'reads': reads})
                if enabled:
                    page.get_by_text('已收到 marker 继续', exact=True).wait_for()
                self.assertEqual(page.locator('.agent-stream-text').inner_text(), '已收到 marker 继续' if enabled else '')
                self.assertNotIn('不应串入的文字', page.locator('#agentTranscript').inner_text())
                self.assertEqual(page.locator('.agent-result-card').count(), 1)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.method === 'POST').length"), 0)
                # 只读恢复进入终态时从持久字段重建，保留展开选择与键盘焦点。
                toggle.focus()
                page.evaluate("""({id, current}) => { window.__kernelConfig.sessionDetails[id] = {
                    messages: [{role: 'user', content: '原用户请求'}, {role: 'assistant', content: '持久的最终回复', activities: [
                        {id: 'sdk-journal', kind: 'tool', label: '查询媒体库', method: 'library.search', status: 'success', summary: '已核对媒体记录。'},
                        {id: 'reasoning:2:journal', kind: 'reasoning', text: '公开摘要 marker'},
                    ]}], active_turn: null, last_turn: {...current, status: 'completed'},
                }; }""", {'id': SESSION_A, 'current': current})
                page.get_by_text('持久的最终回复', exact=True).wait_for()
                self.assertEqual(toggle.get_attribute('aria-expanded'), 'true')
                self.assertTrue(toggle.evaluate('node => node === document.activeElement'))
                self.assertTrue(page.locator('.agent-stream-head').is_visible())
                self.assertEqual(page.locator('time.agent-stream-elapsed:visible').count(), 0)
                self.assertEqual(page.locator('.agent-process-item').count(), 2)
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.method === 'POST').length"), 0)

    def test_pending_receipt_restore_is_read_only_and_preserves_draft_and_scroll(self):
        messages = []
        for index in range(28):
            messages.extend((
                {'role': 'user', 'content': f'之前的问题 {index}'},
                {'role': 'assistant', 'content': f'之前的回复 {index}：' + '保留历史内容。' * 8},
            ))
        initial = session_snapshot(
            SESSION_A,
            messages=messages,
            last_turn={
                'request_id': 'receipt-request', 'turn_id': 'receipt-turn',
                'status': 'completed', 'message': '本轮已完成',
            },
            pending_effect_count=1,
        )
        page = self.page({
            'sessions': {'draft_scope': SCOPE, 'sessions': [{'session_id': SESSION_A, 'title': '后台回执'}]},
            'sessionDetails': {SESSION_A: initial},
        }, stored_session=SESSION_A)
        session_path = f'/api/agent/sessions/{SESSION_A}'
        page.wait_for_function(
            "(path) => window.__kernelCalls.some(call => call.url === path)",
            arg=session_path,
        )
        self.assertTrue(page.locator('#agentPrompt').is_enabled())
        self.assertTrue(page.locator('#agentSend').get_attribute("aria-busy") == "false")

        page.locator('#agentPrompt').fill('观察期间保留的草稿')
        before = page.evaluate("""() => {
            const transcript = document.querySelector('#agentTranscript');
            transcript.scrollTop = 120;
            transcript.dispatchEvent(new Event('scroll'));
            return transcript.scrollTop;
        }""")
        self.assertGreater(before, 0)
        page.evaluate("""({id, path}) => {
            const current = window.__kernelConfig.sessionDetails[id];
            window.__kernelConfig.sessionDetails[id] = {
                ...current,
                pending_effect_count: 0,
                messages: [...current.messages, {role: 'assistant', content: '后台晚到回执已保存。'}],
            };
        }""", {'id': SESSION_A, 'path': session_path})

        page.get_by_text('后台晚到回执已保存。', exact=True).wait_for(timeout=15000)
        after = page.evaluate("document.querySelector('#agentTranscript').scrollTop")
        self.assertAlmostEqual(after, before, delta=1)
        self.assertEqual(page.locator('#agentPrompt').input_value(), '观察期间保留的草稿')
        self.assertTrue(page.locator('#agentPrompt').is_enabled())
        self.assertTrue(page.locator('#agentSend').get_attribute("aria-busy") == "false")
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query' && call.method === 'POST').length"), 0)

    def test_receipt_observation_pauses_while_hidden_and_reloads_on_focus(self):
        initial = session_snapshot(
            SESSION_A,
            messages=[{'role': 'assistant', 'content': '等待后台回执'}],
            last_turn={
                'request_id': 'receipt-hidden-request', 'turn_id': 'receipt-hidden-turn',
                'status': 'completed', 'message': '本轮已完成',
            },
            pending_effect_count=1,
        )
        page = self.page({
            'sessions': {'draft_scope': SCOPE, 'sessions': [{'session_id': SESSION_A, 'title': '后台回执'}]},
            'sessionDetails': {SESSION_A: initial},
        }, stored_session=SESSION_A)
        session_path = f'/api/agent/sessions/{SESSION_A}'
        page.get_by_text('等待后台回执', exact=True).wait_for()
        page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable: true, value: true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        page.evaluate("""id => {
            const current = window.__kernelConfig.sessionDetails[id];
            window.__kernelConfig.sessionDetails[id] = {
                ...current,
                pending_effect_count: 0,
                messages: [...current.messages, {role: 'assistant', content: '隐藏期间完成的回执'}],
            };
        }""", SESSION_A)
        page.wait_for_timeout(5300)
        detail_reads = page.evaluate("(path) => window.__kernelCalls.filter(call => call.url === path).length", session_path)
        self.assertEqual(detail_reads, 1)

        page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable: true, value: false});
            document.dispatchEvent(new Event('visibilitychange'));
            window.dispatchEvent(new Event('focus'));
        }""")
        page.get_by_text('隐藏期间完成的回执', exact=True).wait_for(timeout=5000)
        detail_reads = page.evaluate("(path) => window.__kernelCalls.filter(call => call.url === path).length", session_path)
        self.assertEqual(detail_reads, 2)
        self.assertTrue(page.locator('#agentPrompt').is_enabled())
        self.assertTrue(page.locator('#agentSend').get_attribute("aria-busy") == "false")

    def test_switching_sessions_stops_the_previous_receipt_poll(self):
        page = self.page({
            'sessions': {'draft_scope': SCOPE, 'sessions': [
                {'session_id': SESSION_A, 'title': '待回执会话'},
                {'session_id': SESSION_B, 'title': '另一个会话'},
            ]},
            'sessionDetails': {
                SESSION_A: session_snapshot(SESSION_A, messages=[{'role': 'assistant', 'content': 'A 等待回执'}], pending_effect_count=1),
                SESSION_B: session_snapshot(SESSION_B, messages=[{'role': 'assistant', 'content': 'B 会话内容'}]),
            },
        }, stored_session=SESSION_A)
        page.get_by_text('A 等待回执', exact=True).wait_for()
        page.locator('#toggleAgentRail').click()
        page.locator(f'[data-session-open="{SESSION_B}"]').click()
        page.get_by_text('B 会话内容', exact=True).wait_for()
        page.wait_for_timeout(5300)

        self.assertEqual(page.evaluate("id => window.__kernelCalls.filter(call => call.url === `/api/agent/sessions/${id}`).length", SESSION_A), 1)
        self.assertEqual(page.evaluate("id => window.__kernelCalls.filter(call => call.url === `/api/agent/sessions/${id}`).length", SESSION_B), 1)

    def test_new_foreground_turn_restarts_an_expired_receipt_observation_window(self):
        initial = session_snapshot(
            SESSION_A,
            messages=[{'role': 'assistant', 'content': '旧后台任务等待回执'}],
            last_turn={
                'request_id': 'old-receipt-request', 'turn_id': 'old-receipt-turn',
                'status': 'completed', 'message': '本轮已完成',
            },
            pending_effect_count=1,
        )
        page = self.page({
            'sessions': {'draft_scope': SCOPE, 'sessions': [{'session_id': SESSION_A, 'title': '后台回执'}]},
            'sessionDetails': {SESSION_A: initial},
            'queryEvents': [
                harness._event(1, 'turn.started'),
                harness._event(2, 'turn.completed', {'status': 'success', 'answer': '新任务已完成。'}),
            ],
        }, stored_session=SESSION_A)
        session_path = f'/api/agent/sessions/{SESSION_A}'
        page.get_by_text('旧后台任务等待回执', exact=True).wait_for()
        page.evaluate("""({id, elapsed}) => {
            const current = window.__kernelConfig.sessionDetails[id];
            window.__kernelConfig.sessionDetails[id] = {
                ...current,
                pending_effect_count: 1,
                messages: [
                    ...current.messages,
                    {role: 'user', content: '启动新的后台任务'},
                    {role: 'assistant', content: '新任务已完成。'},
                    {role: 'assistant', content: '新后台任务晚到回执'},
                ],
            };
            const originalNow = Date.now;
            Date.now = () => originalNow() + elapsed;
        }""", {'id': SESSION_A, 'elapsed': 24 * 60 * 60 * 1000 + 1})

        page.locator('#agentPrompt').fill('启动新的后台任务')
        page.locator('#agentSend').click()
        page.get_by_text('新后台任务晚到回执', exact=True).wait_for(timeout=7000)
        page.get_by_text('新任务已完成。', exact=True).wait_for()

        self.assertEqual(
            page.evaluate("path => window.__kernelCalls.filter(call => call.url === path).length", session_path),
            2,
        )

    def test_late_receipt_cannot_replace_an_in_flight_new_request(self):
        initial = session_snapshot(
            SESSION_A,
            messages=[{'role': 'assistant', 'content': '已有历史回复'}],
            last_turn={
                'request_id': 'receipt-request', 'turn_id': 'receipt-turn',
                'status': 'completed', 'message': '本轮已完成',
            },
            pending_effect_count=1,
        )
        page = self.page({
            'sessions': {'draft_scope': SCOPE, 'sessions': [{'session_id': SESSION_A, 'title': '后台回执'}]},
            'sessionDetails': {SESSION_A: initial},
            'holdQueryOpen': True,
        }, stored_session=SESSION_A)
        page.wait_for_function(
            "(path) => window.__kernelCalls.some(call => call.url === path)",
            arg=f'/api/agent/sessions/{SESSION_A}',
        )
        page.evaluate("""(path) => {
            const originalFetch = window.fetch;
            window.__holdReceiptRead = true;
            window.fetch = async (url, options = {}) => {
                if (window.__holdReceiptRead && new URL(String(url), location.href).pathname === path) {
                    window.__holdReceiptRead = false;
                    window.__receiptReadStarted = true;
                    await new Promise(resolve => { window.__releaseReceiptRead = resolve; });
                }
                return originalFetch(url, options);
            };
        }""", f'/api/agent/sessions/{SESSION_A}')
        page.wait_for_function('window.__receiptReadStarted === true', timeout=15000)
        page.evaluate("""id => {
            const current = window.__kernelConfig.sessionDetails[id];
            window.__kernelConfig.sessionDetails[id] = {
                ...current,
                pending_effect_count: 0,
                messages: [...current.messages, {role: 'assistant', content: '不应覆盖新请求的晚到回执。'}],
            };
            window.__kernelConfig.queryEvents = [{
                event_id: 'new-turn-started', type: 'turn.started', sequence: 1,
                session_id: id, turn_id: 'new-turn', request_id: 'new-request', payload: {},
            }];
        }""", SESSION_A)

        page.locator('#agentPrompt').fill('发起新请求')
        page.locator('#agentSend').click()
        page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'true'")
        page.locator('#agentPrompt').fill('新请求期间保留的草稿')
        page.evaluate('window.__releaseReceiptRead()')
        page.wait_for_timeout(100)

        self.assertIn('发起新请求', page.locator('#agentTranscript').inner_text())
        self.assertNotIn('不应覆盖新请求的晚到回执。', page.locator('#agentTranscript').inner_text())
        self.assertEqual(page.locator('#agentPrompt').input_value(), '新请求期间保留的草稿')
        self.assertTrue(page.locator('#agentStopSession').is_visible())
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query' && call.method === 'POST').length"), 1)

    def test_accepted_cancel_waits_for_verified_terminal_instead_of_aborting_ui(self):
        current = {
            'request_id': 'stop-request', 'turn_id': 'stop-turn', 'generation': 3,
            'protected': False, 'status': 'running', 'detail': '正在查询',
        }
        payload = session_snapshot(SESSION_A)
        payload.update(generation=3, messages=[{'role': 'user', 'content': '等待停止'}], active_turn=current)
        page = self.page({'cancelPending': True, 'sessions': {'draft_scope': SCOPE, 'sessions': []}, 'sessionDetails': {SESSION_A: payload}}, stored_session=SESSION_A)
        page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'true'")
        page.locator('#agentStopSession').click()
        page.wait_for_timeout(2200)
        self.assertNotIn('请求已停止', page.locator('#agentResponseStatus').inner_text())
        self.assertIn('停止', page.locator('#agentResponseStatus').inner_text())
        page.evaluate("""({id, turn}) => { window.__kernelConfig.sessionDetails[id] = {
            generation: 3, messages: [{role: 'user', content: '等待停止'}], active_turn: null,
            last_turn: {...turn, status: 'cancelled', message: '已核实取消'},
        }; }""", {'id': SESSION_A, 'turn': current})
        page.get_by_text('已核实取消', exact=True).wait_for(timeout=10000)
        self.assertTrue(page.locator('#agentSend').get_attribute("aria-busy") == "false")
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.url === '/api/agent/session/stop').length"), 1)
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.url === '/api/agent/query').length"), 0)

    def test_failed_terminal_turn_is_restored_after_refresh(self):
        sessions = {'sessions': [{'session_id': SESSION_A, 'title': '失败任务历史'}], 'draft_scope': SCOPE}
        initial = {'sessions': sessions, 'sessionDetails': {SESSION_A: session_snapshot(SESSION_A)}}
        page = self.page(initial, stored_session=SESSION_A)
        for message, expected in (
            ('受控任务失败，状态已同步。', '受控任务失败，状态已同步。'),
            ('本轮未能完成', '当前会话摘要未提供具体失败原因。可查看日志定位；涉及文件变更时，请先核对结果。'),
        ):
            with self.subTest(message=message):
                failed = session_snapshot(
                    SESSION_A,
                    messages=[{'role': 'user', 'content': '执行受控失败任务'}],
                    last_turn={
                        'request_id': 'rq-refresh-failed-0001',
                        'turn_id': 'turn-rq-refresh-failed-0001',
                        'status': 'failed',
                        'message': message,
                    },
                )
                self.reload_ui(page, {'sessions': sessions, 'sessionDetails': {SESSION_A: failed}})
                page.wait_for_function("text => document.querySelector('.agent-result-card.is-interrupted .agent-stream-text')?.textContent.includes(text)", arg=expected)
                self.assertEqual(page.locator('.agent-result-card.is-interrupted .agent-stream-text').inner_text(), expected)
                self.assertEqual(page.locator('.agent-result-card.is-interrupted .agent-stream-text').count(), 1)
                self.assertEqual(page.locator('#agentTranscript .agent-message-user').count(), 1)
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 0)
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 0)

    def test_resume_nonempty_history_preserves_focused_resume_control(self):
        page = self.page({'sessions': {'sessions': [{'session_id': SESSION_A, 'title': '已保存的历史'}]},
                          'sessionDetails': {SESSION_A: {'messages': [{
                              'role': 'assistant', 'content': '之前已经完成的排障记录。',
                              'tools': ['agent.run_code', 'cloud.list'],
                              'tool_labels': ['工作区代码执行', '光鸭文件查询'],
                          }]}}})
        page.locator('#agentResumeLatestSession').click()
        page.wait_for_selector('.agent-narrative')
        self.assertTrue(page.locator('#agentResumeLatestSession').evaluate('(node) => document.activeElement === node'))
        self.assertTrue(page.locator('#agentComposer #agentResumeLatestSession').is_visible())
        self.assertEqual(page.locator('.agent-narrative').inner_text(), '之前已经完成的排障记录。')
        self.assertEqual(page.locator('time.agent-stream-elapsed').count(), 0)
        for tool in ('agent.run_code', 'cloud.list', '工作区代码执行', '光鸭文件查询'):
            self.assertNotIn(tool, page.locator('#agentTranscript').inner_text())
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.method === 'POST').length"), 0)

    def test_candidate_preview_message_never_splits_an_emoji_surrogate_pair(self):
        view = candidate_view()
        view['items'][0]['title'] = '观' * 159 + '🎬' + '测试版本'
        page = self.page({'queryEvents': events_for_candidates(view)})
        page.locator('#agentPrompt').fill('搜索一个长标题')
        page.locator('#agentSend').click()
        page.wait_for_selector('.agent-candidate-select:not([disabled])')
        page.locator('.agent-candidate-select').first.click()
        page.wait_for_function("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length === 2")
        payload = page.evaluate("JSON.parse(window.__kernelCalls.filter(call => call.url === '/api/agent/query').at(-1).body)")
        payload['message'].encode('utf-8', errors='strict')
        self.assertEqual(payload['selection'], {'ref': view['selection_ref'], 'positions': [1], 'target': 'guangya'})

    def test_verified_candidate_view_can_be_recovered_from_session(self):
        view = candidate_view()
        page = self.page({'sessionDetails': {SESSION_A: {'messages': [{'role': 'assistant', 'content': '搜索结果', 'candidate_view': view}, {'role': 'assistant', 'content': '之后的无关对话'}], 'candidate_view': view}}}, stored_session=SESSION_A)
        page.wait_for_selector('.agent-candidate-select:not([disabled])')
        self.assertEqual(page.locator('.agent-candidate-row').count(), 2)
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 0)

    def test_batch_preview_and_results_stay_in_search_card(self):
        view = candidate_view()
        events = [harness._event(1, 'turn.started'), harness._event(2, 'effect.approval_required', {
            'plan': {'plan_id': 'plan_batch_browser_00001', 'effect': 'WRITE', 'preview': {'data': {'source_type': 'resource_candidates', 'count': 2, 'target': 'both', 'resources': [{'position': 1, 'title': '第一集'}, {'position': 2, 'title': '第二集'}], 'receiving_folders': ['光鸭：接收目录 / 每任务隔离目录']}}, 'confirmation': {}}, 'result': {}}),
            harness._event(3, 'turn.completed', {'status': 'approval_required'})]
        for viewport in ({'width': 1280, 'height': 800}, {'width': 390, 'height': 844}):
            with self.subTest(viewport=viewport):
                page = self.page({'queryEvents': events_for_candidates(view)}, viewport=viewport)
                page.locator('#agentPrompt').fill('搜索两个版本')
                page.locator('#agentSend').click()
                page.wait_for_selector('.agent-candidate-select:not([disabled])')
                page.locator('.agent-candidates-more > summary').click()
                page.locator('[data-candidate-position="2"]').check()
                page.evaluate('(events) => {window.__kernelConfig.queryEvents = events}', events)
                page.locator('.agent-candidate-select').click()
                page.wait_for_selector('.agent-candidates [data-effect-confirm]')
                self.assertEqual(page.locator('.agent-candidates').count(), 1)
                self.assertIn('接收目录', page.locator('.agent-candidates .agent-confirmation-card').inner_text())
                page.locator('[data-effect-cancel]').click()
                page.wait_for_selector('.agent-candidates .agent-result-card')
                self.assertTrue(page.locator('.agent-candidate-select').is_enabled())
                self.assertTrue(page.locator('[data-candidate-position="2"]').is_checked())
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), viewport['width'])
                self.snapshot(page, 'batch-preview-' + str(viewport['width']))

    def test_frozen_twenty_resources_expand_without_candidate_view_and_cancel_once(self):
        resources = [
            {'position': index, 'title': f'冻结剧集 {index:02d}', 'site_name': '资源站 A'}
            for index in range(1, 21)
        ]
        resources[-1]['title'] += ' FrozenEpisode20' + 'LongTitle' * 30
        plan_id = 'plan_frozen_twenty_00001'
        events = [
            harness._event(1, 'turn.started'),
            harness._event(2, 'effect.approval_required', {
                'tool': 'ingest.submit',
                'plan': {
                    'plan_id': plan_id, 'effect': 'WRITE',
                    'preview': {'data': {'count': 20, 'target': 'qb', 'resources': resources}},
                    'confirmation': {},
                },
            }),
            harness._event(3, 'turn.completed', {'status': 'approval_required'}),
        ]
        expected = [f"#{item['position']} · {item['title']} · {item['site_name']}" for item in resources]
        for viewport in ({'width': 1280, 'height': 800}, {'width': 390, 'height': 844}):
            with self.subTest(viewport=viewport):
                page = self.page({'queryEvents': events}, viewport=viewport)
                page.locator('#agentPrompt').fill('提交这 20 项，先让我核对冻结清单')
                page.locator('#agentSend').click()
                card = page.locator('.agent-confirmation-card')
                card.wait_for()
                scope = card.locator('.agent-confirmation-scope')
                self.assertEqual(card.count(), 1)
                self.assertEqual(page.locator('.agent-candidates').count(), 0)
                self.assertEqual(scope.locator('li').count(), 20)
                self.assertEqual(scope.locator(':scope > ul > li').all_text_contents(), expected[:8])
                more = scope.locator('details')
                self.assertEqual(more.count(), 1)
                self.assertEqual(more.locator('li').all_text_contents(), expected[8:])
                self.assertFalse(more.evaluate('node => node.open'))
                self.assertTrue(all(item.is_visible() for item in scope.locator(':scope > ul > li').all()))
                self.assertTrue(all(not item.is_visible() for item in more.locator('li').all()))
                summary = more.locator('summary')
                summary_text = summary.inner_text()
                self.assertIn('另有 12 项', summary_text)
                summary_box = summary.bounding_box()
                collapsed_box = card.bounding_box()
                assert summary_box is not None
                self.assertGreaterEqual(summary_box['height'], 44)

                summary.focus()
                summary.press('Enter')
                self.assertTrue(more.evaluate('node => node.open'))
                self.assertNotEqual(summary.evaluate('node => getComputedStyle(node).outlineStyle'), 'none')
                self.assertEqual(scope.locator('li').all_text_contents(), expected)
                self.assertTrue(all(item.is_visible() for item in scope.locator('li').all()))
                self.assertEqual(summary.inner_text(), summary_text)
                expanded_summary_box = summary.bounding_box()
                for dimension in ('width', 'height'):
                    assert expanded_summary_box is not None
                    assert summary_box is not None
                    self.assertLessEqual(abs(expanded_summary_box[dimension] - summary_box[dimension]), 0.5)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), viewport['width'])

                summary.press('Space')
                self.assertFalse(more.evaluate('node => node.open'))
                summary.click()
                self.assertTrue(more.evaluate('node => node.open'))
                summary.click()
                self.assertFalse(more.evaluate('node => node.open'))
                self.assertEqual(summary.inner_text(), summary_text)
                restored_box = card.bounding_box()
                for dimension in ('width', 'height'):
                    assert collapsed_box is not None
                    assert restored_box is not None
                    self.assertLessEqual(abs(restored_box[dimension] - collapsed_box[dimension]), 0.5)
                self.assertEqual(scope.locator('li').count(), 20)
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.method === 'POST').map(call => call.url)"), ['/api/agent/query'])

                # 快速重复激活取消：第一次须同步禁用按钮，不能产生第二次写请求。
                card.locator('[data-effect-cancel]').evaluate('button => { button.click(); button.click(); }')
                page.wait_for_selector('.agent-result-card')
                self.assertIn('本次计划已取消，没有执行任何写操作。', page.locator('.agent-result-card').inner_text())
                self.assertEqual(page.locator('.agent-confirmation-card').count(), 0)
                calls = page.evaluate("window.__kernelCalls.filter(call => call.method === 'POST')")
                self.assertEqual([call['url'] for call in calls], ['/api/agent/query', '/api/agent/actions/confirm/discard'])
                self.assertEqual(json.loads(calls[-1]['body'])['plan_id'], plan_id)

    def test_small_frozen_resource_lists_have_no_empty_disclosure(self):
        for count in (0, 1, 8):
            with self.subTest(count=count):
                resources = [{'position': index, 'title': f'冻结剧集 {index:02d}'} for index in range(1, count + 1)]
                data: dict = {'count': count, 'target': 'qb'}
                data.update({'resource': resources[0]} if count == 1 else {'resources': resources})
                approval = {
                    'plan_id': f'plan_frozen_small_{count:05d}', 'effect': 'WRITE',
                    'preview': {'data': data}, 'confirmation': {},
                }
                page = self.page({'sessionDetails': {SESSION_A: session_snapshot(SESSION_A, pending_approval=approval)}}, stored_session=SESSION_A)
                card = page.locator('.agent-confirmation-card')
                card.wait_for()
                self.assertEqual(card.locator('.agent-confirmation-scope li').count(), count)
                self.assertEqual(card.locator('.agent-confirmation-scope details').count(), 0)
                self.assertNotIn('另有', card.inner_text())
                self.assertTrue(all(item.is_visible() for item in card.locator('.agent-confirmation-scope li').all()))
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.method === 'POST').length"), 0)

    def test_file_change_card_lists_frozen_samples_and_states_unlisted_count(self):
        samples = [f'改名：第{index}集.mkv → 测试剧 S01E{index:02d}.mkv' for index in range(1, 7)]
        for total, unlisted in ((6, None), (13, '其余 7 项未逐条列出')):
            with self.subTest(total=total):
                approval = {
                    'plan_id': f'plan_fs_samples_{total:05d}', 'effect': 'DANGER',
                    'preview': {'data': {'total': total, 'sample_changes': samples}}, 'confirmation': {},
                }
                page = self.page({'sessionDetails': {SESSION_A: session_snapshot(SESSION_A, pending_approval=approval)}}, stored_session=SESSION_A)
                card = page.locator('.agent-confirmation-card')
                card.wait_for()
                items = card.locator('.agent-confirmation-scope li').all_text_contents()
                self.assertEqual(items[:6], samples)
                self.assertEqual(items[6:], [unlisted] if unlisted else [])

    def test_confirmation_waiting_keeps_composer_slot_stable_and_shows_safe_progress(self):
        approval = {
            'plan_id': 'plan_ux_followup_0001',
            'tool_name': 'download.pause',
            'effect': 'WRITE',
            'preview': {'summary': '暂停下载任务', 'data': {'任务': '测试任务'}},
            'result': {},
            'expires_at': '2026-09-03T12:05:00+00:00',
        }
        page = self.page({
            'queryEvents': [
                harness._event(1, 'turn.started', {'kind': 'query'}),
                harness._event(2, 'effect.approval_required', {'tool': 'download.pause', 'plan': approval}),
                harness._event(3, 'turn.completed', {'status': 'approval_required'}),
            ],
            'confirmEvents': [
                harness._event(4, 'effect.completed', {
                    'plan_id': approval['plan_id'],
                    'result': {'ok': True, 'summary': '下载任务已暂停'},
                    'receipt': '下载任务已暂停。',
                }),
                harness._event(5, 'model.started', {'round': 2}),
                harness._event(6, 'tool.started', {'call_id': 'background-1', 'tool': 'guangya.job'}),
                harness._event(7, 'tool.progress', {
                    'tool': 'guangya.job',
                    'phase': 'background_job',
                    'summary': '已安全排队，等待实际状态回执',
                    'operation_ref': 'operation-ux-0001',
                }),
                harness._event(8, 'turn.completed', {'status': 'success', 'answer': '后续核验完成。'}),
            ],
            'confirmDelayMs': 120,
        }, viewport={'width': 390, 'height': 844})
        page.locator('#agentPrompt').fill('暂停任务并继续核验')
        page.locator('#agentSend').click()
        page.locator('.agent-confirmation-card').wait_for()
        before = page.locator('.agent-submit-slot').bounding_box()
        page.locator('[data-effect-confirm]').click()

        page.wait_for_function(
            "() => document.querySelector('.agent-stream-head')?.textContent.includes('已安全排队，等待实际状态回执')"
        )
        during = page.locator('.agent-submit-slot').bounding_box()
        assert before is not None
        assert during is not None
        self.assertAlmostEqual(before['width'], during['width'], delta=0.5)
        self.assertAlmostEqual(before['height'], during['height'], delta=0.5)
        self.assertTrue(page.locator('#agentStopSession').is_visible())
        self.assertTrue(page.locator('#agentSend').is_disabled())
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)

        page.locator('.agent-narrative').wait_for()
        self.assertIn('后续核验完成', page.locator('.agent-narrative').inner_text())

    def test_submitted_276_receipt_is_rendered_once_at_desktop_and_mobile_widths(self):
        approval = {
            'plan_id': 'plan_submitted_276_0001', 'tool_name': 'indexer.submit', 'effect': 'WRITE',
            'preview': {'summary': '提交已选资源'}, 'confirmation': {},
        }
        reports = []
        evidence_dir = os.getenv('MEDIAFLUX_BROWSER_EVIDENCE_DIR')
        for viewport in ({'width': 1280, 'height': 800}, {'width': 390, 'height': 844}):
            with self.subTest(viewport=viewport):
                page = self.page({
                    'queryEvents': [
                        harness._event(1, 'turn.started'),
                        harness._event(2, 'effect.approval_required', {'tool': 'indexer.submit', 'plan': approval}),
                        harness._event(3, 'turn.completed', {'status': 'approval_required'}),
                    ],
                    'confirmEvents': [
                        harness._event(4, 'effect.completed', {
                            'plan_id': approval['plan_id'],
                            'result': SUBMITTED_276_RESULT,
                            'receipt': SUBMITTED_276_RECEIPT,
                        }),
                        harness._event(5, 'turn.completed', {
                            'status': 'success', 'answer': SUBMITTED_276_RECEIPT,
                        }),
                    ],
                }, viewport=viewport)
                page.locator('#agentPrompt').fill('提交已选资源')
                page.locator('#agentSend').click()
                page.locator('.agent-confirmation-card [data-effect-confirm]').click()
                page.locator('.agent-narrative').wait_for()

                metrics = page.evaluate("""() => {
                  const text = document.querySelector('.agent-narrative')?.innerText || '';
                  return {
                    viewportWidth: innerWidth,
                    narrativeCount: document.querySelectorAll('.agent-narrative').length,
                    receiptReferenceCount: (text.match(/#276/g) || []).length,
                    narrativeText: text,
                  };
                }""")
                self.assertEqual(metrics['viewportWidth'], viewport['width'])
                self.assertEqual(metrics['narrativeCount'], 1)
                self.assertEqual(metrics['receiptReferenceCount'], 1)
                self.assertIn('后台任务尚未完成', metrics['narrativeText'])
                self.assertNotIn('✅', metrics['narrativeText'])
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 1)
                reports.append(metrics)
                if evidence_dir:
                    self.snapshot(page, f'submitted-276-{viewport["width"]}')
                page.close()

        if evidence_dir:
            output = Path(evidence_dir)
            output.mkdir(parents=True, exist_ok=True)
            (output / 'submitted-276-dom-counts.json').write_text(
                json.dumps(reports, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
            )

    def test_restored_candidate_receipt_does_not_hide_followup_facts(self):
        view = candidate_view()
        view['last_result'] = {'text': '下载请求 #276 已提交', 'target': 'guangya', 'handled_positions': [1]}
        payload = {'candidate_view': view, 'messages': [{'role': 'assistant', 'content': '找到资源', 'candidate_view': view}, {
            'role': 'assistant', 'content': '下载请求 #276 已提交\n\n还有一个目录待处理。',
            'candidate_result_ref': view['ref'], 'candidate_followup': '还有一个目录待处理。',
        }]}
        page = self.page({'sessionDetails': {SESSION_A: payload}}, stored_session=SESSION_A)
        page.locator('.agent-narrative').last.wait_for()
        self.assertEqual(page.locator('.agent-narrative').last.inner_text(), '还有一个目录待处理。')
        self.assertEqual(page.locator('#agentTranscript').inner_text().count('#276'), 1)

    def test_confirmation_continues_to_later_plan_and_preserves_real_receipts(self):
        first = {
            'plan_id': 'plan_ux_continue_0001', 'tool_name': 'download.pause', 'effect': 'WRITE',
            'preview': {'summary': '暂停下载任务'}, 'confirmation': {},
        }
        second = {
            'plan_id': 'plan_ux_continue_0002', 'tool_name': 'download.resume', 'effect': 'WRITE',
            'preview': {'summary': '继续下载任务'}, 'confirmation': {},
        }
        page = self.page({
            'queryEvents': [
                harness._event(1, 'turn.started'),
                harness._event(2, 'effect.approval_required', {'tool': 'download.pause', 'plan': first}),
                harness._event(3, 'turn.completed', {'status': 'approval_required'}),
            ],
            'confirmEvents': [
                harness._event(4, 'effect.completed', {
                    'plan_id': first['plan_id'], 'receipt': '暂停请求已受理，等待核验。',
                    'result': {'ok': True, 'status': 'submitted', 'summary': '暂停请求已受理'},
                }),
                harness._event(5, 'model.started', {'round': 2}),
                harness._event(6, 'tool.completed', {
                    'call_id': 'followup-read-1', 'tool': 'downloads.list',
                    'result': {'summary': '查询到 3 个下载任务'},
                }),
                harness._event(7, 'effect.approval_required', {'tool': 'download.resume', 'plan': second}),
                harness._event(8, 'turn.completed', {'status': 'approval_required'}),
            ],
        })
        page.locator('#agentPrompt').fill('先暂停，再查询并继续')
        page.locator('#agentSend').click()
        page.locator('[data-effect-confirm="plan_ux_continue_0001"]').click()

        next_plan = page.locator('.agent-confirmation-card[data-plan-id="plan_ux_continue_0002"]')
        next_plan.wait_for()
        self.assertTrue(next_plan.locator('[data-effect-confirm]').is_enabled())
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 1)
        self.assertEqual(next_plan.inner_text().count('暂停请求已受理'), 1)
        self.assertNotIn('上一项已完成', next_plan.inner_text())

        page.evaluate('(events) => { window.__kernelConfig.confirmEvents = events; }', [
            harness._event(9, 'effect.completed', {
                'plan_id': second['plan_id'], 'receipt': '继续请求已受理，等待核验。',
                'result': {'ok': True, 'status': 'submitted', 'summary': '继续请求已受理'},
            }),
            harness._event(10, 'turn.completed', {
                'status': 'success', 'answer': '后续真实查询发现 3 个下载任务；继续请求已受理。',
            }),
        ])
        page.locator('[data-effect-confirm="plan_ux_continue_0002"]').click()
        page.locator('.agent-narrative').wait_for()

        self.assertEqual(
            page.locator('.agent-narrative').inner_text(),
            '后续真实查询发现 3 个下载任务；继续请求已受理。',
        )
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 2)

    def test_disconnect_after_effect_event_preserves_server_receipt(self):
        approval = {
            'plan_id': 'plan_ux_disconnect_0001', 'tool_name': 'indexer.submit', 'effect': 'WRITE',
            'preview': {'summary': '提交资源'}, 'confirmation': {},
        }
        receipt = (
            '📤 下载请求 #276 已提交\n'
            '- 状态：请求已提交，后台任务尚未完成；可以继续查询进度。'
        )
        page = self.page({
            'queryEvents': [
                harness._event(1, 'turn.started'),
                harness._event(2, 'effect.approval_required', {'tool': 'indexer.submit', 'plan': approval}),
                harness._event(3, 'turn.completed', {'status': 'approval_required'}),
            ],
            'confirmEvents': [],
        })
        page.locator('#agentPrompt').fill('提交资源')
        page.locator('#agentSend').click()
        page.locator('.agent-confirmation-card').wait_for()
        page.evaluate("""({event, draftScope}) => {
          const originalFetch = window.fetch;
          const originalSetTimeout = window.setTimeout.bind(window);
          window.__uxConfirmCalls = 0;
          window.setTimeout = (callback, delay = 0, ...args) =>
            originalSetTimeout(callback, delay === 1750 ? 5 : delay, ...args);
          window.fetch = async (url, options = {}) => {
            const path = new URL(String(url), location.href).pathname;
            if (path === '/api/agent/actions/confirm') {
              window.__uxConfirmCalls += 1;
              const request = JSON.parse(options.body || '{}');
              const payload = {...event, request_id: request.request_id,
                session_id: request.session_id, turn_id: `turn-${request.request_id}`};
              return new Response(new ReadableStream({
                start(controller) {
                  controller.enqueue(new TextEncoder().encode(`${JSON.stringify(payload)}\\n`));
                  window.setTimeout(() => controller.error(new Error('simulated stream disconnect')), 10);
                },
              }), {status: 200, headers: {'Content-Type': 'application/x-ndjson'}});
            }
            if (path.startsWith('/api/agent/sessions/')) {
              return new Response(JSON.stringify({
                session_id: decodeURIComponent(path.split('/').pop()), generation: 1, messages: [],
                pending_approval: null, candidate_view: null, active_turn: null, last_turn: null,
                draft_scope: draftScope,
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            return originalFetch(url, options);
          };
        }""", {
            'event': harness._event(4, 'effect.completed', {
                'plan_id': approval['plan_id'], 'receipt': receipt,
                'result': SUBMITTED_276_RESULT,
            }),
            'draftScope': SCOPE,
        })
        page.locator('[data-effect-confirm]').click()
        page.locator('.agent-narrative').wait_for()

        text = page.locator('.agent-narrative').inner_text()
        self.assertIn('下载请求 #276 已提交', text)
        self.assertIn('后台任务尚未完成', text)
        self.assertIn('任务状态未确认', text)
        self.assertEqual(text.count('#276'), 1)
        self.assertEqual(page.evaluate('window.__uxConfirmCalls'), 1)

    def test_legacy_effect_summary_without_receipt_is_marked_unknown(self):
        approval = {
            'plan_id': 'plan_ux_legacy_receipt_0001', 'tool_name': 'indexer.submit', 'effect': 'WRITE',
            'preview': {'summary': '提交资源'}, 'confirmation': {},
        }
        page = self.page({
            'queryEvents': [
                harness._event(1, 'turn.started'),
                harness._event(2, 'effect.approval_required', {'tool': 'indexer.submit', 'plan': approval}),
                harness._event(3, 'turn.completed', {'status': 'approval_required'}),
            ],
            'confirmEvents': [
                harness._event(4, 'effect.completed', {
                    'plan_id': approval['plan_id'],
                    'result': SUBMITTED_276_RESULT,
                }),
                harness._event(5, 'turn.failed', {'message': '连接中断'}),
            ],
        })
        page.locator('#agentPrompt').fill('提交资源')
        page.locator('#agentSend').click()
        page.locator('[data-effect-confirm]').click()
        page.locator('.agent-narrative').wait_for()

        text = page.locator('.agent-narrative').inner_text()
        self.assertIn('执行状态未知', text)
        self.assertIn('服务端摘要：下载请求 #276 已提交', text)
        self.assertNotIn('✅', text)
        self.assertEqual(text.count('#276'), 1)
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 1)

    def test_confirmation_rejects_stop_while_protected_job_stream_waits(self):
        approval = {
            'plan_id': 'plan_ux_waiting_0001',
            'tool_name': 'download.pause',
            'effect': 'WRITE',
            'preview': {'summary': '暂停下载任务', 'data': {'任务': '测试任务'}},
            'result': {},
            'expires_at': '2026-09-03T12:05:00+00:00',
        }
        page = self.page({
            'queryEvents': [
                harness._event(1, 'turn.started', {'kind': 'query'}),
                harness._event(2, 'effect.approval_required', {'tool': 'download.pause', 'plan': approval}),
                harness._event(3, 'turn.completed', {'status': 'approval_required'}),
            ],
            'confirmEvents': [
                harness._event(4, 'effect.completed', {
                    'plan_id': approval['plan_id'],
                    'result': {'ok': True, 'summary': '暂停任务已提交，等待实际状态'},
                    'receipt': '暂停请求已提交；实际状态尚未确认。',
                }),
                harness._event(5, 'tool.progress', {
                    'tool': 'guangya.job',
                    'phase': 'background_job',
                    'summary': '安全排队中，等待实际状态回执',
                    'operation_ref': 'operation-ux-waiting-0001',
                }),
            ],
            'confirmHoldOpen': True,
        }, viewport={'width': 390, 'height': 844})
        page.locator('#agentPrompt').fill('暂停任务并等待实际状态')
        page.locator('#agentSend').click()
        page.locator('.agent-confirmation-card').wait_for()
        page.locator('[data-effect-confirm]').click()
        page.wait_for_function(
            "() => document.querySelector('.agent-stream-head')?.textContent.includes('安全排队中，等待实际状态回执')"
        )
        self.assertTrue(page.locator('#agentStopSession').is_visible())
        self.assertTrue(page.locator('#agentSend').is_disabled())
        self.assertEqual(page.locator('.agent-streaming').count(), 1)

        page.evaluate("""() => {
          const original = window.fetch;
          window.fetch = async (url, options = {}) => {
            const path = new URL(String(url), location.href).pathname;
            if (path === '/api/agent/session/stop') {
              const request = JSON.parse(options.body || '{}');
              const confirm = window.__kernelCalls.findLast(call => call.url === '/api/agent/actions/confirm');
              const requestId = JSON.parse(confirm.body).request_id;
              window.__kernelCalls.push({url: path, method: 'POST', body: options.body || ''});
              window.__kernelConfig.sessionDetails ||= {};
              window.__kernelConfig.sessionDetails[request.session_id] = {
                session_id: request.session_id, generation: 1, draft_scope: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                messages: [{role: 'user', content: '暂停任务并等待实际状态'}],
                pending_approval: null, candidate_view: null, last_turn: null,
                active_turn: {request_id: requestId, turn_id: `turn-${requestId}`, generation: 1,
                  protected: true, status: 'running', detail: '安全排队中，等待实际状态回执'},
              };
              return new Response(JSON.stringify({status: 'critical_pending', stopped: false, model_turn: 'critical_pending'}), {status: 200});
            }
            return original(url, options);
          };
        }""")
        page.locator('#agentStopSession').click()
        page.wait_for_function("() => document.querySelector('#agentResponseStatus').textContent.includes('不可中断')")
        self.assertTrue(page.locator('#agentSend').is_disabled())
        self.assertTrue(page.locator('#agentStopSession').is_visible())
        self.assertEqual(page.locator('.agent-streaming').count(), 1)
        self.assertEqual(page.locator('.agent-cancelled, .is-interrupted').count(), 0)
        calls = page.evaluate('window.__kernelCalls')
        self.assertEqual(sum(call['url'] == '/api/agent/actions/confirm' for call in calls), 1)
        self.assertEqual(sum(call['url'] == '/api/agent/query' for call in calls), 1)
        cancel = next(call for call in calls if call['url'] == '/api/agent/session/stop')
        confirm = next(call for call in calls if call['url'] == '/api/agent/actions/confirm')
        self.assertEqual(json.loads(cancel['body'])['session_id'], json.loads(confirm['body'])['session_id'])
        self.assertEqual(set(json.loads(cancel['body'])), {'session_id'})

    def test_candidate_refresh_restores_choices_and_keeps_card_bound_to_search(self):
        view = candidate_view()
        payload = {'sessions': {'sessions': [], 'draft_scope': SCOPE}, 'sessionDetails': {SESSION_A: {'messages': [{'role': 'assistant', 'content': '搜索结果', 'candidate_view': view}, {'role': 'assistant', 'content': '不相关的后续消息'}], 'candidate_view': view}}}
        page = self.page(payload, stored_session=SESSION_A)
        page.wait_for_selector('.agent-candidate-select:not([disabled])')
        page.locator('.agent-candidates-more > summary').click()
        page.locator('[data-candidate-position="2"]').check()
        page.locator('[data-candidate-position="1"]').uncheck()
        page.locator('.agent-candidate-target select').select_option('qb')
        self.reload_ui(page, payload)
        page.wait_for_selector('.agent-candidate-select:not([disabled])')
        self.assertTrue(page.locator('[data-candidate-position="2"]').is_checked())
        self.assertFalse(page.locator('[data-candidate-position="1"]').is_checked())
        self.assertEqual(page.locator('.agent-candidate-target select').input_value(), 'qb')
        self.assertIn('搜索结果', page.locator('.agent-candidates').locator('..').inner_text())
        self.assertNotIn('不相关', page.locator('.agent-candidates').locator('..').inner_text())

    def test_general_search_results_do_not_recommend_old_episodes_as_updates(self):
        view = candidate_view()
        view['recommended_positions'] = []
        view['items'] = [
            {'position': 1, 'title': '[GM-Team][东大高武学院][01-04][4K]', 'coverage': [None, 1, 4]},
            {'position': 2, 'title': '东大高武学院 S01E08', 'coverage': [1, 8, 8]},
        ]
        events = events_for_candidates(view)
        events[-1] = harness._event(3, 'turn.completed', {'status': 'success', 'answer': '本次检索未找到 S01E09；部分站点超时，仅见旧集资源。'})
        for viewport in ({'width': 1440, 'height': 900}, {'width': 768, 'height': 1024}, {'width': 390, 'height': 844}, {'width': 320, 'height': 640}):
            with self.subTest(viewport=viewport):
                page = self.page({'queryEvents': events}, viewport=viewport)
                page.locator('#agentPrompt').fill('看看有无资源')
                page.locator('#agentSend').click()
                page.wait_for_selector('.agent-narrative')
                self.assertEqual(page.locator('.agent-candidates-heading strong').inner_text(), '搜索结果')
                summary = page.locator('.agent-candidate-recommendation').inner_text()
                self.assertEqual(summary, '请选择需要的版本，再预览下载。')
                self.assertNotIn('01–04', summary)
                self.assertTrue(page.locator('.agent-candidate-select').is_disabled())
                self.assertEqual(page.locator('[data-candidate-position]:checked').count(), 0)
                self.assertFalse(page.locator('.agent-candidates-more').evaluate('(node) => node.open'))
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), viewport['width'])
                self.snapshot(page, 'unverified-search-' + str(viewport['width']))
                # 不把普通搜索下线：用户仍可明确挑选旧资源并预检，不能直接提交下载。
                page.locator('.agent-candidates-more > summary').click()
                page.locator('[data-candidate-position="1"]').check()
                page.locator('.agent-candidate-select').click()
                page.wait_for_function("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length === 2")
                selection = page.evaluate("JSON.parse(window.__kernelCalls.filter(call => call.url === '/api/agent/query')[1].body).selection")
                self.assertEqual(selection['positions'], [1])
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 0)

    def test_no_missing_episode_candidates_has_no_recommendation_or_download_controls(self):
        for status, answer in (
            ('success', '本次没有找到目标第 9 集的资源。'),
            ('success', '搜索结果只有1080p，没有符合2160p门槛的资源。'),
            ('partial', '部分索引站超时，暂时不能确认是否有更新。'),
        ):
            for width in (1440, 390):
                with self.subTest(status=status, answer=answer, width=width):
                    events = [harness._event(1, 'turn.started'), harness._event(2, 'tool.completed', {
                        'tool': 'fixture.resource_read', 'result': {'candidate_view': None},
                    }), harness._event(3, 'turn.completed', {'status': status, 'answer': answer})]
                    page = self.page({'queryEvents': events}, viewport={'width': width, 'height': 844})
                    page.locator('#agentPrompt').fill('仙逆完美世界有更新吗？4K排除1080')
                    page.locator('#agentSend').click()
                    page.wait_for_selector('.agent-narrative')
                    self.assertIn(answer, page.locator('.agent-narrative').inner_text())
                    self.assertEqual(page.locator('.agent-candidates').count(), 0)
                    self.assertEqual(page.locator('.agent-candidate-select').count(), 0)
                    self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 0)
                    self.snapshot(page, f'no-eligible-resources-{status}-{width}')

    def test_recommended_complementary_versions_are_compact_and_warn_on_overlap(self):
        view = candidate_view()
        view['recommended_positions'] = [1, 3]
        view['items'] = [{'position': pos, 'title': f'示例剧集.S01E{start:02}-{end:02}.2160p.{quality}', 'coverage': [1, start, end],
                          'site_name': '资源站 A', 'size_text': '12.8 GB', 'tags': {'resolution': '4K', 'effect': quality}, 'reasons': [], 'warnings': []}
                         for pos, start, end, quality in [(1, 5, 6, 'SDR'), (2, 5, 6, 'HDR / 60帧'), (3, 1, 4, 'SDR'), (4, 1, 4, 'HDR / 60帧')]]
        for viewport in ({'width': 1280, 'height': 800}, {'width': 390, 'height': 844}):
            with self.subTest(viewport=viewport):
                page = self.page({'queryEvents': events_for_candidates(view)}, viewport=viewport)
                page.locator('#agentPrompt').fill('找这部剧第1到6集的资源')
                page.locator('#agentSend').click()
                page.wait_for_selector('.agent-candidate-select:not([disabled])')
                self.assertFalse(page.locator('.agent-candidates-more').evaluate('(node) => node.open'))
                self.assertEqual(page.locator('.agent-candidate-recommendation li').count(), 2)
                self.snapshot(page, 'recommendation-' + str(viewport['width']))
                page.locator('.agent-candidates-more > summary').click()
                page.locator('[data-candidate-position="2"]').check()
                self.assertIn('重叠集数', page.locator('.agent-candidates-note').inner_text())
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 1)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), viewport['width'])

    def test_cross_series_same_episode_keeps_titles_and_does_not_warn_overlap(self):
        view = candidate_view()
        names = ['光阴之外', '择日飞升', '大主宰', '牧神记', '沧元图', '一斩苍穹']
        view['recommended_positions'] = list(range(1, 7))
        view['items'] = [{'position': pos, 'title': f'{name}.S01E08.2160p',
                          'media_title': name, 'requested_episode': [1, 8], 'coverage': [1, 8, 8],
                          'site_name': '测试站点', 'size_text': '1 GB', 'tags': {}, 'reasons': [], 'warnings': []}
                         for pos, name in enumerate(names, 1)]
        for viewport in ({'width': 1280, 'height': 800}, {'width': 390, 'height': 844}):
            with self.subTest(viewport=viewport):
                page = self.page({'queryEvents': events_for_candidates(view)}, viewport=viewport)
                page.locator('#agentPrompt').fill('找这六部缺集的资源')
                page.locator('#agentSend').click()
                page.wait_for_selector('.agent-candidate-select:not([disabled])')
                self.assertEqual(page.locator('.agent-candidate-recommendation li').count(), 6)
                text = page.locator('.agent-candidate-recommendation').inner_text()
                self.assertTrue(all(name in text for name in names))
                self.assertIn('预览下载 6 项', page.locator('.agent-candidate-select').inner_text())
                self.assertNotIn('重叠', page.locator('.agent-candidates-note').inner_text())
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), viewport['width'])

    def test_legacy_single_card_protocol_is_readonly(self):
        view = candidate_view()
        view.pop('selection_ref')
        page = self.page({'queryEvents': events_for_candidates(view)})
        page.locator('#agentPrompt').fill('回看历史候选')
        page.locator('#agentSend').click()
        page.wait_for_selector('.agent-candidates')
        self.assertTrue(page.locator('.agent-candidate-select').is_disabled())
        self.assertIn('仅供回看', page.locator('.agent-candidates-note').inner_text())

    def test_approval_received_before_stream_failure_is_not_lost(self):
        view = candidate_view()
        page = self.page({'queryEvents': events_for_candidates(view)})
        page.locator('#agentPrompt').fill('先搜索')
        page.locator('#agentSend').click()
        page.wait_for_selector('.agent-candidate-select:not([disabled])')
        events = [harness._event(1, 'turn.started'), harness._event(2, 'effect.approval_required', {'plan': {
            'plan_id': 'plan_batch_disconnect_00001', 'effect': 'WRITE', 'preview': {'summary': '预检已完成'}, 'confirmation': {},
        }}), harness._event(3, 'turn.failed', {'message': '连接中断'})]
        page.evaluate('(events) => {window.__kernelConfig.queryEvents = events}', events)
        page.locator('.agent-candidate-select').click()
        page.wait_for_selector('.agent-candidate-feedback')
        self.assertTrue(page.locator('[data-effect-confirm]').is_enabled())
        self.assertTrue(page.locator('.agent-candidate-select').is_disabled())
        self.assertIn('预检已完成', page.locator('.agent-confirmation-card').inner_text())
        self.assertIn('连接中断', page.locator('.agent-candidate-feedback').inner_text())
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 0)

    def test_public_process_expands_without_mixing_answer_or_changing_choice(self):
        for width in (1280, 390):
            with self.subTest(width=width):
                page = self.progress_page(width=width)
                page.locator('#agentPrompt').fill('查找收藏剧集的最新资源')
                page.locator('#agentSend').click()
                self.emit_progress(page, 'turn.started')
                outer = {'tool': 'agent.run_code', 'label': '工作区代码执行',
                         'arguments': {'secret': 'RAW_ARGUMENTS'}, 'stdout': 'RAW_STDOUT',
                         'result': {'summary': '命令已退出（退出码 0）'}}
                self.emit_progress(page, 'model.tool_call', outer)
                self.emit_progress(page, 'tool.started', outer)
                toggle = page.locator('.agent-process-toggle')
                self.assertTrue(toggle.is_hidden())
                self.assertEqual(page.locator('.agent-process').count(), 0)
                before = page.locator('.agent-result-card').bounding_box()
                activity = {'id': 'sdk-resource', 'kind': 'tool', 'label': '查询资源',
                            'method': 'resource.search', 'status': 'running', 'summary': '查询：光阴之外'}
                self.emit_progress(page, 'tool.progress', {**outer, 'activity': activity})
                self.assertEqual(toggle.get_attribute('aria-expanded'), 'false')
                self.assertTrue(toggle.is_visible())
                after = page.locator('.agent-result-card').bounding_box()
                assert before is not None and after is not None
                self.assertAlmostEqual(after['height'], before['height'], delta=0.5)
                self.assertEqual(page.locator('.agent-process').get_attribute('id'), toggle.get_attribute('aria-controls'))
                self.emit_progress(page, 'model.reasoning_summary.delta', {'round': 1, 'summary_id': 'public', 'delta': '先核对剧集，'})
                self.emit_progress(page, 'model.reasoning_summary.delta', {'round': 1, 'summary_id': 'public', 'delta': '再比较资源版本。'})
                self.emit_progress(page, 'model.reasoning_summary.delta', {'round': 2, 'summary_id': 'public', 'delta': '核对下一轮结果。'})
                self.emit_progress(page, 'model.reasoning_summary.delta', {'round': 2, 'summary_id': 'other', 'delta': '检查未读页。'})
                self.emit_progress(page, 'model.reasoning.delta', {'delta': 'PRIVATE_REASONING'})
                self.emit_progress(page, 'model.delta', {'round': 2, 'delta': '这是单独流式显示的答案。'})
                self.assertEqual(page.locator('.agent-stream-text').inner_text(), '这是单独流式显示的答案。')
                self.assertEqual(toggle.get_attribute('aria-expanded'), 'false')
                self.snapshot(page, f'expand-collapsed-{width}')
                toggle.focus()
                toggle.press('Enter')
                self.assertEqual(toggle.get_attribute('aria-expanded'), 'true')
                self.assertTrue(page.locator('.agent-process').is_visible())
                self.assertEqual(page.locator('.agent-process-item').count(), 4)
                self.assertEqual(page.locator('.agent-process-item[data-kind="reasoning"] .agent-activity-summary').all_text_contents(),
                                 ['先核对剧集，再比较资源版本。', '核对下一轮结果。', '检查未读页。'])
                page.evaluate("window.__activityNode = document.querySelector('.agent-process-item')")
                # 同一个 SDK 调用可附在直接工具、effect 或后续公共事件上；只更新原行。
                for kind, status in (('tool.started', 'running'), ('effect.preview_started', 'running'),
                                     ('effect.approval_required', 'approval_required'), ('tool.failed', 'failed'),
                                     ('tool.completed', 'partial'), ('effect.completed', 'success'),
                                     ('effect.failed', 'failed')):
                    self.emit_progress(page, kind, {'activity': {**activity, 'status': status, 'summary': '实际查询：已读 20 条，还有未读页。'}})
                    self.assertTrue(page.evaluate("window.__activityNode === document.querySelector('.agent-process-item')"))
                    self.assertEqual(page.locator('.agent-process-item').count(), 4)
                    self.assertEqual(toggle.get_attribute('aria-expanded'), 'true')
                self.assertEqual(page.locator('.agent-activity-status').first.inner_text(), '失败')
                self.snapshot(page, f'expand-open-{width}')
                if width == 390:
                    toggle.press('Space')
                self.emit_progress(page, 'model.delta', {'round': 2, 'delta': '答案继续更新。'})
                self.emit_progress(page, 'turn.completed', {'status': 'partial', 'answer': '资源查询部分完成，答案与过程独立。'})
                self.assertEqual(toggle.get_attribute('aria-expanded'), 'false' if width == 390 else 'true')
                self.assertEqual(page.locator('.agent-narrative').inner_text(), '资源查询部分完成，答案与过程独立。')
                transcript = page.locator('#agentTranscript').text_content()
                assert transcript is not None
                for raw in ('agent.run_code', '工作区代码执行', 'RAW_ARGUMENTS', 'RAW_STDOUT', '退出码', 'PRIVATE_REASONING'):
                    self.assertNotIn(raw, transcript)
                self.assertTrue(toggle.is_enabled())
                toggle_box = toggle.bounding_box()
                assert toggle_box is not None
                self.assertGreaterEqual(toggle_box['height'], 44)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.method === 'POST').length"), 1)
                self.snapshot(page, f'expand-final-{width}')
                page.close()

    def test_process_is_bounded_and_updates_do_not_scroll_the_reader(self):
        page = self.progress_page(width=390)
        page.emulate_media(reduced_motion='reduce')
        page.locator('#agentPrompt').fill('核对资源列表')
        page.locator('#agentSend').click()
        self.emit_progress(page, 'turn.started')
        page.evaluate("""() => {
            for (let index = 0; index < 85; index++) window.emitProgress('tool.progress', {activity: {
                id: `sdk-${index}`, kind: 'tool', label: `核对资源 ${index}`, method: 'resource.search',
                status: 'success', summary: `已核对第 ${index} 项。`,
            }});
            window.emitProgress('model.reasoning_summary.delta', {round: 1, delta: '公开摘要。'.repeat(1800)});
        }""")
        page.clock.run_for(100)
        self.assertEqual(page.locator('.agent-process-item').count(), 80)
        self.assertEqual(page.locator('.agent-activity-label').first.text_content(), '核对资源 6')
        summary = page.locator('.agent-process-item[data-kind="reasoning"] .agent-activity-summary').text_content()
        assert summary is not None
        self.assertEqual(len(summary), 8000)
        page.locator('.agent-process-toggle').click()
        self.emit_progress(page, 'model.delta', {'round': 1, 'delta': '独立答案正文。\n\n' * 50})
        page.evaluate("""() => {
            const process = document.querySelector('.agent-process');
            const transcript = document.querySelector('#agentTranscript');
            process.scrollTop = 70;
            transcript.scrollTop = 20;
            transcript.dispatchEvent(new Event('scroll'));
            window.__reading = [process.scrollTop, transcript.scrollTop];
        }""")
        self.emit_progress(page, 'tool.progress', {'activity': {'id': 'sdk-84', 'kind': 'tool', 'label': '核对资源 84',
                           'method': 'resource.search', 'status': 'partial', 'summary': '<img src=x onerror=window.__ux_xss=1> 有未读页'}})
        self.emit_progress(page, 'model.reasoning_summary.delta', {'round': 1, 'delta': '超过上限的摘要不会增长。'})
        self.emit_progress(page, 'model.delta', {'round': 1, 'delta': '答案继续追加。'})
        self.emit_progress(page, 'turn.completed', {'status': 'partial', 'answer': '独立答案正文。\n\n' * 50 + '答案继续追加。'})
        positions = page.evaluate("""() => ({before: window.__reading,
            after: [document.querySelector('.agent-process').scrollTop, document.querySelector('#agentTranscript').scrollTop]})""")
        for before, after in zip(positions['before'], positions['after']):
            self.assertAlmostEqual(after, before, delta=1)
        self.assertEqual(page.locator('.agent-process-toggle').get_attribute('aria-expanded'), 'true')
        self.assertEqual(page.locator('.agent-process img').count(), 0)
        self.assertFalse(page.evaluate('Boolean(window.__ux_xss)'))
        self.assertEqual(page.locator('.agent-stream-head > svg').evaluate('node => getComputedStyle(node).animationName'), 'none')

    def test_persisted_activities_expand_without_elapsed_or_invented_old_process(self):
        activities = [
            {'id': 'sdk-history', 'kind': 'tool', 'label': '查询媒体库', 'method': 'library.search', 'status': 'success', 'summary': '已核对 37 集。'},
            {'id': 'sdk-unconfirmed', 'kind': 'tool', 'label': '核对云盘', 'method': 'cloud.list', 'status': 'running', 'summary': '已开始查询，尚无最终回执。'},
            {'id': 'reasoning:2:public', 'kind': 'reasoning', 'text': '只展示后端保存的公开摘要。'},
        ]
        config = {'sessionDetails': {SESSION_A: session_snapshot(SESSION_A, messages=[
            {'role': 'assistant', 'content': '旧对话，没有过程。', 'activities': [], 'tools': ['agent.run_code']},
            {'role': 'assistant', 'content': '已保存的最终答案。', 'activities': activities},
        ])}}
        page = self.page(config, stored_session=SESSION_A, viewport={'width': 390, 'height': 844})
        page.locator('.agent-narrative').last.wait_for()
        self.assertEqual(page.locator('.agent-process').count(), 1)
        self.assertEqual(page.locator('.agent-process-toggle:visible').count(), 1)
        self.assertEqual(page.locator('.agent-stream-head:visible').inner_text(), '处理过程')
        self.assertEqual(page.locator('time.agent-stream-elapsed:visible').count(), 0)
        self.assertNotRegex(page.locator('.agent-stream-head').inner_text(), r'\d+秒')
        toggle = page.locator('.agent-process-toggle')
        self.assertEqual(toggle.get_attribute('aria-expanded'), 'false')
        toggle.focus()
        toggle.press('Enter')
        self.assertEqual(page.locator('.agent-activity-status').all_text_contents(), ['已完成', '结果待核对', ''])
        self.assertIn('只展示后端保存的公开摘要。', page.locator('.agent-process').inner_text())
        transcript = page.locator('#agentTranscript').text_content()
        assert transcript is not None
        self.assertNotIn('agent.run_code', transcript)
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.method === 'POST').length"), 0)
        self.snapshot(page, 'expand-history-mobile')

    def test_process_keeps_confirmation_receipt_and_unconfirmed_failure(self):
        page = self.progress_page()
        page.locator('#agentPrompt').fill('核对并暂停下载')
        page.locator('#agentSend').click()
        self.emit_progress(page, 'turn.started')
        activity = {'id': 'sdk-pause', 'kind': 'tool', 'label': '暂停下载', 'method': 'download.pause',
                    'status': 'approval_required', 'summary': '等待用户确认。'}
        self.emit_progress(page, 'tool.progress', {'activity': activity})
        page.locator('.agent-process-toggle').click()
        plan_id = 'plan_process_confirmation_0001'
        self.emit_progress(page, 'effect.approval_required', {'activity': activity, 'plan': {
            'plan_id': plan_id, 'effect': 'WRITE', 'tool_name': 'download.pause', 'preview': {'summary': '暂停下载任务'},
        }})
        self.emit_progress(page, 'turn.completed', {'status': 'approval_required'})
        self.assertTrue(page.locator('[data-effect-confirm]').is_enabled())
        self.assertEqual(page.locator('.agent-process-toggle').get_attribute('aria-expanded'), 'true')
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 0)
        saved_session = page.evaluate("localStorage.getItem('mediaflux.agent.kernel.session.v1')")
        self.reload_ui(page, {
            'sessions': {'draft_scope': SCOPE, 'sessions': [{'session_id': saved_session, 'title': '待确认下载'}]},
            'sessionDetails': {saved_session: session_snapshot(saved_session,
                messages=[{'role': 'user', 'content': '核对并暂停下载'}], pending_approval={
                    'plan_id': plan_id, 'effect': 'WRITE', 'tool_name': 'download.pause',
                    'preview': {'summary': '暂停下载任务'}, 'activities': [activity],
                })},
        })
        page.locator('[data-effect-confirm]').wait_for()
        self.assertTrue(page.locator('[data-effect-confirm]').is_enabled())
        self.assertEqual(page.locator('.agent-stream-head').inner_text(), '等待确认')
        self.assertEqual(page.locator('time.agent-stream-elapsed:visible').count(), 0)
        self.assertEqual(page.locator('.agent-process-toggle').get_attribute('aria-expanded'), 'false')
        page.locator('.agent-process-toggle').click()
        self.assertEqual(page.locator('.agent-process-item').count(), 1)
        self.assertTrue(page.evaluate("document.querySelector('.agent-process-view').nextElementSibling.matches('.agent-confirmation-card')"))
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 0)
        page.locator('.agent-process-toggle').hover()
        self.assertTrue(page.locator('.agent-stream-label').is_visible())
        # 整行点击按钮覆盖在状态文字上，悬停背景必须透明，防止遮住实际文字。
        self.assertEqual(page.locator('.agent-process-toggle').evaluate('node => getComputedStyle(node).backgroundColor'), 'rgba(0, 0, 0, 0)')
        self.snapshot(page, 'expand-pending-confirmation-restored')
        self.assertTrue(page.locator('.agent-activity-status').is_visible())
        self.assertEqual(page.locator('.agent-activity-status').inner_text(), '等待确认')
        page.evaluate("""events => { window.__kernelConfig.confirmEvents = events; }""", [
            harness._event(1, 'turn.started', {'kind': 'confirmation'}),
            harness._event(2, 'tool.progress', {'activity': {**activity, 'status': 'success', 'summary': '暂停操作已取得回执。'}}),
            harness._event(3, 'effect.completed', {'plan_id': plan_id, 'receipt': '下载任务已暂停。',
                'result': {'ok': True, 'summary': '下载任务已暂停。'}}),
            harness._event(4, 'tool.progress', {'activity': {'id': 'sdk-verify', 'kind': 'tool', 'label': '核对下载状态',
                'method': 'download.status', 'status': 'running', 'summary': '已发出核对请求。'}}),
            harness._event(5, 'turn.failed', {'message': '后续核验连接中断'}),
        ])
        page.locator('[data-effect-confirm]').click()
        page.clock.run_for(200)
        self.assertIn('下载任务已暂停。', page.locator('.agent-narrative').inner_text())
        self.assertIn('后续核验连接中断', page.locator('.agent-narrative').inner_text())
        self.assertEqual(page.locator('.agent-activity-status').all_text_contents(), ['已完成', '结果待核对'])
        self.assertEqual(page.locator('.agent-process-toggle').get_attribute('aria-expanded'), 'true')
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 1)
        self.snapshot(page, 'expand-confirmation-failure')

    def test_unfinished_activity_requires_verification_after_cancel_failure_or_stream_end(self):
        activity = {'id': 'sdk-unfinished', 'kind': 'tool', 'label': '核对云盘结果',
                    'method': 'cloud.list', 'status': 'running', 'summary': '已发出查询请求。'}
        for terminal in ('turn.failed', 'turn.cancelled', None):
            with self.subTest(terminal=terminal):
                events = [harness._event(1, 'turn.started'),
                          harness._event(2, 'tool.progress', {'activity': {**activity, 'id': 'sdk-finished', 'status': 'success'}}),
                          harness._event(3, 'tool.progress', {'activity': activity})]
                if terminal:
                    events.append(harness._event(4, terminal, {'message': '未取得后续结果。'}))
                page = self.page({'queryEvents': events})
                page.locator('#agentPrompt').fill('核对云盘结果')
                page.locator('#agentSend').click()
                page.wait_for_function("document.querySelector('.agent-process-item:last-child .agent-activity-status')?.textContent === '结果待核对'")
                page.locator('.agent-process-toggle').click()
                self.assertEqual(page.locator('.agent-activity-status').all_text_contents(), ['已完成', '结果待核对'])
                self.assertNotIn('处理中', page.locator('.agent-process').inner_text())
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.method === 'POST').length"), 1)
                page.close()

    def test_live_progress_keeps_one_status_elapsed_body_and_frozen_terminal(self):
        # 无公开活动时只显示状态，覆盖重复调用、失败调整和窄屏。
        outcomes = (
            (1280, 'success', '已完成', '已核对收藏，已恢复服务并完成核验。'),
            (390, 'partial', '部分完成', '已核对收藏，部分来源暂时不可用：服务暂时不可用。'),
            (320, 'waiting', '等待后台结果', '已核对收藏，后台任务仍在处理，等待结果。'),
            (390, 'failed', '未能完成', '服务暂时不可用，暂时无法核验收藏。'),
            (1280, 'cancelled', '已停止', '本轮处理已停止。'),
        )
        for width, outcome, terminal_label, final_text in outcomes:
            with self.subTest(width=width, outcome=outcome):
                page = self.progress_page(width=width)
                page.locator('#agentPrompt').fill('看看我收藏的剧集有没有更新')
                page.locator('#agentSend').click()
                self.emit_progress(page, 'turn.started')
                self.emit_progress(page, 'model.started', {'round': 1})
                head = page.locator('.agent-stream-head')
                label = head.locator('span').first
                self.assertEqual(label.inner_text(), '正在思考')
                page.evaluate("""() => {
                    window.progressHead = document.querySelector('.agent-stream-head');
                    window.progressLabel = window.progressHead.querySelector('span');
                    window.progressElapsed = window.progressHead.querySelector('time.agent-stream-elapsed');
                }""")
                before_head = head.bounding_box()
                before_card = page.locator('.agent-result-card').bounding_box()
                assert before_head is not None and before_card is not None
                previous_elapsed = self.elapsed_seconds(page)

                def assert_status(expected, *, page=page, head=head, label=label,
                                  before_head=before_head, width=width):
                    nonlocal previous_elapsed
                    self.assertEqual(head.count(), 1)
                    self.assertTrue(head.is_visible())
                    self.assertEqual(label.inner_text(), expected)
                    self.assertEqual(head.locator('span').count(), 1)
                    self.assertTrue(page.evaluate("""() =>
                        window.progressHead === document.querySelector('.agent-stream-head') &&
                        window.progressLabel === window.progressHead.querySelector('span') &&
                        window.progressElapsed === window.progressHead.querySelector('time.agent-stream-elapsed')
                    """))
                    elapsed = self.elapsed_seconds(page)
                    self.assertGreaterEqual(elapsed, previous_elapsed)
                    previous_elapsed = elapsed
                    text = page.locator('#agentTranscript').inner_text()
                    self.assertNotRegex(text, r'执行过程|退出码|命令已退出|×\s*\d+|其中\s*\d+\s*次')
                    self.assertNotIn('工作区代码执行', text)
                    # 一次取同一帧的坐标，避免入场动画在三次跨进程读取之间继续移动。
                    bounds, label_box, elapsed_box = head.evaluate("""node => [
                        node, node.querySelector('.agent-stream-label'), node.querySelector('time'),
                    ].map(item => item.getBoundingClientRect().toJSON())""")
                    self.assertAlmostEqual(bounds['height'], before_head['height'], delta=0.5)
                    self.assertAlmostEqual(label_box['y'] + label_box['height'] / 2,
                                           elapsed_box['y'] + elapsed_box['height'] / 2, delta=0.5)
                    self.assertLessEqual(elapsed_box['x'] + elapsed_box['width'], bounds['x'] + bounds['width'] + 1)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)

                assert_status('正在思考')
                page.clock.run_for(3200)
                self.assertEqual(self.elapsed_seconds(page), previous_elapsed + 3)
                assert_status('正在思考')
                if outcome in ('success', 'partial'):
                    self.snapshot(page, f'progress-thinking-{width}')
                call = {'tool': 'agent.run_code', 'label': '工作区代码执行', 'call_id': 'first'}
                self.emit_progress(page, 'model.tool_call', call)
                assert_status('正在处理')
                self.assertEqual(page.locator('.agent-streaming .lucide-loader-circle:visible').count(), 1)
                self.emit_progress(page, 'tool.started', call)
                assert_status('正在处理')
                self.emit_progress(page, 'tool.completed', {**call, 'result': {'summary': '命令已退出（退出码 0）'}})
                self.emit_progress(page, 'model.started', {'round': 2})
                assert_status('正在思考')
                self.emit_progress(page, 'model.tool_call', {**call, 'call_id': 'second'})
                assert_status('正在处理')
                after_card = page.locator('.agent-result-card').bounding_box()
                assert after_card is not None
                self.assertAlmostEqual(after_card['height'], before_card['height'], delta=0.5)
                self.emit_progress(page, 'tool.failed', {**call, 'call_id': 'second', 'message': '服务暂时不可用'})
                assert_status('正在调整方案')
                self.emit_progress(page, 'model.started', {'round': 3})
                assert_status('正在思考')
                streamed_text = '已核对收藏，部分来源暂时不可用。'
                self.emit_progress(page, 'model.delta', {'round': 3, 'delta': streamed_text})
                assert_status('正在回复')
                self.assertEqual(page.locator('.agent-stream-text').inner_text(), streamed_text)
                self.assertEqual(page.locator('.agent-streaming .lucide-loader-circle:visible').count(), 0)
                if outcome in ('success', 'partial'):
                    self.snapshot(page, f'progress-reply-{width}')
                page.emulate_media(reduced_motion='reduce')
                # 旧事件仅含 label 也必须使用通用状态，不能泄露工具清单。
                self.emit_progress(page, 'tool.started', {'label': '工作区代码执行', 'call_id': 'third'})
                assert_status('正在处理')
                self.assertEqual(page.locator('.agent-stream-text').inner_text(), streamed_text)
                self.assertEqual(head.locator(':scope > svg').evaluate('node => getComputedStyle(node).animationName'), 'none')
                self.emit_progress(page, 'tool.completed', {**call, 'call_id': 'third', 'result': {'summary': '命令已退出（退出码 0）'}})
                page.clock.run_for(1100)
                assert_status('正在处理')
                if outcome in ('failed', 'cancelled'):
                    self.emit_progress(page, f'turn.{outcome}', {'message': final_text})
                    self.assertEqual(page.locator('.agent-stream-text').inner_text(), final_text)
                else:
                    self.emit_progress(page, 'turn.completed', {'status': outcome, 'answer': final_text})
                    self.assertEqual(page.locator('.agent-narrative').inner_text(), final_text)
                assert_status(terminal_label)
                self.assertEqual(page.locator('.agent-streaming').count(), 0)
                self.assertEqual(page.locator('#agentSend').get_attribute('aria-busy'), 'false')
                frozen_elapsed = self.elapsed_seconds(page)
                self.assertGreaterEqual(frozen_elapsed, 5)
                frozen_text = head.inner_text()
                page.clock.run_for(3000)
                self.assertEqual(self.elapsed_seconds(page), frozen_elapsed)
                self.assertEqual(head.inner_text(), frozen_text)
                assert_status(terminal_label)
                appearance = page.locator('.agent-result-card').evaluate("""node => {
                    const style = getComputedStyle(node);
                    return {background: style.backgroundColor, shadow: style.boxShadow,
                            borders: [style.borderTopWidth, style.borderRightWidth, style.borderBottomWidth, style.borderLeftWidth]};
                }""")
                self.assertEqual(appearance, {'background': 'rgba(0, 0, 0, 0)', 'shadow': 'none', 'borders': ['0px'] * 4})
                # 消息复用同一阅读列；去掉头像后，正文应占满消息宽度。
                layout = page.locator('#agentTranscript').evaluate("""node => {
                    const style = getComputedStyle(node);
                    const available = node.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
                    const width = Math.min(980, available);
                    const left = node.getBoundingClientRect().left + node.clientLeft
                        + parseFloat(style.paddingLeft) + (available - width) / 2;
                    return {width, left, messages: Array.from(node.querySelectorAll('.agent-message'), message => ({
                        box: message.getBoundingClientRect().toJSON(),
                        body: message.querySelector('.agent-message-body').getBoundingClientRect().toJSON(),
                    }))};
                }""")
                self.assertEqual(len(layout['messages']), 2)
                for message in layout['messages']:
                    self.assertAlmostEqual(message['box']['width'], layout['width'], delta=0.5)
                    self.assertAlmostEqual(message['box']['x'], layout['left'], delta=0.5)
                    self.assertAlmostEqual(message['body']['width'], message['box']['width'], delta=0.5)
                    self.assertAlmostEqual(message['body']['x'], message['box']['x'], delta=0.5)
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 1)
                if outcome in ('success', 'partial'):
                    self.snapshot(page, f'progress-final-{width}')
                page.close()

    def test_write_preview_requires_confirmation_without_executing(self):
        for count, approved in ((1, 0), (2, 0), (2, 1)):
            with self.subTest(calls=count, approved=approved):
                plan = {'plan_id': 'plan_grouped_preview', 'tool_name': 'download.pause',
                        'effect': 'WRITE', 'preview': {'summary': '暂停所选下载'}}
                calls = [{'call_id': f'pause-{index}', 'tool': 'download.pause', 'label': '暂停下载'}
                         for index in range(count)]
                sequence = [('turn.started', {})]
                sequence += [('model.tool_call', call) for call in calls]
                sequence += [('effect.approval_required', {**call, 'plan': plan}) if index == approved else
                             ('tool.failed', {**call, 'code': 'not_executed_after_approval' if index > approved else 'invalid_arguments',
                                              'message': '前序写操作需要确认，本调用未执行' if index > approved else '参数无效，本调用未执行'})
                             for index, call in enumerate(calls)]
                sequence.append(('turn.completed', {'status': 'approval_required'}))
                page = self.page({'queryEvents': [harness._event(index, kind, payload)
                                                  for index, (kind, payload) in enumerate(sequence, 1)]})
                page.locator('#agentPrompt').fill('暂停所选下载')
                page.locator('#agentSend').click()
                page.locator('.agent-confirmation-card').wait_for()
                page.wait_for_function("() => document.querySelector('#agentSend').getAttribute('aria-busy') !== 'true'")
                self.assertIn('暂停所选下载', page.locator('.agent-confirmation-card').inner_text())
                self.assertIn('尚未写入任何变更', page.locator('.agent-confirmation-preflight').inner_text())
                self.assertTrue(page.locator('[data-effect-confirm]').is_enabled())
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 0)
                page.close()
