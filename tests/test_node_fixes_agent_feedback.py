"""真实确认结果在事件聚合、Telegram 和浏览器中的一致性。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agent.kernel.adapters import TurnView, consume_events
from app.agent.kernel.events import AgentEventType, EventFactory
from app.agent.kernel.state import AgentInput
from app.agent.models import ToolResult
from app.agent.public_view import format_public_result
from app.bot.agent_adapter import _render_turn
from tests.test_agent_kernel_adapters import event_stream
from tests import test_agent_kernel_browser as browser_tests
from tests.test_agent_kernel_browser import SESSION_ID, _event
from tests.test_agent_kernel_resource_ingest import SameTurnSearchSubmitModel, _collect, _runtime

WARNING = '请先核对下载器，勿直接重复提交'
FAILURE = {'ok': False, 'status': 'manual_review', 'summary': '下载提交结果未知',
           'error': WARNING, 'data': {'target': 'guangya', 'failed': 1}}


class AgentFailureProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_kernel_failure_keeps_dto_and_retry_warning_for_telegram(self):
        session, _, _ = _runtime(SameTurnSearchSubmitModel())
        with patch('app.agent.indexer_candidate_actions.prepare_submit_resource', return_value=(
            ToolResult(True, 'confirmation_required', '确认下载'), 'snapshot')), patch(
            'app.agent.indexer_candidate_actions.submit_resource_confirmed', return_value=ToolResult(
                False, 'manual_review', FAILURE['summary'], data=FAILURE['data'], error=WARNING)):
            first = await _collect(session.run(AgentInput(message='搜索资源并推送到云盘',
                owner='fixture-owner', session_id=SESSION_ID)))
            plan = next(e.payload['plan'] for e in first if e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED)
            events = await _collect(session.confirm(owner='fixture-owner', session_id=SESSION_ID,
                plan_id=plan['plan_id']))
        view = await consume_events(event_stream(events))
        self.assertEqual(view.status, 'failed')
        self.assertEqual(view.effect_result['status'], 'manual_review')
        self.assertIn(WARNING, view.error_message)
        self.assertIn('请先核对下载器', _render_turn(view))
        self.assertIn('勿直接重复提交', _render_turn(view))
        self.assertIn('光鸭云盘', _render_turn(view))
        self.assertNotIn('通过写后校验', _render_turn(view))

    async def test_failure_without_dto_keeps_specific_cause(self):
        factory = EventFactory(session_id='s', turn_id='t', request_id='r')
        view = await consume_events(event_stream([
            factory.create(AgentEventType.TURN_STARTED),
            factory.create(AgentEventType.EFFECT_FAILED, {'code': 'manual_review', 'message': WARNING}),
            factory.create(AgentEventType.TURN_FAILED, {'code': 'turn_failed', 'message': '已确认操作未能完成'}),
        ]))
        self.assertEqual(view.error_code, 'manual_review')
        self.assertEqual(view.error_message, WARNING)
        self.assertIn('请先核对下载器', _render_turn(view))
        self.assertIn('勿直接重复提交', _render_turn(view))

    async def test_empty_effect_result_is_not_verified_success_in_telegram(self):
        view = TurnView(session_id='s', turn_id='t', request_id='r', status='effect_completed')
        self.assertNotIn('通过写后校验', _render_turn(view))
        self.assertIn('勿直接重复提交', _render_turn(view))



class AgentBrowserDependencyTests(unittest.TestCase):
    def test_browser_skip_matches_playwright_availability(self):
        # 无依赖时不得进入setUpClass；有依赖时也不得把真实浏览器断言整体跳过。
        self.assertEqual(
            bool(getattr(AgentConfirmationBoundaryBrowserTests, '__unittest_skip__', False)),
            browser_tests.sync_playwright is None,
        )


@unittest.skipIf(browser_tests.sync_playwright is None, '系统环境未安装 Playwright')
class AgentConfirmationBoundaryBrowserTests(unittest.TestCase):
    # 复用已有浏览器夹具而不继承其测试，避免重复计算回归数量。
    setUpClass = classmethod(browser_tests.AgentKernelBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser_tests.AgentKernelBrowserTests.tearDownClass.__func__)
    make_page = browser_tests.AgentKernelBrowserTests.make_page

    def confirm(self, events, *, malformed_tail="", http_error=None):
        approval = {'plan_id': 'plan-result-boundary', 'tool_name': 'downloads.submit',
                    'effect': 'WRITE', 'preview': {'summary': '提交测试任务'}, 'result': {}}
        page = self.make_page({'sessions': {'sessions': [{'session_id': SESSION_ID,
                'title': '确认结果测试', 'message_count': 2}]},
            'sessionDetails': {SESSION_ID: {'session_id': SESSION_ID, 'messages': [],
                'pending_approval': approval}}, 'confirmEvents': events}, stored_session=SESSION_ID)
        interrupted = not http_error and not any(
            event['type'] in {'turn.failed', 'turn.cancelled'} or (
                event['type'] == 'turn.completed' and event.get('payload', {}).get('status')
                in {'success', 'partial', 'approval_required', 'effect_completed'}
            ) for event in events
        )
        if malformed_tail or interrupted:
            receipts = [format_public_result(event['payload']['result']) for event in events
                        if event['type'] in {'effect.completed', 'effect.failed'}
                        and isinstance(event.get('payload', {}).get('result'), dict)
                        and (event['payload']['result'].get('summary') or event['payload']['result'].get('error'))]
            page.evaluate("""({events, tail, receipts, interrupted}) => {
                const previous = window.fetch;
                window.fetch = async (...args) => {
                    if (String(args[0]) !== '/api/agent/actions/confirm') return previous(...args);
                    const request = JSON.parse(args[1].body);
                    window.__kernelCalls.push({url: String(args[0]), method: 'POST', body: args[1].body});
                    const normalized = events.map(event => ({...event,
                        request_id: request.request_id, session_id: request.session_id}));
                    if (interrupted) window.__kernelConfig.sessionDetails[request.session_id] = {
                        messages: receipts.map(content => ({role: 'assistant', content})),
                        active_turn: null, pending_approval: null,
                        last_turn: {request_id: request.request_id, turn_id: normalized[0]?.turn_id,
                            status: 'interrupted', message: '连接中断，后续结果尚未确认；可刷新会话核对状态，不要重复提交。'},
                    };
                    return new Response(normalized.map(event => JSON.stringify(event) + '\\n').join('') + tail,
                        {status: 200, headers: {'Content-Type': 'application/x-ndjson'}});
                };
            }""", {'events': events, 'tail': malformed_tail, 'receipts': receipts, 'interrupted': interrupted})
        elif http_error:
            page.evaluate("""payload => {
                const previous = window.fetch;
                window.fetch = async (...args) => String(args[0]) === '/api/agent/actions/confirm'
                    ? new Response(JSON.stringify(payload.body), {
                        status: payload.status,
                        headers: {'Content-Type': 'application/json'},
                    })
                    : previous(...args);
            }""", http_error)
        card = page.locator('.agent-confirmation-card')
        card.wait_for()
        card.locator('[data-effect-confirm]').click()
        if interrupted:
            page.get_by_text('连接中断，后续结果尚未确认；可刷新会话核对状态，不要重复提交。', exact=True).wait_for()
            self.assertEqual(page.evaluate("window.__kernelCalls.filter(c => c.url === '/api/agent/actions/confirm').length"), 1)
            return page.locator('#agentTranscript')
        result = page.locator('.agent-result-card:not(.agent-streaming)')
        result.wait_for()
        return result

    def test_missing_or_malformed_effect_completion_reports_unknown_not_success(self):
        for suffix in ([], [_event(3, 'turn.completed', {'status': 'effect_completed'})],
                       [_event(3, 'effect.completed', {'result': {}})],
                       [_event(3, 'effect.completed', {'result': []})]):
            with self.subTest(suffix=suffix):
                result = self.confirm([_event(1, 'turn.started', {'kind': 'confirmation'}),
                    _event(2, 'tool.started', {'kind': 'confirmed_effect'}), *suffix])
                text = result.inner_text()
                self.assertNotIn('通过写后校验', text)
                self.assertNotIn('✅', text)
                self.assertIn('连接中断，后续结果尚未确认', text)
                self.assertIn('可刷新会话核对', text)
                self.assertIn('不要重复提交', text)
                self.assertTrue(result.evaluate("el => el.matches('.is-interrupted') || !!el.querySelector('.is-interrupted')"))

    def test_failed_dto_survives_generic_turn_failure(self):
        result = self.confirm([_event(1, 'turn.started', {}),
            _event(2, 'effect.failed', {'result': FAILURE, 'message': WARNING}),
            _event(3, 'turn.failed', {'message': '已确认操作未能完成'})])
        self.assertIn(WARNING, result.inner_text())
        self.assertIn('光鸭云盘', result.inner_text())
        self.assertIn('提交结果未知', result.inner_text())
        self.assertTrue(result.evaluate("el => el.matches('.is-interrupted') || !!el.querySelector('.is-interrupted')"))

    def test_isolated_effect_completion_preserves_receipt_but_reports_unknown_eof(self):
        result = self.confirm([_event(1, 'turn.started', {}), _event(2, 'effect.completed',
            {'result': {'ok': True, 'status': 'success', 'summary': '任务已暂停'}})])
        text = result.inner_text()
        self.assertIn('任务已暂停', text)
        self.assertIn('连接中断，后续结果尚未确认', text)
        self.assertIn('不要重复提交', text)
        self.assertNotIn('通过写后校验', text)
        self.assertNotIn('is-interrupted', result.locator('.agent-result-card').filter(has_text='任务已暂停').get_attribute('class'))

    def test_explicit_effect_completed_turn_terminal_renders_without_model_followup(self):
        cases = (
            ([_event(1, 'turn.started', {}), _event(2, 'turn.completed', {
                'status': 'effect_completed', 'answer': '直接计划已完成，返回确定性回执。'})],
             '直接计划已完成，返回确定性回执'),
            ([_event(1, 'turn.started', {}), _event(2, 'effect.completed', {
                'result': {'ok': True, 'status': 'success', 'summary': 'DTO 计划已完成'}}),
             _event(3, 'turn.completed', {'status': 'effect_completed'})],
             'DTO 计划已完成'),
        )
        for events, expected in cases:
            with self.subTest(expected=expected):
                result = self.confirm(events)
                text = result.inner_text()
                self.assertIn(expected, text)
                self.assertNotIn('连接中断', text)
                self.assertNotIn('is-interrupted', result.get_attribute('class'))

    def test_failed_effect_without_dto_keeps_specific_cause(self):
        result = self.confirm([_event(1, 'turn.started', {}),
            _event(2, 'effect.failed', {'message': WARNING}),
            _event(3, 'turn.failed', {'message': '已确认操作未能完成'})])
        self.assertIn(WARNING, result.inner_text())

    def test_turn_failure_without_effect_stays_failure(self):
        result = self.confirm([_event(1, 'turn.failed', {'message': '计划已失效，请重新预览'})])
        self.assertIn('计划已失效', result.inner_text())
        self.assertTrue(result.evaluate("el => el.matches('.is-interrupted') || !!el.querySelector('.is-interrupted')"))

    def test_transport_error_after_trusted_effect_does_not_erase_business_result(self):
        for kind, payload in (('effect.completed', {'result': {'ok': True, 'summary': '任务已暂停'}}),
                              ('effect.failed', {'result': FAILURE, 'message': WARNING})):
            with self.subTest(kind=kind):
                result = self.confirm([_event(1, 'turn.started', {}), _event(2, kind, payload)],
                                      malformed_tail='{invalid-json')
                text = result.inner_text()
                if kind == 'effect.completed':
                    self.assertIn('任务已暂停', text)
                    self.assertIn('连接中断，后续结果尚未确认', text)
                    self.assertNotIn('is-interrupted', result.locator('.agent-result-card').filter(has_text='任务已暂停').get_attribute('class'))
                else:
                    self.assertIn(WARNING.replace('，', ','), text)
                    self.assertIn('提交结果未知', text)
                    self.assertTrue(result.evaluate("el => el.matches('.is-interrupted') || !!el.querySelector('.is-interrupted')"))

    def test_transport_error_before_terminal_reports_unknown_not_success(self):
        result = self.confirm([_event(1, 'turn.started', {})], malformed_tail='{invalid-json')
        text = result.inner_text()
        self.assertIn('连接中断，后续结果尚未确认', text)
        self.assertIn('可刷新会话核对状态', text)
        self.assertIn('不要重复提交', text)
        self.assertNotIn('✅', text)
        self.assertTrue(result.evaluate("el => el.matches('.is-interrupted') || !!el.querySelector('.is-interrupted')"))

    def test_http_error_preserves_backend_reason_and_no_duplicate_submit_warning(self):
        for status, reason in ((400, '请求参数已失效，请重新核对计划'), (409, '计划已过期，请核对当前状态')):
            with self.subTest(status=status):
                result = self.confirm([], http_error={
                    'status': status,
                    'body': {'error': reason},
                })
                text = result.inner_text()
                self.assertIn(reason, text)
                self.assertIn('请核对状态，不要重复提交', text)
                self.assertNotIn('Expected property name', text)
                self.assertTrue(result.evaluate("el => el.matches('.is-interrupted') || !!el.querySelector('.is-interrupted')"))
