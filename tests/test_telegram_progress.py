from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import database as db
from app.bot import handlers as bot_handlers
from app.bot.progress import (
    TelegramProgress,
    _register_pending,
    _retry_terminal_until_delivered,
    deliver_terminal_to_existing_message,
    recover_stale_operations,
)
from app.bot.handlers import _recover_stale_progress_until_delivered
from tests.support import IsolatedDatabaseTestCase


class _RichBot:
    def __init__(self):
        self.rich_drafts = []
        self.rich_messages = []
        self.text_drafts = []
        self.actions = []
        self.messages = []

    def send_chat_action(self, chat_id, action, message_thread_id=None):
        self.actions.append((chat_id, action, message_thread_id))

    def send_rich_message_draft(self, chat_id, draft_id, message, **kwargs):
        self.rich_drafts.append((chat_id, draft_id, message.html, kwargs))
        return True

    def send_rich_message(self, chat_id, message, **kwargs):
        self.rich_messages.append((chat_id, message.html, kwargs))
        return SimpleNamespace(message_id=12)

    def send_message_draft(self, chat_id, draft_id, text, **kwargs):
        self.text_drafts.append((chat_id, draft_id, text, kwargs))
        return True

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=13)


class _TextDraftBot(_RichBot):
    send_rich_message_draft = None
    send_rich_message = None


class _EditBot:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.deleted = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=21)

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edits.append((chat_id, message_id, text, kwargs))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True


class _FailingFinalEditBot(_EditBot):
    def __init__(self):
        super().__init__()
        self.edit_calls = 0

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edit_calls += 1
        if self.edit_calls >= 1:
            raise RuntimeError("edit unavailable")
        return super().edit_message_text(text, chat_id, message_id, **kwargs)


class _OfflineAfterBeginBot(_EditBot):
    def send_message(self, chat_id, text, **kwargs):
        if not self.messages:
            return super().send_message(chat_id, text, **kwargs)
        raise RuntimeError("offline")

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        raise RuntimeError("offline")


class _FailingDeleteBot(_EditBot):
    def delete_message(self, chat_id, message_id):
        raise RuntimeError("delete unavailable")


class _FailingRichDraftClearBot(_RichBot):
    def send_rich_message_draft(self, chat_id, draft_id, message, **kwargs):
        if message.html == "":
            raise RuntimeError("draft cleanup unavailable")
        return super().send_rich_message_draft(chat_id, draft_id, message, **kwargs)


_TELEBOT = SimpleNamespace(types=SimpleNamespace(
    InputRichMessage=lambda html: SimpleNamespace(html=html),
))


