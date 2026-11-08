"""真实 Chromium：编辑/限定信任复用原工作台，普通 Web 会话保持隔离；网络仅本地fixture。"""
from __future__ import annotations

import json
import time
import unittest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Browser

from tests import test_agent_kernel_browser as support


def approval(plan_id="editable-original"):
    return {"plan_id": plan_id, "tool_name": "watchlist.add", "effect": "WRITE",
            "confirmation": {"action": "加入探索收藏"},
            "preview": {"summary": "待确认的本地收藏"}, "expires_at": "2099-10-17T12:05:00+00:00",
            "editing": {"schema": {"type": "object", "properties": {"label": {"type": "string"}}}, "values": {"label": "A"}},
            "trust_eligible": True, "trust_scope": {"provider": "tmdb", "media_type": "tv"}}


ACTION_FETCH = """config => {
    const original = window.fetch;
    window.__actionConfig = config;
    window.fetch = async (url, options = {}) => {
        const path = new URL(url, location.href).pathname;
        const headers = Object.fromEntries(new Headers(options.headers || {}).entries());
        window.__headers ||= [];
        window.__headers.push({path, headers, body: options.body || ''});
        let payload, status = 200;
        if (path === '/api/agent/actions/confirm/edit') {
            payload = config.edit || {plan: null}; status = config.editStatus || 200;
        } else if (path === '/api/agent/actions/trust') {
            payload = JSON.parse(options.body).operation === 'revoke' ? {active: false} : config.trust;
        } else return original(url, options);
        window.__kernelCalls.push({url: path, method: options.method || 'GET', body: options.body || ''});
        if (config.hold) await new Promise(resolve => { window.__releaseAction = resolve; });
        return new Response(JSON.stringify(payload), {status, headers: {'Content-Type': 'application/json'}});
    };
}"""


