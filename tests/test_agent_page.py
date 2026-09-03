"""Media Agent 工作区页面与原生前端安全契约。"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import web_credentials
from app.main import create_app
from tests.support import InitializedWebTestCase

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app" / "templates" / "agent.html"
BASE_TEMPLATE = ROOT / "app" / "templates" / "base.html"
SCRIPT = ROOT / "app" / "static" / "js" / "agent.js"
APP_SCRIPT = ROOT / "app" / "static" / "js" / "app.js"
STYLES = ROOT / "app" / "static" / "css" / "agent.css"


class AgentPageTests(InitializedWebTestCase):
    def setUp(self):
        self.client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )

    def tearDown(self):
        self.client.close()

    @staticmethod
    def _csrf_token(response) -> str:
        match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
        if not match:
            match = re.search(
                r'name="csrf_token" (?:content|value)="([^"]+)"', response.text
            )
        if not match:
            raise AssertionError("页面未输出 CSRF Token")
        return match.group(1)

    def _login(self) -> None:
        login_page = self.client.get("/login")
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={
                "csrf_token": self._csrf_token(login_page),
                "username": username,
                "password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def test_anonymous_agent_redirects_to_login(self):
        response = self.client.get("/agent", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("location"), "http://testserver/login")

    def test_global_switch_hides_navigation_and_redirects_agent_page(self):
        self._login()
        with (
            patch("app.routes.pages.is_agent_enabled", return_value=False),
            patch("app.agent.feature_gate.is_agent_enabled", return_value=False),
        ):
            response = self.client.get("/agent", follow_redirects=False)
            dashboard = self.client.get("/")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers.get("location"), "/settings?agent_disabled=1#agent"
        )
        self.assertNotIn("<span>Agent</span>", dashboard.text)

    def test_authenticated_agent_renders_stable_semantic_shell(self):
        self._login()
        response = self.client.get("/agent")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count("<main"), 1)
        self.assertIn('class="agent-page"', response.text)
        self.assertIn('id="agentTranscript" role="log" aria-live="off"', response.text)
        self.assertIn(
            'id="agentResponseStatus" role="status" aria-live="polite" aria-atomic="true"',
            response.text,
        )
        self.assertIn('id="agentComposer"', response.text)
        self.assertIn('id="agentPrompt"', response.text)
        self.assertIn('maxlength="1000"', response.text)
        self.assertIn('id="agentSend"', response.text)
        self.assertIn('id="agentStop"', response.text)
        self.assertIn('id="agentNewSession"', response.text)
        self.assertIn('id="agentNewSession" aria-label="新会话"', response.text)
        self.assertNotIn('class="workspace-bar"', response.text)
        self.assertNotIn('class="agent-console-head"', response.text)
        self.assertIn('class="agent-composer-header"', response.text)
        self.assertIn('id="toggleAgentRail"', response.text)
        self.assertIn('id="agentHistoryRail"', response.text)
        self.assertIn("/settings#agent", response.text)
        self.assertIn('id="agentSessionList"', response.text)
        self.assertIn('id="agentSessionCount"', response.text)
        self.assertRegex(
            response.text,
            re.compile(r'<a href="/agent" class="nav-item active"[^>]*>'),
        )
        self.assertEqual(response.text.count("/static/css/agent.css"), 1)
        self.assertEqual(response.text.count("/static/js/agent.js"), 1)
        self.assertRegex(response.text, r"/static/css/agent\.css\?v=[0-9a-f]{16}")
        self.assertRegex(response.text, r"/static/js/agent\.js\?v=[0-9a-f]{16}")
        self.assertIn('id="agentResumeLatestSession"', response.text)
        self.assertIn("继续上次", response.text)
        self.assertNotRegex(
            response.text, re.compile(r"(?:API_KEY|PASSWORD|TOKEN)=", re.IGNORECASE)
        )
        self.assertIn('class="agent-console is-empty"', response.text)
        self.assertIn('id="agentEmptyIntro"', response.text)
        self.assertIn("直接描述你想处理的任务", response.text)
        self.assertIn('placeholder="询问 MediaFlux"', response.text)
        self.assertIn('data-active-placeholder="继续描述或调整任务"', response.text)
        self.assertNotIn("START HERE", response.text)
        self.assertNotIn("data-agent-welcome", response.text)
        self.assertNotIn("agent-message-system", response.text)
        self.assertNotIn("常用任务", response.text)
        self.assertNotIn("常用 Agent 任务", response.text)
        self.assertNotIn('id="toggleAgentShortcuts"', response.text)
        self.assertNotIn('id="agentShortcutsTimeline"', response.text)
        self.assertNotIn("data-agent-prompt=", response.text)
        self.assertNotIn("data-agent-draft=", response.text)

        styles = STYLES.read_text(encoding="utf-8")
        self.assertNotIn(".agent-shortcuts-trigger", styles)
        self.assertNotIn(".agent-shortcuts-timeline-popover", styles)
        self.assertNotIn(".agent-timeline-task", styles)
        self.assertIn(".agent-console.is-empty", styles)
        self.assertIn(".agent-empty-intro", styles)
        self.assertNotIn(".agent-message-system", styles)
        self.assertNotIn(".agent-starter-grid", styles)
        self.assertIn("--agent-reading-width: 980px", styles)
        self.assertEqual(styles.count("var(--agent-reading-width, 980px)"), 3)

    def test_agent_frontend_consumes_bounded_public_event_stream(self):
        source = SCRIPT.read_text(encoding="utf-8")
        styles = STYLES.read_text(encoding="utf-8")

        self.assertIn("const MAX_TRANSCRIPT_ITEMS = 120", source)
        self.assertIn("function pruneTranscript()", source)
        self.assertIn("async function readEventStream(response, consume)", source)
        for event_type in (
            "turn.started",
            "capabilities.selected",
            "model.delta",
            "model.tool_call",
            "tool.started",
            "tool.progress",
            "tool.completed",
            "tool.failed",
            "effect.approval_required",
            "turn.completed",
            "turn.failed",
            "turn.cancelled",
        ):
            self.assertIn(f"case '{event_type}'", source)
        self.assertNotIn("step.arguments", source)
        self.assertNotIn("payload.arguments", source)
        self.assertIn(".agent-stream-steps", styles)
        self.assertIn(".agent-stream-step", styles)

    def test_agent_frontend_renders_effect_plan_as_safe_confirmation_card(self):
        source = SCRIPT.read_text(encoding="utf-8")
        styles = STYLES.read_text(encoding="utf-8")

        self.assertIn("function buildApproval(approval)", source)
        self.assertIn("系统只冻结了计划，尚未写入任何变更。", source)
        self.assertIn("cancel.dataset.effectCancel", source)
        self.assertIn("confirm.dataset.effectConfirm", source)
        self.assertIn("/api/agent/actions/confirm", source)
        self.assertIn("/api/agent/actions/confirm/discard", source)
        self.assertIn("effect.approval_required", source)
        self.assertIn("effect.completed", source)
        self.assertIn("effect.failed", source)
        self.assertIn(".agent-confirmation-card", styles)
        self.assertIn(".agent-confirmation-actions", styles)
        self.assertIn("textContent", source)
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("insertAdjacentHTML", source)

    def test_agent_frontend_streams_one_model_answer_without_presentation_pass(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("case 'model.delta'", source)
        self.assertIn("turn.text.textContent = value", source)
        self.assertIn("payload.answer", source)
        self.assertIn("function finalizeAnswer(turn, text)", source)
        self.assertEqual(source.count("fetch('/api/agent/query'"), 1)
        self.assertNotIn("presentation", source.casefold())
        self.assertNotIn("agent_trace", source)

    def test_agent_frontend_is_a_thin_event_adapter_without_domain_routes(self):
        source = SCRIPT.read_text(encoding="utf-8")

        for endpoint in (
            "/api/agent/query",
            "/api/agent/query/cancel",
            "/api/agent/actions/confirm",
            "/api/agent/actions/confirm/discard",
            "/api/agent/sessions",
        ):
            self.assertIn(endpoint, source)
        for legacy_endpoint in (
            "/api/agent/tools/",
            "/api/agent/workspace-actions/invoke",
            "/api/agent/actions/${encodeURIComponent(action.tool)}/prepare",
        ):
            self.assertNotIn(legacy_endpoint, source)
        for legacy_renderer in (
            "renderWorkspaceOverview",
            "renderReadPlan",
            "renderSpecializedData",
            "renderInspectionTrace",
            "renderIndexerSearch",
            "renderLibraryAudit",
        ):
            self.assertNotIn(legacy_renderer, source)
        self.assertIn("const TOOL_LABELS", source)
        self.assertIn("applyEvent(turn, event)", source)

    def test_agent_frontend_uses_safe_interruptible_confirmation_flow(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("const controller = new AbortController()", source)
        self.assertIn(
            "activeRequest = {controller, requestId, turn, sessionId}", source
        )
        self.assertIn("if (activeRequest?.requestId !== requestId) return", source)
        self.assertIn("/api/agent/query/cancel", source)
        self.assertIn("active.controller.abort()", source)
        self.assertIn("expireVisibleApprovals()", source)
        self.assertIn("body: JSON.stringify({", source)
        self.assertIn("plan_id: planId", source)
        self.assertIn("stream: true", source)
        self.assertIn("正在执行已确认计划", source)
        self.assertIn("执行完成前不会接受另一项写操作。", source)
        self.assertNotIn("confirmationInFlight", source)
        self.assertIn("event.key === 'Enter'", source)
        self.assertIn("event.shiftKey", source)

        app_source = APP_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('meta[name="csrf-token"]', app_source)
        self.assertIn("X-CSRF-Token", app_source)

    def test_agent_history_mobile_and_accessibility_contracts(self):
        source = SCRIPT.read_text(encoding="utf-8")
        styles = STYLES.read_text(encoding="utf-8")
        template = TEMPLATE.read_text(encoding="utf-8")

        self.assertIn('role="log" aria-live="off"', template)
        self.assertIn('role="status" aria-live="polite"', template)
        self.assertIn('aria-controls="agentHistoryRail"', template)
        self.assertIn("data-agent-history-close", template)
        self.assertIn("historyRail.showModal()", source)
        self.assertIn("historyRail.close()", source)
        self.assertIn("historyButton?.setAttribute('aria-expanded', 'true')", source)
        self.assertIn("historyButton?.setAttribute('aria-expanded', 'false')", source)
        self.assertIn("historyController?.abort()", source)
        self.assertIn("if (generation !== sessionLoadGeneration) return", source)
        self.assertIn("--agent-viewport-height", source)
        self.assertIn(
            "window.visualViewport?.addEventListener('resize', syncViewportHeight",
            source,
        )
        self.assertIn("window.addEventListener('resize', syncViewportHeight", source)
        self.assertIn("const SESSION_RE = /^[A-Za-z0-9_-]{16,64}$/", source)
        self.assertRegex(
            styles,
            re.compile(
                r"@media \(max-width: 680px\)[\s\S]*?"
                r"\.agent-action-btn \{[^}]*width: 44px;[^}]*height: 44px;",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            styles,
            re.compile(
                r"@media \(max-width: 680px\)[\s\S]*?"
                r"\.agent-send, \.agent-stop \{[^}]*width: 44px;[^}]*height: 44px;",
                re.DOTALL,
            ),
        )
        self.assertNotIn('id="closeAgentShortcuts"', template)

    def test_agent_styles_reserve_layout_and_support_mobile_and_reduced_motion(self):
        source = STYLES.read_text(encoding="utf-8")
        self.assertRegex(
            source,
            re.compile(
                r"\.agent-transcript\s*\{[^}]*flex:\s*1[^}]*"
                r"min-height:\s*0[^}]*overflow-y:\s*auto",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"\.agent-submit-slot\s*\{[^}]*width:\s*44px[^}]*"
                r"height:\s*44px[^}]*min-width:\s*44px",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"\.agent-send,\s*\n\.agent-stop\s*\{[^}]*"
                r"width:\s*38px[^}]*height:\s*38px[^}]*min-width:\s*38px",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"@media \(max-width:\s*680px\)[\s\S]*?"
                r"\.agent-send,\s*\.agent-stop\s*\{[^}]*"
                r"width:\s*44px[^}]*height:\s*44px",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"\.agent-streaming,\s*\n\.agent-cancelled\s*\{[^}]*"
                r"min-height:\s*116px",
                re.DOTALL,
            ),
        )
        self.assertIn(".agent-confirmation-executing", source)
        self.assertIn(
            ".agent-confirmation-executing-mark svg { animation: none !important; }",
            source,
        )
        self.assertIn("@media (max-width: 980px)", source)
        self.assertIn("@media (max-width: 680px)", source)
        self.assertIn("@media (prefers-reduced-motion: reduce)", source)
        self.assertRegex(
            source,
            re.compile(
                r"@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?"
                r"\.agent-stream-head \.lucide-loader-circle\s*\{[^}]*"
                r"animation:\s*none\s*!important",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"@media \(prefers-reduced-motion: reduce\)[\s\S]*?"
                r"\.agent-send, \.agent-stop\s*\{[^}]*"
                r"transition:\s*none\s*!important",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"@media \(prefers-reduced-motion: reduce\)[\s\S]*?"
                r"\.agent-stop:hover\s*\{[^}]*"
                r"transform:\s*none\s*!important",
                re.DOTALL,
            ),
        )
        self.assertIn("overflow-wrap: anywhere", source)
        self.assertIn(".agent-page button:focus-visible", source)
        self.assertRegex(
            source,
            re.compile(r"\.agent-session-list\s*\{[^}]*height:\s*230px", re.DOTALL),
        )
        self.assertRegex(
            source,
            re.compile(
                r"\.agent-session-item\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) 44px",
                re.DOTALL,
            ),
        )
        self.assertNotRegex(source, re.compile(r"(?m)^kbd\s*\{"))
        self.assertIn(".agent-overview-metrics", source)
        self.assertIn(".agent-overview-row", source)
        self.assertIn(".agent-next-actions", source)
        self.assertIn(".agent-next-action", source)
        self.assertRegex(
            source,
            re.compile(r"\.agent-next-action-button\s*\{[^}]*min-height:\s*44px", re.DOTALL),
        )
        self.assertRegex(
            source,
            re.compile(r"\.agent-overview-action\s*\{[^}]*min-height:\s*44px", re.DOTALL),
        )
        self.assertRegex(
            source,
            re.compile(r"@media \(max-width:\s*680px\)[\s\S]*\.agent-overview-row"),
        )
        self.assertIn(".agent-media-report", source)
        self.assertIn(".agent-search-group", source)
        self.assertIn(".agent-web-report", source)
        self.assertIn(".agent-web-result", source)
        self.assertIn(".agent-resource-action", source)
        self.assertRegex(
            source,
            re.compile(r"\.agent-resource-action\s*\{[^}]*min-height:\s*44px", re.DOTALL),
        )
        self.assertIn(".agent-episode-chip", source)
        self.assertRegex(
            source,
            re.compile(
                r"@media \(max-width:\s*680px\)[\s\S]*\.agent-episode-chip\s*\{[^}]*min-height:\s*44px",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"@media \(max-width:\s*680px\)[\s\S]*\.agent-movie-followup\s*\{[^}]*min-height:\s*44px",
                re.DOTALL,
            ),
        )
        self.assertIn(".agent-config-report", source)
        self.assertIn(".agent-config-matrix", source)
        self.assertIn(".agent-config-action", source)
        self.assertRegex(
            source,
            re.compile(r"\.agent-config-action\s*\{[^}]*min-height:\s*44px", re.DOTALL),
        )
        self.assertRegex(
            source,
            re.compile(
                r"@media \(max-width:\s*680px\)[\s\S]*\.agent-config-action\s*\{\s*width:\s*100%",
                re.DOTALL,
            ),
        )
        self.assertIn(".agent-season-resource-groups", source)
        self.assertIn(".agent-season-resource-group", source)
        self.assertIn(".agent-narrative", source)
        self.assertIn(".agent-rich-text", source)
        self.assertIn(".agent-answer-lead", source)
        self.assertIn(".agent-result-summary", source)
        self.assertIn(".agent-config-diagnosis", source)
        self.assertIn(".agent-message-assistant .agent-message-label", source)
        self.assertNotIn(".agent-evidence", source)
        self.assertIn(".agent-guidance-actions", source)
        self.assertIn(".agent-confirmation-facts", source)
        self.assertRegex(
            source,
            re.compile(
                r"\.agent-confirmation-fact\s*\{[^}]*"
                r"grid-template-columns:\s*104px minmax\(0, 1fr\)",
                re.DOTALL,
            ),
        )
        self.assertNotIn(".agent-confirmation-heading-mark", source)
        self.assertNotIn(".agent-confirmation-fact-mark", source)
        self.assertIn(".agent-confirmation-status", source)
        self.assertIn(".agent-confirmation-preflight", source)
        self.assertIn(".agent-confirmation-countdown", source)
        self.assertIn("justify-content: flex-end;", source)
        self.assertRegex(
            source,
            re.compile(
                r"\.agent-season-resource-groups\s*\{[^}]*display:\s*grid", re.DOTALL
            ),
        )

    def test_agent_navigation_is_a_primary_workspace_destination(self):
        html = BASE_TEMPLATE.read_text(encoding="utf-8")
        dashboard = html.index("url_for('pages.dashboard')")
        agent = html.index("url_for('pages.agent')")
        discovery = html.index("url_for('pages.discovery')")
        self.assertLess(dashboard, agent)
        self.assertLess(agent, discovery)


if __name__ == "__main__":
    import unittest

    unittest.main()
