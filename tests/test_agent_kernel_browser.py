"""新 Agent Kernel Web 事件适配器的真实浏览器回归。"""

from __future__ import annotations

import json
import os
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, ParamSpec, Protocol

if TYPE_CHECKING:
    from playwright.sync_api import Browser, ViewportSize

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 未安装浏览器测试依赖时由测试套件跳过
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "app" / "static" / "js" / "agent.js"
MAIN_STYLES = ROOT / "app" / "static" / "css" / "main.css"
AGENT_STYLES = ROOT / "app" / "static" / "css" / "agent.css"
SESSION_ID = "session_kernel_browser_0001"


def _chromium_executable(playwright) -> str | None:
    """优先使用与 Playwright 配套的浏览器，避免系统 Chrome 超前导致崩溃。"""
    candidates: list[Path] = []
    configured = (os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path(playwright.chromium.executable_path))
    cache_root = Path.home() / ".cache" / "ms-playwright"
    candidates.extend(
        sorted(
            cache_root.glob("chromium-*/chrome-linux*/chrome"),
            reverse=True,
        )
    )
    candidates.extend((Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium")))
    return next((str(path) for path in candidates if path.is_file()), None)


def _event(sequence: int, event_type: str, payload: dict | None = None) -> dict:
    return {
        "event_id": f"event-{sequence}",
        "type": event_type,
        "occurred_at": "2026-09-03T12:00:00.000+00:00",
        "sequence": sequence,
        "session_id": SESSION_ID,
        "turn_id": "turn-browser-0001",
        "request_id": "request-browser-0001",
        "payload": payload or {},
    }


HTML = """
<!doctype html>
<html lang="zh-CN" data-theme="light">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body class="agent-page-body">
  <div class="app-shell"><main class="main"><div class="content">
    <section class="agent-page" aria-label="Media Agent 工作台">
      <div class="agent-workbench">
        <section class="agent-console is-empty" aria-label="Agent 对话工作区">
          <div class="agent-transcript" id="agentTranscript" role="log"></div>
          <div class="agent-empty-intro" id="agentEmptyIntro"><p>直接描述你想处理的任务</p></div>
          <button type="button" class="agent-new-replies" id="agentNewReplies" hidden>有新回复</button>
          <form class="agent-composer" id="agentComposer">
            <div class="agent-composer-input-wrap">
              <textarea id="agentPrompt" rows="1" maxlength="1000" data-empty-placeholder="询问 MediaFlux" data-active-placeholder="继续描述或调整任务"></textarea>
            </div>
            <div class="agent-composer-header">
              <div class="agent-composer-actions">
                <a class="agent-action-btn" id="agentSettings" href="#settings" aria-label="Agent 设置"><svg aria-hidden="true"></svg></a>
                <button type="button" class="agent-action-btn agent-resume-session" id="agentResumeLatestSession" disabled><span>继续上次</span></button>
              </div>
              <div class="agent-composer-actions-right">
                <button type="button" class="agent-action-btn" id="toggleAgentRail" aria-expanded="false">历史</button>
                <button type="button" class="agent-action-btn" id="agentNewSession">新会话</button>
                <span class="agent-submit-slot">
                  <button type="submit" class="agent-send" id="agentSend" disabled>发送</button>
                  <button type="button" class="agent-send agent-stop" id="agentStopSession" aria-label="停止任务" aria-busy="false" hidden disabled>停止</button>
                </span>
              </div>
            </div>
          </form>
          <section class="agent-start-panel"><div class="agent-start-resume" id="agentStartResume"></div><p class="agent-start-status" id="agentStartActionsStatus" role="status">正在读取本地待办…</p><div class="agent-start-actions" id="agentStartActions" aria-busy="true"></div></section>
        </section>
        <dialog class="agent-rail agent-history-drawer" id="agentHistoryRail">
          <section class="agent-rail-section agent-session-panel">
            <div class="agent-session-heading">
              <div class="agent-session-title"><h3 id="agent-session-heading" tabindex="-1">历史会话</h3><span class="agent-session-count" id="agentSessionCount">0 条</span></div>
              <div class="agent-session-heading-actions">
                <button type="button" class="agent-history-close" data-agent-history-close>关闭</button>
              </div>
            </div>
            <label class="agent-session-search"><input id="agentSessionSearch" type="search" maxlength="80" placeholder="筛选会话标题"></label>
            <div class="agent-session-list" id="agentSessionList" aria-busy="true"></div>
            <p class="agent-history-feedback" id="agentSessionStatus" role="status"></p>
          </section>
        </dialog>
        <p class="sr-only" id="agentResponseStatus" role="status"></p>
      </div>
    </section>
  </div></main></div>
</body></html>
"""


MOCK_FETCH = r"""
(config) => {
  window.renderLucideIcons = () => {};
  window.__kernelCalls = [];
  window.__kernelConfig = config;

  const jsonResponse = (payload, status = 200) => new Response(JSON.stringify(payload), {
    status,
    headers: {'Content-Type': 'application/json'},
  });
  const sessionList = () => {
    const value = window.__kernelConfig.sessions || {sessions: []};
    return {...value, draft_scope: value.draft_scope || 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'};
  };
  const sessionSnapshot = (id, value = {}) => ({
    session_id: id,
    generation: 1,
    messages: [],
    pending_approval: null,
    candidate_view: null,
    active_turn: null,
    last_turn: null,
    draft_scope: sessionList().draft_scope,
    ...value,
  });

  const streamResponse = (events, options, delayMs = 0, holdOpen = false) => {
    const request = JSON.parse(String(options.body || '{}'));
    const streamTurnId = `turn-${request.request_id || 'fixture'}`;
    events = events.map(event => ({
      ...event,
      session_id: request.session_id || event.session_id,
      request_id: request.request_id || event.request_id,
      turn_id: streamTurnId,
    }));
    const encoder = new TextEncoder();
    let timer = null;
    let streamController = null;
    let index = 0;
    const stream = new ReadableStream({
      cancel() {
        if (timer !== null) window.clearTimeout(timer);
        streamController = null;
      },
      start(controller) {
        streamController = controller;
        const push = () => {
          if (!streamController) return;
          if (index < events.length) {
            controller.enqueue(encoder.encode(`${JSON.stringify(events[index])}\n`));
            index += 1;
            timer = window.setTimeout(push, delayMs);
            return;
          }
          if (!holdOpen) controller.close();
        };
        push();
        options.signal?.addEventListener('abort', () => {
          if (timer !== null) window.clearTimeout(timer);
          try { controller.error(new DOMException('Aborted', 'AbortError')); } catch (_) {}
        }, {once: true});
      },
      cancel() {
        if (timer !== null) window.clearTimeout(timer);
        streamController = null;
      },
    });
    return new Response(stream, {status: 200, headers: {'Content-Type': 'application/x-ndjson'}});
  };

  window.fetch = async (url, options = {}) => {
    const parsed = new URL(String(url), location.href);
    const path = parsed.pathname;
    window.__kernelCalls.push({url: path, method: options.method || 'GET', body: String(options.body || '')});
    if (path === '/api/agent/next-actions') {
      if (window.__kernelConfig.nextActionsDelayMs) await new Promise(resolve => setTimeout(resolve, window.__kernelConfig.nextActionsDelayMs));
      return jsonResponse(window.__kernelConfig.nextActions || {actions: [], snapshot_status: 'idle'}, window.__kernelConfig.nextActionsStatus || 200);
    }
    if (path === '/api/agent/sessions') return jsonResponse(sessionList());
    if (path.startsWith('/api/agent/sessions/') && options.method === 'PATCH') {
      const id = decodeURIComponent(path.split('/').pop());
      const item = (window.__kernelConfig.sessions?.sessions || []).find(item => item.session_id === id);
      if (!item) return jsonResponse({error: '会话不存在'}, 404);
      if (window.__kernelConfig.patchStatus) return jsonResponse({error: '会话更新失败'}, window.__kernelConfig.patchStatus);
      Object.assign(item, JSON.parse(options.body));
      return jsonResponse({session: item});
    }
    if (path.startsWith('/api/agent/sessions/') && (options.method || 'GET') === 'GET') {
      const id = decodeURIComponent(path.split('/').pop());
      const details = (window.__kernelConfig.sessionDetails || {})[id];
      if (details) return jsonResponse(sessionSnapshot(id, details));
      if (window.__kernelConfig.sessionSnapshotFallback) return jsonResponse(sessionSnapshot(id));
      return jsonResponse({error: '会话不存在'}, 404);
    }
    if (path.startsWith('/api/agent/sessions/') && options.method === 'DELETE') return jsonResponse({deleted: true});
    if (path === '/api/agent/query') {
      const request = JSON.parse(String(options.body || '{}'));
      if (request.session_id && request.message) {
        window.__kernelConfig.lastQueryBySession ||= {};
        window.__kernelConfig.lastQueryBySession[request.session_id] = request.message;
      }
      return streamResponse(
        window.__kernelConfig.queryEvents || [],
        options,
        Number(window.__kernelConfig.queryDelayMs || 0),
        Boolean(window.__kernelConfig.holdQueryOpen),
      );
    }
    if (path === '/api/agent/session/stop') {
      const request = JSON.parse(String(options.body || '{}'));
      const pending = Boolean(window.__kernelConfig.cancelPending);
      const result = {status: pending ? 'stopping' : 'stopped', stopped: !pending, model_turn: 'stop_requested'};
      if (request.session_id && !pending) {
        window.__kernelConfig.sessionDetails ||= {};
        const snapshot = sessionSnapshot(request.session_id, window.__kernelConfig.sessionDetails[request.session_id] || {});
        const lastCall = window.__kernelCalls.findLast(call => ['/api/agent/query', '/api/agent/actions/confirm'].includes(call.url)
          && JSON.parse(call.body).session_id === request.session_id);
        const requestId = snapshot.active_turn?.request_id || (lastCall && JSON.parse(lastCall.body).request_id);
        const turnId = snapshot.active_turn?.turn_id || `turn-${requestId}`;
        if (!snapshot.messages.length && window.__kernelConfig.lastQueryBySession?.[request.session_id]) {
          snapshot.messages = [{role: 'user', content: window.__kernelConfig.lastQueryBySession[request.session_id]}];
        }
        snapshot.active_turn = null;
        snapshot.last_turn = {
          request_id: requestId,
          turn_id: turnId,
          status: 'cancelled',
          message: '本次任务已停止。',
        };
        window.__kernelConfig.sessionDetails[request.session_id] = snapshot;
      }
      return jsonResponse(result);
    }
    if (path === '/api/agent/actions/confirm') {
      return streamResponse(
        window.__kernelConfig.confirmEvents || [],
        options,
        Number(window.__kernelConfig.confirmDelayMs || 0),
        Boolean(window.__kernelConfig.confirmHoldOpen),
      );
    }
    if (path === '/api/agent/actions/confirm/discard') return jsonResponse({discarded: true});
    throw new Error(`unexpected endpoint: ${path}`);
  };
}
"""


_CleanupArgs = ParamSpec("_CleanupArgs")


class _KernelPageHarness(Protocol):
    browser: Browser
    source: str
    styles: str

    def addCleanup(
        self, function: Callable[_CleanupArgs, object], /,
        *args: _CleanupArgs.args, **kwargs: _CleanupArgs.kwargs,
    ) -> None: ...


@unittest.skipIf(sync_playwright is None, "系统环境未安装 Playwright")
class AgentKernelBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert sync_playwright is not None
        cls.playwright = sync_playwright().start()
        executable_path = _chromium_executable(cls.playwright)
        launch_options: dict = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        if executable_path is not None:
            launch_options["executable_path"] = executable_path
        cls.browser = cls.playwright.chromium.launch(**launch_options)
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.styles = "\n".join(
            (
                MAIN_STYLES.read_text(encoding="utf-8"),
                AGENT_STYLES.read_text(encoding="utf-8"),
            )
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def make_page(
        self: _KernelPageHarness,
        config: dict | None = None,
        *,
        viewport: ViewportSize | None = None,
        stored_session: str = "",
        hash_fragment: str = "",
        initial_prompt: str = "",
        draft_text: str = "",
    ):
        config = config or {"sessions": {"sessions": []}}
        page = self.browser.new_page(
            viewport=viewport or {"width": 1280, "height": 800}
        )
        page.route(
            "http://mediaflux.test/agent",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=HTML,
            ),
        )
        page.goto(f"http://mediaflux.test/agent{hash_fragment}")
        page.add_style_tag(content=self.styles)
        if stored_session:
            page.evaluate(
                "([key, value]) => localStorage.setItem(key, value)",
                ["mediaflux.agent.kernel.session.v1", stored_session],
            )
            draft_scope = str((config.get("sessions") or {}).get("draft_scope") or "")
            if draft_scope:
                page.evaluate(
                    "([key, value]) => localStorage.setItem(key, value)",
                    [f"mediaflux.agent.kernel.session.v1.{draft_scope}", stored_session],
                )
        if initial_prompt:
            page.locator("#agentPrompt").evaluate(
                "(node, value) => { node.value = value; }", initial_prompt
            )
        if draft_text:
            draft_scope = str((config.get("sessions") or {}).get("draft_scope") or "")
            if not stored_session or not draft_scope:
                raise ValueError("draft_text 测试需要 stored_session 与 draft_scope")
            page.evaluate(
                """([scope, sessionId, text]) => sessionStorage.setItem(
                    `mediaflux.agent.drafts.v1.${scope}`,
                    JSON.stringify({[sessionId]: {text, updated_at: Date.now()}})
                )""",
                [draft_scope, stored_session, draft_text],
            )
        page.evaluate(MOCK_FETCH, config)
        page.add_script_tag(content=self.source)
        page.wait_for_function(
            "() => window.__kernelCalls.some(call => call.url === '/api/agent/sessions')"
        )
        self.addCleanup(page.close)
        return page

    def test_real_ndjson_events_render_in_place_without_legacy_endpoints(self) -> None:
        events = [
            _event(1, "turn.started", {"kind": "query"}),
            _event(2, "capabilities.selected", {"tools": ["library.search"]}),
            _event(3, "model.started", {"round": 1}),
            _event(
                4,
                "model.tool_call",
                {"round": 1, "call_id": "call-1", "tool": "library.search"},
            ),
            _event(5, "tool.started", {"call_id": "call-1", "tool": "library.search"}),
            _event(
                6, "tool.completed", {"call_id": "call-1", "tool": "library.search"}
            ),
            _event(7, "model.started", {"round": 2}),
            _event(8, "model.delta", {"round": 2, "delta": "媒体库中"}),
            _event(9, "model.delta", {"round": 2, "delta": "共有 37 集。"}),
            _event(
                10,
                "turn.completed",
                {"status": "success", "answer": "媒体库中共有 37 集。"},
            ),
        ]
        page = self.make_page(
            {"sessions": {"sessions": []}, "queryEvents": events, "queryDelayMs": 35}
        )
        page.locator("#agentPrompt").fill("我的媒体库里光阴之外有多少集")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.locator(".agent-narrative").wait_for()
        self.assertEqual(page.locator(".agent-message-user").count(), 1)
        self.assertEqual(page.locator(".agent-message-assistant").count(), 1)
        self.assertIn(
            "媒体库中共有 37 集", page.locator(".agent-narrative").inner_text()
        )
        calls = page.evaluate("window.__kernelCalls")
        paths = [call["url"] for call in calls]
        self.assertIn("/api/agent/query", paths)
        self.assertFalse(any("/tools/" in path or "/prepare" in path for path in paths))

    def test_stream_display_off_still_shows_confirmation_buttons(self) -> None:
        plan = {"plan_id": "plan-stream-display-test-0001", "tool_name": "download.pause", "effect": "WRITE",
                "preview": {"summary": "暂停指定下载"}, "result": {}, "expires_at": ""}
        page = self.make_page({"sessions": {"sessions": []}, "queryDelayMs": 40, "queryEvents": [
            _event(1, "turn.started", {"stream_display_enabled": False}),
            _event(2, "model.started", {"round": 1}),
            _event(3, "model.delta", {"round": 1, "delta": "未完成的准备文字"}),
            _event(4, "effect.approval_required", {"plan": plan}),
            _event(5, "turn.completed", {"status": "approval_required"}),
        ]})
        page.locator("#agentPrompt").fill("暂停下载")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        card = page.locator(".agent-confirmation-card")
        card.wait_for()
        self.assertIn("暂停指定下载", card.inner_text())
        self.assertTrue(card.locator("[data-effect-confirm]").is_visible())
        self.assertNotIn("未完成的准备文字", page.locator("#agentTranscript").inner_text())

    def test_preview_terminal_replaces_confirmation_with_visible_result(self) -> None:
        for event_type, payload, expected in (
            ("turn.cancelled", {"reason": "user_cancelled"}, "本轮处理已停止。"),
            ("turn.failed", {"message": "预检后的检查点失败"}, "预检后的检查点失败"),
            ("turn.completed", {"status": "partial", "answer": "预检未完成，请重新检查。"}, "预检未完成，请重新检查。"),
        ):
            with self.subTest(terminal=event_type):
                page = self.make_page({"sessions": {"sessions": []}, "queryEvents": [
                    _event(1, "turn.started", {"kind": "query"}),
                    _event(2, "effect.approval_required", {"tool": "download.pause", "plan": {
                        "plan_id": "cancelled-plan", "tool_name": "download.pause",
                        "effect": "WRITE", "preview": {"summary": "暂停测试任务"},
                    }}),
                    _event(3, event_type, payload),
                ]})
                page.locator("#agentPrompt").fill("暂停下载")
                page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
                page.get_by_text(expected, exact=True).wait_for()
                self.assertEqual(page.locator("[data-effect-confirm]").count(), 0)
                self.assertEqual(page.locator("[data-effect-cancel]").count(), 0)
                self.assertNotIn("已提交操作可能继续执行", page.locator("#agentTranscript").inner_text())
                page.close()

    def test_stream_display_toggle_hides_only_incremental_body(self) -> None:
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                page = self.make_page({"sessions": {"sessions": []}, "queryDelayMs": 120, "queryEvents": [
                    _event(1, "turn.started", {"kind": "query", "stream_display_enabled": enabled}),
                    _event(2, "model.started", {"round": 1}),
                    _event(3, "model.delta", {"round": 1, "delta": "尚未完成的草稿"}),
                    _event(4, "model.tool_call", {"call_id": "call-1", "tool": "library.search", "label": "查询进行中"}),
                    _event(5, "turn.completed", {"status": "success", "answer": "完整回答已取得"}),
                ]})
                page.evaluate("""() => {
                    window.__streamSnapshots = [];
                    new MutationObserver(() => window.__streamSnapshots.push(
                        document.querySelector('#agentTranscript').textContent
                    )).observe(document.querySelector('#agentTranscript'), {childList:true,characterData:true,subtree:true});
                }""")
                page.locator("#agentPrompt").fill("测试显示开关")
                page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
                page.locator(".agent-narrative").wait_for()
                snapshots = page.evaluate("window.__streamSnapshots")
                self.assertEqual(any("尚未完成的草稿" in text for text in snapshots), enabled)
                self.assertTrue(any("正在处理" in text for text in snapshots))
                self.assertEqual(page.locator(".agent-narrative").inner_text(), "完整回答已取得")

    def test_tool_boundary_flushes_buffered_markdown_with_animation_frames_paused(self) -> None:
        page = self.make_page({"sessions": {"sessions": []}, "queryDelayMs": 80, "queryEvents": [
            _event(1, "turn.started", {"kind": "query"}),
            _event(2, "model.started", {"round": 1}),
            _event(3, "model.delta", {"round": 1, "delta": "**工具前"}),
            _event(4, "model.delta", {"round": 1, "delta": "的正文"}),
            _event(5, "model.tool_call", {"call_id": "call-1", "tool": "library.search", "label": "工具阶段：媒体库查询"}),
            _event(6, "tool.started", {"call_id": "call-1", "tool": "library.search", "label": "工具阶段：媒体库查询"}),
            _event(7, "model.started", {"round": 2}),
            _event(8, "model.delta", {"round": 2, "delta": "新一轮草稿"}),
            _event(9, "turn.completed", {"status": "success", "answer": "权威最终答案"}),
        ]})
        page.evaluate("""() => {
            const frames = new Map(); let next = 0;
            window.requestAnimationFrame = callback => { frames.set(++next, callback); return next; };
            window.cancelAnimationFrame = id => frames.delete(id);
            window.__resumeFrames = () => { const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn(performance.now())); };
            window.__boundaryBodies = [];
            new MutationObserver(() => {
                const card = document.querySelector('.agent-result-card');
                if (card?.querySelector('.agent-stream-head')?.textContent.includes('正在处理')) {
                    window.__boundaryBodies.push(card.querySelector('.agent-stream-text')?.textContent || '');
                }
            }).observe(document.querySelector('#agentTranscript'), {childList: true, characterData: true, subtree: true});
        }""")
        page.locator("#agentPrompt").fill("查询媒体库")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.wait_for_function("() => document.querySelector('.agent-narrative')?.textContent === '权威最终答案'", polling=20)
        snapshots = page.evaluate("window.__boundaryBodies")
        self.assertTrue(snapshots)
        self.assertTrue(all(body.strip() == "工具前的正文" for body in snapshots), snapshots)
        page.evaluate("window.__resumeFrames()")
        self.assertEqual(page.locator(".agent-narrative").inner_text(), "权威最终答案")

    def test_tool_call_preserves_streamed_body_until_terminal_replaces_draft(self) -> None:
        events = [
            _event(1, "turn.started", {"kind": "query"}),
            _event(2, "model.started", {"round": 1}),
            _event(3, "model.delta", {"round": 1, "delta": "工具前的正文"}),
            _event(
                4,
                "model.tool_call",
                {
                    "round": 1,
                    "call_id": "call-1",
                    "tool": "library.search",
                    "label": "工具阶段：媒体库查询",
                },
            ),
            _event(
                5,
                "tool.started",
                {"call_id": "call-1", "tool": "library.search", "label": "工具阶段：媒体库查询"},
            ),
            _event(
                6,
                "tool.completed",
                {"call_id": "call-1", "tool": "library.search", "label": "媒体库查询"},
            ),
            _event(7, "model.started", {"round": 2}),
            _event(8, "model.delta", {"round": 2, "delta": "新一轮草稿"}),
            _event(
                9,
                "turn.completed",
                {"status": "success", "answer": "权威最终答案"},
            ),
        ]
        page = self.make_page(
            {
                "sessions": {"sessions": []},
                "queryEvents": events,
                "queryDelayMs": 160,
            }
        )
        page.evaluate("""() => {
          window.__agentBodySnapshots = [];
          const transcript = document.querySelector('#agentTranscript');
          new MutationObserver(() => {
            const card = transcript.querySelector('.agent-result-card');
            if (!card) return;
            window.__agentBodySnapshots.push({
              status: card.querySelector('.agent-stream-head')?.textContent.trim() || '',
              body: card.querySelector('.agent-stream-text, .agent-narrative')?.textContent || '',
            });
          }).observe(transcript, {childList: true, characterData: true, subtree: true});
        }""")
        page.locator("#agentPrompt").fill("查询媒体库")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.wait_for_function("""() => window.__agentBodySnapshots.some(
          item => item.status.includes('正在处理')
        )""")
        during_tools = page.evaluate("""() => window.__agentBodySnapshots.filter(
          item => item.status.includes('正在处理')
        )""")
        self.assertTrue(during_tools)
        self.assertTrue(
            all(item["body"].strip() for item in during_tools),
            "工具阶段状态变化不得清空已显示的模型正文",
        )
        self.assertTrue(any("工具前的正文" in item["body"] for item in during_tools))
        page.locator(".agent-narrative").wait_for()
        self.assertEqual(page.locator(".agent-narrative").inner_text(), "权威最终答案")
        self.assertNotIn("工具前的正文", page.locator(".agent-narrative").inner_text())
        self.assertNotIn("新一轮草稿", page.locator(".agent-narrative").inner_text())

        terminal_cases = (
            ("turn.failed", {"message": "终态失败说明"}, ".is-interrupted", "终态失败说明"),
            ("turn.cancelled", {}, ".agent-cancelled", "本轮处理已停止。"),
        )
        for event_type, payload, terminal_selector, expected in terminal_cases:
            with self.subTest(terminal=event_type):
                terminal_page = self.make_page(
                    {
                        "sessions": {"sessions": []},
                        "queryEvents": [
                            _event(1, "turn.started", {"kind": "query"}),
                            _event(2, "model.started", {"round": 1}),
                            _event(3, "model.delta", {"round": 1, "delta": "错误草稿，不可留存"}),
                            _event(4, event_type, payload),
                        ],
                        "queryDelayMs": 40,
                    }
                )
                terminal_page.locator("#agentPrompt").fill("触发终态")
                terminal_page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
                terminal_page.locator(f".agent-result-card{terminal_selector}").wait_for()
                body = terminal_page.locator(".agent-stream-text")
                self.assertEqual(body.inner_text(), expected)
                terminal_page.wait_for_timeout(100)
                self.assertEqual(body.inner_text(), expected)
                self.assertNotIn("错误草稿", body.inner_text())

    def test_partial_budget_answer_finishes_in_place_without_retry_error(self) -> None:
        answer = "## 部分完成\n\n已核对第一部，其余作品尚未检查。\n\n回复继续可接着核对。"
        events = [
            _event(1, "turn.started", {"kind": "query"}),
            _event(2, "tool.failed", {"call_id": "blocked", "tool": "library.check_updates",
                                      "code": "tool_budget_exceeded", "message": "本批工具未执行"}),
            # 无model.delta也必须渲染最终answer（例如预算最后一轮直接确定性收尾）。
            _event(3, "turn.completed", {"status": "partial", "answer": answer,
                                         "finish_reason": "tool_budget_exceeded"}),
        ]
        page = self.make_page({"sessions": {"sessions": []}, "queryEvents": events})
        page.locator("#agentPrompt").fill("检查这批剧集更新")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.locator(".agent-narrative .agent-md-heading").wait_for()
        self.assertEqual(page.locator(".agent-message-assistant").count(), 1)
        self.assertIn("其余作品尚未检查", page.locator(".agent-narrative").inner_text())
        self.assertIn("回复继续", page.locator(".agent-narrative").inner_text())
        self.assertEqual(page.locator(".agent-retry-draft").count(), 0)
        self.assertEqual(page.locator(".agent-streaming, .is-interrupted").count(), 0)

    def test_terminal_answer_releases_composer_without_waiting_for_http_eof(self):
        for status in ("success", "partial", "waiting"):
            with self.subTest(status=status):
                page = self.make_page({"sessions": {"sessions": []}, "holdQueryOpen": True, "queryEvents": [
                    _event(1, "turn.started", {"kind": "query"}),
                    _event(2, "turn.completed", {"status": status, "answer": "已保留完整结果 DONE7788"}),
                    _event(3, "turn.failed", {"message": "不应覆盖终态的迟到错误"}),
                ]})
                page.locator("#agentPrompt").fill("核对媒体库")
                page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
                page.locator(".agent-narrative").wait_for()
                page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
                page.locator("#agentPrompt").fill("下一条消息")
                self.assertTrue(page.locator("#agentSend").is_enabled())
                self.assertIn("DONE7788", page.locator(".agent-message-assistant").inner_text())
                self.assertEqual(page.locator(".agent-retry-draft, .agent-cancelled, .is-interrupted").count(), 0)
                self.assertFalse(any("/cancel" in call["url"] for call in page.evaluate("window.__kernelCalls")))

    def test_waiting_handoff_preserves_live_dom_without_snapshot_recovery(self):
        answer = "查询受到本地额度限制，已保存等待；尚未完成。"
        page = self.make_page({"sessions": {"sessions": []}, "holdQueryOpen": True, "queryDelayMs": 100,
            "queryEvents": [
                _event(1, "turn.started", {"kind": "query"}),
                _event(2, "tool.failed", {"code": "rate_limited", "message": "本地额度限制"}),
                _event(3, "turn.completed", {"status": "waiting", "answer": answer}),
                _event(4, "turn.failed", {"message": "迟到事件不可覆盖交接结果"}),
            ]})
        page.locator("#agentPrompt").fill("核对配置状态")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.locator(".agent-streaming").wait_for()
        page.evaluate("""() => {
            window.__waitingUser = document.querySelector('.agent-message-user');
            window.__waitingCard = document.querySelector('.agent-result-card');
        }""")
        page.get_by_text(answer, exact=True).wait_for(timeout=3000)
        page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'", timeout=3000)
        self.assertTrue(page.evaluate("window.__waitingUser === document.querySelector('.agent-message-user')"))
        self.assertTrue(page.evaluate("window.__waitingCard === document.querySelector('.agent-result-card')"))
        self.assertEqual(page.locator(".agent-retry-draft, .agent-cancelled, .is-interrupted").count(), 0)
        calls = page.evaluate("window.__kernelCalls")
        query = next(index for index, call in enumerate(calls) if call["url"] == "/api/agent/query")
        self.assertFalse(any(call["url"].startswith("/api/agent/sessions/") for call in calls[query + 1:]), calls)
        self.assertNotIn("本轮回复已结束", page.locator("#agentResponseStatus").inner_text())

    def test_cold_waiting_snapshot_renders_answer_without_failure_or_query_replay(self):
        answer = "查询已保存等待；尚未完成。"
        page = self.make_page({"sessions": {"sessions": [{"session_id": SESSION_ID, "title": "等待查询"}]},
            "sessionDetails": {SESSION_ID: {
                "messages": [{"role": "user", "content": "核对配置状态"}, {"role": "assistant", "content": answer}],
                "turn_status": "waiting", "last_turn": {"request_id": "waiting-request", "turn_id": "waiting-turn",
                    "status": "waiting", "message": "本轮已交接，等待任务继续。"},
            }}}, stored_session=SESSION_ID)
        page.get_by_text(answer, exact=True).wait_for(timeout=3000)
        self.assertEqual(page.locator(".agent-retry-draft, .agent-cancelled, .is-interrupted").count(), 0)
        self.assertFalse(any(call["url"] == "/api/agent/query" for call in page.evaluate("window.__kernelCalls")))

    def test_waiting_observation_keeps_dom_until_resumed_answer_arrives(self):
        waiting, final = "查询已保存等待；尚未完成。", "续行已取得真实结果。"
        page = self.make_page({"sessions": {"sessions": []}, "queryEvents": [
            _event(1, "turn.started", {"kind": "query"}),
            _event(2, "turn.completed", {"status": "waiting", "answer": waiting}),
        ]})
        page.evaluate("""([waiting, final]) => {
            const original = window.fetch;
            const schedule = window.setTimeout;
            window.setTimeout = (fn, delay, ...args) => schedule(fn, delay >= 5000 ? 40 : delay, ...args);
            window.__waitingPolls = 0;
            window.__waitingFinished = false;
            window.fetch = async (url, options = {}) => {
                const path = new URL(String(url), location.href).pathname;
                const query = window.__kernelCalls.find(call => call.url === '/api/agent/query');
                if (query && path.startsWith('/api/agent/sessions/') && (!options.method || options.method === 'GET')) {
                    window.__waitingPolls++;
                    const request = JSON.parse(query.body);
                    const done = window.__waitingFinished;
                    return new Response(JSON.stringify({
                        session_id: request.session_id, draft_scope: 'a'.repeat(64), generation: done ? 2 : 1,
                        messages: [{role: 'user', content: request.message}, {role: 'assistant', content: done ? final : waiting}],
                        pending_effect_count: done ? 0 : 1, pending_approval: null, candidate_view: null,
                        active_turn: null, turn_status: done ? 'success' : 'waiting',
                        last_turn: {request_id: request.request_id, turn_id: done ? 'resumed-turn' : 'turn-' + request.request_id,
                            status: done ? 'completed' : 'waiting'},
                    }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                return original(url, options);
            };
        }""", [waiting, final])
        page.locator("#agentPrompt").fill("核对配置状态")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.get_by_text(waiting, exact=True).wait_for(timeout=3000)
        page.evaluate("window.__waitingCard = document.querySelector('.agent-result-card')")
        page.wait_for_function("window.__waitingPolls > 0", timeout=3000)
        self.assertTrue(page.evaluate("window.__waitingCard === document.querySelector('.agent-result-card')"))
        page.evaluate("window.__waitingFinished = true")
        page.get_by_text(final, exact=True).wait_for(timeout=3000)
        self.assertEqual(page.locator(".agent-message-user").count(), 1)
        self.assertEqual(page.locator(".agent-message-assistant").count(), 1)
        self.assertEqual(page.locator(".agent-retry-draft, .agent-cancelled, .is-interrupted").count(), 0)
        self.assertEqual(sum(call["url"] == "/api/agent/query" for call in page.evaluate("window.__kernelCalls")), 1)

    def test_assistant_markdown_is_rendered_as_safe_semantic_dom(self) -> None:
        answer = """# 国漫推荐

以下是结合近期热度整理的作品：

---

## 传统玄幻

- **《剑来》**：动作与水墨表现突出
- `《仙逆》`：节奏紧凑

> 提示：可以继续让我检查媒体库收录状态。

| 作品 | 年份 |
| --- | ---: |
| 剑来 | 2026 |

[打开媒体库](/media-libraries) [危险链接](javascript:alert(1))

公开页面：https://example.com/donghua。

本地路径：C:\\Media\\Anime

```text
Season 1 / S01E01
```

<script>window.__markdownXss = true</script>
"""
        events = [
            _event(1, "turn.started", {"kind": "query"}),
            _event(2, "model.started", {"round": 1}),
            _event(3, "model.delta", {"round": 1, "delta": answer[:80]}),
            _event(4, "model.delta", {"round": 1, "delta": answer[80:]}),
            _event(5, "turn.completed", {"status": "success", "answer": answer}),
        ]
        page = self.make_page(
            {"sessions": {"sessions": []}, "queryEvents": events, "queryDelayMs": 12}
        )
        page.locator("#agentPrompt").fill("最近有什么推荐的国漫")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        narrative = page.locator(".agent-narrative")
        narrative.wait_for()

        self.assertEqual(
            narrative.locator("h2.agent-md-heading-1").inner_text(), "国漫推荐"
        )
        self.assertEqual(
            narrative.locator("h3.agent-md-heading-2").inner_text(), "传统玄幻"
        )
        self.assertEqual(narrative.locator("ul > li").count(), 2)
        self.assertEqual(narrative.locator("strong").inner_text(), "《剑来》")
        self.assertEqual(
            narrative.locator("code.agent-md-inline-code").inner_text(), "《仙逆》"
        )
        self.assertEqual(narrative.locator("blockquote").count(), 1)
        self.assertEqual(narrative.locator("hr").count(), 1)
        self.assertEqual(narrative.locator("table tbody tr").count(), 1)
        self.assertEqual(
            narrative.locator("pre code").inner_text(), "Season 1 / S01E01"
        )
        self.assertEqual(narrative.locator('a[href="/media-libraries"]').count(), 1)
        self.assertEqual(
            narrative.locator('a[href^="https://example.com/donghua"]').count(), 1
        )
        self.assertEqual(narrative.locator("a a").count(), 0)
        self.assertEqual(narrative.locator('a[href^="javascript:"]').count(), 0)
        self.assertEqual(narrative.locator("script").count(), 0)
        self.assertFalse(page.evaluate("Boolean(window.__markdownXss)"))
        self.assertIn(r"C:\Media\Anime", narrative.inner_text())
        self.assertIn("<script>window.__markdownXss", narrative.inner_text())

        page.set_viewport_size({"width": 390, "height": 844})
        layout = page.evaluate("""() => {
          const table = document.querySelector('.agent-md-table-scroll');
          return {
            documentWidth: document.documentElement.scrollWidth,
            viewportWidth: window.innerWidth,
            tableClientWidth: table?.clientWidth || 0,
            tableScrollWidth: table?.scrollWidth || 0,
          };
        }""")
        self.assertLessEqual(layout["documentWidth"], layout["viewportWidth"])
        self.assertGreaterEqual(layout["tableScrollWidth"], layout["tableClientWidth"])

    def test_effect_preview_can_be_cancelled_without_executing_confirm(self) -> None:
        events = [
            _event(1, "turn.started", {"kind": "query"}),
            _event(2, "effect.preview_started", {"tool": "rss.create"}),
            _event(
                3,
                "effect.approval_required",
                {
                    "tool": "rss.create",
                    "plan": {
                        "plan_id": "plan-browser-cancel-0001",
                        "tool_name": "rss.create",
                        "effect": "WRITE",
                        "preview": {
                            "summary": "创建 RSS 订阅",
                            "data": {"周期": "6 小时", "目标": "qB"},
                        },
                        "expires_at": "2026-09-03T12:05:00+00:00",
                    },
                    "result": {},
                },
            ),
            _event(4, "turn.completed", {"status": "approval_required", "answer": ""}),
        ]
        page = self.make_page({"sessions": {"sessions": []}, "queryEvents": events})
        page.locator("#agentPrompt").fill("创建一个每 6 小时刷新的 RSS 订阅")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        card = page.locator(".agent-confirmation-card")
        card.wait_for()
        self.assertIn("创建 RSS 订阅", card.inner_text())
        self.assertTrue(
            card.locator("xpath=ancestor::article[1]").evaluate(
                "node => node.classList.contains('is-confirmation') && node.getAnimations().length === 0"
            )
        )
        # 这里验证的是事件委托与取消 API；先独立断言卡片无位移动画，再直接
        # 分发点击事件，避免系统 Chrome 与 Playwright 版本差异引发 actionability
        # 层的偶发 Target crashed。
        card.locator("[data-effect-cancel]").dispatch_event("click")
        page.locator(".agent-cancelled").wait_for()
        calls = page.evaluate("window.__kernelCalls")
        paths = [call["url"] for call in calls]
        self.assertIn("/api/agent/actions/confirm/discard", paths)
        self.assertNotIn("/api/agent/actions/confirm", paths)
        self.assertIn(
            "没有执行任何写操作", page.locator(".agent-cancelled").inner_text()
        )

    def test_pending_effect_recovers_after_refresh_and_confirm_uses_event_stream(
        self,
    ) -> None:
        approval = {
            "plan_id": "plan-browser-confirm-0001",
            "tool_name": "download.pause",
            "effect": "WRITE",
            "preview": {"summary": "暂停下载任务", "data": {"任务": "测试任务"}},
            "result": {},
            "expires_at": "2026-09-03T12:05:00+00:00",
        }
        confirm_events = [
            _event(4, "turn.started", {"kind": "confirmation"}),
            _event(5, "effect.completed", {"result": {"summary": "下载任务已暂停"}}),
            _event(6, "turn.completed", {"status": "success", "answer": "下载任务已暂停"}),
        ]
        page = self.make_page(
            {
                "sessions": {
                    "sessions": [
                        {
                            "session_id": SESSION_ID,
                            "title": "下载管理",
                            "message_count": 2,
                            "updated_at": "2026-09-03T12:00:00+00:00",
                        }
                    ]
                },
                "sessionDetails": {
                    SESSION_ID: {
                        "session_id": SESSION_ID,
                        "messages": [
                            {"role": "user", "content": "暂停测试任务"},
                            {"role": "assistant", "content": "已生成暂停计划。"},
                        ],
                        "pending_approval": approval,
                    }
                },
                "confirmEvents": confirm_events,
            },
            stored_session=SESSION_ID,
        )
        card = page.locator(".agent-confirmation-card")
        card.wait_for()
        self.assertIn("暂停下载任务", card.inner_text())
        card.locator("[data-effect-confirm]").click()
        result = page.locator(".agent-result-card").filter(has_text="下载任务已暂停")
        result.wait_for()
        calls = page.evaluate("window.__kernelCalls")
        confirm_call = next(
            call for call in calls if call["url"] == "/api/agent/actions/confirm"
        )
        payload = json.loads(confirm_call["body"])
        self.assertEqual(payload["plan_id"], approval["plan_id"])
        self.assertEqual(payload["session_id"], SESSION_ID)

    def test_native_sdk_batch_card_displays_all_items_and_confirms_original_plan(self):
        for tool in ("resources.download", "cloud.change"):
            with self.subTest(tool=tool):
                approval = {
                    "plan_id": "native-sdk-plan", "tool_name": tool, "effect": "write",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "preview": {"summary": "整批20项待确认", "data": {
                        "count": 20, "resources": [{"title": f"对象 {i:02d}"} for i in range(1, 21)],
                    }},
                }
                page = self.make_page({
                    "sessions": {"sessions": [{"session_id": SESSION_ID, "title": "脚本操作"}]},
                    "sessionDetails": {SESSION_ID: {
                        "messages": [{"role": "user", "content": "处理这20项"}], "pending_approval": approval,
                    }},
                    "confirmEvents": [
                        _event(1, "turn.started", {"kind": "confirmation"}),
                        _event(2, "effect.completed", {"plan_id": approval["plan_id"], "result": {
                            "ok": True, "status": "accepted", "summary": "20项已受理，等待完成",
                        }}),
                        _event(3, "turn.completed", {"status": "success", "answer": "20项已受理，等待完成"}),
                    ],
                }, stored_session=SESSION_ID)
                card = page.locator(".agent-confirmation-card")
                card.wait_for()
                self.assertIn("20 项", card.inner_text())
                card.get_by_text("另有 12 项，展开核对", exact=True).click()
                self.assertEqual(card.locator(".agent-confirmation-list li:visible").count(), 20)
                self.assertIn("对象 20", card.inner_text())
                card.locator("[data-effect-confirm]").click()
                page.locator(".agent-result-card").filter(has_text="20项已受理").wait_for()
                calls = [call for call in page.evaluate("window.__kernelCalls")
                         if call["url"] == "/api/agent/actions/confirm"]
                self.assertEqual(len(calls), 1)
                self.assertEqual(json.loads(calls[0]["body"])["plan_id"], approval["plan_id"])
                page.close()

    def test_confirm_stream_accepts_legacy_effect_completed_turn_terminal(self):
        approval = {
            "plan_id": "plan-browser-legacy-terminal-0001",
            "tool_name": "download.pause",
            "effect": "WRITE",
            "preview": {"summary": "暂停下载任务", "data": {"任务": "测试任务"}},
            "result": {},
            "expires_at": "2026-09-03T12:05:00+00:00",
        }
        page = self.make_page({
            "sessions": {"sessions": []},
            "queryEvents": [
                _event(1, "turn.started", {"kind": "query"}),
                _event(2, "effect.approval_required", {
                    "tool": "download.pause",
                    "plan": approval,
                }),
                _event(3, "turn.completed", {"status": "approval_required", "answer": ""}),
            ],
            "confirmEvents": [
                _event(4, "turn.started", {"kind": "confirmation"}),
                _event(5, "effect.completed", {
                    "plan_id": approval["plan_id"],
                    "result": {"ok": True, "summary": "直接 API 计划已完成"},
                }),
                _event(6, "turn.completed", {"status": "effect_completed", "answer": ""}),
            ],
        })
        page.locator("#agentPrompt").fill("执行已有暂停计划")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.locator(".agent-confirmation-card").wait_for()
        page.locator("[data-effect-confirm]").click()

        result = page.locator(".agent-result-card.has-narrative").filter(has_text="直接 API 计划已完成")
        result.wait_for()
        self.assertIn("直接 API 计划已完成", result.inner_text())
        self.assertEqual(page.locator(".agent-streaming").count(), 0)
        self.assertTrue(page.locator("#agentSend").get_attribute("aria-busy") == "false")

    def test_effect_completed_without_turn_terminal_reports_unknown_after_eof(self):
        approval = {
            "plan_id": "plan-browser-incomplete-stream-0001",
            "tool_name": "download.pause",
            "effect": "WRITE",
            "preview": {"summary": "暂停下载任务", "data": {"任务": "测试任务"}},
            "result": {},
            "expires_at": "2026-09-03T12:05:00+00:00",
        }
        page = self.make_page({
            "sessions": {"sessions": []},
            "sessionSnapshotFallback": True,
            "queryEvents": [
                _event(1, "turn.started", {"kind": "query"}),
                _event(2, "effect.approval_required", {
                    "tool": "download.pause",
                    "plan": approval,
                }),
                _event(3, "turn.completed", {"status": "approval_required", "answer": ""}),
            ],
            "confirmEvents": [
                _event(4, "effect.completed", {
                    "plan_id": approval["plan_id"],
                    "result": {"ok": True, "summary": "暂停任务已提交"},
                }),
            ],
        })
        page.locator("#agentPrompt").fill("执行已有暂停计划")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.locator(".agent-confirmation-card").wait_for()
        page.locator("[data-effect-confirm]").click()

        page.wait_for_function(
            "() => document.querySelector('#agentResponseStatus').textContent.includes('任务状态未确认')"
        )
        transcript = page.locator("#agentTranscript")
        text_content = transcript.text_content()
        assert text_content is not None
        self.assertIn("暂停任务已提交", text_content)
        self.assertIn("不会自动重试", page.locator("#agentResponseStatus").inner_text())
        self.assertTrue(page.locator("#agentSend").get_attribute("aria-busy") == "false")
        self.assertFalse(page.locator("#agentSend").is_hidden())
        self.assertEqual(page.locator(".agent-streaming").count(), 0)
        self.assertEqual(page.locator(".agent-cancelled").count(), 0)
        calls = page.evaluate("window.__kernelCalls")
        self.assertEqual(sum(call["url"] == "/api/agent/query" for call in calls), 1)
        self.assertEqual(sum(call["url"] == "/api/agent/actions/confirm" for call in calls), 1)

    def test_confirm_stream_continues_after_effect_completion_until_partial_turn(self):
        approval = {
            "plan_id": "plan-browser-followup-0001",
            "tool_name": "download.pause",
            "effect": "WRITE",
            "preview": {"summary": "暂停下载任务", "data": {"任务": "测试任务"}},
            "result": {},
            "expires_at": "2026-09-03T12:05:00+00:00",
        }
        query_events = [
            _event(1, "turn.started", {"kind": "query"}),
            _event(2, "effect.approval_required", {"tool": "download.pause", "plan": approval}),
            _event(3, "turn.completed", {"status": "approval_required", "answer": ""}),
        ]
        confirm_events = [
            _event(4, "effect.completed", {
                "plan_id": approval["plan_id"],
                "result": {
                    "ok": True,
                    "status": "submitted",
                    "summary": "下载任务已暂停",
                    "data": {"succeeded": 1, "operation_ref": "GY-0000-0000-0000-0000-0000-0000-0000-0001", "stats": {"renamed": 10, "moved": 10, "strm_scope_unknown": 1}},
                },
            }),
            _event(5, "model.started", {"round": 2}),
            _event(6, "model.delta", {"round": 2, "delta": "已完成写入，继续核验。"}),
            _event(7, "tool.started", {"call_id": "verify-1", "tool": "library.check"}),
            _event(8, "tool.progress", {
                "tool": "library.check",
                "phase": "background_job",
                "summary": "正在安全等待核验回执",
                "operation_ref": "operation-followup-0001",
            }),
            _event(9, "turn.completed", {
                "status": "partial",
                "answer": "下载任务已暂停。后续核验暂时不可用，已返回本轮部分结果。",
            }),
        ]
        # 最终答复由服务端统一投影，浏览器不再根据原始effect结果补写第二份回执。
        from app.agent.public_view import format_public_result
        receipt = format_public_result(confirm_events[0]["payload"]["result"])
        confirm_events[0]["payload"]["receipt"] = receipt
        confirm_events[-1]["payload"]["answer"] = receipt + "\n\n后续核验暂时不可用，已返回本轮部分结果。"
        page = self.make_page({
            "sessions": {"sessions": []},
            "queryEvents": query_events,
            "confirmEvents": confirm_events,
            "confirmDelayMs": 40,
        })
        page.locator("#agentPrompt").fill("暂停测试任务并继续核验")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.locator(".agent-confirmation-card").wait_for()
        # 流状态只存在于相邻事件之间；在浏览器内记录，避免跨进程轮询漏掉短暂状态。
        page.evaluate("""() => {
          window.__followupStates = [];
          window.__followupObserver = new MutationObserver(() => {
            window.__followupStates.push({
              text: document.querySelector('.agent-stream-text')?.textContent || '',
              head: document.querySelector('.agent-stream-head')?.textContent || '',
              messages: document.querySelectorAll('.agent-message-assistant').length,
            });
          });
          window.__followupObserver.observe(document.querySelector('#agentTranscript'), {
            childList: true, subtree: true, characterData: true,
          });
        }""")
        page.locator("[data-effect-confirm]").click()
        narrative = page.locator(".agent-narrative")
        narrative.wait_for()
        states = page.evaluate("() => { window.__followupObserver.disconnect(); return window.__followupStates; }")
        self.assertTrue(any("已完成写入，继续核验。" in state["text"] for state in states))
        self.assertTrue(any("正在安全等待核验回执" in state["head"] for state in states))
        self.assertTrue(all(state["messages"] == 1 for state in states))
        text = narrative.inner_text()
        self.assertIn("后续核验暂时不可用", text)
        self.assertIn("下载任务已暂停", text)
        self.assertIn("GY-0000-0000-0000-0000-0000-0000-0000-0001", text)
        self.assertIn("改名 10 项", text)
        self.assertIn("移动 10 项", text)
        self.assertIn("未触发 STRM 联动", text)
        self.assertNotIn("执行失败", text)
        self.assertEqual(page.locator(".is-interrupted").count(), 0)

    def test_confirm_stream_replaces_completed_step_with_a_new_plan_only(self):
        from app.agent.public_view import format_public_result

        first = {
            "plan_id": "plan-browser-chain-0001",
            "tool_name": "download.pause",
            "effect": "WRITE",
            "preview": {"summary": "暂停下载任务", "data": {"任务": "第一步"}},
            "result": {},
            "expires_at": "2026-09-03T12:05:00+00:00",
        }
        second = {
            "plan_id": "plan-browser-chain-0002",
            "tool_name": "download.resume",
            "effect": "WRITE",
            "preview": {"summary": "继续下载任务", "data": {"任务": "第二步"}},
            "result": {},
            "expires_at": "2026-09-03T12:06:00+00:00",
        }
        third = {
            **second,
            "plan_id": "plan-browser-chain-0003",
            "preview": {"summary": "重新预检继续下载任务", "data": {"任务": "第三步"}},
        }
        first_result = {"ok": True, "status": "completed", "summary": "第一步已完成"}
        second_result = {"ok": False, "status": "failed", "summary": "第二步未执行，下载任务状态已变化"}
        first_receipt = format_public_result(first_result)
        second_receipt = format_public_result(second_result)
        page = self.make_page({
            "sessions": {"sessions": []},
            "queryEvents": [
                _event(1, "turn.started", {"kind": "query"}),
                _event(2, "effect.approval_required", {"tool": "download.pause", "plan": first}),
                _event(3, "turn.completed", {"status": "approval_required", "answer": ""}),
            ],
            "confirmEvents": [
                _event(4, "effect.completed", {
                    "plan_id": first["plan_id"],
                    "result": first_result,
                    "receipt": first_receipt,
                }),
                _event(5, "model.started", {"round": 2}),
                _event(6, "model.delta", {"round": 2, "delta": "准备第二步。"}),
                _event(7, "effect.approval_required", {"tool": "download.resume", "plan": second}),
                _event(8, "turn.completed", {"status": "approval_required", "answer": ""}),
            ],
        })
        page.locator("#agentPrompt").fill("先暂停，再按条件继续")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.locator("[data-effect-confirm]").click()

        next_card = page.locator('.agent-confirmation-card[data-plan-id="plan-browser-chain-0002"]')
        next_card.wait_for()
        self.assertEqual(page.locator(".agent-confirmation-card").count(), 1)
        self.assertEqual(page.locator('[data-effect-confirm="plan-browser-chain-0001"]').count(), 0)
        self.assertTrue(next_card.locator('[data-effect-confirm="plan-browser-chain-0002"]').is_enabled())
        self.assertIn("第一步已完成", next_card.inner_text())
        self.assertEqual(next_card.locator(".agent-confirmation-previous .agent-confirmation-result").inner_text(),
                         first_receipt)
        page.wait_for_function("() => document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
        self.assertEqual(page.evaluate(
            "window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').map(call => JSON.parse(call.body).plan_id)"
        ), [first["plan_id"]])

        page.evaluate("events => { window.__kernelConfig.confirmEvents = events; }", [
            _event(9, "effect.failed", {
                "plan_id": second["plan_id"],
                "code": "confirmation_stale",
                "message": second_result["summary"],
                "result": second_result,
                "receipt": second_receipt,
            }),
            _event(10, "effect.approval_required", {"tool": "download.resume", "plan": third}),
            _event(11, "turn.completed", {"status": "approval_required", "answer": "已重新预检，尚未执行新计划"}),
        ])
        next_card.locator("[data-effect-confirm]").click()

        refreshed_card = page.locator('.agent-confirmation-card[data-plan-id="plan-browser-chain-0003"]')
        refreshed_card.wait_for()
        self.assertEqual(page.locator(".agent-confirmation-card").count(), 1)
        self.assertEqual(page.locator('[data-effect-confirm="plan-browser-chain-0002"]').count(), 0)
        self.assertTrue(refreshed_card.locator('[data-effect-confirm="plan-browser-chain-0003"]').is_enabled())
        previous = refreshed_card.locator(".agent-confirmation-previous")
        self.assertIn("上一项操作的执行结果", previous.inner_text())
        self.assertEqual(previous.locator(".agent-confirmation-result").inner_text(), second_receipt)
        self.assertNotIn("第一步已完成", refreshed_card.inner_text())
        self.assertIn("重新预检继续下载任务", refreshed_card.inner_text())
        page.wait_for_function("() => document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
        self.assertEqual(page.evaluate(
            "window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').map(call => JSON.parse(call.body).plan_id)"
        ), [first["plan_id"], second["plan_id"]])

        # 显式确认 C 后流程失败；C 没有执行回执，最终汇总仍须保留 A/B 的事实。
        page.evaluate("events => { window.__kernelConfig.confirmEvents = events; }", [
            _event(12, "turn.failed", {"message": "模型暂时不可用"}),
        ])
        refreshed_card.locator("[data-effect-confirm]").click()
        narrative = page.locator(".agent-result-card.has-narrative .agent-narrative")
        narrative.wait_for()
        text = narrative.inner_text()
        self.assertEqual(text.count(first_receipt), 1)
        self.assertEqual(text.count(second_receipt), 1)
        page.wait_for_function("() => document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
        self.assertEqual(page.evaluate(
            "window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').map(call => JSON.parse(call.body).plan_id)"
        ), [first["plan_id"], second["plan_id"], third["plan_id"]])
        self.assertEqual(
            page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/query').length"), 1,
        )

    def test_preauthorized_effect_receipt_precedes_next_confirmation(self):
        from app.agent.public_view import format_public_result

        earlier = {
            "plan_id": "plan-browser-before-trust-0001", "tool_name": "download.pause",
            "effect": "WRITE", "preview": {"summary": "暂停下载任务"},
        }
        pending = {
            "plan_id": "plan-browser-after-trust-0003", "tool_name": "watchlist.add",
            "effect": "WRITE", "preview": {"summary": "电影收藏仍需确认"},
        }
        earlier_result = {"ok": True, "status": "completed", "summary": "此前暂停已完成"}
        trusted_result = {"ok": True, "status": "completed", "summary": "预授权剧集收藏已保存"}
        earlier_receipt = format_public_result(earlier_result)
        trusted_receipt = format_public_result(trusted_result)
        following_events = [
            _event(5, "effect.completed", {
                "plan_id": "plan-browser-trusted-0002", "tool": "watchlist.add",
                "result": trusted_result, "receipt": trusted_receipt,
                "scoped_trust": {"active": False},
            }),
            _event(6, "effect.approval_required", {"plan": pending}),
            _event(7, "turn.completed", {"status": "approval_required"}),
        ]
        for has_earlier_card in (False, True):
            with self.subTest(has_earlier_card=has_earlier_card):
                query_events = [_event(1, "turn.started", {"kind": "query"})]
                if has_earlier_card:
                    query_events.extend([
                        _event(2, "effect.approval_required", {"plan": earlier}),
                        _event(3, "turn.completed", {"status": "approval_required"}),
                    ])
                else:
                    query_events.extend(following_events)
                page = self.make_page({
                    "sessions": {"sessions": []}, "queryEvents": query_events,
                    "confirmEvents": [
                        _event(4, "effect.completed", {
                            "plan_id": earlier["plan_id"], "result": earlier_result,
                            "receipt": earlier_receipt,
                        }),
                        *following_events,
                    ],
                })
                page.locator("#agentPrompt").fill("按已有授权收藏剧集，再预检电影收藏")
                page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
                if has_earlier_card:
                    page.locator(f'[data-effect-confirm="{earlier["plan_id"]}"]').click()

                card = page.locator(f'.agent-confirmation-card[data-plan-id="{pending["plan_id"]}"]')
                card.wait_for()
                previous = card.locator(".agent-confirmation-previous")
                self.assertEqual(previous.count(), 1)
                self.assertIn("上一项操作的执行结果", previous.inner_text())
                self.assertEqual(previous.locator(".agent-confirmation-result").inner_text(), trusted_receipt)
                self.assertNotIn("此前暂停已完成", card.inner_text())
                self.assertTrue(card.locator("[data-effect-confirm]").is_enabled())
                page.wait_for_function("() => document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
                confirmed_ids = [earlier["plan_id"]] if has_earlier_card else []
                self.assertEqual(page.evaluate(
                    "window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').map(call => JSON.parse(call.body).plan_id)"
                ), confirmed_ids)

                # 新卡仍需显式确认；后续失败不能丢掉预授权或更早卡片的累计回执。
                page.evaluate("events => { window.__kernelConfig.confirmEvents = events; }", [
                    _event(8, "turn.failed", {"message": "后续检查未完成"}),
                ])
                card.locator("[data-effect-confirm]").click()
                narrative = page.locator(".agent-result-card.has-narrative .agent-narrative")
                narrative.wait_for()
                text = narrative.inner_text()
                self.assertEqual(text.count(trusted_receipt), 1)
                self.assertEqual(text.count(earlier_receipt), int(has_earlier_card))
                self.assertIn("后续流程未完成", text)
                page.wait_for_function("() => document.querySelector('#agentSend').getAttribute('aria-busy') === 'false'")
                self.assertEqual(page.evaluate(
                    "window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').map(call => JSON.parse(call.body).plan_id)"
                ), confirmed_ids + [pending["plan_id"]])

    def test_changed_snapshot_keeps_new_confirmation_actionable_without_resubmission(self):
        first = {"plan_id": "plan-stale-original-0001", "tool_name": "cloud.clear_recycle",
                 "effect": "DANGER", "preview": {"summary": "确认清空2项"}}
        second = {**first, "plan_id": "plan-stale-refreshed-0002", "preview": {"summary": "确认清空3项"}}
        page = self.make_page({
            "sessions": {"sessions": []},
            "queryEvents": [
                _event(1, "turn.started"),
                _event(2, "effect.approval_required", {"plan": first}),
                _event(3, "turn.completed", {"status": "approval_required"}),
            ],
            "confirmEvents": [
                _event(4, "effect.failed", {"plan_id": first["plan_id"], "code": "confirmation_stale",
                                           "message": "回收站内容已变化", "result": {"ok": False, "summary": "回收站内容已变化"}}),
                _event(5, "effect.approval_required", {"plan": second}),
                _event(6, "turn.completed", {"status": "approval_required", "answer": "已重新预检，尚未执行新计划"}),
            ],
        })
        page.locator("#agentPrompt").fill("清空回收站")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.locator("[data-effect-confirm]").click()
        next_card = page.locator('[data-plan-id="plan-stale-refreshed-0002"]')
        next_card.wait_for()
        self.assertEqual(page.locator(".agent-confirmation-card").count(), 1)
        self.assertTrue(next_card.locator("[data-effect-confirm]").is_enabled())
        self.assertIn("确认清空3项", next_card.inner_text())
        # 旧卡未执行的原因须置顶并标明归属，不能被读成新计划的结果。
        previous = next_card.locator(".agent-confirmation-previous")
        self.assertIn("上一项操作的执行结果", previous.inner_text())
        self.assertIn("回收站内容已变化", previous.inner_text())
        self.assertTrue(previous.evaluate(
            "node => !!(node.compareDocumentPosition(node.parentNode.querySelector('.agent-confirmation-intro'))"
            " & Node.DOCUMENT_POSITION_FOLLOWING)"))
        self.assertEqual(page.locator('[data-effect-confirm="plan-stale-original-0001"]').count(), 0)
        self.assertEqual(page.evaluate("window.__kernelCalls.filter(call => call.url === '/api/agent/actions/confirm').length"), 1)

    def test_confirm_stream_keeps_trusted_receipt_when_followup_turn_fails(self):
        approval = {
            "plan_id": "plan-browser-partial-0001",
            "tool_name": "download.pause",
            "effect": "WRITE",
            "preview": {"summary": "暂停下载任务", "data": {"任务": "测试任务"}},
            "result": {},
            "expires_at": "2026-09-03T12:05:00+00:00",
        }
        page = self.make_page({
            "sessions": {"sessions": []},
            "queryEvents": [
                _event(1, "turn.started", {"kind": "query"}),
                _event(2, "effect.approval_required", {"tool": "download.pause", "plan": approval}),
                _event(3, "turn.completed", {"status": "approval_required", "answer": ""}),
            ],
            "confirmEvents": [
                _event(4, "effect.completed", {
                    "plan_id": approval["plan_id"],
                    "result": {"ok": True, "summary": "下载任务已暂停"},
                }),
                _event(5, "model.started", {"round": 2}),
                _event(6, "model.delta", {"round": 2, "delta": "继续查询媒体服务。"}),
                _event(7, "turn.failed", {"message": "模型暂时不可用"}),
            ],
        })
        page.locator("#agentPrompt").fill("暂停并继续查询")
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.locator("[data-effect-confirm]").click()

        result = page.locator(".agent-result-card.has-narrative")
        result.wait_for()
        text = result.inner_text()
        self.assertIn("下载任务已暂停", text)
        self.assertIn("后续流程未完成", text)
        self.assertNotIn("执行失败", text)

    def test_release_format_hash_is_ordinary_on_desktop_and_mobile(self) -> None:
        viewports: tuple[ViewportSize, ...] = (
            {"width": 1280, "height": 800},
            {"width": 390, "height": 844},
        )
        for viewport in viewports:
            with self.subTest(viewport=viewport):
                page = self.make_page(
                    {"sessions": {"sessions": []}},
                    viewport=viewport,
                    hash_fragment="#release-format-teaching",
                )
                page.wait_for_function(
                    "() => window.__kernelCalls.some(call => call.url === '/api/agent/sessions')"
                )
                self.assertEqual(
                    page.evaluate("() => location.hash"), "#release-format-teaching"
                )
                self.assertEqual(page.locator("#agentPrompt").input_value(), "")
                self.assertEqual(page.locator(".agent-console.is-empty").count(), 1)
                self.assertEqual(
                    page.locator(
                        "#agentReleaseFormatGuide, .agent-release-format-guide, "
                        "[data-agent-draft], [data-release-format-teaching]"
                    ).count(),
                    0,
                )
                paths = [call["url"] for call in page.evaluate("window.__kernelCalls")]
                self.assertNotIn("/api/agent/query", paths)
                self.assertNotIn("/api/agent/actions/confirm", paths)
                self.assertNotIn("/api/agent/actions/confirm/discard", paths)

                if viewport["width"] <= 390:
                    layout = page.evaluate("""() => {
                        const composer = document.querySelector('.agent-composer').getBoundingClientRect();
                        return {
                            documentWidth: document.documentElement.scrollWidth,
                            bodyWidth: document.body.scrollWidth,
                            viewportWidth: window.innerWidth,
                            composerLeft: composer.left,
                            composerRight: composer.right,
                        };
                    }""")
                    self.assertLessEqual(layout["documentWidth"], layout["viewportWidth"])
                    self.assertLessEqual(layout["bodyWidth"], layout["viewportWidth"])
                    self.assertGreaterEqual(layout["composerLeft"], -0.5)
                    self.assertLessEqual(
                        layout["composerRight"], layout["viewportWidth"] + 0.5
                    )

    def test_release_format_hash_does_not_inject_or_replace_existing_draft(self) -> None:
        draft = "这是我已经写好的发布格式草稿，请不要替换。"
        page = self.make_page(
            {"sessions": {"sessions": []}},
            hash_fragment="#release-format-teaching",
            initial_prompt=draft,
        )
        self.assertEqual(page.locator("#agentPrompt").input_value(), draft)
        self.assertEqual(page.locator(".agent-console.is-empty").count(), 1)
        before_calls = len(page.evaluate("window.__kernelCalls"))
        page.evaluate("""() => {
            history.replaceState(null, '', '#other');
            window.dispatchEvent(new Event('hashchange'));
            history.replaceState(null, '', '#release-format-teaching');
            window.dispatchEvent(new Event('hashchange'));
        }""")
        self.assertEqual(page.evaluate("() => location.hash"), "#release-format-teaching")
        self.assertEqual(page.locator("#agentPrompt").input_value(), draft)
        self.assertEqual(len(page.evaluate("window.__kernelCalls")), before_calls)
        paths = [call["url"] for call in page.evaluate("window.__kernelCalls")]
        self.assertNotIn("/api/agent/query", paths)
        self.assertNotIn("/api/agent/actions/confirm", paths)
        self.assertNotIn("/api/agent/actions/confirm/discard", paths)

    def test_release_format_hash_does_not_change_pending_approval_or_session(self) -> None:
        approval = {
            "plan_id": "plan-browser-release-format-0001",
            "tool_name": "recognition.save_release_format",
            "effect": "WRITE",
            "preview": {"summary": "保存发布格式规则", "data": {"规则": "示例规则"}},
            "result": {},
            "expires_at": "2026-09-03T12:05:00+00:00",
        }
        page = self.make_page(
            {
                "sessions": {
                    "sessions": [
                        {
                            "session_id": SESSION_ID,
                            "title": "发布格式规则",
                            "message_count": 2,
                            "updated_at": "2026-09-03T12:00:00+00:00",
                        }
                    ]
                },
                "sessionDetails": {
                    SESSION_ID: {
                        "session_id": SESSION_ID,
                        "messages": [
                            {"role": "user", "content": "准备保存发布格式规则"},
                            {"role": "assistant", "content": "已生成待确认计划。"},
                        ],
                        "pending_approval": approval,
                    }
                },
            },
            hash_fragment="#release-format-teaching",
            stored_session=SESSION_ID,
        )
        card = page.locator(".agent-confirmation-card")
        card.wait_for(state="visible")
        self.assertEqual(page.locator("#agentPrompt").input_value(), "")
        self.assertTrue(card.is_visible())
        self.assertEqual(
            page.evaluate(
                "() => localStorage.getItem('mediaflux.agent.kernel.session.v1')"
            ),
            SESSION_ID,
        )
        self.assertEqual(
            page.evaluate("() => location.hash"), "#release-format-teaching"
        )
        paths = [call["url"] for call in page.evaluate("window.__kernelCalls")]
        self.assertNotIn("/api/agent/query", paths)
        self.assertNotIn("/api/agent/actions/confirm", paths)
        self.assertNotIn("/api/agent/actions/confirm/discard", paths)

    def test_release_format_hash_does_not_change_existing_conversation_or_session(self) -> None:
        page = self.make_page(
            {
                "sessions": {
                    "sessions": [
                        {
                            "session_id": SESSION_ID,
                            "title": "已有 Agent 对话",
                            "message_count": 2,
                            "updated_at": "2026-09-03T12:00:00+00:00",
                        }
                    ]
                },
                "sessionDetails": {
                    SESSION_ID: {
                        "session_id": SESSION_ID,
                        "messages": [
                            {"role": "user", "content": "检查媒体库状态"},
                            {"role": "assistant", "content": "检查已完成。"},
                        ],
                    }
                },
            },
            hash_fragment="#release-format-teaching",
            stored_session=SESSION_ID,
        )
        page.locator(".agent-message-assistant").wait_for(state="visible")
        self.assertEqual(page.locator("#agentPrompt").input_value(), "")
        self.assertEqual(page.locator(".agent-console.is-empty").count(), 0)
        self.assertEqual(
            page.evaluate(
                "() => localStorage.getItem('mediaflux.agent.kernel.session.v1')"
            ),
            SESSION_ID,
        )
        self.assertEqual(
            page.evaluate("() => location.hash"), "#release-format-teaching"
        )
        paths = [call["url"] for call in page.evaluate("window.__kernelCalls")]
        self.assertNotIn("/api/agent/query", paths)

    def test_empty_composer_centers_prompt_with_leading_action(self) -> None:
        page = self.make_page({"sessions": {"sessions": []}})
        self.assertEqual(page.locator("#agentReleaseFormatGuide").count(), 0)
        layout = page.evaluate("""() => {
          const centerY = (selector) => {
            const rect = document.querySelector(selector).getBoundingClientRect();
            return rect.top + rect.height / 2;
          };
          const prompt = document.querySelector('#agentPrompt');
          const promptStyle = getComputedStyle(prompt);
          return {
            promptDisplay: promptStyle.display,
            promptPaddingTop: parseFloat(promptStyle.paddingTop),
            promptPaddingBottom: parseFloat(promptStyle.paddingBottom),
            promptCenter: centerY('#agentPrompt'),
            settingsCenter: centerY('#agentSettings'),
            sendCenter: centerY('#agentSend'),
          };
        }""")
        self.assertEqual(layout["promptDisplay"], "block")
        self.assertEqual(layout["promptPaddingTop"], 9)
        self.assertEqual(layout["promptPaddingBottom"], 7)
        self.assertAlmostEqual(
            layout["promptCenter"], layout["settingsCenter"], delta=0.5
        )
        self.assertAlmostEqual(layout["promptCenter"], layout["sendCenter"], delta=0.5)

    def test_mobile_composer_stays_inside_viewport_and_stop_has_stable_slot(
        self,
    ) -> None:
        events = [_event(1, "turn.started", {"kind": "query"})]
        page = self.make_page(
            {
                "sessions": {"sessions": []},
                "queryEvents": events,
                "holdQueryOpen": True,
            },
            viewport={"width": 390, "height": 844},
        )
        page.locator("#agentPrompt").fill("执行一个较长的只读巡检")
        before = page.locator(".agent-submit-slot").bounding_box()
        send_before = page.locator("#agentSend").bounding_box()
        assert send_before is not None
        self.assertAlmostEqual(send_before["width"], send_before["height"], delta=0.5)
        assert before is not None
        self.assertAlmostEqual(send_before["width"], before["width"], delta=0.5)
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.wait_for_function("document.querySelector('#agentSend').getAttribute('aria-busy') === 'true'")
        during = page.locator(".agent-submit-slot").bounding_box()
        assert during is not None
        self.assertAlmostEqual(before["width"], during["width"], delta=0.5)
        self.assertAlmostEqual(before["height"], during["height"], delta=0.5)
        layout = page.evaluate("""() => {
          const composer = document.querySelector('.agent-composer').getBoundingClientRect();
          return {
            documentWidth: document.documentElement.scrollWidth,
            viewportWidth: window.innerWidth,
            composerLeft: composer.left,
            composerRight: composer.right,
          };
        }""")
        self.assertLessEqual(layout["documentWidth"], layout["viewportWidth"])
        self.assertGreaterEqual(layout["composerLeft"], -0.5)
        self.assertLessEqual(layout["composerRight"], layout["viewportWidth"] + 0.5)
        page.locator("#agentStopSession").click()
        page.locator(".agent-cancelled").wait_for()
        paths = [call["url"] for call in page.evaluate("window.__kernelCalls")]
        self.assertIn("/api/agent/session/stop", paths)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
