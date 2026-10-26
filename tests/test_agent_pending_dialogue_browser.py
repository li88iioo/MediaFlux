"""真实 Agent 页面/Chromium 的待确认卡回归；API/事件均为 mock，不调用真实 LLM。"""

import json

from tests.test_agent_kernel_browser import _event
from tests.test_agent_ux_browser import candidate_view
from tests.test_agent_ux_shell_browser import AgentShellBrowserCase

PLAN_A = "plan_pending_dialogue_A_0001"
PLAN_B = "plan_pending_dialogue_B_0002"


def approval(plan_id=PLAN_A):
    return {
        "plan_id": plan_id,
        "tool_name": "download.pause",
        "effect": "WRITE",
        "preview": {"summary": "核对后暂停选定的下载任务。"},
        "confirmation": {"action": "暂停下载任务", "impact": "仅影响选定任务。"},
        "expires_at": "2099-10-15T12:00:00+08:00",
    }


def preview_events(plan):
    return [
        _event(1, "turn.started", {"kind": "query"}),
        _event(2, "effect.approval_required", {"tool": plan["tool_name"], "plan": plan}),
        _event(3, "turn.completed", {"status": "approval_required", "answer": ""}),
    ]


def read_events():
    return [
        _event(1, "turn.started", {"kind": "query"}),
        _event(2, "tool.completed", {"tool": "downloads.list", "result": {"ok": True}}),
        _event(3, "turn.completed", {"status": "success", "answer": "已完成只读讨论，没有修改计划。"}),
    ]


