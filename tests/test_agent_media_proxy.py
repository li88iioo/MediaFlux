"""媒体反代 Agent 工具的严格参数、脱敏、确认与自然语言路由回归。"""
from __future__ import annotations

import json
from unittest.mock import Mock, patch

import httpx

from app import database as db
from app.agent.media_proxy_actions import (
    media_proxy_enabled_arguments,
    media_proxy_status_arguments,
    media_proxy_test_arguments,
    summarize_media_proxy_status,
    test_media_proxy_instance as run_media_proxy_instance_test,
)
from app.agent.orchestrator import (
    is_media_proxy_control_message,
    is_media_proxy_status_summary_message,
    is_media_proxy_test_request_message,
    media_proxy_instance_enabled_request,
    media_proxy_test_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from tests.support import IsolatedDatabaseTestCase


class AgentMediaProxyTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_proxy_instances")
            conn.execute("DELETE FROM agent_action_history")
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self._port = 19000

    def tearDown(self) -> None:
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    def _add_proxy(
        self,
        *,
        enabled: int = 1,
        server_type: str = "jellyfin",
        upstream_url: str = "http://secret-upstream.invalid:8096/emby",
        api_key: str = "SECRET_MEDIA_PROXY_KEY",
    ) -> int:
        self._port += 1
        return db.add_media_proxy_instance(
            name=f"Private Proxy {self._port}",
            server_type=server_type,
            upstream_url=upstream_url,
            api_key=api_key,
            listen_host="127.0.0.1",
            listen_port=self._port,
            local_root="/private/media/root",
            enabled=enabled,
        )

    @staticmethod
    def _serialized(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    def _assert_private_values_absent(self, value: object) -> None:
        serialized = self._serialized(value)
        for private in (
            "Private Proxy",
            "secret-upstream.invalid",
            "SECRET_MEDIA_PROXY_KEY",
            "127.0.0.1",
            "/private/media/root",
        ):
            self.assertNotIn(private, serialized)

    def test_validators_registry_and_natural_language_are_strict(self):
        self.assertEqual(media_proxy_status_arguments({}), {})
        self.assertEqual(
            media_proxy_test_arguments({"instance_number": 2}),
            {"instance_number": 2},
        )
        self.assertEqual(
            media_proxy_enabled_arguments({"instance_number": 2, "enabled": False}),
            {"instance_number": 2, "enabled": False},
        )
        invalid_test = (
            {},
            {"instance_number": 0},
            {"instance_number": True},
            {"instance_number": "1"},
            {"instance_number": 1, "url": "http://attacker.invalid"},
        )
        for arguments in invalid_test:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                media_proxy_test_arguments(arguments)
        invalid_write = (
            {},
            {"instance_number": 1, "enabled": 1},
            {"instance_number": True, "enabled": False},
            {"instance_number": 1, "enabled": False, "all": True},
        )
        for arguments in invalid_write:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                media_proxy_enabled_arguments(arguments)

        tools = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        self.assertEqual(tools["media_proxy.status_summary"]["risk"], "read")
        self.assertEqual(tools["media_proxy.test_instance"]["risk"], "read")
        self.assertEqual(tools["media_proxy.set_instance_enabled"]["risk"], "low_write")
        self.assertTrue(tools["media_proxy.set_instance_enabled"]["requires_confirmation"])

        self.assertEqual(
            media_proxy_instance_enabled_request("停用媒体反代实例 2"),
            {"instance_number": 2, "enabled": False},
        )
        self.assertEqual(
            media_proxy_test_request("测试媒体代理 2"),
            {"instance_number": 2},
        )
        self.assertTrue(is_media_proxy_status_summary_message("查看 media proxy 运行情况"))
        self.assertTrue(is_media_proxy_control_message("启用媒体反代"))
        self.assertTrue(is_media_proxy_test_request_message("测试媒体反代"))
        for message in (
            "停用媒体反代",
            "停用媒体反代 1 和媒体反代 2",
            "修改媒体反代 1 的地址",
            "启用并停用媒体反代 1",
        ):
            with self.subTest(message=message):
                self.assertIsNone(media_proxy_instance_enabled_request(message))

    def test_status_summary_uses_public_ordinals_and_never_leaks_configuration(self):
        first = self._add_proxy()
        second = self._add_proxy(server_type="emby")
        self._add_proxy(enabled=0)
        db.update_media_proxy_instance(second, {"status": "error", "last_error": "SECRET_RAW_ERROR"})
        manager = Mock()
        manager.status.return_value = {first: {"running": True}}
        with patch("app.agent.media_proxy_actions.get_media_proxy_manager", return_value=manager):
            result = summarize_media_proxy_status({})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["instance_count"], 3)
        self.assertEqual(result.data["enabled_count"], 2)
        self.assertEqual(result.data["disabled_count"], 1)
        self.assertEqual(result.data["running_count"], 1)
        self.assertEqual(result.data["error_count"], 1)
        self.assertEqual(
            [item["runtime_status"] for item in result.data["instances"]],
            ["running", "error", "disabled"],
        )
        self.assertEqual(
            set(result.data["instances"][0]),
            {"instance_number", "server_type", "enabled", "runtime_status"},
        )
        self.assertFalse(result.data["network_accessed"])
        self._assert_private_values_absent(result.to_dict())
        self.assertNotIn("SECRET_RAW_ERROR", self._serialized(result.to_dict()))
        self.assertNotIn(f'"id": {first}', self._serialized(result.to_dict()))

    def test_probe_maps_fixed_outcomes_without_exposing_target_or_raw_errors(self):
        internal_id = self._add_proxy()
        cases = (
            ({"status_code": 204, "latency_ms": 12}, None, True, "reachable"),
            ({"status_code": 401, "latency_ms": 13}, None, False, "authentication_failed"),
            ({"status_code": 302, "latency_ms": 14}, None, False, "redirect_not_allowed"),
            ({"status_code": 503, "latency_ms": 15}, None, False, "upstream_error"),
            (None, httpx.TimeoutException("SECRET timeout target"), False, "timeout"),
        )
        for payload, failure, ok, expected in cases:
            def fake_run(awaitable):
                awaitable.close()
                if failure is not None:
                    raise failure
                return payload

            with self.subTest(expected=expected), patch(
                "app.agent.media_proxy_actions.run_awaitable_sync",
                side_effect=fake_run,
            ) as run_mock:
                result = run_media_proxy_instance_test({"instance_number": 1})
            self.assertEqual(result.ok, ok)
            self.assertEqual(result.data["connection_status"], expected)
            self.assertEqual(result.data["instance_number"], 1)
            self.assertTrue(result.data["network_accessed"])
            self._assert_private_values_absent(result.to_dict())
            self.assertNotIn("SECRET timeout target", self._serialized(result.to_dict()))
            called_awaitable = run_mock.call_args.args[0]
            self.assertTrue(called_awaitable.cr_frame is None)
            self.assertGreater(internal_id, 0)

    def test_confirmed_enable_change_updates_only_target_and_refreshes_runtime(self):
        internal_id = self._add_proxy(enabled=1)
        manager = Mock()
        manager.request_reconcile.return_value = True
        service = get_agent_service()
        with patch(
            "app.agent.media_proxy_actions.clear_signed_url_cache"
        ) as clear_cache, patch(
            "app.agent.media_proxy_actions.get_media_proxy_manager",
            return_value=manager,
        ):
            prepared = service.prepare(
                "media_proxy.set_instance_enabled",
                {"instance_number": 1, "enabled": False},
                owner="owner",
            )
            self.assertEqual(int(db.get_media_proxy_instance(internal_id)["enabled"]), 1)
            confirmed = service.confirm(
                prepared["confirmation"]["confirmation_id"], owner="owner"
            )

        self.assertEqual(int(db.get_media_proxy_instance(internal_id)["enabled"]), 0)
        clear_cache.assert_called_once_with(internal_id)
        manager.request_reconcile.assert_called_once_with()
        self.assertEqual(confirmed["result"]["status"], "completed")
        self.assertTrue(confirmed["result"]["data"]["runtime_refreshed"])
        self._assert_private_values_absent({"prepared": prepared, "confirmed": confirmed})

    def test_confirmation_rejects_stale_configuration_without_runtime_side_effect(self):
        internal_id = self._add_proxy(enabled=1)
        service = get_agent_service()
        prepared = service.prepare(
            "media_proxy.set_instance_enabled",
            {"instance_number": 1, "enabled": False},
            owner="owner",
        )
        db.update_media_proxy_instance(
            internal_id,
            {"upstream_url": "http://changed-secret.invalid:8096"},
        )
        manager = Mock()
        with patch(
            "app.agent.media_proxy_actions.clear_signed_url_cache"
        ) as clear_cache, patch(
            "app.agent.media_proxy_actions.get_media_proxy_manager",
            return_value=manager,
        ):
            confirmed = service.confirm(
                prepared["confirmation"]["confirmation_id"], owner="owner"
            )

        self.assertEqual(confirmed["result"]["status"], "conflict")
        self.assertEqual(int(db.get_media_proxy_instance(internal_id)["enabled"]), 1)
        clear_cache.assert_not_called()
        manager.request_reconcile.assert_not_called()
        self.assertNotIn("changed-secret.invalid", self._serialized(confirmed))

    def test_orchestrator_routes_exact_targets_and_clarifies_ambiguous_requests(self):
        self._add_proxy(enabled=1)
        service = get_agent_service()
        manager = Mock()
        manager.status.return_value = {}
        with patch("app.agent.media_proxy_actions.get_media_proxy_manager", return_value=manager):
            status = service.query("查看媒体反代状态", present=False)
        self.assertEqual(status["tool_call"]["name"], "media_proxy.status_summary")

        def fake_run(awaitable):
            awaitable.close()
            return {"status_code": 204, "latency_ms": 3}

        with patch(
            "app.agent.media_proxy_actions.run_awaitable_sync", side_effect=fake_run
        ):
            tested = service.query("测试媒体反代实例 1", present=False)
        self.assertEqual(tested["tool_call"]["name"], "media_proxy.test_instance")

        prepared = service.query(
            "停用媒体反代实例 1", owner="owner", present=False
        )
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(
            prepared["tool_call"]["name"], "media_proxy.set_instance_enabled"
        )
        clarified = service.query("停用媒体反代", owner="owner", present=False)
        self.assertEqual(clarified["result"]["status"], "clarification_required")
        self.assertNotIn("confirmation", clarified)


if __name__ == "__main__":
    import unittest

    unittest.main()


class AgentMediaProxyPlaybackFailureTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_proxy_playback_records")
            conn.execute("DELETE FROM media_proxy_playback_sessions")
            conn.execute("DELETE FROM media_proxy_instances")
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.instance_id = db.add_media_proxy_instance(
            name="PRIVATE INSTANCE NAME",
            server_type="jellyfin",
            config_source="custom",
            upstream_url="http://private-upstream.invalid:8096",
            api_key="PRIVATE-API-KEY",
            listen_host="127.0.0.1",
            listen_port=19111,
            local_root="/private/library",
            enabled=1,
        )

    def tearDown(self) -> None:
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    def test_tool_returns_attention_as_successful_safe_aggregate(self):
        db.record_media_proxy_playback_attempt(
            instance_id=self.instance_id,
            playback_session_key="PRIVATE-SESSION",
            media_item_id="PRIVATE-ITEM",
            media_name="PRIVATE MOVIE",
            route_class="guangya_direct",
            method="GET",
            status_code=502,
            source="guangya",
            failure_stage="signed_url",
            error="PRIVATE https://secret.invalid/file?token=SECRET /private/path",
            total_latency_ms=55,
        )
        response = get_agent_service().invoke(
            "media_proxy.playback_failure_summary",
            {"hours": 24, "instance_number": 1},
            owner="owner-a",
        )
        result = response["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["data"]["failed"], 1)
        self.assertEqual(result["data"]["failure_stages"][0]["stage"], "signed_url")
        serialized = json.dumps(response, ensure_ascii=False)
        for secret in (
            "PRIVATE", "secret.invalid", "SECRET", "/private", "PRIVATE-SESSION"
        ):
            self.assertNotIn(secret, serialized)

    def test_natural_query_precedes_generic_proxy_status(self):
        from app.agent.orchestrator import (
            media_proxy_playback_failure_summary_request,
        )

        self.assertEqual(
            media_proxy_playback_failure_summary_request(
                "查看媒体反代实例 1 最近 6 小时播放失败摘要"
            ),
            {"hours": 6, "instance_number": 1},
        )
        response = get_agent_service().query(
            "查看媒体反代最近 24 小时播放失败摘要", present=False
        )
        self.assertEqual(
            response["tool_call"]["name"],
            "media_proxy.playback_failure_summary",
        )
        clarified = get_agent_service().query(
            "查看媒体反代最近 48 小时播放失败摘要", present=False
        )
        self.assertEqual(clarified["result"]["status"], "clarification_required")
