"""Agent 真实交互边界：时间语义、跨轮纠正与 Telegram 资源引用。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.agent.confirmation import ConfirmationStore, confirmation_reply_intent
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import (
    AgentOrchestrator,
    is_unsafe_qb_bulk_delete_request,
    resource_search_correction_request,
    unsupported_scheduled_action_request,
)
from app.agent.recent_resource_candidates import RecentResourceCandidateStore
from app.agent.registry import ToolRegistry
from app.agent.response_contract import build_response_contract
from app.agent.prompts.core import current_date_context
from app.bot.agent_adapter import (
    SQLiteTelegramAgentActionStore,
    TelegramAgentActionStore,
    _publish_telegram_callback_response,
    _telegram_reply_context,
    handle_agent_message,
)
from tests.support import isolated_test_database


def _identity(arguments: dict) -> dict:
    return dict(arguments)


def _read_tool(name: str, calls: list[dict]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        risk=RiskLevel.READ,
        parameters={},
        validator=_identity,
        handler=lambda arguments: (
            calls.append(dict(arguments))
            or ToolResult(True, "success", "搜索完成", data={"items": []})
        ),
    )


def _resource_result(*, title: str, prefix: str) -> ToolResult:
    items = []
    for index in range(1, 3):
        items.append({
            "result_id": f"{prefix}-resource-{index:04d}",
            "title": f"{title} candidate {index}",
            "site_id": "test",
            "site_name": "Test",
            "download_state": "ready",
            "download_kinds": ["magnet"],
            "size_text": f"{index}.0 GiB",
        })
    return ToolResult(
        True,
        "success",
        "找到资源",
        data={"query": title, "items": items},
    )


class ConfirmationPhraseEdgeTests(unittest.TestCase):
    def test_confirmation_and_cancellation_colloquialisms_are_exact(self):
        for value in ("确认吧", "确认执行吧", "执行吧", "可以执行"):
            with self.subTest(value=value):
                self.assertEqual(confirmation_reply_intent(value), "confirm")
        for value in ("先别执行", "别执行", "不用了", "撤销操作"):
            with self.subTest(value=value):
                self.assertEqual(confirmation_reply_intent(value), "cancel")
        for value in ("明天确认状态", "执行情况怎么样", "不用了吗"):
            with self.subTest(value=value):
                self.assertIsNone(confirmation_reply_intent(value))


class ScheduledActionGuardTests(unittest.TestCase):
    def test_unsupported_exact_schedule_is_detected_without_blocking_intervals(self):
        for message in (
            "六小时后刷新一次全部 RSS 订阅",
            "半小时后清理下载记录",
            "待会儿刷新 RSS",
            "周六晚上检查订阅",
            "下午 3 点刷新 RSS",
            "每周六晚上检查《光阴之外》有没有更新",
            "明天帮我下载《示例剧》",
            "9月8日晚上重启媒体反代",
        ):
            with self.subTest(message=message):
                self.assertTrue(unsupported_scheduled_action_request(message))

        for message in (
            "蜜柑 RSS 每 6 小时刷新",
            "将 STRM 定时同步改为每天 4 点",
            "把光鸭定时整理设为每天 02:30",
            "搜索《明天删除全部下载任务》的资源",
            "搜索 明天删除全部下载任务 的资源",
            "搜索 待会儿删除全部下载任务 有没有资源",
            "明天会自动刷新吗？",
            "RSS 当前多久刷新一次？",
            "稍后继续完成未完成的检查",
        ):
            with self.subTest(message=message):
                self.assertEqual(unsupported_scheduled_action_request(message), "")

    def test_query_does_not_silently_execute_delayed_request(self):
        agent = AgentOrchestrator(ToolRegistry(), ConfirmationStore())
        with patch.object(agent, "prepare") as prepare, patch.object(
            agent, "_query_with_model_tools"
        ) as model:
            response = agent.query(
                "六小时后刷新一次全部 RSS 订阅",
                owner="owner-a",
                present=False,
            )
        prepare.assert_not_called()
        model.assert_not_called()
        self.assertEqual(response["mode"], "clarification")
        self.assertIn("不会", response["result"]["summary"])
        self.assertIn("立即执行", response["result"]["summary"])

    def test_resource_search_does_not_hide_a_separate_delayed_action_clause(self):
        self.assertEqual(
            unsupported_scheduled_action_request(
                "搜索《光阴之外》的资源，然后明天帮我下载"
            ),
            "明天",
        )

    def test_prompt_exposes_an_absolute_local_date_only_as_relative_date_anchor(self):
        prompt = current_date_context(date(2026, 9, 2))
        self.assertIn("2026-09-02", prompt)
        self.assertIn("只用于", prompt)
        self.assertIn("时效性数据源", prompt)
        self.assertNotIn("已经上线", prompt.split("不代表", 1)[0])


class ResourceCorrectionEdgeTests(unittest.TestCase):
    def test_year_correction_reuses_latest_explicit_resource_title(self):
        context = [
            {"role": "user", "text": "搜索《暗芝居》的资源"},
            {"role": "assistant", "text": "找到 5 项候选"},
        ]
        self.assertEqual(
            resource_search_correction_request(
                "不是这个，换成 2024 年那个", context
            ),
            {"title": "暗芝居", "limit": 20, "year": 2024},
        )

    def test_correction_revokes_old_confirmation_and_runs_fresh_read(self):
        calls: list[dict] = []
        registry = ToolRegistry()
        registry.register(_read_tool("indexer.search_resources", calls))
        confirmations = ConfirmationStore(
            token_factory=lambda: "ticket-resource-correction-0001"
        )
        agent = AgentOrchestrator(registry, confirmations)
        confirmations.issue(
            owner="owner-a",
            tool_name="ingest.submit",
            arguments={"positions": [2], "target": "qb"},
        )
        context = [
            {"role": "user", "text": "搜索《暗芝居》的资源"},
            {"role": "assistant", "text": "找到候选"},
        ]

        with patch.object(agent, "_query_with_model_tools") as model:
            response = agent.query(
                "不是这个，换成 2024 年那个",
                owner="owner-a",
                conversation_context=context,
                trusted_conversation_context=True,
                present=False,
            )

        model.assert_not_called()
        self.assertEqual(calls, [{"title": "暗芝居", "limit": 20, "year": 2024}])
        self.assertEqual(response["tool_call"]["name"], "indexer.search_resources")
        self.assertEqual(confirmations.list_active_tickets(owner="owner-a"), [])
        self.assertIn("已取消", " ".join(response["result"]["suggestions"]))


class ResourceReplyBindingTests(unittest.TestCase):
    def test_exact_historical_snapshot_is_used_and_unbound_reply_fails_closed(self):
        recent = RecentResourceCandidateStore()
        old_search_id = recent.capture(
            owner="owner-a",
            result=_resource_result(title="Old", prefix="old-search"),
        )
        recent.capture(
            owner="owner-a",
            result=_resource_result(title="New", prefix="new-search"),
        )
        agent = AgentOrchestrator(
            ToolRegistry(),
            ConfirmationStore(),
            recent_resource_store=recent,
        )
        prepared_response = {
            "mode": "confirmation_required",
            "result": {"ok": True, "summary": "待确认"},
        }

        with patch.object(agent, "prepare", return_value=prepared_response) as prepare:
            response = agent.query(
                "第 2 个到 qB",
                owner="owner-a",
                reply_context={
                    "text": "旧候选资源",
                    "message_id": 77,
                    "resource_search_id": old_search_id,
                },
                present=False,
            )
        self.assertIs(response, prepared_response)
        arguments = prepare.call_args.args[1]
        self.assertEqual(arguments["positions"], [2])
        self.assertEqual(arguments["target"], "qb")
        self.assertEqual(arguments["search_id"], old_search_id)

        with patch.object(agent, "prepare") as unbound_prepare:
            unbound = agent.query(
                "第 2 个到 qB",
                owner="owner-a",
                reply_context={"text": "旧候选资源", "message_id": 77},
                present=False,
            )
        unbound_prepare.assert_not_called()
        self.assertEqual(unbound["mode"], "clarification")
        self.assertIn("没有可恢复", unbound["result"]["summary"])

    def test_reply_mapping_is_owner_bound_bounded_by_ttl_and_revocable(self):
        search_id = "rs_1234567890abcdef"
        for store_type in (TelegramAgentActionStore, SQLiteTelegramAgentActionStore):
            with self.subTest(store=store_type.__name__), isolated_test_database(
                f"{store_type.__name__}.db"
            ):
                now = [100.0]
                store = store_type(
                    clock=lambda: now[0], resource_reply_ttl_seconds=10
                )
                self.assertTrue(
                    store.bind_resource_reply(
                        owner="owner-a", message_id=41, search_id=search_id
                    )
                )
                self.assertEqual(
                    store.resource_reply_search_id(owner="owner-a", message_id=41),
                    search_id,
                )
                self.assertEqual(
                    store.resource_reply_search_id(owner="owner-b", message_id=41),
                    "",
                )
                now[0] = 111.0
                self.assertEqual(
                    store.resource_reply_search_id(owner="owner-a", message_id=41),
                    "",
                )
                now[0] = 120.0
                store.bind_resource_reply(
                    owner="owner-a", message_id=42, search_id=search_id
                )
                self.assertEqual(store.revoke_owner(owner="owner-a"), 1)
                self.assertEqual(
                    store.resource_reply_search_id(owner="owner-a", message_id=42),
                    "",
                )

    def test_resource_buttons_and_pagination_keep_the_exact_search_snapshot(self):
        search_id = "rs_1234567890abcdef"
        candidates = [
            {
                "result_id": f"resource-result-{index:04d}",
                "title": f"Candidate {index}",
                "site": "Test",
                "size": "1 GiB",
            }
            for index in range(1, 5)
        ]
        for store_type in (TelegramAgentActionStore, SQLiteTelegramAgentActionStore):
            with self.subTest(store=store_type.__name__), isolated_test_database(
                f"resource-search-{store_type.__name__}.db"
            ):
                store = store_type()
                interaction = store.create_resource_interaction(
                    owner="owner-a",
                    candidates=candidates,
                    search_id=search_id,
                )
                prepared = store.inspect(
                    interaction["items"][0]["qb_action_id"], owner="owner-a"
                )
                self.assertEqual(prepared["search_id"], search_id)
                page = store.resolve(
                    interaction["next_action_id"], owner="owner-a"
                )
                self.assertEqual(page["search_id"], search_id)

                with self.assertRaises(ValueError):
                    store.create_resource_interaction(
                        owner="owner-a",
                        candidates=candidates,
                        search_id="invalid search id",
                    )

    def test_telegram_reply_context_recovers_bound_search_id(self):
        store = TelegramAgentActionStore()
        search_id = "rs_1234567890abcdef"
        store.bind_resource_reply(
            owner="tg-owner", message_id=41, search_id=search_id
        )
        replied = SimpleNamespace(
            chat=SimpleNamespace(id=100),
            from_user=SimpleNamespace(is_bot=True),
            text="候选资源",
            caption=None,
            message_id=41,
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=100), reply_to_message=replied
        )
        with patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ):
            self.assertEqual(
                _telegram_reply_context(message, owner="tg-owner"),
                {
                    "text": "候选资源",
                    "message_id": 41,
                    "resource_search_id": search_id,
                },
            )

    def test_sent_telegram_resource_card_is_bound_after_successful_delivery(self):
        search_id = "rs_1234567890abcdef"
        owner = "tg:v1:100\x1f200"
        store = TelegramAgentActionStore(
            token_factory=iter(("resource-qb", "resource-guangya")).__next__
        )
        service = Mock()
        service.active_confirmation_count.return_value = 0
        service.begin_query_confirmation_epoch.return_value = 1
        service.invalidate_query_confirmation_epoch.return_value = 1
        service.query.return_value = {
            "mode": "tool_result",
            "response_contract": build_response_contract(
                task_kind="resource_search",
                presentation="resource_candidates",
                resource_candidates="primary",
            ),
            "tool_call": {"name": "indexer.search_resources", "elapsed_ms": 5},
            "result": {
                "ok": True,
                "status": "success",
                "summary": "找到 1 项资源",
                "suggestions": [],
                "evidence": [],
                "data": {
                    "search_id": search_id,
                    "items": [{
                        "result_id": "resource-result-0001",
                        "title": "Example.S01E01.1080p",
                        "site_name": "Test",
                        "size_text": "1.0 GiB",
                        "download_state": "ready",
                        "download_kinds": ["magnet"],
                    }],
                },
            },
        }
        history = Mock()
        history.session_generation.return_value = 0
        history.get_llm_context.return_value = []
        history.append_query_turn.return_value = True

        class Bot:
            def __init__(self) -> None:
                self.replies: list[tuple] = []

            def reply_to(self, message, text, **kwargs):
                self.replies.append((message, text, kwargs))
                return SimpleNamespace(
                    chat=SimpleNamespace(id=100), message_id=88
                )

        class Markup:
            def __init__(self, row_width=2) -> None:
                self.row_width = row_width
                self.buttons: list[object] = []

            def add(self, *buttons) -> None:
                self.buttons.extend(buttons)

        telebot = SimpleNamespace(types=SimpleNamespace(
            InlineKeyboardMarkup=Markup,
            InlineKeyboardButton=lambda text, callback_data: SimpleNamespace(
                text=text, callback_data=callback_data
            ),
        ))
        message = SimpleNamespace(
            text="搜索示例剧资源",
            chat=SimpleNamespace(id=100),
            from_user=SimpleNamespace(id=200),
            message_id=901,
            reply_to_message=None,
        )
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        bot = Bot()
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_agent_conversation_history_repository",
            return_value=history,
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ), patch(
            "app.bot.agent_adapter.agent_rate_limiter.allow", return_value=True
        ):
            self.assertTrue(handle_agent_message(bot, telebot, message))

        self.assertEqual(
            store.resource_reply_search_id(owner=owner, message_id=88), search_id
        )
        self.assertTrue(bot.replies)

    def test_callback_resource_card_is_bound_after_successful_delivery(self):
        search_id = "rs_1234567890abcdef"
        owner = "tg:v1:100\x1f200"
        store = TelegramAgentActionStore()
        recorded = Mock()

        class Coordinator:
            @staticmethod
            def is_current(_operation) -> bool:
                return True

            @staticmethod
            def publish_if_current(_operation, callback):
                return True, callback()

            @staticmethod
            def finalize_if_current(_operation, callback):
                return True, callback()

        class Bot:
            @staticmethod
            def reply_to(_message, _text, **_kwargs):
                return SimpleNamespace(
                    chat=SimpleNamespace(id=100), message_id=89
                )

        message = SimpleNamespace(chat=SimpleNamespace(id=100), message_id=7)
        with patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ), patch(
            "app.bot.agent_adapter._record_telegram_callback_conversation",
            recorded,
        ):
            self.assertTrue(
                _publish_telegram_callback_response(
                    Bot(),
                    message,
                    coordinator=Coordinator(),
                    operation=object(),
                    owner=owner,
                    response={"mode": "tool_result"},
                    history_generation=1,
                    history_message="查看资源",
                    fallback_summary="已找到资源",
                    prepare_output=lambda: ("资源候选", object()),
                    resource_search_id=search_id,
                )
            )

        self.assertEqual(
            store.resource_reply_search_id(owner=owner, message_id=89), search_id
        )
        recorded.assert_called_once()


class DownloadDeletionScopeTests(unittest.TestCase):
    def test_generic_download_task_bulk_delete_is_not_misrouted(self):
        self.assertTrue(
            is_unsafe_qb_bulk_delete_request(
                "把当前所有资源下载任务删掉但保留文件"
            )
        )
        self.assertTrue(
            is_unsafe_qb_bulk_delete_request("清空下载队列，但不要删除文件")
        )
        self.assertFalse(
            is_unsafe_qb_bulk_delete_request("移除下载任务《示例剧》并保留文件")
        )
        self.assertFalse(
            is_unsafe_qb_bulk_delete_request(
                "搜索《明天删除全部下载任务》的资源"
            )
        )
        self.assertFalse(
            is_unsafe_qb_bulk_delete_request(
                "搜索 明天删除全部下载任务 有没有资源"
            )
        )
        self.assertTrue(
            is_unsafe_qb_bulk_delete_request(
                "搜索《示例剧》的资源，然后清空下载队列"
            )
        )


if __name__ == "__main__":
    unittest.main()
