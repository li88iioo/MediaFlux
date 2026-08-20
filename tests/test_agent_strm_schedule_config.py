"""Media Agent STRM 定时同步策略受控配置测试。"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import config
from app.agent import strm_schedule_config_actions
from app.agent.models import RiskLevel
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_strm_schedule_policy_summary_message,
    strm_schedule_policy_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import reset_agent_service_for_tests
from app.agent.strm_schedule_config_actions import (
    preview_set_strm_schedule_policy,
    strm_schedule_policy_arguments,
    strm_schedule_policy_confirmation_context,
    summarize_strm_schedule_policy,
)
from app.agent.tools import build_tool_registry
from app.main import create_app
from app.modules.scheduler import STRMScheduler
from tests.support import IsolatedDatabaseTestCase

_STRM_KEYS = {
    "STRM_SCHEDULE_ENABLED",
    "STRM_SCHEDULE_CRON",
    "STRM_NOTIFY_ENABLED",
}


class StrmSchedulePolicyUnitTests(unittest.TestCase):
    def test_arguments_are_partial_bounded_and_strict(self):
        self.assertEqual(
            strm_schedule_policy_arguments({
                "enabled": True,
                "cron": "  0   4 * * * ",
                "notify_enabled": False,
            }),
            {"enabled": True, "cron": "0 4 * * *", "notify_enabled": False},
        )
        for arguments in (
            {},
            {"enabled": 1},
            {"notify_enabled": "true"},
            {"cron": 4},
            {"cron": ""},
            {"cron": "0 4 * *"},
            {"cron": "0 4 * * * *"},
            {"key": "STRM_SCHEDULE_ENABLED"},
            {"token": "secret"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                strm_schedule_policy_arguments(arguments)

    def test_natural_language_is_explicit(self):
        self.assertEqual(
            strm_schedule_policy_request("启用 STRM 定时同步"),
            {"enabled": True},
        )
        self.assertEqual(
            strm_schedule_policy_request("关闭 STRM 任务通知"),
            {"notify_enabled": False},
        )
        self.assertEqual(
            strm_schedule_policy_request("将 STRM 定时同步改为每天 4 点"),
            {"cron": "0 4 * * *"},
        )
        self.assertEqual(
            strm_schedule_policy_request("将 STRM 调度表达式设为 15 3 * * 1"),
            {"cron": "15 3 * * 1"},
        )
        for message in (
            "不要关闭 STRM 定时同步",
            "能否开启 STRM 定时同步",
            "STRM 定时同步怎么配置",
            "查看 STRM 同步状态",
        ):
            self.assertIsNone(strm_schedule_policy_request(message))
        self.assertTrue(is_strm_schedule_policy_summary_message("查看 STRM 调度策略"))
        self.assertFalse(is_strm_schedule_policy_summary_message("查看 STRM 同步状态"))

    def test_registry_summary_and_ownerless_route(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(capabilities["strm.schedule_policy"]["risk"], RiskLevel.READ.value)
        self.assertEqual(
            capabilities["strm.set_schedule_policy"]["risk"], RiskLevel.LOW_WRITE.value
        )
        self.assertTrue(capabilities["strm.set_schedule_policy"]["requires_confirmation"])
        with self.assertRaisesRegex(AgentToolError, "需要确认"):
            registry.execute("strm.set_schedule_policy", {"enabled": True})
        agent = AgentOrchestrator(registry)
        self.assertEqual(
            agent.query("查看 STRM 调度策略")["tool_call"]["name"],
            "strm.schedule_policy",
        )
        self.assertEqual(
            agent.query("启用 STRM 定时同步")["result"]["status"],
            "unsupported",
        )

    def test_summary_preview_context_do_not_leak_other_config(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            secret = "strm-secret-must-not-leak"
            config.write_env_file(env_file, {
                "STRM_SCHEDULE_ENABLED": "0",
                "STRM_SCHEDULE_CRON": "0 4 * * *",
                "STRM_NOTIFY_ENABLED": "1",
                "GY_TOKEN": secret,
            }, replace=False)
            with patch.object(config, "ENV_FILE", env_file), patch.object(
                config, "_cache", None
            ), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()):
                summary = summarize_strm_schedule_policy({})
                preview = preview_set_strm_schedule_policy({"enabled": True})
                context = strm_schedule_policy_confirmation_context({"enabled": True})
        rendered = repr((summary.to_dict(), preview.to_dict(), context))
        self.assertNotIn(secret, rendered)
        self.assertNotIn("GY_TOKEN", rendered)
        self.assertNotIn(str(env_file), rendered)
        self.assertEqual(len(context), 64)
        self.assertIn("不会立即", " ".join(preview.suggestions))

    def test_confirmation_preparer_uses_one_snapshot(self):
        registry = build_tool_registry()
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            config.write_env_file(env_file, {
                "STRM_SCHEDULE_ENABLED": "0",
                "STRM_SCHEDULE_CRON": "0 4 * * *",
                "STRM_NOTIFY_ENABLED": "1",
            }, replace=False)
            with patch.object(config, "ENV_FILE", env_file), patch.object(
                config, "_cache", None
            ), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()), patch.object(
                strm_schedule_config_actions,
                "_capture",
                wraps=strm_schedule_config_actions._capture,
            ) as capture:
                spec, arguments, context, preview, _elapsed = registry.prepare_confirmation(
                    "strm.set_schedule_policy", {"enabled": True}
                )
        self.assertEqual(spec.name, "strm.set_schedule_policy")
        self.assertEqual(arguments, {"enabled": True})
        self.assertTrue(preview.ok)
        self.assertEqual(len(context), 64)
        self.assertEqual(capture.call_count, 1)

    def test_scheduler_reload_only_invalidates_schedule(self):
        scheduler = STRMScheduler()
        scheduler._next_run = "future"
        scheduler._loaded_cron = "0 4 * * *"
        with patch.object(scheduler, "trigger") as trigger:
            scheduler.reload()
        self.assertIsNone(scheduler._next_run)
        self.assertEqual(scheduler._loaded_cron, "")
        self.assertTrue(scheduler._wake_event.is_set())
        trigger.assert_not_called()


class StrmSchedulePolicyApiTests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.temp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp.name) / "user.env"
        self.previous_env = {key: os.environ.get(key) for key in _STRM_KEYS}
        for key in _STRM_KEYS:
            os.environ.pop(key, None)
        config.write_env_file(self.env_file, {
            "STRM_SCHEDULE_ENABLED": "0",
            "STRM_SCHEDULE_CRON": "0 4 * * *",
            "STRM_NOTIFY_ENABLED": "1",
            "GY_TOKEN": "must-not-leak",
        }, replace=False)
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
        for key in _STRM_KEYS:
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
        response = self.client.post("/login", data={
            "username": "admin", "password": "123456", "csrf_token": token,
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def prepare(self, csrf: str, arguments: dict):
        response = self.client.post(
            "/api/agent/actions/strm.set_schedule_policy/prepare",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "arguments": arguments},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_query_prepare_confirm_and_replay(self):
        csrf = self.login()
        prepared = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "message": "启用 STRM 定时同步"},
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        body = prepared.json()
        self.assertEqual(body["mode"], "confirmation_required")
        self.assertEqual(body["confirmation"]["tool"], "strm.set_schedule_policy")
        self.assertEqual(config._read_env_file(self.env_file)["STRM_SCHEDULE_ENABLED"], "0")

        scheduler = Mock()
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "confirmation_id": body["confirmation"]["confirmation_id"]},
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertTrue(confirmed.json()["result"]["ok"])
        self.assertEqual(config._read_env_file(self.env_file)["STRM_SCHEDULE_ENABLED"], "1")
        scheduler.reload.assert_called_once_with()
        scheduler.trigger.assert_not_called()
        replay = self.client.post(
            "/api/agent/actions/confirm",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "confirmation_id": body["confirmation"]["confirmation_id"]},
        )
        self.assertEqual(replay.status_code, 409, replay.text)

    def test_noop_override_stale_and_failures(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        noop = self.client.post(
            "/api/agent/actions/strm.set_schedule_policy/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"enabled": False}},
        )
        self.assertEqual(noop.status_code, 409, noop.text)

        config._STARTUP_ENV_OVERRIDES = frozenset({"STRM_SCHEDULE_ENABLED"})
        os.environ["STRM_SCHEDULE_ENABLED"] = "1"
        overridden = self.client.post(
            "/api/agent/actions/strm.set_schedule_policy/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"enabled": False}},
        )
        self.assertEqual(overridden.status_code, 409, overridden.text)
        config._STARTUP_ENV_OVERRIDES = frozenset()
        os.environ.pop("STRM_SCHEDULE_ENABLED", None)

        agent_rate_limiter.reset()
        prepared = self.prepare(csrf, {"enabled": True})
        values = config._read_env_file(self.env_file)
        values["UNRELATED_SETTING"] = "changed"
        config.write_env_file(self.env_file, values, replace=True)
        stale = self.client.post(
            "/api/agent/actions/confirm",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "confirmation_id": prepared.json()["confirmation"]["confirmation_id"]},
        )
        self.assertEqual(stale.status_code, 409, stale.text)

        agent_rate_limiter.reset()
        prepared = self.prepare(csrf, {"enabled": True})
        scheduler = Mock()
        with patch(
            "app.agent.strm_schedule_config_actions.config.update_runtime_env_file",
            side_effect=config.AtomicPublishError("secret failure"),
        ), patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
            failed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": prepared.json()["confirmation"]["confirmation_id"]},
            )
        self.assertEqual(failed.status_code, 503, failed.text)
        scheduler.reload.assert_not_called()
        self.assertNotIn("secret failure", failed.text)

    def test_runtime_refresh_failure_is_deferred_success(self):
        csrf = self.login()
        prepared = self.prepare(csrf, {"enabled": True})
        scheduler = Mock()
        scheduler.reload.side_effect = RuntimeError("private runtime detail")
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "confirmation_id": prepared.json()["confirmation"]["confirmation_id"]},
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        result = confirmed.json()["result"]
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["runtime_refreshed"])
        self.assertIn("重启服务", " ".join(result["suggestions"]))
        self.assertNotIn("private runtime detail", confirmed.text)


if __name__ == "__main__":
    unittest.main()
