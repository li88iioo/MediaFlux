"""RSS 单订阅与订阅列表安全摘要契约。"""
from __future__ import annotations

import inspect
import json
from unittest.mock import ANY, Mock, patch

from app import database as db
from app.agent.models import ToolContext, ToolResult
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_rss_recent_activity_message,
    is_rss_subscription_summaries_message,
    rss_subscription_summary_name,
    rss_subscription_summary_request,
)
from app.agent.registry import AgentToolError
from app.agent.result_projection import project_agent_response_for_llm
from app.agent.rss_actions import (
    get_rss_recent_activity,
    get_rss_subscription_summary,
    list_rss_subscription_summaries,
    rss_subscription_summaries_arguments,
    rss_subscription_summary_arguments,
)
from app.agent.tools import build_tool_registry
from app.repositories import rss as rss_repository
from tests.support import IsolatedDatabaseTestCase

_SNAPSHOT = "2026-08-01 12:00:00"


def _clear_rss() -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM rss_entries")
        conn.execute("DELETE FROM rss_items")


def _set_entry(
    entry_id: int,
    *,
    status: str | None,
    processed: int = 0,
    created_at: str = _SNAPSHOT,
    submitted_at: str = "",
    processed_at: str | None = None,
) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE rss_entries SET status=?,processed=?,created_at=?,submitted_at=?,processed_at=? WHERE id=?",
            (status, processed, created_at, submitted_at, processed_at, entry_id),
        )


class RssSubscriptionSummaryTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        _clear_rss()

    def test_validators_are_strict(self):
        self.assertEqual(rss_subscription_summaries_arguments({}), {})
        self.assertEqual(
            rss_subscription_summary_arguments({"subscription_id": 12}),
            {"subscription_id": 12},
        )
        for invalid in (
            {"extra": 1},
            {"subscription_id": True},
            {"subscription_id": 0},
            {"subscription_id": "12"},
            {"subscription_id": 12, "extra": 1},
        ):
            with self.assertRaises(AgentToolError):
                rss_subscription_summary_arguments(invalid)

    def test_summary_never_returns_subscription_or_entry_secrets(self):
        subscription_name = "private-subscription-name"
        secret_values = (
            "rss-passkey-secret",
            "exclude-secret",
            "guid-secret",
            "payload-secret",
            "/volume/private/rss",
            "cloud-directory-secret",
        )
        subscription_id = db.add_rss_subscription(
            name=subscription_name,
            urls=f"https://example.invalid/feed?passkey={secret_values[0]}",
            exclude_keywords=secret_values[1],
            refresh_interval_minutes=30,
            qb_save_path=secret_values[4],
            gy_target_dir=secret_values[5],
        )
        entry_id = db.add_rss_entry(
            subscription_id,
            "private-entry-title",
            secret_values[2],
            payload=secret_values[3],
        )
        assert entry_id is not None
        _set_entry(
            entry_id,
            status="failed",
            created_at="2026-07-30 12:00:00",
        )

        with patch("app.agent.rss_actions.db.now", return_value=_SNAPSHOT):
            listed = list_rss_subscription_summaries({})
            detail = get_rss_subscription_summary({"subscription_id": subscription_id})

        self.assertTrue(listed.ok)
        self.assertTrue(detail.ok)
        self.assertEqual(detail.data["subscription_number"], subscription_id)
        self.assertEqual(detail.data["name"], subscription_name)
        self.assertEqual(listed.data["items"][0]["name"], subscription_name)
        self.assertEqual(detail.data["entry_counts"]["failed"], 1)
        serialized = json.dumps(
            {"listed": listed.to_dict(), "detail": detail.to_dict()},
            ensure_ascii=False,
        )
        for secret in (*secret_values, "private-entry-title"):
            self.assertNotIn(secret, serialized)

    def test_list_is_bounded_and_llm_projection_keeps_complete_items(self):
        for index in range(20):
            db.add_rss_subscription(
                name=f"secret-{index}",
                urls=f"https://example.invalid/{index}",
                enabled=1,
                refresh_interval_minutes=30,
            )
        with patch("app.agent.rss_actions.db.now", return_value=_SNAPSHOT):
            result = list_rss_subscription_summaries({})
        self.assertEqual(result.data["total"], 20)
        self.assertEqual(result.data["returned"], 16)
        self.assertTrue(result.data["truncated"])
        self.assertEqual(len(result.data["items"]), 16)

        projected = project_agent_response_for_llm({
            "tool_call": {"name": "rss.subscription_summaries"},
            "result": result.to_dict(),
        })
        assert projected is not None
        projected_items = projected["data"]["项目"]
        self.assertGreaterEqual(len(projected_items), 1)
        self.assertLessEqual(len(projected_items), 16)
        for item in projected_items:
            self.assertEqual(
                set(item),
                {
                    "订阅编号",
                    "订阅名称",
                    "已启用",
                    "调度状态",
                    "需关注数量",
                    "近 24 小时下载次数",
                },
            )
            self.assertNotEqual(item["调度状态"], "内部状态")

    def test_recent_activity_counts_only_successful_downloads_in_last_24_hours(self):
        subscription_id = db.add_rss_subscription(
            name="Mikan", urls="https://example.invalid/rss", enabled=1
        )
        cases = (
            ("recent", "downloaded", 1, "2026-08-01 11:00:00"),
            ("boundary", "downloaded", 1, "2026-07-31 12:00:00"),
            ("old", "downloaded", 1, "2026-07-31 11:59:59"),
            ("skipped", "skipped", 1, "2026-08-01 11:30:00"),
            ("unprocessed", "downloaded", 0, "2026-08-01 11:45:00"),
        )
        for title, status, processed, processed_at in cases:
            entry_id = db.add_rss_entry(
                subscription_id, title, f"guid-{title}", payload="magnet:?xt=test"
            )
            assert entry_id is not None
            _set_entry(
                entry_id,
                status=status,
                processed=processed,
                processed_at=processed_at,
            )

        with patch("app.agent.rss_actions.db.now", return_value=_SNAPSHOT):
            recent = get_rss_recent_activity({})
            listed = list_rss_subscription_summaries({})

        self.assertEqual(recent.status, "completed")
        self.assertEqual(recent.data["window_hours"], 24)
        self.assertEqual(recent.data["downloaded"], 2)
        self.assertEqual(recent.data["subscriptions"][0]["name"], "Mikan")
        self.assertEqual(recent.data["subscriptions"][0]["downloaded_last_24h"], 2)
        self.assertEqual(listed.data["items"][0]["downloaded_last_24h"], 2)
        for message in (
            "RSS 最近24小时下载了多少次",
            "RSS 24小时统计",
            "RSS近24h下载了多少次",
            "RSS过去24h下载数量",
            "RSS最近一天下载情况",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_rss_recent_activity_message(message))

    def test_recent_activity_total_is_not_truncated_by_subscription_breakdown(self):
        for index in range(20):
            subscription_id = db.add_rss_subscription(
                name=f"Feed {index:02d}",
                urls=f"https://example.invalid/{index}",
                enabled=1,
            )
            entry_id = db.add_rss_entry(
                subscription_id,
                f"entry-{index}",
                f"guid-{index}",
                payload="magnet:?xt=test",
            )
            assert entry_id is not None
            _set_entry(
                entry_id,
                status="downloaded",
                processed=1,
                processed_at="2026-08-01 11:00:00",
            )

        with patch("app.agent.rss_actions.db.now", return_value=_SNAPSHOT):
            recent = get_rss_recent_activity({})

        self.assertEqual(recent.data["downloaded"], 20)
        self.assertEqual(recent.data["subscriptions_returned"], 16)
        self.assertTrue(recent.data["subscriptions_truncated"])
        self.assertEqual(len(recent.data["subscriptions"]), 16)

    def test_list_query_limits_subscriptions_before_entry_aggregation(self):
        source = inspect.getsource(rss_repository._query_rss_subscription_safe_summaries)
        self.assertIn("WITH selected_items AS", source)
        self.assertIn("FROM selected_items i", source)
        self.assertIn('SELECT COUNT(*) FROM rss_items', source)
        self.assertNotIn("COUNT(*) OVER()", source)

    def test_natural_language_read_routes_are_precise(self):
        for message in (
            "查看 RSS 订阅 12 状态",
            "请检查 rss #12",
            "RSS id 12 摘要",
            "查看 RSS 订阅 12 的刷新周期",
            "查看 RSS 订阅 12 是否启用",
            "查看 RSS 订阅 12 概览",
        ):
            self.assertEqual(
                rss_subscription_summary_request(message),
                {"subscription_id": 12},
                message,
            )
        for message in (
            "RSS 订阅 0 状态",
            "RSS 订阅 1234567890 状态",
            "RSS 订阅 12",
            "查看订阅 12",
            "刷新 RSS 订阅 12",
            "停用 RSS 订阅 12",
        ):
            self.assertIsNone(rss_subscription_summary_request(message), message)

        for message in (
            "查看 RSS 订阅列表",
            "查看所有 RSS 订阅状态",
            "RSS 订阅摘要",
            "列出 RSS 订阅",
        ):
            self.assertTrue(is_rss_subscription_summaries_message(message), message)
        for message in (
            "查看 RSS 订阅 12 概览",
            "停用所有 RSS 订阅",
            "刷新全部 RSS 订阅",
        ):
            self.assertFalse(is_rss_subscription_summaries_message(message), message)

    def test_named_summary_parser_distinguishes_single_and_bulk_requests(self):
        cases = {
            "查看 Mikan RSS订阅摘要": "Mikan",
            "查询 RSS 订阅 Anime Feed 状态": "Anime Feed",
            "查看 Mikan RSS订阅概览": "Mikan",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(rss_subscription_summary_name(message), expected)
                self.assertFalse(is_rss_subscription_summaries_message(message))

        self.assertIsNone(rss_subscription_summary_name("列出 RSS 订阅摘要"))
        self.assertTrue(is_rss_subscription_summaries_message("列出 RSS 订阅摘要"))

    def test_named_summary_routes_unique_subscription_to_safe_detail(self):
        subscription_id = db.add_rss_subscription(
            name="Mikan",
            urls="https://example.invalid/mikan",
            enabled=1,
        )
        registry = Mock()
        registry.execute.return_value = (
            ToolResult(ok=True, status="healthy", summary="ok"),
            1,
        )
        agent = AgentOrchestrator(registry)

        response = agent.query("查看 Mikan RSS订阅摘要", present=False)

        self.assertEqual(response["tool_call"]["name"], "rss.get_subscription_summary")
        registry.execute.assert_called_once_with(
            "rss.get_subscription_summary",
            {"subscription_id": subscription_id},
            context=ToolContext(owner="", request_id=ANY),
        )

    def test_named_summary_fails_closed_when_name_is_ambiguous(self):
        registry = Mock()
        agent = AgentOrchestrator(registry)
        with patch(
            "app.agent.orchestrator.resolve_rss_subscription_name",
            return_value=type(
                "Resolution",
                (),
                {
                    "status": "ambiguous",
                    "subscription_id": None,
                    "candidate_ids": (11, 12),
                },
            )(),
        ):
            response = agent.query("查看 Mikan RSS订阅摘要", present=False)

        self.assertEqual(response["result"]["status"], "unsupported")
        self.assertIn("多个", response["result"]["summary"])
        self.assertIn("11", response["result"]["suggestions"][0])
        registry.execute.assert_not_called()

    def test_orchestrator_routes_detail_and_list_before_aggregate_diagnosis(self):
        registry = Mock()
        registry.execute.return_value = (
            ToolResult(ok=True, status="healthy", summary="ok"),
            1,
        )
        agent = AgentOrchestrator(registry)

        detail = agent.query("检查 RSS 订阅 12 状态")
        self.assertEqual(detail["tool_call"]["name"], "rss.get_subscription_summary")
        registry.execute.assert_called_once_with(
            "rss.get_subscription_summary",
            {"subscription_id": 12},
            context=ToolContext(owner="", request_id=ANY),
        )

        registry.reset_mock()
        registry.execute.return_value = (
            ToolResult(ok=True, status="completed", summary="ok"),
            1,
        )
        listed = agent.query("列出全部 RSS 订阅")
        self.assertEqual(listed["tool_call"]["name"], "rss.subscription_summaries")
        registry.execute.assert_called_once_with(
            "rss.subscription_summaries", {}, context=ToolContext(owner="", request_id=ANY)
        )

    def test_rss_followups_keep_the_previous_topic(self):
        registry = Mock()
        registry.execute.return_value = (
            ToolResult(ok=True, status="completed", summary="ok"),
            1,
        )
        agent = AgentOrchestrator(registry)
        context = [
            {"role": "assistant", "tool_name": "rss.diagnose"},
            {"role": "assistant", "text": "可以继续刷新或查看列表。"},
        ]

        listed = agent.query("列出列表", conversation_context=context)
        self.assertEqual(listed["tool_call"]["name"], "rss.subscription_summaries")

        subscription_id = db.add_rss_subscription(
            "Mikan", "https://mikan.invalid/rss"
        )
        with patch.object(
            agent, "_query_with_model_tools", return_value=None
        ) as planner, patch.object(
            agent, "prepare", return_value={"mode": "confirmation_required"}
        ) as prepare:
            refreshed = agent.query(
                "刷新一下", owner="owner", conversation_context=context
            )
        self.assertEqual(refreshed["mode"], "confirmation_required")
        planner.assert_not_called()
        prepare.assert_called_once_with(
            "rss.refresh_subscription",
            {"subscription_id": subscription_id},
            owner="owner",
        )

        unrelated = [{"role": "assistant", "tool_name": "downloads.diagnose_queue"}]
        registry.reset_mock()
        response = agent._rss_context_followup(
            "刷新一下", owner="owner", conversation_context=unrelated
        )
        self.assertIsNone(response)

        stale_topic = [
            {"role": "assistant", "tool_name": "rss.diagnose"},
            {"role": "user", "text": "下载队列现在怎么样"},
            {"role": "assistant", "text": "下载队列正常。"},
        ]
        with patch.object(agent, "prepare") as stale_prepare:
            stale_response = agent._rss_context_followup(
                "刷新一下", owner="owner", conversation_context=stale_topic
            )
        self.assertIsNone(stale_response)
        stale_prepare.assert_not_called()

    def test_tools_are_read_only_and_do_not_require_confirmation(self):
        capabilities = {
            item["name"]: item for item in build_tool_registry().capabilities()
        }
        for name in ("rss.subscription_summaries", "rss.get_subscription_summary"):
            self.assertEqual(capabilities[name]["risk"], "read")
            self.assertFalse(capabilities[name]["requires_confirmation"])


if __name__ == "__main__":
    import unittest

    unittest.main()
