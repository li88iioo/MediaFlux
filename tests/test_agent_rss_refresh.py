"""Media Agent 单订阅 RSS 刷新的确认、竞态与脱敏回归。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app import database as db
from app.agent.action_history import action_history_owner_digest
from app.agent.errors import AgentToolError
from app.agent.rate_limit import agent_rate_limiter
from app.agent.rss_refresh_actions import (
    prepare_rss_subscription_refresh,
    prepare_rss_subscriptions_refresh,
    rss_refresh_subscriptions_arguments,
)
from app.modules.rss import RSS_REFRESH_BUSY_ERROR, RSSEngine
from tests.agent_kernel_test_harness import (
    get_kernel_test_service as get_agent_service,
)
from tests.agent_kernel_test_harness import (
    reset_kernel_test_service as reset_agent_service_for_tests,
)
from tests.support import IsolatedDatabaseTestCase


class RSSRefreshAgentTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_entries")
            conn.execute("DELETE FROM rss_items")
            conn.execute("DELETE FROM agent_action_history")
        reset_agent_service_for_tests()
        self.sid = db.add_rss_subscription(
            "Private RSS",
            "https://secret.example/rss?passkey=RSS_SECRET",
            exclude_keywords="PRIVATE_FILTER",
        )

    def tearDown(self) -> None:
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    def test_preview_is_local_only_and_sanitized(self):
        with patch.object(RSSEngine, "refresh") as refresh:
            result, _context = prepare_rss_subscription_refresh(
                {"subscription_id": self.sid}
            )
        self.assertTrue(result.ok)
        refresh.assert_not_called()
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in (
            "Private RSS",
            "secret.example",
            "passkey",
            "RSS_SECRET",
            "PRIVATE_FILTER",
        ):
            self.assertNotIn(secret, serialized)

    def test_confirm_executes_once_and_returns_aggregate_only(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.refresh_subscription", {"subscription_id": self.sid}, owner="owner"
        )
        with patch.object(
            RSSEngine, "refresh", return_value={"total": 7, "new": 3, "skipped": 2}
        ) as refresh:
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args, (self.sid,))
        self.assertIn("expected_revision", refresh.call_args.kwargs)
        self.assertEqual(confirmed["result"]["status"], "completed")
        self.assertEqual(
            confirmed["result"]["data"],
            {"subscription_id": self.sid, "total": 7, "new": 3, "skipped": 2},
        )
        serialized = json.dumps(confirmed, ensure_ascii=False)
        for secret in (
            "Private RSS",
            "secret.example",
            "passkey",
            "RSS_SECRET",
            "PRIVATE_FILTER",
        ):
            self.assertNotIn(secret, serialized)
        history = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"), limit=1
        )[0]
        self.assertEqual(history["tool_name"], "rss.refresh_subscription")
        self.assertEqual(history["status"], "completed")
        details = json.loads(history["safe_details"])
        self.assertEqual(
            {
                key: details[key]
                for key in ("new", "skipped", "subscription_id", "total")
            },
            {"new": 3, "skipped": 2, "subscription_id": self.sid, "total": 7},
        )
        self.assertEqual(details["contract_action"], "立即刷新 RSS 订阅")
        self.assertEqual(details["contract_risk"], "write")
        history_serialized = json.dumps(dict(history), ensure_ascii=False)
        for secret in (
            "Private RSS",
            "secret.example",
            "passkey",
            "RSS_SECRET",
            "PRIVATE_FILTER",
        ):
            self.assertNotIn(secret, history_serialized)

    def test_single_refresh_surfaces_partial_source_failure(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.refresh_subscription", {"subscription_id": self.sid}, owner="owner"
        )
        with patch.object(
            RSSEngine,
            "refresh",
            return_value={
                "total": 7,
                "new": 3,
                "skipped": 2,
                "partial": True,
                "failed_sources": 1,
            },
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        result = confirmed["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["failed_sources"], 1)
        self.assertIn("部分完成", result["summary"])
        self.assertIn("暂不可用源", result["suggestions"][0])

    def test_bulk_refresh_is_strict_local_preview_and_aggregates_partial_results(self):
        second_id = db.add_rss_subscription(
            "Mikan", "https://second.secret.invalid/rss?token=SECOND_SECRET"
        )
        arguments = {"subscription_ids": [self.sid, second_id]}
        self.assertEqual(rss_refresh_subscriptions_arguments(arguments), arguments)
        self.assertEqual(
            rss_refresh_subscriptions_arguments({"scope": "all_configured"}),
            {"scope": "all_configured"},
        )
        invalid_arguments = (
            {},
            {"subscription_ids": []},
            {"subscription_ids": [self.sid, self.sid]},
            {"subscription_ids": [True]},
            {"subscription_ids": ["1"]},
            {"subscription_ids": list(range(1, 34))},
            {"subscription_ids": [self.sid], "all": True},
            {"scope": "selected"},
            {"scope": "all_enabled"},
            {"scope": "all_configured", "subscription_ids": [self.sid]},
            {"scope": "all_enabled", "subscription_ids": [self.sid]},
        )
        for invalid in invalid_arguments:
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                rss_refresh_subscriptions_arguments(invalid)
        tools = {
            item["name"]: item for item in get_agent_service().capabilities()["tools"]
        }
        spec = tools["rss.refresh_subscriptions"]
        self.assertEqual(spec["risk"], "write")
        self.assertTrue(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])
        self.assertEqual(
            spec["parameters"]["properties"]["scope"]["enum"], ["all_configured"]
        )
        with patch.object(RSSEngine, "refresh") as refresh:
            preview, _context = prepare_rss_subscriptions_refresh(arguments)
        refresh.assert_not_called()
        self.assertTrue(preview.ok)
        self.assertEqual(preview.status, "confirmation_required")
        self.assertEqual(preview.data["subscription_count"], 2)
        preview_serialized = json.dumps(preview.to_dict(), ensure_ascii=False)
        self.assertIn("Private RSS", preview_serialized)
        self.assertIn("Mikan", preview_serialized)
        for secret in (
            "secret.example",
            "RSS_SECRET",
            "second.secret.invalid",
            "SECOND_SECRET",
        ):
            self.assertNotIn(secret, preview_serialized)
        service = get_agent_service()
        prepared = service.prepare(
            "rss.refresh_subscriptions", arguments, owner="owner"
        )
        with patch.object(
            RSSEngine,
            "refresh",
            side_effect=[
                {
                    "total": 7,
                    "new": 3,
                    "skipped": 2,
                    "partial": True,
                    "failed_sources": 2,
                },
                {"error": RSS_REFRESH_BUSY_ERROR, "busy": True},
            ],
        ) as refresh:
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in refresh.call_args_list], [self.sid, second_id]
        )
        self.assertTrue(
            all(call.kwargs.get("expected_revision") for call in refresh.call_args_list)
        )
        result = confirmed["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            {
                key: result["data"][key]
                for key in (
                    "requested",
                    "refreshed",
                    "failed",
                    "partial_subscriptions",
                    "failed_sources",
                    "total",
                    "new",
                    "skipped",
                )
            },
            {
                "requested": 2,
                "refreshed": 1,
                "failed": 1,
                "partial_subscriptions": 1,
                "failed_sources": 2,
                "total": 7,
                "new": 3,
                "skipped": 2,
            },
        )
        self.assertEqual(
            set(item["status"] for item in result["data"]["subscriptions"]),
            {"partial", "busy"},
        )
        partial_sub = next(
            item
            for item in result["data"]["subscriptions"]
            if item["status"] == "partial"
        )
        self.assertEqual(partial_sub["failed_sources"], 2)
        self.assertIn("暂不可用源 2", result["summary"])
        self.assertTrue(any("暂不可用源" in item for item in result["suggestions"]))
        serialized = json.dumps(confirmed, ensure_ascii=False)
        self.assertIn("Private RSS", serialized)
        self.assertIn("Mikan", serialized)
        for secret in (
            "secret.example",
            "RSS_SECRET",
            "second.secret.invalid",
            "SECOND_SECRET",
        ):
            self.assertNotIn(secret, serialized)

    def test_removed_all_enabled_scope_is_rejected(self):
        with self.assertRaises(AgentToolError) as invalid:
            get_agent_service().prepare(
                "rss.refresh_subscriptions", {"scope": "all_enabled"}, owner="owner"
            )
        self.assertEqual(invalid.exception.code, "invalid_arguments")
        self.assertIn("允许范围", invalid.exception.safe_message)

    def test_configuration_change_makes_confirmation_stale(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.refresh_subscription", {"subscription_id": self.sid}, owner="owner"
        )
        db.update_rss_subscription(self.sid, {"urls": "https://changed.invalid/rss"})
        with (
            patch.object(RSSEngine, "refresh") as refresh,
            self.assertRaises(AgentToolError) as stale,
        ):
            service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertEqual(stale.exception.code, "confirmation_stale")
        refresh.assert_not_called()

    def test_busy_result_is_safe(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.refresh_subscription", {"subscription_id": self.sid}, owner="owner"
        )
        with patch.object(
            RSSEngine,
            "refresh",
            return_value={"error": RSS_REFRESH_BUSY_ERROR, "busy": True},
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertFalse(confirmed["result"]["ok"])
        self.assertEqual(confirmed["result"]["status"], "busy")
        self.assertNotIn("secret", json.dumps(confirmed, ensure_ascii=False).casefold())
        history = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"), limit=1
        )[0]
        self.assertEqual(history["tool_name"], "rss.refresh_subscription")
        self.assertEqual(history["status"], "busy")
        details = json.loads(history["safe_details"])
        self.assertEqual(details["contract_action"], "立即刷新 RSS 订阅")
        self.assertEqual(details["contract_risk"], "write")
        self.assertNotIn("subscription_id", details)
