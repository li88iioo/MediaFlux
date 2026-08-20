from __future__ import annotations

import asyncio
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
        self.assertNotIn('class="qb-connection-status"', downloads_panel)
        agent_panel = html[html.index('id="settings-panel-agent"'):html.index('id="settings-panel-metadata"')]
        telegram_panel = html[html.index('id="settings-panel-telegram"'):html.index('id="settings-panel-agent"')]
        discovery_panel = html[html.index('id="settings-panel-discovery"'):html.index('id="settings-panel-downloads"')]
        metadata_panel = html[html.index('id="settings-panel-metadata"'):html.index('id="settings-panel-discovery"')]
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
        self.assertIn("正在验证地址、鉴权、协议与结构化输出…", html)
        self.assertIn("连接正常 · ${protocol} · ${data.elapsed_ms||0} ms", html)
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
        self.assertEqual(
            payload["__managed_fields"],
            [
                "AGENT_LIBRARY_PATROL_ENABLED",
                "AGENT_LLM_API_KEY",
                "AGENT_LLM_MODEL",
                "TAVILY_API_KEY",
            ],
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("deployment-secret", serialized)
        self.assertNotIn("llm-deployment-secret", serialized)

    def test_unchanged_agent_values_do_not_persist_or_restart_runtime(self):
        values = {
            "AGENT_ENABLED": "1",
            "AGENT_LLM_MODEL": "old-model",
        }
        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.routes.api.config.set_and_save") as persist, patch(
            "app.bot.restart_bot"
        ) as restart, patch("app.services.clear_dashboard_cache"):
            unchanged = save_config(self._request(), {"AGENT_ENABLED": "1"})
            changed = save_config(
                self._request(),
                {"AGENT_ENABLED": "1", "AGENT_LLM_MODEL": "new-model"},
            )

        self.assertEqual(unchanged, {"success": True})
        self.assertEqual(changed, {"success": True})
        persist.assert_called_once_with({"AGENT_LLM_MODEL": "new-model"})
        restart.assert_not_called()

    def test_agent_feature_gate_hot_toggle_controls_all_schedulers(self):
        request = self._request()
        request.app.state.background_services_enabled = True
        verification = MagicMock()
        patrol = MagicMock()
        durable = MagicMock()

        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": "1" if key == "AGENT_ENABLED" else default,
        ), patch("app.routes.api.config.set_and_save"), patch(
            "app.bot.restart_bot"
        ), patch("app.services.clear_dashboard_cache"), patch(
            "app.agent.feature_gate.is_agent_enabled", return_value=False
        ), patch(
            "app.modules.agent_download_verification_scheduler.get_download_library_verification_scheduler",
            return_value=verification,
        ), patch(
            "app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler",
            return_value=patrol,
        ), patch(
            "app.modules.agent_jobs_scheduler.get_agent_jobs_scheduler",
            return_value=durable,
        ):
            response = save_config(request, {"AGENT_ENABLED": "0"})

        self.assertEqual(response, {"success": True})
        for scheduler in (verification, patrol, durable):
            scheduler.stop.assert_called_once_with()
            scheduler.start.assert_not_called()

        verification.reset_mock()
        patrol.reset_mock()
        durable.reset_mock()
        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": "0" if key == "AGENT_ENABLED" else default,
        ), patch("app.routes.api.config.set_and_save"), patch(
            "app.bot.restart_bot"
        ), patch("app.services.clear_dashboard_cache"), patch(
            "app.agent.feature_gate.is_agent_enabled", return_value=True
        ), patch(
            "app.modules.agent_download_verification_scheduler.get_download_library_verification_scheduler",
            return_value=verification,
        ), patch(
            "app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler",
            return_value=patrol,
        ), patch(
            "app.modules.agent_jobs_scheduler.get_agent_jobs_scheduler",
            return_value=durable,
        ):
            response = save_config(request, {"AGENT_ENABLED": "1"})

        self.assertEqual(response, {"success": True})
        for scheduler in (verification, patrol, durable):
            scheduler.start.assert_called_once_with()
            scheduler.stop.assert_not_called()

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

    def test_model_test_auto_falls_back_and_requires_strict_probe_output(self):
        from app.routes import tools_api

        class FakeClient:
            instance = None

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                self.closed = False
                self.responses = [
                    SimpleNamespace(status_code=404, text="{}"),
                    SimpleNamespace(
                        status_code=200,
                        text=json.dumps({
                            "choices": [{"message": {"content": '{"ok": true}'}}]
                        }),
                    ),
                ]
                FakeClient.instance = self

            async def post_json(self, url, *, json, headers, max_redirects):
                self.calls.append((url, json, headers, max_redirects))
                return self.responses.pop(0)

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
        self.assertTrue(client.closed)
        self.assertEqual(
            [call[0] for call in client.calls],
            [
                "https://api.example.test/v1/responses",
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
        self.assertNotIn("provider-secret", json.dumps(chat_body))
        self.assertEqual(
            client.calls[1][2]["Authorization"], "Bearer provider-secret"
        )

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
