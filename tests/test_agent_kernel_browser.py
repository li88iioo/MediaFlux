"""新 Agent Kernel Web 事件适配器的真实浏览器回归。"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

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
    configured = str(os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "").strip()
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
                  <button type="button" class="agent-stop" id="agentStop" hidden disabled>停止</button>
                </span>
              </div>
            </div>
          </form>
        </section>
        <dialog class="agent-rail agent-history-drawer" id="agentHistoryRail">
          <section class="agent-rail-section agent-session-panel">
            <div class="agent-session-heading">
              <div><span class="agent-kicker">HISTORY</span><h3>最近会话</h3></div>
              <div class="agent-session-heading-actions">
                <span class="agent-session-count" id="agentSessionCount">0</span>
                <button type="button" class="agent-history-close" data-agent-history-close>关闭</button>
              </div>
            </div>
            <div class="agent-session-list" id="agentSessionList" aria-busy="true"></div>
            <p id="agentSessionStatus" role="status"></p>
          </section>
        </dialog>
        <p id="agentResponseStatus" role="status"></p>
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

  const streamResponse = (events, options, delayMs = 0, holdOpen = false) => {
    const encoder = new TextEncoder();
    let timer = null;
    let streamController = null;
    let index = 0;
    const stream = new ReadableStream({
      start(controller) {
        streamController = controller;
        const push = () => {
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
    if (path === '/api/agent/sessions') return jsonResponse(window.__kernelConfig.sessions || {sessions: []});
    if (path.startsWith('/api/agent/sessions/') && (options.method || 'GET') === 'GET') {
      const id = decodeURIComponent(path.split('/').pop());
      return jsonResponse((window.__kernelConfig.sessionDetails || {})[id] || {error: '会话不存在'},
        (window.__kernelConfig.sessionDetails || {})[id] ? 200 : 404);
    }
    if (path.startsWith('/api/agent/sessions/') && options.method === 'DELETE') return jsonResponse({deleted: true});
    if (path === '/api/agent/query') {
      return streamResponse(
        window.__kernelConfig.queryEvents || [],
        options,
        Number(window.__kernelConfig.queryDelayMs || 0),
        Boolean(window.__kernelConfig.holdQueryOpen),
      );
    }
    if (path === '/api/agent/query/cancel') return jsonResponse({cancelled: true});
    if (path === '/api/agent/actions/confirm') {
      return streamResponse(window.__kernelConfig.confirmEvents || [], options, 0, false);
    }
    if (path === '/api/agent/actions/confirm/discard') return jsonResponse({discarded: true});
    throw new Error(`unexpected endpoint: ${path}`);
  };
}
"""


@unittest.skipIf(sync_playwright is None, "系统环境未安装 Playwright")
class AgentKernelBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        executable_path = _chromium_executable(cls.playwright)
        launch_options = {
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
        self,
        config: dict | None = None,
        *,
        viewport: dict[str, int] | None = None,
        stored_session: str = "",
    ):
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
        page.goto("http://mediaflux.test/agent")
        page.add_style_tag(content=self.styles)
        if stored_session:
            page.evaluate(
                "([key, value]) => localStorage.setItem(key, value)",
                ["mediaflux.agent.kernel.session.v1", stored_session],
            )
        page.evaluate(MOCK_FETCH, config or {"sessions": {"sessions": []}})
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
        page.wait_for_function(
            "() => document.querySelector('.agent-stream-steps')?.textContent.includes('查询媒体库完成')"
        )
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
            _event(1, "turn.started", {"kind": "confirmation"}),
            _event(2, "effect.completed", {"result": {"summary": "下载任务已暂停"}}),
            _event(3, "turn.completed", {"status": "effect_completed", "answer": ""}),
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

    def test_empty_composer_centers_prompt_with_leading_action(self) -> None:
        page = self.make_page({"sessions": {"sessions": []}})
        layout = page.evaluate("""() => {
          const centerY = (selector) => {
            const rect = document.querySelector(selector).getBoundingClientRect();
            return rect.top + rect.height / 2;
          };
          const prompt = document.querySelector('#agentPrompt');
          return {
            promptDisplay: getComputedStyle(prompt).display,
            promptCenter: centerY('#agentPrompt'),
            settingsCenter: centerY('#agentSettings'),
            sendCenter: centerY('#agentSend'),
          };
        }""")
        self.assertEqual(layout["promptDisplay"], "block")
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
        page.locator("#agentComposer").evaluate("form => form.requestSubmit()")
        page.locator("#agentStop").wait_for(state="visible")
        during = page.locator(".agent-submit-slot").bounding_box()
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
        page.locator("#agentStop").click()
        page.locator(".agent-cancelled").wait_for()
        paths = [call["url"] for call in page.evaluate("window.__kernelCalls")]
        self.assertIn("/api/agent/query/cancel", paths)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
