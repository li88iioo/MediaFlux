"""媒体探索页面的路由与静态安全契约测试。"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.support import InitializedWebTestCase

from app.config import web_credentials
from app.main import create_app


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app" / "templates" / "discovery.html"
SCRIPT = ROOT / "app" / "static" / "js" / "discovery.js"
APP_SCRIPT = ROOT / "app" / "static" / "js" / "app.js"
SETTINGS_TEMPLATE = ROOT / "app" / "templates" / "settings.html"
BASE_TEMPLATE = ROOT / "app" / "templates" / "base.html"
STYLES = ROOT / "app" / "static" / "css" / "main.css"


class DiscoveryPageTests(InitializedWebTestCase):
    """只验证页面外壳，不要求探索 API 或 Provider 已注册。"""

    def setUp(self):
        self.env_patch = patch.dict(os.environ, {"DISCOVERY_ENABLED": "1"})
        self.env_patch.start()
        self.client = TestClient(create_app(), raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.env_patch.stop()

    @staticmethod
    def _csrf_token(response) -> str:
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

    def test_anonymous_discovery_redirects_to_login(self):
        response = self.client.get("/discovery", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("location"), "http://testserver/login")

    def test_authenticated_discovery_renders_semantic_stable_shell(self):
        self._login()

        response = self.client.get("/discovery")

        self.assertEqual(response.status_code, 200)
        self.assertRegex(
            response.text,
            re.compile(r'<div\s+class="discovery-page"', re.S),
        )
        self.assertEqual(response.text.count('<main'), 1)
        self.assertIn('<h1 class="sr-only" id="discovery-heading"', response.text)
        self.assertNotIn('class="workspace-bar"', response.text)
        self.assertNotIn('class="discovery-masthead"', response.text)
        self.assertIn('class="discovery-mobile-bar"', response.text)
        self.assertEqual(response.text.count('id="toggleSidebar"'), 1)
        self.assertNotIn('data-theme-location="workspace"', response.text)
        self.assertRegex(
            response.text,
            re.compile(r'discovery-toolbar-meta[\s\S]*id="discovery-refresh"'),
        )
        self.assertIn('aria-labelledby="discovery-heading"', response.text)
        self.assertIn('id="discovery-source-tabs"', response.text)
        self.assertIn('aria-controls="discovery-stage"', response.text)
        self.assertIn('tabindex="-1"', response.text)
        self.assertIn('id="discovery-stage" role="tabpanel"', response.text)
        self.assertIn('id="discovery-filter-region"', response.text)
        self.assertIn('id="discovery-provider-status"', response.text)
        self.assertIn('id="discovery-sections"', response.text)
        self.assertIn('id="discovery-grid"', response.text)
        self.assertIn('id="discovery-live"', response.text)
        self.assertIn('id="discovery-refresh"', response.text)
        self.assertIn(
            'data-discovery-dialog-close aria-label="关闭" title="关闭"',
            response.text,
        )
        self.assertRegex(
            response.text,
            re.compile(r'<a href="/discovery" class="nav-item active"[^>]*>'),
        )
        self.assertEqual(response.text.count("/static/js/discovery.js"), 1)
        self.assertRegex(
            response.text,
            re.compile(
                r'<script[^>]+src="/static/js/discovery\.js(?:\?[^"]*)?"[^>]*\bdefer\b'
            ),
        )
        self.assertNotRegex(response.text, re.compile(r"DOUBAN_FRODO_API_(?:KEY|SECRET)", re.I))
        self.assertNotRegex(
            response.text,
            re.compile(r"(?:image\.tmdb\.org|doubanio\.com|bgm\.tv|bangumi\.tv)", re.I),
        )

    def test_discovery_renders_resource_results_flag_from_config(self):
        self._login()

        with patch("app.routes.pages.config.get_bool") as get_bool:
            get_bool.side_effect = lambda key, default=False: {
                "DISCOVERY_ENABLED": True,
                "DISCOVERY_RESOURCE_RESULTS_ENABLED": False,
            }.get(key, default)
            response = self.client.get("/discovery")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-resource-results-enabled="false"', response.text)

    def test_settings_renders_server_managed_douban_without_orphaned_dbcl2_card(self):
        self._login()

        response = self.client.get("/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn("豆瓣公共探索", response.text)
        self.assertIn("服务端兼容回退", response.text)
        self.assertIn('data-key="DISCOVERY_RESOURCE_RESULTS_ENABLED"', response.text)
        self.assertNotIn("可选 dbcl2 回退", response.text)
        self.assertRegex(
            response.text,
            re.compile(
                r'豆瓣公共探索[\s\S]+data-key="DOUBAN_DBCL2"'
            ),
        )
        self.assertNotIn("DOUBAN_FRODO_API_KEY", response.text)
        self.assertNotIn("DOUBAN_FRODO_API_SECRET", response.text)
        self.assertNotIn("data-disclosure-toggle", response.text)
        self.assertNotIn("配置可选回退", response.text)
        self.assertNotIn('data-key="BANGUMI_USER_AGENT"', response.text)

    def test_settings_never_renders_stored_frodo_or_dbcl2_values(self):
        self._login()

        with patch(
            "app.routes.api.config.all_items",
            return_value={
                "DOUBAN_FRODO_API_KEY": "server-key-must-not-render",
                "DOUBAN_FRODO_API_SECRET": "server-secret-must-not-render",
                "DOUBAN_DBCL2": "123456789:test-dbcl2-value",
            },
        ):
            response = self.client.get("/settings")

        for forbidden in (
            "server-key-must-not-render",
            "server-secret-must-not-render",
            "123456789:test-dbcl2-value",
        ):
            self.assertNotIn(forbidden, response.text)

    def test_settings_template_has_no_frodo_disclosure_runtime(self):
        source = APP_SCRIPT.read_text(encoding="utf-8")
        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")

        self.assertNotIn("[data-disclosure-toggle]", source)
        self.assertNotIn("setupDiscoveryDisclosure", template)
        self.assertNotIn("data-stable-disclosure", template)
        self.assertNotIn("data-disclosure-panel", template)
        self.assertNotIn("disclosureReady", template)

    def test_discovery_script_has_stable_safe_request_lifecycle(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("AbortController", source)
        self.assertRegex(source, re.compile(r"SKELETON_DELAY_MS\s*=\s*300"))
        self.assertIn("textContent", source)
        self.assertNotIn(".innerHTML", source)
        self.assertIn("replaceChildren", source)
        self.assertIn("window.location.origin", source)
        self.assertIn("preserveContent", source)
        self.assertIn("is-refreshing", source)
        self.assertIn("/api/discovery/sections", source)
        self.assertIn("/api/discovery/items", source)
        self.assertIn("/api/discovery/watchlist", source)
        self.assertIn("/api/discovery/map", source)
        self.assertIn("item.state === 'watchlisted'", source)
        self.assertIn("poster_token: posterTokenFromItem(item)", source)
        self.assertIn("tmdb_id: candidate.tmdb_id", source)
        self.assertIn("tmdb_title: candidate.title", source)
        self.assertIn("tmdb_year: candidate.year", source)
        self.assertNotIn("confirmed_tmdb_id", source)
        self.assertIn("loadMappingCandidates", source)
        self.assertIn("section.enabled === false", source)
        self.assertIn("tmdb: 'discover'", source)
        self.assertIn("SKELETON_MIN_VISIBLE_MS = 300", source)
        self.assertIn("keepSkeletonVisible", source)
        self.assertIn("posterTokenFromItem", source)
        self.assertIn("detailController", source)
        self.assertIn("const previousPage", source)
        self.assertIn("state.requestId === loadRequestId", source)
        self.assertIn("mappingRequestId !== state.detailRequestId", source)
        self.assertIn("elements.sections.hidden = state.mode !== 'sections'", source)
        self.assertIn("payload.cached", source)
        self.assertIn("async function refreshActive()", source)
        self.assertIn("if (state.loading || state.loadingMore) return false", source)
        self.assertIn("state.page = 1", source)
        self.assertIn("elements.refresh.addEventListener('click', refreshActive)", source)
        self.assertIn("const refreshRequestId = state.requestId", source)
        self.assertIn("if (!loaded && state.requestId === refreshRequestId)", source)
        self.assertIn("elements.refresh.disabled = isLoading || state.loading", source)
        self.assertIn("reusableCard(item, index, 'h4', scope)", source)
        self.assertIn("filter(([key]) => key !== 'defaults')", source)
        self.assertIn("payload.defaults", source)
        self.assertNotIn("poster_key:", source)
        self.assertIn("!(key in filterState)", source)
        self.assertIn("state.filters[definition.key] = select.value", source)
        self.assertIn("const previousFilters = {...state.filters}", source)
        self.assertIn("if (!loaded && state.requestId === loadRequestId)", source)
        self.assertIn("state.filters = previousFilters", source)
        self.assertIn("mergeFailedSections", source)
        self.assertIn("const filterDefinitionsCache = new Map()", source)
        self.assertIn("const viewSnapshots = new Map()", source)
        self.assertIn("saveCurrentViewSnapshot", source)
        self.assertIn("restoreCachedView", source)
        self.assertIn("snapshotIsFresh", source)
        self.assertIn("section?.loading", source)
        self.assertIn("Promise.all([definitionsPromise, itemsPromise])", source)
        self.assertIn("hydrateSectionQueue", source)
        self.assertIn("SECTION_BACKGROUND_CONCURRENCY", source)
        self.assertIn("section.loading", source)
        self.assertIn("target.dataset.globalSkeleton === 'true'", source)
        self.assertIn("resourceSubmitState: new Map()", source)
        self.assertIn("resourceResults: new Map()", source)
        self.assertIn("INDEXER_DOWNLOAD_BATCH_PATH", source)
        self.assertIn("summary.partial > 0 || summary.review_required > 0 ? 'warning'", source)
        self.assertIn("summary.failed > 0 ? (summary.succeeded ? 'warning' : 'error')", source)
        self.assertIn("summary.duplicate > 0 ? 'warning' : 'success'", source)
        self.assertIn("item.status === 'partial'", source)
        self.assertIn("'部分成功：'", source)
        self.assertIn("'；失败：'", source)
        self.assertIn("role', notification.type === 'error' ? 'alert' : 'status'", source)
        self.assertIn("pendingResourceNotification", source)
        self.assertIn("resourceSubmissionRequests", source)
        self.assertIn("pendingResourceNotifications: []", source)
        self.assertIn("isTerminalResourceSelection", source)
        self.assertIn("resourceResultSubmitting", source)
        self.assertIn(
            "`成功 ${summary.succeeded}，部分 ${summary.partial}，待核对 ${summary.review_required}，失败 ${summary.failed}，重复 ${summary.duplicate}`",
            source,
        )
        self.assertIn("RESOURCE_MANUAL_REVIEW_MESSAGE", source)
        self.assertNotIn(".innerHTML", source)

    def test_settings_server_renders_resource_site_visibility_without_flash(self):
        self._login()

        with patch("app.routes.pages.config.get_bool") as get_bool:
            get_bool.side_effect = lambda key, default=False: {
                "DISCOVERY_ENABLED": True,
                "DISCOVERY_RESOURCE_RESULTS_ENABLED": False,
            }.get(key, default)
            disabled = self.client.get("/settings")

        self.assertEqual(disabled.status_code, 200)
        self.assertRegex(
            disabled.text,
            re.compile(
                r'<input[^>]+data-key="DISCOVERY_RESOURCE_RESULTS_ENABLED"(?![^>]*checked)[^>]*>',
                re.S,
            ),
        )
        self.assertRegex(
            disabled.text,
            re.compile(
                r'<div class="indexer-site-box"[^>]+data-indexer-site-box[^>]+hidden[^>]+aria-hidden="true"',
                re.S,
            ),
        )

        with patch("app.routes.pages.config.get_bool") as get_bool:
            get_bool.side_effect = lambda key, default=False: {
                "DISCOVERY_ENABLED": True,
                "DISCOVERY_RESOURCE_RESULTS_ENABLED": True,
            }.get(key, default)
            enabled = self.client.get("/settings")

        self.assertRegex(
            enabled.text,
            re.compile(
                r'<input[^>]+data-key="DISCOVERY_RESOURCE_RESULTS_ENABLED"[^>]+checked[^>]*>',
                re.S,
            ),
        )
        indexer_box = re.search(
            r'<div class="indexer-site-box"[^>]+data-indexer-site-box[^>]*>',
            enabled.text,
            re.S,
        )
        self.assertIsNotNone(indexer_box)
        self.assertNotIn(" hidden", indexer_box.group(0))
        self.assertIn('aria-hidden="false"', indexer_box.group(0))

    def test_settings_exposes_persistent_indexer_site_selector(self):
        html = SETTINGS_TEMPLATE.read_text(encoding="utf-8") + (ROOT / "app/static/js/settings.js").read_text(encoding="utf-8")
        for contract in (
            'data-key="INDEXER_ENABLED_SITES"',
            'data-indexer-site="nyaa"',
            'data-indexer-site="mikan"',
            'data-indexer-site="btbtla"',
            'data-indexer-site="1lou"',
            'data-indexer-site="tpb"',
            'data-indexer-site="sukebei"',
            '成人内容，默认关闭',
            'const INDEXER_SITE_ORDER=',
            'const DEFAULT_INDEXER_SITES=',
            'function syncIndexerSiteSelection',
            'function loadIndexerSiteSelection',
            'indexerSiteBox.hidden=!enabled',
            "indexerSiteBox.setAttribute('aria-hidden',enabled?'false':'true')",
        ):
            self.assertIn(contract, html)
        self.assertRegex(
            html,
            re.compile(
                r'<input[^>]+type="hidden"[^>]+data-key="INDEXER_ENABLED_SITES"',
                re.S,
            ),
        )
        for contract in (
            'data-indexer-site-chip',
            '<strong>Sukebei</strong><small>成人</small>',
        ):
            self.assertIn(contract, html)
        self.assertIn("const DEFAULT_INDEXER_SITES=['nyaa','mikan','btbtla','1lou','tpb'];", html)
        self.assertNotIn("AnimeTosho", html)
        self.assertNotIn('data-indexer-site="animetosho"', html)
        styles = STYLES.read_text(encoding="utf-8")
        self.assertRegex(
            styles,
            re.compile(
                r'\.indexer-site-selector\s*\{[^}]*display:\s*flex[^}]*flex-wrap:\s*wrap',
                re.S,
            ),
        )
        self.assertRegex(
            styles,
            re.compile(
                r'\.indexer-site-option\s+input\s*\{[^}]*position:\s*absolute[^}]*opacity:\s*0',
                re.S,
            ),
        )
        self.assertIn('.indexer-site-option input:checked + [data-indexer-site-chip]', styles)
        self.assertIn('.indexer-site-option input:focus-visible + [data-indexer-site-chip]', styles)
        self.assertIn('.indexer-site-box[hidden]', styles)
        self.assertNotIn("classList.toggle('is-disabled'", html)
        selector_styles = styles[styles.index('/* Discovery indexer site selector */'):]
        self.assertNotIn(
            'grid-template-columns: repeat(2,minmax(0,1fr))',
            selector_styles,
        )

    def test_discovery_error_panel_routes_tmdb_unconfigured_to_settings_metadata(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for contract in (
            "function isTmdbConfigError",
            "TMDB_API_KEY",
            "SOURCE INTERRUPTED",
            "资料源暂不可用",
            "isTmdbConfigError(error)",
            "button.href = '/settings#metadata'",
            "node('span', '', '去配置')",
            "node('span', '', '重新连接')",
        ):
            self.assertIn(contract, source)
        self.assertRegex(
            source,
            re.compile(
                r"if\s*\(isTmdbConfigError\(error\)\)\s*\{[\s\S]*?"
                r"node\('a',\s*'jump-btn discovery-retry'\)[\s\S]*?"
                r"button\.href\s*=\s*'/settings#metadata'[\s\S]*?"
                r"node\('span',\s*'',\s*'去配置'\)",
                re.S,
            ),
        )

    def test_discovery_navigation_sits_between_dashboard_and_guangya(self):
        html = BASE_TEMPLATE.read_text(encoding="utf-8")
        dashboard = html.index("url_for('pages.dashboard')")
        discovery = html.index("url_for('pages.discovery')")
        guangya = html.index('data-nav-cluster="guangya"')

        self.assertLess(dashboard, discovery)
        self.assertLess(discovery, guangya)

    def test_discovery_styles_fix_poster_geometry_and_responsive_controls(self):
        source = STYLES.read_text(encoding="utf-8")

        self.assertIn(".discovery-page {", source)
        self.assertRegex(
            source,
            re.compile(r"\.discovery-page\s*\{[^}]*min-width:\s*0[^}]*width:\s*100%", re.S),
        )
        self.assertRegex(
            source,
            re.compile(
                r"\.discovery-control-deck\s*\{[^}]*min-width:\s*0[^}]*"
                r"overflow:\s*hidden[^}]*border-radius:\s*14px",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(r"\.discovery-source-tabs\s*\{[^}]*width:\s*100%[^}]*max-width:\s*100%", re.S),
        )
        self.assertRegex(
            source,
            re.compile(r"\.discovery-poster\s*\{[^}]*aspect-ratio:\s*2\s*/\s*3", re.S),
        )
        self.assertRegex(
            source,
            re.compile(r"\.discovery-toolbar\s*\{[^}]*min-height:", re.S),
        )
        self.assertRegex(
            source,
            re.compile(
                r"\.discovery-grid\s*>\s*\.discovery-state\s*\{[^}]*"
                r"grid-column:\s*1\s*/\s*-1",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"\.discovery-dialog\s*\{[^}]*position:\s*fixed[^}]*"
                r"inset:\s*0[^}]*margin:\s*auto",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(r"\.discovery-mobile-bar\s*\{[^}]*display:\s*none", re.S),
        )
        self.assertIn(".discovery-skeleton", source)
        self.assertIn(".discovery-card-title", source)
        self.assertRegex(
            source,
            re.compile(r"\.discovery-card\s*\{[^}]*border-radius:\s*13px", re.S),
        )
        self.assertRegex(
            source,
            re.compile(r"\.discovery-search-input\s*\{[^}]*border-radius:\s*10px", re.S),
        )
        shelf_rule = re.search(r"\.discovery-shelf\s*\{(?P<body>[^}]*)\}", source, re.S)
        self.assertIsNotNone(shelf_rule)
        shelf_styles = shelf_rule.group("body")
        self.assertNotIn("padding:", shelf_styles)
        self.assertNotIn("border:", shelf_styles)
        self.assertNotIn("background:", shelf_styles)
        self.assertNotIn(".discovery-shelf { padding:", source)
        self.assertRegex(
            source,
            re.compile(r"\.discovery-card-action\s*\{[^}]*width:\s*36px;[^}]*height:\s*36px", re.S),
        )
        self.assertRegex(source, re.compile(r"\.discovery-provider-status\s*\{[^}]*width:\s*40px[^}]*height:\s*40px", re.S))
        self.assertIn("@media (max-width: 900px)", source)
        self.assertRegex(
            source,
            re.compile(
                r"@media \(max-width:\s*900px\).*?"
                r"\.discovery-dialog\s*\{[^}]*width:\s*calc\(100vw - 28px\)",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"@media \(max-width:\s*560px\).*?"
                r"\.discovery-resource-sites\s*\{[^}]*overflow-x:\s*auto",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"@media \(max-width:\s*560px\).*?"
                r"\.discovery-resource-batch-actions\s*\{[^}]*"
                r"grid-template-columns:\s*1fr\s+1fr\s+1\.25fr",
                re.S,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"@media \(max-width:\s*560px\).*?"
                r"\.discovery-resource-bulk\s*\{[^}]*position:\s*sticky",
                re.S,
            ),
        )
        self.assertIn("@media (prefers-reduced-motion: reduce)", source)
        self.assertRegex(
            source,
            re.compile(r':root\[data-theme="light"\] \.discovery-page\s*\{[^}]*--discovery-amber:\s*#6f4a00', re.S),
        )
        self.assertIn("--discovery-muted-readable:", source)
        self.assertIn(".discovery-resource-item-status.is-partial", source)
        self.assertRegex(
            source,
            re.compile(r"\.discovery-card-source\s*\{[^}]*color:\s*var\(--discovery-muted-readable\)", re.S),
        )
        self.assertIn("--discovery-success-readable:", source)
        self.assertIn("--discovery-warning-readable:", source)
        self.assertRegex(
            source,
            re.compile(r"\.discovery-provider-status\s*\{[^}]*color:\s*var\(--discovery-success-readable\)", re.S),
        )
        self.assertNotIn(".discovery-sequence", source)
        for selector in (
            ".discovery-dialog-loading",
            ".discovery-detail-original",
            ".discovery-detail-field dt",
            ".discovery-mapping-panel > p",
            ".discovery-map-candidate span",
        ):
            self.assertRegex(
                source,
                re.compile(re.escape(selector) + r"\s*\{[^}]*color:\s*var\(--discovery-muted-readable\)", re.S),
            )

    def test_discovery_static_icons_are_hidden_from_accessibility_tree(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "templates" / "discovery.html").read_text(encoding="utf-8")

        icons = re.findall(r"<i\s+[^>]*data-lucide=[^>]*>", source)
        self.assertTrue(icons)
        self.assertTrue(all('aria-hidden="true"' in icon for icon in icons))

    def test_discovery_uses_quiet_shell_and_green_source_tab_selection(self):
        template = (Path(__file__).resolve().parents[1] / "app" / "templates" / "discovery.html").read_text(encoding="utf-8")
        styles = STYLES.read_text(encoding="utf-8")

        self.assertIn("{% block body_class %}discovery-workspace-page{% endblock %}", template)
        self.assertRegex(
            styles,
            re.compile(r"\.discovery-workspace-page::after\s*\{[^}]*display:\s*none", re.S),
        )
        self.assertRegex(
            styles,
            re.compile(
                r"\.discovery-source-tab\.is-active\s*\{[^}]*"
                r"color:\s*var\(--accent\)[^}]*background:\s*var\(--accent-soft\)",
                re.S,
            ),
        )
        self.assertNotIn(".discovery-source-tab.is-active::after", styles)
        self.assertNotIn(".discovery-source-tab::after", styles)


if __name__ == "__main__":
    unittest.main()