class TelegramProgressTests(IsolatedDatabaseTestCase):
    def setUp(self):
        db.kv_set("telegram_pending_operations_v1", "[]")

    def test_existing_message_terminal_uses_edit_then_send_fallback(self):
        bot = _FailingFinalEditBot()
        source = SimpleNamespace(chat=SimpleNamespace(id="100"), message_id=44)
        delivered = deliver_terminal_to_existing_message(
            bot, _TELEBOT, source, "<b>操作完成</b>", label="Agent 操作结果"
        )
        self.assertTrue(delivered)
        self.assertEqual(bot.messages[-1][1], "<b>操作完成</b>")
        self.assertEqual(json.loads(db.kv_get("telegram_pending_operations_v1", "[]")), [])

    def test_prefers_rich_draft_and_persists_rich_final_message(self):
        bot = _RichBot()
        progress = TelegramProgress(bot, _TELEBOT, "100", "资源搜索", timeout_seconds=60)
        progress.begin("<b>正在搜索</b>\n阶段：准备中")
        self.assertEqual(progress.mode, "rich_draft")
        self.assertEqual(
            bot.rich_drafts[-1][2],
            "<p><b>正在搜索</b><br>阶段：准备中</p>",
        )
        self.assertTrue(progress.update("<b>仍在搜索</b>"))
        self.assertTrue(progress.finish(
            "<b>搜索完成</b>\n\n<b>状态</b>  已完成\n<b>概览</b>  2 个来源"
        ))
        self.assertEqual(
            bot.rich_messages[-1][1],
            "<p><b>搜索完成</b></p>"
            "<p><b>状态</b>  已完成<br><b>概览</b>  2 个来源</p>",
        )
        self.assertEqual(bot.rich_drafts[-1][2], "")
        self.assertEqual(bot.text_drafts, [])
        self.assertFalse(progress.update("不应覆盖终态"))
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")

    def test_progress_typing_stays_in_forum_topic(self):
        bot = _RichBot()
        source = SimpleNamespace(
            chat=SimpleNamespace(id="100"),
            message_id=44,
            message_thread_id=77,
        )
        progress = TelegramProgress(
            bot,
            _TELEBOT,
            "100",
            "资源搜索",
            source_message=source,
            timeout_seconds=60,
        )
        progress.begin("搜索中")
        try:
            self.assertIn(("100", "typing", 77), bot.actions)
            self.assertEqual(bot.rich_drafts[-1][3]["message_thread_id"], 77)
        finally:
            progress.finish("搜索完成")

    def test_restart_recovery_keeps_draft_cleanup_and_fallback_in_forum_topic(self):
        _register_pending({
            "id": "topic-recovery",
            "chat_id": "100",
            "label": "资源搜索",
            "mode": "rich_draft",
            "draft_id": 99,
            "message_id": 44,
            "message_thread_id": 77,
            "started_at": 1,
            "deadline": 2,
            "terminal_text": "<b>搜索已中断</b>",
            "terminal_pending": True,
        })

        class RecoveryBot(_RichBot):
            def edit_message_text(self, *_args, **_kwargs):
                raise RuntimeError("message is no longer editable")

        bot = RecoveryBot()
        self.assertEqual(recover_stale_operations(bot, _TELEBOT), 1)
        self.assertEqual(bot.rich_drafts[-1][1:3], (99, ""))
        self.assertEqual(bot.rich_drafts[-1][3]["message_thread_id"], 77)
        self.assertEqual(bot.messages[-1][2]["message_thread_id"], 77)
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")

    def test_falls_back_to_text_draft_then_editable_message(self):
        draft_bot = _TextDraftBot()
        draft = TelegramProgress(draft_bot, _TELEBOT, "100", "RSS", timeout_seconds=60)
        draft.begin("刷新中")
        self.assertEqual(draft.mode, "draft")
        self.assertTrue(draft.finish("刷新完成"))
        self.assertEqual(draft_bot.messages[-1][1], "刷新完成")

        edit_bot = _EditBot()
        edit = TelegramProgress(edit_bot, _TELEBOT, "100", "整理", timeout_seconds=60)
        edit.begin("整理中")
        self.assertEqual(edit.mode, "edit")
        self.assertTrue(edit.finish("整理完成"))
        self.assertEqual(edit_bot.edits[-1][2], "整理完成")

    def test_dismiss_source_message_removes_confirmation_and_detaches_future_sends(self):
        bot = _EditBot()
        source = SimpleNamespace(
            chat=SimpleNamespace(id="100"),
            message_id=8,
        )
        progress = TelegramProgress(
            bot,
            _TELEBOT,
            "100",
            "整理",
            source_message=source,
            timeout_seconds=60,
        )
        progress.begin("整理中")

        self.assertEqual(bot.messages[-1][2]["reply_to_message_id"], 8)
        self.assertTrue(progress.dismiss_source_message())
        self.assertEqual(bot.deleted, [("100", 8)])
        self.assertIsNone(progress.source_message)

    def test_dismiss_source_message_detaches_even_when_delete_fails(self):
        bot = _FailingDeleteBot()
        source = SimpleNamespace(chat=SimpleNamespace(id="100"), message_id=8)
        progress = TelegramProgress(
            bot, _TELEBOT, "100", "整理", source_message=source, timeout_seconds=60,
        )
        progress.begin("整理中")

        self.assertFalse(progress.dismiss_source_message())
        self.assertIsNone(progress.source_message)
        self.assertTrue(progress.finish("整理完成"))
        self.assertEqual(bot.edits[-1][2], "整理完成")


    def test_dismiss_cleanup_failure_never_becomes_restart_terminal_receipt(self):
        bot = _FailingRichDraftClearBot()
        progress = TelegramProgress(
            bot, _TELEBOT, "100", "光鸭 STRM 完整同步", timeout_seconds=60
        )
        progress.begin("同步中")

        self.assertFalse(progress.dismiss("光鸭 STRM 同步已结束，汇总消息已发送。"))
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")
        self.assertEqual(bot.rich_messages, [])

    def test_restart_recovery_discards_legacy_strm_cleanup_receipt_silently(self):
        db.kv_set("telegram_pending_operations_v1", json.dumps([{
            "id": "legacy-strm-cleanup",
            "chat_id": "100",
            "label": "光鸭 STRM 完整同步",
            "mode": "rich_draft",
            "draft_id": 99,
            "terminal_text": "光鸭 STRM 同步已结束，汇总消息已发送。",
            "terminal_pending": True,
        }], ensure_ascii=False))
        bot = _RichBot()

        self.assertEqual(recover_stale_operations(bot, _TELEBOT), 1)
        self.assertEqual(bot.rich_drafts[-1][1:3], (99, ""))
        self.assertEqual(bot.messages, [])
        self.assertEqual(bot.rich_messages, [])
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")

    def test_failed_terminal_delivery_stays_persisted_for_restart_recovery(self):
        bot = _OfflineAfterBeginBot()
        progress = TelegramProgress(bot, _TELEBOT, "100", "资源搜索", timeout_seconds=60)
        progress.begin("搜索中")

        self.assertFalse(progress.finish("<b>搜索完成</b>\n找到 3 项资源"))
        rows = json.loads(db.kv_get("telegram_pending_operations_v1", "[]"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["terminal_text"], "<b>搜索完成</b>\n找到 3 项资源")
        self.assertTrue(rows[0]["terminal_pending"])

        recovery_bot = _EditBot()
        self.assertEqual(recover_stale_operations(recovery_bot), 1)
        self.assertEqual(recovery_bot.edits[-1][2], "<b>搜索完成</b>\n找到 3 项资源")
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")

    def test_runtime_terminal_retry_delivers_without_process_restart(self):
        _register_pending({
            "id": "runtime-terminal",
            "chat_id": "100",
            "label": "Agent 操作结果",
            "mode": "edit",
            "message_id": 44,
            "message_thread_id": 77,
            "started_at": 1,
            "deadline": 2,
            "terminal_text": "<b>操作已完成</b>",
            "terminal_pending": True,
            "clear_reply_markup": True,
        })
        bot = _FailingFinalEditBot()
        stop_event = __import__("threading").Event()

        recovered = _retry_terminal_until_delivered(
            bot, _TELEBOT, "runtime-terminal", stop_event, delays=(0.0,)
        )

        self.assertEqual(recovered, 1)
        self.assertEqual(bot.messages[-1][1], "<b>操作已完成</b>")
        self.assertEqual(bot.messages[-1][2]["message_thread_id"], 77)
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")

    def test_pending_operations_are_not_silently_truncated(self):
        for index in range(51):
            _register_pending({
                "id": f"operation-{index}",
                "chat_id": "100",
                "label": f"任务 {index}",
            })

        rows = json.loads(db.kv_get("telegram_pending_operations_v1", "[]"))
        self.assertEqual(len(rows), 51)
        self.assertEqual(rows[0]["id"], "operation-0")
        self.assertEqual(rows[-1]["id"], "operation-50")

    def test_final_edit_failure_falls_back_to_new_message(self):
        bot = _FailingFinalEditBot()
        progress = TelegramProgress(bot, _TELEBOT, "100", "RSS 刷新", timeout_seconds=60)
        progress.begin("刷新中")

        self.assertEqual(progress.mode, "edit")
        self.assertTrue(progress.finish("<b>RSS 刷新完成</b>"))
        self.assertEqual(bot.messages[-1][1], "<b>RSS 刷新完成</b>")
        self.assertEqual(bot.deleted, [("100", 21)])
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")

    def test_bind_task_run_updates_only_the_existing_pending_operation(self):
        bot = _EditBot()
        progress = TelegramProgress(bot, _TELEBOT, "100", "光鸭整理", timeout_seconds=60)
        progress.begin("整理中")

        self.assertTrue(progress.bind_task_run("guangya_organize", 42))
        rows = json.loads(db.kv_get("telegram_pending_operations_v1", "[]"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], progress.operation_id)
        self.assertEqual(rows[0]["task_name"], "guangya_organize")
        self.assertEqual(rows[0]["task_run_id"], 42)
        self.assertFalse(progress.bind_task_run("guangya_organize", 0))
        progress.dismiss()

    def test_restart_recovery_uses_exact_organize_task_run_terminal_state(self):
        run_id = db.add_task_run("guangya_organize", "telegram")
        db.finish_task_run(
            run_id,
            "success",
            result=json.dumps({
                "stats": {"total": 3, "moved": 2, "need_confirm": 1, "failed": 0}
            }),
        )
        db.kv_set("telegram_pending_operations_v1", json.dumps([{
            "id": "organize-success",
            "chat_id": "100",
            "label": "光鸭整理",
            "mode": "edit",
            "message_id": 7,
            "task_name": "guangya_organize",
            "task_run_id": run_id,
        }]))

        bot = _EditBot()
        self.assertEqual(recover_stale_operations(bot), 1)
        rendered = bot.edits[-1][2]
        self.assertIn("光鸭整理完成", rendered)
        self.assertIn("视频 3", rendered)
        self.assertIn("已移动 2", rendered)
        self.assertNotIn("已中断", rendered)

    def test_restart_recovery_distinguishes_process_interruption_from_failure(self):
        cases = (
            ("failed", "上次进程在整理任务运行期间中断", "光鸭整理已中断"),
            ("failed", "RuntimeError: provider unavailable", "光鸭整理失败"),
            ("partial", "", "光鸭整理部分完成"),
            ("skipped", "", "光鸭整理已停止"),
        )
        for index, (status, error, expected) in enumerate(cases, start=1):
            with self.subTest(status=status, error=error):
                run_id = db.add_task_run("guangya_organize", "telegram")
                db.finish_task_run(run_id, status, result="{}", error=error)
                db.kv_set("telegram_pending_operations_v1", json.dumps([{
                    "id": f"organize-{index}",
                    "chat_id": "100",
                    "label": "光鸭整理",
                    "mode": "edit",
                    "message_id": index,
                    "task_name": "guangya_organize",
                    "task_run_id": run_id,
                }]))
                bot = _EditBot()
                self.assertEqual(recover_stale_operations(bot), 1)
                self.assertIn(expected, bot.edits[-1][2])

    def test_restart_recovery_clears_native_draft_before_interrupt_notice(self):
        db.kv_set("telegram_pending_operations_v1", (
            '[{"id":"draft-old","chat_id":"100","label":"RSS 刷新",'
            '"mode":"rich_draft","draft_id":99,"started_at":1,"deadline":2}]'
        ))
        bot = _RichBot()
        self.assertEqual(recover_stale_operations(bot, _TELEBOT), 1)
        self.assertEqual(bot.rich_drafts[-1][1:3], (99, ""))
        self.assertIn("已中断", bot.messages[-1][1])
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")

    def test_restart_recovery_falls_back_to_new_message_when_edit_fails(self):
        db.kv_set("telegram_pending_operations_v1", (
            '[{"id":"old-edit","chat_id":"100","label":"RSS 刷新",'
            '"mode":"edit","message_id":7,"started_at":1,"deadline":2}]'
        ))
        bot = _FailingFinalEditBot()

        self.assertEqual(recover_stale_operations(bot), 1)

        self.assertIn("已中断", bot.messages[-1][1])
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")

    def test_startup_recovery_keeps_retrying_until_snapshot_is_delivered(self):
        bot = _EditBot()
        stop_event = SimpleNamespace(
            is_set=Mock(return_value=False),
            wait=Mock(return_value=False),
        )
        with patch(
            "app.bot.progress.recover_stale_operations", side_effect=[0, 0, 1]
        ) as recover, patch(
            "app.bot.progress.pending_stale_operation_count",
            side_effect=[1, 1, 0],
        ) as pending:
            recovered = _recover_stale_progress_until_delivered(
                bot,
                _TELEBOT,
                stop_event,
                operation_ids=("old-operation",),
                delays=(0.01, 0.02),
            )

        self.assertEqual(recovered, 1)
        self.assertEqual(recover.call_count, 3)
        self.assertEqual(pending.call_count, 3)
        self.assertEqual(
            [call.kwargs["operation_ids"] for call in recover.call_args_list],
            [("old-operation",)] * 3,
        )
        self.assertEqual(
            [call.args[0] for call in stop_event.wait.call_args_list],
            [0.1, 0.1],
        )

    def test_init_snapshots_stale_operations_before_starting_recovery_thread(self):
        bot = object()
        events: list[str] = []
        old_stop = __import__("threading").Event()

        class DeferredThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon
                events.append("thread-created")

            def start(self):
                events.append("thread-started")

            def is_alive(self):
                return False

        saved = (
            bot_handlers._bot,
            bot_handlers._registered_bot_id,
            bot_handlers._progress_recovery_thread,
            bot_handlers._progress_recovery_stop,
        )
        bot_handlers._bot = None
        bot_handlers._registered_bot_id = None
        bot_handlers._progress_recovery_thread = None
        bot_handlers._progress_recovery_stop = old_stop
        try:
            with patch.object(bot_handlers, "_configuration_complete", return_value=True), patch.object(
                bot_handlers, "get_bot", return_value=bot
            ), patch.object(
                bot_handlers, "_register_commands", side_effect=lambda *_: events.append("registered")
            ), patch(
                "app.bot.progress.pending_stale_operation_ids",
                side_effect=lambda: events.append("snapshotted") or ("startup-old",),
            ), patch.object(bot_handlers.threading, "Thread", DeferredThread):
                self.assertIs(bot_handlers.init_bot(), bot)

            self.assertEqual(
                events,
                ["registered", "snapshotted", "thread-created", "thread-started"],
            )
            self.assertTrue(old_stop.is_set())
            self.assertIsNot(bot_handlers._progress_recovery_stop, old_stop)
            self.assertFalse(bot_handlers._progress_recovery_stop.is_set())
        finally:
            (
                bot_handlers._bot,
                bot_handlers._registered_bot_id,
                bot_handlers._progress_recovery_thread,
                bot_handlers._progress_recovery_stop,
            ) = saved

    def test_stop_does_not_clear_a_new_recovery_generation(self):
        old_stop = __import__("threading").Event()
        new_stop = __import__("threading").Event()

        class ReplacementThread:
            def is_alive(self):
                return True

        new_thread = ReplacementThread()
        new_thread_stop = __import__("threading").Event()
        new_bot = object()

        class OldThread:
            def is_alive(self):
                return True

            def join(self, timeout):
                bot_handlers._bot = new_bot
                bot_handlers._bot_thread = new_thread
                bot_handlers._bot_thread_stop = new_thread_stop
                bot_handlers._progress_recovery_stop = new_stop
                bot_handlers._progress_recovery_thread = new_thread

        saved = (
            bot_handlers._bot,
            bot_handlers._bot_thread,
            bot_handlers._bot_thread_stop,
            bot_handlers._progress_recovery_thread,
            bot_handlers._progress_recovery_stop,
        )
        bot_handlers._bot = None
        bot_handlers._bot_thread = None
        bot_handlers._bot_thread_stop = None
        bot_handlers._progress_recovery_thread = OldThread()
        bot_handlers._progress_recovery_stop = old_stop
        try:
            with patch(
                "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker"
            ):
                bot_handlers.stop_bot(timeout=0.01)

            self.assertTrue(old_stop.is_set())
            self.assertIs(bot_handlers._bot, new_bot)
            self.assertIs(bot_handlers._bot_thread, new_thread)
            self.assertIs(bot_handlers._bot_thread_stop, new_thread_stop)
            self.assertIs(bot_handlers._progress_recovery_stop, new_stop)
            self.assertIs(bot_handlers._progress_recovery_thread, new_thread)
        finally:
            (
                bot_handlers._bot,
                bot_handlers._bot_thread,
                bot_handlers._bot_thread_stop,
                bot_handlers._progress_recovery_thread,
                bot_handlers._progress_recovery_stop,
            ) = saved

    def test_stop_reports_incomplete_while_old_recovery_generation_is_alive(self):
        class RecoveryThread:
            def __init__(self):
                self.joined = False

            def is_alive(self):
                return True

            def join(self, timeout):
                self.joined = True

        recovery = RecoveryThread()
        recovery_stop = __import__("threading").Event()
        saved = (
            bot_handlers._bot,
            bot_handlers._bot_thread,
            bot_handlers._bot_thread_stop,
            bot_handlers._progress_recovery_thread,
            bot_handlers._progress_recovery_stop,
        )
        bot_handlers._bot = None
        bot_handlers._bot_thread = None
        bot_handlers._bot_thread_stop = None
        bot_handlers._progress_recovery_thread = recovery
        bot_handlers._progress_recovery_stop = recovery_stop
        try:
            with patch(
                "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker"
            ):
                stopped = bot_handlers._stop_bot_locked(
                    timeout=0.01, cancel_operations=False,
                )

            self.assertFalse(stopped)
            self.assertTrue(recovery.joined)
            self.assertTrue(recovery_stop.is_set())
            self.assertIs(bot_handlers._progress_recovery_thread, recovery)
            self.assertIs(bot_handlers._progress_recovery_stop, recovery_stop)
        finally:
            (
                bot_handlers._bot,
                bot_handlers._bot_thread,
                bot_handlers._bot_thread_stop,
                bot_handlers._progress_recovery_thread,
                bot_handlers._progress_recovery_stop,
            ) = saved

    def test_restart_holds_control_lock_across_stop_reset_and_start(self):
        events: list[str] = []

        class TrackingLock:
            def __enter__(self):
                events.append("lock-enter")

            def __exit__(self, exc_type, exc, traceback):
                events.append("lock-exit")

        with patch.object(bot_handlers, "_lifecycle_control_lock", TrackingLock()), patch.object(
            bot_handlers,
            "_stop_bot_locked",
            side_effect=lambda **_kwargs: events.append("stop") or True,
        ), patch(
            "app.notifier.reset", side_effect=lambda: events.append("reset")
        ), patch.object(
            bot_handlers, "_start_bot_locked", side_effect=lambda: events.append("start") or True
        ):
            self.assertTrue(bot_handlers.restart_bot())

        self.assertEqual(
            events,
            ["lock-enter", "stop", "reset", "start", "lock-exit"],
        )

    def test_restart_does_not_start_new_generation_until_old_polling_exits(self):
        with patch.object(bot_handlers, "_stop_bot_locked", return_value=False), patch(
            "app.notifier.reset"
        ) as reset, patch.object(bot_handlers, "_start_bot_locked") as start:
            self.assertFalse(bot_handlers.restart_bot())

        reset.assert_not_called()
        start.assert_not_called()

    def test_polling_supervisor_reconnects_after_unexpected_exception(self):
        class StopEvent:
            def __init__(self):
                self.stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, _timeout):
                return self.stopped

        stop_event = StopEvent()

        class Bot:
            def __init__(self):
                self.calls = 0

            def infinity_polling(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("network")
                stop_event.stopped = True

        bot = Bot()
        with patch.object(bot_handlers, "init_bot", return_value=bot), patch.object(
            bot_handlers, "_ensure_command_menu", return_value=True
        ):
            bot_handlers.start_bot_blocking(stop_event)

        self.assertEqual(bot.calls, 2)

    def test_polling_supervisor_waits_before_reconnect_after_normal_return(self):
        class StopEvent:
            def __init__(self):
                self.stopped = False
                self.waits = []

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                self.waits.append(timeout)
                self.stopped = True
                return True

        stop_event = StopEvent()
        bot = type("Bot", (), {"infinity_polling": lambda self, **_kwargs: None})()
        with patch.object(bot_handlers, "init_bot", return_value=bot), patch.object(
            bot_handlers, "_ensure_command_menu", return_value=True
        ):
            bot_handlers.start_bot_blocking(stop_event)

        self.assertEqual(stop_event.waits, [1.0])

    def test_startup_stop_event_prevents_late_initialization_and_polling(self):
        stop_event = __import__("threading").Event()
        stop_event.set()
        with patch.object(bot_handlers, "init_bot") as initialize:
            bot_handlers.start_bot_blocking(stop_event)
        initialize.assert_not_called()

    def test_init_stop_event_prevents_recovery_generation_creation(self):
        stop_event = __import__("threading").Event()
        stop_event.set()
        with patch.object(bot_handlers, "_configuration_complete") as configured, patch.object(
            bot_handlers, "get_bot"
        ) as get_shared_bot:
            self.assertIsNone(bot_handlers.init_bot(stop_event=stop_event))
        configured.assert_not_called()
        get_shared_bot.assert_not_called()

    def test_recovery_snapshot_does_not_collect_operations_created_after_start(self):
        db.kv_set("telegram_pending_operations_v1", json.dumps([
            {
                "id": "startup-old", "chat_id": "100", "label": "旧任务",
                "mode": "edit", "message_id": 7, "started_at": 1,
                "deadline": 2,
            },
            {
                "id": "current-new", "chat_id": "100", "label": "当前任务",
                "mode": "edit", "message_id": 8, "started_at": 3,
                "deadline": 9999999999,
            },
        ]))
        bot = _EditBot()

        self.assertEqual(
            recover_stale_operations(
                bot, operation_ids=("startup-old",)
            ),
            1,
        )

        remaining = json.loads(db.kv_get("telegram_pending_operations_v1"))
        self.assertEqual([row["id"] for row in remaining], ["current-new"])
        self.assertEqual([edit[1] for edit in bot.edits], [7])

    def test_restart_recovery_marks_stale_edit_operation_interrupted(self):
        db.kv_set("telegram_pending_operations_v1", (
            '[{"id":"old","chat_id":"100","label":"资源搜索：测试",'
            '"mode":"edit","message_id":7,"started_at":1,"deadline":2}]'
        ))
        bot = _EditBot()
        self.assertEqual(recover_stale_operations(bot), 1)
        self.assertIn("已中断", bot.edits[-1][2])
        self.assertEqual(db.kv_get("telegram_pending_operations_v1"), "[]")
