from __future__ import annotations

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.operation_coordinator import reset_agent_operation_state_for_tests
from app.agent.orchestrator import AgentOrchestrator
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError, ToolRegistry
from app.bot.agent_adapter import (
    TelegramAgentActionStore,
    _render_resource_candidates,
    _stream_preview_html,
    _truncate_telegram_html,
    handle_agent_callback,
    handle_agent_guide,
    handle_agent_message,
    handle_agent_patrol_callback,
    handle_agent_reset,
    render_agent_response,
    telegram_agent_access,
    telegram_agent_owner,
)
from app.clients.openai_compatible import ProviderStreamError
from tests.support import isolated_test_database


class _Markup:
    def __init__(self, row_width=2):
        self.row_width = row_width
        self.buttons = []

    def add(self, *buttons):
        self.buttons.extend(buttons)


class _Telebot:
    types = SimpleNamespace(
        InlineKeyboardMarkup=_Markup,
        InlineKeyboardButton=lambda text, callback_data: SimpleNamespace(
            text=text, callback_data=callback_data
        ),
    )


class _Bot:
    def __init__(self):
        self.replies = []
        self.edits = []
        self.answers = []

    def reply_to(self, message, text, **kwargs):
        self.replies.append((message, text, kwargs))

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edits.append((text, chat_id, message_id, kwargs))

    def answer_callback_query(self, callback_id, text, **kwargs):
        self.answers.append((callback_id, text, kwargs))


class _InputRichMessage:
    def __init__(self, *, html=None, **kwargs):
        self.html = html


class _ReplyParameters:
    def __init__(self, *, message_id):
        self.message_id = message_id


class _RichTelebot:
    types = SimpleNamespace(
        InlineKeyboardMarkup=_Markup,
        InlineKeyboardButton=lambda text, callback_data: SimpleNamespace(
            text=text, callback_data=callback_data
        ),
        InputRichMessage=_InputRichMessage,
        ReplyParameters=_ReplyParameters,
    )


class _RichDraftBot(_Bot):
    def __init__(self):
        super().__init__()
        self.rich_drafts = []
        self.rich_messages = []

    def send_rich_message_draft(
        self, chat_id, draft_id, rich_message, message_thread_id=None
    ):
        self.rich_drafts.append(
            (chat_id, draft_id, rich_message, message_thread_id)
        )
        return True

    def send_rich_message(self, chat_id, rich_message, **kwargs):
        self.rich_messages.append((chat_id, rich_message, kwargs))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=77)


class _PlainDraftBot(_Bot):
    def __init__(self):
        super().__init__()
        self.drafts = []
        self.sent = []

    def send_message_draft(
        self, chat_id, draft_id, text, message_thread_id=None, **kwargs
    ):
        self.drafts.append((chat_id, draft_id, text, message_thread_id, kwargs))
        return True

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=78)


class _RichDraftFallbackBot(_PlainDraftBot):
    def send_rich_message_draft(
        self, chat_id, draft_id, rich_message, message_thread_id=None
    ):
        raise RuntimeError("rich drafts unavailable")

    def send_rich_message(self, chat_id, rich_message, **kwargs):
        raise AssertionError("failed rich draft must not use rich finalization")


class _RichFinalFallbackBot(_RichDraftBot):
    def __init__(self):
        super().__init__()
        self.sent = []

    def send_rich_message(self, chat_id, rich_message, **kwargs):
        raise RuntimeError("rich finalization unavailable")

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=79)


class _EditStreamBot(_Bot):
    def __init__(self):
        super().__init__()
        self.sent = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=80)


class _FailingEditStreamBot(_EditStreamBot):
    def __init__(self):
        super().__init__()
        self.deleted = []

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        raise RuntimeError("edit unavailable")

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True


class _CallbackEditFallbackBot(_Bot):
    def __init__(self, *, offline: bool = False):
        super().__init__()
        self.offline = offline
        self.edit_attempts = []
        self.sent = []

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edit_attempts.append((text, chat_id, message_id, kwargs))
        raise RuntimeError("edit unavailable")

    def send_message(self, chat_id, text, **kwargs):
        if self.offline:
            raise RuntimeError("send unavailable")
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=81)


class _RichDraftFalseBot(_PlainDraftBot):
    def send_rich_message_draft(
        self, chat_id, draft_id, rich_message, message_thread_id=None
    ):
        return False

    def send_rich_message(self, chat_id, rich_message, **kwargs):
        raise AssertionError("rejected rich draft must not use rich finalization")


class _PlainDraftFalseBot(_EditStreamBot):
    def send_message_draft(
        self, chat_id, draft_id, text, message_thread_id=None, **kwargs
    ):
        return False


class _PlainDraftUpdateFalseBot(_PlainDraftBot):
    def send_message_draft(
        self, chat_id, draft_id, text, message_thread_id=None, **kwargs
    ):
        self.drafts.append((chat_id, draft_id, text, message_thread_id, kwargs))
        return len(self.drafts) == 1


def _message(text="检查媒体库", *, reply_to_message=None, message_id=9):
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=100),
        from_user=SimpleNamespace(id=200),
        message_id=message_id,
        reply_to_message=reply_to_message,
    )


def _answer_response(summary: str) -> dict:
    return {
        "mode": "answer",
        "result": {
            "ok": True,
            "status": "healthy",
            "summary": summary,
            "suggestions": [],
            "evidence": [],
        },
    }


def _callback(data, *, callback_id="patrol-callback", chat_id=100, user_id=200):
    return SimpleNamespace(
        id=callback_id,
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=11,
        ),
    )


class TelegramAgentAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._database_context = isolated_test_database(
            "telegram-agent-adapter.db"
        )
        self._database_context.__enter__()
        self.agent_gate_patch = patch(
            "app.bot.agent_adapter.is_agent_enabled", return_value=True
        )
        try:
            self.agent_gate_patch.start()
        except Exception:
            self._database_context.__exit__(None, None, None)
            raise
        agent_rate_limiter.reset()
        reset_agent_operation_state_for_tests()

    def tearDown(self) -> None:
        try:
            self.agent_gate_patch.stop()
            agent_rate_limiter.reset()
            reset_agent_operation_state_for_tests()
        finally:
            self._database_context.__exit__(None, None, None)

    def test_access_is_fail_closed_and_requires_chat_and_user(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200, 201 invalid",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ):
            self.assertEqual(telegram_agent_access("100", "200"), "allowed")
            self.assertEqual(telegram_agent_access("100", "202"), "unauthorized")
            self.assertEqual(telegram_agent_access("101", "200"), "unauthorized")
        with patch("app.bot.agent_adapter.get", return_value=""):
            self.assertEqual(telegram_agent_access("100", "200"), "disabled")

    def test_owner_is_stable_and_rejects_non_numeric_identity(self):
        self.assertEqual(telegram_agent_owner("-100", "200"), "tg:v1:-100\x1f200")
        with self.assertRaises(ValueError):
            telegram_agent_owner("chat", "200")

    def test_action_store_is_owner_bound_one_time_and_invalidates_sibling(self):
        tokens = iter(["confirm-token", "cancel-token"])
        store = TelegramAgentActionStore(
            ttl_seconds=60,
            token_factory=lambda: next(tokens),
        )
        confirm = store.create(owner="owner-a", confirmation_id="ticket", action="confirm")
        cancel = store.create(owner="owner-a", confirmation_id="ticket", action="cancel")
        with self.assertRaises(ValueError):
            store.resolve(confirm, owner="owner-b")
        # 非所有者尝试不能消耗票据；合法所有者仍可确认，随后兄弟按钮失效。
        resolved = store.resolve(confirm, owner="owner-a")
        self.assertEqual(resolved, {"confirmation_id": "ticket", "action": "confirm"})
        with self.assertRaises(ValueError):
            store.resolve(cancel, owner="owner-a")

    def test_action_store_revokes_only_requested_owner(self):
        tokens = iter(["owner-a-confirm", "owner-a-cancel", "owner-b-confirm"])
        store = TelegramAgentActionStore(
            ttl_seconds=60,
            token_factory=lambda: next(tokens),
        )
        first = store.create(owner="owner-a", confirmation_id="ticket-a", action="confirm")
        second = store.create(owner="owner-a", confirmation_id="ticket-a", action="cancel")
        retained = store.create(owner="owner-b", confirmation_id="ticket-b", action="confirm")

        self.assertEqual(store.revoke_owner(owner="owner-a"), 2)
        self.assertEqual(store.revoke_owner(owner="owner-a"), 0)
        for action_id in (first, second):
            with self.assertRaises(ValueError):
                store.resolve(action_id, owner="owner-a")
        self.assertEqual(
            store.resolve(retained, owner="owner-b"),
            {"confirmation_id": "ticket-b", "action": "confirm"},
        )

    def test_workspace_actions_are_allowlisted_owner_bound_and_one_time(self):
        store = TelegramAgentActionStore(
            ttl_seconds=60,
            token_factory=lambda: "workspace-action",
        )
        action_id = store.create_workspace_action(
            owner="owner-a", action_key=" review_rss "
        )
        self.assertEqual(action_id, "workspace-action")
        self.assertEqual(
            store.inspect(action_id, owner="owner-a"),
            {
                "action": "invoke_workspace_action",
                "tool_name": "",
                "action_key": "review_rss",
            },
        )
        with self.assertRaises(ValueError):
            store.resolve(action_id, owner="owner-b")
        self.assertEqual(
            store.resolve(action_id, owner="owner-a"),
            {"action": "invoke_workspace_action", "action_key": "review_rss"},
        )
        with self.assertRaises(ValueError):
            store.resolve(action_id, owner="owner-a")
        with self.assertRaises(ValueError):
            store.create_workspace_action(
                owner="owner-a", action_key="client_controlled_tool"
            )

    def test_workspace_action_claim_can_restore_without_extending_expiry(self):
        now = [100.0]
        store = TelegramAgentActionStore(
            ttl_seconds=5,
            clock=lambda: now[0],
            token_factory=lambda: "workspace-claim",
        )
        action_id = store.create_workspace_action(
            owner="owner-a", action_key="review_rss"
        )
        claimed = store.claim_workspace_action(action_id, owner="owner-a")
        self.assertEqual(claimed["action_key"], "review_rss")
        self.assertEqual(claimed["expires_at"], 105.0)
        with self.assertRaises(ValueError):
            store.claim_workspace_action(action_id, owner="owner-a")
        self.assertTrue(
            store.restore_workspace_action(
                action_id,
                owner="owner-a",
                action_key="review_rss",
                expires_at=claimed["expires_at"],
            )
        )
        now[0] = 106.0
        with self.assertRaises(ValueError):
            store.claim_workspace_action(action_id, owner="owner-a")

    def test_resource_actions_are_owner_bound_grouped_and_expire(self):
        now = [100.0]
        tokens = iter(["resource-qb", "resource-guangya", "other-resource"])
        store = TelegramAgentActionStore(
            ttl_seconds=5,
            clock=lambda: now[0],
            token_factory=lambda: next(tokens),
        )
        qb = store.create_resource_prepare(
            owner="owner-a",
            result_id="resource_result_123456",
            target="qb",
            group_id="message-a",
        )
        guangya = store.create_resource_prepare(
            owner="owner-a",
            result_id="resource_result_123456",
            target="guangya",
            group_id="message-a",
        )
        other = store.create_resource_prepare(
            owner="owner-a",
            result_id="resource_result_654321",
            target="qb",
            group_id="message-b",
        )

        with self.assertRaises(ValueError):
            store.resolve(qb, owner="owner-b")
        self.assertEqual(
            store.resolve(qb, owner="owner-a"),
            {
                "action": "prepare_resource",
                "result_id": "resource_result_123456",
                "target": "qb",
            },
        )
        with self.assertRaises(ValueError):
            store.resolve(guangya, owner="owner-a")
        now[0] = 106.0
        with self.assertRaises(ValueError):
            store.resolve(other, owner="owner-a")

    def test_read_tool_actions_are_strict_owner_bound_and_one_time(self):
        store = TelegramAgentActionStore(
            ttl_seconds=60,
            token_factory=lambda: "missing-episode-read",
        )
        action_id = store.create_read_tool(
            owner="owner-a",
            tool_name="library.search_missing_episode_resources",
            arguments={
                "query": "The Show",
                "tmdb_id": "12345",
                "season": 2,
                "episode": 3,
                "as_of": "2026-08-01",
            },
        )
        self.assertEqual(action_id, "missing-episode-read")
        with self.assertRaises(ValueError):
            store.resolve(action_id, owner="owner-b")
        self.assertEqual(
            store.resolve(action_id, owner="owner-a"),
            {
                "action": "invoke_read_tool",
                "tool_name": "library.search_missing_episode_resources",
                "arguments": {
                    "query": "The Show",
                    "tmdb_id": "12345",
                    "season": 2,
                    "episode": 3,
                    "as_of": "2026-08-01",
                    "sites": [],
                    "limit": 20,
                },
            },
        )
        with self.assertRaises(ValueError):
            store.resolve(action_id, owner="owner-a")
        with self.assertRaises(ValueError):
            store.create_read_tool(
                owner="owner-a",
                tool_name="indexer.search_resources",
                arguments={"query": "The Show"},
            )

    def test_render_projects_only_safe_summary_and_guidance_without_internal_evidence(self):
        response = {
            "result": {
                "ok": True,
                "summary": "完成 https://private.example/a token=secret-value",
                "data": {"api_key": "must-not-leak", "path": "/volume/private/file"},
                "error": "raw-error-must-not-leak",
                "suggestions": ["查看 /volume/private/file", "继续核验"],
                "evidence": [
                    {
                        "source": "library",
                        "description": "来自 /volume/private/index",
                    }
                ],
            }
        }
        text = render_agent_response(response)
        self.assertNotIn("Agent 已完成", text)
        self.assertIn("[链接已隐藏]", text)
        self.assertNotIn("/volume/private", text)
        self.assertNotIn("must-not-leak", text)
        self.assertNotIn("raw-error", text)
        self.assertNotIn("secret-value", text)
        self.assertIn("<b>接下来可以</b>", text)
        self.assertIn("继续核验", text)
        self.assertNotIn("可修改后发送", text)
        self.assertNotIn("<b>依据</b>", text)
        self.assertNotIn("（library）", text)

    def test_failed_narrative_keeps_the_deterministic_error(self):
        text = render_agent_response({
            "result": {
                "ok": False,
                "status": "unavailable",
                "summary": "暂时无法完成检查",
                "error": "下载器当前不可用，请检查连接。",
            },
            "presentation": {
                "source": "llm",
                "kind": "narrative",
                "narrative": "当前没有取得可用结果。",
            },
        })
        self.assertIn("没能完成这次请求", text)
        self.assertIn("当前没有取得可用结果", text)
        self.assertIn("下载器当前不可用", text)

    def test_public_display_is_used_when_no_llm_narrative_exists(self):
        text = render_agent_response({
            "result": {
                "ok": False,
                "status": "private_internal_status",
                "summary": "旧摘要 private.tool_name",
                "error": "旧错误 token=private-value",
                "suggestions": [],
            },
            "display": {
                "version": 1,
                "status": {"key": "attention", "label": "需要留意", "tone": "warning"},
                "summary": "已检查下载队列，目前有 2 项需要留意。",
                "error": "下载器连接不稳定，请稍后重试。",
                "details": {},
                "guidance": [{
                    "label": "重新检查下载队列",
                    "prompt": "重新检查下载队列",
                    "kind": "read",
                }],
            },
        })

        self.assertIn("已检查下载队列", text)
        self.assertIn("下载器连接不稳定", text)
        self.assertIn("重新检查下载队列", text)
        self.assertIn("<b>需要留意</b>", text)
        self.assertNotIn("private.tool_name", text)
        self.assertNotIn("private_internal_status", text)
        self.assertNotIn("private-value", text)

    def test_llm_narrative_still_has_priority_over_public_display(self):
        text = render_agent_response({
            "result": {"ok": True, "status": "success", "summary": "旧摘要"},
            "display": {
                "version": 1,
                "status": {"key": "success", "label": "已完成", "tone": "good"},
                "summary": "确定性公开摘要",
                "error": "",
                "details": {},
                "guidance": [],
            },
            "presentation": {
                "source": "llm",
                "kind": "narrative",
                "narrative": "我已经检查完了，目前没有需要处理的问题。",
            },
        })

        self.assertIn("目前没有需要处理的问题", text)
        self.assertNotIn("确定性公开摘要", text)
        self.assertNotIn("旧摘要", text)

    def test_public_display_status_wins_over_conflicting_internal_status(self):
        text = render_agent_response({
            "result": {
                "ok": True,
                "status": "success",
                "summary": "内部摘要",
            },
            "display": {
                "version": 1,
                "status": {
                    "key": "unavailable",
                    "label": "媒体服务暂时不可用",
                    "tone": "error",
                },
                "summary": "暂时无法读取媒体库，请稍后重试。",
                "error": "连接检查没有通过。",
                "details": {},
                "guidance": [],
            },
        })

        self.assertIn("<b>媒体服务暂时不可用</b>", text)
        self.assertIn("暂时无法读取媒体库", text)
        self.assertIn("连接检查没有通过", text)
        self.assertNotIn("内部摘要", text)

    def test_telegram_html_truncation_preserves_controlled_markup(self):
        text = "<b>" + ("状态正常 &amp; 可继续 " * 80) + "</b>"
        truncated = _truncate_telegram_html(text, limit=180)

        self.assertLessEqual(len(truncated), 180)
        self.assertTrue(truncated.startswith("<b>"))
        self.assertTrue(truncated.endswith("（内容过长，已截断）"))
        self.assertEqual(truncated.count("<b>"), truncated.count("</b>"))
        self.assertNotRegex(truncated, r"&[^;]*$")

    def test_markdown_heavy_llm_narrative_is_rendered_as_short_paragraphs(self):
        text = render_agent_response({
            "result": {"ok": True, "status": "success", "summary": "确定性摘要"},
            "presentation": {
                "source": "llm",
                "kind": "narrative",
                "narrative": (
                    "**结论** 已完成检查。 **Agent 解读** 当前状态正常。"
                    " **关键数据与范围:** * 下载任务 16 项 * 当前速度 0 B/s"
                    " **下一步建议** 再检查长期没有速度的任务。"
                ),
                "guidance": [],
            },
        })
        self.assertNotIn("**", text)
        self.assertNotIn("Agent 解读", text)
        self.assertNotIn("<b>结论</b>", text)
        self.assertIn("已完成检查。", text)
        self.assertIn("\n\n当前状态正常。", text)
        self.assertIn("• 下载任务 16 项", text)
        self.assertIn("• 当前速度 0 B/s", text)

    def test_library_audit_projection_is_readable_and_redacted(self):
        response = {
            "tool_call": {"name": "library.audit_library_episodes"},
            "result": {
                "ok": True,
                "status": "updates_available",
                "summary": "全库缺集核对完成。",
                "data": {
                    "checked_series_count": 8,
                    "updates_available_count": 2,
                    "missing_episode_count": 3,
                    "unmapped_series_count": 1,
                    "inconclusive_count": 1,
                    "job_id": "job-secret-value",
                    "path": "/volume/private/library",
                    "findings": [
                        {
                            "title": "光阴之外",
                            "tmdb_id": 123456,
                            "missing_count": 2,
                            "missing_sample": [{"season": 1, "episode": 3}],
                        }
                    ],
                },
                "suggestions": [],
            },
        }

        text = render_agent_response(response)

        self.assertIn("已实际核对 8 部", text)
        self.assertIn("确认 2 部共缺 3 集", text)
        self.assertIn("《光阴之外》缺 2 集", text)
        self.assertIn("S01E03", text)
        self.assertIn("1 部缺少可靠 TMDB 映射", text)
        self.assertNotIn("job-secret-value", text)
        self.assertNotIn("123456", text)
        self.assertNotIn("/volume/private", text)

    def test_running_library_audit_does_not_claim_zero_means_no_missing(self):
        text = render_agent_response({
            "tool_call": {"name": "agent.job_status"},
            "result": {
                "ok": True,
                "status": "running",
                "summary": "后台巡检正在运行。",
                "data": {
                    "job_type": "library_episode_audit",
                    "task_status": "running",
                    "progress_current": 0,
                    "progress_total": 0,
                    "missing_episode_count": 0,
                },
                "suggestions": [],
            },
        })

        self.assertIn("正在统计媒体库范围", text)
        self.assertIn("完成前不会把零值解释为没有缺集", text)
        self.assertNotIn("0/0", text)
        self.assertNotIn("暂未发现", text)

    def test_library_audit_llm_narrative_has_priority_over_structured_projection(self):
        text = render_agent_response({
            "tool_call": {"name": "library.audit_library_episodes"},
            "result": {
                "ok": True,
                "status": "updates_available",
                "summary": "确定性摘要",
                "data": {
                    "checked_series_count": 8,
                    "updates_available_count": 2,
                    "missing_episode_count": 3,
                },
                "suggestions": [],
            },
            "presentation": {
                "source": "llm",
                "kind": "narrative",
                "narrative": "我已经核对了本地剧集库存，确认有两部剧需要补集。",
            },
        })

        self.assertIn("确认有两部剧需要补集", text)
        self.assertNotIn("<b>核对范围</b>", text)
        self.assertNotIn("确定性摘要", text)

    def test_stream_preview_uses_same_paragraph_and_list_projection(self):
        text = _stream_preview_html(
            "**结论** 已完成搜索。 **Agent 解读** 暂未找到结果。"
            " **下一步建议** * 检查片名 * 稍后重试"
        )
        self.assertNotIn("**", text)
        self.assertIn("<b>已完成搜索。</b>", text)
        self.assertIn("\n\n暂未找到结果。", text)
        self.assertIn("• 检查片名", text)
        self.assertIn("<code>▍</code>", text)

    def test_resource_candidate_message_keeps_candidates_outside_llm_narrative(self):
        response = {
            "result": {"ok": True, "summary": "已找到资源。"},
            "presentation": {
                "source": "llm",
                "kind": "narrative",
                "narrative": (
                    "**结论** 已完成多站搜索。 **Agent 解读** 找到两个候选。"
                    " **关键数据与范围** * 已检查 3 个站点 * 返回 2 项"
                ),
            },
        }
        candidates = [
            {
                "result_id": "r1",
                "site": "Nyaa",
                "size": "1.2 GB",
                "episode": "",
                "title": "光阴之外 S01E01",
            },
            {
                "result_id": "r2",
                "site": "Mikan",
                "size": "1.4 GB",
                "episode": "",
                "title": "光阴之外 S01E02",
            },
        ]
        text = _render_resource_candidates(response, candidates)
        self.assertNotIn("**", text)
        self.assertIn("<b>已找到资源。</b>", text)
        self.assertNotIn("找到两个候选", text)
        self.assertNotIn("已检查 3 个站点", text)
        self.assertIn("<b>候选资源</b>", text)
        self.assertEqual(text.count("光阴之外 S01E01"), 1)
        self.assertIn("\n\n<b>2.</b>", text)

    def test_resource_candidate_summary_prefers_public_display_over_llm_narrative(self):
        candidates = [{
            "result_id": "r1",
            "site": "Nyaa",
            "size": "1.2 GB",
            "episode": "",
            "title": "光阴之外 S01E01",
        }]
        response = {
            "result": {"ok": True, "summary": "旧摘要"},
            "display": {"summary": "公开摘要", "guidance": []},
        }
        self.assertIn("公开摘要", _render_resource_candidates(response, candidates))
        response["presentation"] = {
            "source": "llm",
            "kind": "narrative",
            "narrative": "模型说明",
        }
        text = _render_resource_candidates(response, candidates)
        self.assertIn("公开摘要", text)
        self.assertNotIn("模型说明", text)

    def test_selection_required_is_rendered_as_a_followup_not_a_failure(self):
        text = render_agent_response({
            "result": {
                "ok": False,
                "status": "selection_required",
                "summary": "第 2 个要下到哪里？请选择 qB、光鸭或两边。",
                "suggestions": ["第 2 个到 qB。", "第 2 个到光鸭。"],
                "evidence": [],
            }
        })
        self.assertIn("下到哪里", text)
        self.assertNotIn("没能完成", text)
        self.assertIn("<b>接下来可以</b>", text)

    def test_render_read_plan_is_compact_ordered_and_redacted(self):
        response = {
            "mode": "read_plan",
            "result": {
                "ok": False,
                "summary": "复合检查完成：1 项正常，1 项需要关注",
                "data": {
                    "steps": [
                        {
                            "tool_name": "workspace.health",
                            "arguments": {"api_key": "must-not-leak"},
                            "result": {"ok": True, "summary": "系统健康"},
                        },
                        {
                            "tool_name": "strm.status",
                            "result": {
                                "ok": False,
                                "summary": "路径 /volume/private 与 token=secret-value 不可用",
                                "error": "raw-error-must-not-leak",
                            },
                        },
                    ]
                },
                "suggestions": [],
            },
        }
        text = render_agent_response(response)
        self.assertIn("检查步骤", text)
        self.assertIn("1. <b>工作区健康</b> · 完成", text)
        self.assertIn("2. <b>STRM 状态</b> · 需关注", text)
        self.assertIn("[路径已隐藏]", text)
        self.assertIn("[凭据已隐藏]", text)
        self.assertNotIn("must-not-leak", text)
        self.assertNotIn("raw-error", text)
        self.assertNotIn("secret-value", text)

    def test_agent_guide_is_discoverable_without_querying_service(self):
        cases = (
            ({"TG_AGENT_ENABLED": "0"}, "Media Agent 当前未启用。请在控制台启用后再试。"),
            (
                {
                    "TG_AGENT_ENABLED": "1",
                    "TG_CHAT_ID": "100",
                    "TG_AGENT_ALLOWED_USER_IDS": "201",
                },
                "当前身份未获准使用 Media Agent。",
            ),
            (
                {
                    "TG_AGENT_ENABLED": "1",
                    "TG_CHAT_ID": "100",
                    "TG_AGENT_ALLOWED_USER_IDS": "200",
                },
                "Media Agent 已就绪",
            ),
        )
        for values, expected in cases:
            with self.subTest(expected=expected):
                bot = _Bot()
                with patch(
                    "app.bot.agent_adapter.get",
                    side_effect=lambda key, default="": values.get(key, default),
                ), patch("app.bot.agent_adapter.get_agent_service") as service:
                    handle_agent_guide(bot, _message("/agent"))
                service.assert_not_called()
                self.assertIn(expected, bot.replies[0][1])
                if expected == "Media Agent 已就绪":
                    self.assertIn("下载链接仍按原下载流程处理", bot.replies[0][1])
                    self.assertIn("/agent_reset", bot.replies[0][1])

    def test_agent_reset_is_owner_scoped_and_fail_closed(self):
        allowed = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        bot = _Bot()
        service = Mock()
        history = Mock()
        store = Mock()
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": allowed.get(key, default),
        ), patch("app.bot.agent_adapter.get_agent_service", return_value=service), patch(
            "app.bot.agent_adapter.get_agent_conversation_history_repository",
            return_value=history,
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store", return_value=store
        ):
            handle_agent_reset(bot, _message("/agent_reset"))
        service.reset_session.assert_called_once_with(owner="tg:v1:100\x1f200")
        history.delete_session.assert_called_once()
        store.revoke_owner.assert_called_once_with(owner="tg:v1:100\x1f200")
        self.assertEqual(
            bot.replies[0][1],
            "Agent 会话已重置。已清除当前会话上下文和待确认操作。",
        )

        for values, expected in (
            ({"TG_AGENT_ENABLED": "0"}, "Media Agent 当前未启用，无法重置会话。"),
            (
                {
                    "TG_AGENT_ENABLED": "1",
                    "TG_CHAT_ID": "100",
                    "TG_AGENT_ALLOWED_USER_IDS": "201",
                },
                "当前身份未获准使用 Media Agent。",
            ),
        ):
            with self.subTest(expected=expected):
                bot = _Bot()
                with patch(
                    "app.bot.agent_adapter.get",
                    side_effect=lambda key, default="": values.get(key, default),
                ), patch("app.bot.agent_adapter.get_agent_service") as blocked_service:
                    handle_agent_reset(bot, _message("/agent_reset"))
                blocked_service.assert_not_called()
                self.assertEqual(bot.replies[0][1], expected)

    def test_agent_reset_hides_internal_failures(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        for failure in (
            AgentToolError("secret internal error", code="confirmation_invalid"),
            RuntimeError("secret internal error"),
        ):
            with self.subTest(failure=type(failure).__name__):
                bot = _Bot()
                service = Mock()
                history = Mock()
                store = Mock()
                service.reset_session.side_effect = failure
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
                ):
                    handle_agent_reset(bot, _message("/agent_reset"))
                self.assertEqual(
                    bot.replies[0][1],
                    "Agent 会话暂时无法重置，请稍后重试。",
                )
                self.assertNotIn("secret internal error", bot.replies[0][1])
                store.revoke_owner.assert_called_once_with(
                    owner="tg:v1:100\x1f200"
                )
                history.delete_session.assert_not_called()

    def test_duplicate_inbound_message_is_processed_once(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "TG_AGENT_STREAMING_ENABLED": "0",
        }
        bot = _Bot()
        service = Mock()
        service.query.return_value = _answer_response("下载队列正常")
        history = Mock()
        message = _message("检查下载队列", message_id=610)

        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter._telegram_conversation_context",
            return_value=([], 1),
        ), patch(
            "app.bot.agent_adapter._record_telegram_conversation", history
        ):
            self.assertTrue(handle_agent_message(bot, _Telebot, message))
            self.assertTrue(handle_agent_message(bot, _Telebot, message))

        service.query.assert_called_once()
        history.assert_called_once()
        self.assertEqual(len(bot.replies), 1)
        self.assertIn("下载队列正常", bot.replies[0][1])

    def test_latest_message_revokes_stale_publication_and_history(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "TG_AGENT_STREAMING_ENABLED": "0",
        }
        old_started = threading.Event()
        release_old = threading.Event()

        class LatestWinsService:
            def query(self, message, **_kwargs):
                if message == "旧请求":
                    old_started.set()
                    if not release_old.wait(timeout=5):
                        raise TimeoutError("测试未释放旧 Telegram 请求")
                    return _answer_response("旧结果不应发布")
                return _answer_response("新结果已发布")

        bot = _Bot()
        history = Mock()
        service = LatestWinsService()
        try:
            with patch(
                "app.bot.agent_adapter.get",
                side_effect=lambda key, default="": values.get(key, default),
            ), patch(
                "app.bot.agent_adapter.get_agent_service", return_value=service
            ), patch(
                "app.bot.agent_adapter._telegram_conversation_context",
                return_value=([], 1),
            ), patch(
                "app.bot.agent_adapter._record_telegram_conversation", history
            ), ThreadPoolExecutor(max_workers=1) as pool:
                old_future = pool.submit(
                    handle_agent_message,
                    bot,
                    _Telebot,
                    _message("旧请求", message_id=620),
                )
                self.assertTrue(old_started.wait(timeout=3), "旧请求未开始执行")
                self.assertTrue(
                    handle_agent_message(
                        bot, _Telebot, _message("新请求", message_id=621)
                    )
                )
                release_old.set()
                self.assertTrue(old_future.result(timeout=5))
        finally:
            release_old.set()

        self.assertEqual(len(bot.replies), 1)
        self.assertIn("新结果已发布", bot.replies[0][1])
        self.assertNotIn("旧结果不应发布", bot.replies[0][1])
        history.assert_called_once()
        self.assertEqual(history.call_args.kwargs["message"], "新请求")

    def test_reset_during_query_revokes_stale_publication_and_history(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "TG_AGENT_STREAMING_ENABLED": "0",
        }
        query_started = threading.Event()
        release_query = threading.Event()

        class ResettableService:
            def __init__(self):
                self.reset_session = Mock()

            def query(self, _message, **_kwargs):
                query_started.set()
                if not release_query.wait(timeout=5):
                    raise TimeoutError("测试未释放 Telegram 请求")
                return _answer_response("重置前旧结果不应发布")

        bot = _Bot()
        service = ResettableService()
        history_record = Mock()
        history_repository = Mock()
        action_store = Mock()
        try:
            with patch(
                "app.bot.agent_adapter.get",
                side_effect=lambda key, default="": values.get(key, default),
            ), patch(
                "app.bot.agent_adapter.get_agent_service", return_value=service
            ), patch(
                "app.bot.agent_adapter._telegram_conversation_context",
                return_value=([], 1),
            ), patch(
                "app.bot.agent_adapter._record_telegram_conversation",
                history_record,
            ), patch(
                "app.bot.agent_adapter.get_agent_conversation_history_repository",
                return_value=history_repository,
            ), patch(
                "app.bot.agent_adapter.get_telegram_agent_action_store",
                return_value=action_store,
            ), ThreadPoolExecutor(max_workers=1) as pool:
                query_future = pool.submit(
                    handle_agent_message,
                    bot,
                    _Telebot,
                    _message("执行慢查询", message_id=630),
                )
                self.assertTrue(query_started.wait(timeout=3), "查询未开始执行")
                handle_agent_reset(bot, _message("/agent_reset", message_id=631))
                release_query.set()
                self.assertTrue(query_future.result(timeout=5))
        finally:
            release_query.set()

        service.reset_session.assert_called_once_with(owner="tg:v1:100\x1f200")
        history_repository.delete_session.assert_called_once()
        self.assertEqual(action_store.revoke_owner.call_count, 2)
        action_store.revoke_owner.assert_any_call(owner="tg:v1:100\x1f200")
        history_record.assert_not_called()
        self.assertEqual(len(bot.replies), 1)
        self.assertIn("会话已重置", bot.replies[0][1])
        self.assertNotIn("重置前旧结果不应发布", bot.replies[0][1])

    def test_reset_deletes_edit_placeholder_left_by_cancelled_query(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "TG_AGENT_STREAMING_ENABLED": "1",
        }
        query_started = threading.Event()
        release_query = threading.Event()

        class DeletableEditBot(_EditStreamBot):
            def __init__(self):
                super().__init__()
                self.deleted = []

            def delete_message(self, chat_id, message_id):
                self.deleted.append((chat_id, message_id))
                return True

        class ResettableService:
            def __init__(self):
                self.reset_session = Mock()

            def query(self, _message, **_kwargs):
                query_started.set()
                if not release_query.wait(timeout=5):
                    raise TimeoutError("测试未释放 Telegram 请求")
                return _answer_response("重置前旧结果不应发布")

        bot = DeletableEditBot()
        service = ResettableService()
        history_record = Mock()
        history_repository = Mock()
        action_store = Mock()
        try:
            with patch(
                "app.bot.agent_adapter.get",
                side_effect=lambda key, default="": values.get(key, default),
            ), patch(
                "app.bot.agent_adapter.get_agent_service", return_value=service
            ), patch(
                "app.bot.agent_adapter._telegram_conversation_context",
                return_value=([], 1),
            ), patch(
                "app.bot.agent_adapter._record_telegram_conversation",
                history_record,
            ), patch(
                "app.bot.agent_adapter.get_agent_conversation_history_repository",
                return_value=history_repository,
            ), patch(
                "app.bot.agent_adapter.get_telegram_agent_action_store",
                return_value=action_store,
            ), ThreadPoolExecutor(max_workers=1) as pool:
                query_future = pool.submit(
                    handle_agent_message,
                    bot,
                    _Telebot,
                    _message("执行带占位消息的慢查询", message_id=634),
                )
                self.assertTrue(query_started.wait(timeout=3), "查询未开始执行")
                handle_agent_reset(bot, _message("/agent_reset", message_id=635))
                release_query.set()
                self.assertTrue(query_future.result(timeout=5))
        finally:
            release_query.set()

        service.reset_session.assert_called_once_with(owner="tg:v1:100\x1f200")
        history_repository.delete_session.assert_called_once()
        history_record.assert_not_called()
        self.assertIn((100, 80), bot.deleted)
        self.assertNotIn("重置前旧结果不应发布", " ".join(item[1] for item in bot.replies))

    def test_reset_deletes_final_reply_that_finishes_after_lease_revocation(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "TG_AGENT_STREAMING_ENABLED": "0",
        }
        final_send_started = threading.Event()
        release_final_send = threading.Event()

        class LateReplyBot(_Bot):
            def __init__(self):
                super().__init__()
                self.deleted = []

            def reply_to(self, message, text, **kwargs):
                message_id = 902
                if "迟到旧结果" in text:
                    final_send_started.set()
                    if not release_final_send.wait(timeout=5):
                        raise TimeoutError("测试未释放 Telegram 最终回复")
                    message_id = 901
                self.replies.append((message, text, kwargs))
                return SimpleNamespace(
                    chat=SimpleNamespace(id=message.chat.id),
                    message_id=message_id,
                )

            def delete_message(self, chat_id, message_id):
                self.deleted.append((chat_id, message_id))
                return True

        class ResettableService:
            def __init__(self):
                self.reset_session = Mock()

            def query(self, _message, **_kwargs):
                return _answer_response("迟到旧结果")

        bot = LateReplyBot()
        service = ResettableService()
        history_record = Mock()
        history_repository = Mock()
        action_store = Mock()
        try:
            with patch(
                "app.bot.agent_adapter.get",
                side_effect=lambda key, default="": values.get(key, default),
            ), patch(
                "app.bot.agent_adapter.get_agent_service", return_value=service
            ), patch(
                "app.bot.agent_adapter._telegram_conversation_context",
                return_value=([], 1),
            ), patch(
                "app.bot.agent_adapter._record_telegram_conversation",
                history_record,
            ), patch(
                "app.bot.agent_adapter.get_agent_conversation_history_repository",
                return_value=history_repository,
            ), patch(
                "app.bot.agent_adapter.get_telegram_agent_action_store",
                return_value=action_store,
            ), ThreadPoolExecutor(max_workers=1) as pool:
                query_future = pool.submit(
                    handle_agent_message,
                    bot,
                    _Telebot,
                    _message("执行会产生迟到回复的查询", message_id=632),
                )
                self.assertTrue(
                    final_send_started.wait(timeout=3), "最终 Telegram 回复未开始"
                )
                handle_agent_reset(bot, _message("/agent_reset", message_id=633))
                release_final_send.set()
                self.assertTrue(query_future.result(timeout=5))
        finally:
            release_final_send.set()

        service.reset_session.assert_called_once_with(owner="tg:v1:100\x1f200")
        history_repository.delete_session.assert_called_once()
        history_record.assert_not_called()
        self.assertIn((100, 901), bot.deleted)
        self.assertTrue(any("会话已重置" in reply[1] for reply in bot.replies))

    def test_reset_is_not_blocked_by_slow_telegram_stream_start(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "TG_AGENT_STREAMING_ENABLED": "1",
        }
        stream_started = threading.Event()
        release_stream = threading.Event()
        reset_completed = threading.Event()
        bot = _Bot()
        service = Mock()
        service.query.return_value = _answer_response("旧请求不应继续执行")
        history_repository = Mock()
        action_store = Mock()

        def block_stream_start(*_args, **_kwargs):
            stream_started.set()
            if not release_stream.wait(timeout=5):
                raise TimeoutError("测试未释放 Telegram 草稿启动")
            return None

        def reset_agent() -> None:
            handle_agent_reset(bot, _message("/agent_reset", message_id=641))
            reset_completed.set()

        try:
            with patch(
                "app.bot.agent_adapter.get",
                side_effect=lambda key, default="": values.get(key, default),
            ), patch(
                "app.bot.agent_adapter.get_agent_service", return_value=service
            ), patch(
                "app.bot.agent_adapter._begin_agent_stream",
                side_effect=block_stream_start,
            ), patch(
                "app.bot.agent_adapter.get_agent_conversation_history_repository",
                return_value=history_repository,
            ), patch(
                "app.bot.agent_adapter.get_telegram_agent_action_store",
                return_value=action_store,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                message_future = pool.submit(
                    handle_agent_message,
                    bot,
                    _Telebot,
                    _message("执行慢查询", message_id=640),
                )
                self.assertTrue(stream_started.wait(timeout=3), "草稿启动未进入")
                reset_future = pool.submit(reset_agent)
                self.assertTrue(
                    reset_completed.wait(timeout=1),
                    "Telegram I/O 不应阻塞同 owner 会话重置",
                )
                release_stream.set()
                reset_future.result(timeout=3)
                self.assertTrue(message_future.result(timeout=5))
        finally:
            release_stream.set()

        service.reset_session.assert_called_once_with(owner="tg:v1:100\x1f200")
        service.query.assert_not_called()
        history_repository.delete_session.assert_called_once()
        self.assertEqual(len(bot.replies), 1)
        self.assertIn("会话已重置", bot.replies[0][1])

    def test_new_message_revokes_previous_confirmation_callback(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "TG_AGENT_STREAMING_ENABLED": "0",
        }
        bot = _Bot()
        service = Mock()
        service.query.return_value = _answer_response("新请求已处理")
        store = TelegramAgentActionStore(token_factory=lambda: "stale-confirm-action")
        action_id = store.create(
            owner="tg:v1:100\x1f200",
            confirmation_id="stale-confirmation-ticket",
            action="confirm",
        )

        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ):
            self.assertTrue(
                handle_agent_message(
                    bot,
                    _Telebot,
                    _message("开始新的检查", message_id=632),
                )
            )
            handle_agent_callback(
                bot,
                _callback(f"aga:{action_id}", callback_id="stale-confirm"),
                _Telebot,
            )

        service.confirm.assert_not_called()
        self.assertEqual(bot.answers[-1][1], "操作已过期或无效")
        self.assertTrue(bot.answers[-1][2].get("show_alert"))

    def test_message_uses_isolated_owner_and_opaque_confirmation_callbacks(self):
        bot = _Bot()
        service = Mock()
        service.query.return_value = {
            "mode": "confirmation_required",
            "result": {
                "ok": True,
                "summary": "准备提交下载任务",
                "suggestions": [],
                "evidence": [],
            },
            "confirmation": {
                "confirmation_id": "private-confirmation-ticket",
                "expires_in": 60,
                "contract": {
                    "version": 1,
                    "action": "提交资源下载",
                    "object": "你刚才选择的资源候选",
                    "impact": "会向 qBittorrent 创建下载任务。",
                    "reversibility": "可在下载器中暂停或删除任务。",
                    "preflight_at": "2026-08-09T12:34:56+08:00",
                    "risk": "danger",
                    "preflight_summary": "预检通过：下载器连接正常。",
                },
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.agent_adapter.get_agent_service", return_value=service):
            handled = handle_agent_message(bot, _Telebot, _message())
        self.assertTrue(handled)
        service.query.assert_called_once_with(
            "检查媒体库",
            owner="tg:v1:100\x1f200",
            query_tool_rate_identity="tg:v1:100\x1f200",
            llm_tool_rate_identity="tg:v1:100\x1f200",
        )
        markup = bot.replies[0][2]["reply_markup"]
        self.assertEqual(len(markup.buttons), 2)
        self.assertEqual(markup.buttons[0].text, "确认：提交资源下载")
        rendered = bot.replies[0][1]
        for expected in (
            "操作对象",
            "你刚才选择的资源候选",
            "影响",
            "撤销方式",
            "预检",
            "请在 60 秒内完成确认",
        ):
            self.assertIn(expected, rendered)
        callback_values = [button.callback_data for button in markup.buttons]
        self.assertTrue(all(value.startswith("aga:") for value in callback_values))
        self.assertTrue(all("private-confirmation-ticket" not in value for value in callback_values))

    def test_history_context_is_forwarded_when_llm_is_disabled(self):
        bot = _Bot()
        service = Mock()
        service.query.return_value = {
            "mode": "conversation",
            "result": {
                "ok": True,
                "summary": "我还记得上一轮，但本次走确定性路由。",
                "suggestions": [],
                "evidence": [],
            },
        }
        history = Mock()
        history.session_generation.return_value = 4
        expected_context = [
            {"role": "user", "text": "检查下载队列"},
            {"role": "assistant", "text": "下载队列正常"},
        ]
        history.get_llm_context.return_value = expected_context
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "AGENT_LLM_ENABLED": "0",
            "TG_AGENT_STREAMING_ENABLED": "0",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_agent_conversation_history_repository",
            return_value=history,
        ):
            handled = handle_agent_message(bot, _Telebot, _message("继续看一下"))

        self.assertTrue(handled)
        query_kwargs = service.query.call_args.kwargs
        self.assertEqual(query_kwargs["conversation_context"], expected_context)
        history.append_query_turn.assert_called_once()
        self.assertEqual(
            history.append_query_turn.call_args.kwargs["expected_generation"], 4
        )
        self.assertEqual(
            history.append_query_turn.call_args.kwargs["message"], "继续看一下"
        )

    def test_confirmation_callback_history_omits_internal_identifiers(self):
        bot = _Bot()
        service = Mock()
        service.confirm.return_value = {
            "mode": "confirmed_action",
            "tool_call": {"name": "rss.submit_pending_to_qb"},
            "confirmation": {"confirmation_id": "private-ticket"},
            "result": {
                "ok": True,
                "summary": "下载任务已提交",
                "suggestions": ["查看下载队列"],
                "evidence": [],
            },
        }
        history = Mock()
        history.session_generation.return_value = 9
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(token_factory=lambda: "opaque-action")
        action_id = store.create(
            owner="tg:v1:100\x1f200",
            confirmation_id="private-ticket",
            action="confirm",
        )
        call = _callback(f"aga:{action_id}", callback_id="safe-history")
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ), patch(
            "app.bot.agent_adapter.get_agent_conversation_history_repository",
            return_value=history,
        ):
            handle_agent_callback(bot, call)

        history.append_query_turn.assert_called_once()
        saved = history.append_query_turn.call_args.kwargs
        self.assertEqual(saved["message"], "确认执行待处理操作")
        self.assertEqual(saved["expected_generation"], 9)
        self.assertNotIn("tool_call", saved["response"])
        self.assertNotIn("confirmation", saved["response"])
        serialized = repr(saved)
        self.assertNotIn("private-ticket", serialized)
        self.assertNotIn("rss.submit_pending_to_qb", serialized)
        self.assertNotIn("opaque-action", serialized)

    def test_bot_reply_context_is_forwarded_without_changing_reply_target(self):
        bot = _Bot()
        service = Mock()
        service.query.return_value = {
            "mode": "clarification",
            "result": {
                "ok": True,
                "status": "clarification_required",
                "summary": "请先选择要检查的区域。",
                "suggestions": ["检查下载队列里的异常"],
                "evidence": [],
            },
        }
        quoted = SimpleNamespace(
            chat=SimpleNamespace(id=100),
            from_user=SimpleNamespace(is_bot=True),
            text="系统简报发现 67 项需要关注",
            caption=None,
            message_id=41,
        )
        message = _message("关注一下啥情况", reply_to_message=quoted)
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.agent_adapter.get_agent_service", return_value=service):
            handled = handle_agent_message(bot, _Telebot, message)

        self.assertTrue(handled)
        service.query.assert_called_once_with(
            "关注一下啥情况",
            owner="tg:v1:100\x1f200",
            query_tool_rate_identity="tg:v1:100\x1f200",
            llm_tool_rate_identity="tg:v1:100\x1f200",
            reply_context={
                "text": "系统简报发现 67 项需要关注",
                "message_id": 41,
            },
        )
        self.assertIs(bot.replies[0][0], message)

    def test_untrusted_reply_context_is_ignored(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        history = Mock()
        history.session_generation.return_value = 0
        history.get_llm_context.return_value = []
        history.append_query_turn.return_value = False
        for message_id, quoted in enumerate((
            SimpleNamespace(
                chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(is_bot=False),
                text="用户自己的消息",
                caption=None,
                message_id=40,
            ),
            SimpleNamespace(
                chat=SimpleNamespace(id=101),
                from_user=SimpleNamespace(is_bot=True),
                text="其他聊天里的 Bot 消息",
                caption=None,
                message_id=40,
            ),
        ), start=30):
            with self.subTest(quoted=quoted):
                bot = _Bot()
                service = Mock()
                service.query.return_value = {
                    "mode": "answer",
                    "result": {
                        "ok": True,
                        "summary": "检查完成",
                        "suggestions": [],
                        "evidence": [],
                    },
                }
                with patch(
                    "app.bot.agent_adapter.get",
                    side_effect=lambda key, default="": values.get(key, default),
                ), patch(
                    "app.bot.agent_adapter.get_agent_service", return_value=service
                ), patch(
                    "app.bot.agent_adapter.get_agent_conversation_history_repository",
                    return_value=history,
                ):
                    self.assertTrue(handle_agent_message(
                        bot,
                        _Telebot,
                        _message(
                            "继续",
                            reply_to_message=quoted,
                            message_id=message_id,
                        ),
                    ))
                service.query.assert_called_once_with(
                    "继续",
                    owner="tg:v1:100\x1f200",
                    query_tool_rate_identity="tg:v1:100\x1f200",
                    llm_tool_rate_identity="tg:v1:100\x1f200",
                )

    def test_message_prefers_native_rich_draft_stream_and_persists_final_message(self):
        bot = _RichDraftBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "result": {
                "ok": True,
                "summary": "巡检完成。" + ("媒体库状态正常，" * 50),
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ):
            self.assertTrue(
                handle_agent_message(bot, _RichTelebot, _message("检查媒体库"))
            )

        self.assertEqual(bot.replies, [])
        self.assertEqual(len(bot.rich_drafts), 2)
        self.assertEqual({item[1] for item in bot.rich_drafts}, {bot.rich_drafts[0][1]})
        self.assertIn("tg-thinking", bot.rich_drafts[0][2].html)
        self.assertEqual(len(bot.rich_messages), 1)
        final_chat, final_rich, final_kwargs = bot.rich_messages[0]
        self.assertEqual(final_chat, 100)
        self.assertIn("巡检完成", final_rich.html)
        self.assertNotIn("Agent 已完成", final_rich.html)
        self.assertEqual(final_kwargs["reply_parameters"].message_id, 9)

    def test_message_streams_provider_deltas_with_throttled_draft_and_exact_final(self):
        bot = _RichDraftBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "tool_call": {
                "name": "downloads.diagnose_queue",
                "arguments": {},
            },
            "result": {
                "ok": True,
                "summary": "下载队列状态正常",
                "suggestions": [],
                "evidence": [],
            },
        }
        history = Mock()

        async def provider_stream(*_args, **_kwargs):
            yield "下载队列"
            yield "状态正常，共 16 项任务。"

        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.stream_tool_answer", provider_stream
        ), patch(
            "app.bot.agent_adapter._record_telegram_conversation", history
        ), patch(
            "app.bot.agent_adapter._TELEGRAM_STREAM_UPDATE_INTERVAL_SECONDS", 60.0
        ):
            self.assertTrue(
                handle_agent_message(bot, _RichTelebot, _message("检查下载队列"))
            )

        service.query.assert_called_once_with(
            "检查下载队列",
            owner="tg:v1:100\x1f200",
            query_tool_rate_identity="tg:v1:100\x1f200",
            llm_tool_rate_identity="tg:v1:100\x1f200",
            present=False,
        )
        # 半句话不会发布；只保留初始 thinking 与完整句子的末尾 flush。
        self.assertEqual(len(bot.rich_drafts), 2)
        self.assertIn("tg-thinking", bot.rich_drafts[0][2].html)
        self.assertIn("下载队列状态正常", bot.rich_drafts[1][2].html)
        self.assertIn("共 16 项任务", bot.rich_drafts[1][2].html)
        self.assertEqual(len(bot.rich_messages), 1)
        self.assertIn("下载队列状态正常", bot.rich_messages[0][1].html)
        self.assertIn("16 项任务", bot.rich_messages[0][1].html)
        self.assertNotIn("正在整理结果", bot.rich_messages[0][1].html)
        history.assert_called_once()
        recorded = history.call_args.kwargs["response"]
        self.assertEqual(
            recorded["presentation"]["narrative"],
            "下载队列状态正常，共 16 项任务。",
        )

    def test_message_does_not_replay_after_partial_provider_stream_failure(self):
        bot = _RichDraftBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "tool_call": {
                "name": "downloads.diagnose_queue",
                "arguments": {},
            },
            "result": {
                "ok": True,
                "summary": "不应在中断后重放这个摘要",
                "suggestions": [],
                "evidence": [],
            },
        }
        history = Mock()

        async def broken_stream(*_args, **_kwargs):
            yield "已经生成一部分安全回答。"
            raise ProviderStreamError("upstream interrupted")

        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.stream_tool_answer", broken_stream
        ), patch(
            "app.bot.agent_adapter._record_telegram_conversation", history
        ):
            self.assertTrue(
                handle_agent_message(bot, _RichTelebot, _message("检查下载队列"))
            )

        self.assertEqual(len(bot.rich_messages), 1)
        final_html = bot.rich_messages[0][1].html
        self.assertIn("已经生成一部分安全回答", final_html)
        self.assertIn("生成中断", final_html)
        self.assertNotIn("不应在中断后重放这个摘要", final_html)
        history.assert_not_called()

    def test_message_never_publishes_unsafe_provider_delta(self):
        bot = _RichDraftBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "tool_call": {
                "name": "downloads.diagnose_queue",
                "arguments": {},
            },
            "result": {
                "ok": True,
                "summary": "不应在中断后重放这个摘要",
                "suggestions": [],
                "evidence": [],
            },
        }
        history = Mock()

        async def unsafe_stream(*_args, **_kwargs):
            yield "已经生成一部分安全回答。请访问 https://"
            yield "private.invalid/result"

        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.stream_tool_answer", unsafe_stream
        ), patch(
            "app.bot.agent_adapter._record_telegram_conversation", history
        ):
            self.assertTrue(
                handle_agent_message(bot, _RichTelebot, _message("检查下载队列"))
            )

        self.assertEqual(len(bot.rich_messages), 1)
        serialized = " ".join(
            item[2].html for item in bot.rich_drafts
        ) + " " + bot.rich_messages[0][1].html
        self.assertIn("已经生成一部分安全回答", serialized)
        self.assertIn("生成中断", serialized)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("private.invalid", serialized)
        self.assertNotIn("不应在中断后重放这个摘要", serialized)
        history.assert_not_called()

    def test_message_never_publishes_chinese_credential_delta(self):
        bot = _RichDraftBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "tool_call": {
                "name": "downloads.diagnose_queue",
                "arguments": {},
            },
            "result": {
                "ok": True,
                "summary": "不应在中断后重放这个摘要",
                "suggestions": [],
                "evidence": [],
            },
        }
        history = Mock()

        async def unsafe_stream(*_args, **_kwargs):
            yield "已经完成基础检查。"
            yield "凭证：秘密值"

        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.stream_tool_answer", unsafe_stream
        ), patch(
            "app.bot.agent_adapter._record_telegram_conversation", history
        ):
            self.assertTrue(
                handle_agent_message(bot, _RichTelebot, _message("检查下载队列"))
            )

        self.assertEqual(len(bot.rich_messages), 1)
        serialized = " ".join(
            item[2].html for item in bot.rich_drafts
        ) + " " + bot.rich_messages[0][1].html
        self.assertIn("已经完成基础检查", serialized)
        self.assertIn("生成中断", serialized)
        self.assertNotIn("秘密值", serialized)
        self.assertNotIn("不应在中断后重放这个摘要", serialized)
        history.assert_not_called()

    def test_message_uses_plain_draft_when_rich_draft_is_unavailable(self):
        bot = _PlainDraftBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "result": {
                "ok": True,
                "summary": "检查完成",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ):
            self.assertTrue(handle_agent_message(bot, _Telebot, _message()))

        self.assertEqual(bot.replies, [])
        self.assertEqual(len(bot.drafts), 2)
        self.assertEqual({item[1] for item in bot.drafts}, {bot.drafts[0][1]})
        self.assertEqual(bot.drafts[0][2], "")
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("检查完成", bot.sent[0][1])
        self.assertNotIn("Agent 已完成", bot.sent[0][1])
        self.assertEqual(bot.sent[0][2]["reply_to_message_id"], 9)

    def test_message_falls_back_to_plain_draft_when_rich_draft_rejects(self):
        bot = _RichDraftFallbackBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "result": {
                "ok": True,
                "summary": "检查完成",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ):
            self.assertTrue(handle_agent_message(bot, _RichTelebot, _message()))

        self.assertEqual(len(bot.drafts), 2)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.replies, [])

    def test_false_rich_draft_result_falls_back_to_plain_draft(self):
        bot = _RichDraftFalseBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "result": {"ok": True, "summary": "检查完成", "suggestions": [], "evidence": []},
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.agent_adapter.get_agent_service", return_value=service):
            self.assertTrue(handle_agent_message(bot, _RichTelebot, _message()))

        self.assertEqual(len(bot.drafts), 2)
        self.assertEqual(len(bot.sent), 1)

    def test_false_plain_draft_update_still_persists_final_message(self):
        bot = _PlainDraftUpdateFalseBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "result": {"ok": True, "summary": "检查完成", "suggestions": [], "evidence": []},
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.agent_adapter.get_agent_service", return_value=service):
            self.assertTrue(handle_agent_message(bot, _Telebot, _message()))

        self.assertEqual(len(bot.drafts), 2)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("检查完成", bot.sent[0][1])
        self.assertNotIn("Agent 已完成", bot.sent[0][1])

    def test_false_plain_draft_result_falls_back_to_editable_message(self):
        bot = _PlainDraftFalseBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "result": {"ok": True, "summary": "检查完成", "suggestions": [], "evidence": []},
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.agent_adapter.get_agent_service", return_value=service):
            self.assertTrue(handle_agent_message(bot, _Telebot, _message()))

        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(len(bot.edits), 2)
        self.assertIn("检查完成", bot.edits[-1][0])
        self.assertNotIn("Agent 已完成", bot.edits[-1][0])

    def test_rich_draft_finalization_falls_back_to_normal_message(self):
        bot = _RichFinalFallbackBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "result": {
                "ok": True,
                "summary": "检查完成",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ):
            self.assertTrue(
                handle_agent_message(bot, _RichTelebot, _message("检查媒体库"))
            )

        self.assertEqual(len(bot.rich_drafts), 2)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("检查完成", bot.sent[0][1])
        self.assertNotIn("Agent 已完成", bot.sent[0][1])
        self.assertEqual(bot.replies, [])

    def test_message_falls_back_to_editing_one_placeholder(self):
        bot = _EditStreamBot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "result": {
                "ok": True,
                "summary": "检查完成",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ):
            self.assertTrue(handle_agent_message(bot, _Telebot, _message()))

        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(len(bot.edits), 2)
        self.assertEqual({item[2] for item in bot.edits}, {80})
        self.assertIn("检查完成", bot.edits[-1][0])
        self.assertNotIn("Agent 已完成", bot.edits[-1][0])
        self.assertEqual(bot.replies, [])


    def test_final_edit_failure_sends_new_message_and_deletes_placeholder(self):
        bot = _FailingEditStreamBot()
        service = Mock()
        service.query.return_value = _answer_response("检查完成")
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ):
            self.assertTrue(handle_agent_message(bot, _Telebot, _message()))

        self.assertEqual(len(bot.sent), 2)
        self.assertIn("检查完成", bot.sent[-1][1])
        self.assertEqual(bot.deleted, [(100, 80)])

    def test_workspace_next_actions_render_only_safe_opaque_buttons(self):
        bot = _Bot()
        service = Mock()
        service.query.return_value = {
            "mode": "tool_result",
            "tool_call": {"name": "workspace.next_actions", "elapsed_ms": 8},
            "result": {
                "ok": True,
                "summary": "发现 1 个建议行动",
                "suggestions": [],
                "evidence": [],
                "data": {
                    "actions": [
                        {
                            "action_key": "review_rss",
                            "label": "检查 RSS 订阅",
                            "risk": "read",
                            "requires_confirmation": False,
                            "target_tool": "client.must.not.control",
                        },
                        {
                            "action_key": "unknown_action",
                            "label": "未知行动",
                            "risk": "read",
                            "requires_confirmation": False,
                        },
                        {
                            "action_key": "review_downloads",
                            "label": "不安全行动",
                            "risk": "write",
                            "requires_confirmation": True,
                        },
                    ]
                },
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(token_factory=lambda: "opaque-workspace")
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ):
            self.assertTrue(
                handle_agent_message(bot, _Telebot, _message("工作区下一步"))
            )

        markup = bot.replies[0][2]["reply_markup"]
        self.assertEqual([button.text for button in markup.buttons], ["检查 RSS 订阅"])
        self.assertEqual(markup.buttons[0].callback_data, "aga:opaque-workspace")
        self.assertNotIn("review_rss", markup.buttons[0].callback_data)
        self.assertNotIn("client.must.not.control", markup.buttons[0].callback_data)
        service.invoke_workspace_action.assert_not_called()

    def test_indexer_results_are_safely_projected_as_opaque_prepare_actions(self):
        bot = _Bot()
        service = Mock()
        service.query.return_value = {
            "mode": "tool_result",
            "tool_call": {"name": "indexer.search_resources", "elapsed_ms": 8},
            "result": {
                "ok": True,
                "summary": "找到 4 项资源",
                "suggestions": [],
                "evidence": [],
                "data": {
                    "api_key": "must-not-leak",
                    "items": [
                        {
                            "result_id": "resource_result_111111",
                            "site_name": "Nyaa",
                            "title": "示例资源一 magnet:?xt=private",
                            "size_text": "1.2 GB",
                            "download_state": "resolvable",
                            "download_kinds": ["magnet"],
                        },
                        {
                            "result_id": "resource_result_222222",
                            "site_name": "Mikan",
                            "title": "示例资源二",
                            "size_text": "900 MB",
                            "download_state": "ready",
                            "download_kinds": ["torrent"],
                        },
                        {
                            "result_id": "resource_result_333333",
                            "site_name": "Search only",
                            "title": "不可下载资源",
                            "download_state": "search_only",
                            "download_kinds": [],
                        },
                        {
                            "result_id": "bad",
                            "title": "无效句柄",
                            "download_state": "ready",
                            "download_kinds": ["magnet"],
                        },
                    ],
                },
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        tokens = iter(["one-qb", "one-gy", "two-qb", "two-gy"])
        store = TelegramAgentActionStore(token_factory=lambda: next(tokens))
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ):
            self.assertTrue(handle_agent_message(bot, _Telebot, _message("找资源")))

        text = bot.replies[0][1]
        markup = bot.replies[0][2]["reply_markup"]
        self.assertIn("候选资源", text)
        self.assertIn("示例资源一", text)
        self.assertIn("示例资源二", text)
        self.assertIn("[链接已隐藏]", text)
        self.assertNotIn("不可下载资源", text)
        self.assertNotIn("resource_result_", text)
        self.assertNotIn("must-not-leak", text)
        self.assertEqual([button.text for button in markup.buttons], [
            "1 · qB", "1 · 光鸭", "2 · qB", "2 · 光鸭",
        ])
        self.assertTrue(all(button.callback_data.startswith("aga:") for button in markup.buttons))
        self.assertFalse(any("resource_result_" in button.callback_data for button in markup.buttons))
        service.prepare.assert_not_called()
        service.confirm.assert_not_called()

    def test_episode_audit_offers_opaque_followup_and_nested_resource_actions(self):
        bot = _Bot()
        service = Mock()
        service.query.return_value = {
            "mode": "tool_result",
            "tool_call": {"name": "library.audit_episodes", "elapsed_ms": 8},
            "result": {
                "ok": True,
                "status": "updates_available",
                "summary": "发现 1 集已播但本地尚未收录",
                "suggestions": [],
                "evidence": [],
                "data": {
                    "resource_followups": [{
                        "tool": "library.search_missing_episode_resources",
                        "episode_label": "S02E03",
                        "arguments": {
                            "query": "The Show",
                            "tmdb_id": "12345",
                            "season": 2,
                            "episode": 3,
                            "as_of": "2026-08-01",
                        },
                    }],
                },
            },
        }
        service.invoke.return_value = {
            "mode": "tool_result",
            "tool_call": {
                "name": "library.search_missing_episode_resources",
                "elapsed_ms": 15,
            },
            "result": {
                "ok": True,
                "summary": "已确认缺失并找到 1 项资源",
                "suggestions": [],
                "evidence": [],
                "data": {
                    "verification": {"season": 2, "episode": 3},
                    "search": {
                        "items": [{
                            "result_id": "resource_result_123456",
                            "site_name": "Nyaa",
                            "title": "The Show S02E03 magnet:?xt=private",
                            "size_text": "1.2 GB",
                            "download_state": "ready",
                            "download_kinds": ["magnet"],
                        }],
                    },
                },
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        tokens = iter(["episode-read", "resource-qb", "resource-guangya"])
        store = TelegramAgentActionStore(token_factory=lambda: next(tokens))
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ):
            self.assertTrue(handle_agent_message(bot, _Telebot, _message("检查缺集")))
            followup_markup = bot.replies[0][2]["reply_markup"]
            self.assertEqual(
                [button.text for button in followup_markup.buttons],
                ["S02E03 找资源"],
            )
            self.assertEqual(
                followup_markup.buttons[0].callback_data,
                "aga:episode-read",
            )
            self.assertNotIn("The Show", followup_markup.buttons[0].callback_data)

            handle_agent_callback(
                bot,
                _callback(
                    followup_markup.buttons[0].callback_data,
                    callback_id="episode-followup",
                ),
                _Telebot,
            )

        service.invoke.assert_called_once_with(
            "library.search_missing_episode_resources",
            {
                "query": "The Show",
                "tmdb_id": "12345",
                "season": 2,
                "episode": 3,
                "as_of": "2026-08-01",
                "sites": [],
                "limit": 20,
            },
            owner="tg:v1:100\x1f200",
        )
        service.prepare.assert_not_called()
        service.confirm.assert_not_called()
        self.assertEqual(bot.edits, [])
        self.assertEqual(len(bot.replies), 2)
        result_text = bot.replies[1][1]
        result_markup = bot.replies[1][2]["reply_markup"]
        self.assertIn("S02E03", result_text)
        self.assertIn("[链接已隐藏]", result_text)
        self.assertNotIn("resource_result_123456", result_text)
        self.assertEqual(
            [button.callback_data for button in result_markup.buttons],
            ["aga:resource-qb", "aga:resource-guangya"],
        )
        self.assertEqual(bot.answers[-1][1], "正在查询，请稍候")

    def test_episode_audit_ignores_mismatched_or_unsafe_followups(self):
        bot = _Bot()
        service = Mock()
        service.query.return_value = {
            "mode": "tool_result",
            "tool_call": {"name": "library.audit_episodes", "elapsed_ms": 8},
            "result": {
                "ok": True,
                "summary": "发现缺集",
                "suggestions": [],
                "evidence": [],
                "data": {
                    "resource_followups": [
                        {
                            "tool": "library.search_missing_episode_resources",
                            "episode_label": "S02E04",
                            "arguments": {
                                "query": "The Show",
                                "tmdb_id": "12345",
                                "season": 2,
                                "episode": 3,
                                "as_of": "2026-08-01",
                            },
                        },
                        {
                            "tool": "indexer.submit_resource",
                            "episode_label": "S02E03",
                            "arguments": {"result_id": "private-result"},
                        },
                    ],
                },
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.agent_adapter.get_agent_service", return_value=service):
            self.assertTrue(handle_agent_message(bot, _Telebot, _message("检查缺集")))
        self.assertIsNone(bot.replies[0][2]["reply_markup"])
        service.invoke.assert_not_called()

    def test_read_tool_rate_limit_preserves_unconsumed_followup(self):
        bot = _Bot()
        service = Mock()
        service.invoke.return_value = {
            "mode": "tool_result",
            "tool_call": {
                "name": "library.search_missing_episode_resources",
                "elapsed_ms": 10,
            },
            "result": {
                "ok": False,
                "summary": "暂未找到可提交资源",
                "suggestions": [],
                "evidence": [],
                "data": {
                    "verification": {"season": 2, "episode": 3},
                    "search": {"items": []},
                },
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(token_factory=lambda: "episode-read")
        action_id = store.create_read_tool(
            owner="tg:v1:100\x1f200",
            tool_name="library.search_missing_episode_resources",
            arguments={
                "query": "The Show",
                "tmdb_id": "12345",
                "season": 2,
                "episode": 3,
                "as_of": "2026-08-01",
            },
        )
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ), patch(
            "app.bot.agent_adapter.allow_agent_tool",
            side_effect=[False, True],
        ) as limiter:
            handle_agent_callback(
                bot,
                _callback(f"aga:{action_id}", callback_id="limited"),
                _Telebot,
            )
            service.invoke.assert_not_called()
            self.assertEqual(bot.answers[-1][1], "请求过于频繁，请稍后重试")

            handle_agent_callback(
                bot,
                _callback(f"aga:{action_id}", callback_id="allowed"),
                _Telebot,
            )

        self.assertEqual(limiter.call_count, 2)
        service.invoke.assert_called_once()
        self.assertEqual(len(bot.replies), 1)
        self.assertEqual(bot.answers[-1][1], "正在查询，请稍候")


    def test_workspace_action_rate_limit_preserves_ticket_then_invokes_handoff(self):
        bot = _Bot()
        service = Mock()
        service.invoke_workspace_action.return_value = {
            "mode": "tool_result",
            "tool_call": {"name": "rss.diagnose", "elapsed_ms": 5},
            "result": {
                "ok": True,
                "summary": "RSS 状态已核对",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(token_factory=lambda: "workspace-action")
        action_id = store.create_workspace_action(
            owner="tg:v1:100\x1f200", action_key="review_rss"
        )
        resolution = {
            "action_key": "review_rss",
            "label": "检查 RSS 订阅",
            "target_tool": "rss.diagnose",
            "arguments": {},
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ), patch(
            "app.bot.agent_adapter.resolve_workspace_action_handoff",
            return_value=resolution,
        ), patch(
            "app.bot.agent_adapter.allow_agent_tool", side_effect=[False, True]
        ) as limiter:
            handle_agent_callback(
                bot,
                _callback(f"aga:{action_id}", callback_id="limited"),
                _Telebot,
            )
            service.invoke_workspace_action.assert_not_called()
            self.assertEqual(bot.answers[-1][1], "请求过于频繁，请稍后重试")

            handle_agent_callback(
                bot,
                _callback(f"aga:{action_id}", callback_id="allowed"),
                _Telebot,
            )

        self.assertEqual(limiter.call_count, 2)
        service.invoke_workspace_action.assert_called_once_with(
            "review_rss",
            owner="tg:v1:100\x1f200",
            rate_identity="",
        )
        self.assertEqual(len(bot.replies), 1)
        self.assertIn("RSS 状态已核对", bot.replies[0][1])
        self.assertEqual(bot.answers[-1][1], "正在执行，请稍候")

    def test_concurrent_workspace_callbacks_consume_target_budget_once(self):
        bot = _Bot()
        service = Mock()
        service.invoke_workspace_action.return_value = {
            "mode": "tool_result",
            "tool_call": {"name": "rss.diagnose", "elapsed_ms": 5},
            "result": {
                "ok": True,
                "summary": "RSS 状态已核对",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(token_factory=lambda: "workspace-race")
        action_id = store.create_workspace_action(
            owner="tg:v1:100\x1f200", action_key="review_rss"
        )
        resolution = {
            "action_key": "review_rss",
            "label": "检查 RSS 订阅",
            "target_tool": "rss.diagnose",
            "arguments": {},
        }
        barrier = threading.Barrier(2)

        def resolve_action(_arguments):
            barrier.wait(timeout=2)
            return resolution

        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ), patch(
            "app.bot.agent_adapter.resolve_workspace_action_handoff",
            side_effect=resolve_action,
        ), patch(
            "app.bot.agent_adapter.allow_agent_tool", return_value=True
        ) as limiter:
            calls = [
                _callback(f"aga:{action_id}", callback_id=f"race-{index}")
                for index in range(2)
            ]
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda call: handle_agent_callback(bot, call, _Telebot), calls))

        limiter.assert_called_once_with("tg:v1:100\x1f200", "rss.diagnose")
        service.invoke_workspace_action.assert_called_once_with(
            "review_rss", owner="tg:v1:100\x1f200", rate_identity=""
        )
        self.assertEqual(len(bot.replies), 1)
        self.assertEqual(
            sorted(answer[1] for answer in bot.answers),
            ["操作已过期或无效", "正在执行，请稍候"],
        )

    def test_revoked_callback_cannot_supersede_new_message_after_inspect(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "TG_AGENT_STREAMING_ENABLED": "0",
        }
        callback_inspected = threading.Event()
        release_callback = threading.Event()
        message_started = threading.Event()
        release_message = threading.Event()

        class BlockingInspectStore(TelegramAgentActionStore):
            def inspect(self, action_id, *, owner):
                metadata = super().inspect(action_id, owner=owner)
                callback_inspected.set()
                if not release_callback.wait(timeout=5):
                    raise TimeoutError("测试未释放 callback inspect")
                return metadata

        class LatestWinsService:
            def invoke(self, *_args, **_kwargs):
                raise AssertionError("已撤销 callback 不应执行工具")

            def query(self, _message, **_kwargs):
                message_started.set()
                if not release_message.wait(timeout=5):
                    raise TimeoutError("测试未释放新消息")
                return _answer_response("新消息结果已发布")

        owner = "tg:v1:100\x1f200"
        store = BlockingInspectStore(token_factory=lambda: "inspect-race")
        action_id = store.create_read_tool(
            owner=owner,
            tool_name="library.search_missing_episode_resources",
            arguments={
                "query": "The Show",
                "tmdb_id": "12345",
                "season": 2,
                "episode": 3,
                "as_of": "2026-08-01",
            },
        )
        bot = _Bot()
        service = LatestWinsService()
        message_history = Mock()
        try:
            with patch(
                "app.bot.agent_adapter.get",
                side_effect=lambda key, default="": values.get(key, default),
            ), patch(
                "app.bot.agent_adapter.get_agent_service", return_value=service
            ), patch(
                "app.bot.agent_adapter.get_telegram_agent_action_store",
                return_value=store,
            ), patch(
                "app.bot.agent_adapter.allow_agent_tool", return_value=True
            ), patch(
                "app.bot.agent_adapter._telegram_conversation_context",
                return_value=([], 1),
            ), patch(
                "app.bot.agent_adapter._record_telegram_conversation",
                message_history,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                callback_future = pool.submit(
                    handle_agent_callback,
                    bot,
                    _callback(f"aga:{action_id}", callback_id="inspect-old"),
                    _Telebot,
                )
                self.assertTrue(callback_inspected.wait(timeout=3))
                message_future = pool.submit(
                    handle_agent_message,
                    bot,
                    _Telebot,
                    _message("新请求", message_id=704),
                )
                self.assertTrue(message_started.wait(timeout=3))
                release_callback.set()
                callback_future.result(timeout=5)
                release_message.set()
                self.assertTrue(message_future.result(timeout=5))
        finally:
            release_callback.set()
            release_message.set()

        self.assertEqual(len(bot.replies), 1)
        self.assertIn("新消息结果已发布", bot.replies[0][1])
        self.assertIn("操作已过期或无效", [answer[1] for answer in bot.answers])
        message_history.assert_called_once()

    def test_read_callback_new_message_suppresses_stale_reply_and_history(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "TG_AGENT_STREAMING_ENABLED": "0",
        }
        callback_started = threading.Event()
        release_callback = threading.Event()

        class LatestWinsService:
            def invoke(self, _tool_name, _arguments, *, owner):
                self.owner = owner
                callback_started.set()
                if not release_callback.wait(timeout=5):
                    raise TimeoutError("测试未释放只读 callback")
                return _answer_response("旧 callback 结果不应发布")

            def query(self, message, **_kwargs):
                self.message = message
                return _answer_response("新消息结果已发布")

        owner = "tg:v1:100\x1f200"
        store = TelegramAgentActionStore(token_factory=lambda: "read-latest")
        action_id = store.create_read_tool(
            owner=owner,
            tool_name="library.search_missing_episode_resources",
            arguments={
                "query": "The Show",
                "tmdb_id": "12345",
                "season": 2,
                "episode": 3,
                "as_of": "2026-08-01",
            },
        )
        bot = _Bot()
        service = LatestWinsService()
        callback_history = Mock()
        message_history = Mock()
        try:
            with patch(
                "app.bot.agent_adapter.get",
                side_effect=lambda key, default="": values.get(key, default),
            ), patch(
                "app.bot.agent_adapter.get_agent_service", return_value=service
            ), patch(
                "app.bot.agent_adapter.get_telegram_agent_action_store",
                return_value=store,
            ), patch(
                "app.bot.agent_adapter.allow_agent_tool", return_value=True
            ), patch(
                "app.bot.agent_adapter._telegram_conversation_context",
                return_value=([], 1),
            ), patch(
                "app.bot.agent_adapter._record_telegram_callback_conversation",
                callback_history,
            ), patch(
                "app.bot.agent_adapter._record_telegram_conversation",
                message_history,
            ), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    handle_agent_callback,
                    bot,
                    _callback(f"aga:{action_id}", callback_id="read-old"),
                    _Telebot,
                )
                self.assertTrue(callback_started.wait(timeout=3))
                self.assertTrue(
                    handle_agent_message(
                        bot, _Telebot, _message("新请求", message_id=701)
                    )
                )
                release_callback.set()
                future.result(timeout=5)
        finally:
            release_callback.set()

        self.assertEqual(len(bot.replies), 1)
        self.assertIn("新消息结果已发布", bot.replies[0][1])
        self.assertNotIn("旧 callback 结果不应发布", bot.replies[0][1])
        callback_history.assert_not_called()
        message_history.assert_called_once()

    def test_workspace_callback_new_message_suppresses_stale_reply_and_history(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "TG_AGENT_STREAMING_ENABLED": "0",
        }
        callback_started = threading.Event()
        release_callback = threading.Event()

        class LatestWinsService:
            def invoke_workspace_action(self, _action_key, **_kwargs):
                callback_started.set()
                if not release_callback.wait(timeout=5):
                    raise TimeoutError("测试未释放工作区 callback")
                return _answer_response("旧工作区结果不应发布")

            def query(self, _message, **_kwargs):
                return _answer_response("新消息结果已发布")

        owner = "tg:v1:100\x1f200"
        store = TelegramAgentActionStore(token_factory=lambda: "workspace-latest")
        action_id = store.create_workspace_action(
            owner=owner, action_key="review_rss"
        )
        resolution = {
            "action_key": "review_rss",
            "label": "检查 RSS 订阅",
            "target_tool": "rss.diagnose",
            "arguments": {},
        }
        bot = _Bot()
        service = LatestWinsService()
        callback_history = Mock()
        try:
            with patch(
                "app.bot.agent_adapter.get",
                side_effect=lambda key, default="": values.get(key, default),
            ), patch(
                "app.bot.agent_adapter.get_agent_service", return_value=service
            ), patch(
                "app.bot.agent_adapter.get_telegram_agent_action_store",
                return_value=store,
            ), patch(
                "app.bot.agent_adapter.resolve_workspace_action_handoff",
                return_value=resolution,
            ), patch(
                "app.bot.agent_adapter.allow_agent_tool", return_value=True
            ), patch(
                "app.bot.agent_adapter._telegram_conversation_context",
                return_value=([], 1),
            ), patch(
                "app.bot.agent_adapter._record_telegram_callback_conversation",
                callback_history,
            ), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    handle_agent_callback,
                    bot,
                    _callback(f"aga:{action_id}", callback_id="workspace-old"),
                    _Telebot,
                )
                self.assertTrue(callback_started.wait(timeout=3))
                self.assertTrue(
                    handle_agent_message(
                        bot, _Telebot, _message("新请求", message_id=702)
                    )
                )
                release_callback.set()
                future.result(timeout=5)
        finally:
            release_callback.set()

        self.assertEqual(len(bot.replies), 1)
        self.assertIn("新消息结果已发布", bot.replies[0][1])
        self.assertNotIn("旧工作区结果不应发布", bot.replies[0][1])
        callback_history.assert_not_called()

    def test_patrol_callback_new_message_suppresses_stale_reply_and_history(self):
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
            "TG_AGENT_STREAMING_ENABLED": "0",
        }
        callback_started = threading.Event()
        release_callback = threading.Event()

        class LatestWinsService:
            def query(self, message, **_kwargs):
                if message == "查看最近全库巡检结果":
                    callback_started.set()
                    if not release_callback.wait(timeout=5):
                        raise TimeoutError("测试未释放巡检 callback")
                    return _answer_response("旧巡检结果不应发布")
                return _answer_response("新消息结果已发布")

        bot = _Bot()
        service = LatestWinsService()
        callback_history = Mock()
        try:
            with patch(
                "app.bot.agent_adapter.get",
                side_effect=lambda key, default="": values.get(key, default),
            ), patch(
                "app.bot.agent_adapter.get_agent_service", return_value=service
            ), patch(
                "app.bot.agent_adapter._telegram_conversation_context",
                return_value=([], 1),
            ), patch(
                "app.bot.agent_adapter._record_telegram_callback_conversation",
                callback_history,
            ), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    handle_agent_patrol_callback,
                    bot,
                    _callback("agp:summary", callback_id="patrol-old"),
                    _Telebot,
                )
                self.assertTrue(callback_started.wait(timeout=3))
                self.assertTrue(
                    handle_agent_message(
                        bot, _Telebot, _message("新请求", message_id=703)
                    )
                )
                release_callback.set()
                future.result(timeout=5)
        finally:
            release_callback.set()

        self.assertEqual(len(bot.replies), 1)
        self.assertIn("新消息结果已发布", bot.replies[0][1])
        self.assertNotIn("旧巡检结果不应发布", bot.replies[0][1])
        callback_history.assert_not_called()

    def test_patrol_summary_callback_is_owner_bound_and_replies_without_editing(self):
        bot = _Bot()
        service = Mock()
        service.query.return_value = {
            "mode": "tool_result",
            "tool_call": {"name": "library.patrol_status", "elapsed_ms": 4},
            "result": {
                "ok": True,
                "summary": "最近巡检发现 1 部剧集存在已播缺集。",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ):
            handle_agent_patrol_callback(bot, _callback("agp:summary"), _Telebot)

        service.query.assert_called_once_with(
            "查看最近全库巡检结果",
            owner="tg:v1:100\x1f200",
            query_tool_rate_identity="tg:v1:100\x1f200",
            llm_tool_rate_identity="tg:v1:100\x1f200",
        )
        service.prepare.assert_not_called()
        service.confirm.assert_not_called()
        self.assertEqual(len(bot.replies), 1)
        self.assertEqual(bot.edits, [])
        self.assertIn("最近巡检发现", bot.replies[0][1])
        self.assertEqual(bot.answers[-1][1], "正在查询，请稍候")

    def test_patrol_resources_callback_refreshes_snapshot_then_offers_safe_actions(self):
        bot = _Bot()
        service = Mock()
        service.query.side_effect = [
            {
                "mode": "tool_result",
                "tool_call": {"name": "library.patrol_status", "elapsed_ms": 4},
                "result": {
                    "ok": True,
                    "summary": "最近巡检发现缺集。",
                    "suggestions": [],
                    "evidence": [],
                },
            },
            {
                "mode": "tool_result",
                "tool_call": {
                    "name": "library.search_missing_season_resources",
                    "elapsed_ms": 8,
                },
                "result": {
                    "ok": True,
                    "summary": "找到 1 项资源",
                    "suggestions": [],
                    "evidence": [],
                    "data": {
                        "episodes": [{
                            "season": 2,
                            "episode": 3,
                            "search": {
                                "items": [{
                                    "result_id": "resource_result_123456",
                                    "site_name": "Nyaa",
                                    "title": "The Show S02E03 magnet:?xt=private",
                                    "size_text": "1.2 GB",
                                    "download_state": "ready",
                                    "download_kinds": ["magnet"],
                                }],
                            },
                        }],
                    },
                },
            },
        ]
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(
            token_factory=iter(["resource-qb", "resource-guangya"]).__next__
        )
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ):
            handle_agent_patrol_callback(bot, _callback("agp:resources"), _Telebot)

        owner = "tg:v1:100\x1f200"
        self.assertEqual(service.query.call_args_list, [
            unittest.mock.call(
                "查看最近全库巡检结果",
                owner=owner,
                query_tool_rate_identity=owner,
                llm_tool_rate_identity=owner,
            ),
            unittest.mock.call(
                "把刚才巡检发现的缺集找资源",
                owner=owner,
                query_tool_rate_identity=owner,
                llm_tool_rate_identity=owner,
            ),
        ])
        service.prepare.assert_not_called()
        service.confirm.assert_not_called()
        text = bot.replies[0][1]
        markup = bot.replies[0][2]["reply_markup"]
        self.assertIn("候选资源", text)
        self.assertIn("S02E03", text)
        self.assertIn("[链接已隐藏]", text)
        self.assertNotIn("resource_result_123456", text)
        self.assertEqual(
            [button.callback_data for button in markup.buttons],
            ["aga:resource-qb", "aga:resource-guangya"],
        )
        self.assertEqual(bot.edits, [])
        self.assertEqual(bot.answers[-1][1], "正在查询，请稍候")

    def test_patrol_callback_fails_closed_for_unknown_or_unauthorized_action(self):
        bot = _Bot()
        service = Mock()
        allowed = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": allowed.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ):
            handle_agent_patrol_callback(bot, _callback("agp:unknown"), _Telebot)
            handle_agent_patrol_callback(
                bot,
                _callback("agp:summary", callback_id="unauthorized", user_id=201),
                _Telebot,
            )
        service.query.assert_not_called()
        self.assertEqual(bot.replies, [])
        self.assertEqual(
            [answer[1] for answer in bot.answers],
            ["操作已过期或无效", "操作已过期或无效"],
        )


    def test_patrol_resources_callback_reports_when_snapshot_has_no_followup(self):
        bot = _Bot()
        service = Mock()
        service.query.side_effect = [
            {
                "mode": "tool_result",
                "result": {
                    "ok": True,
                    "summary": "本次巡检未发现已播缺集。",
                    "suggestions": [],
                    "evidence": [],
                },
            },
            {
                "mode": "tool_result",
                "result": {
                    "ok": False,
                    "summary": "当前没有可继续的全库巡检结果。",
                    "suggestions": [],
                    "evidence": [],
                },
            },
        ]
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ):
            handle_agent_patrol_callback(bot, _callback("agp:resources"), _Telebot)
        self.assertEqual(service.query.call_count, 2)
        self.assertIn("当前没有可继续", bot.replies[0][1])
        self.assertEqual(bot.answers[-1][1], "正在查询，请稍候")

    def test_patrol_callback_rate_limits_before_agent_query(self):
        bot = _Bot()
        service = Mock()
        service.query.return_value = {
            "mode": "tool_result",
            "result": {
                "ok": True,
                "summary": "巡检摘要",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter._TELEGRAM_CALLBACK_LIMIT_PER_MINUTE", 1
        ):
            handle_agent_patrol_callback(bot, _callback("agp:summary"), _Telebot)
            handle_agent_patrol_callback(
                bot,
                _callback("agp:summary", callback_id="second"),
                _Telebot,
            )
        self.assertEqual(service.query.call_count, 1)
        self.assertEqual(bot.answers[-1][1], "请求过于频繁，请稍后重试")

    def test_resource_callback_only_prepares_then_reuses_confirmation_gate(self):
        bot = _Bot()
        service = Mock()
        service.prepare.return_value = {
            "mode": "confirmation_required",
            "tool_call": {"name": "indexer.submit_resource", "elapsed_ms": 3},
            "result": {
                "ok": True,
                "summary": "确认后提交到 qBittorrent",
                "suggestions": [],
                "evidence": [],
            },
            "confirmation": {
                "confirmation_id": "private-confirmation-ticket",
                "expires_in": 60,
                "contract": {
                    "version": 1,
                    "action": "提交资源下载",
                    "object": "你刚才选择的资源候选",
                    "impact": "会向 qBittorrent 创建下载任务。",
                    "reversibility": "可在下载器中暂停或删除任务。",
                    "preflight_at": "2026-08-09T12:34:56+08:00",
                    "risk": "danger",
                    "preflight_summary": "预检通过：下载器连接正常。",
                },
            },
        }
        service.confirm.return_value = {
            "mode": "confirmed_action",
            "result": {
                "ok": True,
                "summary": "下载任务已提交",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        tokens = iter(["prepare-action", "confirm-action", "cancel-action"])
        store = TelegramAgentActionStore(token_factory=lambda: next(tokens))
        prepare_id = store.create_resource_prepare(
            owner="tg:v1:100\x1f200",
            result_id="resource_result_123456",
            target="qb",
            group_id="message-a",
        )
        prepare_call = SimpleNamespace(
            id="prepare-callback",
            data=f"aga:{prepare_id}",
            from_user=SimpleNamespace(id=200),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
        )
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ):
            handle_agent_callback(bot, prepare_call, _Telebot)
            service.prepare.assert_called_once_with(
                "indexer.submit_resource",
                {"result_id": "resource_result_123456", "target": "qb"},
                owner="tg:v1:100\x1f200",
            )
            service.confirm.assert_not_called()
            confirmation_markup = bot.edits[0][3]["reply_markup"]
            self.assertEqual(len(confirmation_markup.buttons), 2)
            self.assertEqual(
                confirmation_markup.buttons[0].text,
                "确认：提交资源下载",
            )
            self.assertIn("操作对象", bot.edits[0][0])
            self.assertIn("下载器连接正常", bot.edits[0][0])
            self.assertNotIn("resource_result_123456", bot.edits[0][0])
            self.assertTrue(all(
                "private-confirmation-ticket" not in button.callback_data
                for button in confirmation_markup.buttons
            ))

            handle_agent_callback(bot, prepare_call, _Telebot)
            self.assertEqual(bot.answers[-1][1], "操作已过期或无效")
            service.prepare.assert_called_once()

            confirm_call = SimpleNamespace(
                id="confirm-callback",
                data=confirmation_markup.buttons[0].callback_data,
                from_user=SimpleNamespace(id=200),
                message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
            )
            handle_agent_callback(bot, confirm_call, _Telebot)

        service.confirm.assert_called_once_with(
            "private-confirmation-ticket", owner="tg:v1:100\x1f200"
        )
        self.assertIn("下载任务已提交", bot.edits[-1][0])
        self.assertIsNone(bot.edits[-1][3]["reply_markup"])

    def test_resource_prepare_failure_is_safe_and_never_confirms(self):
        bot = _Bot()
        service = Mock()
        service.prepare.side_effect = AgentToolError(
            "private backend unavailable", code="precondition_failed"
        )
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(token_factory=lambda: "prepare-action")
        action_id = store.create_resource_prepare(
            owner="tg:v1:100\x1f200",
            result_id="resource_result_123456",
            target="guangya",
            group_id="message-a",
        )
        call = SimpleNamespace(
            id="prepare-callback",
            data=f"aga:{action_id}",
            from_user=SimpleNamespace(id=200),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
        )
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ):
            handle_agent_callback(bot, call, _Telebot)

        service.confirm.assert_not_called()
        service.discard_confirmation.assert_not_called()
        self.assertIn("无法准备资源提交", bot.edits[0][0])
        self.assertNotIn("private backend", bot.edits[0][0])
        self.assertIsNone(bot.edits[0][3]["reply_markup"])
        self.assertEqual(bot.answers[-1][1], "正在准备，请稍候")

    def test_message_admission_is_rate_limited_per_owner(self):
        bot = _Bot()
        service = Mock()
        service.query.return_value = {
            "mode": "answer",
            "result": {
                "ok": True,
                "summary": "检查完成",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter._TELEGRAM_QUERY_LIMIT_PER_MINUTE", 1
        ):
            self.assertTrue(handle_agent_message(bot, _Telebot, _message("第一次")))
            self.assertTrue(handle_agent_message(bot, _Telebot, _message("第二次")))
        service.query.assert_called_once()
        self.assertEqual(bot.replies[-1][1], "请求过于频繁，请稍后重试。")

    def test_resource_search_uses_owner_scoped_tool_rate_limit(self):
        bot = _Bot()
        calls: list[dict] = []
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="indexer.search_resources",
            description="indexer.search_resources",
            risk=RiskLevel.READ,
            parameters={},
            handler=lambda arguments: (
                calls.append(dict(arguments))
                or ToolResult(True, "找到资源", "indexer.search_resources")
            ),
            validator=lambda arguments: dict(arguments),
        ))
        service = AgentOrchestrator(registry)
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ):
            for message_id in range(20, 26):
                self.assertTrue(handle_agent_message(
                    bot,
                    _Telebot,
                    _message("搜索《沙丘2》的资源", message_id=message_id),
                ))
            self.assertTrue(handle_agent_message(
                bot,
                _Telebot,
                _message("搜索《沙丘2》的资源", message_id=26),
            ))

        self.assertEqual(len(calls), 6)
        self.assertEqual(
            bot.replies[-1][1],
            "Agent 无法处理该请求，请调整问题后重试。",
        )

    def test_disabled_agent_does_not_consume_plain_text(self):
        bot = _Bot()
        with patch("app.bot.agent_adapter.get", return_value="0"):
            self.assertFalse(handle_agent_message(bot, _Telebot, _message()))
        self.assertEqual(bot.replies, [])

    def test_callback_admission_preserves_limited_valid_ticket(self):
        bot = _Bot()
        service = Mock()
        service.confirm.return_value = {
            "mode": "confirmed_action",
            "result": {
                "ok": True,
                "summary": "操作已完成",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        tokens = iter(["first-action", "second-action"])
        store = TelegramAgentActionStore(token_factory=lambda: next(tokens))
        first_id = store.create(
            owner="tg:v1:100\x1f200",
            confirmation_id="ticket-1",
            action="confirm",
        )
        second_id = store.create(
            owner="tg:v1:100\x1f200",
            confirmation_id="ticket-2",
            action="confirm",
        )

        def callback(action_id):
            return SimpleNamespace(
                id=f"callback-{action_id}",
                data=f"aga:{action_id}",
                from_user=SimpleNamespace(id=200),
                message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
            )

        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ), patch(
            "app.bot.agent_adapter._TELEGRAM_CALLBACK_LIMIT_PER_MINUTE", 1
        ):
            handle_agent_callback(bot, callback(first_id))
            handle_agent_callback(bot, callback(second_id))
            self.assertEqual(bot.answers[-1][1], "请求过于频繁，请稍后重试")
            # 限流发生在一次性 resolve 前；窗口恢复后仍可使用同一有效票据。
            agent_rate_limiter.reset()
            handle_agent_callback(bot, callback(second_id))
        self.assertEqual(
            service.confirm.call_args_list,
            [
                unittest.mock.call("ticket-1", owner="tg:v1:100\x1f200"),
                unittest.mock.call("ticket-2", owner="tg:v1:100\x1f200"),
            ],
        )

    def test_invalid_callback_does_not_consume_valid_confirmation_budget(self):
        bot = _Bot()
        service = Mock()
        service.confirm.return_value = {
            "mode": "confirmed_action",
            "result": {
                "ok": True,
                "summary": "操作已完成",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(token_factory=lambda: "valid-action")
        store.create(
            owner="tg:v1:100\x1f200", confirmation_id="ticket", action="confirm"
        )
        invalid_call = SimpleNamespace(
            id="invalid", data="aga:missing", from_user=SimpleNamespace(id=200),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
        )
        valid_call = SimpleNamespace(
            id="valid", data="aga:valid-action", from_user=SimpleNamespace(id=200),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
        )
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.agent_adapter.get_agent_service", return_value=service
        ), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store",
            return_value=store,
        ), patch(
            "app.bot.agent_adapter._TELEGRAM_CALLBACK_LIMIT_PER_MINUTE", 1
        ):
            handle_agent_callback(bot, invalid_call)
            handle_agent_callback(bot, valid_call)
        service.confirm.assert_called_once_with(
            "ticket", owner="tg:v1:100\x1f200"
        )
        self.assertEqual(bot.answers[-1][1], "正在执行，请稍候")

    def test_callback_confirms_once_and_edits_existing_message(self):
        bot = _Bot()
        service = Mock()
        service.confirm.return_value = {
            "mode": "confirmed_action",
            "result": {
                "ok": True,
                "summary": "操作已完成",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(token_factory=lambda: "opaque-action")
        action_id = store.create(
            owner="tg:v1:100\x1f200",
            confirmation_id="ticket",
            action="confirm",
        )
        call = SimpleNamespace(
            id="callback",
            data=f"aga:{action_id}",
            from_user=SimpleNamespace(id=200),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=100), message_id=11
            ),
        )
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.agent_adapter.get_agent_service", return_value=service), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store", return_value=store
        ):
            handle_agent_callback(bot, call)
            handle_agent_callback(bot, call)
        service.confirm.assert_called_once_with("ticket", owner="tg:v1:100\x1f200")
        self.assertEqual(len(bot.edits), 1)
        self.assertEqual(bot.answers[-1][1], "操作已过期或无效")

    def test_confirm_callback_runtime_failure_reports_service_error(self):
        bot = _Bot()
        service = Mock()
        service.confirm.side_effect = RuntimeError("provider unavailable")
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(token_factory=lambda: "runtime-failure")
        action_id = store.create(
            owner="tg:v1:100\x1f200", confirmation_id="ticket", action="confirm"
        )
        call = SimpleNamespace(
            id="callback",
            data=f"aga:{action_id}",
            from_user=SimpleNamespace(id=200),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
        )
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.agent_adapter.get_agent_service", return_value=service), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store", return_value=store
        ):
            handle_agent_callback(bot, call)

        service.confirm.assert_called_once_with("ticket", owner="tg:v1:100\x1f200")
        self.assertEqual(bot.answers[-1][1], "正在执行，请稍候")
        self.assertEqual(len(bot.edits), 1)
        self.assertIn("服务暂时不可用", bot.edits[0][0])

    def test_confirmed_callback_falls_back_to_new_terminal_message(self):
        bot = _CallbackEditFallbackBot()
        service = Mock()
        service.confirm.return_value = {
            "mode": "confirmed_action",
            "result": {
                "ok": True,
                "summary": "操作已完成",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(token_factory=lambda: "fallback-action")
        action_id = store.create(
            owner="tg:v1:100\x1f200", confirmation_id="ticket", action="confirm"
        )
        call = SimpleNamespace(
            id="callback",
            data=f"aga:{action_id}",
            from_user=SimpleNamespace(id=200),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
        )
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.agent_adapter.get_agent_service", return_value=service), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store", return_value=store
        ):
            handle_agent_callback(bot, call)

        service.confirm.assert_called_once_with("ticket", owner="tg:v1:100\x1f200")
        self.assertEqual(len(bot.edit_attempts), 1)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("操作已完成", bot.sent[0][1])
        self.assertEqual(json.loads(db.kv_get("telegram_pending_operations_v1", "[]")), [])

    def test_confirmed_callback_delivery_failure_is_persisted_not_misreported(self):
        bot = _CallbackEditFallbackBot(offline=True)
        service = Mock()
        service.confirm.return_value = {
            "mode": "confirmed_action",
            "result": {
                "ok": True,
                "summary": "操作已完成",
                "suggestions": [],
                "evidence": [],
            },
        }
        values = {
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        store = TelegramAgentActionStore(token_factory=lambda: "offline-action")
        action_id = store.create(
            owner="tg:v1:100\x1f200", confirmation_id="ticket", action="confirm"
        )
        call = SimpleNamespace(
            id="callback",
            data=f"aga:{action_id}",
            from_user=SimpleNamespace(id=200),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
        )
        with patch(
            "app.bot.agent_adapter.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.agent_adapter.get_agent_service", return_value=service), patch(
            "app.bot.agent_adapter.get_telegram_agent_action_store", return_value=store
        ), patch(
            "app.bot.progress.schedule_terminal_delivery_retry"
        ) as schedule_retry:
            handle_agent_callback(bot, call)

        service.confirm.assert_called_once_with("ticket", owner="tg:v1:100\x1f200")
        self.assertEqual(len(bot.edit_attempts), 1)
        self.assertNotIn("执行失败", bot.edit_attempts[0][0])
        pending = json.loads(db.kv_get("telegram_pending_operations_v1", "[]"))
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["terminal_pending"])
        self.assertIn("操作已完成", pending[0]["terminal_text"])
        schedule_retry.assert_called_once_with(
            bot, None, pending[0]["id"]
        )



class TelegramAgentConfigTests(unittest.TestCase):
    @staticmethod
    def _validate(payload, current=None):
        from app.routes.api import _validate_telegram_agent_updates

        values = dict(current or {})
        with patch(
            "app.routes.api.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ):
            return _validate_telegram_agent_updates(payload)

    def test_enabled_agent_requires_complete_bot_and_user_allowlist(self):
        cases = (
            ({"TG_AGENT_ENABLED": "1"}, {}, "Bot Token"),
            (
                {"TG_AGENT_ENABLED": "1"},
                {"TG_BOT_TOKEN": "token"},
                "Chat ID",
            ),
            (
                {"TG_AGENT_ENABLED": "1"},
                {"TG_BOT_TOKEN": "token", "TG_CHAT_ID": "100"},
                "用户 ID",
            ),
        )
        for payload, current, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(
                ValueError, expected
            ):
                self._validate(payload, current)

    def test_allowlist_is_validated_deduplicated_and_normalized(self):
        current = {
            "TG_BOT_TOKEN": "token",
            "TG_CHAT_ID": "-100",
            "TG_AGENT_ENABLED": "0",
        }
        result = self._validate(
            {
                "TG_AGENT_ENABLED": "true",
                "TG_AGENT_ALLOWED_USER_IDS": "200; 201，200",
            },
            current,
        )
        self.assertEqual(
            result,
            {
                "TG_AGENT_ENABLED": "1",
                "TG_AGENT_ALLOWED_USER_IDS": "200,201",
            },
        )
        with self.assertRaisesRegex(ValueError, "无效用户 ID"):
            self._validate(
                {"TG_AGENT_ALLOWED_USER_IDS": "200,not-a-user"}, current
            )

    def test_disabling_agent_is_not_blocked_by_legacy_incomplete_config(self):
        for current in (
            {"TG_BOT_TOKEN": "legacy-token", "TG_CHAT_ID": ""},
            {"TG_AGENT_ALLOWED_USER_IDS": "200 invalid"},
        ):
            with self.subTest(current=current):
                self.assertEqual(
                    self._validate({"TG_AGENT_ENABLED": "0"}, current),
                    {"TG_AGENT_ENABLED": "0"},
                )

    def test_masked_token_and_chat_preserve_existing_values(self):
        current = {
            "TG_BOT_TOKEN": "token",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ENABLED": "0",
        }
        self.assertEqual(
            self._validate(
                {"TG_BOT_TOKEN": "********", "TG_CHAT_ID": "********"},
                current,
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
