"""Media Agent 前端关键行为的真实浏览器回归测试。

系统 Python 安装 Playwright 时执行；项目虚拟环境未安装时自动跳过，避免扩大运行时依赖。
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 由标准测试套件验证跳过路径
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "app" / "static" / "js" / "agent.js"
MAIN_STYLES = ROOT / "app" / "static" / "css" / "main.css"
AGENT_STYLES = ROOT / "app" / "static" / "css" / "agent.css"


def _pending_action_plan(
    plan_id: str, *, risk: str = "write", expires_in: int = 120
) -> dict[str, object]:
    return {
        "version": 1,
        "plan_id": plan_id,
        "status": "awaiting_approval",
        "title": "执行受控操作",
        "target": "当前选择的对象",
        "impact": "会执行行动计划中列出的写操作。",
        "reversibility": "可按对应业务流程撤销或重新调整。",
        "risk": risk,
        "preflight_at": "2026-08-31T12:00:00+08:00",
        "preflight_summary": "预检通过。",
        "expires_in": expires_in,
        "decisions": [
            {"id": "execute", "label": "执行"},
            {"id": "cancel", "label": "取消"},
        ],
    }


@unittest.skipIf(sync_playwright is None, "系统环境未安装 Playwright")
class AgentBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True,
            executable_path="/usr/bin/google-chrome",
            args=["--no-sandbox"],
        )
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.styles = "\n".join((
            MAIN_STYLES.read_text(encoding="utf-8"),
            AGENT_STYLES.read_text(encoding="utf-8"),
        ))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content("""
            <!doctype html><html><body>
            <section class="agent-page">
              <button class="agent-new-session" id="agentNewSession" type="button">新对话</button>
              <button id="draft" type="button" data-agent-draft="检查《剧名》有没有缺集">缺集</button>
              <div id="agentCapabilities"><strong>—</strong><span>载入中</span></div>
              <span id="agentSessionCount">0</span>
              <div class="agent-session-list" id="agentSessionList" aria-busy="true"></div>
              <p id="agentSessionStatus" role="status" aria-live="polite"></p>
              <section class="agent-console is-empty">
                <div id="agentTranscript"></div>
                <div class="agent-empty-intro" id="agentEmptyIntro"><p>直接描述你想处理的任务</p></div>
                <form id="agentComposer"><textarea id="agentPrompt" data-empty-placeholder="询问 MediaFlux" data-active-placeholder="继续描述或调整任务"></textarea><span class="agent-submit-slot"><button id="agentSend" type="submit"><span>发送任务</span><i data-lucide="arrow-up"></i></button><button id="agentStop" type="button" hidden disabled><span>停止任务</span><i data-lucide="square"></i></button></span></form>
              </section>
            </section>
            </body></html>
        """)
        self.page.add_style_tag(content=self.styles)
        slow_initial_restore = self._testMethodName == "test_slow_initial_restore_does_not_overwrite_new_conversation"
        browser_config = {
            "sessionsDelayMs": 700 if slow_initial_restore else 0,
            "sessionsResponse": {
                "sessions": [{
                    "session_id": "agent_session_history_0001",
                    "title": "不应覆盖的新会话",
                    "message_count": 2,
                    "updated_at": "2026-08-04 10:00:00",
                }]
            } if slow_initial_restore else {"sessions": []},
            "sessionDetails": {
                "agent_session_history_0001": {
                    "session_id": "agent_session_history_0001",
                    "title": "不应覆盖的新会话",
                    "messages": [
                        {"role": "user", "data": {"text": "旧会话内容"}},
                        {"role": "assistant", "data": {
                            "mode": "read_only", "tool_name": "library.search",
                            "ok": True, "status": "success", "summary": "旧会话结果",
                            "error": "", "suggestions": [],
                        }},
                    ],
                }
            } if slow_initial_restore else {},
        }
        self.page.evaluate("""config => {
            window.renderLucideIcons = () => {};
            window.__agentCalls = [];
            window.__agentQueryResponse = {};
            window.__agentQueryGate = null;
            window.__agentQuerySignal = null;
            window.__agentLateStreamDelayMs = 0;
            window.__agentStreamEvents = null;
            window.__agentCancelGate = null;
            window.__agentCancelSignal = null;
            window.__agentPrepareResponse = {};
            window.__agentPrepareGate = null;
            window.__agentPrepareSignal = null;
            window.__agentConfirmResponse = {};
            window.__agentConfirmError = null;
            window.__agentSessionsDelayMs = Number(config.sessionsDelayMs || 0);
            window.__agentSessionsResponse = config.sessionsResponse || {sessions: []};
            window.__agentSessionDetails = config.sessionDetails || {};
            window.__agentSessionDeleteStatus = 200;
            window.__agentSessionDeleteError = '会话删除失败';
            window.__agentResetGate = null;
            window.__resolveAgentReset = null;
            window.__agentConfirmGate = null;
            window.__resolveAgentConfirm = null;
            window.fetch = async (url, options = {}) => {
                const requestUrl = String(url || '');
                window.__agentCalls.push({url: requestUrl, method: options.method || 'GET', body: options.body || ''});
                const method = String(options.method || 'GET').toUpperCase();
                let payload = window.__agentQueryResponse;
                let responseStatus = 200;
                if (requestUrl === '/api/agent/capabilities') {
                    payload = {tools: [{name: 'library.search', requires_confirmation: false}]};
                } else if (requestUrl === '/api/agent/sessions') {
                    const delayMs = window.__agentSessionsDelayMs;
                    const capturedPayload = JSON.parse(JSON.stringify(window.__agentSessionsResponse));
                    if (delayMs > 0) {
                        await new Promise(resolve => setTimeout(resolve, delayMs));
                    }
                    payload = capturedPayload;
                } else if (requestUrl.startsWith('/api/agent/sessions/')) {
                    const sessionId = decodeURIComponent(requestUrl.split('/').pop());
                    if (method === 'DELETE' && window.__agentSessionDeleteStatus !== 200) {
                        return new Response(JSON.stringify({error: window.__agentSessionDeleteError}), {
                            status: window.__agentSessionDeleteStatus,
                            headers: {'Content-Type': 'application/json'},
                        });
                    }
                    payload = method === 'DELETE'
                        ? {deleted: true, reset: {reset: true}}
                        : {session: window.__agentSessionDetails[sessionId] || null};
                } else if (requestUrl === '/api/agent/query/cancel') {
                    window.__agentCancelSignal = options.signal || null;
                    if (window.__agentCancelGate) {
                        await Promise.race([
                            window.__agentCancelGate,
                            new Promise((_, reject) => {
                                options.signal?.addEventListener('abort', () => {
                                    reject(new DOMException('Aborted', 'AbortError'));
                                }, {once: true});
                            }),
                        ]);
                    }
                    payload = {cancelled: true};
                } else if (requestUrl === '/api/agent/query') {
                    window.__agentQuerySignal = options.signal || null;
                    if (Array.isArray(window.__agentStreamEvents)) {
                        const request = JSON.parse(String(options.body || '{}'));
                        const events = window.__agentStreamEvents.map((event) => ({
                            ...event,
                            request_id: request.request_id,
                        }));
                        return new Response(
                            `${events.map(event => JSON.stringify(event)).join('\\n')}\\n`,
                            {
                                status: 200,
                                headers: {'Content-Type': 'application/x-ndjson'},
                            },
                        );
                    }
                    if (window.__agentLateStreamDelayMs > 0) {
                        const request = JSON.parse(String(options.body || '{}'));
                        const encoder = new TextEncoder();
                        const events = [
                            {type: 'status', request_id: request.request_id, phase: 'answering'},
                            {type: 'delta', request_id: request.request_id, delta: '不应出现的迟到文本'},
                            {
                                type: 'final', request_id: request.request_id,
                                payload: {
                                    request_id: request.request_id,
                                    mode: 'conversation',
                                    result: {
                                        ok: true, status: 'success', summary: '不应出现的迟到结果',
                                        data: {}, evidence: [], suggestions: [],
                                    },
                                },
                            },
                        ];
                        const stream = new ReadableStream({
                            start(controller) {
                                controller.enqueue(encoder.encode(`${JSON.stringify(events[0])}\n`));
                                window.setTimeout(() => {
                                    try {
                                        controller.enqueue(encoder.encode(
                                            `${JSON.stringify(events[1])}\n${JSON.stringify(events[2])}\n`,
                                        ));
                                        controller.close();
                                    } catch (_) { /* reader was cancelled */ }
                                }, window.__agentLateStreamDelayMs);
                            },
                        });
                        return new Response(stream, {
                            status: 200,
                            headers: {'Content-Type': 'application/x-ndjson'},
                        });
                    }
                    if (window.__agentQueryGate) {
                        await Promise.race([
                            window.__agentQueryGate,
                            new Promise((_, reject) => {
                                options.signal?.addEventListener('abort', () => {
                                    reject(new DOMException('Aborted', 'AbortError'));
                                }, {once: true});
                            }),
                        ]);
                    }
                    payload = window.__agentQueryResponse;
                } else if (requestUrl.includes('/prepare')) {
                    window.__agentPrepareSignal = options.signal || null;
                    if (window.__agentPrepareGate) {
                        await Promise.race([
                            window.__agentPrepareGate,
                            new Promise((_, reject) => {
                                options.signal?.addEventListener('abort', () => {
                                    reject(new DOMException('Aborted', 'AbortError'));
                                }, {once: true});
                            }),
                        ]);
                    }
                    payload = window.__agentPrepareResponse;
                } else if (requestUrl.endsWith('/actions/confirm/discard')) {
                    payload = {discarded: true};
                } else if (requestUrl.endsWith('/actions/confirm')) {
                    if (window.__agentConfirmGate) await window.__agentConfirmGate;
                    if (window.__agentConfirmError) {
                        payload = window.__agentConfirmError.payload;
                        responseStatus = Number(window.__agentConfirmError.status || 500);
                    } else {
                        payload = window.__agentConfirmResponse;
                    }
                } else if (requestUrl.endsWith('/session/reset')) {
                    if (window.__agentResetGate) await window.__agentResetGate;
                    payload = {reset: true};
                }
                return new Response(JSON.stringify(payload), {
                    status: responseStatus,
                    headers: {'Content-Type': 'application/json'},
                });
            };
            return true;
        }""", browser_config)
        self.page.add_script_tag(content=self.source)
        self.page.wait_for_function("""() =>
            window.__agentCalls.some(call => call.url === '/api/agent/capabilities')
            && window.__agentCalls.some(call => call.url === '/api/agent/sessions')
        """)

    def tearDown(self):
        self.page.close()

    def _set_query_response(self, payload: dict) -> None:
        self.page.evaluate("payload => { window.__agentQueryResponse = payload; }", payload)

    def test_draft_stays_local_and_selects_placeholder(self):
        initial_calls = self.page.evaluate("window.__agentCalls.length")
        self.page.locator("#draft").click()
        self.assertEqual(self.page.locator("#agentPrompt").input_value(), "检查《剧名》有没有缺集")
        self.assertEqual(self.page.evaluate("window.__agentCalls.length"), initial_calls)
        self.assertEqual(
            self.page.evaluate("[agentPrompt.selectionStart, agentPrompt.selectionEnd]"),
            [3, 5],
        )

    def test_stop_cancels_current_query_without_layout_shift_or_history_refresh(self):
        sessions_before = self.page.evaluate(
            "window.__agentCalls.filter(call => call.url === '/api/agent/sessions').length"
        )
        slot_before = self.page.locator(".agent-submit-slot").bounding_box()
        self.page.evaluate("""() => {
            window.__agentQueryGate = new Promise(() => {});
        }""")
        self.page.locator("#agentPrompt").fill("检查下载队列")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        self.page.locator("#agentStop").wait_for(state="visible")
        slot_busy = self.page.locator(".agent-submit-slot").bounding_box()
        self.assertEqual(slot_before["width"], slot_busy["width"])
        self.assertEqual(slot_before["height"], slot_busy["height"])

        self.page.locator("#agentStop").click()
        self.page.locator(".agent-cancelled").wait_for()
        self.page.wait_for_function("""() =>
            window.__agentCalls.some(call => call.url === '/api/agent/query/cancel')
        """)
        payloads = self.page.evaluate("""() => {
            const query = window.__agentCalls.find(call => call.url === '/api/agent/query');
            const cancel = window.__agentCalls.find(call => call.url === '/api/agent/query/cancel');
            return {
                query: JSON.parse(query.body),
                cancel: JSON.parse(cancel.body),
                aborted: Boolean(window.__agentQuerySignal?.aborted),
            };
        }""")
        self.assertEqual(payloads["query"]["request_id"], payloads["cancel"]["request_id"])
        self.assertEqual(payloads["query"]["session_id"], payloads["cancel"]["session_id"])
        self.assertTrue(payloads["aborted"])
        self.assertIn("任务已停止", self.page.locator(".agent-cancelled").inner_text())
        self.assertTrue(self.page.locator("#agentStop").is_hidden())
        self.assertTrue(self.page.locator("#agentSend").is_visible())
        sessions_after = self.page.evaluate(
            "window.__agentCalls.filter(call => call.url === '/api/agent/sessions').length"
        )
        self.assertEqual(sessions_before, sessions_after)

    def test_final_fallback_replaces_streamed_draft_in_place(self):
        self.page.evaluate("""() => {
            window.__agentStreamEvents = [
                {type: 'status', phase: 'answering'},
                {type: 'delta', delta: '已完成基础检查。'},
                {
                    type: 'final',
                    payload: {
                        request_id: 'deterministic-fallback',
                        mode: 'read_only',
                        tool_call: {name: 'downloads.diagnose_queue', elapsed_ms: 3},
                        result: {
                            ok: true,
                            status: 'healthy',
                            summary: '下载队列状态正常',
                            error: '',
                            suggestions: [],
                            data: {'总数': 16},
                            evidence: [],
                        },
                        display: {
                            version: 1,
                            status: {key: 'success', label: '正常', tone: 'good'},
                            summary: '下载队列状态正常',
                            error: '',
                            details: {'总数': 16},
                            guidance: [],
                        },
                    },
                },
            ];
        }""")
        self.page.locator("#agentPrompt").fill("检查下载队列状态")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")

        self.page.get_by_text("下载队列状态正常", exact=True).wait_for()
        self.assertEqual(self.page.locator(".agent-result-card").count(), 1)
        self.assertEqual(self.page.locator(".agent-streaming").count(), 0)
        transcript = self.page.locator("#agentTranscript").inner_text()
        self.assertNotIn("已完成基础检查。", transcript)
        self.assertIn("下载队列状态正常", transcript)

    def test_late_stream_events_do_not_overwrite_stopped_message(self):
        # 全量套件高负载下，250ms 可能先于 Playwright 完成真实点击而结束流，
        # 给停止操作留出稳定窗口，同时仍等待迟到事件实际到达后再断言。
        self.page.evaluate("window.__agentLateStreamDelayMs = 2000")
        self.page.locator("#agentPrompt").fill("检查迟到流事件")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        self.page.locator(".agent-streaming").wait_for()

        self.page.locator("#agentStop").click()
        cancelled = self.page.locator(".agent-cancelled")
        cancelled.wait_for()
        self.page.wait_for_timeout(2300)

        self.assertIn("任务已停止", cancelled.inner_text())
        self.assertNotIn("不应出现的迟到文本", self.page.locator("#agentTranscript").inner_text())
        self.assertNotIn("不应出现的迟到结果", self.page.locator("#agentTranscript").inner_text())
        self.assertEqual(self.page.locator(".agent-cancelled").count(), 1)

    def test_new_session_does_not_hang_when_cancel_endpoint_stalls(self):
        self.page.evaluate("""() => {
            window.__agentQueryGate = new Promise(() => {});
            window.__agentCancelGate = new Promise(() => {});
        }""")
        self.page.locator("#agentPrompt").fill("检查取消超时")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        self.page.locator("#agentStop").wait_for(state="visible")

        self.page.locator("#agentNewSession").click()
        self.page.wait_for_function("document.getElementById('agentNewSession').disabled")
        self.page.wait_for_function(
            "window.__agentCalls.some(call => call.url === '/api/agent/session/reset')",
            timeout=3500,
        )
        self.page.wait_for_function("!document.getElementById('agentNewSession').disabled")

        self.assertTrue(self.page.evaluate("Boolean(window.__agentCancelSignal?.aborted)"))
        self.assertFalse(self.page.locator("#agentPrompt").is_disabled())
        self.assertTrue(self.page.locator("#agentStop").is_hidden())

    def test_episode_audit_uses_specialized_report(self):
        self._set_query_response({
            "request_id": "episode-test",
            "mode": "tool_result",
            "tool_call": {"name": "library.audit_episodes", "elapsed_ms": 18},
            "result": {
                "ok": True,
                "status": "updates_available",
                "summary": "发现 2 集已播但本地尚未收录",
                "data": {
                    "query": "黑镜", "title": "黑镜", "tmdb_id": "42009", "season": 7,
                    "as_of": "2026-08-01", "expected_aired": 6, "local_episode_count": 4,
                    "missing_count": 2,
                    "missing_sample": [{"season": 7, "episode": 5}, {"season": 7, "episode": 6}],
                    "resource_followups": [
                        {
                            "tool": "library.search_missing_episode_resources",
                            "label": "搜索 S07E05 资源",
                            "episode_label": "S07E05",
                            "arguments": {
                                "query": "黑镜", "tmdb_id": "42009", "season": 7,
                                "episode": 5, "as_of": "2026-08-01",
                            },
                        },
                    ],
                    "resource_followups_truncated": True,
                    "sources": [{"server_type": "jellyfin", "server_name": "Jellyfin", "status": "ready", "local_episode_count": 4}],
                },
                "evidence": [], "suggestions": [],
            },
        })
        self.page.locator("#agentPrompt").fill("检查《黑镜》第 7 季有没有缺集")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        report = self.page.locator(".agent-media-report")
        report.wait_for()
        self.assertIn("确认缺失", report.inner_text())
        self.assertIn("S07E05", report.inner_text())
        self.assertIn("Jellyfin", report.inner_text())
        self.assertIn("发现缺集", self.page.locator(".agent-result-card").inner_text())
        action = report.locator(".agent-episode-chip")
        self.assertEqual(action.count(), 1)
        self.assertEqual(action.get_attribute("aria-label"), "搜索 S07E05 资源")
        self.assertIn("当前提供前 1 集快捷搜索", report.inner_text())

        self._set_query_response({
            "request_id": "episode-resource-test",
            "mode": "read_only",
            "tool_call": {
                "name": "library.search_missing_episode_resources",
                "arguments": {
                    "query": "黑镜", "tmdb_id": "42009", "season": 7,
                    "episode": 5, "as_of": "2026-08-01",
                },
                "elapsed_ms": 24,
            },
            "result": {
                "ok": True, "status": "success", "summary": "已确认 S07E05 缺失；找到 0 条资源",
                "data": {
                    "verification": {
                        "title": "黑镜", "season": 7, "episode": 5,
                        "verified_missing": True,
                    },
                    "search": {
                        "items": [], "returned": 0, "sites_attempted": [],
                        "providers_attempted": 0, "providers_succeeded": 0,
                    },
                },
                "evidence": [], "suggestions": [],
            },
        })
        action.click()
        self.page.wait_for_function("window.__agentCalls.some(call => call.url.includes('library.search_missing_episode_resources'))")
        call = self.page.evaluate("window.__agentCalls.findLast(call => call.url.includes('library.search_missing_episode_resources'))")
        self.assertEqual(call["url"], "/api/agent/tools/library.search_missing_episode_resources")
        request_body = json.loads(call["body"])
        self.assertRegex(request_body.pop("session_id"), r"^[A-Za-z0-9_-]{16,64}$")
        self.assertEqual(request_body, {
            "arguments": {
                "query": "黑镜", "tmdb_id": "42009", "season": 7,
                "episode": 5, "as_of": "2026-08-01",
            },
        })
        self.page.locator(".agent-search-report").last.wait_for()
        self.assertIn("已确认目标为已播缺集", self.page.locator(".agent-search-report").last.inner_text())
        self.assertEqual(action.get_attribute("aria-busy"), "false")

    def test_missing_season_resource_report_is_bounded_safe_and_mobile_stable(self):
        self.page.set_viewport_size({"width": 640, "height": 900})
        malicious = '<img src=x onerror="window.__agentXss=true">'
        episodes = []
        for episode in range(1, 5):
            episodes.append({
                "season": 2,
                "episode": episode,
                "episode_label": f"S02E{episode:02d}" if episode < 4 else "S02E99",
                "status": "success" if episode != 2 else "unavailable",
                "search": {
                    "items": [{
                        "result_id": f"season_result_resource_{episode:04d}",
                        "site_id": "nyaa",
                        "site_name": "Nyaa",
                        "title": malicious if episode == 1 else f"示例剧 S02E{episode:02d}",
                        "download_state": "ready",
                        "download_kinds": ["magnet"],
                    }],
                    "errors": [],
                    "has_more": False,
                },
            })
        self.page.evaluate("payload => { window.__agentPrepareResponse = payload; }", {
            "request_id": "season-prepare-test",
            "mode": "confirmation_required",
            "tool_call": {"name": "indexer.submit_resource", "elapsed_ms": 5},
            "result": {
                "ok": True, "status": "ready", "summary": "资源提交预检已完成",
                "data": {}, "evidence": [], "suggestions": [],
            },
            "action_plan": _pending_action_plan(
                "confirm-season-demo", risk="danger"
            ),
        })
        self._set_query_response({
            "request_id": "season-resource-test",
            "mode": "read_only",
            "tool_call": {"name": "library.search_missing_season_resources", "elapsed_ms": 18},
            "result": {
                "ok": True,
                "status": "partial",
                "summary": "已核验第 2 季并搜索部分缺集资源",
                "data": {
                    "verification": {
                        "title": "示例剧", "season": 2, "as_of": "2026-08-03",
                        "verified_missing": True,
                    },
                    "missing_total": 4,
                    "processed": 3,
                    "remaining": 1,
                    "failed": 1,
                    "truncated": True,
                    "episodes": episodes,
                },
                "evidence": [],
                "suggestions": [],
            },
        })
        self.page.locator("#agentPrompt").fill("给《示例剧》第 2 季所有缺集找资源")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        report = self.page.locator(".agent-season-resource-report")
        report.wait_for()
        text = report.inner_text()
        self.assertIn("季度缺集已重新核验", text)
        self.assertIn("确认缺集", text)
        self.assertIn("本批处理", text)
        self.assertIn("候选资源", text)
        self.assertIn("本次最多处理 3 集；其余 1 集可再次按季度检索。", text)
        self.assertEqual(report.locator(".agent-season-resource-group").count(), 3)
        self.assertNotIn("S02E99", text)
        self.assertIn(malicious, text)
        self.assertEqual(report.locator("img").count(), 0)
        self.assertFalse(self.page.evaluate("window.__agentXss === true"))
        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth"),
            self.page.evaluate("document.documentElement.clientWidth"),
        )

        action = report.locator('[data-agent-target="qb"]').first
        action.wait_for()
        action.click()
        self.page.locator(".agent-confirmation-card").wait_for()
        calls = self.page.evaluate("window.__agentCalls")
        prepare_calls = [call for call in calls if "/prepare" in call["url"]]
        self.assertEqual(len(prepare_calls), 1)
        request_body = json.loads(prepare_calls[0]["body"])
        self.assertRegex(request_body.pop("session_id"), r"^[A-Za-z0-9_-]{16,64}$")
        self.assertEqual(request_body, {
            "arguments": {
                "result_id": "season_result_resource_0001",
                "target": "qb",
            },
        })
        self.assertFalse(any(call["url"].endswith("/actions/confirm") for call in calls))
        self.page.locator(".agent-confirmation-cancel").click()

        previous_report_count = self.page.locator(".agent-season-resource-report").count()
        self._set_query_response({
            "request_id": "season-not-missing-test",
            "mode": "read_only",
            "tool_call": {"name": "library.search_missing_season_resources", "elapsed_ms": 4},
            "result": {
                "ok": False,
                "status": "not_missing",
                "summary": "第 2 季没有确认缺集",
                "data": {
                    "verification": {
                        "title": "示例剧", "season": 2, "as_of": "2026-08-03",
                        "verified_missing": False,
                    },
                    "episodes": [],
                },
                "evidence": [],
                "suggestions": [],
            },
        })
        self.page.locator("#agentPrompt").fill("再次检查")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        self.page.wait_for_function(
            "count => document.querySelectorAll('.agent-season-resource-report').length === count + 1",
            arg=previous_report_count,
        )
        latest = self.page.locator(".agent-season-resource-report").nth(previous_report_count)
        self.assertIn("当前季度无需补集", latest.inner_text())
        self.assertEqual(latest.locator(".agent-season-resource-group").count(), 0)

    def test_config_explanation_is_safe_touch_sized_and_mobile_stable(self):
        self.page.set_viewport_size({"width": 640, "height": 900})
        malicious = '<img src=x onerror="window.__agentXss=true">'
        long_capability = "受影响能力" + ("超长字段" * 32)
        self._set_query_response({
            "request_id": "config-explain-test",
            "mode": "tool_result",
            "tool_call": {"name": "config.explain_component", "elapsed_ms": 3},
            "result": {
                "ok": True,
                "status": "disabled",
                "summary": "豆瓣探索：已关闭",
                "data": {
                    "component": "douban",
                    "label": "豆瓣探索",
                    "status": "disabled",
                    "enabled": False,
                    "purpose": malicious,
                    "required_field_labels": [],
                    "missing_field_labels": [],
                    "blocked_capabilities": [long_capability],
                    "next_steps": ["该功能当前已关闭，可由 Agent 发起受控开启并在确认后保存。"],
                    "managed_by_environment": False,
                    "agent_action": {
                        "supported": True,
                        "tool": "config.set_feature_state",
                        "feature": "douban",
                        "enabled": True,
                        "requires_confirmation": True,
                        "prompt": "开启豆瓣探索",
                    },
                },
                "evidence": [],
                "suggestions": ["该功能当前已关闭，可由 Agent 发起受控开启并在确认后保存。"],
            },
        })
        self.page.locator("#agentPrompt").fill("为什么豆瓣探索不可用")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        report = self.page.locator(".agent-config-report")
        report.wait_for()

        text = report.inner_text()
        self.assertIn("CONFIGURATION MAP", text)
        self.assertIn("豆瓣探索", text)
        self.assertIn(malicious, text)
        self.assertIn(long_capability, text)
        self.assertEqual(report.locator("img").count(), 0)
        self.assertFalse(self.page.evaluate("window.__agentXss === true"))
        action = report.locator(".agent-config-action")
        self.assertEqual(action.get_attribute("data-agent-prompt"), "开启豆瓣探索")
        box = action.bounding_box()
        self.assertIsNotNone(box)
        self.assertGreaterEqual(box["width"], 44)
        self.assertGreaterEqual(box["height"], 44)
        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth"),
            self.page.evaluate("document.documentElement.clientWidth"),
        )

    def test_unavailable_episode_audit_does_not_claim_library_is_complete(self):
        self._set_query_response({
            "request_id": "episode-unavailable",
            "mode": "tool_result",
            "tool_call": {"name": "library.audit_episodes", "elapsed_ms": 12},
            "result": {
                "ok": False, "status": "unavailable", "summary": "媒体服务器暂时不可用",
                "error": "媒体服务器暂时不可用。",
                "data": {
                    "query": "黑镜", "as_of": "2026-08-01",
                    "sources": [{"server_type": "jellyfin", "server_name": "Jellyfin", "status": "unavailable", "local_episode_count": 0}],
                },
                "evidence": [], "suggestions": [],
            },
        })
        self.page.locator("#agentPrompt").fill("检查《黑镜》有没有缺集")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        report = self.page.locator(".agent-media-report")
        report.wait_for()
        text = report.inner_text()
        self.assertIn("未形成可靠缺集结论", text)
        self.assertNotIn("未发现缺集", text)
        self.assertNotIn("确认缺失", text)

    def test_verified_missing_with_failed_indexer_is_not_rendered_as_empty_success(self):
        self._set_query_response({
            "request_id": "missing-search-failed",
            "mode": "tool_result",
            "tool_call": {"name": "library.search_missing_episode_resources", "elapsed_ms": 20},
            "result": {
                "ok": False, "status": "disabled", "summary": "已确认指定集缺失，但资源站未启用",
                "error": "资源站检索未启用。",
                "data": {
                    "verification": {"title": "黑镜", "season": 7, "episode": 5, "verified_missing": True},
                    "search": {},
                },
                "evidence": [], "suggestions": [],
            },
        })
        self.page.locator("#agentPrompt").fill("给《黑镜》第 7 季第 5 集找缺集资源")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        report = self.page.locator(".agent-search-report")
        report.wait_for()
        text = report.inner_text()
        self.assertIn("资源站检索未完成", text)
        self.assertIn("资源站检索未启用", text)
        self.assertNotIn("检索状态\n完成", text)
        self.assertEqual(self.page.locator(".agent-search-group").count(), 0)

    def test_discovery_provider_failure_keeps_unavailable_status(self):
        self._set_query_response({
            "request_id": "discovery-unavailable",
            "mode": "tool_result",
            "tool_call": {"name": "discovery.search", "elapsed_ms": 25},
            "result": {
                "ok": False, "status": "unavailable", "summary": "外部影视数据源不可用",
                "data": {
                    "query": "黑镜", "returned": 0, "providers_attempted": ["tmdb"],
                    "providers_succeeded": [], "errors": [{"provider": "tmdb", "code": "unavailable"}],
                    "items": [], "has_more": False,
                },
                "evidence": [], "suggestions": [],
            },
        })
        self.page.locator("#agentPrompt").fill("在网上找《黑镜》")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        group = self.page.locator(".agent-search-group")
        group.wait_for()
        self.assertIn("暂不可用", group.inner_text())
        self.assertIn("未能完成检索", group.inner_text())
        self.assertIn("tmdb", self.page.locator(".agent-search-errors").inner_text())

    def test_indexer_result_prepares_confirmation_without_direct_submit(self):
        result_id = "result_demo_resource_123456"
        self._set_query_response({
            "request_id": "search-test",
            "mode": "tool_result",
            "tool_call": {"name": "indexer.search_resources", "elapsed_ms": 30},
            "result": {
                "ok": True, "status": "success", "summary": "找到 1 项资源",
                "data": {
                    "query": "黑镜", "returned": 1, "sites_attempted": ["nyaa"],
                    "sites_succeeded": ["nyaa"], "partial": False, "cached": False, "has_more": False,
                    "items": [{
                        "result_id": result_id, "site_id": "nyaa", "site_name": "Nyaa",
                        "title": "Black Mirror S07E05 1080p", "size_text": "1.2 GB", "seeders": 18,
                        "download_state": "resolvable", "download_kinds": ["magnet"],
                    }],
                },
                "evidence": [], "suggestions": [],
            },
        })
        self.page.evaluate("payload => { window.__agentPrepareResponse = payload; }", {
            "request_id": "prepare-test",
            "mode": "confirmation_required",
            "tool_call": {"name": "indexer.submit_resource", "elapsed_ms": 5},
            "result": {"ok": True, "status": "ready", "summary": "资源提交预检已完成", "data": {}, "evidence": [], "suggestions": []},
            "action_plan": {
                "version": 1,
                "plan_id": "plan-confirm-demo-123456",
                "status": "awaiting_approval",
                "title": "提交资源下载",
                "target": "你刚才选择的资源候选",
                "impact": "会向 qBittorrent 创建下载任务。",
                "reversibility": "可在下载器中暂停或删除任务。",
                "risk": "danger",
                "preflight_at": "2026-08-28T12:00:00+08:00",
                "preflight_summary": "下载器连接正常。",
                "expires_in": 120,
                "decisions": [
                    {"id": "execute", "label": "执行"},
                    {"id": "cancel", "label": "取消"},
                ],
            },
        })
        self.page.evaluate("payload => { window.__agentConfirmResponse = payload; }", {
            "request_id": "confirm-test",
            "mode": "confirmed_action",
            "tool_call": {"name": "indexer.submit_resource", "elapsed_ms": 8},
            "result": {"ok": True, "status": "accepted", "summary": "下载任务已提交", "data": {"target": "qb"}, "evidence": [], "suggestions": []},
        })
        self.page.locator("#agentPrompt").fill("搜索《黑镜》的资源")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        action = self.page.locator('[data-agent-target="qb"]')
        action.wait_for()
        self.assertIn("Black Mirror", self.page.locator(".agent-search-group").inner_text())
        action.click()
        self.page.locator(".agent-confirmation-card").wait_for()
        self.assertIn("行动计划", self.page.locator(".agent-confirmation-head").inner_text())
        self.assertIn("提交资源下载", self.page.locator(".agent-confirmation-card").inner_text())
        self.assertEqual(
            self.page.locator(".agent-confirmation-submit").inner_text(), "执行"
        )
        self.assertEqual(
            self.page.locator(".agent-confirmation-cancel").inner_text(), "取消"
        )
        calls = self.page.evaluate("window.__agentCalls")
        prepare_calls = [call for call in calls if "/prepare" in call["url"]]
        self.assertEqual(len(prepare_calls), 1)
        request_body = json.loads(prepare_calls[0]["body"])
        self.assertRegex(request_body.pop("session_id"), r"^[A-Za-z0-9_-]{16,64}$")
        self.assertEqual(request_body, {
            "arguments": {"result_id": result_id, "target": "qb"},
        })
        self.assertFalse(any(call["url"].endswith("/indexer.submit_resource") for call in calls))
        self.assertNotIn(result_id, self.page.locator("body").inner_text())
        session_calls_before_confirm = len([
            call for call in self.page.evaluate("window.__agentCalls")
            if call["url"].endswith("/api/agent/sessions")
        ])
        self.page.locator(".agent-confirmation-submit").click()
        self.page.get_by_text("下载任务已提交", exact=True).wait_for()
        self.page.wait_for_function(
            "count => window.__agentCalls.filter(call => call.url.endsWith('/api/agent/sessions')).length > count",
            arg=session_calls_before_confirm,
        )
        calls = self.page.evaluate("window.__agentCalls")
        confirm_calls = [call for call in calls if call["url"].endswith("/actions/confirm")]
        self.assertEqual(len(confirm_calls), 1)
        confirm_body = json.loads(confirm_calls[0]["body"])
        self.assertRegex(
            confirm_body["session_id"],
            r"^[A-Za-z0-9_-]{16,64}$",
        )
        self.assertEqual(confirm_body["plan_id"], "plan-confirm-demo-123456")
        self.assertEqual(self.page.locator(".agent-confirmation-submit").count(), 0)

    def test_new_conversation_aborts_inflight_resource_prepare(self):
        result_id = "result_abort_resource_123456"
        self._set_query_response({
            "request_id": "search-abort-test",
            "mode": "tool_result",
            "tool_call": {"name": "indexer.search_resources", "elapsed_ms": 30},
            "result": {
                "ok": True, "status": "success", "summary": "找到 1 项资源",
                "data": {
                    "query": "黑镜", "returned": 1, "sites_attempted": ["nyaa"],
                    "sites_succeeded": ["nyaa"], "partial": False, "cached": False,
                    "has_more": False,
                    "items": [{
                        "result_id": result_id, "site_id": "nyaa", "site_name": "Nyaa",
                        "title": "Black Mirror S07E05 1080p", "size_text": "1.2 GB",
                        "seeders": 18, "download_state": "resolvable",
                        "download_kinds": ["magnet"],
                    }],
                },
                "evidence": [], "suggestions": [],
            },
        })
        self.page.evaluate("""() => {
            window.__agentPrepareGate = new Promise(() => {});
        }""")
        self.page.locator("#agentPrompt").fill("搜索《黑镜》的资源")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        action = self.page.locator('[data-agent-target="qb"]')
        action.wait_for()
        action.click()
        self.page.wait_for_function("window.__agentPrepareSignal !== null")
        self.page.locator("#agentNewSession").click()
        self.page.wait_for_function("window.__agentPrepareSignal.aborted === true")
        self.page.wait_for_function(
            "window.__agentCalls.some(call => call.url === '/api/agent/session/reset')"
        )
        self.assertEqual(self.page.locator(".agent-confirmation-card").count(), 0)

    def test_web_search_renderer_filters_links_and_escapes_provider_text(self):
        self._set_query_response({
            "request_id": "web-search-test",
            "mode": "tool_result",
            "tool_call": {"name": "web.search", "elapsed_ms": 24},
            "result": {
                "ok": True,
                "status": "ok",
                "summary": "找到 3 条网页结果",
                "data": {
                    "query": "Jellyfin 12",
                    "provider": "tavily",
                    "topic": "general",
                    "search_depth": "basic",
                    "cached": False,
                    "credits_used": 1,
                    "elapsed_ms": 19,
                    "total": 3,
                    "results": [
                        {
                            "title": "<img src=x onerror=window.__agentXss=1> Official guide",
                            "url": "https://example.com/guide",
                            "source": "example.com",
                            "snippet": "<script>window.__agentXss=2</script> safe snippet",
                            "score": 0.92,
                        },
                        {
                            "title": "Unsafe scheme",
                            "url": "javascript:alert(1)",
                            "source": "invalid",
                            "snippet": "hidden",
                            "score": 1,
                        },
                        {
                            "title": "Credential URL",
                            "url": "https://user:pass@example.com/private",
                            "source": "invalid",
                            "snippet": "hidden",
                            "score": 1,
                        },
                    ],
                },
                "evidence": [],
                "suggestions": [],
            },
        })
        self.page.locator("#agentPrompt").fill("联网搜索 Jellyfin 12")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        link = self.page.locator(".agent-web-result-title")
        link.wait_for()

        self.assertEqual(link.count(), 1)
        self.assertEqual(link.get_attribute("href"), "https://example.com/guide")
        self.assertEqual(link.get_attribute("target"), "_blank")
        self.assertEqual(link.get_attribute("rel"), "noopener noreferrer")
        self.assertIn("<img src=x", link.inner_text())
        self.assertIn("<script>window.__agentXss=2</script>", self.page.locator(".agent-web-result p").inner_text())
        self.assertEqual(self.page.locator(".agent-web-result img").count(), 0)
        self.assertEqual(self.page.locator(".agent-web-result script").count(), 0)
        self.assertIsNone(self.page.evaluate("window.__agentXss"))

    def test_successful_query_refreshes_session_archive_without_replacing_transcript(self):
        self._set_query_response({
            "request_id": "history-refresh",
            "mode": "tool_result",
            "tool_call": {"name": "library.search", "elapsed_ms": 4},
            "result": {
                "ok": True, "status": "success", "summary": "找到结果",
                "data": {}, "evidence": [], "suggestions": [],
            },
        })
        before = self.page.evaluate(
            "window.__agentCalls.filter(call => call.url === '/api/agent/sessions').length"
        )
        self.page.locator("#agentPrompt").fill("搜索沙丘")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        self.page.locator(".agent-message-assistant").last.wait_for()
        self.page.wait_for_function(
            "before => window.__agentCalls.filter(call => call.url === '/api/agent/sessions').length > before",
            arg=before,
        )
        self.assertEqual(self.page.locator(".agent-message-user").count(), 1)
        self.assertEqual(self.page.locator(".agent-message-assistant").count(), 1)

    def test_slow_initial_restore_does_not_overwrite_new_conversation(self):
        self._set_query_response({
            "request_id": "new-conversation-wins",
            "mode": "read_only",
            "tool_call": {"name": "library.search", "elapsed_ms": 4},
            "result": {
                "ok": True, "status": "success", "summary": "新会话结果",
                "data": {}, "evidence": [], "suggestions": [],
            },
        })
        self.page.evaluate("""() => {
            window.__agentSessionsDelayMs = 0;
            window.__agentSessionsResponse = {
                sessions: [{
                    session_id: 'agent_session_history_0002',
                    title: '新会话摘要', message_count: 2, updated_at: '2026-08-04 10:01:00'
                }]
            };
        }""")
        self.page.locator("#agentPrompt").fill("新会话内容")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        self.page.locator(".agent-message-assistant").wait_for()
        self.page.wait_for_timeout(850)
        transcript = self.page.locator("#agentTranscript").inner_text()
        self.assertIn("新会话内容", transcript)
        self.assertIn("新会话结果", transcript)
        self.assertNotIn("旧会话内容", transcript)
        self.assertNotIn("旧会话结果", transcript)
        self.assertFalse(self.page.evaluate(
            "window.__agentCalls.some(call => call.url === '/api/agent/sessions/agent_session_history_0001')"
        ))
        self.assertIn("新会话摘要", self.page.locator("#agentSessionList").inner_text())
        self.assertNotIn("不应覆盖的新会话", self.page.locator("#agentSessionList").inner_text())

    def test_session_reset_disables_confirmation_actions_until_revoked(self):
        self._set_query_response({
            "request_id": "reset-confirmation-race",
            "mode": "confirmation_required",
            "tool_call": {"name": "cloud.organize", "elapsed_ms": 4},
            "action_plan": _pending_action_plan("confirm-reset-race"),
            "result": {
                "ok": True, "status": "confirmation_required", "summary": "等待确认",
                "data": {}, "evidence": [], "suggestions": [],
            },
        })
        self.page.locator("#agentPrompt").fill("整理云盘")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        self.page.locator(".agent-confirmation-card").wait_for()
        self.assertEqual(self.page.locator(".agent-confirmation-card").count(), 1)
        self.assertEqual(self.page.locator(".agent-result-card").count(), 0)
        self.page.set_viewport_size({"width": 640, "height": 900})
        new_session_box = self.page.locator("#agentNewSession").bounding_box()
        confirm_box = self.page.locator(".agent-confirmation-submit").bounding_box()
        cancel_box = self.page.locator(".agent-confirmation-cancel").bounding_box()
        self.assertGreaterEqual(new_session_box["width"], 44)
        self.assertGreaterEqual(new_session_box["height"], 44)
        self.assertGreaterEqual(confirm_box["height"], 44)
        self.assertGreaterEqual(cancel_box["height"], 44)
        self.page.evaluate("""() => {
            window.__agentResetGate = new Promise(resolve => { window.__resolveAgentReset = resolve; });
        }""")
        self.page.locator("#agentNewSession").click()
        self.page.wait_for_function("document.getElementById('agentNewSession').disabled")
        self.assertTrue(self.page.locator(".agent-confirmation-submit").is_disabled())
        self.assertTrue(self.page.locator(".agent-confirmation-cancel").is_disabled())
        self.page.evaluate("window.__resolveAgentReset()")
        self.page.wait_for_function("!document.getElementById('agentNewSession').disabled")
        self.assertEqual(self.page.locator(".agent-confirmation-card").count(), 0)

    def test_opening_active_session_preserves_pending_confirmation(self):
        self._set_query_response({
            "request_id": "active-session-confirmation",
            "mode": "confirmation_required",
            "tool_call": {"name": "cloud.organize", "elapsed_ms": 4},
            "action_plan": _pending_action_plan("confirm-active-session"),
            "result": {
                "ok": True,
                "status": "confirmation_required",
                "summary": "等待确认",
                "data": {},
                "evidence": [],
                "suggestions": [],
            },
        })
        self.page.locator("#agentPrompt").fill("整理云盘")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        self.page.locator(".agent-confirmation-card").wait_for()
        session_id = self.page.evaluate("""() => {
            const call = [...window.__agentCalls].reverse().find(item => item.url === '/api/agent/query');
            return JSON.parse(call.body).session_id;
        }""")
        reset_calls = self.page.evaluate(
            "window.__agentCalls.filter(call => call.url === '/api/agent/session/reset').length"
        )
        self.page.evaluate("""sessionId => {
            const item = document.createElement('article');
            item.className = 'agent-session-item is-active';
            item.dataset.sessionId = sessionId;
            const button = document.createElement('button');
            button.type = 'button';
            button.dataset.agentSessionOpen = sessionId;
            button.textContent = '当前会话';
            item.append(button);
            document.getElementById('agentSessionList').replaceChildren(item);
        }""", session_id)
        self.page.locator("[data-agent-session-open]").click()
        self.assertEqual(self.page.locator(".agent-confirmation-card").count(), 1)
        self.assertEqual(
            self.page.evaluate(
                "window.__agentCalls.filter(call => call.url === '/api/agent/session/reset').length"
            ),
            reset_calls,
        )

    def test_session_list_keeps_stable_reserved_height(self):
        before = self.page.locator("#agentSessionList").bounding_box()["height"]
        self.page.evaluate("""() => {
            const list = document.getElementById('agentSessionList');
            list.replaceChildren(...Array.from({length: 6}, (_, index) => {
                const item = document.createElement('article');
                item.className = 'agent-session-item';
                const button = document.createElement('button');
                button.className = 'agent-session-open';
                button.textContent = `会话 ${index + 1}`;
                item.append(button);
                return item;
            }));
        }""")
        after = self.page.locator("#agentSessionList").bounding_box()["height"]
        self.assertEqual(before, after)

    def test_confirmation_countdown_stops_while_execution_is_in_flight(self):
        self._set_query_response({
            "request_id": "confirm-countdown-race",
            "mode": "confirmation_required",
            "tool_call": {"name": "cloud.organize", "elapsed_ms": 4},
            "action_plan": _pending_action_plan(
                "confirm-countdown-race", expires_in=1
            ),
            "result": {
                "ok": True, "status": "confirmation_required", "summary": "等待确认",
                "data": {}, "evidence": [], "suggestions": [],
            },
        })
        self.page.evaluate("""() => {
            window.__agentConfirmResponse = {
                request_id: 'confirmed', mode: 'tool_result',
                tool_call: {name: 'cloud.organize', elapsed_ms: 5},
                result: {ok: true, status: 'accepted', summary: '任务已提交', data: {}, evidence: [], suggestions: []}
            };
            window.__agentConfirmGate = new Promise(resolve => { window.__resolveAgentConfirm = resolve; });
        }""")
        self.page.locator("#agentPrompt").fill("整理云盘")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        card = self.page.locator(".agent-confirmation-card")
        card.wait_for()
        actions = card.locator(".agent-confirmation-actions")
        before_height = actions.bounding_box()["height"]
        self.page.locator(".agent-confirmation-submit").click()
        execution = card.locator(".agent-confirmation-executing")
        execution.wait_for()
        self.page.wait_for_timeout(1250)
        during_height = actions.bounding_box()["height"]
        self.assertAlmostEqual(before_height, during_height, delta=1)
        self.assertIn("服务端正在受控执行", card.inner_text())
        self.assertIn("新会话、切换和删除将在结束后恢复", execution.inner_text())
        self.assertTrue(self.page.locator("#agentNewSession").is_disabled())
        self.assertNotIn("未执行任何写操作", card.inner_text())
        self.page.evaluate("window.__resolveAgentConfirm()")
        self.page.wait_for_function("document.querySelectorAll('.agent-confirmation-submit').length === 0")
        self.assertIn("任务已提交", self.page.locator("#agentTranscript").inner_text())
        self.assertFalse(self.page.locator("#agentNewSession").is_disabled())

    def test_runtime_disable_confirm_error_preserves_card_for_same_plan_retry(self):
        plan_id = "runtime-retry-plan-123456"
        self._set_query_response({
            "request_id": "runtime-retry-query",
            "mode": "confirmation_required",
            "tool_call": {"name": "strm.run_once", "elapsed_ms": 4},
            "action_plan": _pending_action_plan(plan_id, expires_in=120),
            "result": {
                "ok": True,
                "status": "confirmation_required",
                "summary": "等待确认",
                "data": {},
                "evidence": [],
                "suggestions": [],
            },
        })
        self.page.evaluate("""() => {
            window.__agentConfirmError = {
                status: 409,
                payload: {
                    error: 'Media Agent 已关闭',
                    code: 'agent_runtime_disabled',
                    retryable: true,
                },
            };
            window.__agentConfirmResponse = {
                request_id: 'runtime-retry-confirmed',
                mode: 'confirmed_action',
                tool_call: {name: 'strm.run_once', elapsed_ms: 6},
                action_plan: {status: 'completed'},
                result: {
                    ok: true,
                    status: 'accepted',
                    summary: 'STRM 同步已提交',
                    data: {},
                    evidence: [],
                    suggestions: [],
                },
            };
        }""")
        self.page.locator("#agentPrompt").fill("立即执行 STRM 同步")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        card = self.page.locator(".agent-confirmation-card")
        card.wait_for()
        deadline = card.get_attribute("data-plan-expires-at")

        self.page.locator(".agent-confirmation-submit").click()
        self.page.get_by_text(
            "Media Agent 状态已变化，本次未执行；重新启用后可再次点击执行。",
            exact=True,
        ).wait_for()
        self.assertEqual(card.get_attribute("data-plan-id"), plan_id)
        self.assertEqual(card.get_attribute("data-plan-expires-at"), deadline)
        self.assertNotIn("is-expired", card.get_attribute("class") or "")
        self.assertFalse(self.page.locator(".agent-confirmation-submit").is_disabled())
        self.assertFalse(self.page.locator(".agent-confirmation-cancel").is_disabled())

        self.page.evaluate("window.__agentConfirmError = null")
        self.page.locator(".agent-confirmation-submit").click()
        self.page.get_by_text("STRM 同步已提交", exact=True).wait_for()
        confirm_calls = self.page.evaluate("""() => window.__agentCalls.filter(
            call => call.url.endsWith('/actions/confirm')
        )""")
        self.assertEqual(len(confirm_calls), 2)
        self.assertTrue(all(json.loads(call["body"])["plan_id"] == plan_id for call in confirm_calls))

    def test_session_delete_requires_two_deliberate_clicks(self):
        session_id = "agent_session_history_0001"
        self.page.evaluate("""sessionId => {
            window.__collapseCalls = 0;
            window.MFAnim = {
                slideOutAndCollapse: () => { window.__collapseCalls += 1; },
            };
            const item = document.createElement('article');
            item.className = 'agent-session-item';
            item.dataset.sessionId = sessionId;
            const open = document.createElement('button');
            open.type = 'button';
            open.className = 'agent-session-open';
            open.dataset.agentSessionOpen = sessionId;
            const title = document.createElement('strong');
            title.textContent = '历史会话';
            open.append(title);
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.dataset.agentSessionDelete = sessionId;
            remove.setAttribute('aria-label', '删除会话');
            remove.textContent = '删除';
            item.append(open, remove);
            document.getElementById('agentSessionList').replaceChildren(item);
        }""", session_id)
        remove = self.page.locator("[data-agent-session-delete]")
        remove.click()
        self.assertTrue(remove.evaluate("button => button.classList.contains('is-armed')"))
        self.assertEqual(self.page.evaluate(
            "window.__agentCalls.filter(call => call.method === 'DELETE').length"
        ), 0)
        self.assertEqual(self.page.evaluate("window.__collapseCalls"), 0)
        self.page.wait_for_function("document.getElementById('agentSessionStatus').textContent.includes('再次点击')")
        self.assertIn("再次点击", self.page.locator("#agentSessionStatus").inner_text())
        remove.click()
        self.page.wait_for_function(
            "window.__agentCalls.filter(call => call.method === 'DELETE').length === 1"
        )
        self.page.wait_for_function("window.__collapseCalls === 1")

    def test_session_delete_success_restores_focus_to_adjacent_session(self):
        session_ids = [
            "agent_session_history_prev",
            "agent_session_history_delete",
            "agent_session_history_next",
        ]
        self.page.evaluate("""sessionIds => {
            const list = document.getElementById('agentSessionList');
            const makeItem = sessionId => {
                const item = document.createElement('article');
                item.className = 'agent-session-item';
                item.dataset.sessionId = sessionId;
                const open = document.createElement('button');
                open.type = 'button';
                open.className = 'agent-session-open';
                open.dataset.agentSessionOpen = sessionId;
                const title = document.createElement('strong');
                title.textContent = sessionId;
                open.append(title);
                const remove = document.createElement('button');
                remove.type = 'button';
                remove.dataset.agentSessionDelete = sessionId;
                remove.setAttribute('aria-label', `删除会话：${sessionId}`);
                remove.textContent = '删除';
                item.append(open, remove);
                return item;
            };
            list.replaceChildren(...sessionIds.map(makeItem));
            window.__agentSessionsResponse = {
                sessions: [sessionIds[0], sessionIds[2]].map((sessionId, index) => ({
                    session_id: sessionId,
                    title: `保留会话 ${index + 1}`,
                    message_count: 1,
                    updated_at: '2026-08-30T08:00:00Z',
                })),
            };
        }""", session_ids)
        remove = self.page.locator(f'[data-agent-session-delete="{session_ids[1]}"]')
        remove.click()
        remove.click()
        self.page.wait_for_function(
            "expected => document.activeElement?.dataset.agentSessionOpen === expected",
            arg=session_ids[2],
        )
        self.assertEqual(
            self.page.evaluate("document.activeElement.dataset.agentSessionOpen"),
            session_ids[2],
        )

    def test_session_delete_failure_keeps_row_and_restores_button(self):
        session_id = "agent_session_history_0002"
        self.page.evaluate("""sessionId => {
            window.__agentSessionDeleteStatus = 500;
            window.__collapseCalls = 0;
            window.MFAnim = {
                slideOutAndCollapse: () => { window.__collapseCalls += 1; },
            };
            const item = document.createElement('article');
            item.className = 'agent-session-item';
            item.dataset.sessionId = sessionId;
            const open = document.createElement('button');
            open.type = 'button';
            open.className = 'agent-session-open';
            open.dataset.agentSessionOpen = sessionId;
            const title = document.createElement('strong');
            title.textContent = '保留的会话';
            open.append(title);
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.dataset.agentSessionDelete = sessionId;
            remove.setAttribute('aria-label', '删除会话');
            remove.textContent = '删除';
            item.append(open, remove);
            document.getElementById('agentSessionList').replaceChildren(item);
        }""", session_id)
        remove = self.page.locator(f'[data-agent-session-delete="{session_id}"]')
        remove.click()
        remove.click()
        self.page.wait_for_function(
            "document.getElementById('agentSessionStatus').textContent.includes('失败')"
        )
        self.page.wait_for_function(
            "!document.querySelector('[data-agent-session-delete]').disabled"
        )
        self.assertEqual(self.page.locator(f'.agent-session-item[data-session-id="{session_id}"]').count(), 1)
        self.assertEqual(self.page.evaluate("window.__collapseCalls"), 0)
        self.assertFalse(remove.evaluate("button => button.classList.contains('is-armed')"))
        self.assertEqual(remove.get_attribute("aria-busy"), "false")
        self.assertEqual(remove.get_attribute("aria-label"), "删除会话：保留的会话")

    def test_saved_session_restores_only_safe_noninteractive_summary(self):
        session_id = "agent_session_history_0001"
        self.page.evaluate("""sessionId => {
            window.__agentSessionDetails[sessionId] = {
                session_id: sessionId,
                title: '检查黑镜缺集',
                messages: [
                    {role: 'user', data: {text: '检查《黑镜》有没有缺集'}},
                    {role: 'assistant', data: {
                        mode: 'tool_result', tool_name: 'library.audit_episodes',
                        ok: true, status: 'success', summary: '媒体库检查完成',
                        error: '', suggestions: ['继续检查更新']
                    }}
                ]
            };
            const item = document.createElement('article');
            item.className = 'agent-session-item';
            item.dataset.sessionId = sessionId;
            const button = document.createElement('button');
            button.type = 'button';
            button.dataset.agentSessionOpen = sessionId;
            button.textContent = '检查黑镜缺集';
            item.append(button);
            document.getElementById('agentSessionList').replaceChildren(item);
        }""", session_id)
        self.page.locator("[data-agent-session-open]").click()
        self.page.locator(".agent-message-assistant.is-recovered").wait_for()
        self.assertIn("检查《黑镜》有没有缺集", self.page.locator("#agentTranscript").inner_text())
        self.assertIn("媒体库检查完成", self.page.locator("#agentTranscript").inner_text())
        self.assertEqual(self.page.locator(".agent-confirmation-card").count(), 0)
        self.assertTrue(self.page.locator(".agent-session-item").evaluate("item => item.classList.contains('is-active')"))
        self.assertEqual(
            self.page.locator("[data-agent-session-open]").get_attribute("aria-current"),
            "page",
        )

    def test_new_conversation_revokes_short_term_state_but_keeps_archive(self):
        self.page.evaluate("""() => {
            window.__agentSessionsResponse = {
                sessions: [{
                    session_id: 'agent_session_history_0001',
                    title: '旧会话', message_count: 2, updated_at: '2026-08-04T10:00:00+08:00'
                }]
            };
        }""")
        self.page.locator("#agentNewSession").click()
        self.page.wait_for_function(
            "window.__agentCalls.some(call => call.url === '/api/agent/session/reset' && call.method === 'POST')"
        )
        reset_call = self.page.evaluate(
            "window.__agentCalls.find(call => call.url === '/api/agent/session/reset')"
        )
        self.assertIn("session_id", json.loads(reset_call["body"]))
        self.page.wait_for_function("!document.getElementById('agentNewSession').disabled")
        self.assertFalse(self.page.locator("#agentNewSession").is_disabled())
        self.assertEqual(
            self.page.evaluate(
                "window.__agentCalls.filter(call => call.method === 'DELETE' && call.url.startsWith('/api/agent/sessions/')).length"
            ),
            0,
        )

    def test_resource_actions_are_touch_sized_readable_and_do_not_overflow_at_640px(self):
        self.page.set_viewport_size({"width": 640, "height": 900})
        self._set_query_response({
            "request_id": "mobile-resource-test",
            "mode": "tool_result",
            "tool_call": {"name": "indexer.search_resources", "elapsed_ms": 30},
            "result": {
                "ok": True, "status": "success", "summary": "找到 1 项资源",
                "data": {
                    "query": "黑镜", "returned": 1, "sites_attempted": ["nyaa"],
                    "sites_succeeded": ["nyaa"], "partial": False, "cached": False, "has_more": False,
                    "items": [{
                        "result_id": "result_mobile_resource_123456",
                        "site_id": "nyaa", "site_name": "Nyaa",
                        "title": "Black Mirror S07E05 1080p", "size_text": "1.2 GB", "seeders": 18,
                        "download_state": "resolvable", "download_kinds": ["magnet"],
                    }],
                },
                "evidence": [], "suggestions": [],
            },
        })
        self.page.locator("#agentPrompt").fill("搜索《黑镜》的资源")
        self.page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        actions = self.page.locator(".agent-resource-action")
        actions.first.wait_for()

        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth"),
            self.page.evaluate("document.documentElement.clientWidth"),
        )
        for index in range(actions.count()):
            box = actions.nth(index).bounding_box()
            self.assertIsNotNone(box)
            self.assertGreaterEqual(box["width"], 44)
            self.assertGreaterEqual(box["height"], 44)

        contrast = actions.first.evaluate(r"""element => {
            const parse = value => (value.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
            const luminance = value => {
                const channels = parse(value).map(channel => {
                    const normalized = channel / 255;
                    return normalized <= 0.03928
                        ? normalized / 12.92
                        : Math.pow((normalized + 0.055) / 1.055, 2.4);
                });
                return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
            };
            const style = getComputedStyle(element);
            const lighter = Math.max(luminance(style.color), luminance(style.backgroundColor));
            const darker = Math.min(luminance(style.color), luminance(style.backgroundColor));
            return (lighter + 0.05) / (darker + 0.05);
        }""")
        self.assertGreaterEqual(contrast, 4.5)


if __name__ == "__main__":
    unittest.main()
