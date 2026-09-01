"""Media Agent 全库巡检策略受控配置测试。"""
from __future__ import annotations

from datetime import datetime
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import config
from app.agent import library_patrol_config_actions
from app.agent.library_patrol_config_actions import (
    patrol_policy_arguments,
    prepare_patrol_policy_confirmation,
    summarize_patrol_policy,
)
from app.agent.models import RiskLevel
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_library_patrol_policy_summary_message,
    library_patrol_policy_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.main import create_app
from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler
from tests.support import IsolatedDatabaseTestCase


_PATROL_KEYS = {
    "AGENT_LIBRARY_PATROL_ENABLED",
    "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED",
    "AGENT_LIBRARY_PATROL_INTERVAL_HOURS",
    "AGENT_LIBRARY_PATROL_MAX_SERIES",
}


class PatrolPolicyUnitTests(unittest.TestCase):
    def test_arguments_are_partial_bounded_and_strict(self):
        self.assertEqual(
            patrol_policy_arguments({
                "enabled": True,
                "notify_enabled": False,
                "interval_hours": 168,
                "max_series": 100,
            }),
            {
                "enabled": True,
                "notify_enabled": False,
                "interval_hours": 168,
                "max_series": 100,
            },
        )
        for arguments in (
            {},
            {"enabled": 1},
            {"notify_enabled": "true"},
            {"interval_hours": True},
            {"interval_hours": 0},
            {"interval_hours": 169},
            {"max_series": 0},
            {"max_series": 101},
            {"key": "AGENT_LIBRARY_PATROL_ENABLED"},
            {"token": "secret"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                patrol_policy_arguments(arguments)

    def test_natural_language_is_explicit_and_composable(self):
        self.assertEqual(
            library_patrol_policy_request(
                "把全库巡检改为每 24 小时一次，最多检查 50 部剧，并开启通知"
            ),
            {"interval_hours": 24, "max_series": 50, "notify_enabled": True},
        )
        self.assertEqual(
            library_patrol_policy_request("启用自动缺集巡检并开启巡检通知"),
            {"notify_enabled": True, "enabled": True},
        )
        self.assertEqual(
            library_patrol_policy_request("关闭全库巡检通知"),
            {"notify_enabled": False},
        )
        self.assertEqual(
            library_patrol_policy_request("关闭定时巡检"),
            {"enabled": False},
        )
        for message in (
            "不要关闭定时巡检",
            "不关闭定时巡检",
            "不用关闭定时巡检",
            "无需关闭定时巡检",
            "取消关闭定时巡检",
            "我不想开启全库巡检",
            "当前全库巡检每 24 小时",
            "全库巡检间隔 24 小时",
            "全库巡检最多 50 Mbps",
            "如果关闭全库巡检会怎样",
            "能否开启自动缺集巡检",
            "开启自动缺集巡检然后关闭媒体探索",
            "上次全库巡检发现了什么",
            "自动缺集巡检怎么配置",
        ):
            with self.subTest(message=message):
                self.assertIsNone(library_patrol_policy_request(message))
        with self.assertRaisesRegex(AgentToolError, "冲突"):
            library_patrol_policy_request("把全库巡检改为每 12 小时，再改为每 24 小时")

    def test_summary_intent_does_not_steal_status_or_live_audit(self):
        for message in (
            "查看全库巡检策略",
            "自动缺集巡检怎么配置",
            "当前全库巡检间隔是多少",
            "全库巡检通知开着吗",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_library_patrol_policy_summary_message(message))
        for message in (
            "上次自动缺集巡检结果",
            "巡检整个媒体库有没有缺集",
            "把刚才巡检发现的缺集找资源",
            "开启自动缺集巡检",
        ):
            with self.subTest(message=message):
                self.assertFalse(is_library_patrol_policy_summary_message(message))

    def test_registry_contract_and_routes(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(capabilities["library.patrol_policy"]["risk"], RiskLevel.READ.value)
        self.assertEqual(
            capabilities["library.set_patrol_policy"]["risk"],
            RiskLevel.LOW_WRITE.value,
        )
        self.assertTrue(capabilities["library.set_patrol_policy"]["requires_confirmation"])
        with self.assertRaisesRegex(AgentToolError, "需要确认"):
            registry.execute("library.set_patrol_policy", {"enabled": True})

        agent = AgentOrchestrator(registry)
        self.assertEqual(
            agent.query("查看全库巡检策略")["tool_call"]["name"],
            "library.patrol_policy",
        )
        self.assertEqual(
            agent.query("上次自动缺集巡检结果")["tool_call"]["name"],
            "library.patrol_status",
        )
        ownerless_audit = agent.query("巡检整个媒体库有没有缺集")
        self.assertEqual(ownerless_audit["result"]["status"], "unsupported")
        self.assertTrue(
            capabilities["library.start_episode_audit"]["requires_confirmation"]
        )
        ownerless = agent.query("开启自动缺集巡检")
        self.assertEqual(ownerless["result"]["status"], "unsupported")

    def test_summary_preview_and_context_do_not_leak_other_config(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            secret = "patrol-secret-must-not-leak"
            config.write_env_file(
                env_file,
                {
                    "AGENT_LIBRARY_PATROL_ENABLED": "0",
                    "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED": "1",
                    "AGENT_LIBRARY_PATROL_INTERVAL_HOURS": "12",
                    "AGENT_LIBRARY_PATROL_MAX_SERIES": "40",
                    "TMDB_API_KEY": secret,
                },
                replace=False,
            )
            with patch.object(config, "ENV_FILE", env_file), patch.object(
                config, "_cache", None
            ), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()):
                summary = summarize_patrol_policy({})
                preview, context = prepare_patrol_policy_confirmation(
                    {"notify_enabled": False}
                )
        rendered = repr((summary.to_dict(), preview.to_dict(), context))
        self.assertNotIn(secret, rendered)
        self.assertNotIn("TMDB_API_KEY", rendered)
        self.assertNotIn(str(env_file), rendered)
        self.assertEqual(len(context), 64)
        self.assertIn("丢弃", " ".join(preview.suggestions))

    def test_confirmation_preparer_uses_one_snapshot(self):
        registry = build_tool_registry()
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            config.write_env_file(
                env_file,
                {
                    "AGENT_LIBRARY_PATROL_ENABLED": "0",
                    "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED": "1",
                    "AGENT_LIBRARY_PATROL_INTERVAL_HOURS": "24",
                    "AGENT_LIBRARY_PATROL_MAX_SERIES": "50",
                },
                replace=False,
            )
            with patch.object(config, "ENV_FILE", env_file), patch.object(
                config, "_cache", None
            ), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()), patch.object(
                library_patrol_config_actions,
                "_capture",
                wraps=library_patrol_config_actions._capture,
            ) as capture:
                spec, arguments, context, preview, _elapsed = registry.prepare_confirmation(
                    "library.set_patrol_policy",
                    {"enabled": True},
                )
        self.assertEqual(spec.name, "library.set_patrol_policy")
        self.assertEqual(arguments, {"enabled": True})
        self.assertTrue(preview.ok)
        self.assertEqual(len(context), 64)
        self.assertEqual(capture.call_count, 1)

    def test_notification_backlog_side_effect_is_disclosed_while_already_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            config.write_env_file(
                env_file,
                {
                    "AGENT_LIBRARY_PATROL_ENABLED": "0",
                    "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED": "0",
                    "AGENT_LIBRARY_PATROL_INTERVAL_HOURS": "24",
                    "AGENT_LIBRARY_PATROL_MAX_SERIES": "50",
                },
                replace=False,
            )
            with patch.object(config, "ENV_FILE", env_file), patch.object(
                config, "_cache", None
            ), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()):
                preview, context = prepare_patrol_policy_confirmation({"enabled": True})
        self.assertTrue(preview.ok)
        self.assertEqual(len(context), 64)
        self.assertIn("积压", " ".join(preview.suggestions))
        self.assertIn("无法恢复", " ".join(preview.suggestions))

    def test_scheduler_can_reload_without_immediate_patrol(self):
        scheduler = AgentLibraryPatrolScheduler(
            clock=lambda: datetime(2026, 8, 4, 12, 0, 0)
        )
        with patch.object(scheduler, "_enabled", return_value=True), patch.object(
            scheduler, "_interval_seconds", return_value=6 * 60 * 60
        ), patch.object(
            scheduler, "_notifications_enabled", return_value=False
        ), patch(
            "app.modules.agent_library_patrol_scheduler.db.reschedule_agent_library_patrol"
        ) as reschedule, patch(
            "app.modules.agent_library_patrol_scheduler.db.discard_agent_library_patrol_notifications"
        ) as discard_notifications:
            scheduler.reload(immediate=False)
        reschedule.assert_called_once_with(next_run_at="2026-08-04 18:00:00")
        discard_notifications.assert_called_once_with()
        self.assertTrue(scheduler._wake_event.is_set())


class PatrolPolicyApiTests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.temp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp.name) / "user.env"
        self.previous_env = {key: os.environ.get(key) for key in _PATROL_KEYS}
        for key in _PATROL_KEYS:
            os.environ.pop(key, None)
        config.write_env_file(
            self.env_file,
            {
                "AGENT_LIBRARY_PATROL_ENABLED": "0",
                "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED": "1",
                "AGENT_LIBRARY_PATROL_INTERVAL_HOURS": "24",
                "AGENT_LIBRARY_PATROL_MAX_SERIES": "50",
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
        for key in _PATROL_KEYS:
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

    def prepare(self, csrf: str, arguments: dict):
        response = self.client.post(
            "/api/agent/actions/library.set_patrol_policy/prepare",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "arguments": arguments},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_query_prepare_confirm_summary_and_replay(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        prepared = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001",
                "message": "把全库巡检改为每 12 小时一次，最多检查 80 部剧，并关闭通知"
            },
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        body = prepared.json()
        self.assertEqual(body["mode"], "confirmation_required")
        self.assertEqual(body["tool_call"]["name"], "library.set_patrol_policy")
        self.assertIn("丢弃", " ".join(body["result"]["suggestions"]))
        self.assertEqual(
            body["result"]["data"]["changed_fields"],
            ["notify_enabled", "interval_hours", "max_series"],
        )
        self.assertEqual(
            config._read_env_file(self.env_file)["AGENT_LIBRARY_PATROL_INTERVAL_HOURS"],
            "24",
        )

        scheduler = Mock()
        confirmation_id = body["action_plan"]["plan_id"]
        with patch(
            "app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler",
            return_value=scheduler,
        ):
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        result = confirmed.json()["result"]
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["runtime_refreshed"])
        self.assertEqual(result["data"]["runtime_scope"], "current_process")
        self.assertNotIn("多 worker", " ".join(result["suggestions"]))
        scheduler.reload.assert_called_once_with(immediate=False)
        saved = config._read_env_file(self.env_file)
        self.assertEqual(saved["AGENT_LIBRARY_PATROL_INTERVAL_HOURS"], "12")
        self.assertEqual(saved["AGENT_LIBRARY_PATROL_MAX_SERIES"], "80")
        self.assertEqual(saved["AGENT_LIBRARY_PATROL_NOTIFY_ENABLED"], "0")

        summary = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "查看全库巡检策略"},
        )
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(summary.json()["tool_call"]["name"], "library.patrol_policy")
        self.assertEqual(summary.json()["result"]["data"]["policy"]["interval_hours"], 12)
        replay = self.client.post(
            "/api/agent/actions/confirm",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
        )
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertNotIn("must-not-leak", prepared.text + confirmed.text + summary.text)

    def test_invalid_noop_external_override_and_direct_execute_fail_closed(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        direct = self.client.post(
            "/api/agent/tools/library.set_patrol_policy",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"enabled": True}},
        )
        self.assertEqual(direct.status_code, 409, direct.text)
        for arguments in (
            {},
            {"enabled": 1},
            {"interval_hours": 0},
            {"max_series": 101},
            {"token": "secret"},
        ):
            with self.subTest(arguments=arguments):
                agent_rate_limiter.reset()
                invalid = self.client.post(
                    "/api/agent/actions/library.set_patrol_policy/prepare",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "arguments": arguments},
                )
                self.assertEqual(invalid.status_code, 400, invalid.text)
        agent_rate_limiter.reset()
        noop = self.client.post(
            "/api/agent/actions/library.set_patrol_policy/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"interval_hours": 24}},
        )
        self.assertEqual(noop.status_code, 409, noop.text)

        agent_rate_limiter.reset()
        config._STARTUP_ENV_OVERRIDES = frozenset({"AGENT_LIBRARY_PATROL_ENABLED"})
        os.environ["AGENT_LIBRARY_PATROL_ENABLED"] = "1"
        overridden = self.client.post(
            "/api/agent/actions/library.set_patrol_policy/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"enabled": False}},
        )
        self.assertEqual(overridden.status_code, 409, overridden.text)
        self.assertIn("运行环境", overridden.text)

    def test_stale_snapshot_and_atomic_failure_do_not_reload(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        prepared = self.prepare(csrf, {"enabled": True})
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
        self.assertEqual(
            config._read_env_file(self.env_file)["AGENT_LIBRARY_PATROL_ENABLED"], "0"
        )

        agent_rate_limiter.reset()
        prepared = self.prepare(csrf, {"enabled": True})
        scheduler = Mock()
        with patch(
            "app.agent.library_patrol_config_actions.config.update_runtime_env_file",
            side_effect=config.AtomicPublishError("secret failure"),
        ), patch(
            "app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler",
            return_value=scheduler,
        ):
            failed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": prepared.json()["action_plan"]["plan_id"]},
            )
        self.assertEqual(failed.status_code, 503, failed.text)
        result = failed.json()["result"]
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        scheduler.reload.assert_not_called()
        self.assertNotIn("secret failure", failed.text)

    def test_runtime_refresh_failure_is_reported_as_deferred_success(self):
        csrf = self.login()
        prepared = self.prepare(csrf, {"enabled": True})
        scheduler = Mock()
        scheduler.reload.side_effect = RuntimeError("private runtime detail")
        with patch(
            "app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler",
            return_value=scheduler,
        ):
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "plan_id": prepared.json()["action_plan"]["plan_id"]},
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        result = confirmed.json()["result"]
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["runtime_refreshed"])
        self.assertIn("重启服务", " ".join(result["suggestions"]))
        self.assertNotIn("private runtime detail", confirmed.text)

    def test_direct_prepare_and_query_share_write_rate_limit(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        for _ in range(2):
            direct = self.client.post(
                "/api/agent/actions/library.set_patrol_policy/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"enabled": True}},
            )
            self.assertEqual(direct.status_code, 200, direct.text)
            query = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "开启自动缺集巡检"},
            )
            self.assertEqual(query.status_code, 200, query.text)
        limited = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "开启自动缺集巡检"},
        )
        self.assertEqual(limited.status_code, 429, limited.text)


if __name__ == "__main__":
    unittest.main()
