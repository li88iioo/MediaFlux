from __future__ import annotations

import types
import unittest
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

from app.agent.kernel.adapters import ApprovalView, TurnView
from app.agent.kernel.events import AgentEventType, EventFactory
from app.agent.kernel.state import SessionBusyError
from app.bot import agent_adapter as adapter


class Button:
    def __init__(self, text, callback_data):
        self.text = text
        self.callback_data = callback_data


class Markup:
    def __init__(self, row_width=1):
        self.row_width = row_width
        self.buttons = []

    def add(self, *buttons):
        self.buttons.extend(buttons)


TELEBOT = types.SimpleNamespace(
    types=types.SimpleNamespace(
        InlineKeyboardMarkup=Markup,
        InlineKeyboardButton=Button,
    )
)


@dataclass
class User:
    id: int


@dataclass
class Chat:
    id: int


class Message:
    def __init__(self, text="检查媒体库", *, chat_id=-100, user_id=7, message_id=11):
        self.text = text
        self.chat = Chat(chat_id)
        self.from_user = User(user_id)
        self.message_id = message_id
        self.message_thread_id = None
        self.reply_to_message = None


class Call:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.from_user = User(7)
        self.id = "callback-1"


class FakeBot:
    def __init__(self):
        self.replies = []
        self.edits = []
        self.answers = []
        self.sent = []
        self.actions = []
        self.deleted = []
        self._next = 100

    def reply_to(self, source, text, **kwargs):
        self.replies.append((text, kwargs))
        target = Message(text, chat_id=source.chat.id, user_id=0, message_id=self._next)
        self._next += 1
        return target

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edits.append((text, chat_id, message_id, kwargs))

    def answer_callback_query(self, callback_id, text, **kwargs):
        self.answers.append((callback_id, text, kwargs))

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        target = Message(text, chat_id=chat_id, user_id=0, message_id=self._next)
        self._next += 1
        return target

    def send_chat_action(self, chat_id, action, **kwargs):
        self.actions.append((chat_id, action, kwargs))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    def edit_message_reply_markup(self, chat_id, message_id, **kwargs):
        self.edits.append(("", chat_id, message_id, kwargs))

    def stop_polling(self):
        pass


class FakeDraftBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.drafts = []

    def send_message_draft(self, chat_id, draft_id, text, **kwargs):
        self.drafts.append((chat_id, draft_id, text, kwargs))
        return True


class FakeTelegramTransport:
    def __init__(self, view, *, events=(), confirm_events=(), confirm_view=None):
        self.view = view
        self.confirm_view = confirm_view
        self.events = tuple(events)
        self.confirm_events = tuple(confirm_events)
        self.queries = []
        self.confirmations = []
        self.cancelled = []

    async def query(self, envelope, *, observe=None, cancellation=None):
        self.queries.append(envelope)
        if observe is not None:
            for event in self.events:
                await observe(event)
        return self.view

    async def confirm(self, envelope, *, observe=None):
        self.confirmations.append(envelope)
        if observe is not None:
            for event in self.confirm_events:
                await observe(event)
        if self.confirm_view is not None:
            return self.confirm_view
        return TurnView(
            session_id=envelope.session_id,
            turn_id="turn-confirm",
            request_id=envelope.request_id,
            status="effect_completed",
            effect_result={"summary": "订阅已创建"},
        )

    async def cancel_effect(self, envelope):
        self.cancelled.append(envelope)
        return True

    async def cancel(self, *, owner, session_id):
        return True


class FakeStore:
    async def load(self, *, owner, session_id):
        return types.SimpleNamespace(pending_effect_plan_id="plan_1234567890abcdef")

    async def reset_session(self, *, owner, session_id):
        return None


class FakeLifecycle:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    async def reset(self, *, owner, session_id):
        self.calls.append((owner, session_id))
        if self.error is not None:
            raise self.error


