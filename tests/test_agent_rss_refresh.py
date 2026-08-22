"""Media Agent 单订阅 RSS 刷新的确认、竞态与脱敏回归。"""
from __future__ import annotations

import json
import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.action_history import action_history_owner_digest
from app.agent.orchestrator import (
    is_rss_diagnosis_message,
    is_rss_subscription_refresh_write_message,
    rss_subscription_refresh_name,
    rss_subscription_refresh_request,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.rss_refresh_actions import (
    preview_rss_subscription_refresh,
    preview_rss_subscriptions_refresh,
    rss_refresh_subscription_arguments,
    rss_refresh_subscriptions_arguments,
)
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.main import create_app
from app.modules.rss import RSS_REFRESH_BUSY_ERROR, RSSEngine
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

    def test_arguments_registration_and_natural_language_are_strict(self):
        self.assertEqual(
            rss_refresh_subscription_arguments({"subscription_id": self.sid}),
            {"subscription_id": self.sid},
        )
        for invalid in ({}, {"subscription_id": 0}, {"subscription_id": -1},
                        {"subscription_id": True}, {"subscription_id": "1"},
                        {"subscription_id": self.sid, "all": True}):
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                rss_refresh_subscription_arguments(invalid)

        tools = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        spec = tools["rss.refresh_subscription"]
        self.assertEqual(spec["risk"], "write")
        self.assertTrue(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])
        with self.assertRaises(AgentToolError) as direct:
            get_agent_service().registry.execute(
                "rss.refresh_subscription", {"subscription_id": self.sid}
            )
        self.assertEqual(direct.exception.code, "confirmation_required")

        self.assertEqual(
            rss_subscription_refresh_request(f"刷新 RSS 订阅 {self.sid}"),
            {"subscription_id": self.sid},
        )
        self.assertEqual(
            rss_subscription_refresh_request(f"请刷新 rss #{self.sid} 一下"),
            {"subscription_id": self.sid},
        )
        for message in (
            "刷新 RSS 订阅",
            "刷新一下 RSS 订阅",
            "刷新一次 RSS",
            "刷新全部 RSS 订阅",
            "查看 RSS 订阅 1",
            "刷新订阅 1",
        ):
            self.assertIsNone(rss_subscription_refresh_request(message))
        self.assertTrue(is_rss_subscription_refresh_write_message("刷新 RSS 订阅"))
        for negated in (
            "不要刷新所有 RSS 订阅",
            "别刷新全部 RSS 订阅",
            "无需手动刷新 RSS",
            "取消 RSS 自动刷新",
            "RSS 订阅不刷新",
            "RSS 订阅先不刷新",
            "刷新 RSS 订阅请不要执行",
            "刷新 RSS 订阅吧，不要",
            "不是刷新 RSS 订阅",
            "并非刷新 RSS 订阅",
            "并不刷新 RSS 订阅",
            "请刷新 RSS 订阅，但不是现在",
            "刷新 RSS 订阅不是要执行",
        ):
            with self.subTest(negated=negated):
                self.assertFalse(is_rss_subscription_refresh_write_message(negated))
        self.assertTrue(is_rss_subscription_refresh_write_message("请分别刷新 RSS 订阅"))
        self.assertEqual(rss_subscription_refresh_name("刷新 Mikan RSS 订阅"), "Mikan")
        self.assertEqual(rss_subscription_refresh_name("刷新 RSS Mikan"), "Mikan")
        self.assertEqual(rss_subscription_refresh_name("刷新一下《Mikan》RSS"), "Mikan")
        self.assertIsNone(rss_subscription_refresh_name("刷新全部 RSS 订阅"))
        self.assertIsNone(rss_subscription_refresh_name("刷新一个rss订阅"))
        diagnosis = "查看 RSS 刷新失败状态"
        self.assertTrue(is_rss_diagnosis_message(diagnosis))
        self.assertFalse(is_rss_subscription_refresh_write_message(diagnosis))
        routed = get_agent_service().query(diagnosis, owner="owner")
        self.assertEqual(routed["tool_call"]["name"], "rss.diagnose")

    def test_negated_refresh_never_creates_confirmation(self):
        context = [{
            "role": "assistant",
            "text": "当前已列出 RSS 订阅。",
            "tool_name": "rss.subscription_summaries",
            "status": "completed",
        }]
        for index, message in enumerate((
            "不要刷新所有 RSS 订阅",
            "别刷新全部订阅",
            "无需手动刷新 RSS",
            "取消 RSS 自动刷新",
            "RSS 订阅不刷新",
            "RSS 订阅先不刷新",
            "刷新 RSS 订阅请不要执行",
            "刷新 RSS 订阅吧，不要",
            "不是刷新 RSS 订阅",
            "并非刷新 RSS 订阅",
            "并不刷新 RSS 订阅",
            "请刷新 RSS 订阅，但不是现在",
            "刷新 RSS 订阅不是要执行",
        )):
            with self.subTest(message=message), patch.object(RSSEngine, "refresh") as refresh:
                response = get_agent_service().query(
                    message,
                    owner=f"owner-negated-{index}",
                    conversation_context=context,
                    present=False,
                )

            refresh.assert_not_called()
            self.assertEqual(response["mode"], "conversation")
            self.assertIsNone(response["tool_call"])
            self.assertIn("不会刷新 RSS", response["result"]["summary"])

    def test_zero_width_rss_scope_is_rejected_before_routing(self):
        with patch.object(RSSEngine, "refresh") as refresh, self.assertRaises(ValueError):
            get_agent_service().query(
                "刷新全\u200b部 RSS 订阅", owner="owner", present=False
            )
        refresh.assert_not_called()

    def test_separate_refresh_wording_is_not_mistaken_for_negation(self):
        with patch.object(RSSEngine, "refresh") as refresh:
            prepared = get_agent_service().query(
                "请分别刷新 RSS 订阅", owner="owner", present=False
            )

        refresh.assert_not_called()
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "rss.refresh_subscription")
        self.assertEqual(prepared["result"]["data"]["subscription_id"], self.sid)

    def test_unique_disabled_subscription_can_still_be_manually_refreshed(self):
        db.update_rss_subscription(self.sid, {"enabled": False})

        prepared = get_agent_service().query(
            "刷新一个rss订阅", owner="owner", present=False
        )

        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "rss.refresh_subscription")
        self.assertEqual(prepared["result"]["data"]["subscription_id"], self.sid)

    def test_generic_refresh_one_continues_recent_rss_topic(self):
        db.update_rss_subscription(self.sid, {"enabled": False})

        prepared = get_agent_service().query(
            "刷新一个",
            owner="owner",
            conversation_context=[{
                "role": "assistant",
                "text": "当前有一个 RSS 订阅，可以继续手动刷新。",
                "tool_name": "rss.subscription_summaries",
                "status": "completed",
            }],
            present=False,
        )

        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "rss.refresh_subscription")
        self.assertEqual(prepared["result"]["data"]["subscription_id"], self.sid)

    def test_contextual_refresh_all_variants_keep_bulk_scope(self):
        db.add_rss_subscription("Second RSS", "https://second.invalid/rss")
        context = [{
            "role": "assistant",
            "text": "当前已列出 RSS 订阅。",
            "tool_name": "rss.subscription_summaries",
            "status": "completed",
        }]

        for index, message in enumerate((
            "刷新全部订阅",
            "刷新所有 RSS 订阅",
            "刷新所有",
            "所有 RSS 订阅刷新",
        )):
            with self.subTest(message=message), patch.object(RSSEngine, "refresh") as refresh:
                prepared = get_agent_service().query(
                    message,
                    owner=f"owner-{index}",
                    conversation_context=context,
                    present=False,
                )

            refresh.assert_not_called()
            self.assertEqual(prepared["mode"], "confirmation_required")
            self.assertEqual(prepared["tool_call"]["name"], "rss.refresh_subscriptions")
            self.assertEqual(prepared["result"]["data"]["scope"], "all_configured")
            self.assertEqual(prepared["result"]["data"]["subscription_count"], 2)

    def test_disabled_state_correction_continues_recent_rss_refresh_intent(self):
        db.update_rss_subscription(self.sid, {"enabled": False})

        prepared = get_agent_service().query(
            "没启动不影响刷新啊",
            owner="owner",
            conversation_context=[{
                "role": "assistant",
                "text": "这个 RSS 订阅目前停用，因此刚才没有继续刷新。",
                "tool_name": "rss.subscription_summaries",
                "status": "completed",
            }],
            present=False,
        )

        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "rss.refresh_subscription")
        self.assertEqual(prepared["result"]["data"]["subscription_id"], self.sid)

    def test_rss_context_does_not_hijack_other_domain_refresh_correction(self):
        db.update_rss_subscription(self.sid, {"enabled": False})
        service = get_agent_service()
        for message in (
            "下载任务没启动不影响刷新啊",
            "qB 没启动不影响刷新啊",
            "qBittorrent 没启动不影响刷新啊",
            "下载器没启动不影响刷新啊",
        ):
            with self.subTest(message=message), patch.object(
                service, "_query_with_model_tools", return_value=None
            ) as model_route:
                response = service.query(
                    message,
                    owner="owner",
                    conversation_context=[{
                        "role": "assistant",
                        "text": "已列出全部 RSS 订阅。",
                        "tool_name": "rss.subscription_summaries",
                        "status": "completed",
                    }],
                    present=False,
                )

            self.assertNotIn(
                (response.get("tool_call") or {}).get("name"),
                {"rss.refresh_subscription", "rss.refresh_subscriptions"},
            )
            self.assertTrue(model_route.call_args.kwargs["read_only"])

    def test_refresh_by_unique_subscription_name_resolves_before_confirmation(self):
        mikan_id = db.add_rss_subscription("Ｍｉｋａｎ", "https://mikan.invalid/rss")
        service = get_agent_service()
        prepared = service.query("刷新 mikan RSS 订阅", owner="owner")
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "rss.refresh_subscription")
        self.assertEqual(prepared["result"]["data"]["subscription_id"], mikan_id)
        serialized = json.dumps(prepared, ensure_ascii=False)
        self.assertNotIn("mikan.invalid", serialized)

    def test_refresh_by_bare_unique_subscription_name_requires_recent_rss_context(self):
        mikan_id = db.add_rss_subscription("Mikan", "https://mikan.invalid/rss")
        self.assertIsNone(rss_subscription_refresh_name("刷新 mikan"))

        prepared = get_agent_service().query(
            "刷新 mikan",
            owner="owner",
            conversation_context=[{
                "role": "assistant",
                "text": "已列出全部 RSS 订阅。",
                "tool_name": "rss.subscription_summaries",
            }],
        )

        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "rss.refresh_subscription")
        self.assertEqual(prepared["result"]["data"]["subscription_id"], mikan_id)

    def test_bare_refresh_does_not_hijack_other_domains(self):
        db.add_rss_subscription("下载队列", "https://queue.invalid/rss")
        service = get_agent_service()
        with patch.object(service, "_query_with_model_tools", return_value=None):
            response = service.query("刷新下载队列", owner="owner", present=False)

        self.assertNotEqual(
            (response.get("tool_call") or {}).get("name"),
            "rss.refresh_subscription",
        )

    def test_refresh_by_subscription_name_does_not_guess_duplicates(self):
        first = db.add_rss_subscription("Mikan", "https://first.invalid/rss")
        second = db.add_rss_subscription("ｍｉｋａｎ", "https://second.invalid/rss")
        response = get_agent_service().query("刷新 Mikan RSS", owner="owner")
        self.assertEqual(response["result"]["status"], "unsupported")
        self.assertIn("多个名为", response["result"]["summary"])
        self.assertEqual(
            response["result"]["suggestions"],
            [f"刷新 RSS 订阅 {first}。", f"刷新 RSS 订阅 {second}。"],
        )
        serialized = json.dumps(response, ensure_ascii=False)
        self.assertNotIn("first.invalid", serialized)
        self.assertNotIn("second.invalid", serialized)

    def test_refresh_by_unknown_subscription_name_returns_human_guidance(self):
        response = get_agent_service().query("刷新 Missing RSS 订阅", owner="owner")
        self.assertEqual(response["result"]["status"], "unsupported")
        self.assertIn("没有找到名为《Missing》", response["result"]["summary"])
        self.assertIn("列出全部 RSS 订阅", response["result"]["suggestions"][0])

    def test_preview_is_local_only_and_sanitized(self):
        with patch.object(RSSEngine, "refresh") as refresh:
            result = preview_rss_subscription_refresh({"subscription_id": self.sid})
        self.assertTrue(result.ok)
        refresh.assert_not_called()
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for secret in ("Private RSS", "secret.example", "passkey", "RSS_SECRET", "PRIVATE_FILTER"):
            self.assertNotIn(secret, serialized)

    def test_confirm_executes_once_and_returns_aggregate_only(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.refresh_subscription", {"subscription_id": self.sid}, owner="owner"
        )
        with patch.object(RSSEngine, "refresh", return_value={
            "total": 7, "new": 3, "skipped": 2,
        }) as refresh:
            confirmed = service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args, (self.sid,))
        self.assertIn("expected_revision", refresh.call_args.kwargs)
        self.assertEqual(confirmed["result"]["status"], "completed")
        self.assertEqual(confirmed["result"]["data"], {
            "subscription_id": self.sid, "total": 7, "new": 3, "skipped": 2,
        })
        serialized = json.dumps(confirmed, ensure_ascii=False)
        for secret in ("Private RSS", "secret.example", "passkey", "RSS_SECRET", "PRIVATE_FILTER"):
            self.assertNotIn(secret, serialized)

        history = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"), limit=1
        )[0]
        self.assertEqual(history["tool_name"], "rss.refresh_subscription")
        self.assertEqual(history["status"], "completed")
        details = json.loads(history["safe_details"])
        self.assertEqual(
            {key: details[key] for key in ("new", "skipped", "subscription_id", "total")},
            {"new": 3, "skipped": 2, "subscription_id": self.sid, "total": 7},
        )
        self.assertEqual(details["contract_action"], "立即刷新 RSS 订阅")
        self.assertEqual(details["contract_risk"], "write")
        history_serialized = json.dumps(dict(history), ensure_ascii=False)
        for secret in ("Private RSS", "secret.example", "passkey", "RSS_SECRET", "PRIVATE_FILTER"):
            self.assertNotIn(secret, history_serialized)

    def test_single_refresh_surfaces_partial_source_failure(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.refresh_subscription", {"subscription_id": self.sid}, owner="owner"
        )
        with patch.object(RSSEngine, "refresh", return_value={
            "total": 7, "new": 3, "skipped": 2,
            "partial": True, "failed_sources": 1,
        }):
            confirmed = service.confirm(
                prepared["confirmation"]["confirmation_id"], owner="owner"
            )

        result = confirmed["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["failed_sources"], 1)
        self.assertIn("部分完成", result["summary"])
        self.assertIn("暂不可用源", result["suggestions"][0])

    def test_bulk_refresh_is_strict_local_preview_and_aggregates_partial_results(self):
        second_id = db.add_rss_subscription(
            "Mikan",
            "https://second.secret.invalid/rss?token=SECOND_SECRET",
        )
        arguments = {"subscription_ids": [self.sid, second_id]}
        self.assertEqual(rss_refresh_subscriptions_arguments(arguments), arguments)
        for scope in ("all_configured", "all_enabled"):
            with self.subTest(scope=scope):
                self.assertEqual(
                    rss_refresh_subscriptions_arguments({"scope": scope}),
                    {"scope": scope},
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
            {"scope": "all_configured", "subscription_ids": [self.sid]},
            {"scope": "all_enabled", "subscription_ids": [self.sid]},
        )
        for invalid in invalid_arguments:
            with self.subTest(invalid=invalid), self.assertRaises(AgentToolError):
                rss_refresh_subscriptions_arguments(invalid)

        tools = {item["name"]: item for item in get_agent_service().capabilities()["tools"]}
        spec = tools["rss.refresh_subscriptions"]
        self.assertEqual(spec["risk"], "write")
        self.assertTrue(spec["requires_confirmation"])
        self.assertFalse(spec["parameters"]["additionalProperties"])
        self.assertEqual(
            spec["parameters"]["properties"]["scope"]["enum"],
            ["all_configured", "all_enabled"],
        )

        with patch.object(RSSEngine, "refresh") as refresh:
            preview = preview_rss_subscriptions_refresh(arguments)
        refresh.assert_not_called()
        self.assertTrue(preview.ok)
        self.assertEqual(preview.status, "confirmation_required")
        self.assertEqual(preview.data["subscription_count"], 2)
        preview_serialized = json.dumps(preview.to_dict(), ensure_ascii=False)
        self.assertIn("Private RSS", preview_serialized)
        self.assertIn("Mikan", preview_serialized)
        for secret in ("secret.example", "RSS_SECRET", "second.secret.invalid", "SECOND_SECRET"):
            self.assertNotIn(secret, preview_serialized)

        service = get_agent_service()
        prepared = service.prepare("rss.refresh_subscriptions", arguments, owner="owner")
        with patch.object(
            RSSEngine,
            "refresh",
            side_effect=[
                {
                    "total": 7, "new": 3, "skipped": 2,
                    "partial": True, "failed_sources": 2,
                },
                {"error": RSS_REFRESH_BUSY_ERROR, "busy": True},
            ],
        ) as refresh:
            confirmed = service.confirm(
                prepared["confirmation"]["confirmation_id"], owner="owner"
            )

        self.assertEqual(refresh.call_count, 2)
        self.assertEqual([call.args[0] for call in refresh.call_args_list], [self.sid, second_id])
        self.assertTrue(all(call.kwargs.get("expected_revision") for call in refresh.call_args_list))
        result = confirmed["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            {key: result["data"][key] for key in (
                "requested", "refreshed", "failed",
                "partial_subscriptions", "failed_sources",
                "total", "new", "skipped"
            )},
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
            item for item in result["data"]["subscriptions"] if item["status"] == "partial"
        )
        self.assertEqual(partial_sub["failed_sources"], 2)
        self.assertIn("暂不可用源 2", result["summary"])
        self.assertTrue(any("暂不可用源" in item for item in result["suggestions"]))
        serialized = json.dumps(confirmed, ensure_ascii=False)
        self.assertIn("Private RSS", serialized)
        self.assertIn("Mikan", serialized)
        for secret in ("secret.example", "RSS_SECRET", "second.secret.invalid", "SECOND_SECRET"):
            self.assertNotIn(secret, serialized)

    def test_refresh_all_configured_includes_disabled_within_scope_limit(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_items")
        ids = [
            db.add_rss_subscription(
                f"Subscription {number}",
                f"https://feed-{number}.invalid/rss?token=SECRET-{number}",
            )
            for number in range(30)
        ]
        disabled_id = db.add_rss_subscription(
            "Disabled", "https://disabled.invalid/rss?token=DISABLED"
        )
        db.update_rss_subscription(disabled_id, {"enabled": False})

        service = get_agent_service()
        prepared = service.query("刷新全部 RSS 订阅", owner="owner")

        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(prepared["tool_call"]["name"], "rss.refresh_subscriptions")
        self.assertEqual(prepared["result"]["data"]["scope"], "all_configured")
        self.assertEqual(
            prepared["result"]["data"]["subscription_count"], len(ids) + 1
        )
        self.assertFalse(prepared["result"]["data"]["subscriptions_truncated"])
        self.assertEqual(
            len(prepared["result"]["data"]["subscriptions"]), len(ids) + 1
        )

        with patch.object(
            RSSEngine,
            "refresh",
            return_value={"total": 1, "new": 1, "skipped": 0},
        ) as refresh:
            confirmed = service.confirm(
                prepared["confirmation"]["confirmation_id"], owner="owner"
            )

        expected_ids = [*ids, disabled_id]
        self.assertEqual(refresh.call_count, len(expected_ids))
        self.assertCountEqual(
            [call.args[0] for call in refresh.call_args_list], expected_ids
        )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertEqual(
            confirmed["result"]["data"]["requested"], len(expected_ids)
        )
        self.assertEqual(
            confirmed["result"]["data"]["refreshed"], len(expected_ids)
        )
        serialized = json.dumps(confirmed, ensure_ascii=False)
        self.assertNotIn("disabled.invalid", serialized)
        self.assertNotIn("DISABLED", serialized)
        self.assertNotIn("SECRET-", serialized)

    def test_refresh_all_configured_rejects_scope_above_hard_limit(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_items")
        for number in range(33):
            subscription_id = db.add_rss_subscription(
                f"Subscription {number}", f"https://feed-{number}.invalid/rss"
            )
            if number % 2:
                db.update_rss_subscription(subscription_id, {"enabled": False})

        with patch.object(RSSEngine, "refresh") as refresh:
            response = get_agent_service().query(
                "刷新全部 RSS 订阅", owner="owner", present=False
            )

        self.assertEqual(response["result"]["status"], "unsupported")
        self.assertIn("当前范围包含 33 个订阅", response["result"]["summary"])
        self.assertIn("最多选择 32 个", response["result"]["suggestions"][0])
        self.assertNotIn("confirmation", response)
        refresh.assert_not_called()

    def test_legacy_all_enabled_scope_still_excludes_disabled_subscriptions(self):
        disabled_id = db.add_rss_subscription(
            "Disabled", "https://disabled.invalid/rss"
        )
        db.update_rss_subscription(disabled_id, {"enabled": False})

        prepared = get_agent_service().prepare(
            "rss.refresh_subscriptions", {"scope": "all_enabled"}, owner="owner"
        )

        self.assertEqual(prepared["result"]["data"]["scope"], "all_enabled")
        self.assertEqual(prepared["result"]["data"]["subscription_count"], 1)
        self.assertNotIn("Disabled", json.dumps(prepared, ensure_ascii=False))

    def test_refresh_all_configured_confirmation_stales_when_membership_changes(self):
        prepared = get_agent_service().query("刷新所有 RSS", owner="owner")
        db.add_rss_subscription("Later", "https://later.invalid/rss")

        with patch.object(RSSEngine, "refresh") as refresh, self.assertRaises(
            AgentToolError
        ) as stale:
            get_agent_service().confirm(
                prepared["confirmation"]["confirmation_id"], owner="owner"
            )

        self.assertEqual(stale.exception.code, "confirmation_stale")
        refresh.assert_not_called()

    def test_configuration_change_makes_confirmation_stale(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.refresh_subscription", {"subscription_id": self.sid}, owner="owner"
        )
        db.update_rss_subscription(self.sid, {"urls": "https://changed.invalid/rss"})
        with patch.object(RSSEngine, "refresh") as refresh, self.assertRaises(AgentToolError) as stale:
            service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")
        self.assertEqual(stale.exception.code, "confirmation_stale")
        refresh.assert_not_called()

    def test_busy_result_is_safe(self):
        service = get_agent_service()
        prepared = service.prepare(
            "rss.refresh_subscription", {"subscription_id": self.sid}, owner="owner"
        )
        with patch.object(RSSEngine, "refresh", return_value={
            "error": RSS_REFRESH_BUSY_ERROR, "busy": True,
        }):
            confirmed = service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")
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


class RSSRefreshAgentApiTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_entries")
            conn.execute("DELETE FROM rss_items")
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.sid = db.add_rss_subscription("Private", "https://secret.invalid/rss")
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
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

    def _login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post("/login", data={
            "username": "admin", "password": "123456", "csrf_token": token,
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def test_query_prepare_confirm_busy_and_replay(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        prepared = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": f"刷新 RSS 订阅 {self.sid}"},
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        body = prepared.json()
        self.assertEqual(body["mode"], "confirmation_required")
        confirmation_id = body["confirmation"]["confirmation_id"]
        with patch.object(RSSEngine, "refresh", return_value={
            "error": RSS_REFRESH_BUSY_ERROR, "busy": True,
        }):
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
            )
        self.assertEqual(confirmed.status_code, 409, confirmed.text)
        self.assertEqual(confirmed.json()["result"]["status"], "busy")
        replay = self.client.post(
            "/api/agent/actions/confirm",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "confirmation_id": confirmation_id},
        )
        self.assertEqual(replay.status_code, 409, replay.text)

    def test_session_list_then_contextual_refresh_can_be_discarded(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        session_id = "rssJourneySession001"

        with patch("app.agent.orchestrator.compose_tool_answer", return_value=None):
            listed = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"message": "列出全部 RSS 订阅", "session_id": session_id},
            )
            self.assertEqual(listed.status_code, 200, listed.text)
            listed_body = listed.json()
            self.assertEqual(
                listed_body["tool_call"]["name"], "rss.subscription_summaries"
            )
            self.assertEqual(listed_body["result"]["data"]["total"], 1)
            self.assertEqual(
                listed_body["result"]["data"]["items"][0]["subscription_number"],
                self.sid,
            )

            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"message": "刷新一下", "session_id": session_id},
            )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        prepared_body = prepared.json()
        self.assertEqual(prepared_body["mode"], "confirmation_required")
        self.assertEqual(
            prepared_body["confirmation"]["tool"], "rss.refresh_subscription"
        )
        confirmation_id = prepared_body["confirmation"]["confirmation_id"]

        discarded = self.client.post(
            "/api/agent/actions/confirm/discard",
            headers=headers,
            json={"confirmation_id": confirmation_id, "session_id": session_id},
        )
        self.assertEqual(discarded.status_code, 200, discarded.text)
        self.assertEqual(discarded.json(), {"discarded": True})

        with patch.object(RSSEngine, "refresh") as refresh:
            replay = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"confirmation_id": confirmation_id, "session_id": session_id},
            )
        self.assertEqual(replay.status_code, 409, replay.text)
        refresh.assert_not_called()

    def test_auth_csrf_strict_prepare_and_shared_limit(self):
        path = "/api/agent/actions/rss.refresh_subscription/prepare"
        payload = {"session_id": "test_session_identifier_0001", "arguments": {"subscription_id": self.sid}}
        self.assertEqual(self.client.post(path, json=payload).status_code, 401)
        csrf = self._login()
        self.assertEqual(self.client.post(path, json=payload).status_code, 403)
        headers = {"X-CSRF-Token": csrf}
        rejected = self.client.post(path, headers=headers, json={"session_id": "test_session_identifier_0001",
            "arguments": {"subscription_id": self.sid, "all": True},
        })
        self.assertEqual(rejected.status_code, 400, rejected.text)
        agent_rate_limiter.reset()
        with patch(
            "app.agent.llm_router.get",
            side_effect=lambda key, default="": (
                "0" if key == "AGENT_LLM_ENABLED" else default
            ),
        ):
            for _ in range(3):
                response = self.client.post(
                    "/api/agent/query", headers=headers,
                    json={"session_id": "test_session_identifier_0001", "message": f"刷新 RSS 订阅 {self.sid}"},
                )
                self.assertEqual(response.status_code, 200, response.text)
        limited = self.client.post(path, headers=headers, json=payload)
        self.assertEqual(limited.status_code, 429, limited.text)
