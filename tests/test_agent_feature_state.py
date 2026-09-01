"""Media Agent 非敏感功能开关确认动作测试。"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config
from app.agent.feature_actions import (
    feature_state_arguments,
    prepare_feature_state_confirmation,
)
from app.agent.models import RiskLevel
from app.agent.orchestrator import (
    AgentOrchestrator,
    feature_state_followup_request,
    feature_state_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class FeatureStateUnitTests(unittest.TestCase):
    def test_arguments_are_strict_aliases_and_boolean_only(self):
        self.assertEqual(
            feature_state_arguments({"feature": "discovery", "enabled": False}),
            {"feature": "discovery", "enabled": False},
        )
        self.assertEqual(
            feature_state_arguments({"feature": "web_search", "enabled": True}),
            {"feature": "web_search", "enabled": True},
        )
        for arguments in (
            {},
            {"feature": "DISCOVERY_ENABLED", "enabled": True},
            {"feature": " Discovery ", "enabled": False},
            {"feature": " WEB_SEARCH ", "enabled": True},
            {"feature": "discovery", "enabled": 1},
            {"feature": "discovery", "enabled": "false"},
            {"feature": "discovery", "enabled": False, "key": "TMDB_API_KEY"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                feature_state_arguments(arguments)

    def test_natural_language_requires_explicit_action_and_selects_specific_feature(self):
        self.assertEqual(
            feature_state_request("请关闭媒体探索"),
            {"feature": "discovery", "enabled": False},
        )
        self.assertEqual(
            feature_state_request("把探索页站点资源结果开启"),
            {"feature": "resource_results", "enabled": True},
        )
        self.assertEqual(
            feature_state_request("停用豆瓣探索"),
            {"feature": "douban", "enabled": False},
        )
        self.assertEqual(
            feature_state_request("启用多站资源搜索"),
            {"feature": "indexer_search", "enabled": True},
        )
        self.assertEqual(
            feature_state_request("开启联网搜索"),
            {"feature": "web_search", "enabled": True},
        )
        self.assertEqual(
            feature_state_request("把 Tavily 搜索关闭"),
            {"feature": "web_search", "enabled": False},
        )
        self.assertEqual(
            feature_state_request("打开多站资源索引"),
            {"feature": "indexer_search", "enabled": True},
        )
        self.assertEqual(
            feature_state_request("把多站资源索引关掉"),
            {"feature": "indexer_search", "enabled": False},
        )
        self.assertEqual(
            feature_state_request("关闭光鸭磁力链接离线转存"),
            {"feature": "offline_magnet", "enabled": False},
        )
        self.assertEqual(
            feature_state_request("关闭 ED2K 离线转存"),
            {"feature": "offline_ed2k", "enabled": False},
        )
        self.assertEqual(
            feature_state_request("打开光鸭 HTTP 离线转存"),
            {"feature": "offline_http", "enabled": True},
        )
        self.assertEqual(
            feature_state_request("关闭 STRM 元数据同步"),
            {"feature": "strm_metadata", "enabled": False},
        )
        self.assertEqual(
            feature_state_request("关闭下载后入库复核通知"),
            {"feature": "download_verification_notify", "enabled": False},
        )
        self.assertIsNone(feature_state_request("媒体探索现在是什么状态"))
        self.assertIsNone(feature_state_request("媒体探索为什么没打开"))
        for message in (
            "我不想关闭媒体探索",
            "不要关闭媒体探索",
            "如果关闭媒体探索会怎样",
            "关闭媒体探索后还能搜索吗",
            "开启媒体探索有什么风险",
            "打开探索页看看",
            "关闭探索页",
            "关闭当前探索页",
            "关闭媒体探索并启用豆瓣探索",
        ):
            with self.subTest(message=message):
                self.assertIsNone(feature_state_request(message))

    def test_pronoun_followup_uses_only_the_latest_single_feature_topic(self):
        context = [
            {"role": "user", "text": "多站资源索引现在是关闭的吗"},
            {"role": "assistant", "text": "当前处于关闭状态"},
        ]
        self.assertEqual(
            feature_state_followup_request("打开它", context),
            {"feature": "indexer_search", "enabled": True},
        )
        original_get_bool = config.get_bool

        def get_bool_with_disabled_indexer(key: str, default: bool = False) -> bool:
            if key == "INDEXER_SEARCH_ENABLED":
                return False
            return original_get_bool(key, default)

        with patch(
            "app.agent.feature_actions.config.get_bool",
            side_effect=get_bool_with_disabled_indexer,
        ):
            response = AgentOrchestrator(build_tool_registry()).query(
                "打开它",
                owner="owner",
                conversation_context=context,
            )
        self.assertEqual(response["mode"], "confirmation_required")
        self.assertEqual(response["tool_call"]["name"], "config.set_feature_state")
        self.assertEqual(response["result"]["data"]["feature"], "indexer_search")

        correction_context = [
            {"role": "user", "text": "把多站资源索引关闭"},
            {"role": "assistant", "text": "准备关闭多站资源索引"},
        ]
        self.assertEqual(
            feature_state_followup_request("说反了，还是打开", correction_context),
            {"feature": "indexer_search", "enabled": True},
        )
        with patch(
            "app.agent.feature_actions.config.get_bool",
            side_effect=get_bool_with_disabled_indexer,
        ):
            corrected = AgentOrchestrator(build_tool_registry()).query(
                "说反了，还是打开",
                owner="owner",
                conversation_context=correction_context,
            )
        self.assertEqual(corrected["mode"], "confirmation_required")
        self.assertEqual(corrected["tool_call"]["name"], "config.set_feature_state")
        self.assertTrue(corrected["result"]["data"]["requested_enabled"])

        for unsafe_correction in ("别关", "不要关闭", "说反了，还是打开吗？"):
            with self.subTest(unsafe_correction=unsafe_correction):
                self.assertIsNone(
                    feature_state_followup_request(unsafe_correction, correction_context)
                )

        ambiguous_context = [
            {"role": "user", "text": "媒体探索和豆瓣探索分别是什么状态"},
        ]
        self.assertIsNone(
            feature_state_followup_request("关闭它", ambiguous_context)
        )
        ambiguous = AgentOrchestrator(build_tool_registry()).query(
            "关闭它",
            owner="owner",
            conversation_context=ambiguous_context,
        )
        self.assertEqual(ambiguous["mode"], "clarification")
        self.assertIsNone(ambiguous["tool_call"])

        stale_context = [
            {"role": "user", "text": "媒体探索现在是关闭的吗"},
            {"role": "assistant", "text": "当前处于关闭状态"},
            {"role": "user", "text": "下载队列现在有多少任务"},
        ]
        self.assertIsNone(feature_state_followup_request("打开它", stale_context))
        stale = AgentOrchestrator(build_tool_registry()).query(
            "打开它",
            owner="owner",
            conversation_context=stale_context,
        )
        self.assertEqual(stale["mode"], "clarification")
        self.assertIsNone(stale["tool_call"])

    def test_strm_metadata_action_is_not_stolen_by_generic_strm_diagnosis(self):
        original_get_bool = config.get_bool

        def get_bool_with_metadata_enabled(key: str, default: bool = False) -> bool:
            if key == "STRM_METADATA_ENABLED":
                return True
            return original_get_bool(key, default)

        with patch(
            "app.agent.feature_actions.config.get_bool",
            side_effect=get_bool_with_metadata_enabled,
        ):
            response = AgentOrchestrator(build_tool_registry()).query(
                "关闭 STRM 元数据同步", owner="owner"
            )
        self.assertEqual(response["mode"], "confirmation_required")
        self.assertEqual(response["tool_call"]["name"], "config.set_feature_state")
        self.assertEqual(response["result"]["data"]["feature"], "strm_metadata")
        self.assertFalse(response["result"]["data"]["requested_enabled"])

    def test_registry_exposes_low_write_confirmation_gate(self):
        registry = build_tool_registry()
        capability = next(
            item for item in registry.capabilities()
            if item["name"] == "config.set_feature_state"
        )
        self.assertEqual(capability["risk"], RiskLevel.LOW_WRITE.value)
        self.assertTrue(capability["requires_confirmation"])
        with self.assertRaisesRegex(AgentToolError, "需要确认"):
            registry.execute(
                "config.set_feature_state",
                {"feature": "discovery", "enabled": False},
            )

    def test_preview_and_context_do_not_leak_file_contents(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            secret = "super-secret-token"
            config.write_env_file(
                env_file,
                {
                    "DISCOVERY_ENABLED": "1",
                    "TMDB_API_KEY": secret,
                },
                replace=False,
            )
            with patch.object(config, "ENV_FILE", env_file), patch.object(
                config, "_cache", None
            ), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()), patch.dict(
                os.environ, {"DISCOVERY_ENABLED": ""}, clear=False
            ), patch("app.agent.feature_actions.config.update_runtime_env_file") as writer:
                result, first = prepare_feature_state_confirmation(
                    {"feature": "discovery", "enabled": False}
                )
                first_preview = result
                second_preview, second = prepare_feature_state_confirmation(
                    {"feature": "discovery", "enabled": False}
                )

            self.assertTrue(result.ok)
            self.assertTrue(first_preview.ok)
            self.assertTrue(second_preview.ok)
            self.assertEqual(result.status, "confirmation_required")
            self.assertEqual(first, second)
            self.assertRegex(first, r"^[0-9a-f]{64}$")
            self.assertNotIn(secret, str(result.to_dict()))
            self.assertNotIn(secret, first)
            self.assertNotIn("DISCOVERY_ENABLED", str(result.to_dict()))
            writer.assert_not_called()


class FeatureStateAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.temp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp.name) / "user.env"
        self.feature_keys = {
            "DISCOVERY_ENABLED",
            "DISCOVERY_DOUBAN_ENABLED",
            "DISCOVERY_RESOURCE_RESULTS_ENABLED",
            "INDEXER_SEARCH_ENABLED",
            "INDEXER_ENABLED_SITES",
            "INDEXER_SUKEBEI_ENABLED",
            "WEB_SEARCH_ENABLED",
            "TAVILY_API_KEY",
            "OFFLINE_MAGNET_ENABLED",
            "OFFLINE_ED2K_ENABLED",
            "OFFLINE_HTTP_ENABLED",
            "STRM_METADATA_ENABLED",
            "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED",
        }
        self.previous_env = {key: os.environ.get(key) for key in self.feature_keys}
        for key in self.feature_keys:
            os.environ.pop(key, None)
        config.write_env_file(
            self.env_file,
            {
                "DISCOVERY_ENABLED": "1",
                "DISCOVERY_DOUBAN_ENABLED": "1",
                "DISCOVERY_RESOURCE_RESULTS_ENABLED": "1",
                "INDEXER_SEARCH_ENABLED": "1",
                "INDEXER_ENABLED_SITES": "nyaa,mikan",
                "INDEXER_SUKEBEI_ENABLED": "0",
                "WEB_SEARCH_ENABLED": "0",
                "TAVILY_API_KEY": "tavily-secret-must-not-leak",
                "OFFLINE_MAGNET_ENABLED": "1",
                "OFFLINE_ED2K_ENABLED": "1",
                "OFFLINE_HTTP_ENABLED": "0",
                "STRM_METADATA_ENABLED": "0",
                "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED": "1",
                "TMDB_API_KEY": "must-not-leak",
            },
            replace=False,
        )
        self.env_patch = patch.object(config, "ENV_FILE", self.env_file)
        self.cache_patch = patch.object(config, "_cache", None)
        self.override_patch = patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset())
        self.env_patch.start()
        self.cache_patch.start()
        self.override_patch.start()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.override_patch.stop()
        self.cache_patch.stop()
        self.env_patch.stop()
        for key in self.feature_keys:
            os.environ.pop(key, None)
        for key, value in self.previous_env.items():
            if value is not None:
                os.environ[key] = value
        self.temp.cleanup()
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def _prepare_feature(self, csrf: str, feature: str, enabled: bool):
        prepared = self.client.post(
            "/api/agent/actions/config.set_feature_state/prepare",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "arguments": {"feature": feature, "enabled": enabled}},
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        return prepared.json()

    def _prepare_and_confirm(self, csrf: str, feature: str, enabled: bool):
        prepared = self._prepare_feature(csrf, feature, enabled)
        confirmed = self.client.post(
            "/api/agent/actions/confirm",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "plan_id": prepared["action_plan"]["plan_id"]},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        return prepared["result"], confirmed.json()["result"]

    def test_query_prepare_confirm_replay_and_runtime_refresh(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        with patch("app.discovery.service.shutdown_discovery_service") as stop_service, patch(
            "app.discovery.search.shutdown_discovery_search_service"
        ) as stop_search:
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "请关闭媒体探索"},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            body = prepared.json()
            self.assertEqual(body["mode"], "confirmation_required")
            self.assertEqual(body["tool_call"]["name"], "config.set_feature_state")
            self.assertEqual(body["action_plan"]["risk"], "low_write")
            confirmation_id = body["action_plan"]["plan_id"]
            self.assertEqual(config._read_env_file(self.env_file)["DISCOVERY_ENABLED"], "1")

            direct = self.client.post(
                "/api/agent/tools/config.set_feature_state",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"feature": "discovery", "enabled": False}},
            )
            self.assertEqual(direct.status_code, 409, direct.text)

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertEqual(confirmed.json()["result"]["status"], "completed")
            self.assertFalse(confirmed.json()["result"]["data"]["enabled"])
            self.assertEqual(
                confirmed.json()["result"]["data"]["verification_state"],
                "verified",
            )
            self.assertEqual(
                confirmed.json()["result"]["data"]["runtime_scope"],
                "current_process",
            )
            self.assertEqual(config._read_env_file(self.env_file)["DISCOVERY_ENABLED"], "0")
            self.assertFalse(config.get_bool("DISCOVERY_ENABLED", True))
            stop_service.assert_called_once_with()
            stop_search.assert_called_once_with()

            replay = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )
            self.assertEqual(replay.status_code, 409, replay.text)

        combined = prepared.text + direct.text + confirmed.text + replay.text
        self.assertNotIn("must-not-leak", combined)
        self.assertNotIn("DISCOVERY_ENABLED", combined)

    def test_douban_refreshes_runtime_but_indexer_display_toggles_do_not(self):
        csrf = self.login()
        with patch("app.discovery.service.shutdown_discovery_service") as stop_service, patch(
            "app.discovery.search.shutdown_discovery_search_service"
        ) as stop_search:
            _, douban = self._prepare_and_confirm(csrf, "douban", False)
            self.assertTrue(douban["data"]["runtime_refreshed"])
            stop_service.assert_called_once_with()
            stop_search.assert_called_once_with()

            _, resource_results = self._prepare_and_confirm(
                csrf, "resource_results", False
            )
            _, indexer_search = self._prepare_and_confirm(csrf, "indexer_search", False)
            self.assertTrue(resource_results["data"]["runtime_refreshed"])
            self.assertTrue(indexer_search["data"]["runtime_refreshed"])
            self.assertEqual(resource_results["data"]["runtime_scope"], "current_process")
            self.assertEqual(indexer_search["data"]["runtime_scope"], "current_process")
            self.assertFalse(any("多 worker" in item for item in resource_results["suggestions"]))
            self.assertFalse(any("多 worker" in item for item in indexer_search["suggestions"]))
            stop_service.assert_called_once_with()
            stop_search.assert_called_once_with()

        values = config._read_env_file(self.env_file)
        self.assertEqual(values["DISCOVERY_DOUBAN_ENABLED"], "0")
        self.assertEqual(values["DISCOVERY_RESOURCE_RESULTS_ENABLED"], "0")
        self.assertEqual(values["INDEXER_SEARCH_ENABLED"], "0")

    def test_other_page_feature_toggles_are_confirmed_without_starting_jobs(self):
        csrf = self.login()
        requests = (
            ("offline_magnet", False, "历史消息"),
            ("offline_ed2k", False, "历史消息"),
            ("offline_http", True, "历史消息"),
            ("strm_metadata", True, "不会立即启动同步任务"),
            ("download_verification_notify", False, "不会启动、暂停或删除下载任务"),
        )
        prepared_results = []
        confirmed_results = []
        with patch("app.discovery.service.shutdown_discovery_service") as stop_service, patch(
            "app.discovery.search.shutdown_discovery_search_service"
        ) as stop_search:
            for feature, enabled, effect_fragment in requests:
                agent_rate_limiter.reset()
                prepared, confirmed = self._prepare_and_confirm(csrf, feature, enabled)
                prepared_results.append((prepared, effect_fragment))
                confirmed_results.append(confirmed)

        for prepared, effect_fragment in prepared_results:
            self.assertTrue(any(
                effect_fragment in effect
                for effect in prepared["data"]["effects"]
            ))
        for result in confirmed_results:
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["runtime_scope"], "current_process")
        stop_service.assert_not_called()
        stop_search.assert_not_called()

        values = config._read_env_file(self.env_file)
        self.assertEqual(values["OFFLINE_MAGNET_ENABLED"], "0")
        self.assertEqual(values["OFFLINE_ED2K_ENABLED"], "0")
        self.assertEqual(values["OFFLINE_HTTP_ENABLED"], "1")
        self.assertEqual(values["STRM_METADATA_ENABLED"], "1")
        self.assertEqual(values["AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED"], "0")

    def test_web_search_toggle_requires_provider_and_never_calls_it_during_write(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        with patch("app.agent.web_search_actions._search_tavily") as provider:
            prepared = self.client.post(
                "/api/agent/actions/config.set_feature_state/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"feature": "web_search", "enabled": True}},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            body = prepared.json()
            self.assertNotIn("tavily-secret-must-not-leak", prepared.text)
            self.assertTrue(any(
                "不会访问 Tavily" in effect
                for effect in body["result"]["data"]["effects"]
            ))
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": body["action_plan"]["plan_id"]},
            )

        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["result"]["data"]["feature"], "web_search")
        self.assertTrue(config.get_bool("WEB_SEARCH_ENABLED", False))
        provider.assert_not_called()

        values = config._read_env_file(self.env_file)
        values["WEB_SEARCH_ENABLED"] = "0"
        values["TAVILY_API_KEY"] = ""
        config.write_env_file(self.env_file, values, replace=True)
        os.environ.pop("WEB_SEARCH_ENABLED", None)
        config._cache = None
        missing = self.client.post(
            "/api/agent/actions/config.set_feature_state/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"feature": "web_search", "enabled": True}},
        )
        self.assertEqual(missing.status_code, 409, missing.text)
        self.assertIn("联网搜索供应商", missing.text)
        self.assertNotIn("TAVILY_API_KEY", missing.text)

    def test_runtime_refresh_failure_keeps_saved_config_and_reports_deferred_refresh(self):
        csrf = self.login()
        with patch(
            "app.discovery.service.shutdown_discovery_service",
            side_effect=RuntimeError("simulated refresh failure"),
        ), patch("app.discovery.search.shutdown_discovery_search_service") as stop_search:
            _, result = self._prepare_and_confirm(csrf, "discovery", False)

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["data"]["runtime_refreshed"])
        self.assertTrue(result["suggestions"])
        self.assertEqual(config._read_env_file(self.env_file)["DISCOVERY_ENABLED"], "0")
        self.assertFalse(config.get_bool("DISCOVERY_ENABLED", True))
        stop_search.assert_not_called()

    def test_second_runtime_shutdown_failure_reports_partial_refresh(self):
        csrf = self.login()
        with patch("app.discovery.service.shutdown_discovery_service") as stop_service, patch(
            "app.discovery.search.shutdown_discovery_search_service",
            side_effect=RuntimeError("simulated search refresh failure"),
        ) as stop_search:
            _, result = self._prepare_and_confirm(csrf, "douban", False)

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["data"]["runtime_refreshed"])
        self.assertEqual(result["data"]["runtime_scope"], "current_process")
        self.assertTrue(result["suggestions"])
        self.assertEqual(config._read_env_file(self.env_file)["DISCOVERY_DOUBAN_ENABLED"], "0")
        stop_service.assert_called_once_with()
        stop_search.assert_called_once_with()

    def test_preview_explains_dependencies_and_inflight_request_limit(self):
        values = config._read_env_file(self.env_file)
        values["DISCOVERY_ENABLED"] = "0"
        values["INDEXER_SEARCH_ENABLED"] = "0"
        values["DISCOVERY_RESOURCE_RESULTS_ENABLED"] = "0"
        config.write_env_file(self.env_file, values, replace=True)
        config._cache = None

        enabled, _context = prepare_feature_state_confirmation(
            {"feature": "resource_results", "enabled": True}
        )
        self.assertTrue(enabled.ok)
        self.assertTrue(any("媒体探索总开关" in item for item in enabled.suggestions))
        self.assertTrue(any("多站资源搜索" in item for item in enabled.suggestions))

        values["DISCOVERY_RESOURCE_RESULTS_ENABLED"] = "1"
        config.write_env_file(self.env_file, values, replace=True)
        config._cache = None
        disabled, _context = prepare_feature_state_confirmation(
            {"feature": "resource_results", "enabled": False}
        )
        self.assertTrue(any("不会取消已经发出的外部请求" in item for item in disabled.suggestions))

    def test_confirm_write_failures_do_not_refresh_runtime_or_leak_config(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        original = self.env_file.read_bytes()
        cases = (
            (config.ConcurrentConfigUpdateError("simulated conflict"), 409, "conflict"),
            (config.AtomicPublishError("simulated publish failure"), 503, "unavailable"),
        )
        for error, expected_status, expected_result_status in cases:
            with self.subTest(error=type(error).__name__):
                prepared = self.client.post(
                    "/api/agent/actions/config.set_feature_state/prepare",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "arguments": {"feature": "discovery", "enabled": False}},
                )
                self.assertEqual(prepared.status_code, 200, prepared.text)
                confirmation_id = prepared.json()["action_plan"]["plan_id"]
                with patch(
                    "app.agent.feature_actions.config.update_runtime_env_file",
                    side_effect=error,
                ), patch(
                    "app.discovery.service.shutdown_discovery_service"
                ) as stop_service, patch(
                    "app.discovery.search.shutdown_discovery_search_service"
                ) as stop_search:
                    confirmed = self.client.post(
                        "/api/agent/actions/confirm",
                        headers=headers,
                        json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
                    )

                self.assertEqual(confirmed.status_code, expected_status, confirmed.text)
                self.assertEqual(confirmed.json()["result"]["status"], expected_result_status)
                self.assertEqual(self.env_file.read_bytes(), original)
                self.assertNotIn("must-not-leak", confirmed.text)
                self.assertNotIn("TMDB_API_KEY", confirmed.text)
                stop_service.assert_not_called()
                stop_search.assert_not_called()

    def test_prepare_rejects_extra_fields_noop_missing_sites_and_external_override(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        invalid = self.client.post(
            "/api/agent/actions/config.set_feature_state/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"feature": "discovery", "enabled": False, "key": "X"}},
        )
        self.assertEqual(invalid.status_code, 400, invalid.text)

        noop = self.client.post(
            "/api/agent/actions/config.set_feature_state/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"feature": "discovery", "enabled": True}},
        )
        self.assertEqual(noop.status_code, 409, noop.text)

        values = config._read_env_file(self.env_file)
        values["INDEXER_SEARCH_ENABLED"] = "0"
        values["INDEXER_ENABLED_SITES"] = ""
        config.write_env_file(self.env_file, values, replace=True)
        config._cache = None
        missing_sites = self.client.post(
            "/api/agent/actions/config.set_feature_state/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"feature": "indexer_search", "enabled": True}},
        )
        self.assertEqual(missing_sites.status_code, 409, missing_sites.text)
        self.assertIn("资源站点", missing_sites.text)

        config._STARTUP_ENV_OVERRIDES = frozenset({"DISCOVERY_ENABLED"})
        os.environ["DISCOVERY_ENABLED"] = "1"
        overridden = self.client.post(
            "/api/agent/actions/config.set_feature_state/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"feature": "discovery", "enabled": False}},
        )
        self.assertEqual(overridden.status_code, 409, overridden.text)
        self.assertIn("运行环境", overridden.text)

    def test_confirm_rejects_stale_snapshot_without_writing(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        prepared = self.client.post(
            "/api/agent/actions/config.set_feature_state/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"feature": "douban", "enabled": False}},
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        confirmation_id = prepared.json()["action_plan"]["plan_id"]
        values = config._read_env_file(self.env_file)
        values["UNRELATED_SETTING"] = "changed"
        config.write_env_file(self.env_file, values, replace=True)
        stale = self.client.post(
            "/api/agent/actions/confirm",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn("配置已变化", stale.text)
        self.assertEqual(config._read_env_file(self.env_file)["DISCOVERY_DOUBAN_ENABLED"], "1")

    def test_confirm_requires_csrf(self):
        self.login()
        response = self.client.post(
            "/api/agent/actions/confirm",
            json={"session_id": "test_session_identifier_0001", "plan_id": "x" * 24},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
