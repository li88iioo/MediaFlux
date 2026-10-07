"""Agent体验的真实浏览器回归：等待、草稿、稳定位置与安全候选交互。"""
from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

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
                self.assertLessEqual(abs(before['y'] - after['y']), 0.5)
                self.assertLessEqual(abs(before['height'] - after['height']), 0.5)
                console = page.locator('.agent-workbench').bounding_box()
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
                self.assertIsNotNone(resume_box)
                console = page.locator('.agent-workbench').bounding_box()
                self.assertLessEqual(abs(composer_box['y'] + composer_box['height'] / 2 - console['y'] - console['height'] / 2), 0.5)
                self.assertGreaterEqual(resume_box['y'], composer_box['y'] + composer_box['height'])
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
        page.wait_for_selector('#agentStop:not([hidden])')
        self.assertTrue(page.locator('#agentPrompt').is_enabled())
        page.locator('#agentPrompt').fill('另外只看失败任务')
        page.locator('#agentPrompt').press('Enter')
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 1)
        page.locator('#agentStop').click()
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
                page.wait_for_function("() => document.querySelector('#agentStop').hidden")
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
        page = self.page({'queryEvents': events_for_candidates(view)})
        page.locator('#agentPrompt').fill('找第一集的版本')
        page.locator('#agentSend').click()
        page.wait_for_selector('.agent-candidate-select:not([disabled])')
        self.assertEqual(page.locator('.agent-candidate-row').count(), 2)
        self.assertFalse(page.locator('.agent-candidates-more').evaluate('(node) => node.open'))
        self.assertEqual(page.locator('.agent-candidates img').count(), 0)
        self.assertFalse(page.evaluate('Boolean(window.__ux_xss)'))
        page.locator('.agent-candidates-more > summary').click()
        self.assertIn('1080p', page.locator('.agent-candidate-info').first.inner_text())
        page.locator('.agent-candidate-detail > summary').first.click()
        self.assertIn('字幕需要人工核对', page.locator('.agent-candidate-detail').first.inner_text())
        page.locator('[data-candidate-position="2"]').check()
        page.locator('.agent-candidate-target select').select_option('both')
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
        self.assertEqual((before['width'], before['height']), (after['width'], after['height']))
        self.snapshot(page, 'candidates-desktop')

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
        page = self.page({'queryEvents': events_for_candidates(candidate_view(expires_at=time.time() - 5))}, viewport={'width': 390, 'height': 844})
        page.locator('#agentPrompt').fill('显示旧的搜索结果')
        page.locator('#agentSend').click()
        page.wait_for_selector('.agent-candidate-row', state='attached')
        page.wait_for_function("() => document.querySelector('#agentStop').hidden")
        self.assertTrue(page.locator('.agent-candidate-select').first.is_disabled())
        self.assertIn('仅供回看', page.locator('.agent-candidates-note').inner_text())
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
        self.assertLessEqual(abs(page.locator('#agentComposer').bounding_box()['y'] - before['y']), 0.5)
        page.evaluate("""({id, turn}) => { window.__kernelConfig.sessionDetails[id] = {
            generation: 2, messages: [{role: 'user', content: '原请求'}, {role: 'assistant', content: '恢复后仅一次完成'}],
            active_turn: null, last_turn: {...turn, status: 'completed', message: '完成'},
        }; }""", {'id': SESSION_A, 'turn': current})
        page.get_by_text('恢复后仅一次完成', exact=True).wait_for(timeout=10000)
        self.assertEqual(page.locator('.agent-message-assistant').count(), 1)
        self.assertEqual(page.locator('#agentPrompt').input_value(), '保留未发送草稿')
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
        self.assertTrue(page.locator('#agentStop').is_hidden())

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
        self.assertTrue(page.locator('#agentStop').is_hidden())
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
        self.assertTrue(page.locator('#agentStop').is_hidden())

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
        page.locator('#agentStop').wait_for(state='visible')
        page.locator('#agentPrompt').fill('新请求期间保留的草稿')
        page.evaluate('window.__releaseReceiptRead()')
        page.wait_for_timeout(100)

        self.assertIn('发起新请求', page.locator('#agentTranscript').inner_text())
        self.assertNotIn('不应覆盖新请求的晚到回执。', page.locator('#agentTranscript').inner_text())
        self.assertEqual(page.locator('#agentPrompt').input_value(), '新请求期间保留的草稿')
        self.assertTrue(page.locator('#agentStop').is_visible())
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query' && call.method === 'POST').length"), 1)

    def test_accepted_cancel_waits_for_verified_terminal_instead_of_aborting_ui(self):
        current = {
            'request_id': 'stop-request', 'turn_id': 'stop-turn', 'generation': 3,
            'protected': False, 'status': 'running', 'detail': '正在查询',
        }
        payload = session_snapshot(SESSION_A)
        payload.update(generation=3, messages=[{'role': 'user', 'content': '等待停止'}], active_turn=current)
        page = self.page({'cancelPending': True, 'sessions': {'draft_scope': SCOPE, 'sessions': []}, 'sessionDetails': {SESSION_A: payload}}, stored_session=SESSION_A)
        page.locator('#agentStop').wait_for(state='visible')
        page.locator('#agentStop').click()
        page.wait_for_timeout(2200)
        self.assertNotIn('请求已停止', page.locator('#agentResponseStatus').inner_text())
        self.assertIn('停止', page.locator('#agentResponseStatus').inner_text())
        page.evaluate("""({id, turn}) => { window.__kernelConfig.sessionDetails[id] = {
            generation: 3, messages: [{role: 'user', content: '等待停止'}], active_turn: null,
            last_turn: {...turn, status: 'cancelled', message: '已核实取消'},
        }; }""", {'id': SESSION_A, 'turn': current})
        page.get_by_text('已核实取消', exact=True).wait_for(timeout=10000)
        self.assertTrue(page.locator('#agentStop').is_hidden())
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.url === '/api/agent/query/cancel').length"), 1)
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.url === '/api/agent/query').length"), 0)

    def test_failed_terminal_turn_is_restored_after_refresh(self):
        sessions = {'sessions': [{'session_id': SESSION_A, 'title': '失败任务历史'}], 'draft_scope': SCOPE}
        initial = {'sessions': sessions, 'sessionDetails': {SESSION_A: session_snapshot(SESSION_A)}}
        page = self.page(initial, stored_session=SESSION_A)
        failed = session_snapshot(
            SESSION_A,
            messages=[{'role': 'user', 'content': '执行受控失败任务'}],
            last_turn={
                'request_id': 'rq-refresh-failed-0001',
                'turn_id': 'turn-rq-refresh-failed-0001',
                'status': 'failed',
                'message': '受控任务失败，状态已同步。',
            },
        )
        self.reload_ui(page, {'sessions': sessions, 'sessionDetails': {SESSION_A: failed}})
        page.wait_for_function("() => document.querySelector('.agent-result-card.is-interrupted .agent-stream-text')?.textContent.includes('受控任务失败')")
        self.assertEqual(page.locator('.agent-result-card.is-interrupted .agent-stream-text').count(), 1)
        self.assertEqual(page.locator('#agentTranscript .agent-message-user').count(), 1)
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 0)
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 0)

    def test_resume_nonempty_history_preserves_focused_resume_control(self):
        page = self.page({'sessions': {'sessions': [{'session_id': SESSION_A, 'title': '已保存的历史'}]},
                          'sessionDetails': {SESSION_A: {'messages': [{'role': 'assistant', 'content': '之前已经完成的排障记录。'}]}}})
        page.locator('#agentResumeLatestSession').click()
        page.wait_for_selector('.agent-narrative')
        self.assertTrue(page.locator('#agentResumeLatestSession').evaluate('(node) => document.activeElement === node'))
        self.assertTrue(page.locator('#agentComposer #agentResumeLatestSession').is_visible())

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
        self.assertAlmostEqual(before['width'], during['width'], delta=0.5)
        self.assertAlmostEqual(before['height'], during['height'], delta=0.5)
        self.assertTrue(page.locator('#agentStop').is_visible())
        self.assertTrue(page.locator('#agentSend').is_hidden())
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
                  const effectSteps = [...document.querySelectorAll('.agent-stream-step[data-step-key^="effect:"]')];
                  return {
                    viewportWidth: innerWidth,
                    narrativeCount: document.querySelectorAll('.agent-narrative').length,
                    receiptReferenceCount: (text.match(/#276/g) || []).length,
                    traceCount: document.querySelectorAll('.agent-tool-trace').length,
                    effectStepCount: effectSteps.length,
                    effectCompletionCheckCount: effectSteps.filter(step => step.querySelector('[data-lucide="check"]')).length,
                    narrativeText: text,
                  };
                }""")
                self.assertEqual(metrics['viewportWidth'], viewport['width'])
                self.assertEqual(metrics['narrativeCount'], 1)
                self.assertEqual(metrics['receiptReferenceCount'], 1)
                self.assertEqual(metrics['traceCount'], 1)
                self.assertEqual(metrics['effectStepCount'], 1)
                self.assertEqual(metrics['effectCompletionCheckCount'], 0)
                self.assertIn('后台任务尚未完成', metrics['narrativeText'])
                self.assertNotIn('✅', metrics['narrativeText'])
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

    def test_confirmation_continues_to_later_plan_and_reuses_one_trace(self):
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
        self.assertEqual(page.locator('.agent-tool-trace').count(), 1)
        self.assertEqual(page.locator('.agent-tool-trace .agent-stream-steps').count(), 1)
        self.assertIn('查询下载任务完成', ' '.join(page.locator('.agent-tool-trace .agent-stream-step').all_text_contents()))
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

        self.assertEqual(page.locator('.agent-tool-trace').count(), 1)
        self.assertEqual(page.locator('.agent-tool-trace .agent-stream-steps').count(), 1)
        self.assertEqual(page.locator('.agent-tool-trace .agent-stream-step').count(), 5)
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
        self.assertEqual(page.locator('.agent-tool-trace').count(), 1)
        self.assertEqual(page.locator('.agent-tool-trace .agent-stream-step[data-step-key^="effect:"]').count(), 1)
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
        effect_step = page.locator('.agent-tool-trace .agent-stream-step[data-step-key^="effect:"]')
        self.assertEqual(effect_step.count(), 1)
        self.assertIn('执行状态未知', effect_step.text_content())
        self.assertEqual(effect_step.locator('[data-lucide="check"]').count(), 0)

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
        self.assertTrue(page.locator('#agentStop').is_visible())
        self.assertTrue(page.locator('#agentSend').is_hidden())
        self.assertEqual(page.locator('.agent-streaming').count(), 1)

        page.evaluate("""() => {
          const original = window.fetch;
          window.fetch = async (url, options = {}) => {
            const path = new URL(String(url), location.href).pathname;
            if (path === '/api/agent/query/cancel') {
              const request = JSON.parse(options.body || '{}');
              window.__kernelCalls.push({url: path, method: 'POST', body: options.body || ''});
              window.__kernelConfig.sessionDetails ||= {};
              window.__kernelConfig.sessionDetails[request.session_id] = {
                session_id: request.session_id, generation: 1, draft_scope: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                messages: [{role: 'user', content: '暂停任务并等待实际状态'}],
                pending_approval: null, candidate_view: null, last_turn: null,
                active_turn: {request_id: request.request_id, turn_id: `turn-${request.request_id}`, generation: 1,
                  protected: true, status: 'running', detail: '安全排队中，等待实际状态回执'},
              };
              return new Response(JSON.stringify({cancelled: false}), {status: 200});
            }
            return original(url, options);
          };
        }""")
        page.locator('#agentStop').click()
        page.wait_for_function("() => document.querySelector('#agentResponseStatus').textContent.includes('未被接受')")
        page.wait_for_function("() => document.querySelector('#agentStop').hidden")
        self.assertEqual(page.locator('.agent-streaming').count(), 1)
        self.assertEqual(page.locator('.agent-cancelled, .is-interrupted').count(), 0)
        calls = page.evaluate('window.__kernelCalls')
        self.assertEqual(sum(call['url'] == '/api/agent/actions/confirm' for call in calls), 1)
        self.assertEqual(sum(call['url'] == '/api/agent/query' for call in calls), 1)
        cancel = next(call for call in calls if call['url'] == '/api/agent/query/cancel')
        confirm = next(call for call in calls if call['url'] == '/api/agent/actions/confirm')
        self.assertEqual(json.loads(cancel['body'])['session_id'], json.loads(confirm['body'])['session_id'])
        self.assertEqual(json.loads(cancel['body'])['request_id'], json.loads(confirm['body'])['request_id'])

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
                        'tool': 'library.search_missing_episode_resources', 'result': {'candidate_view': None},
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