class AgentPendingDialogueBrowserTests(AgentShellBrowserCase):
    def dialogue_page(self, viewport=None):
        # 复用真实 Jinja/base、静态资源路由和 Chromium；所有 API 仍由本地夹具响应。
        page, unexpected, errors = self.make_shell(
            viewport or {"width": 1280, "height": 800}, history=False
        )
        self.addCleanup(self.assertEqual, unexpected, [])
        self.addCleanup(self.assertEqual, errors, [])
        page.evaluate(
            """plan => {
                window.__dialogueSnapshot = {pending_approval: plan};
                window.__dialogueMode = '';
                window.__dialogueReads = 0;
                const fixtureFetch = window.fetch;
                window.fetch = async (url, options = {}) => {
                    const path = String(url);
                    if (!path.startsWith('/api/agent/sessions/') || (options.method || 'GET') !== 'GET') {
                        return fixtureFetch(url, options);
                    }
                    // 与真实 HTTP 一样按大小写不敏感的 Headers 读取，支持所有 HeadersInit 形式。
                    const headers = new Headers(options.headers || {});
                    window.__kernelCalls.push({url: path, method: 'GET', body: '', headers: Object.fromEntries(headers.entries())});
                    window.__dialogueReads++;
                    const mode = window.__dialogueMode;
                    if (mode === 'network') throw new TypeError('mock network failure');
                    if (mode === 'http') return new Response('{}', {status: 503});
                    if (mode === 'malformed') return new Response('{');
                    if (mode === 'timeout') await new Promise((resolve, reject) => {
                        options.signal.addEventListener('abort', () => reject(new DOMException('mock timeout', 'AbortError')), {once: true});
                    });
                    const requestId = headers.get('X-Agent-Request-Id');
                    const terminalType = window.__kernelConfig.queryEvents.at(-1)?.type;
                    const payload = {
                        session_id: decodeURIComponent(path.split('/').pop()), generation: 2,
                        messages: [{role: 'assistant', content: '此详情正文不得重建到当前页面'}],
                        pending_effect_count: 0, active_turn: null,
                        last_turn: {request_id: requestId, turn_id: `turn-${requestId}`,
                            status: terminalType === 'turn.failed' ? 'failed' : terminalType === 'turn.cancelled' ? 'cancelled' : 'completed'},
                        ...window.__dialogueSnapshot,
                    };
                    if (mode === 'missing') delete payload.pending_approval;
                    if (mode === 'foreign') payload.session_id = 'session_other_dialogue_0001';
                    if (mode === 'no_terminal') payload.last_turn = null;
                    if (mode === 'old_terminal') payload.last_turn.request_id = 'older-request';
                    if (mode === 'protected' || mode === 'other_active') payload.active_turn = {
                        request_id: mode === 'protected' ? requestId : 'other-request',
                        turn_id: `turn-${requestId}`, protected: mode === 'protected', status: 'running',
                    };
                    if (mode === 'deferred') {
                        // 故意忽略 abort，模拟切会话后仍晚到的旧快照。
                        await new Promise(resolve => { window.__releaseDialogueSnapshot = resolve; });
                        window.__dialogueLateReturned = true;
                    }
                    return new Response(JSON.stringify(payload), {headers: {'Content-Type': 'application/json'}});
                };
            }""",
            approval(),
        )
        self.send(page, preview_events(approval()), approval(), "准备原计划")
        page.evaluate(
            """() => {
                window.__originalApproval = document.querySelector('.agent-confirmation-card');
                window.__originalButtons = [...window.__originalApproval.querySelectorAll('button')];
                window.__originalMessage = document.querySelector('.agent-message');
                window.__maxActiveApprovals = 1;
                new MutationObserver(() => {
                    window.__maxActiveApprovals = Math.max(window.__maxActiveApprovals,
                        document.querySelectorAll('.agent-confirmation-card:not(.is-expired)').length);
                }).observe(document.querySelector('#agentTranscript'), {childList: true, subtree: true, attributes: true, attributeFilter: ['class']});
            }"""
        )
        return page

    def send(self, page, events, pending, question="继续讨论", *, mode="", wait=True):
        page.evaluate(
            """({events, pending, mode}) => {
                window.__kernelConfig.queryEvents = events;
                window.__kernelConfig.queryDelayMs = 35;
                window.__dialogueSnapshot = {pending_approval: pending};
                window.__dialogueMode = mode;
            }""",
            {"events": events, "pending": pending, "mode": mode},
        )
        page.locator("#agentPrompt").fill(question)
        page.locator("#agentSend").click()
        if wait:
            page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")

    def assert_original_card(self, page, *, retired=False):
        self.assertTrue(page.evaluate("window.__originalApproval.isConnected"))
        self.assertTrue(page.evaluate(
            """() => window.__originalButtons.every((button, i) =>
                button === window.__originalApproval.querySelectorAll('button')[i])"""
        ))
        card = page.locator(f'[data-plan-id="{PLAN_A}"]')
        self.assertEqual(card.count(), 1)
        self.assertTrue(card.evaluate("node => node === window.__originalApproval"))
        self.assertEqual(card.evaluate("node => node.classList.contains('is-expired')"), retired)
        for button in card.locator("button").all():
            self.assertEqual(button.is_disabled(), retired)
        self.assertTrue(page.evaluate("window.__originalMessage.isConnected"))
        self.assertNotIn("此详情正文不得重建到当前页面", page.locator("#agentTranscript").inner_text())

    def test_read_keeps_original_card_and_buttons_then_confirms_same_plan(self):
        for viewport in ({"width": 1280, "height": 800}, {"width": 390, "height": 844}):
            with self.subTest(viewport=viewport):
                page = self.dialogue_page(viewport)
                card = page.locator(f'[data-plan-id="{PLAN_A}"]')
                before = card.bounding_box()
                assert before is not None
                reads = page.evaluate("window.__dialogueReads")
                self.send(page, read_events(), approval())
                self.assert_original_card(page)
                self.assertEqual(page.evaluate("window.__dialogueReads"), reads + 1)
                after = card.bounding_box()
                assert after is not None
                for dimension in ("width", "height"):
                    self.assertAlmostEqual(before[dimension], after[dimension], delta=0.5)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), viewport["width"])
                page.evaluate("events => window.__kernelConfig.confirmEvents = events", [
                    _event(1, "turn.started", {"kind": "confirmation"}),
                    _event(2, "effect.completed", {
                        "plan_id": PLAN_A, "result": {"ok": True, "summary": "原冻结计划执行完成"},
                        "receipt": "原冻结计划执行完成",
                    }),
                    _event(3, "turn.completed", {"status": "success", "answer": "原冻结计划执行完成"}),
                ])
                card.locator("[data-effect-confirm]").click()
                page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
                calls = page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm')")
                self.assertEqual(len(calls), 1)
                self.assertEqual(json.loads(calls[0]["body"])["plan_id"], PLAN_A)

    def test_reconciliation_preserves_request_identity_across_header_forms(self):
        for header_form in ("lowercase", "mixed_case", "headers", "pairs"):
            with self.subTest(header_form=header_form):
                page = self.dialogue_page()
                page.evaluate(
                    """headerForm => {
                        const fixtureFetch = window.fetch;
                        window.fetch = (url, options = {}) => {
                            if (!String(url).startsWith('/api/agent/sessions/') || (options.method || 'GET') !== 'GET') {
                                return fixtureFetch(url, options);
                            }
                            const normalized = new Headers(options.headers || {});
                            const entries = [...normalized.entries()].map(([key, value]) => [
                                key === 'x-agent-request-id' ? 'x-AgEnT-rEqUeSt-Id' : key, value,
                            ]);
                            const headers = headerForm === 'headers' ? normalized
                                : headerForm === 'pairs' ? entries
                                : headerForm === 'mixed_case' ? Object.fromEntries(entries)
                                : Object.fromEntries(normalized.entries());
                            return fixtureFetch(url, {...options, headers});
                        };
                    }""",
                    header_form,
                )
                self.send(page, read_events(), None, "核对已取消的计划")
                calls = page.evaluate("window.__kernelCalls")
                query = next(call for call in reversed(calls) if call["url"] == "/api/agent/query")
                snapshot = next(call for call in reversed(calls) if call["url"].startswith("/api/agent/sessions/") and call["method"] == "GET")
                request_id = json.loads(query["body"])["request_id"]
                self.assertTrue(request_id)
                self.assertEqual(snapshot["headers"].get("x-agent-request-id"), request_id)
                self.assert_original_card(page, retired=True)

    def test_model_cancel_retires_original_card_even_if_final_answer_fails(self):
        for terminal in (
            _event(3, "turn.completed", {"status": "success", "answer": "本轮已结束。"}),
            _event(3, "turn.failed", {"message": "取消之后答复失败"}),
        ):
            with self.subTest(terminal=terminal["type"]):
                page = self.dialogue_page()
                self.send(page, [
                    _event(1, "turn.started", {"kind": "query"}),
                    _event(2, "tool.completed", {"tool": "agent.pending_plan", "result": {
                        "ok": True, "data": {"pending": False, "cancelled": True},
                    }}),
                    terminal,
                ], None, "取消原计划")
                self.assert_original_card(page, retired=True)
                status = page.locator(f'[data-plan-id="{PLAN_A}"] .agent-confirmation-preflight').inner_text()
                self.assertIn("取消或失效", status)
                self.assertNotIn("替代", status)

    def test_new_approval_retires_only_old_id_without_two_active_cards_even_if_get_fails(self):
        page = self.dialogue_page()
        self.send(page, preview_events(approval(PLAN_B)), approval(PLAN_B), "修改计划", mode="network")
        self.assert_original_card(page, retired=True)
        new_card = page.locator(f'[data-plan-id="{PLAN_B}"]')
        self.assertTrue(new_card.locator("[data-effect-confirm]").is_enabled())
        self.assertEqual(page.locator(".agent-confirmation-card:not(.is-expired)").count(), 1)
        self.assertEqual(page.evaluate("window.__maxActiveApprovals"), 1)

    def test_prepare_failure_or_no_changes_without_new_card_retires_old_pending(self):
        for terminal in (
            _event(3, "turn.failed", {"message": "新预检失败，没有生成新计划"}),
            _event(3, "turn.completed", {"status": "success", "answer": "no_changes，无需生成新计划"}),
        ):
            with self.subTest(terminal=terminal["type"]):
                page = self.dialogue_page()
                self.send(page, [
                    _event(1, "turn.started", {"kind": "query"}),
                    _event(2, "effect.preview_started", {"tool": "download.pause"}),
                    terminal,
                ], None, "修改原计划")
                self.assert_original_card(page, retired=True)
                self.assertEqual(page.locator(".agent-confirmation-card").count(), 1)

    def test_failed_missing_or_protected_snapshot_keeps_old_card_without_false_cancellation(self):
        for mode in ("network", "http", "malformed", "missing", "foreign", "protected", "other_active", "no_terminal", "old_terminal", "timeout"):
            with self.subTest(mode=mode):
                page = self.dialogue_page()
                self.send(page, read_events(), None, mode=mode)
                self.assert_original_card(page)
                status = page.locator(f'[data-plan-id="{PLAN_A}"] .agent-confirmation-preflight').inner_text()
                self.assertNotRegex(status, "取消|失效|替代")

    def test_read_failure_or_stop_keeps_card_when_snapshot_still_has_original_plan(self):
        for terminal in (
            _event(2, "turn.failed", {"message": "只读讨论失败"}),
            _event(2, "turn.cancelled", {"reason": "user_cancelled"}),
        ):
            with self.subTest(terminal=terminal["type"]):
                page = self.dialogue_page()
                self.send(page, [_event(1, "turn.started", {"kind": "query"}), terminal], approval())
                self.assert_original_card(page)

    def test_candidate_preview_reconciles_old_card_only_after_real_pending_change(self):
        for pending in (approval(), None, approval(PLAN_B)):
            with self.subTest(pending=pending and pending["plan_id"]):
                page = self.dialogue_page()
                self.send(page, [
                    _event(1, "turn.started", {"kind": "query"}),
                    _event(2, "tool.completed", {"tool": "resource.search", "result": {"candidate_view": candidate_view()}}),
                    _event(3, "turn.completed", {"status": "success", "answer": "候选已找到。"}),
                ], approval())
                events = preview_events(pending) if pending and pending["plan_id"] == PLAN_B else [
                    _event(1, "turn.started", {"kind": "query"}),
                    _event(2, "turn.failed", {"message": "预检没有生成新卡"}),
                ]
                page.evaluate(
                    """({events, pending}) => {
                        window.__kernelConfig.queryEvents = events;
                        window.__dialogueSnapshot = {pending_approval: pending};
                    }""",
                    {"events": events, "pending": pending},
                )
                page.locator("[data-candidate-select]").click()
                page.wait_for_function("document.querySelector('[data-candidate-select]').getAttribute('aria-busy') === 'false'")
                self.assert_original_card(page, retired=pending is None or pending["plan_id"] != PLAN_A)
                self.assertEqual(page.evaluate("window.__maxActiveApprovals"), 1)
                if pending and pending["plan_id"] == PLAN_B:
                    self.assertTrue(page.locator(f'[data-effect-confirm="{PLAN_B}"]').is_enabled())

    def test_late_old_snapshot_does_not_retire_card_in_new_session(self):
        page = self.dialogue_page()
        self.send(page, read_events(), None, mode="deferred", wait=False)
        page.wait_for_function("typeof window.__releaseDialogueSnapshot === 'function'")
        page.locator("#agentNewSession").click()
        self.send(page, preview_events(approval(PLAN_B)), approval(PLAN_B), "准备另一个会话的计划")
        page.evaluate("window.__releaseDialogueSnapshot()")
        page.wait_for_function("window.__dialogueLateReturned === true")
        self.assertTrue(page.locator(f'[data-effect-confirm="{PLAN_B}"]').is_enabled())
        self.assertEqual(page.locator(".agent-confirmation-card:not(.is-expired)").count(), 1)

    def reject_next_confirmation(self, page):
        page.evaluate("""() => {
            const fixtureFetch = window.fetch;
            let rejected = false;
            window.fetch = async (url, options = {}) => {
                if (String(url) !== '/api/agent/actions/confirm' || rejected) return fixtureFetch(url, options);
                rejected = true;
                window.__kernelCalls.push({url: String(url), method: 'POST', body: options.body});
                return new Response(JSON.stringify({error: 'Agent 请求过于频繁，请稍后重试'}), {
                    status: 429, headers: {'Content-Type': 'application/json'},
                });
            };
        }""")

    def test_rejected_confirmation_rechecks_same_pending_plan_before_manual_retry(self):
        for width, mode in ((1280, "no_terminal"), (390, "old_terminal")):
            with self.subTest(width=width, mode=mode):
                page = self.dialogue_page({"width": width, "height": 844})
                self.reject_next_confirmation(page)
                page.evaluate("mode => window.__dialogueMode = mode", mode)
                reads = page.evaluate("window.__dialogueReads")
                page.locator("[data-effect-confirm]").click()
                page.wait_for_function("document.getElementById('agentSend').getAttribute('aria-busy') === 'false'")
                self.assertEqual(page.evaluate("window.__dialogueReads"), reads + 1)
                self.assertTrue(page.locator(f'[data-effect-confirm="{PLAN_A}"]').is_enabled())
                self.assertTrue(page.locator(f'[data-effect-cancel="{PLAN_A}"]').is_enabled())
                self.assertEqual(page.locator(".agent-confirmation-card:not(.is-expired)").count(), 1)
                self.assertIn("请求过于频繁", page.locator(".is-interrupted").inner_text())
                self.assertIn("尚未执行", page.locator("#agentTranscript").inner_text())
                self.assertNotIn("此详情正文不得重建到当前页面", page.locator("#agentTranscript").inner_text())
                calls = page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm')")
                self.assertEqual(len(calls), 1)
                snapshot = page.evaluate("window.__kernelCalls.filter(call => call.url.startsWith('/api/agent/sessions/') && call.method === 'GET').at(-1)")
                self.assertEqual(snapshot["headers"]["x-agent-request-id"], json.loads(calls[0]["body"])["request_id"])
                page.evaluate("events => window.__kernelConfig.confirmEvents = events", [
                    _event(1, "turn.started", {"kind": "confirmation"}),
                    _event(2, "effect.completed", {"plan_id": PLAN_A, "receipt": "人工重试已执行一次", "result": {"ok": True}}),
                    _event(3, "turn.completed", {"status": "success", "answer": "人工重试已执行一次"}),
                ])
                page.locator(f'[data-effect-confirm="{PLAN_A}"]').click()
                page.wait_for_function("document.getElementById('agentSend').getAttribute('aria-busy') === 'false'")
                calls = page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm')")
                self.assertEqual(len(calls), 2)
                bodies = [json.loads(call["body"]) for call in calls]
                self.assertEqual([body["plan_id"] for body in bodies], [PLAN_A, PLAN_A])
                self.assertNotEqual(bodies[0]["request_id"], bodies[1]["request_id"])
                self.assertIn("人工重试已执行一次", page.locator("#agentTranscript").inner_text())
                self.assertIn("请求过于频繁", page.locator(".is-interrupted").inner_text())

    def test_rejected_confirmation_late_read_cannot_restore_after_session_stop(self):
        for width, confirmation in ((1280, "revoked"), (390, "cleared")):
            with self.subTest(width=width, confirmation=confirmation):
                page = self.dialogue_page({"width": width, "height": 844})
                self.reject_next_confirmation(page)
                # HTTP 429 未进入 Kernel，不能伪造本次确认的 last_turn。
                page.evaluate("window.__dialogueMode = 'deferred'; window.__dialogueSnapshot.last_turn = null")
                page.locator('[data-effect-confirm]').click()
                page.wait_for_function("typeof window.__releaseDialogueSnapshot === 'function'")
                page.evaluate("""confirmation => {
                    const fixtureFetch = window.fetch;
                    window.__stopCalls = [];
                    window.fetch = async (url, options = {}) => {
                        if (String(url) !== '/api/agent/session/stop') return fixtureFetch(url, options);
                        window.__stopCalls.push(JSON.parse(options.body));
                        // 旧核实读已捕获待确认计划；stop 后所有新读只能看到撤销状态。
                        window.__dialogueMode = 'no_terminal';
                        window.__dialogueSnapshot = {pending_approval: null, last_turn: null};
                        return new Response(JSON.stringify({ok: true, status: 'stopped', stopped: true,
                            model_turn: 'not_running', confirmation, background_tasks: [],
                            uncancellable: [], stop_requested_generation: 0, cancelled_continuations: []}),
                            {headers: {'Content-Type': 'application/json'}});
                    };
                }""", confirmation)
                page.locator('#agentStopSession').click()
                page.wait_for_function("window.__stopCalls.length === 1 && document.getElementById('agentStopSession').getAttribute('aria-busy') === 'false'")
                page.evaluate('window.__releaseDialogueSnapshot()')
                page.wait_for_function("window.__dialogueLateReturned === true")
                # 即使已有观察器在三次无匹配轮次后退出，也不能留下可用的旧确认卡。
                page.wait_for_function("document.getElementById('agentSend').getAttribute('aria-busy') === 'false'", timeout=8000)
                calls = page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm')")
                self.assertEqual(len(calls), 1, "停止和迟到核实读均不能自动重放确认")
                self.assertEqual(page.evaluate('window.__stopCalls'), [{"session_id": json.loads(calls[0]['body'])['session_id']}])
                self.assertEqual(page.locator(f'[data-effect-confirm="{PLAN_A}"]:enabled').count(), 0)
                self.assertEqual(page.locator(f'[data-effect-cancel="{PLAN_A}"]:enabled').count(), 0)
                self.assertEqual(page.locator('.agent-confirmation-card:not(.is-expired)').count(), 0)
                self.assertNotIn('仍待确认', page.locator('#agentTranscript').inner_text())

    def test_session_stop_preserves_confirmed_effect_until_observed_terminal(self):
        page = self.dialogue_page()
        page.evaluate("""events => {
            window.__kernelConfig.confirmEvents = events;
            window.__kernelConfig.confirmHoldOpen = true;
            window.__confirmationAborted = false;
            window.__stopCalls = [];
            const fixtureFetch = window.fetch;
            window.fetch = async (url, options = {}) => {
                if (String(url) === '/api/agent/actions/confirm') {
                    window.__confirmedRequest = JSON.parse(options.body);
                    options.signal.addEventListener('abort', () => { window.__confirmationAborted = true; }, {once: true});
                }
                if (String(url) !== '/api/agent/session/stop') return fixtureFetch(url, options);
                window.__stopCalls.push(JSON.parse(options.body));
                const request = window.__confirmedRequest;
                window.__dialogueMode = 'no_terminal';
                window.__dialogueSnapshot = {
                    pending_approval: null, last_turn: null,
                    active_turn: {request_id: request.request_id, turn_id: `turn-${request.request_id}`,
                        generation: 2, status: 'running', protected: true},
                };
                return new Response(JSON.stringify({ok: true, status: 'critical_pending', stopped: false,
                    model_turn: 'critical_pending', confirmation: 'unchanged', background_tasks: []}),
                    {headers: {'Content-Type': 'application/json'}});
            };
        }""", [_event(1, 'turn.started', {'kind': 'confirmation'})])
        page.locator('[data-effect-confirm]').click()
        page.wait_for_function("window.__confirmedRequest && document.getElementById('agentSend').getAttribute('aria-busy') === 'true'")
        page.locator('#agentStopSession').click()
        page.wait_for_function("window.__stopCalls.length === 1 && document.getElementById('agentStopSession').getAttribute('aria-busy') === 'false'")
        self.assertFalse(page.evaluate('window.__confirmationAborted'))
        self.assertEqual(page.locator('#agentSend').get_attribute('aria-busy'), 'true')
        self.assertEqual(page.locator('.agent-cancelled').count(), 0)
        # 流暂未给出终态；已确认写入仍需由原观察器收到最终持久化回执。
        page.evaluate("""() => {
            const request = window.__confirmedRequest;
            window.__dialogueMode = '';
            window.__dialogueSnapshot = {
                pending_approval: null, active_turn: null,
                last_turn: {request_id: request.request_id, turn_id: `turn-${request.request_id}`,
                    status: 'completed'},
                messages: [{role: 'user', content: '准备原计划'},
                    {role: 'assistant', content: '已确认操作终态回执'}],
            };
        }""")
        page.get_by_text('已确认操作终态回执', exact=True).wait_for()
        self.assertEqual(page.locator('#agentSend').get_attribute('aria-busy'), 'false')
        self.assertTrue(page.evaluate('window.__confirmationAborted'), '仅在读到终态后释放旧流')
        calls = page.evaluate('window.__kernelCalls')
        self.assertEqual(sum(call['url'] == '/api/agent/actions/confirm' for call in calls), 1)
        self.assertNotIn('/api/agent/query/cancel', [call['url'] for call in calls])

    def test_rejected_confirmation_does_not_restore_unverified_or_retired_plan(self):
        cases = [("no_terminal", None), ("old_terminal", approval(PLAN_B))]
        cases.extend((mode, approval()) for mode in (
            "network", "http", "malformed", "missing", "foreign", "protected", "other_active", "timeout",
        ))
        for mode, pending in cases:
            with self.subTest(mode=mode, pending=pending and pending["plan_id"]):
                page = self.dialogue_page()
                self.reject_next_confirmation(page)
                page.evaluate("({mode, pending}) => { window.__dialogueMode = mode; window.__dialogueSnapshot = {pending_approval: pending}; }",
                              {"mode": mode, "pending": pending})
                page.locator("[data-effect-confirm]").click()
                page.wait_for_function("document.getElementById('agentSend').getAttribute('aria-busy') === 'false'")
                self.assertEqual(page.locator("[data-effect-confirm], [data-effect-cancel]").count(), 0)
                self.assertIn("请求过于频繁", page.locator(".is-interrupted").inner_text())
                self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 1)

    def test_rejected_confirmation_late_read_cannot_restore_into_new_session(self):
        page = self.dialogue_page()
        self.reject_next_confirmation(page)
        page.evaluate("window.__dialogueMode = 'deferred'")
        page.locator("[data-effect-confirm]").click()
        page.wait_for_function("typeof window.__releaseDialogueSnapshot === 'function'")
        page.locator("#agentNewSession").click()
        self.send(page, preview_events(approval(PLAN_B)), approval(PLAN_B), "准备另一个会话的计划")
        page.evaluate("window.__releaseDialogueSnapshot()")
        page.wait_for_function("window.__dialogueLateReturned === true")
        self.assertEqual(page.locator(f'[data-effect-confirm="{PLAN_A}"]').count(), 0)
        self.assertTrue(page.locator(f'[data-effect-confirm="{PLAN_B}"]').is_enabled())
        self.assertEqual(page.locator(".agent-confirmation-card:not(.is-expired)").count(), 1)
        self.assertNotIn("请求过于频繁", page.locator("#agentTranscript").inner_text())
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 1)

    def test_rejected_candidate_confirmation_preserves_selection_and_cancel_context(self):
        page = self.dialogue_page({"width": 390, "height": 844})
        self.send(page, [
            _event(1, "turn.started"),
            _event(2, "tool.completed", {"tool": "resource.search", "result": {"candidate_view": candidate_view()}}),
            _event(3, "turn.completed", {"status": "success", "answer": "候选已找到。"}),
        ], approval())
        plan = approval(PLAN_B)
        page.evaluate("({events, plan}) => { window.__kernelConfig.queryEvents = events; window.__dialogueSnapshot = {pending_approval: plan}; }",
                      {"events": preview_events(plan), "plan": plan})
        page.locator("[data-candidate-select]").click()
        page.wait_for_function("document.getElementById('agentSend').getAttribute('aria-busy') === 'false'")
        page.evaluate("""() => {
            window.__candidateGroup = document.querySelector('.agent-candidates');
            window.__candidateSelection = window.__candidateGroup.querySelector('.agent-confirmation-card')._candidateSelection;
            window.__dialogueMode = 'old_terminal';
        }""")
        self.reject_next_confirmation(page)
        page.locator(f'[data-effect-confirm="{PLAN_B}"]').click()
        page.wait_for_function("document.getElementById('agentSend').getAttribute('aria-busy') === 'false'")
        self.assertTrue(page.evaluate("""() => {
            const card = document.querySelector('.agent-candidates .agent-confirmation-card:not(.is-expired)');
            return window.__candidateGroup.isConnected && card?._agentTurn.candidateGroup === window.__candidateGroup
                && JSON.stringify(card._candidateSelection) === JSON.stringify(window.__candidateSelection);
        }"""))
        self.assertIn("请求过于频繁", page.locator(".agent-candidates .is-interrupted").inner_text())
        page.locator(f'[data-effect-cancel="{PLAN_B}"]').click()
        page.locator(".agent-candidates .agent-cancelled").wait_for()
        self.assertTrue(page.locator("[data-candidate-select]").is_enabled())
        self.assertTrue(page.locator('[data-candidate-position="1"]').is_checked())
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 1)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
