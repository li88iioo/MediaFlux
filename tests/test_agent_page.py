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
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()

    @staticmethod
    def _csrf_token(response) -> str:
        match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
        if not match:
            match = re.search(r'name="csrf_token" (?:content|value)="([^"]+)"', response.text)
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
        with patch("app.routes.pages.is_agent_enabled", return_value=False), patch(
            "app.agent.feature_gate.is_agent_enabled", return_value=False
        ):
            response = self.client.get("/agent", follow_redirects=False)
            dashboard = self.client.get("/")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/settings?agent_disabled=1#agent")
        self.assertNotIn('<span>Agent</span>', dashboard.text)

    def test_authenticated_agent_renders_stable_semantic_shell(self):
        self._login()
        response = self.client.get("/agent")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count("<main"), 1)
        self.assertIn('class="agent-page"', response.text)
        self.assertIn('id="agentTranscript" role="log" aria-live="off"', response.text)
        self.assertIn('id="agentResponseStatus" role="status" aria-live="polite" aria-atomic="true"', response.text)
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
        self.assertIn('/settings#agent', response.text)
        self.assertIn('id="agentSessionList"', response.text)
        self.assertIn('id="agentSessionCount"', response.text)
        self.assertRegex(
            response.text,
            re.compile(r'<a href="/agent" class="nav-item active"[^>]*>'),
        )
        self.assertEqual(response.text.count("/static/css/agent.css"), 1)
        self.assertEqual(response.text.count("/static/js/agent.js"), 1)
        self.assertIn("/static/css/agent.css?v=20260829e", response.text)
        self.assertIn("/static/js/agent.js?v=20260829a", response.text)
        self.assertIn('id="agentResumeLatestSession"', response.text)
        self.assertIn("继续上次", response.text)
        self.assertNotRegex(response.text, re.compile(r"(?:API_KEY|PASSWORD|TOKEN)=", re.I))
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
        self.assertNotIn('data-agent-prompt=', response.text)
        self.assertNotIn('data-agent-draft=', response.text)

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

    def test_agent_frontend_renders_bounded_read_plan_without_raw_arguments(self):
        source = SCRIPT.read_text(encoding="utf-8")
        styles = STYLES.read_text(encoding="utf-8")
        self.assertIn("renderReadPlan", source)
        self.assertIn("agent.read_plan", source)
        self.assertIn("READ_PLAN_LABELS", source)
        self.assertNotIn("DIAGNOSTIC RUN", source)
        self.assertNotIn("PARTIAL CHECK", source)
        self.assertNotIn("VERIFIED SOURCES", source)
        self.assertNotIn("step.arguments", source)
        self.assertIn(".agent-read-plan-list", styles)
        self.assertIn(".agent-read-plan-step", styles)
        self.assertRegex(
            styles,
            re.compile(r"@media \(max-width:\s*680px\)[\s\S]*\.agent-read-plan-step"),
        )

    def test_agent_frontend_renders_notices_as_static_non_actionable_context(self):
        source = SCRIPT.read_text(encoding="utf-8")
        styles = STYLES.read_text(encoding="utf-8")

        self.assertIn("function responseNotices", source)
        self.assertIn("function renderNotices", source)
        self.assertIn("const PUBLIC_NARRATIVE_SOURCES = new Set(['llm', 'system', 'native'])", source)
        self.assertIn("PUBLIC_NARRATIVE_SOURCES.has(presentation?.source)", source)
        self.assertIn("section.setAttribute('aria-label', '数据说明')", source)
        self.assertIn("PUBLIC_NARRATIVE_SOURCES.has(data.presentation_source)", source)
        self.assertIn("notices: Array.isArray(data.notices) ? data.notices : []", source)
        self.assertIn(".agent-notices", styles)
        self.assertIn(".agent-notices-copy p", styles)
        render_notices = source.split("function renderNotices", 1)[1].split("function renderNarrative", 1)[0]
        self.assertNotIn("dataset.agentPrompt", render_notices)
        self.assertNotIn("dataset.agentDraft", render_notices)
        self.assertNotIn("button", render_notices)

    def test_agent_frontend_collapses_search_details_when_narrative_exists(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("const COLLAPSIBLE_NARRATIVE_DETAIL_TOOLS", source)
        self.assertIn("renderResultDisclosure(specializedData, '查看核对明细')", source)
        self.assertIn("narrative && COLLAPSIBLE_NARRATIVE_DETAIL_TOOLS.has(toolName)", source)

    def test_agent_frontend_renders_workspace_overview_and_draft_workflows(self):
        source = SCRIPT.read_text(encoding="utf-8")
        styles = STYLES.read_text(encoding="utf-8")
        self.assertIn("WORKSPACE_OVERVIEW_TOOLS", source)
        self.assertIn("renderWorkspaceOverview", source)
        self.assertIn("renderWorkspaceNextActions", source)
        self.assertIn("workspace.next_actions", source)
        self.assertIn("data-agent-workspace-action", source)
        self.assertIn("/api/agent/workspace-actions/invoke", source)
        self.assertIn("action_key: actionKey", source)
        self.assertNotIn("target_tool: action", source)
        self.assertIn("renderSpecializedData", source)
        self.assertIn("responseInspectionTrace", source)
        self.assertIn("renderInspectionTrace", source)
        self.assertIn("payload?.agent_trace", source)
        self.assertIn("本次核对", source)
        self.assertIn(".agent-inspection-trace", styles)
        self.assertIn(".agent-inspection-item", styles)
        self.assertRegex(
            styles,
            re.compile(
                r"@media \(max-width:\s*680px\)[\s\S]*"
                r"\.agent-guidance-action, \.agent-result-disclosure > summary \{ min-height: 44px; \}"
            ),
        )
        self.assertIn("WORKFLOW_ACTIONS", source)
        self.assertIn("[data-agent-prompt], [data-agent-draft]", source)
        self.assertIn("promptInput.setSelectionRange", source)
        self.assertIn("['unavailable', '暂不可用']", source)
        self.assertIn("['not_probed', '本次未探测']", source)
        self.assertIn("'strm.triage_failures'", source)
        self.assertIn("disabled: '已停用'", source)
        self.assertIn("indexers: '资源站'", source)
        self.assertIn("agent-overview-action", source)
        self.assertIn("EPISODE_AUDIT_TOOLS", source)
        self.assertIn("renderEpisodeAudit", source)
        self.assertIn("library.count_series_episodes", source)
        self.assertIn("renderSeriesEpisodeCount", source)
        self.assertIn(".agent-series-count", styles)
        self.assertIn("LIBRARY_AUDIT_TOOLS", source)
        self.assertIn("renderLibraryAudit", source)
        self.assertIn("library.audit_library_episodes", source)
        self.assertIn("library.start_episode_audit", source)
        self.assertIn("agent.job_status", source)
        self.assertIn("library.patrol_status", source)
        self.assertIn("完成前不会把零值解释为没有缺集", source)
        self.assertIn("renderMissingEpisodeResources", source)
        self.assertIn("renderUnifiedSearch", source)
        self.assertIn("renderIndexerSearch", source)
        self.assertIn("renderWebSearch", source)
        self.assertIn("name === 'web.search'", source)
        self.assertIn("safeExternalUrl", source)
        self.assertIn("title.target = '_blank'", source)
        self.assertIn("title.rel = 'noopener noreferrer'", source)
        self.assertIn("prepareResourceSubmission", source)
        self.assertIn("runDirectTool", source)
        self.assertIn("directToolActions", source)
        self.assertIn("/api/agent/tools/${action.tool}", source)
        self.assertIn("library.search_missing_episode_resources", source)
        self.assertIn("resource_followups_truncated", source)
        self.assertIn("当前提供前 ${followups.size} 集快捷搜索", source)
        self.assertIn("/api/agent/actions/indexer.submit_resource/prepare", source)
        self.assertIn("dataset.agentResourceId", source)
        self.assertIn("updates_available: '发现缺集'", source)
        self.assertIn("not_supported: '暂不支持'", source)
        self.assertIn("movie_library_presence_with_resource_followup", source)
        self.assertIn("本次不自动判断“电影版本已更新”", source)
        self.assertIn("另 ${hiddenMatches} 条未展开", source)
        self.assertIn("data.media_type === 'movie' ? ''", source)
        self.assertIn("搜索资源站候选", source)
        self.assertIn("comparison_unavailable: '需人工核对'", source)
        self.assertIn("review_required: '待核对'", source)
        self.assertIn("data-agent-direct-tool", source)
        self.assertIn(".agent-episode-chip, [data-agent-direct-tool]", source)
        self.assertIn("renderConfigExplanation", source)
        self.assertIn("config.explain_component", source)
        self.assertIn("agent-config-report", source)
        self.assertIn("required_field_labels", source)
        self.assertIn("missing_field_labels", source)
        self.assertIn("blocked_capabilities", source)
        self.assertIn("managed_by_environment", source)
        self.assertIn("config.set_feature_state", source)
        self.assertIn("renderMissingSeasonResources", source)
        self.assertIn("library.search_missing_season_resources", source)
        self.assertIn("agent-season-resource-report", source)
        self.assertIn("agent-season-resource-groups", source)
        self.assertIn("episodes.slice(0, 3)", source)
        self.assertIn("当前季度无需补集", source)
        self.assertIn("未执行批量资源站搜索", source)

    def test_agent_frontend_uses_safe_interruptible_confirmation_flow(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("new AbortController()", source)
        self.assertIn("conversationGeneration", source)
        self.assertIn("/api/agent/query", source)
        self.assertIn("/api/agent/query/cancel", source)
        self.assertIn("request_id: operation.requestId", source)
        self.assertIn("function stopActiveQuery", source)
        self.assertIn("event.type === 'cancelled'", source)
        self.assertIn("AGENT_PHASE_LABELS", source)
        self.assertIn("/api/agent/capabilities", source)
        self.assertIn("/api/agent/actions/confirm", source)
        self.assertIn("confirmation_id: confirmationId", source)
        self.assertIn("confirmationInFlight", source)
        self.assertIn("setConfirmationBusy(true)", source)
        self.assertIn("syncSessionLifecycleControls", source)
        self.assertIn("服务端会先完成本次写入", source)
        self.assertIn("新会话、切换和删除将在结束后恢复", source)
        self.assertIn("if (confirmationInFlight || sessionResetInFlight) return", source)
        self.assertIn("crypto?.randomUUID", source)
        self.assertIn("session_id: sessionId", source)
        self.assertIn("/api/agent/session/reset", source)
        self.assertIn("/api/agent/sessions", source)
        self.assertIn("data-agent-session-delete", source)
        self.assertIn("method: 'DELETE'", source)
        self.assertIn("Promise.allSettled([loadCapabilities(), loadSessions()])", source)
        self.assertIn("latestSessionId", source)
        self.assertIn("resumeLatestSessionButton", source)
        self.assertIn("openSession(latestSessionId)", source)
        self.assertNotIn("loadSessions({restoreLatest: true})", source)
        self.assertNotIn("allowInitialHistoryRestore", source)
        self.assertNotIn("restoreGeneration === conversationGeneration", source)
        self.assertIn("function createRetryDraftButton(draft)", source)
        self.assertIn("function createRetryActions(draft)", source)
        self.assertIn("agent-retry-immediate", source)
        self.assertIn("立即重试", source)
        self.assertIn("编辑指令", source)
        self.assertIn("agent-confirmation-reprepare", source)
        self.assertIn("重新预检", source)
        self.assertIn("agent-retry-draft", source)
        self.assertIn("appendRequestError(error, pending, normalized)", source)
        self.assertIn("markStreamInterrupted(error.streamView, error.message, normalized)", source)
        self.assertIn("sessionResetInFlight", source)
        self.assertIn("transcript.setAttribute('aria-live', 'off')", source)
        self.assertNotIn("transcript.setAttribute('aria-live', 'polite')", source)
        self.assertIn("announceResponseStatus(responseAnnouncement(payload))", source)
        self.assertIn("button.classList.contains('is-armed')", source)
        self.assertIn("startConfirmationCountdown", source)
        self.assertIn("error.payload = payload", source)
        self.assertIn("error?.payload?.result && error?.payload?.tool_call", source)
        self.assertIn("renderConsumedConfirmation(card, error.payload)", source)
        self.assertIn("function responseGuidance(payload)", source)
        self.assertIn("function renderNarrative(presentation)", source)
        self.assertIn("presentation: narrative ? {", source)
        self.assertIn("narrative,", source)
        self.assertIn("function renderNarrativeState(display, result)", source)
        self.assertIn("function renderResultDisclosure(content", source)
        self.assertIn("const display = payload?.display", source)
        self.assertIn("renderData(display.details)", source)
        self.assertNotIn("renderData(result.data)", source)
        self.assertIn("card.append(renderResultDisclosure(genericData))", source)
        self.assertNotIn("card.append(narrative ? renderResultDisclosure(genericData) : genericData)", source)

    def test_agent_history_mobile_and_accessibility_contracts(self):
        source = SCRIPT.read_text(encoding="utf-8")
        styles = STYLES.read_text(encoding="utf-8")
        template = TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("closeHistoryRail({returnFocus: false})", source)
        self.assertIn("promptInput.focus({preventScroll: true})", source)
        self.assertIn("const switchBlocked = requestInFlight || confirmationInFlight || sessionResetInFlight", source)
        self.assertIn("const newSessionBlocked = confirmationInFlight || sessionResetInFlight", source)
        self.assertIn("停止当前任务并开始新会话", source)
        self.assertIn("任务执行中，完成后可新建、切换或删除会话", source)
        self.assertIn("syncSessionLifecycleControls();", source)
        self.assertIn("const viewport = window.visualViewport", source)
        self.assertIn("--agent-viewport-height", source)
        self.assertIn("window.visualViewport.addEventListener('resize'", source)
        self.assertNotIn('id="closeAgentShortcuts"', template)
        self.assertNotIn("shortcutsClose", source)
        self.assertNotIn("toggleShortcutsTimeline", source)
        self.assertNotIn("agentShortcutsTimeline", source)
        self.assertRegex(
            styles,
            re.compile(
                r"@media \(max-width: 680px\)[\s\S]*?"
                r"\.agent-action-btn \{[^}]*width: 44px;[^}]*height: 44px;",
                re.S,
            ),
        )
        self.assertRegex(
            styles,
            re.compile(
                r"@media \(max-width: 680px\)[\s\S]*?"
                r"\.agent-send, \.agent-stop \{[^}]*width: 44px;[^}]*height: 44px;",
                re.S,
            ),
        )
        self.assertNotIn("!narrative ? renderData(result.data) : null", source)
        self.assertIn("payload?.display?.guidance", source)
        self.assertIn("const MAX_GENERIC_SECTIONS = 4", source)
        self.assertIn("function normalizeDisplaySource(value)", source)
        self.assertIn("function readableParagraphs(value)", source)
        self.assertIn("function renderTextBlocks(value", source)
        self.assertIn("function replaceTextBlocks(target", source)
        self.assertIn("{promoteFirst: true}", source)
        self.assertNotIn("streamView.content.textContent = answer", source)
        self.assertIn("streamView.textNode.appendData(delta)", source)
        self.assertIn("function transcriptIsNearBottom()", source)
        self.assertNotIn("replaceTextBlocks(streamView.content", source)
        self.assertNotIn("function renderEvidence", source)
        self.assertIn("presentation?.kind !== 'narrative'", source)
        self.assertIn("function renderGuidance(payload)", source)
        self.assertIn("button.dataset.agentPrompt = item.prompt", source)
        self.assertIn("button.dataset.agentDraft = item.prompt", source)
        self.assertIn("confirmation?.contract", source)
        self.assertIn("const confirmationRequired", source)
        self.assertIn("function confirmationPayloadFromActionPlan(payload)", source)
        self.assertIn("payload?.action_plan?.plan_id", source)
        self.assertIn("renderConfirmation(confirmationPayload, payload.tool_call, payload)", source)
        self.assertIn("['操作对象', contract.object]", source)
        self.assertIn("['将会发生', contract.impact]", source)
        self.assertIn("['如何撤销', contract.reversibility]", source)
        self.assertIn("contract.preflight_summary", source)
        self.assertIn("contract.preflight_at", source)
        self.assertIn("'执行'", source)
        self.assertIn("'取消'", source)
        self.assertNotIn("meta.push(payload.tool_call.name)", source)
        self.assertNotIn("meta.push(`${payload.tool_call.elapsed_ms} ms`)", source)
        self.assertIn("confirmButton.disabled || confirmationInFlight", source)
        self.assertIn("error?.status === 409", source)
        self.assertIn("行动计划已失效，请重新提交原任务生成新的预检", source)
        self.assertIn("const MIN_PENDING_MS = 320", source)
        self.assertIn("textContent", source)
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("insertAdjacentHTML", source)
        self.assertNotIn("localStorage", source)
        self.assertNotIn("sessionStorage", source)
        self.assertRegex(source, re.compile(r"event\.key === ['\"]Enter['\"]"))
        self.assertIn("event.shiftKey", source)

        app_source = APP_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("meta[name=\"csrf-token\"]", app_source)
        self.assertIn("X-CSRF-Token", app_source)

    def test_agent_styles_reserve_layout_and_support_mobile_and_reduced_motion(self):
        source = STYLES.read_text(encoding="utf-8")
        self.assertRegex(
            source,
            re.compile(
                r"\.agent-transcript\s*\{[^}]*flex:\s*1[^}]*"
                r"min-height:\s*0[^}]*overflow-y:\s*auto",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"\.agent-submit-slot\s*\{[^}]*width:\s*44px[^}]*"
                r"height:\s*44px[^}]*min-width:\s*44px",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"\.agent-send,\s*\n\.agent-stop\s*\{[^}]*"
                r"width:\s*38px[^}]*height:\s*38px[^}]*min-width:\s*38px",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"@media \(max-width:\s*680px\)[\s\S]*?"
                r"\.agent-send,\s*\.agent-stop\s*\{[^}]*"
                r"width:\s*44px[^}]*height:\s*44px",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"\.agent-streaming,\s*\n\.agent-cancelled\s*\{[^}]*"
                r"min-height:\s*116px",
                re.S,
            ),
        )
        self.assertIn(".agent-confirmation-executing", source)
        self.assertIn(".agent-confirmation-executing-mark svg { animation: none !important; }", source)
        self.assertIn("@media (max-width: 980px)", source)
        self.assertIn("@media (max-width: 680px)", source)
        self.assertIn("@media (prefers-reduced-motion: reduce)", source)
        self.assertRegex(
            source,
            re.compile(
                r"@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?"
                r"\.agent-stream-head \.lucide-loader-circle\s*\{[^}]*"
                r"animation:\s*none\s*!important",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"@media \(prefers-reduced-motion: reduce\)[\s\S]*?"
                r"\.agent-send, \.agent-stop\s*\{[^}]*"
                r"transition:\s*none\s*!important",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"@media \(prefers-reduced-motion: reduce\)[\s\S]*?"
                r"\.agent-stop:hover\s*\{[^}]*"
                r"transform:\s*none\s*!important",
                re.S,
            ),
        )
        self.assertIn("overflow-wrap: anywhere", source)
        self.assertIn(".agent-page button:focus-visible", source)
        self.assertRegex(
            source,
            re.compile(r"\.agent-session-list\s*\{[^}]*height:\s*230px", re.S),
        )
        self.assertRegex(
            source,
            re.compile(r"\.agent-session-item\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) 44px", re.S),
        )
        self.assertNotRegex(source, re.compile(r"(?m)^kbd\s*\{"))
        self.assertIn(".agent-overview-metrics", source)
        self.assertIn(".agent-overview-row", source)
        self.assertIn(".agent-next-actions", source)
        self.assertIn(".agent-next-action", source)
        self.assertRegex(source, re.compile(r"\.agent-next-action-button\s*\{[^}]*min-height:\s*44px", re.S))
        self.assertRegex(source, re.compile(r"\.agent-overview-action\s*\{[^}]*min-height:\s*44px", re.S))
        self.assertRegex(source, re.compile(r"@media \(max-width:\s*680px\)[\s\S]*\.agent-overview-row"))
        self.assertIn(".agent-media-report", source)
        self.assertIn(".agent-search-group", source)
        self.assertIn(".agent-web-report", source)
        self.assertIn(".agent-web-result", source)
        self.assertIn(".agent-resource-action", source)
        self.assertRegex(source, re.compile(r"\.agent-resource-action\s*\{[^}]*min-height:\s*44px", re.S))
        self.assertIn(".agent-episode-chip", source)
        self.assertRegex(
            source,
            re.compile(r"@media \(max-width:\s*680px\)[\s\S]*\.agent-episode-chip\s*\{[^}]*min-height:\s*44px", re.S),
        )
        self.assertRegex(
            source,
            re.compile(r"@media \(max-width:\s*680px\)[\s\S]*\.agent-movie-followup\s*\{[^}]*min-height:\s*44px", re.S),
        )
        self.assertIn(".agent-config-report", source)
        self.assertIn(".agent-config-matrix", source)
        self.assertIn(".agent-config-action", source)
        self.assertRegex(source, re.compile(r"\.agent-config-action\s*\{[^}]*min-height:\s*44px", re.S))
        self.assertRegex(
            source,
            re.compile(r"@media \(max-width:\s*680px\)[\s\S]*\.agent-config-action\s*\{\s*width:\s*100%", re.S),
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
        self.assertIn(".agent-confirmation-preflight", source)
        self.assertIn(".agent-confirmation-countdown", source)
        self.assertRegex(
            source,
            re.compile(r"\.agent-season-resource-groups\s*\{[^}]*display:\s*grid", re.S),
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