class AgentKernelTelegramAdapterTests(unittest.TestCase):
    def setUp(self):
        self.config_values = {
            "TG_AGENT_ALLOWED_USER_IDS": "7",
            "TG_CHAT_ID": "-100",
            "TG_AGENT_ENABLED": "1",
        }

    def _get(self, key, default=""):
        return self.config_values.get(key, default)

    def _patch_access(self):
        return (
            patch.object(adapter.config, "get", side_effect=self._get),
            patch.object(adapter, "is_agent_enabled", return_value=True),
            patch.object(adapter.agent_rate_limiter, "allow", return_value=True),
        )

    def test_owner_and_session_are_stable_and_user_scoped(self):
        with self._patch_access()[0]:
            self.assertTrue(adapter.telegram_user_is_allowed(7))
        self.assertEqual(adapter.telegram_agent_owner(-100, 7), "tg:v1:-100\x1f7")
        self.assertEqual(
            adapter.telegram_agent_session_id(-100, 7),
            adapter.telegram_agent_session_id(-100, 7),
        )
        self.assertNotEqual(
            adapter.telegram_agent_session_id(-100, 7),
            adapter.telegram_agent_session_id(-100, 8),
        )

    def test_reset_uses_unified_lifecycle_and_reports_protected_effect(self):
        bot = FakeBot()
        lifecycle = FakeLifecycle(
            error=SessionBusyError("confirmed effect is executing")
        )
        runtime = types.SimpleNamespace(lifecycle=lifecycle)
        access_patches = self._patch_access()

        with (
            access_patches[0],
            access_patches[1],
            access_patches[2],
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
        ):
            adapter.handle_agent_reset(bot, Message(text="/agent_reset"))

        self.assertEqual(len(lifecycle.calls), 1)
        self.assertIn("已确认写操作正在执行", bot.replies[-1][0])

    def _query_candidate_response(self, answer, candidates, *, status="success"):
        transport = FakeTelegramTransport(TurnView(
            session_id="tg_session", turn_id="turn-candidate-answer", request_id="candidate-answer",
            status=status, answer=answer, error_message="索引站查询失败", candidate_view=candidates,
        ))
        runtime = types.SimpleNamespace(telegram=transport, store=FakeStore())
        bot = FakeBot()
        access = self._patch_access()
        draft = {"handle": "ref_" + "x" * 24, "positions": candidates.get("recommended_positions", []),
                 "target": "guangya", "expanded": False, "phase": "select"}
        with (access[0], access[1], access[2],
              patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
              patch("app.bot.agent_candidates.start_draft", new=AsyncMock(return_value=draft)) as start):
            self.assertTrue(adapter.handle_agent_message(
                bot, TELEBOT, Message("仙逆完美世界有更新吗？4k的排除1080")))
        messages = [(text, kwargs) for text, _, _, kwargs in bot.edits]
        messages += [(text, kwargs) for _, text, kwargs in bot.sent]
        return messages, start

    def test_no_recommendation_keeps_query_answer_without_download_keyboard(self):
        candidates = {"items": [{"position": 1, "title": "完美世界.S01E01.1080p"}],
                      "recommended_positions": []}
        for answer in (
            "仙逆、完美世界均没有已播缺集。",
            "暂未找到符合4K且排除1080p条件的资源。",
            "索引站超时，暂时无法判断是否有符合条件的资源。",
            "找到两项可查看的2160p资源，可按序号指定预检。",
        ):
            with self.subTest(answer=answer):
                messages, start = self._query_candidate_response(answer, candidates)
                self.assertTrue(any(answer in text for text, _ in messages))
                self.assertFalse(any(kwargs.get("reply_markup") for _, kwargs in messages))
                self.assertFalse(any("资源搜索与批选" in text for text, _ in messages))
                start.assert_not_called()

    def test_explicitly_selected_general_resources_keep_the_manual_picker(self):
        candidates = {"items": [{"position": 2, "title": "示例资源.2160p"}],
                      "recommended_positions": [], "explicit_selection": True}
        messages, start = self._query_candidate_response("找到一个符合要求的版本。", candidates)
        self.assertTrue(any("找到一个符合要求的版本" in text for text, _ in messages))
        self.assertEqual(sum(bool(kwargs.get("reply_markup")) for _, kwargs in messages), 1)
        start.assert_awaited_once()

    def test_empty_candidate_items_never_generate_preview_controls(self):
        messages, start = self._query_candidate_response(
            "未找到匹配资源。", {"items": [], "recommended_positions": [1]})
        self.assertTrue(any("未找到匹配资源" in text for text, _ in messages))
        self.assertFalse(any(kwargs.get("reply_markup") for _, kwargs in messages))
        start.assert_not_called()

    def test_recommendation_is_appended_after_the_complete_answer(self):
        answer = "结论：完美世界有4K资源。\n" + "查询依据与版本说明。" * 700 + "\n完整回答结束。"
        messages, start = self._query_candidate_response(answer, {
            "items": [{"position": 1, "title": "完美世界.S01E01.2160p"}],
            "recommended_positions": [1],
        })
        bodies = "\n".join(text for text, _ in messages)
        self.assertIn("结论：完美世界有4K资源", bodies)
        self.assertIn("完整回答结束", bodies)
        controls = [(text, kwargs) for text, kwargs in messages if kwargs.get("reply_markup")]
        self.assertEqual(len(controls), 1)
        self.assertIn("资源推荐与批选", controls[0][0])
        self.assertNotIn("结论：", controls[0][0])
        self.assertTrue(any("预览下载 1 项" == button.text
                            for button in controls[0][1]["reply_markup"].buttons))
        start.assert_awaited_once()

    def test_failed_or_cancelled_turn_cannot_be_replaced_by_a_candidate_card(self):
        for status, expected in (("failed", "索引站查询失败"), ("cancelled", "已停止")):
            with self.subTest(status=status):
                messages, start = self._query_candidate_response("", {
                    "items": [{"position": 1, "title": "仙逆.2160p"}], "recommended_positions": [1],
                }, status=status)
                self.assertTrue(any(expected in text for text, _ in messages))
                self.assertFalse(any(kwargs.get("reply_markup") for _, kwargs in messages))
                start.assert_not_called()

    def test_disabled_agent_does_not_capture_normal_telegram_text(self):
        bot = FakeBot()
        with patch.object(adapter, "is_agent_enabled", return_value=False):
            self.assertFalse(adapter.handle_agent_message(bot, TELEBOT, Message()))
        self.assertEqual(bot.replies, [])

    def test_query_streams_typing_and_renders_markdown_as_telegram_html(self):
        factory = EventFactory(
            session_id="tg_session",
            turn_id="turn-stream",
            request_id="request-stream",
        )
        answer = (
            "### 2026 新番推荐\n"
            "1. **《葬送的芙莉莲》第二季**\n"
            "   - **题材**：奇幻 / 冒险\n\n"
            "---\n"
            "> 定档信息可能变化。"
        )
        transport = FakeTelegramTransport(
            TurnView(
                session_id="tg_session",
                turn_id="turn-stream",
                request_id="request-stream",
                status="success",
                answer=answer,
            ),
            events=(
                factory.create(AgentEventType.MODEL_STARTED, {"round": 1}),
                factory.create(
                    AgentEventType.MODEL_DELTA,
                    {"round": 1, "delta": answer[:35]},
                ),
                factory.create(
                    AgentEventType.MODEL_DELTA,
                    {"round": 1, "delta": answer[35:]},
                ),
            ),
        )
        runtime = types.SimpleNamespace(telegram=transport, store=FakeStore())
        bot = FakeDraftBot()
        patches = self._patch_access()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
        ):
            handled = adapter.handle_agent_message(
                bot, TELEBOT, Message("2026 新番推荐")
            )

        self.assertTrue(handled)
        self.assertTrue(any(action == "typing" for _, action, _ in bot.actions))
        self.assertEqual(bot.drafts, [])
        self.assertEqual(bot.sent[0][2]["reply_to_message_id"], 11)
        streamed = [
            text
            for text, _chat, _message, _kwargs in bot.edits
            if "正在输出" in text
        ]
        self.assertTrue(streamed)
        self.assertIn("<b>2026 新番推荐</b>", streamed[0])
        final_text, _chat_id, _message_id, final_kwargs = bot.edits[-1]
        self.assertEqual(final_kwargs["parse_mode"], "HTML")
        self.assertIn("<b>2026 新番推荐</b>", final_text)
        self.assertIn("<b>《葬送的芙莉莲》第二季</b>", final_text)
        self.assertIn("────────", final_text)
        self.assertIn("<blockquote>定档信息可能变化。</blockquote>", final_text)
        self.assertNotIn("###", final_text)
        self.assertNotIn("**", final_text)
        self.assertNotIn("正在输出", final_text)

    def test_partial_answer_preserves_full_markdown_and_execution_trace(self):
        answer = "## 部分完成\n" + "已核对的说明。" * 900 + "\n**最后一部仍待确认，可以继续。**"
        rendered = adapter._render_turn(TurnView(
            session_id="partial", turn_id="turn", request_id="request", status="partial",
            answer=answer, tool_calls=("library.check_updates",),
        ))
        self.assertIn("<b>部分完成</b>", rendered)
        self.assertIn("最后一部仍待确认，可以继续。", rendered)
        self.assertIn("🔎 执行：", rendered)
        self.assertNotIn("Agent 暂时无法完成", rendered)

    def test_long_stream_keeps_a_bounded_latest_preview(self):
        factory = EventFactory(
            session_id="tg_session",
            turn_id="turn-long-stream",
            request_id="request-long-stream",
        )
        answer = "\n".join(
            f"{index}. **推荐 {index}**：" + ("详细说明" * 20)
            for index in range(1, 81)
        )
        transport = FakeTelegramTransport(
            TurnView(
                session_id="tg_session",
                turn_id="turn-long-stream",
                request_id="request-long-stream",
                status="success",
                answer=answer,
            ),
            events=(
                factory.create(AgentEventType.MODEL_STARTED, {"round": 1}),
                factory.create(
                    AgentEventType.MODEL_DELTA,
                    {"round": 1, "delta": answer},
                ),
            ),
        )
        runtime = types.SimpleNamespace(telegram=transport, store=FakeStore())
        bot = FakeDraftBot()
        patches = self._patch_access()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
        ):
            handled = adapter.handle_agent_message(
                bot, TELEBOT, Message("最近有什么推荐的美剧")
            )

        self.assertTrue(handled)
        streamed = [
            text
            for text, _chat, _message, _kwargs in bot.edits
            if "正在输出" in text
        ]
        self.assertTrue(streamed)
        preview = streamed[-1]
        self.assertIn("回答较长，下面显示最新生成内容", preview)
        self.assertIn("<b>推荐 80</b>", preview)
        self.assertNotIn("<b>推荐 1</b>", preview)
        self.assertLess(len(preview), 1_600)

        final_chunks = [bot.edits[-1][0], *(text for _chat, text, _kwargs in bot.sent[1:])]
        self.assertGreater(len(final_chunks), 1)
        self.assertTrue(
            all(
                adapter.telegram_html_text_length(chunk) <= adapter._MAX_MESSAGE
                for chunk in final_chunks
            )
        )
        complete = "\n".join(final_chunks)
        self.assertIn("<b>推荐 1</b>", complete)
        self.assertIn("<b>推荐 80</b>", complete)
        self.assertNotIn("正在输出", complete)

    def test_stream_overflow_preview_never_exceeds_telegram_hard_limit(self):
        preview = adapter._truncate_stream_overflow_preview(
            "<b>超长回答</b>\n" + ("😀" * 3_000)
        )

        self.assertLessEqual(
            adapter.telegram_html_text_length(preview),
            adapter._TELEGRAM_MESSAGE_LIMIT,
        )
        self.assertIn("前文已生成", preview)
        self.assertIn("正在输出", preview)

    def test_query_renders_kernel_approval_with_direct_effect_buttons(self):
        approval = ApprovalView(
            plan_id="plan_1234567890abcdef",
            tool_name="rss.create_subscription",
            effect="WRITE",
            preview={
                "summary": "将创建 RSS 订阅",
                "data": {
                    "target": "qb",
                    "count": 1,
                    "effects": ["创建一条每 6 小时刷新的 RSS 规则"],
                },
            },
            result={},
            expires_at="2026-09-03T12:00:00Z",
            confirmation={
                "action": "创建 RSS 订阅",
                "impact": "确认后会保存订阅规则。",
                "reversibility": "可在 RSS 页面删除。",
            },
        )
        transport = FakeTelegramTransport(
            TurnView(
                session_id="tg_session",
                turn_id="turn",
                request_id="request",
                status="approval_required",
                approval=approval,
            )
        )
        runtime = types.SimpleNamespace(telegram=transport, store=FakeStore())
        bot = FakeBot()
        patches = self._patch_access()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
        ):
            handled = adapter.handle_agent_message(
                bot, TELEBOT, Message("创建 RSS 订阅")
            )
        self.assertTrue(handled)
        self.assertEqual(len(transport.queries), 1)
        markup = bot.edits[-1][3]["reply_markup"]
        self.assertEqual(
            [button.callback_data for button in markup.buttons],
            ["agk:c:plan_1234567890abcdef", "agk:x:plan_1234567890abcdef"],
        )
        self.assertIn("等待确认", bot.edits[-1][0])
        self.assertIn("创建 RSS 订阅", bot.edits[-1][0])
        self.assertIn("qBittorrent", bot.edits[-1][0])
        self.assertIn("确认后会保存订阅规则", bot.edits[-1][0])

    def test_confirm_callback_clears_buttons_for_replaced_plan(self):
        transport = FakeTelegramTransport(None)
        store = types.SimpleNamespace(load=AsyncMock(return_value=types.SimpleNamespace(
            pending_effect_plan_id="plan_replaced_123456",
        )))
        runtime = types.SimpleNamespace(telegram=transport, store=store)
        bot = FakeBot()
        message = Message("preview", user_id=0, message_id=33)
        message.reply_markup = Markup()
        call = Call("agk:c:plan_1234567890abcdef", message)
        patches = self._patch_access()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
        ):
            adapter.handle_agent_callback(bot, call, TELEBOT)

        self.assertEqual(transport.confirmations, [])
        self.assertEqual(len(bot.edits), 1)
        self.assertEqual(bot.edits[0][0], "")
        self.assertIsNone(bot.edits[0][3]["reply_markup"])
        self.assertIn("已处理或被替代", bot.answers[-1][1])
        self.assertTrue(bot.answers[-1][2]["show_alert"])

    def test_confirm_callback_executes_plan_without_model_protocol(self):
        factory = EventFactory(
            session_id="tg_session",
            turn_id="turn-confirm",
            request_id="tgcb_callback-1",
        )
        transport = FakeTelegramTransport(
            None,
            confirm_events=(factory.create(
                AgentEventType.MODEL_STARTED,
                {"round": 2, "phase": "confirmed_synthesis"},
            ),),
        )
        runtime = types.SimpleNamespace(telegram=transport, store=FakeStore())
        bot = FakeBot()
        message = Message("preview", user_id=0, message_id=33)
        call = Call("agk:c:plan_1234567890abcdef", message)
        patches = self._patch_access()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
        ):
            adapter.handle_agent_callback(bot, call, TELEBOT)
        self.assertEqual(len(transport.confirmations), 1)
        self.assertIn("正在核对确认计划", bot.edits[0][0])
        self.assertIsNone(bot.edits[0][3]["reply_markup"])
        self.assertTrue(any(
            "正在整理执行结果" in edit[0] for edit in bot.edits
        ))
        self.assertIn("订阅已创建", bot.edits[-1][0])

    def test_confirm_callback_finishes_a_claimed_effect_failure_after_observer_progress(self):
        effect_result = {
            "ok": False,
            "status": "confirmation_stale",
            "summary": "资源快照已失效，请重新预检",
        }
        factory = EventFactory(
            session_id="tg_session",
            turn_id="turn-confirm-failure",
            request_id="tgcb_callback-1",
        )
        transport = FakeTelegramTransport(
            TurnView(
                session_id="tg_session",
                turn_id="turn-query-unused",
                request_id="query-unused",
                status="success",
            ),
            confirm_events=(
                factory.create(
                    AgentEventType.EFFECT_FAILED,
                    {
                        "code": "confirmation_stale",
                        "message": "资源快照已失效，请重新预检",
                        "result": effect_result,
                    },
                ),
                factory.create(
                    AgentEventType.TURN_FAILED,
                    {
                        "code": "confirmation_stale",
                        "message": "资源快照已失效，请重新预检",
                    },
                ),
            ),
            confirm_view=TurnView(
                session_id="tg_session",
                turn_id="turn-confirm-failure",
                request_id="tgcb_callback-1",
                status="failed",
                effect_result=effect_result,
                error_code="confirmation_stale",
                error_message="资源快照已失效，请重新预检",
            ),
        )
        runtime = types.SimpleNamespace(telegram=transport, store=FakeStore())
        bot = FakeBot()
        call = Call("agk:c:plan_1234567890abcdef", Message("preview", user_id=0, message_id=33))
        patches = self._patch_access()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
            patch.object(adapter, "_settle_candidate_draft", return_value=None),
        ):
            adapter.handle_agent_callback(bot, call, TELEBOT)

        self.assertEqual(len(transport.confirmations), 1)
        self.assertGreaterEqual(len(bot.edits), 3)
        self.assertIn("正在核对确认计划", bot.edits[0][0])
        self.assertTrue(any(
            "执行未完成，正在整理结果…" in edit[0] for edit in bot.edits
        ))
        self.assertIn("资源快照已失效", bot.edits[-1][0])
        self.assertIn("请重新预检", bot.edits[-1][0])
        self.assertNotIn("执行未完成，正在整理结果…", bot.edits[-1][0])
        self.assertIsNone(bot.edits[-1][3]["reply_markup"])

    def test_confirm_callback_settles_unclaimed_confirmation_failure_on_original_card(self):
        transport = FakeTelegramTransport(
            TurnView(
                session_id="tg_session",
                turn_id="turn-query-unused",
                request_id="query-unused",
                status="success",
            ),
            confirm_view=TurnView(
                session_id="tg_session",
                turn_id="turn-confirm-invalid",
                request_id="tgcb_callback-1",
                status="failed",
                error_code="confirmation_invalid",
                error_message="确认计划无效、已过期或已被使用",
            ),
        )
        runtime = types.SimpleNamespace(telegram=transport, store=FakeStore())
        bot = FakeBot()
        call = Call("agk:c:plan_1234567890abcdef", Message("preview", user_id=0, message_id=33))
        patches = self._patch_access()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime),
        ):
            adapter.handle_agent_callback(bot, call, TELEBOT)

        self.assertEqual(len(transport.confirmations), 1)
        self.assertEqual(len(bot.edits), 2)
        self.assertIn("正在核对确认计划", bot.edits[0][0])
        self.assertIn("这次确认未被接受", bot.edits[-1][0])
        self.assertIn("已过期或已被使用", bot.edits[-1][0])
        self.assertIn("重新生成预览", bot.edits[-1][0])
        self.assertIsNone(bot.edits[-1][3]["reply_markup"])
        self.assertIn("正在核对确认计划", bot.answers[-1][1])
        self.assertEqual(bot.sent, [])

    def test_real_kernel_duplicate_and_expired_confirm_do_not_edit_or_execute_again(self):
        import asyncio
        import tempfile
        from pathlib import Path

        from app.agent.kernel.capabilities import (
            CapabilityRetriever,
            KernelToolSpec,
            ToolCatalog,
            ToolEffect,
        )
        from app.agent.confirmation import ConfirmationStore
        from app.agent.kernel.effects import ConfirmationEffectPlanStore, PreparedEffect
        from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
        from app.agent.kernel.pipeline import ToolPipeline
        from app.agent.kernel.session import AgentSession
        from app.agent.kernel.state import InMemorySessionStateStore
        from app.agent.kernel.transports import QueryEnvelope, TelegramKernelTransport
        from tests.test_agent_kernel_core import ScriptedModel

        clock = [1000.0]
        executions = []

        def execute(_arguments, _snapshot, _context):
            executions.append("write")
            return {"summary": "测试写操作已完成"}

        tool = KernelToolSpec(
            name="download.pause",
            domain="download",
            description="暂停下载任务",
            examples=("暂停下载",),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            prepare=lambda _arguments, _context: PreparedEffect(
                preview={"summary": "将执行测试写操作"},
                snapshot_fingerprint="test-snapshot",
            ),
            execute_confirmed=execute,
        )
        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        model = ScriptedModel(
            [[
                ModelEvent(
                    ModelEventType.TOOL_CALL_COMPLETED,
                    tool_call=ModelToolCall("write", tool.name, {}),
                ),
                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
            ]]
        )
        session = AgentSession(
            model=model,
            catalog=catalog,
            retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state, effect_store=ConfirmationEffectPlanStore(
                ConfirmationStore(clock=lambda: clock[0]),
            )),
            state_store=state,
        )
        model.rounds.append(list(model.rounds[0]))
        model.rounds.insert(1, [ModelEvent(ModelEventType.TEXT_DELTA, text="测试写操作已完成"),
                                ModelEvent(ModelEventType.FINISH, finish_reason="stop")])
        transport = TelegramKernelTransport(session)
        owner = adapter.telegram_agent_owner(-100, 7)
        session_id = adapter.telegram_agent_session_id(-100, 7)
        bot = FakeBot()
        call = Call(
            "agk:c:placeholder",
            Message("preview", chat_id=-100, user_id=0, message_id=33),
        )
        call.message.message_thread_id = 73
        runtime = types.SimpleNamespace(telegram=transport, store=None)
        patches = self._patch_access()

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.database.resolve_db_path",
            return_value=Path(temp_dir) / "kernel-test.db",
        ), patch.object(
            adapter, "get_agent_kernel_runtime", return_value=runtime
        ), patch.object(
            adapter, "_settle_candidate_draft", return_value=None
        ):
            preview = asyncio.run(
                transport.query(
                    QueryEnvelope(
                        owner=owner,
                        session_id=session_id,
                        message="手动清洗云盘目录",
                    )
                )
            )
            self.assertIsNotNone(preview.approval)
            call.data = f"agk:c:{preview.approval.plan_id}"

            with patches[0], patches[1], patches[2]:
                adapter.handle_agent_callback(bot, call, TELEBOT)
            first_edits = list(bot.edits)
            self.assertTrue(first_edits)
            self.assertIn("测试写操作已完成", first_edits[-1][0])

            # 同一一次性计划再次确认：真实 Kernel 只发 TURN_FAILED，
            # 不应让 TG observer 把原来的终态重新改成工具进度。
            with patches[0], patches[1], patches[2]:
                adapter.handle_agent_callback(bot, call, TELEBOT)
            self.assertGreater(len(bot.edits), len(first_edits))
            self.assertIn("这次确认未被接受", bot.edits[-1][0])
            self.assertIsNone(bot.edits[-1][3]["reply_markup"])
            self.assertEqual(executions, ["write"])

            # 新预览超过默认十分钟后点击：不执行、不改原消息，但必须给明确反馈。
            preview = asyncio.run(transport.query(QueryEnvelope(
                owner=owner, session_id=session_id, message="重新预览云盘文件变更",
            )))
            call.data = f"agk:c:{preview.approval.plan_id}"
            clock[0] += 601
            with patches[0], patches[1], patches[2]:
                adapter.handle_agent_callback(bot, call, TELEBOT)

        self.assertGreater(len(bot.edits), len(first_edits))
        self.assertEqual(executions, ["write"])
        self.assertIn("这次确认未被接受", bot.edits[-1][0])
        self.assertIn("已过期或已被使用", bot.edits[-1][0])
        self.assertIn("重新生成预览", bot.edits[-1][0])
        self.assertIsNone(bot.edits[-1][3]["reply_markup"])
        self.assertEqual(len(model.requests), 3)

    def test_confirm_continues_progress_and_returns_next_approval(self):
        next_plan = ApprovalView(plan_id="next_plan_1234567890abcdef", tool_name="cloud.move", effect="WRITE",
            preview={"summary": "下一步移动目录"}, result={}, expires_at="")
        factory = EventFactory(session_id="tg_session", turn_id="confirm-flow", request_id="request")
        transport = FakeTelegramTransport(None, confirm_view=TurnView(
            session_id="tg_session", turn_id="confirm-flow", request_id="request", status="approval_required",
            approval=next_plan, effect_result={"ok": True, "summary": "改名已完成"}), confirm_events=(
                factory.create(AgentEventType.TOOL_STARTED, {"kind": "confirmed_effect", "tool": "cloud.change"}),
                factory.create(AgentEventType.TOOL_PROGRESS, {"phase": "background_job", "tool": "cloud.change", "summary": "正在改名 3/10"}),
                factory.create(AgentEventType.MODEL_STARTED, {"round": 1}),
                factory.create(AgentEventType.MODEL_DELTA, {"round": 1, "delta": "改名完成，继续准备移动。"}),
            ))
        bot, observed = FakeBot(), []
        progress_class = adapter._ExistingMessageProgress
        def progress(*args):
            instance = progress_class(*args)
            observed.append(instance)
            return instance
        access = self._patch_access()
        with access[0], access[1], access[2], patch.object(adapter, "get_agent_kernel_runtime", return_value=types.SimpleNamespace(telegram=transport, store=FakeStore())),              patch.object(adapter, "_ExistingMessageProgress", side_effect=progress), patch.object(adapter, "_settle_candidate_draft") as settle:
            adapter.handle_agent_callback(bot, Call("agk:c:plan_1234567890abcdef", Message("preview", user_id=0, message_id=33)), TELEBOT)
        settle.assert_called_once()
        self.assertEqual(settle.call_args.kwargs["next_plan_id"], next_plan.plan_id)
        self.assertIn("改名已完成", bot.edits[-1][0])
        self.assertIn("下一步移动目录", bot.edits[-1][0])
        self.assertTrue(any(b.callback_data == "agk:c:" + next_plan.plan_id for b in bot.edits[-1][3]["reply_markup"].buttons))
        self.assertTrue(observed[0].finished_event.is_set(), "终态必须关闭原typing/progress心跳")
        self.assertTrue(bot.actions, "确认后应沿用typing反馈")

    def test_old_callback_is_explicitly_retired(self):
        bot = FakeBot()
        call = Call("invalid:callback", Message(user_id=0))
        patches = self._patch_access()
        with patches[0], patches[1], patches[2]:
            adapter.handle_agent_callback(bot, call, TELEBOT)
        self.assertIn("旧操作已失效", bot.answers[-1][1])