@unittest.skipIf(support.sync_playwright is None, "需要 Playwright")
class ConfirmationEditBrowserTests(unittest.TestCase):
    browser: Browser
    source: str
    styles: str

    @classmethod
    def setUpClass(cls):
        support.AgentKernelBrowserTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        support.AgentKernelBrowserTests.tearDownClass.__func__(cls)

    make_page = support.AgentKernelBrowserTests.make_page

    def page(self, *, width=390, extra=None, action=None):
        config = {"sessions": {"sessions": [{"session_id": support.SESSION_ID, "title": "编辑测试"}]},
                  "sessionDetails": {support.SESSION_ID: {"messages": [{"role": "user", "content": "收藏A"}], "pending_approval": approval()}},
                  **(extra or {})}
        page = self.make_page(config, stored_session=support.SESSION_ID, viewport={"width": width, "height": 844})
        page.locator('.agent-confirmation-card').wait_for()
        page.evaluate(ACTION_FETCH, action or {})
        return page

    def test_keyboard_cancel_editor_and_trust_never_send_or_leave_dirty_confirm(self):
        page = self.page()
        editor = page.locator('.agent-confirmation-editor').filter(has=page.get_by_text('修改参数', exact=True))
        editor.locator('summary').click()
        field = editor.locator('input')
        field.fill('B')
        self.assertTrue(page.locator('[data-effect-confirm]').is_disabled())
        field.press('Escape')
        self.assertFalse(editor.evaluate('node => node.open'))
        self.assertEqual(field.input_value(), 'A')
        self.assertFalse(page.locator('[data-effect-confirm]').is_disabled())
        self.assertTrue(editor.locator('summary').evaluate('node => node === document.activeElement'))
        trust = page.locator('.agent-confirmation-editor').filter(has=page.get_by_text('限定低风险预授权（默认关闭）', exact=True))
        trust.locator('summary').click()
        trust.locator('input').first.check()
        trust.locator('input').first.press('Escape')
        self.assertFalse(trust.evaluate('node => node.open'))
        self.assertFalse(trust.locator('input').first.is_checked())
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.url.endsWith('/edit') || c.url.endsWith('/trust')).length"), 0)

    def test_edit_only_sends_patch_and_new_card_replaces_old(self):
        new = approval('editable-replacement')
        new['editing']['values']['label'] = 'B'
        page = self.page(action={"edit": {"plan": new, "old_plan_invalidated": True}})
        editor = page.locator('.agent-confirmation-editor').first
        editor.locator('summary').click()
        editor.locator('input').fill('B')
        editor.locator('button[type=submit]').click()
        page.locator('[data-effect-confirm="editable-replacement"]').wait_for()
        self.assertEqual(page.locator('[data-effect-confirm="editable-original"]').count(), 0)
        call = page.evaluate("window.__kernelCalls.find(c => c.url.endsWith('/edit'))")
        body = json.loads(call['body'])
        self.assertEqual(body['arguments'], {'label': 'B'})
        self.assertEqual(body['session_id'], support.SESSION_ID)
        self.assertNotIn('/api/agent/actions/confirm', [c['url'] for c in page.evaluate('window.__kernelCalls')])

    def test_failed_or_no_change_edit_explicitly_invalidates_original_card(self):
        for action, message in [({"edit": {"plan": None}}, '旧卡已失效'),
                                ({"editStatus": 409, "edit": {"error": "预检拒绝", "old_plan_invalidated": True}}, '修改失败，旧卡已失效')]:
            with self.subTest(action=action):
                page = self.page(action=action)
                editor = page.locator('.agent-confirmation-editor').first
                editor.locator('summary').click()
                editor.locator('input').fill('B')
                editor.locator('button[type=submit]').click()
                page.locator('.agent-confirmation-card.is-expired').wait_for()
                self.assertIn(message, editor.inner_text())
                self.assertTrue(page.locator('[data-effect-confirm]').is_disabled())
                self.assertNotIn('/api/agent/actions/confirm', [c['url'] for c in page.evaluate('window.__kernelCalls')])

    def test_explicit_trust_grant_persists_outside_card_and_revoke_or_switch_hides_it(self):
        grant = {"active": True, "tool_name": "watchlist.add", "scope": {"provider": "tmdb", "media_type": "tv"},
                 "remaining": 5, "expires_at": time.time() + 300}
        page = self.page(action={"trust": grant})
        trust = page.locator('.agent-confirmation-editor').nth(1)
        trust.locator('summary').click()
        self.assertIn('允许动作：加入探索收藏', trust.inner_text())
        self.assertNotIn('检索媒体信息', trust.inner_text())
        trust.locator('button[type=submit]').click()
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.url.endsWith('/trust')).length"), 0)
        for checkbox in trust.locator('input[type=checkbox]').all():
            checkbox.check()
        trust.locator('select').nth(0).select_option('5')
        trust.locator('select').nth(1).select_option('300')
        trust.locator('button[type=submit]').click()
        bar = page.locator('.agent-scoped-trust-bar').filter(has_text='剩余 5 次')
        bar.wait_for()
        body = json.loads(page.evaluate("window.__kernelCalls.find(c => c.url.endsWith('/trust')).body"))
        self.assertEqual(body['scope'], grant['scope'])
        self.assertEqual(body['tool_name'], grant['tool_name'])
        self.assertEqual(body['max_actions'], 5)
        self.assertEqual(body['ttl_seconds'], 300)
        self.assertEqual(page.locator('.agent-composer-header button, .agent-composer-header a').count(), 6)
        page.locator('[data-effect-cancel]').click()
        page.locator('.agent-cancelled').wait_for()
        self.assertTrue(bar.is_visible())
        bounding_box_result = bar.locator('button').bounding_box()
        assert bounding_box_result is not None
        self.assertGreaterEqual(bounding_box_result['height'], 44)
        bar.locator('button').click()
        page.wait_for_function("() => ![...document.querySelectorAll('.agent-scoped-trust-bar')].some(n => !n.hidden)")
        page.close()
        page = self.page(extra={"sessionDetails": {support.SESSION_ID: {"messages": [{"role": "user", "content": "A"}], "pending_approval": approval(), "scoped_trust": grant}}})
        page.locator('.agent-scoped-trust-bar').filter(has_text='剩余 5 次').wait_for()
        page.locator('#agentNewSession').click()
        self.assertFalse(page.locator('.agent-scoped-trust-bar').filter(has_text='剩余 5 次').is_visible())

    def test_mobile_editor_and_trust_busy_layout_stays_within_half_pixel(self):
        for width in (320, 360, 390):
            for index in (0, 1):
                with self.subTest(width=width, editor=index):
                    page = self.page(width=width, action={"hold": True, "trust": {"active": False}})
                    details = page.locator('.agent-confirmation-editor').nth(index)
                    details.locator('summary').click()
                    if index == 0:
                        details.locator('input').fill('B')
                    else:
                        for checkbox in details.locator('input[type=checkbox]').all():
                            checkbox.check()
                        details.locator('select').nth(0).select_option('5')
                        details.locator('select').nth(1).select_option('300')
                    measure = """node => {
                        const form = node.querySelector('form'), r = form.getBoundingClientRect();
                        return {height:r.height, buttons:[...form.querySelectorAll('button')].map(b => {const v=b.getBoundingClientRect(); return {x:v.x-r.x,y:v.y-r.y,width:v.width,height:v.height};}),
                          overflow:document.documentElement.scrollWidth-innerWidth,
                          controls:[...node.querySelectorAll('input:not([type=checkbox]),select,button,summary')].map(c=>c.getBoundingClientRect().height)};
                    }"""
                    before = details.evaluate(measure)
                    details.locator('form').evaluate('form => form.requestSubmit()')
                    page.wait_for_function('() => typeof window.__releaseAction === "function"')
                    after = details.evaluate(measure)
                    self.assertLessEqual(after['overflow'], .5)
                    self.assertTrue(all(height >= 44 for height in before['controls']))
                    self.assertLessEqual(abs(before['height'] - after['height']), .5)
                    for first, second in zip(before['buttons'], after['buttons']):
                        self.assertTrue(all(abs(first[key] - second[key]) <= .5 for key in first))
                    page.evaluate('window.__releaseAction()')
                    page.close()

    def test_removed_tg_handoff_has_no_entry_or_authority_over_web_sessions(self):
        from playwright.sync_api import expect

        legacy = {'binding_id': 'b' * 64, 'session_id': 'tg_handoff_browser_session_01',
                  'source': 'telegram', 'expires_at': time.time() + 1800}
        key = 'mediaflux.agent.handoff.v1.' + 'a' * 64
        events = [support._event(1, 'turn.started', {'kind': 'query'}),
                  support._event(2, 'turn.completed', {'status': 'success', 'answer': '只读结束'})]
        page = self.page(extra={'queryEvents': events})
        page.evaluate('([key, value]) => sessionStorage.setItem(key, JSON.stringify(value))', [key, legacy])
        config = page.evaluate('window.__kernelConfig')
        page.add_init_script(f'({support.MOCK_FETCH})({json.dumps(config)}); ({ACTION_FETCH})({{}});')
        page.reload()
        page.add_style_tag(content=self.styles)
        page.add_script_tag(content=self.source)
        page.locator('.agent-confirmation-card').wait_for()
        self.assertIn('收藏A', page.locator('#agentTranscript').inner_text())
        page.locator('#toggleAgentRail').click()
        self.assertEqual(page.locator('#agentHandoffPanel, [data-handoff-active]').count(), 0)
        self.assertEqual(page.get_by_text('TG 单会话续接', exact=True).count(), 0)
        page.locator('[data-agent-history-close]').click()
        for count, message in enumerate(('只读查询', '新的Web查询'), 1):
            if count == 2:
                page.locator('#agentNewSession').click()
            page.locator('#agentPrompt').fill(message)
            expect(page.locator('#agentSend')).to_be_enabled()
            page.locator('#agentSend').click()
            page.wait_for_function(f"() => window.__headers.filter(c=>c.path==='/api/agent/query').length==={count}")
            page.wait_for_function("() => document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
        calls = page.evaluate('window.__headers')
        queries = [json.loads(call['body']) for call in calls if call['path'] == '/api/agent/query']
        self.assertEqual([row['message'] for row in queries], ['只读查询', '新的Web查询'])
        self.assertEqual(queries[0]['session_id'], support.SESSION_ID)
        self.assertNotEqual(queries[1]['session_id'], support.SESSION_ID)
        self.assertIn('/api/agent/sessions/' + support.SESSION_ID, [call['path'] for call in calls])
        for call in calls:
            self.assertNotIn('x-agent-handoff', call['headers'])
            self.assertFalse(call['path'].startswith('/api/agent/handoffs/'))
            self.assertNotIn(legacy['session_id'], call['path'] + call['body'])
        self.assertEqual(page.evaluate('key => JSON.parse(sessionStorage.getItem(key))', key), legacy)

    def test_confirmation_execution_collapses_both_same_session_forms(self):
        page = self.page(extra={'confirmEvents': [support._event(1, 'turn.started', {'kind': 'confirmation'})], 'holdConfirmOpen': True})
        for details in page.locator('.agent-confirmation-card .agent-confirmation-editor').all():
            details.locator('summary').click()
        page.locator('[data-effect-confirm]').click()
        page.locator('.agent-confirmation-executing').wait_for()
        self.assertEqual(page.locator('.agent-confirmation-editor[open]').count(), 0)
        self.assertEqual(page.locator('.agent-confirmation-card input:enabled,.agent-confirmation-card select:enabled').count(), 0)
