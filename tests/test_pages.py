"""页面路由、看板样式与设置页遥测联动测试。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_CSS = ROOT / "app" / "static" / "css" / "dashboard-workbench.css"
MAIN_CSS = ROOT / "app" / "static" / "css" / "main.css"
LOCAL_MEDIA_CSS = ROOT / "app" / "static" / "css" / "local-media.css"
MEDIA_HUB_CSS = ROOT / "app" / "static" / "css" / "media-hub.css"
AGENT_CSS = ROOT / "app" / "static" / "css" / "agent.css"
CORE_LAYOUT_CSS = ROOT / "app" / "static" / "css" / "core-layout.css"

DASHBOARD_HTML = ROOT / "app" / "templates" / "dashboard.html"
BASE_HTML = ROOT / "app" / "templates" / "base.html"
LOCAL_MEDIA_HTML = ROOT / "app" / "templates" / "local_media.html"
GLOBAL_SEARCH_HTML = ROOT / "app" / "templates" / "global_search.html"
MEDIA_RECENT_HTML = ROOT / "app" / "templates" / "media_recent.html"
AGENT_HTML = ROOT / "app" / "templates" / "agent.html"
SETTINGS_HTML = ROOT / "app" / "templates" / "settings.html"
SETTINGS_JS = ROOT / "app" / "static" / "js" / "settings.js"
LOGIN_HTML = ROOT / "app" / "templates" / "login.html"


class PagesUiContractTests(unittest.TestCase):
    def test_dashboard_flat_modern_styles_and_cachebuster(self):
        css = DASHBOARD_CSS.read_text(encoding="utf-8")
        self.assertIn("--dashboard-shadow: none;", css)
        self.assertIn("--dashboard-shadow-hover: none;", css)
        self.assertIn(".dashboard-page .dashboard-metric", css)
        self.assertIn(".dashboard-page .dashboard-panel", css)
        self.assertIn(
            ".dashboard-page .dashboard-poster-card:hover .dashboard-poster-art { transform: translateY(-2px); box-shadow: none; }",
            css,
        )
        self.assertIn(".dashboard-page .dashboard-shell { width: 100%; max-width: none; margin: 0; }", css)
        self.assertNotIn("width: min(1760px, 100%);", css)

        dashboard_html = DASHBOARD_HTML.read_text(encoding="utf-8")
        self.assertIn("css/dashboard-workbench.css') }}?v=20260820a", dashboard_html)

    def test_dashboard_topbar_mobile_responsive_contract(self):
        css = DASHBOARD_CSS.read_text(encoding="utf-8")
        # 确保彻底移除了小屏下隐藏配置按钮的错误规则
        self.assertNotIn(".dashboard-page .dashboard-config-button { display: none; }", css)
        self.assertIn(".dashboard-page .dashboard-config-button {\n        display: inline-flex !important;\n    }", css)

        # 确保媒体选择器在 <=900px 下自适应保留名称和下拉箭头，而不是单绿点
        self.assertIn(".dashboard-page .dashboard-connection-picker select {", css)
        self.assertIn("max-width: 160px;", css)
        self.assertIn("text-overflow: ellipsis;", css)
        self.assertIn("white-space: nowrap;", css)

        # 确保搜索框在移动端自适应弹性伸缩
        self.assertIn(".dashboard-page .dashboard-global-search {\n        width: auto;\n        min-width: 0;\n        flex: 1;\n        height: 40px;\n    }", css)

        # 确保 <=480px 超小屏断点精细适配 (115px max-width, 36px 控件尺寸)
        self.assertIn("@media (max-width: 480px)", css)
        self.assertIn("max-width: 115px;", css)
        self.assertIn(".dashboard-page .dashboard-mobile-menu {\n        width: 36px;\n        height: 36px;\n        flex: 0 0 36px;\n    }", css)
        self.assertIn(".dashboard-page .dashboard-top-actions .icon-btn {\n        width: 36px;\n        height: 36px;\n        flex: 0 0 36px;\n    }", css)

    def test_large_screen_fluid_full_width_layout_contracts(self):
        """验证所有主流页面的定宽限制均已解除，实现大屏 100% 全宽流式排版并更新缓存戳。"""
        main_css = MAIN_CSS.read_text(encoding="utf-8")
        self.assertIn("width: 100%;\n    max-width: none;\n    margin: 0;", main_css)
        self.assertNotIn("width: min(100%, 1540px);", main_css)
        self.assertNotIn("max-width: 1760px;", main_css)

        local_media_css = LOCAL_MEDIA_CSS.read_text(encoding="utf-8")
        self.assertIn(".lm-page {\n    width: 100%;\n    max-width: none;\n    margin: 0;", local_media_css)
        self.assertNotIn("width: min(100%, 1540px);", local_media_css)

        media_hub_css = MEDIA_HUB_CSS.read_text(encoding="utf-8")
        self.assertIn(".media-hub-page .media-hub-shell {\n    width: 100%;\n    max-width: none;", media_hub_css)
        self.assertNotIn("width: min(1760px, 100%);", media_hub_css)

        agent_css = AGENT_CSS.read_text(encoding="utf-8")
        self.assertIn("width: 100%;\n    max-width: none;\n    min-width: 0;\n    margin: 0;", agent_css)
        self.assertNotIn("width: min(1760px, 100%);", agent_css)

        core_layout_css = CORE_LAYOUT_CSS.read_text(encoding="utf-8")
        self.assertIn("--mf-content-max: none;", core_layout_css)
        self.assertNotIn("--mf-content-max: 1560px;", core_layout_css)
        self.assertIn(".content {\n    flex: 1;\n    width: 100%;\n    max-width: none;\n    margin: 0;", core_layout_css)

        # 验证各页面 HTML 缓存版本戳
        base_html = BASE_HTML.read_text(encoding="utf-8")
        self.assertRegex(base_html, r"css/main\.css'\) \}\}\?v=202608(?:1[0-9]|2[0-9])[a-z]")

        local_media_html = LOCAL_MEDIA_HTML.read_text(encoding="utf-8")
        self.assertRegex(local_media_html, r"css/local-media\.css'\) \}\}\?v=202608(?:1[0-9]|2[0-9])[a-z]")

        global_search_html = GLOBAL_SEARCH_HTML.read_text(encoding="utf-8")
        self.assertRegex(global_search_html, r"css/dashboard-workbench\.css'\) \}\}\?v=202608(?:1[0-9]|2[0-9])[a-z]")
        self.assertRegex(global_search_html, r"css/media-hub\.css'\) \}\}\?v=202608(?:1[0-9]|2[0-9])[a-z]")

        media_recent_html = MEDIA_RECENT_HTML.read_text(encoding="utf-8")
        self.assertRegex(media_recent_html, r"css/dashboard-workbench\.css'\) \}\}\?v=202608(?:1[0-9]|2[0-9])[a-z]")
        self.assertRegex(media_recent_html, r"css/media-hub\.css'\) \}\}\?v=202608(?:1[0-9]|2[0-9])[a-z]")

        agent_html = AGENT_HTML.read_text(encoding="utf-8")
        self.assertRegex(agent_html, r"css/agent\.css'\) \}\}\?v=202608(?:1[0-9]|2[0-9])[a-z]")

        login_html = LOGIN_HTML.read_text(encoding="utf-8")
        self.assertRegex(login_html, r"css/core-layout\.css'\) \}\}\?v=202608(?:1[0-9]|2[0-9])[a-z]")

    def test_settings_telegram_quick_commands(self):
        html = (SETTINGS_HTML.read_text(encoding="utf-8") + SETTINGS_JS.read_text(encoding="utf-8"))
        self.assertIn(
            '<span class="telemetry-command-code">/media_search</span><span class="telemetry-command-desc">资源搜索</span>',
            html,
        )
        self.assertIn(
            '<span class="telemetry-command-code">/sync_gy</span><span class="telemetry-command-desc">STRM 完整同步</span>',
            html,
        )
        self.assertNotIn("/sync_gy_full", html)
        self.assertIn(
            '<span class="telemetry-command-code">/organize</span><span class="telemetry-command-desc">统一整理</span>',
            html,
        )
        self.assertIn(
            '<span class="telemetry-command-code">/status</span><span class="telemetry-command-desc">状态遥测</span>',
            html,
        )
        # 确认旧指令已从速查面板中替换
        self.assertNotIn('<span class="telemetry-command-code">/search</span>', html)
        self.assertNotIn('<span class="telemetry-command-code">/start</span><span class="telemetry-command-desc">欢迎初始化</span>', html)

    def test_settings_telemetry_js_script_contract(self):
        html = (SETTINGS_HTML.read_text(encoding="utf-8") + SETTINGS_JS.read_text(encoding="utf-8"))
        self.assertIn("syncTelemetryWidgets", html)
        self.assertIn("telemetryEndpointLabel", html)
        self.assertIn("telemetryFirewallScope", html)
        self.assertIn("window.location.origin", html)
        self.assertIn("当前访问端点", html)
        self.assertIn("由 Docker 发布配置控制", html)
        self.assertIn("copyLocalUrlBtn", html)
        self.assertIn("复制当前访问地址", html)
        self.assertNotIn('data-key="WEB_HOST"', html)
        self.assertNotIn('data-key="WEB_PORT"', html)
        self.assertNotIn("network_apply", html)
        self.assertNotIn("/network-applying", html)

    def test_settings_page_endpoint_telemetry_elements(self):
        html = (SETTINGS_HTML.read_text(encoding="utf-8") + SETTINGS_JS.read_text(encoding="utf-8"))
        self.assertNotIn('data-lan-ip="{{ lan_ip }}"', html)
        self.assertIn('id="telemetryEndpointLabel"', html)
        self.assertIn('id="telemetryFirewallScope"', html)
        self.assertIn('id="telemetryLocalUrl"', html)
        self.assertIn('id="copyLocalUrlBtn"', html)

    def test_settings_large_screen_layout_and_network_2col_contract(self):
        html = (SETTINGS_HTML.read_text(encoding="utf-8") + SETTINGS_JS.read_text(encoding="utf-8"))
        self.assertIn("css/settings-agent.css') }}?v=20260829b", html)
        self.assertIn('id="settings-panel-network"', html)
        self.assertIn('class="settings-layout-2col"', html)
        self.assertIn('aria-label="网络代理遥测与探针"', html)
        self.assertIn('<span>代理策略与分流</span>', html)
        self.assertIn('<span>固定检测目标</span>', html)

        css_path = ROOT / "app" / "static" / "css" / "settings-agent.css"
        css = css_path.read_text(encoding="utf-8")
        # 确保移除了 1360px 宽度限制，实现大屏自适应全屏铺展
        self.assertNotIn("max-width: 1360px;", css)
        self.assertIn("grid-template-columns: minmax(0, 1.25fr) minmax(320px, 1fr);", css)
        self.assertIn(".settings-network-card .network-config-grid", css)

    def test_settings_discovery_cards_do_not_inherit_catalog_hover_motion(self):
        css = (ROOT / "app" / "static" / "css" / "settings-agent.css").read_text(encoding="utf-8")

        self.assertIn(".settings-page #settings-panel-discovery .discovery-card {", css)
        self.assertIn("transition: none;", css)
        self.assertIn(".settings-page #settings-panel-discovery .discovery-card:hover {", css)
        self.assertIn("transform: none;", css)
        self.assertIn("background: var(--bg-elevated);", css)
        self.assertIn("box-shadow: 0 1px 2px rgba(15, 23, 42, .035);", css)
        self.assertIn(".settings-page #settings-panel-discovery .discovery-card:focus-within {", css)


if __name__ == "__main__":
    unittest.main()