class TelegramAgentExecutorTests(unittest.TestCase):
    def test_cancelled_job_does_not_kill_the_only_worker(self):
        import asyncio
        import threading

        for failure in (RuntimeError("ordinary failure"), asyncio.CancelledError("cancelled query")):
            with self.subTest(failure=type(failure).__name__):
                executor = adapter.TelegramAgentExecutor(max_queries=1, max_controls=0)
                release, completed = threading.Event(), threading.Event()
                next_job = None
                executor.start()
                finish = executor._finished
                def observe_finish(future):
                    try:
                        finish(future)
                    finally:
                        completed.set()
                def fail():
                    release.wait(2)
                    raise failure
                try:
                    with patch.object(executor, "_finished", side_effect=observe_finish):
                        failed = executor.submit(fail)
                        release.set()
                        with self.assertRaises(type(failure)) as caught:
                            failed.result(1)
                        self.assertIs(caught.exception, failure)
                        self.assertTrue(completed.wait(1))
                        next_job = executor.submit(lambda: "worker still available")
                        self.assertEqual(next_job.result(1), "worker still available")
                finally:
                    release.set()
                    if next_job is not None:
                        next_job.cancel()
                    self.assertTrue(executor.stop(timeout=2))

    def test_already_terminal_future_never_leaks_through_submit(self):
        import asyncio
        from concurrent.futures import Future

        executor = adapter.TelegramAgentExecutor(max_queries=1, max_controls=0)
        executor.start()
        try:
            for outcome in ("success", "failure", "cancelled"):
                with self.subTest(outcome=outcome):
                    future = Future()
                    if outcome == "success":
                        future.set_result("done")
                    elif outcome == "failure":
                        future.set_exception(asyncio.CancelledError("cancelled query"))
                    else:
                        future.cancel()
                    with patch.object(executor._pool, "submit", return_value=future):
                        self.assertIs(executor.submit(lambda: None), future)
                    self.assertEqual(executor.submit(lambda: "lease released").result(1), "lease released")
        finally:
            self.assertTrue(executor.stop(timeout=2))

    def test_queries_are_bounded_and_do_not_occupy_control_capacity(self):
        import threading
        from concurrent.futures import wait

        executor = adapter.TelegramAgentExecutor(max_queries=2, max_controls=1)
        release = threading.Event()
        started = [threading.Event(), threading.Event()]
        executor.start()
        try:
            def query(index):
                started[index].set()
                release.wait(3)
                return index
            jobs = [executor.submit(query, index) for index in range(2)]
            self.assertTrue(all(event.wait(1) for event in started))
            self.assertIsNone(executor.submit(lambda: None))
            control = executor.submit(lambda: "control responded", control=True)
            self.assertEqual(control.result(1), "control responded")
            self.assertFalse(executor.stop(timeout=0.01))
            self.assertFalse(executor.start())
            self.assertIsNone(executor.submit(lambda: None, control=True))
            release.set()
            wait(jobs, timeout=2)
            self.assertTrue(executor.stop(timeout=1))
            self.assertTrue(executor.start())
            self.assertEqual(executor.submit(lambda: "new generation").result(1), "new generation")
        finally:
            release.set()
            self.assertTrue(executor.stop(timeout=3))

    def test_stop_cancels_real_kernel_queries_but_drains_protected_work(self):
        import asyncio
        import threading
        from app.agent.kernel.transports import QueryEnvelope, TelegramKernelTransport
        from tests.test_agent_kernel_transports import make_session

        executor = adapter.TelegramAgentExecutor(max_queries=2, max_controls=1)
        executor.start()
        entered = threading.Event()
        exited = threading.Event()
        release_effect = threading.Event()

        class SlowModel:
            async def stream(self, request, *, cancellation):
                entered.set()
                try:
                    await cancellation.wait()
                    cancellation.raise_if_cancelled()
                    yield  # never reached; this is an async generator
                finally:
                    exited.set()

        session = make_session()
        session.model = SlowModel()
        transport = TelegramKernelTransport(session)
        try:
            def query():
                return asyncio.run(transport.query(
                    QueryEnvelope(owner="owner", session_id="session", message="slow"),
                    cancellation=adapter.AGENT_CANCELLATION.get(),
                ))
            job = executor.submit(query)
            self.assertTrue(entered.wait(1))
            effect = executor.submit(lambda: release_effect.wait(3), control=True)
            self.assertFalse(executor.stop(timeout=0.1))
            self.assertEqual(job.result(1).status, "cancelled")
            self.assertTrue(exited.is_set())
            self.assertFalse(effect.done())
            self.assertFalse(executor.start())
            release_effect.set()
            self.assertTrue(executor.stop(timeout=1))
        finally:
            release_effect.set()
            executor.stop(timeout=3)

    def test_registered_handlers_return_while_queries_run_and_controls_still_dispatch(self):
        import threading
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        executor = adapter.TelegramAgentExecutor(max_queries=2, max_controls=1)
        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        release = threading.Event()
        entered = [threading.Event(), threading.Event()]
        returned = [threading.Event(), threading.Event()]
        controlled = threading.Event()
        messages = [Message(text=f"slow{index}", message_id=11 + index) for index in range(2)]
        callers = []
        def query(bot, telebot, message):
            entered[message.message_id - 11].set()
            release.wait(3)
        values = {"TG_CHAT_ID": "-100", "TG_AGENT_ALLOWED_USER_IDS": "7"}
        try:
            with patch.object(adapter, "AGENT_EXECUTOR", executor), patch.object(
                handlers, "get", side_effect=lambda key, default="": values.get(key, default)
            ), patch.object(adapter, "handle_agent_message", side_effect=query), patch.object(
                adapter, "handle_agent_callback", side_effect=lambda *args: controlled.set()
            ):
                handlers._register_commands(bot, telebot)
                handler = next(fn for filters, fn in bot.message_handlers if fn.__name__ == "wrapped" and filters.get("func") and filters["func"](messages[0]))
                def receive(index):
                    handler(messages[index])
                    returned[index].set()
                callers = [threading.Thread(target=receive, args=(index,)) for index in range(2)]
                for caller in callers:
                    caller.start()
                self.assertTrue(all(event.wait(1) for event in entered))
                self.assertTrue(all(event.wait(0.2) for event in returned), "TeleBot worker仍等待整个Agent回合")
                call = Call("agk:c:plan_1234567890abcdef", Message(user_id=777))
                callback = next(fn for filters, fn in bot.callback_handlers if filters["func"](call))
                callback(call)
                self.assertTrue(controlled.wait(1))
                release.set()
                self.assertTrue(executor.stop(timeout=2))
        finally:
            release.set()
            for caller in callers:
                caller.join(2)
            executor.stop(timeout=3)

    def test_confirmed_effect_survives_stop_timeout_settles_progress_and_blocks_restart(self):
        import asyncio
        import threading
        from types import SimpleNamespace
        from app.bot import handlers
        from app.agent.kernel.capabilities import CapabilityRetriever, KernelToolSpec, ToolCatalog, ToolEffect
        from app.agent.kernel.effects import PreparedEffect
        from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
        from app.agent.kernel.pipeline import ToolPipeline
        from app.agent.kernel.session import AgentSession
        from app.agent.kernel.state import InMemorySessionStateStore
        from app.agent.kernel.transports import QueryEnvelope, TelegramKernelTransport
        from tests.test_agent_kernel_core import ScriptedModel

        entered, release, executed = threading.Event(), threading.Event(), threading.Event()

        def execute(arguments, snapshot, context):
            self.assertEqual(snapshot, "fixture")
            entered.set()
            self.assertTrue(release.wait(3))
            executed.set()
            return {"summary": "completed"}

        tool = KernelToolSpec(
            name="download.pause", domain="download", description="暂停下载", examples=("暂停下载",),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect=ToolEffect.WRITE,
            prepare=lambda *_: PreparedEffect(preview={"summary": "preview"}, snapshot_fingerprint="fixture"),
            execute_confirmed=execute,
        )
        catalog = ToolCatalog([tool])
        state = InMemorySessionStateStore()
        model = ScriptedModel([[
            ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("write", "download.pause", {})),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls"),
        ]])
        model.rounds.append([ModelEvent(ModelEventType.TEXT_DELTA, text="completed"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")])
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(catalog=catalog, state_store=state), state_store=state)
        transport = TelegramKernelTransport(session)
        owner = adapter.telegram_agent_owner(-100, 7)
        session_id = adapter.telegram_agent_session_id(-100, 7)
        preview = asyncio.run(transport.query(QueryEnvelope(
            owner=owner, session_id=session_id, message="暂停下载",
        )))
        bot = FakeBot()
        call = Call(
            f"agk:c:{preview.approval.plan_id}",
            Message("确认暂停下载", chat_id=-100, user_id=0, message_id=33),
        )
        runtime = SimpleNamespace(telegram=transport, store=state)
        progress_instances = []
        progress_class = adapter._ExistingMessageProgress

        def make_progress(*args):
            progress = progress_class(*args)
            progress_instances.append(progress)
            return progress

        executor = adapter.TelegramAgentExecutor()
        executor.start()
        saved_bot_state = (
            handlers._bot, handlers._bot_thread, handlers._bot_thread_stop,
            handlers._progress_recovery_thread, handlers._progress_recovery_stop,
            handlers._registered_bot_id,
        )
        handlers._bot = bot
        handlers._bot_thread = handlers._bot_thread_stop = None
        handlers._progress_recovery_thread = handlers._progress_recovery_stop = None
        try:
            with patch.object(adapter, "telegram_agent_access", return_value="allowed"), patch.object(
                adapter.agent_rate_limiter, "allow", return_value=True
            ), patch.object(adapter, "AGENT_EXECUTOR", executor
            ), patch.object(
                adapter, "get_agent_kernel_runtime", return_value=runtime
            ), patch.object(
                adapter, "_ExistingMessageProgress", side_effect=make_progress
            ), patch.object(
                adapter, "_settle_candidate_draft"
            ), patch.object(
                handlers, "_configuration_complete", return_value=True
            ), patch(
                "app.bot.progress._register_pending"
            ), patch(
                "app.bot.progress._update_pending", return_value=True
            ), patch(
                "app.bot.progress._remove_pending"
            ), patch(
                "app.bot.progress.stop_terminal_delivery_retries"
            ), patch(
                "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker"
            ):
                job = executor.submit(
                    lambda: adapter.handle_agent_callback(bot, call, TELEBOT),
                    control=True,
                )
                self.assertTrue(entered.wait(1))
                self.assertFalse(handlers.stop_bot(timeout=0.02))
                self.assertFalse(executed.is_set())
                self.assertTrue(progress_instances)
                self.assertIn("勿重复提交", bot.edits[-1][0])
                self.assertNotIn("已中断", bot.edits[-1][0])
                self.assertFalse(progress_instances[0].finished_event.is_set())
                self.assertFalse(handlers.start_bot())
                release.set()
                job.result(2)
                self.assertTrue(executed.is_set())
                self.assertIn("completed", bot.edits[-1][0])
                self.assertTrue(progress_instances[0].finished_event.is_set())
                self.assertTrue(executor.stop(timeout=1))
        finally:
            release.set()
            executor.stop(timeout=3)
            (
                handlers._bot, handlers._bot_thread, handlers._bot_thread_stop,
                handlers._progress_recovery_thread, handlers._progress_recovery_stop,
                handlers._registered_bot_id,
            ) = saved_bot_state
