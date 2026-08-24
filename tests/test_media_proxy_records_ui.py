from __future__ import annotations

import unittest
from pathlib import Path


class MediaProxyRecordsUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = Path("app/templates/media_proxy.html").read_text(encoding="utf-8")
        self.css = Path("app/static/css/main.css").read_text(encoding="utf-8")

    def test_proxy_tabs_reuse_strm_spacing_without_double_gap(self):
        self.assertIn('class="strm-nav-tabs proxy-nav-tabs"', self.template)
        self.assertIn('.proxy-settings-shell { display: flex; flex-direction: column; gap: 0; }', self.css)
        self.assertNotIn('.proxy-nav-tabs { display: flex;', self.css)

    def test_proxy_tabs_restore_fragment_before_paint_and_keep_aria_in_sync(self):
        self.assertIn("document.documentElement.dataset.mediaProxyInitialTab", self.template)
        self.assertIn("window.history.replaceState", self.template)
        self.assertIn("activateProxyTab(window.location?.hash?.slice(1)||''", self.template)
        for tab in ("instances", "records", "advanced"):
            self.assertIn(f'id="proxyTabButton_{tab}"', self.template)
            self.assertIn(f'aria-controls="proxyTab_{tab}"', self.template)
            self.assertIn(f'aria-labelledby="proxyTabButton_{tab}"', self.template)
        self.assertIn('html[data-media-proxy-initial-tab="records"] #proxyTab_records', self.css)
        self.assertIn('html[data-media-proxy-initial-tab="advanced"] #proxyTab_advanced', self.css)

    def test_initial_instance_loading_uses_delayed_stable_skeletons(self):
        self.assertNotIn("正在读取服务器配置...", self.template)
        self.assertNotIn("正在加载反代实例...", self.template)
        self.assertIn("proxy-initial-skeleton", self.template)
        self.assertIn("initialProxyLoaderTimer", self.template)
        self.assertIn("settleInitialProxyLoading", self.template)
        self.assertIn("min-height: 96px;", self.css)
        self.assertIn("proxy-skeleton-sweep", self.css)

    def test_records_panel_has_filters_stable_loading_and_pagination(self):
        for element_id in (
            "proxyRecordsPanel", "proxyRecordInstance", "proxyRecordStatus",
            "proxyRecordSource", "proxyRecordRefreshBtn", "proxyRecordClearBtn",
            "proxyRecordList", "proxyRecordPrev", "proxyRecordNext",
            "proxyRecordPage", "proxyCacheMetrics",
        ):
            self.assertIn(f'id="{element_id}"', self.template)
        self.assertIn("proxy-record-list", self.css)
        self.assertIn("min-height:", self.css)
        self.assertIn("proxy-record-loading", self.css)
        self.assertIn("proxy-session-card", self.css)
        self.assertIn("proxy-session-detail", self.css)
        self.assertIn("媒体播放会话", self.template)
        self.assertIn("一次媒体播放显示一条摘要", self.template)
        self.assertIn("@media (max-width:", self.css)

    def test_background_refresh_keeps_rows_and_controls_have_stable_busy_state(self):
        self.assertIn("let proxyRecordsLoaded=false", self.template)
        self.assertIn("loadProxyRecords({background:true})", self.template)
        self.assertIn("setProxyRecordsBusy", self.template)
        self.assertIn("proxyRecordRefreshBtn", self.template)
        self.assertIn("proxyRecordRefreshText", self.template)
        self.assertIn("proxyRecordRequestSerial", self.template)
        self.assertIn("serial!==proxyRecordRequestSerial", self.template)
        self.assertIn(".proxy-record-action { min-width: 116px;", self.css)

    def test_copy_feedback_reports_failure_and_restores_lucide_markup(self):
        self.assertIn("function paintProxyCopyState", self.template)
        self.assertIn("text:'复制失败'", self.template)
        self.assertIn("state:'copy-failed'", self.template)
        self.assertIn("paintProxyCopyState(btn,{text:url,icon:'copy'})", self.template)
        self.assertIn(".proxy-url-chip.copy-failed", self.css)
        self.assertIn(".proxy-url-text {", self.css)
        self.assertIn("overflow-wrap: anywhere;", self.css)

    def test_session_identity_prefers_recorded_media_name_with_id_fallback(self):
        self.assertIn(
            "if(session.media_name)return session.media_name", self.template
        )
        self.assertIn("function compactProxyIdentifier", self.template)
        self.assertIn(
            "`媒体 ${compactProxyIdentifier(session.media_item_id)}`",
            self.template,
        )
        self.assertNotIn('title="${esc(rawSourceId)}"', self.template)
        self.assertNotIn('title="${esc(session.media_item_id)}"', self.template)

    def test_clear_requires_confirmation_and_rows_use_safe_badges(self):
        self.assertIn("CLEAR PLAYBACK RECORDS", self.template)
        self.assertIn("/api/media-proxy/sessions", self.template)
        self.assertIn("/api/media-proxy/records?session_id=", self.template)
        self.assertIn("proxyExpandedSessions", self.template)
        self.assertIn("loadProxySessionDetails", self.template)
        self.assertIn("历史 / 未关联链路", self.template)
        self.assertIn("unlinked:'true'", self.template)
        self.assertIn("sourceBadge", self.template)
        self.assertIn("sessionStatusBadge", self.template)
        self.assertIn("route_class", self.template)
        self.assertIn("cache_hit", self.template)
        self.assertNotIn("record.url", self.template)
        self.assertNotIn("record.token", self.template)
        self.assertNotIn("signed_url", self.template)
        self.assertNotIn("session.session_key", self.template)


if __name__ == "__main__":
    unittest.main()
