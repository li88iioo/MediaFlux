"""Media Agent 非敏感白名单策略的只读与确认写入测试。"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config
from app.agent import safe_policy_actions
from app.agent.llm_router import read_tool_capabilities
from app.agent.models import RiskLevel
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_safe_policy_mutation_candidate,
    is_safe_policy_summary_message,
    safe_policy_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.safe_policy_actions import (
    preview_set_safe_policy,
    safe_policy_arguments,
    safe_policy_confirmation_context,
    safe_policy_summary_arguments,
    summarize_safe_policies,
)
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


_POLICY_KEYS = {
    "TMDB_MATCH_MODE",
    "LOGIN_WALLPAPER_MODE",
    "TAVILY_SEARCH_DEPTH",
    "TAVILY_MAX_RESULTS",
    "TAVILY_CACHE_TTL_SECONDS",
    "TAVILY_DAILY_CREDIT_LIMIT",
    "TAVILY_TIMEOUT_SECONDS",
    "DISCOVERY_CACHE_TTL_SECONDS",
    "DISCOVERY_STALE_TTL_SECONDS",
    "DOUBAN_CACHE_TTL_SECONDS",
    "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS",
    "TMDB_API_KEY",
}


class SafePolicyUnitTests(unittest.TestCase):
    def test_arguments_are_strict_and_bounded(self):
        self.assertEqual(safe_policy_summary_arguments({}), {})
        self.assertEqual(
            safe_policy_arguments({"policy": "tmdb_match_mode", "value": " STRICT "}),
            {"policy": "tmdb_match_mode", "value": "strict"},
        )
        self.assertEqual(
            safe_policy_arguments({"policy": "web_search_max_results", "value": 10}),
            {"policy": "web_search_max_results", "value": 10},
        )
        self.assertEqual(
            safe_policy_arguments({"policy": "web_search_daily_credit_limit", "value": 100000}),
            {"policy": "web_search_daily_credit_limit", "value": 100000},
        )
        invalid = (
            {"extra": True},
            {},
            {"policy": "unknown", "value": "strict"},
            {"policy": "tmdb_match_mode", "value": "fast"},
            {"policy": "web_search_max_results", "value": True},
            {"policy": "web_search_max_results", "value": 0},
            {"policy": "web_search_timeout_seconds", "value": 31},
            {"policy": "web_search_cache_ttl_seconds", "value": 29},
            {"policy": "web_search_daily_credit_limit", "value": 100001},
            {"policy": "discovery_cache_ttl_seconds", "value": 59},
            {"policy": "discovery_stale_ttl_seconds", "value": 2592001},
            {"policy": "douban_cache_ttl_seconds", "value": 299},
            {"policy": "indexer_btbtla_min_interval_seconds", "value": -1},
            {"policy": "tmdb_match_mode", "value": "strict", "key": "TMDB_API_KEY"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                if arguments == {"extra": True}:
                    safe_policy_summary_arguments(arguments)
                else:
                    safe_policy_arguments(arguments)

    def test_natural_language_is_explicit_and_single_target(self):
        expected = {
            "把 TMDB 匹配模式改为严格": {"policy": "tmdb_match_mode", "value": "strict"},
            "把登录页壁纸改为默认": {"policy": "login_wallpaper_mode", "value": "default"},
            "把网页搜索深度改为高级": {"policy": "web_search_depth", "value": "advanced"},
            "网页搜索结果上限改为 8 条": {"policy": "web_search_max_results", "value": 8},
            "网页搜索超时改成 15 秒": {"policy": "web_search_timeout_seconds", "value": 15},
            "网页搜索缓存时间改为 15 分钟": {
                "policy": "web_search_cache_ttl_seconds", "value": 900,
            },
            "Tavily 每日额度改为 100000 次": {
                "policy": "web_search_daily_credit_limit", "value": 100000,
            },
            "媒体探索缓存时间改为 6 小时": {
                "policy": "discovery_cache_ttl_seconds", "value": 21600,
            },
            "探索旧缓存保留时间改为 7 天": None,
            "探索旧缓存保留时间改为 168 小时": {
                "policy": "discovery_stale_ttl_seconds", "value": 604800,
            },
            "豆瓣探索缓存时间改为 30 分钟": {
                "policy": "douban_cache_ttl_seconds", "value": 1800,
            },
            "BTBTLA 请求间隔设为 8 秒": {
                "policy": "indexer_btbtla_min_interval_seconds",
                "value": 8,
            },
        }
        for message, arguments in expected.items():
            with self.subTest(message=message):
                self.assertEqual(safe_policy_request(message), arguments)
        for message in (
            "不要把网页搜索超时改成 15 秒",
            "网页搜索超时能否改成 15 秒",
            "把网页搜索超时改成 15 分钟",
            "把网页搜索超时改成 15 秒并把结果上限改成 8 条",
        ):
            with self.subTest(message=message):
                self.assertIsNone(safe_policy_request(message))
        self.assertTrue(is_safe_policy_summary_message("查看当前安全策略"))
        self.assertFalse(is_safe_policy_summary_message("把网页搜索深度改为高级"))
        self.assertTrue(is_safe_policy_mutation_candidate("把网页搜索超时改成 15 分钟"))

    def test_registry_and_routes_keep_write_confirmation_gated(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(
            capabilities["config.safe_policy_summary"]["risk"], RiskLevel.READ.value
        )
        self.assertEqual(
            capabilities["config.set_safe_policy"]["risk"], RiskLevel.LOW_WRITE.value
        )
        self.assertTrue(capabilities["config.set_safe_policy"]["requires_confirmation"])
        with self.assertRaisesRegex(AgentToolError, "需要确认"):
            registry.execute(
                "config.set_safe_policy",
                {"policy": "web_search_timeout_seconds", "value": 15},
            )
        self.assertIn(
            "config.safe_policy_summary",
            {item["name"] for item in read_tool_capabilities(registry)},
        )
        agent = AgentOrchestrator(registry)
        self.assertEqual(
            agent.query("查看当前安全策略")["tool_call"]["name"],
            "config.safe_policy_summary",
        )
        self.assertEqual(
            agent.query("把网页搜索超时改为 15 秒")["result"]["status"],
            "unsupported",
        )
        unclear = agent.query("把网页搜索超时改为 15 分钟")
        self.assertEqual(unclear["result"]["status"], "clarification_required")

    def test_summary_and_preview_never_expose_unrelated_config(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            secret = "safe-policy-secret-must-not-leak"
            config.write_env_file(
                env_file,
                {
                    "TMDB_MATCH_MODE": "strict",
                    "LOGIN_WALLPAPER_MODE": "default",
                    "TAVILY_SEARCH_DEPTH": "basic",
                    "TAVILY_MAX_RESULTS": "5",
                    "TAVILY_CACHE_TTL_SECONDS": "900",
                    "TAVILY_DAILY_CREDIT_LIMIT": "100",
                    "TAVILY_TIMEOUT_SECONDS": "10",
                    "DISCOVERY_CACHE_TTL_SECONDS": "21600",
                    "DISCOVERY_STALE_TTL_SECONDS": "604800",
                    "DOUBAN_CACHE_TTL_SECONDS": "21600",
                    "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS": "5",
                    "TMDB_API_KEY": secret,
                },
                replace=False,
            )
            with patch.object(config, "ENV_FILE", env_file), patch.object(
                config, "_cache", None
            ), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()):
                summary = summarize_safe_policies({})
                preview = preview_set_safe_policy(
                    {"policy": "web_search_timeout_seconds", "value": 15}
                )
                context = safe_policy_confirmation_context(
                    {"policy": "web_search_timeout_seconds", "value": 15}
                )
        rendered = repr((summary.to_dict(), preview.to_dict(), context))
        self.assertTrue(summary.ok)
        self.assertEqual(summary.data["policy_count"], 11)
        self.assertTrue(preview.ok)
        self.assertRegex(context, r"^[0-9a-f]{64}$")
        self.assertNotIn(secret, rendered)
        self.assertNotIn("TMDB_API_KEY", rendered)
        self.assertNotIn(str(env_file), rendered)

    def test_discovery_cache_ttls_keep_safe_ordering(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            config.write_env_file(
                env_file,
                {
                    "DISCOVERY_CACHE_TTL_SECONDS": "21600",
                    "DISCOVERY_STALE_TTL_SECONDS": "604800",
                },
                replace=False,
            )
            with patch.object(config, "ENV_FILE", env_file), patch.object(
                config, "_cache", None
            ), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()):
                too_long = preview_set_safe_policy(
                    {"policy": "discovery_cache_ttl_seconds", "value": 604801}
                )
                too_short = preview_set_safe_policy(
                    {"policy": "discovery_stale_ttl_seconds", "value": 21599}
                )
                valid = preview_set_safe_policy(
                    {"policy": "discovery_stale_ttl_seconds", "value": 86400}
                )
        self.assertFalse(too_long.ok)
        self.assertEqual(too_long.status, "precondition_failed")
        self.assertFalse(too_short.ok)
        self.assertEqual(too_short.status, "precondition_failed")
        self.assertTrue(valid.ok)


class SafePolicyAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.temp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp.name) / "user.env"
        self.previous_env = {key: os.environ.get(key) for key in _POLICY_KEYS}
        for key in _POLICY_KEYS:
            os.environ.pop(key, None)
        config.write_env_file(
            self.env_file,
            {
                "TMDB_MATCH_MODE": "strict",
                "LOGIN_WALLPAPER_MODE": "default",
                "TAVILY_SEARCH_DEPTH": "basic",
                "TAVILY_MAX_RESULTS": "5",
                "TAVILY_CACHE_TTL_SECONDS": "900",
                "TAVILY_DAILY_CREDIT_LIMIT": "100",
                "TAVILY_TIMEOUT_SECONDS": "10",
                "DISCOVERY_CACHE_TTL_SECONDS": "21600",
                "DISCOVERY_STALE_TTL_SECONDS": "604800",
                "DOUBAN_CACHE_TTL_SECONDS": "21600",
                "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS": "5",
                "TMDB_API_KEY": "tmdb-secret-must-not-leak",
            },
            replace=False,
        )
        self.env_patch = patch.object(config, "ENV_FILE", self.env_file)
        self.cache_patch = patch.object(config, "_cache", None)
        self.override_patch = patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset())
        self.env_patch.start()
        self.cache_patch.start()
        self.override_patch.start()
        self.client = TestClient(
            create_app(start_background=False),
            raise_server_exceptions=False,
        )

    def tearDown(self):
        self.client.close()
        self.override_patch.stop()
        self.cache_patch.stop()
        self.env_patch.stop()
        for key in _POLICY_KEYS:
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

    def test_query_prepare_confirm_replay_and_stale_snapshot(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        prepared = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "把网页搜索超时改为 15 秒"},
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        body = prepared.json()
        self.assertEqual(body["mode"], "confirmation_required")
        self.assertEqual(body["confirmation"]["tool"], "config.set_safe_policy")
        self.assertEqual(config._read_env_file(self.env_file)["TAVILY_TIMEOUT_SECONDS"], "10")

        confirmed = self.client.post(
            "/api/agent/actions/confirm",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "confirmation_id": body["confirmation"]["confirmation_id"]},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        result = confirmed.json()["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["policy"], "web_search_timeout_seconds")
        self.assertEqual(config._read_env_file(self.env_file)["TAVILY_TIMEOUT_SECONDS"], "15")
        self.assertNotIn("tmdb-secret-must-not-leak", confirmed.text)

        replay = self.client.post(
            "/api/agent/actions/confirm",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "confirmation_id": body["confirmation"]["confirmation_id"]},
        )
        self.assertEqual(replay.status_code, 409, replay.text)

        agent_rate_limiter.reset()
        prepared = self.client.post(
            "/api/agent/actions/config.set_safe_policy/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"policy": "web_search_timeout_seconds", "value": 20}},
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        values = config._read_env_file(self.env_file)
        values["UNRELATED_SETTING"] = "changed"
        config.write_env_file(self.env_file, values, replace=True)
        stale = self.client.post(
            "/api/agent/actions/confirm",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "confirmation_id": prepared.json()["confirmation"]["confirmation_id"]},
        )
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_runtime_refresh_failure_is_deferred_success(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        prepared = self.client.post(
            "/api/agent/actions/config.set_safe_policy/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"policy": "web_search_max_results", "value": 8}},
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        with patch.dict(
            safe_policy_actions._RUNTIME_REFRESHERS,
            {"web_search_max_results": lambda: False},
        ):
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": prepared.json()["confirmation"]["confirmation_id"]},
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        result = confirmed.json()["result"]
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["runtime_refreshed"])
        self.assertIn("重启服务", " ".join(result["suggestions"]))


if __name__ == "__main__":
    unittest.main()
