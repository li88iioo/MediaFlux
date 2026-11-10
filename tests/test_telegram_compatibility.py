"""Telegram SDK 参数、进度错误与不重放合同；所有传输均为本地 mock。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import telebot
from telebot.apihelper import ApiTelegramException

from app import database as db
from app.bot import agent_adapter
from app.bot.progress import TelegramProgress, recover_stale_operations
from tests.support import IsolatedDatabaseTestCase


class _Bot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.error = None

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=42)

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edits.append((text, chat_id, message_id, kwargs))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(message_id=message_id)


_SOURCE = SimpleNamespace(
    chat=SimpleNamespace(id=100), message_id=18, message_thread_id=7,
)
_UNCHANGED = "Bad Request: message is not modified"
_UNCHANGED_FULL = (
    _UNCHANGED + ": specified new message content and reply markup are exactly "
    "the same as a current content and reply markup of the message"
)


def _api_error(code, description, *, retry_after=0):
    return ApiTelegramException("editMessageText", None, {
        "ok": False, "error_code": code, "description": description,
        "parameters": {"retry_after": retry_after},
    })


class TelegramCompatibilityTests(IsolatedDatabaseTestCase):
    def setUp(self):
        db.kv_set("telegram_pending_operations_v1", "[]")
        start = patch("app.bot.progress.threading.Thread.start")
        start.start()
        self.addCleanup(start.stop)

    def progress(self, bot=None, module=telebot):
        bot = _Bot() if bot is None else bot
        progress = TelegramProgress(
            bot, module, 100, "测试任务", source_message=_SOURCE,
            prefer_persistent_message=True,
        ).begin("正在准备")
        self.addCleanup(progress._claim_finished)
        return progress

    def test_real_sdk_uses_modern_parameters_without_warning_and_keeps_thread_reply(self):
        bot = telebot.TeleBot("123456:local-mock-only", threaded=False)
        payload = {
            "message_id": 42, "date": 0, "chat": {"id": 100, "type": "private"},
            "text": "local mock",
        }
        with (
            patch("telebot.apihelper._make_request", return_value=payload) as request,
            self.assertNoLogs("TeleBot", level="WARNING"),
        ):
            progress = self.progress(bot)
            self.assertTrue(progress.update("正在处理"))
            self.assertTrue(progress.finish_many(("第一段终态", "第二段终态")))
        calls = [call for call in request.call_args_list if call.args[1] != "sendChatAction"]
        self.assertEqual(len(calls), 4)
        for call in calls:
            params = call.kwargs["params"]
            self.assertNotIn("disable_web_page_preview", params)
            self.assertNotIn("reply_to_message_id", params)
            self.assertTrue(json.loads(params["link_preview_options"])["is_disabled"])
        first = calls[0].kwargs["params"]
        self.assertEqual(json.loads(first["reply_parameters"])["message_id"], 18)
        self.assertEqual(first["message_thread_id"], 7)
        followup = calls[-1].kwargs["params"]
        self.assertNotIn("reply_parameters", followup)
        self.assertEqual(followup["message_thread_id"], 7)

    def test_legacy_signatures_select_legacy_keywords_before_any_request(self):
        class LegacyBot:
            def __init__(self):
                self.recorder = _Bot()
                self.sent = self.recorder.sent
                self.edits = self.recorder.edits

            def send_message(
                self, chat_id, text, *, parse_mode, disable_web_page_preview,
                reply_to_message_id=None, message_thread_id=None,
            ):
                return self.recorder.send_message(
                    chat_id, text, disable_web_page_preview=disable_web_page_preview,
                    reply_to_message_id=reply_to_message_id,
                    message_thread_id=message_thread_id,
                )

            def edit_message_text(
                self, text, chat_id, message_id, *, parse_mode, disable_web_page_preview,
            ):
                return self.recorder.edit_message_text(
                    text, chat_id, message_id,
                    disable_web_page_preview=disable_web_page_preview,
                )
        bot = LegacyBot()
        progress = self.progress(bot)
        self.assertTrue(progress.update("处理中"))
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0][2]["reply_to_message_id"], 18)
        self.assertTrue(bot.edits[0][3]["disable_web_page_preview"])

    def test_missing_sdk_types_keep_fake_compatible(self):
        progress = self.progress(module=SimpleNamespace(types=SimpleNamespace()))
        self.assertTrue(progress.update("处理中"))
        self.assertEqual(progress.bot.sent[0][2]["reply_to_message_id"], 18)
        self.assertTrue(progress.bot.sent[0][2]["disable_web_page_preview"])

    def test_type_error_after_sender_was_called_never_retries_with_other_parameters(self):
        bot = _Bot()
        bot.send_message = Mock(side_effect=TypeError("response decoding failed"))
        progress = TelegramProgress(bot, telebot, 100, "test", source_message=_SOURCE)
        result = progress._send_real_result("正文")
        self.assertFalse(result.ok)
        self.assertTrue(result.outcome_unknown)
        bot.send_message.assert_called_once()

    def test_initial_and_repeated_progress_are_noops_but_terminal_still_edits_markup(self):
        progress = self.progress()
        self.assertTrue(progress.update("正在准备"))
        self.assertEqual(progress.bot.edits, [])
        self.assertTrue(progress.update("处理中"))
        self.assertTrue(progress.update("处理中"))
        self.assertEqual(len(progress.bot.edits), 1)
        markup = SimpleNamespace(buttons=["终态按钮"])
        self.assertTrue(progress.finish("处理中", reply_markup=markup))
        self.assertEqual(len(progress.bot.edits), 2)
        self.assertIs(progress.bot.edits[-1][3]["reply_markup"], markup)
        self.assertEqual(len(progress.bot.sent), 1)
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")

    def test_only_canonical_400_not_modified_is_success_and_does_not_create_fallback(self):
        for description in (_UNCHANGED, _UNCHANGED_FULL):
            with self.subTest(description=description):
                progress = self.progress()
                progress.bot.error = _api_error(400, description)
                self.assertTrue(progress.update("正在处理"))
                self.assertTrue(progress.update("正在处理"))
                self.assertEqual(len(progress.bot.edits), 1)
                self.assertTrue(progress.finish("最终结果"))
                self.assertEqual(len(progress.bot.sent), 1)

    def test_not_modified_lookalikes_or_wrong_status_are_not_swallowed(self):
        for code, description in (
            (403, _UNCHANGED), (429, _UNCHANGED),
            (400, "Bad Request: can't parse entities: message is not modified"),
            (400, _UNCHANGED + " (echoed user text)"),
        ):
            with self.subTest(code=code, description=description):
                progress = self.progress()
                progress.bot.error = _api_error(code, description, retry_after=5)
                self.assertFalse(progress.finish("最终结果"))
                self.assertEqual(len(progress.bot.sent), 1)

    def test_429_progress_obeys_retry_after_and_does_not_cache_failed_text(self):
        progress = self.progress()
        progress.bot.error = _api_error(429, "Too Many Requests: retry after 12", retry_after=12)
        with patch("app.bot.progress.time.monotonic", return_value=100.0) as clock:
            self.assertFalse(progress.update("处理中"))
            progress.bot.error = None
            clock.return_value = 111.9
            self.assertFalse(progress.update("更新的进度"))
            self.assertEqual(len(progress.bot.edits), 1)
            clock.return_value = 112.0
            self.assertTrue(progress.update("处理中"))
        self.assertEqual(len(progress.bot.edits), 2)
        self.assertEqual(len(progress.bot.sent), 1)
        self.assertTrue(progress.finish("最终结果"))

    def test_api_failure_logs_only_safe_code_classification_and_controlled_description(self):
        progress = self.progress()
        private = (
            "Bad Request: can't parse entities: <PRIVATE_BODY_姓名电话> "
            "https://api.telegram.org/bot123456:PRIVATE_BOT_TOKEN_123456789/sendMessage "
            "https://private.invalid/user/path?token=PRIVATE_QUERY "
            "123456:PRIVATE_BOT_TOKEN_123456789"
        )
        progress.bot.error = _api_error(400, private)
        with self.assertLogs("app.bot.progress", level="INFO") as capture:
            self.assertFalse(progress.update("PRIVATE_MESSAGE_BODY"))
        output = "\n".join(capture.output)
        self.assertIn("status=400", output)
        self.assertIn("category=invalid_format", output)
        for sensitive in ("PRIVATE", "https://", "api.telegram.org", "123456:", "姓名电话"):
            self.assertNotIn(sensitive, output)
        self.assertEqual(len(progress.bot.sent), 1)

    def test_rate_limited_terminal_recovery_does_not_retry_before_deadline(self):
        progress = self.progress()
        progress.bot.error = _api_error(429, "Too Many Requests: retry after 12", retry_after=12)
        with patch("app.bot.progress.time.time", return_value=1000.0) as clock:
            self.assertFalse(progress.finish("最终结果"))
            self.assertEqual(len(progress.bot.edits), 1)
            progress.bot.error = None
            clock.return_value = 1011.9
            self.assertEqual(recover_stale_operations(progress.bot, telebot), 0)
            self.assertEqual(len(progress.bot.edits), 1)
            clock.return_value = 1012.0
            self.assertEqual(recover_stale_operations(progress.bot, telebot), 1)
        self.assertEqual(len(progress.bot.edits), 2)
        self.assertEqual(progress.bot.edits[-1][0], "最终结果")
        self.assertEqual(len(progress.bot.sent), 1)

    def test_callback_progress_shares_noop_and_429_protection(self):
        bot = _Bot()
        progress = agent_adapter._ExistingMessageProgress(bot, _SOURCE)
        self.assertTrue(progress.update("确认执行中"))
        self.assertTrue(progress.update("确认执行中"))
        self.assertEqual(len(bot.edits), 1)
        bot.error = _api_error(429, "Too Many Requests: retry after 9", retry_after=9)
        with patch("app.bot.progress.time.monotonic", return_value=50.0) as clock:
            self.assertFalse(progress.update("继续处理"))
            bot.error = None
            clock.return_value = 58.0
            self.assertFalse(progress.update("结果准备中"))
            self.assertEqual(len(bot.edits), 2)
            clock.return_value = 59.0
            self.assertTrue(progress.update("结果准备中"))
        self.assertIsNone(bot.edits[-1][3]["reply_markup"])

    def test_callback_terminal_unchanged_never_sends_duplicate(self):
        bot = _Bot()
        bot.error = _api_error(400, _UNCHANGED_FULL)
        agent_adapter._edit_final(bot, _SOURCE, "任务完成")
        self.assertEqual(bot.sent, [])

    def test_callback_terminal_unknown_or_api_error_never_sends_fallback_or_more_chunks(self):
        for error in (
            requests.ReadTimeout("PRIVATE_TOKEN in failed response"),
            requests.exceptions.SSLError("PRIVATE_TLS_URL"),
            _api_error(429, "Too Many Requests: retry after 10", retry_after=10),
            _api_error(403, "Forbidden: PRIVATE_BODY"),
            _api_error(400, "Bad Request: can't parse entities: PRIVATE_BODY"),
        ):
            with self.subTest(type=type(error).__name__):
                bot = _Bot()
                bot.error = error
                with self.assertLogs("app.bot.progress", level="WARNING") as capture:
                    agent_adapter._edit_final(bot, _SOURCE, "长结果" * 4000)
                self.assertEqual(len(bot.edits), 1)
                self.assertEqual(bot.sent, [])
                self.assertNotIn("PRIVATE", "\n".join(capture.output))

    def test_callback_terminal_explicit_missing_message_may_fallback_once(self):
        bot = _Bot()
        bot.error = _api_error(400, "Bad Request: message to edit not found")
        markup = SimpleNamespace(buttons=["终态按钮"])
        agent_adapter._edit_final(bot, _SOURCE, "最终结果", reply_markup=markup)
        self.assertEqual(len(bot.edits), 1)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0][2]["message_thread_id"], 7)
        self.assertIs(bot.sent[0][2]["reply_markup"], markup)

    def test_confirmation_without_progress_event_keeps_original_topic_on_fallback(self):
        bot = _Bot()
        bot.error = _api_error(400, "Bad Request: message can't be edited")
        progress = agent_adapter._ExistingMessageProgress(bot, _SOURCE)
        self.assertTrue(progress.finish("已结束", clear_reply_markup=True))
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0][2].get("message_thread_id"), 7)

    def test_callback_fallback_retires_old_keyboard_only_after_replacement_delivery(self):
        for deletion_fails, sending_fails in ((False, False), (True, False), (False, True)):
            with self.subTest(deletion_fails=deletion_fails, sending_fails=sending_fails):
                class RetiringBot(_Bot):
                    def __init__(self):
                        super().__init__()
                        self.deleted = []
                        self.cleared = []

                    def send_message(self, chat_id, text, **kwargs):
                        if sending_fails:
                            raise requests.ReadTimeout("replacement response unknown")
                        return super().send_message(chat_id, text, **kwargs)

                    def delete_message(self, chat_id, message_id):
                        self.deleted.append((chat_id, message_id))
                        if deletion_fails:
                            raise _api_error(400, "Bad Request: message can't be deleted")
                        return True

                    def edit_message_reply_markup(self, chat_id, message_id, **kwargs):
                        self.cleared.append((chat_id, message_id, kwargs))
                        return SimpleNamespace(message_id=message_id)

                bot = RetiringBot()
                bot.error = _api_error(400, "Bad Request: message can't be edited")
                delivered = agent_adapter._edit_final(bot, _SOURCE, "处理完成")
                self.assertEqual(delivered, not sending_fails)
                self.assertEqual(bot.deleted, [] if sending_fails else [(100, 18)])
                if deletion_fails:
                    self.assertEqual(bot.cleared, [(100, 18, {"reply_markup": None})])
                else:
                    self.assertEqual(bot.cleared, [])
                if not sending_fails:
                    self.assertEqual(len(bot.sent), 1)
                    self.assertEqual(bot.sent[0][2]["message_thread_id"], 7)

    def test_raw_exception_text_without_api_400_is_not_not_modified_success(self):
        progress = self.progress()
        progress.bot.error = RuntimeError(_UNCHANGED)
        self.assertFalse(progress.update("新内容"))
        self.assertFalse(progress.finish("最终结果"))
        self.assertTrue(progress.terminal_outcome_unknown)
        self.assertEqual(len(progress.bot.sent), 1)
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")

    def test_unknown_edit_invalidates_previous_success_cache_without_sending_new_message(self):
        progress = self.progress()
        progress.bot.error = requests.ReadTimeout("response lost")
        self.assertFalse(progress.update("可能已经生效的新内容"))
        progress.bot.error = None
        self.assertTrue(progress.update("正在准备"))
        self.assertEqual(len(progress.bot.edits), 2)
        self.assertEqual(len(progress.bot.sent), 1)

    def test_same_text_with_markup_clear_is_not_skipped(self):
        progress = self.progress()
        self.assertTrue(progress.update("正在准备", clear_reply_markup=True))
        self.assertEqual(len(progress.bot.edits), 1)
        self.assertIsNone(progress.bot.edits[0][3]["reply_markup"])
        self.assertTrue(progress.update("正在准备", clear_reply_markup=True))
        self.assertEqual(len(progress.bot.edits), 1)

    def test_duplicate_update_cache_is_local_to_operation(self):
        first, second = self.progress(), self.progress()
        self.assertTrue(first.update("处理中"))
        self.assertTrue(second.update("处理中"))
        self.assertEqual(len(first.bot.edits), 1)
        self.assertEqual(len(second.bot.edits), 1)

    def test_rejected_progress_can_still_deliver_terminal_without_fallback_message(self):
        progress = self.progress()
        progress.bot.error = _api_error(400, "Bad Request: can't parse entities")
        self.assertFalse(progress.update("<broken>进度"))
        progress.bot.error = None
        self.assertTrue(progress.finish("最终结果"))
        self.assertEqual(progress.bot.edits[-1][0], "最终结果")
        self.assertEqual(len(progress.bot.sent), 1)
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")

    def test_callback_modern_sdk_success_has_no_deprecated_warnings(self):
        bot = telebot.TeleBot("123456:local-mock-only", threaded=False)
        payload = {
            "message_id": 18, "date": 0, "chat": {"id": 100, "type": "private"},
            "text": "local mock",
        }
        with (
            patch("telebot.apihelper._make_request", return_value=payload) as request,
            self.assertNoLogs("TeleBot", level="WARNING"),
        ):
            progress = agent_adapter._ExistingMessageProgress(bot, _SOURCE)
            self.assertTrue(progress.update("执行中"))
            self.assertTrue(progress.finish("任务完成", clear_reply_markup=True))
        self.assertEqual(request.call_count, 3)  # typing + 进度编辑 + 终态编辑
        edits = [call for call in request.call_args_list if call.args[1] == "editMessageText"]
        self.assertEqual(len(edits), 2)
        for call in edits:
            params = call.kwargs["params"]
            self.assertNotIn("disable_web_page_preview", params)
            self.assertTrue(json.loads(params["link_preview_options"])["is_disabled"])

    def test_reply_only_fake_does_not_receive_duplicate_reply_reference(self):
        class ReplyOnlyBot:
            def __init__(self):
                self.calls = []

            def reply_to(self, source, text, **kwargs):
                self.calls.append((source, text, kwargs))
                return SimpleNamespace(message_id=42)
        bot = ReplyOnlyBot()
        progress = self.progress(bot)
        self.assertEqual(progress.mode, "reply")
        self.assertEqual(len(bot.calls), 1)
        self.assertNotIn("reply_to_message_id", bot.calls[0][2])
        self.assertNotIn("reply_parameters", bot.calls[0][2])
        self.assertIs(bot.calls[0][0], _SOURCE)

    def test_draft_progress_deduplicates_only_successful_updates_and_respects_429(self):
        class DraftBot(_Bot):
            def __init__(self):
                super().__init__()
                self.drafts = []
                self.accepted = True

            def send_message_draft(self, chat_id, draft_id, text, **kwargs):
                self.drafts.append(text)
                if self.error is not None:
                    raise self.error
                return self.accepted
        bot = DraftBot()
        progress = TelegramProgress(bot, telebot, 100, "草稿").begin("正在准备")
        self.addCleanup(progress._claim_finished)
        self.assertEqual(progress.mode, "draft")
        self.assertTrue(progress.update("正在准备"))
        self.assertEqual(bot.drafts, ["正在准备"])
        bot.accepted = False
        self.assertFalse(progress.update("处理中"))
        bot.accepted = True
        self.assertTrue(progress.update("处理中"))
        self.assertEqual(bot.drafts.count("处理中"), 2)
        bot.error = _api_error(429, "rate limited", retry_after=5)
        with patch("app.bot.progress.time.monotonic", return_value=10.0) as clock:
            self.assertFalse(progress.update("新进度"))
            bot.error = None
            clock.return_value = 14.9
            self.assertFalse(progress.update("新进度"))
            self.assertEqual(bot.drafts.count("新进度"), 1)
            clock.return_value = 15.0
            self.assertTrue(progress.update("新进度"))
        self.assertEqual(bot.sent, [])

    def test_repeated_terminal_429_extends_deadline_without_replaying_send(self):
        progress = self.progress()
        progress.bot.error = _api_error(429, "rate limited", retry_after=10)
        with patch("app.bot.progress.time.time", return_value=1000.0) as clock:
            self.assertFalse(progress.finish("最终结果"))
            clock.return_value = 1010.0
            progress.bot.error = _api_error(429, "rate limited", retry_after=30)
            self.assertEqual(recover_stale_operations(progress.bot, telebot), 0)
            progress.bot.error = None
            clock.return_value = 1039.9
            self.assertEqual(recover_stale_operations(progress.bot, telebot), 0)
            self.assertEqual(len(progress.bot.edits), 2)
            clock.return_value = 1040.0
            self.assertEqual(recover_stale_operations(progress.bot, telebot), 1)
        self.assertEqual(len(progress.bot.edits), 3)
        self.assertEqual(len(progress.bot.sent), 1)

    def test_network_failure_logs_distinguish_timeout_tls_without_original_url(self):
        progress = self.progress()
        for error, category in (
            (requests.ReadTimeout("https://PRIVATE.invalid/"), "read_timeout"),
            (requests.exceptions.SSLError("https://PRIVATE.invalid/"), "tls_error"),
            (requests.ConnectTimeout("https://PRIVATE.invalid/"), "connect_timeout"),
            (RuntimeError("PRIVATE_BODY"), "outcome_unknown"),
        ):
            with self.subTest(category=category):
                progress.bot.error = error
                with self.assertLogs("app.bot.progress", level="INFO") as capture:
                    self.assertFalse(progress.update("新内容"))
                output = "\n".join(capture.output)
                self.assertIn("category=" + category, output)
                self.assertNotIn("PRIVATE", output)
                self.assertNotIn("https://", output)
        self.assertEqual(len(progress.bot.sent), 1)

    def test_display_boundary_contains_preview_constructor_failure_and_task_can_finish(self):
        progress = self.progress()
        business = Mock(return_value="最终结果")
        with (
            patch.object(telebot.types, "LinkPreviewOptions", side_effect=ValueError(
                "PRIVATE_BODY https://private.invalid/bot123456:PRIVATE_TOKEN"
            )),
            self.assertLogs("app.bot.progress", level="INFO") as capture,
        ):
            self.assertFalse(progress.update("新的进度"))
            business()
        self.assertEqual(capture.records[0].getMessage(),
                         "Telegram 进度更新失败 category=adapter_error type=ValueError")
        business.assert_called_once_with()
        self.assertEqual(progress.bot.edits, [])
        self.assertEqual(progress._last_rendered, "正在准备")
        self.assertEqual(progress._update_retry_at, 0.0)
        self.assertFalse(progress._finished)
        self.assertTrue(progress.finish(business.return_value))
        self.assertEqual(len(progress.bot.sent), 1)
        self.assertEqual(len(progress.bot.edits), 1)

    def test_display_boundary_contains_edit_property_failure_without_resending(self):
        class PropertyBot(_Bot):
            broken = False

            def editing_property(self):
                if self.broken:
                    raise RuntimeError("PRIVATE_PROPERTY https://private.invalid/")
                return super().edit_message_text
        setattr(PropertyBot, "edit_message_text", property(PropertyBot.editing_property))
        bot = PropertyBot()
        progress = self.progress(bot)
        bot.broken = True
        with self.assertLogs("app.bot.progress", level="INFO") as capture:
            self.assertFalse(progress.update("新的进度"))
        self.assertEqual(capture.records[0].getMessage(),
                         "Telegram 进度更新失败 category=adapter_error type=RuntimeError")
        self.assertEqual(bot.edits, [])
        self.assertEqual(progress._last_rendered, "正在准备")
        bot.broken = False
        self.assertTrue(progress.finish("最终结果"))
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(len(bot.edits), 1)

    def test_display_boundary_contains_rich_message_adapter_failure_before_transport(self):
        progress = self.progress()
        progress.mode = "rich_draft"
        progress.bot.send_rich_message_draft = Mock()
        with (
            patch("app.bot.progress._rich_message", side_effect=RuntimeError("PRIVATE_RICH")),
            self.assertLogs("app.bot.progress", level="INFO") as capture,
        ):
            self.assertFalse(progress.update("新的进度"))
        self.assertEqual(capture.records[0].getMessage(),
                         "Telegram 进度更新失败 category=adapter_error type=RuntimeError")
        progress.bot.send_rich_message_draft.assert_not_called()
        self.assertEqual(progress._last_rendered, "正在准备")
        self.assertEqual(len(progress.bot.sent), 1)
        progress.mode = "edit"
        self.assertTrue(progress.finish("最终结果"))

    def test_display_boundary_summary_failure_keeps_429_cooldown_and_cache_protocol(self):
        progress = self.progress()
        progress.bot.error = _api_error(429, "rate limited", retry_after=12)
        with patch("app.bot.progress.time.monotonic", return_value=100.0) as clock:
            with (
                patch("app.bot.progress.telegram_error_summary", side_effect=RuntimeError(
                    "PRIVATE_SUMMARY"
                )),
                self.assertLogs("app.bot.progress", level="INFO") as capture,
            ):
                self.assertFalse(progress.update("新的进度"))
            self.assertEqual(capture.records[0].getMessage(),
                             "Telegram 进度更新失败 category=adapter_error type=RuntimeError")
            self.assertEqual(progress._update_retry_at, 112.0)
            self.assertEqual(progress._last_rendered, "正在准备")
            progress.bot.error = None
            clock.return_value = 111.9
            self.assertFalse(progress.update("新的进度"))
            self.assertEqual(len(progress.bot.edits), 1)
            clock.return_value = 112.0
            self.assertTrue(progress.update("新的进度"))
        self.assertEqual(progress._last_rendered, "新的进度")
        self.assertTrue(progress.finish("最终结果"))
        self.assertEqual(len(progress.bot.sent), 1)

    def test_display_boundary_summary_failure_keeps_unknown_result_cache_invalidation(self):
        progress = self.progress()
        progress.bot.error = requests.ReadTimeout("PRIVATE_RESPONSE")
        with (
            patch("app.bot.progress.telegram_error_summary", side_effect=RuntimeError(
                "PRIVATE_SUMMARY"
            )),
            self.assertLogs("app.bot.progress", level="INFO") as capture,
        ):
            self.assertFalse(progress.update("可能已生效的新内容"))
        self.assertEqual(capture.records[0].getMessage(),
                         "Telegram 进度更新失败 category=adapter_error type=RuntimeError")
        self.assertIsNone(progress._last_rendered)
        self.assertEqual(progress._update_retry_at, 0.0)
        self.assertEqual(len(progress.bot.edits), 1)
        self.assertEqual(len(progress.bot.sent), 1)
        progress.bot.error = None
        self.assertTrue(progress.finish("最终结果"))
        self.assertEqual(len(progress.bot.sent), 1)
