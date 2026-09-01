from __future__ import annotations

import asyncio
from contextlib import contextmanager
import errno
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.responses import JSONResponse
from starlette.datastructures import URL

from app import config
from app.agent.feature_gate import is_agent_enabled
from app.main import create_app
from app.routes import strm_api
from app.routes.api import get_config, save_config
from app.web import relative_url_for


class AgentSettingsUiTests(unittest.TestCase):
    @staticmethod
    def _request():
        return SimpleNamespace(
            session={"logged_in": True},
            app=SimpleNamespace(
                state=SimpleNamespace(
                    background_services_enabled=False,
                    media_proxy_manager=None,
                )
            ),
        )

    @staticmethod
    def _payload(response):
        if isinstance(response, JSONResponse):
            return json.loads(response.body)
        return response

    def test_shared_config_save_surfaces_runtime_warnings_without_false_success(self):
        source = Path("app/static/js/app.js").read_text(encoding="utf-8")
        stylesheet = Path("app/static/css/main.css").read_text(encoding="utf-8")

        self.assertIn("Array.isArray(data.warnings)", source)
        self.assertIn("warnings.join('；'), 'warning'", source)
        self.assertIn("triangle-alert", source)
        self.assertIn(".app-toast.is-warning", stylesheet)

    def test_agent_defaults_to_disabled_when_unconfigured(self):
        with patch(
            "app.agent.feature_gate.config.get_bool",
            side_effect=lambda _key, default=False: default,
        ) as get_bool:
            self.assertFalse(is_agent_enabled())
        get_bool.assert_called_once_with("AGENT_ENABLED", False)

        html = (Path("app/templates/settings.html").read_text(encoding="utf-8") + Path("app/static/js/settings.js").read_text(encoding="utf-8"))
        self.assertIn("AGENT_ENABLED:'0'", html)
        self.assertNotIn("AGENT_ENABLED:'1'", html)

    def test_settings_expose_patrol_and_complete_tavily_controls(self):
        html = (Path("app/templates/settings.html").read_text(encoding="utf-8") + Path("app/static/js/settings.js").read_text(encoding="utf-8"))
        self.assertIn('<div class="card card-pad" id="settingsForm">', html)
        self.assertNotIn('<form class="card card-pad" id="settingsForm"', html)
        self.assertEqual(html.count('<form class="settings-panel'), 7)
        self.assertEqual(html.count('onsubmit="return false;" novalidate'), 7)
        self.assertIn('data-lucide="bot"', html)
        self.assertNotIn('data-lucide="message-circle-check"', html)
        self.assertIn('data-settings-target="agent"', html)
        self.assertIn('data-settings-panel="agent"', html)
        for key in (
            "AGENT_LIBRARY_PATROL_ENABLED",
            "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED",
            "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED",
            "AGENT_LIBRARY_PATROL_INTERVAL_HOURS",
            "AGENT_LIBRARY_PATROL_MAX_SERIES",
            "TAVILY_CACHE_TTL_SECONDS",
            "TAVILY_TIMEOUT_SECONDS",
        ):
            self.assertIn(f'data-key="{key}"', html)
        for protocol in (
            '<option value="auto">自动（Responses → Chat Completions）</option>',
            '<option value="responses">OpenAI Responses API</option>',
            '<option value="chat_completions">OpenAI Chat Completions API</option>',
            '<option value="anthropic_messages">Anthropic Messages API</option>',
        ):
            self.assertIn(protocol, html)
        for default in (
            "TAVILY_CACHE_TTL_SECONDS:'900'",
            "TAVILY_TIMEOUT_SECONDS:'10'",
            "AGENT_LIBRARY_PATROL_ENABLED:'0'",
            "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED:'1'",
            "AGENT_LIBRARY_PATROL_INTERVAL_HOURS:'24'",
            "AGENT_LIBRARY_PATROL_MAX_SERIES:'50'",
        ):
            self.assertIn(default, html)
        self.assertIn("不会立即扫描全库", html)
        self.assertNotIn("password-toggle-btn", html)
        self.assertIn('id="testQbBtn"', html)
        self.assertIn("/api/downloads/qb/test", html)
        self.assertIn('id="qbConnectionState"', html)
        downloads_panel = html[html.index('id="settings-panel-downloads"'):html.index('id="settings-panel-network"')]
        self.assertIn('id="qbConnectionState"', downloads_panel)
        self.assertIn('id="testQbBtn"', downloads_panel)
        self.assertIn('class="settings-layout-2col"', downloads_panel)
        self.assertIn('class="settings-telemetry-card"', downloads_panel)
        self.assertNotIn('data-key="DOWNLOAD_TORRENT_RETENTION_DAYS"', downloads_panel)
        self.assertNotIn('class="qb-connection-status"', downloads_panel)
        downloads_ui = (
            Path("app/templates/downloads.html").read_text(encoding="utf-8")
            + Path("app/static/js/downloads.js").read_text(encoding="utf-8")
        )
        downloads_styles = Path("app/static/css/main.css").read_text(encoding="utf-8")
        self.assertIn('id="torrentCachePolicyModal"', downloads_ui)
        self.assertIn('class="download-cache-dialog"', downloads_ui)
        self.assertNotIn('class="card download-cache-dialog"', downloads_ui)
        self.assertIn('data-key="DOWNLOAD_TORRENT_RETENTION_DAYS"', downloads_ui)
        self.assertIn('min="0" max="3650"', downloads_ui)
        self.assertIn("设置原始 .torrent 的保留时间", downloads_ui)
        self.assertIn("0 表示永久，最多 3650 天", downloads_ui)
        self.assertIn("不删除下载任务或本地文件", downloads_ui)
        self.assertNotIn("data-cache-retention-days", downloads_ui)
        self.assertNotIn("qB Web API 导出原始种子", downloads_ui)
        self.assertNotIn("download-cache-backends", downloads_ui)
        self.assertIn("window.loadAppConfig({signal:controller.signal})", downloads_ui)
        self.assertIn("window.saveAppConfig(torrentCachePolicyForm", downloads_ui)
        self.assertIn(".download-cache-actions .download-cache-action", downloads_styles)
        self.assertIn("height: 40px;", downloads_styles)
        self.assertIn("width: 132px;", downloads_styles)
        agent_panel = html[html.index('id="settings-panel-agent"'):html.index('id="settings-panel-metadata"')]
        telegram_panel = html[html.index('id="settings-panel-telegram"'):html.index('id="settings-panel-agent"')]
        discovery_panel = html[html.index('id="settings-panel-discovery"'):html.index('id="settings-panel-downloads"')]
        metadata_panel = html[html.index('id="settings-panel-metadata"'):html.index('id="settings-panel-discovery"')]
        self.assertIn("1LOU Google 回退", discovery_panel)
        self.assertNotIn("1LOU 优先使用 Google", html)
        self.assertIn("TMDB 候选召回模式", metadata_panel)
        self.assertIn("AI 线索可信门槛", metadata_panel)
        self.assertNotIn('data-key="TMDB_PREVIEW_CONFIRM"', metadata_panel)
        self.assertNotIn("TMDB_PREVIEW_CONFIRM:'1'", html)
        for key in ("AGENT_LLM_API_URL", "AGENT_LLM_API_KEY", "AGENT_LLM_MODEL", "WEB_SEARCH_ENABLED", "TAVILY_API_KEY"):
            self.assertIn(f'data-key="{key}"', agent_panel)
            self.assertNotIn(f'data-key="{key}"', telegram_panel)
            self.assertNotIn(f'data-key="{key}"', discovery_panel)
        for duplicate_key in (
            "AI_RECOGNITION_API_URL",
            "AI_RECOGNITION_API_KEY",
            "AI_RECOGNITION_MODEL",
            "AI_RECOGNITION_TIMEOUT_SECONDS",
        ):
            self.assertNotIn(f'data-key="{duplicate_key}"', metadata_panel)
        self.assertNotIn('data-settings-jump="agent"', metadata_panel)
        self.assertNotIn("复用 Media Agent 模型连接", metadata_panel)
        self.assertNotIn('class="metadata-settings-hero"', metadata_panel)
        self.assertNotIn('class="metadata-handoff-card"', metadata_panel)
        telegram_card, telegram_savebar = telegram_panel.split('class="settings-savebar"', 1)
        self.assertIn('id="testTelegramBtn"', telegram_card)
        self.assertNotIn('id="testTelegramBtn"', telegram_savebar)
        self.assertIn('id="testAgentModelBtn"', agent_panel)
        self.assertIn('aria-describedby="agentModelState"', agent_panel)
        self.assertIn('id="agentModelCapabilities"', agent_panel)
        self.assertIn('data-capability="tool_calling"', agent_panel)
        self.assertIn('class="agent-field agent-field-wide agent-model-field"', agent_panel)
        self.assertIn('class="agent-field agent-timeout-field"', agent_panel)
        fetch_model_button = re.search(
            r'<button[^>]+id="fetchAgentModelsBtn"[^>]*>.*?</button>',
            agent_panel,
        )
        test_model_button = re.search(
            r'<button[^>]+id="testAgentModelBtn"[^>]*>.*?</button>',
            agent_panel,
        )
        self.assertIsNotNone(fetch_model_button)
        self.assertIsNotNone(test_model_button)
        for button, label in (
            (fetch_model_button.group(0), "获取模型列表"),
            (test_model_button.group(0), "测试模型连接"),
        ):
            self.assertIn(f'aria-label="{label}"', button)
            self.assertIn(f'title="{label}"', button)
            self.assertIn('aria-busy="false"', button)
            self.assertNotRegex(button, r'</i>\s*[^<\s]')
        self.assertIn("/api/tools/ai/test", html)
        self.assertIn("正在验证结构化输出、工具调用与流式输出…", html)
        self.assertIn("全功能可用 · ${protocol} · ${data.elapsed_ms||0} ms", html)
        self.assertIn("settings-agent.css", html)
        self.assertIn('class="settings-panel settings-panel-split active" id="settings-panel-console"', html)
        self.assertIn('class="settings-panel settings-panel-split" id="settings-panel-telegram"', html)
        self.assertIn('class="agent-settings-stack"', agent_panel)
        self.assertGreaterEqual(agent_panel.count('class="agent-settings-card agent-settings-card-'), 4)
        self.assertNotIn("agent-settings-index", agent_panel)
        self.assertNotIn("若配置由部署环境提供", agent_panel)
        self.assertNotIn("01 模型路由", agent_panel)
        self.assertIn("data-managed-note", agent_panel)
        self.assertIn(" hidden></p>", agent_panel)
        for target in (
            "console", "telegram", "agent", "metadata",
            "discovery", "downloads", "network",
        ):
            self.assertIn(f'id="settings-tab-{target}"', html)
            self.assertIn(f'aria-controls="settings-panel-{target}"', html)
            self.assertIn(f'id="settings-panel-{target}"', html)
            self.assertIn('role="tabpanel"', html)
            panel_id = html.index(f'id="settings-panel-{target}"')
            panel_start = html.rfind('<form class="settings-panel', 0, panel_id)
            panel_end = html.index('</form>', panel_id)
            self.assertGreaterEqual(panel_start, 0)
            self.assertGreater(panel_end, panel_start)
            self.assertIn(
                'onsubmit="return false;" novalidate',
                html[panel_start:html.index('>', panel_start) + 1],
            )
        self.assertIn("event.key==='ArrowRight'", html)
        self.assertIn("event.key==='Home'", html)
        for label in (
            "启用自动巡检",
            "发送巡检通知",
            "发送下载后入库复核通知",
            "巡检间隔（小时）",
            "单次巡检剧集上限",
            "启用 Tavily 网络搜索",
            "Tavily API Key",
            "Tavily 搜索缓存时间（秒）",
            "Tavily 请求超时（秒）",
        ):
            self.assertIn(f'aria-label="{label}"', html)

    def test_ai_model_list_route_is_registered(self):
        app = create_app()
        self.assertEqual(str(app.url_path_for("ai_models")), "/api/tools/ai/models")
        self.assertEqual(str(app.url_path_for("ai_model_test")), "/api/tools/ai/test")

    def test_template_url_for_is_relative_even_with_https_forwarded_origin(self):
        request = SimpleNamespace(
            url_for=lambda _name, **_params: URL(
                "https://media.example.test:1258/rss?source=dashboard#latest"
            )
        )
        self.assertEqual(
            relative_url_for({"request": request}, "pages.rss"),
            "/rss?source=dashboard#latest",
        )

    def test_config_get_projects_effective_managed_values_without_exposing_secrets(self):
        effective = {
            "TAVILY_API_KEY": "deployment-secret",
            "AGENT_LLM_API_KEY": "llm-deployment-secret",
            "AGENT_LLM_MODEL": "deployment-model",
            "AGENT_LIBRARY_PATROL_ENABLED": "1",
            "DOWNLOAD_TORRENT_RETENTION_DAYS": "30",
            "GY_STRM_BASE_URL": "http://mediaflux.internal:1258",
        }
        with patch(
            "app.routes.api.config.all_items",
            return_value={"TAVILY_API_KEY": "stale-file-value"},
        ), patch(
            "app.routes.api.config.has_external_override",
            side_effect=lambda key: key in effective,
        ), patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": effective.get(key, default),
        ):
            payload = get_config(self._request())

        self.assertEqual(payload["TAVILY_API_KEY"], "********")
        self.assertEqual(payload["AGENT_LLM_API_KEY"], "********")
        self.assertEqual(payload["AGENT_LLM_MODEL"], "deployment-model")
        self.assertEqual(payload["AGENT_LIBRARY_PATROL_ENABLED"], "1")
        self.assertEqual(payload["GY_STRM_BASE_URL"], "http://mediaflux.internal:1258")
        self.assertEqual(payload["DOWNLOAD_TORRENT_RETENTION_DAYS"], "30")
        self.assertEqual(
            payload["__managed_fields"],
            [
                "AGENT_LIBRARY_PATROL_ENABLED",
                "AGENT_LLM_API_KEY",
                "AGENT_LLM_MODEL",
                "DOWNLOAD_TORRENT_RETENTION_DAYS",
                "GY_STRM_BASE_URL",
                "TAVILY_API_KEY",
            ],
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("deployment-secret", serialized)
        self.assertNotIn("llm-deployment-secret", serialized)

    def test_only_real_config_changes_persist_and_advance_agent_runtime(self):
        values = {
            "AGENT_ENABLED": "1",
            "AGENT_LLM_MODEL": "old-model",
        }
        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.routes.api.config.set_and_save") as persist, patch(
            "app.bot.restart_bot"
        ) as restart, patch("app.services.clear_dashboard_cache"), patch(
            "app.modules.agent_runtime.request_agent_runtime_reconcile"
        ) as reconcile, patch(
            "app.bot.handlers.request_command_menu_refresh"
        ) as refresh_menu, patch(
            "app.agent.feature_gate.invalidate_agent_runtime_generation"
        ) as invalidate:
            unchanged = save_config(self._request(), {"AGENT_ENABLED": "1"})
            changed = save_config(
                self._request(),
                {"AGENT_ENABLED": "1", "AGENT_LLM_MODEL": "new-model"},
            )

        self.assertEqual(unchanged, {"success": True})
        self.assertEqual(changed, {"success": True})
        persist.assert_called_once_with({"AGENT_LLM_MODEL": "new-model"})
        restart.assert_not_called()
        reconcile.assert_not_called()
        refresh_menu.assert_not_called()
        invalidate.assert_called_once_with()

    def test_agent_feature_gate_hot_toggle_queues_runtime_without_bot_restart(self):
        request = self._request()
        request.app.state.background_services_enabled = True

        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": "1" if key == "AGENT_ENABLED" else default,
        ), patch("app.routes.api.config.set_and_save"), patch(
            "app.bot.restart_bot"
        ) as restart, patch("app.services.clear_dashboard_cache"), patch(
            "app.modules.agent_runtime.request_agent_runtime_reconcile"
        ) as reconcile, patch(
            "app.bot.handlers.request_command_menu_refresh"
        ) as refresh_menu, patch(
            "app.agent.feature_gate.invalidate_agent_runtime_generation"
        ) as invalidate:
            response = save_config(request, {"AGENT_ENABLED": "0"})

        self.assertEqual(response, {"success": True})
        restart.assert_not_called()
        reconcile.assert_called_once_with()
        refresh_menu.assert_called_once_with()
        invalidate.assert_called_once_with()

    def test_agent_toggle_publishes_config_and_generation_in_one_transition(self):
        events: list[str] = []

        @contextmanager
        def transition():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": "1" if key == "AGENT_ENABLED" else default,
        ), patch(
            "app.routes.api.config.set_and_save",
            side_effect=lambda _updates: events.append("persist"),
        ), patch(
            "app.agent.feature_gate.agent_runtime_transition",
            side_effect=transition,
        ), patch(
            "app.agent.feature_gate.invalidate_agent_runtime_generation",
            side_effect=lambda: events.append("invalidate") or 1,
        ), patch("app.services.clear_dashboard_cache"):
            response = save_config(self._request(), {"AGENT_ENABLED": "0"})

        self.assertEqual(response, {"success": True})
        self.assertEqual(events, ["enter", "persist", "invalidate", "exit"])

    def test_telegram_agent_toggle_refreshes_menu_and_runtime_generation(self):
        request = self._request()
        request.app.state.background_services_enabled = True

        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": "1" if key == "TG_AGENT_ENABLED" else default,
        ), patch("app.routes.api.config.set_and_save"), patch(
            "app.bot.restart_bot"
        ) as restart, patch("app.services.clear_dashboard_cache"), patch(
            "app.modules.agent_runtime.request_agent_runtime_reconcile"
        ) as reconcile, patch(
            "app.bot.handlers.request_command_menu_refresh"
        ) as refresh_menu, patch(
            "app.agent.feature_gate.invalidate_agent_runtime_generation"
        ) as invalidate:
            response = save_config(request, {"TG_AGENT_ENABLED": "0"})

        self.assertEqual(response, {"success": True})
        restart.assert_not_called()
        reconcile.assert_not_called()
        refresh_menu.assert_called_once_with()
        invalidate.assert_called_once_with()

    def test_telegram_identity_changes_advance_runtime_generation(self):
        cases = (
            ("TG_BOT_TOKEN", "123:old-token", "123:new-token"),
            ("TG_CHAT_ID", "100", "101"),
            ("TG_AGENT_ALLOWED_USER_IDS", "200", "201"),
        )
        for key, old_value, new_value in cases:
            with self.subTest(key=key):
                values = {
                    "TG_AGENT_ENABLED": "0",
                    "TG_BOT_TOKEN": "123:old-token",
                    "TG_CHAT_ID": "100",
                    "TG_AGENT_ALLOWED_USER_IDS": "200",
                    key: old_value,
                }
                with patch(
                    "app.routes.api.config.get",
                    side_effect=lambda name, default="": values.get(name, default),
                ), patch(
                    "app.routes.api.config.set_and_save"
                ) as persist, patch(
                    "app.notifier.reset"
                ), patch(
                    "app.services.clear_dashboard_cache"
                ), patch(
                    "app.agent.feature_gate.invalidate_agent_runtime_generation"
                ) as invalidate:
                    response = save_config(self._request(), {key: new_value})

                self.assertEqual(response, {"success": True})
                persist.assert_called_once_with({key: new_value})
                invalidate.assert_called_once_with()

    def test_download_verification_notification_toggle_reloads_scheduler(self):
        request = self._request()
        scheduler = MagicMock()

        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": (
                "1" if key == "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED" else default
            ),
        ), patch("app.routes.api.config.set_and_save"), patch(
            "app.services.clear_dashboard_cache"
        ), patch(
            "app.modules.agent_download_verification_scheduler."
            "get_download_library_verification_scheduler",
            return_value=scheduler,
        ):
            response = save_config(
                request, {"AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED": "0"}
            )

        self.assertEqual(response, {"success": True})
        scheduler.reload.assert_called_once_with()

    def test_torrent_retention_days_are_validated_saved_and_reloaded(self):
        request = self._request()
        tracker = MagicMock()

        with patch(
            "app.routes.api.config.get",
            side_effect=lambda _key, default="": default,
        ), patch("app.routes.api.config.set_and_save") as persist, patch(
            "app.services.clear_dashboard_cache"
        ), patch(
            "app.modules.download_tracker.get_download_tracker",
            return_value=tracker,
        ):
            response = save_config(
                request, {"DOWNLOAD_TORRENT_RETENTION_DAYS": "30"}
            )

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({"DOWNLOAD_TORRENT_RETENTION_DAYS": "30"})
        tracker.reload.assert_called_once_with(reset_torrent_cleanup=True)

        for invalid in ("-1", "3651", "1.5", "not-a-number"):
            with patch("app.routes.api.config.set_and_save") as rejected_persist:
                rejected = save_config(
                    request, {"DOWNLOAD_TORRENT_RETENTION_DAYS": invalid}
                )
            self.assertEqual(rejected.status_code, 400)
            rejected_persist.assert_not_called()

    def test_blank_torrent_retention_normalizes_to_permanent(self):
        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": (
                "30" if key == "DOWNLOAD_TORRENT_RETENTION_DAYS" else default
            ),
        ), patch("app.routes.api.config.set_and_save") as persist, patch(
            "app.services.clear_dashboard_cache"
        ), patch(
            "app.modules.download_tracker.get_download_tracker"
        ):
            response = save_config(
                self._request(), {"DOWNLOAD_TORRENT_RETENTION_DAYS": ""}
            )

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({"DOWNLOAD_TORRENT_RETENTION_DAYS": "0"})

    def test_secret_can_be_replaced_or_explicitly_cleared_without_reveal(self):
        with patch("app.routes.api.config.set_and_save") as persist, patch(
            "app.services.clear_dashboard_cache"
        ):
            replaced = save_config(
                self._request(), {"AGENT_LLM_API_KEY": "rotated-secret"}
            )
        self.assertEqual(replaced, {"success": True})
        persist.assert_called_once_with({"AGENT_LLM_API_KEY": "rotated-secret"})

        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": (
                "existing-secret" if key == "AGENT_LLM_API_KEY" else default
            ),
        ), patch("app.routes.api.config.set_and_save") as persist, patch(
            "app.services.clear_dashboard_cache"
        ):
            cleared = save_config(
                self._request(), {"__clear_secrets": ["AGENT_LLM_API_KEY"]}
            )
        self.assertEqual(cleared, {"success": True})
        persist.assert_called_once_with({"AGENT_LLM_API_KEY": ""})

    def test_model_list_rejects_unknown_protocol_and_returns_provider_models(self):
        from app.routes import tools_api

        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api, "run_awaitable_sync"
        ) as runner:
            rejected = tools_api.ai_models(
                self._request(), {"base_url": "https://ai.invalid/v1", "protocol": "legacy"}
            )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("接口协议", self._payload(rejected)["error"])
        runner.assert_not_called()

        def resolve_models(awaitable):
            awaitable.close()
            return ["model-a", "model-b"]

        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api, "_fetch_ai_models"
        ) as fetch_models, patch.object(
            tools_api, "run_awaitable_sync", side_effect=resolve_models
        ) as runner, patch.object(
            tools_api.config, "get_int", return_value=12
        ):
            response = tools_api.ai_models(
                self._request(),
                {
                    "base_url": "https://ai.invalid/v1",
                    "api_key": "draft-secret",
                    "protocol": "responses",
                },
            )
        payload = self._payload(response)
        self.assertEqual(payload["models"], ["model-a", "model-b"])
        self.assertEqual(payload["protocol"], "responses")
        fetch_models.assert_called_once_with(
            base_url="https://ai.invalid/v1",
            api_key="draft-secret",
            protocol="responses",
            timeout_seconds=12,
        )
        self.assertEqual(runner.call_count, 1)

    def test_model_test_uses_current_draft_without_saving_it(self):
        from app.routes import tools_api

        def resolve_test(awaitable):
            awaitable.close()
            return {
                "ok": True,
                "protocol": "anthropic_messages",
                "status_code": 200,
                "elapsed_ms": 184,
            }

        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api.agent_rate_limiter, "allow", return_value=True
        ) as allow, patch.object(
            tools_api, "_test_ai_provider", wraps=tools_api._test_ai_provider
        ) as test_provider, patch.object(
            tools_api,
            "run_awaitable_sync",
            side_effect=resolve_test,
        ) as runner:
            response = tools_api.ai_model_test(
                self._request(),
                {
                    "base_url": "https://api.example.test/v1/messages",
                    "api_key": "draft-secret",
                    "protocol": "auto",
                    "model": "claude-test",
                    "timeout_seconds": 14,
                },
            )

        self.assertEqual(
            self._payload(response),
            {
                "ok": True,
                "protocol": "anthropic_messages",
                "status_code": 200,
                "elapsed_ms": 184,
            },
        )
        allow.assert_called_once()
        test_provider.assert_called_once_with(
            base_url="https://api.example.test/v1/messages",
            api_key="draft-secret",
            protocol="anthropic_messages",
            model="claude-test",
            timeout_seconds=14,
        )
        self.assertEqual(runner.call_count, 1)

    def test_model_test_has_rate_limit_and_actionable_safe_errors(self):
        from app.routes import tools_api

        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api.agent_rate_limiter, "allow", return_value=False
        ), patch.object(tools_api, "_test_ai_provider") as test_provider:
            limited = tools_api.ai_model_test(self._request(), {})
        self.assertEqual(limited.status_code, 429)
        self.assertIn("过于频繁", self._payload(limited)["error"])
        test_provider.assert_not_called()

        failure = tools_api._AIProviderTestFailure(
            "upstream_status", protocol="responses", status_code=401
        )

        def reject_test(awaitable):
            awaitable.close()
            raise failure

        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api.agent_rate_limiter, "allow", return_value=True
        ), patch.object(
            tools_api, "run_awaitable_sync", side_effect=reject_test
        ):
            rejected = tools_api.ai_model_test(
                self._request(),
                {
                    "base_url": "https://api.example.test/v1",
                    "api_key": "not-returned",
                    "protocol": "responses",
                    "model": "model-test",
                    "timeout_seconds": 12,
                },
            )
        self.assertEqual(rejected.status_code, 502)
        serialized = json.dumps(self._payload(rejected), ensure_ascii=False)
        self.assertIn("鉴权失败", serialized)
        self.assertNotIn("not-returned", serialized)

    def test_model_test_auto_falls_back_and_reports_full_capability_matrix(self):
        from app.routes import tools_api

        class FakeStream:
            status_code = 200
            headers = {"content-type": "text/event-stream; charset=utf-8"}

            async def aiter_bytes(self):
                yield b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n'
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                yield b'data: [DONE]\n\n'

        class StreamContext:
            async def __aenter__(self):
                return FakeStream()

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        class FakeClient:
            instance = None

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                self.stream_calls = []
                self.closed = False
                self.responses = [
                    SimpleNamespace(status_code=404, text="{}"),
                    SimpleNamespace(
                        status_code=200,
                        text=json.dumps({
                            "choices": [{"message": {"content": '{"ok": true}'}}]
                        }),
                    ),
                    SimpleNamespace(
                        status_code=200,
                        text=json.dumps({
                            "choices": [{"message": {
                                "content": None,
                                "tool_calls": [{
                                    "id": "call_probe",
                                    "type": "function",
                                    "function": {
                                        "name": "mediaflux_connectivity_probe",
                                        "arguments": "{}",
                                    },
                                }],
                            }}]
                        }),
                    ),
                ]
                FakeClient.instance = self

            async def post_json(self, url, *, json, headers, max_redirects):
                self.calls.append((url, json, headers, max_redirects))
                return self.responses.pop(0)

            def stream_post_json(self, url, *, json, headers, max_redirects):
                self.stream_calls.append((url, json, headers, max_redirects))
                return StreamContext()

            async def aclose(self):
                self.closed = True

        with patch.object(tools_api, "FixedHostHttpClient", FakeClient):
            result = asyncio.run(tools_api._test_ai_provider(
                base_url="https://api.example.test/v1",
                api_key="provider-secret",
                protocol="auto",
                model="model-test",
                timeout_seconds=12,
            ))

        client = FakeClient.instance
        self.assertIsNotNone(client)
        self.assertEqual(result["protocol"], "chat_completions")
        self.assertEqual(result["status_code"], 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["capabilities"], {
            "structured_output": True,
            "tool_calling": True,
            "streaming": True,
        })
        self.assertTrue(client.closed)
        self.assertEqual(
            [call[0] for call in client.calls],
            [
                "https://api.example.test/v1/responses",
                "https://api.example.test/v1/chat/completions",
                "https://api.example.test/v1/chat/completions",
            ],
        )
        chat_body = client.calls[1][1]
        self.assertEqual(chat_body["model"], "model-test")
        self.assertEqual(
            chat_body["response_format"]["json_schema"]["schema"],
            tools_api._AI_MODEL_TEST_SCHEMA,
        )
        self.assertNotIn("tools", chat_body)
        tool_body = client.calls[2][1]
        self.assertIn("tools", tool_body)
        self.assertEqual(
            tool_body["tools"][0]["function"]["name"],
            "mediaflux_connectivity_probe",
        )
        self.assertTrue(client.stream_calls[0][1]["stream"])
        self.assertNotIn("provider-secret", json.dumps(chat_body))
        self.assertEqual(
            client.calls[1][2]["Authorization"], "Bearer provider-secret"
        )

    def test_model_test_does_not_claim_json_only_provider_supports_tools(self):
        from app.routes import tools_api

        class UnsupportedStream:
            status_code = 200
            headers = {"content-type": "application/json"}

        class StreamContext:
            async def __aenter__(self):
                return UnsupportedStream()

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        class FakeClient:
            def __init__(self, **kwargs):
                self.responses = [
                    SimpleNamespace(
                        status_code=200,
                        text=json.dumps({
                            "choices": [{"message": {"content": '{"ok": true}'}}]
                        }),
                    ),
                    SimpleNamespace(
                        status_code=200,
                        text=json.dumps({
                            "choices": [{"message": {"content": "工具不可用"}}]
                        }),
                    ),
                ]

            async def post_json(self, *args, **kwargs):
                return self.responses.pop(0)

            def stream_post_json(self, *args, **kwargs):
                return StreamContext()

            async def aclose(self):
                return None

        with patch.object(tools_api, "FixedHostHttpClient", FakeClient):
            result = asyncio.run(tools_api._test_ai_provider(
                base_url="https://api.example.test/v1",
                api_key="",
                protocol="chat_completions",
                model="json-only-model",
                timeout_seconds=12,
            ))

        self.assertEqual(result["capabilities"], {
            "structured_output": True,
            "tool_calling": False,
            "streaming": False,
        })

    def test_config_save_rejects_environment_managed_agent_field(self):
        with patch(
            "app.routes.api.config.has_external_override",
            side_effect=lambda key: key == "TAVILY_TIMEOUT_SECONDS",
        ), patch("app.routes.api.config.set_and_save") as persist:
            response = save_config(
                self._request(), {"TAVILY_TIMEOUT_SECONDS": "12"}
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("部署环境管理", self._payload(response)["error"])
        persist.assert_not_called()

    def test_config_save_rejects_entire_mixed_payload_with_removed_network_key(self):
        with patch("app.routes.api.config.set_and_save") as persist:
            response = save_config(
                self._request(),
                {"WEB_PORT": "22366", "AGENT_LLM_MODEL": "other-model"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("WEB_PORT", self._payload(response)["error"])
        persist.assert_not_called()

    def test_config_save_rejects_removed_network_keys_as_unknown(self):
        with patch("app.routes.api.config.set_and_save") as persist:
            response = save_config(
                self._request(), {"WEB_HOST": "0.0.0.0", "WEB_PORT": "22366"}
            )

        self.assertEqual(response.status_code, 400)
        error = self._payload(response)["error"]
        self.assertIn("WEB_HOST", error)
        self.assertIn("WEB_PORT", error)
        persist.assert_not_called()

    def test_strm_schedule_returns_conflict_without_reloading_for_external_override(self):
        scheduler = MagicMock()
        with patch.object(strm_api, "require_api_login", return_value=None), patch.object(
            strm_api.config,
            "set_and_save",
            side_effect=config.ExternalConfigOverrideError(
                "目标配置由运行环境覆盖: STRM_SCHEDULE_ENABLED"
            ),
        ), patch.object(strm_api, "get_scheduler", return_value=scheduler):
            response = strm_api.update_schedule(
                self._request(),
                {"enabled": True, "cron": "0 4 * * *", "notify_enabled": True},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("STRM_SCHEDULE_ENABLED", self._payload(response)["error"])
        scheduler.reload.assert_not_called()
        scheduler.status.assert_not_called()

    def test_strm_schedule_maps_storage_error_without_reloading(self):
        scheduler = MagicMock()
        with patch.object(strm_api, "require_api_login", return_value=None), patch.object(
            strm_api.config,
            "set_and_save",
            side_effect=PermissionError(errno.EPERM, "operation not permitted"),
        ), patch.object(
            strm_api, "get_scheduler", return_value=scheduler
        ), self.assertLogs("app.routes.strm_api", level="ERROR") as captured:
            response = strm_api.update_schedule(
                self._request(),
                {"enabled": True, "cron": "0 4 * * *", "notify_enabled": True},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("数据目录权限", self._payload(response)["error"])
        self.assertEqual(len(captured.records), 1)
        self.assertIsNone(captured.records[0].exc_info)
        scheduler.reload.assert_not_called()
        scheduler.status.assert_not_called()

    def test_frontend_skips_managed_fields_and_keeps_stable_notice(self):
        script = Path("app/static/js/app.js").read_text(encoding="utf-8")
        css = Path("app/static/css/main.css").read_text(encoding="utf-8")
        agent_css = Path("app/static/css/settings-agent.css").read_text(encoding="utf-8")
        self.assertIn("config.__managed_fields", script)
        self.assertIn("managedByEnvironment", script)
        self.assertIn("if (field.dataset.managedByEnvironment === 'true') return;", script)
        self.assertIn("[data-managed-note]", script)
        self.assertIn("note.hidden = managed.length === 0", script)
        self.assertIn("部署环境已锁定", script)
        self.assertNotIn("页面会自动锁定并避免覆盖", script)
        self.assertIn(".settings-managed-note", css)
        self.assertIn(".agent-settings-shell", agent_css)
        self.assertIn(".settings-panel-split .form-row", agent_css)
        self.assertIn("--settings-split-control: 100%", agent_css)
        self.assertIn("grid-template-columns: minmax(130px, 190px) minmax(0, 1fr)", agent_css)
        self.assertIn("--settings-inline-gutter: clamp(28px, 2.2vw, 40px)", agent_css)
        self.assertIn("--settings-topbar-h: 64px", agent_css)
        self.assertNotIn("--topbar-h: 64px", agent_css)
        self.assertNotIn(".settings-page .sidebar { background:", agent_css)
        self.assertIn("min-height: var(--settings-topbar-h)", agent_css)
        self.assertIn("top: calc(var(--settings-topbar-h) + 12px)", agent_css)
        self.assertIn("padding: 0 var(--settings-inline-gutter)", agent_css)
        self.assertIn("margin: 16px var(--settings-inline-gutter) 0", agent_css)
        self.assertNotIn("width: min(1324px, 100%)", agent_css)
        self.assertNotIn("width: min(1536px, 100%)", agent_css)
        self.assertIn('grid-template-areas:\n        "global global"\n        "model search"\n        "patrol notify"', agent_css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", agent_css)
        self.assertIn("border-left: 1px solid var(--border-soft)", agent_css)
        self.assertIn("min-height: 28px", css)
        self.assertNotIn("repeat(7,minmax(0,1fr))", css)
        self.assertIn("min-height: 52px", agent_css)
        self.assertIn("height: 40px", agent_css)
        self.assertIn("background: var(--accent-soft)", agent_css)
        self.assertTrue(
            "grid-template-columns: repeat(2, 42px)" in agent_css
            or "grid-template-columns: repeat(3, 42px)" in agent_css
        )
        self.assertIn("width: 42px", agent_css)
        self.assertIn("height: 42px", agent_css)
        self.assertIn(".agent-timeout-field > .agent-input-unit { width: min(100%, 190px); }", agent_css)
        self.assertIn("justify-content: end", agent_css)

    def test_settings_save_waits_for_config_before_enabling_actions(self):
        html = (Path("app/templates/settings.html").read_text(encoding="utf-8") + Path("app/static/js/settings.js").read_text(encoding="utf-8"))
        save_buttons = re.findall(r"<button[^>]+data-save-settings[^>]*>", html)

        self.assertEqual(len(save_buttons), 7)
        for button in save_buttons:
            self.assertIn("disabled", button)
            self.assertIn('aria-disabled="true"', button)
        self.assertEqual(html.count("正在读取当前分区配置…"), 7)
        self.assertEqual(html.count("data-ready-message="), 7)
        self.assertIn("let configReady=false;", html)
        self.assertIn("if(!configReady)return;", html)
        self.assertIn("loadIndexerSiteSelection(config);\n        setConfigReady();", html)
        self.assertIn("}).catch(setConfigLoadError);", html)
        self.assertIn("配置读取失败，请刷新页面后重试", html)
        self.assertIn("form.setAttribute('aria-busy','true');", html)

    def test_tmdb_lock_search_ignores_out_of_order_responses(self):
        html = (Path("app/templates/settings.html").read_text(encoding="utf-8") + Path("app/static/js/settings.js").read_text(encoding="utf-8"))

        self.assertIn("let lockRequestGeneration=0;", html)
        self.assertIn("const generation=++lockRequestGeneration;", html)
        self.assertGreaterEqual(
            html.count("if(generation!==lockRequestGeneration)return;"), 2
        )
        self.assertIn("if(body.dataset.loaded!=='true')", html)
        self.assertIn("if(generation===lockRequestGeneration)", html)



if __name__ == "__main__":
    unittest.main()
