from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.bot.handlers import (
    _dispatch_resource_download,
    _handle_resource_search_callback,
    _handle_write_confirmation_callback,
    _resource_search_view,
    _resource_target_view,
)
from app.modules.telegram_resource_search import (
    TelegramIndexerWorker,
    TelegramResourceSearchError,
    TelegramResourceSearchStore,
)


class _Markup:
    def __init__(self, row_width=2):
        self.row_width = row_width
        self.buttons = []

    def add(self, *buttons):
        self.buttons.extend(buttons)


_TELEBOT = SimpleNamespace(types=SimpleNamespace(
    InlineKeyboardMarkup=_Markup,
    InlineKeyboardButton=lambda text, callback_data: SimpleNamespace(
        text=text, callback_data=callback_data,
    ),
))


def _items():
    return [
        {
            "result_id": "r1",
            "site_id": "nyaa",
            "site_name": "Nyaa",
            "title": "作品 A [1080p]",
            "size_text": "1.2 GB",
            "seeders": 18,
            "published_at": "2026-08-04T01:00:00+00:00",
        },
        {
            "result_id": "r2",
            "site_id": "mikan",
            "site_name": "Mikan",
            "title": "作品 A 第 2 集",
            "size_text": "900 MB",
            "seeders": 4,
            "published_at": "2026-08-03T01:00:00+00:00",
        },
    ]


def _sites():
    return [
        {"site_id": "nyaa", "site_name": "Nyaa", "status": "success", "count": 1, "message": ""},
        {"site_id": "mikan", "site_name": "Mikan", "status": "success", "count": 1, "message": ""},
        {"site_id": "1lou", "site_name": "1Lou", "status": "error", "count": 0, "message": "响应超时"},
    ]


class TelegramIndexerWorkerTests(unittest.TestCase):
    def test_search_rejects_disabled_indexer_before_starting_worker(self):
        worker = TelegramIndexerWorker()

        with (
            patch(
                "app.modules.telegram_resource_search.config.get_bool",
                return_value=False,
            ),
            patch.object(worker, "_call") as call,
        ):
            with self.assertRaisesRegex(TelegramResourceSearchError, "资源站搜索当前已关闭"):
                worker.search("Frieren")

        call.assert_not_called()
        self.assertIsNone(worker._thread)

    def test_call_closes_coroutine_when_threadsafe_submission_fails(self):
        worker = TelegramIndexerWorker()
        worker._loop = Mock()
        worker._service = object()
        captured = []

        async def pending():
            return "unused"

        def factory(_service):
            coroutine = pending()
            captured.append(coroutine)
            return coroutine

        with patch.object(worker, "_ensure_started"), patch(
            "app.modules.telegram_resource_search.asyncio.run_coroutine_threadsafe",
            side_effect=RuntimeError("loop stopped"),
        ):
            with self.assertRaisesRegex(RuntimeError, "loop stopped"):
                worker._call(factory, timeout=0.1)

        self.assertEqual(len(captured), 1)
        self.assertIsNone(captured[0].cr_frame)

    def test_close_failure_keeps_worker_service_and_loop_for_retry(self):
        worker = TelegramIndexerWorker()

        class Service:
            def __init__(self):
                self.close_calls = 0

            async def aclose(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("close failed once")

        service = Service()
        with patch(
            "app.modules.telegram_resource_search.build_indexer_service",
            return_value=service,
        ):
            worker._ensure_started()
            thread = worker._thread
            loop = worker._loop
            self.assertIsNotNone(thread)
            self.assertIsNotNone(loop)

            self.assertFalse(worker.stop(timeout=0.05))
            deadline = time.monotonic() + 1.0
            while service.close_calls < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(service.close_calls, 1)
            self.assertIs(worker._thread, thread)
            self.assertIs(worker._loop, loop)
            self.assertIs(worker._service, service)
            self.assertTrue(thread.is_alive())
            self.assertTrue(worker._stopping)

            factory = Mock()
            with self.assertRaisesRegex(
                TelegramResourceSearchError, "正在关闭"
            ):
                worker._call(factory, timeout=0.1)
            factory.assert_not_called()

            self.assertTrue(worker.stop(timeout=1.0))

        self.assertEqual(service.close_calls, 2)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker._loop)
        self.assertIsNone(worker._service)
        self.assertFalse(worker._stopping)


class TelegramResourceSearchStoreTests(unittest.TestCase):
    def test_session_and_actions_are_owner_bound_and_expire(self):
        now = [10.0]
        store = TelegramResourceSearchStore(
            ttl_seconds=30, max_sessions=2, max_actions=10, clock=lambda: now[0]
        )
        session_id = store.create_session(
            chat_id="100", user_id="9", query="作品 A", items=_items(), sites=_sites()
        )
        action_id = store.create_action(
            session_id, "100", "9", "view", {"site_id": "nyaa", "page": 0}
        )
        self.assertEqual(store.resolve_action(action_id, "100", "9")["kind"], "view")
        with self.assertRaisesRegex(TelegramResourceSearchError, "不属于"):
            store.resolve_action(action_id, "100", "10")
        now[0] = 41.0
        with self.assertRaisesRegex(TelegramResourceSearchError, "过期"):
            store.snapshot(session_id, "100", "9")


class TelegramResourceSearchViewTests(unittest.TestCase):
    def setUp(self):
        self.store = TelegramResourceSearchStore()
        self.session_id = self.store.create_session(
            chat_id="100", user_id="9", query="作品 A", items=_items(), sites=_sites()
        )

    def test_site_filter_changes_visible_resources_and_keeps_error_reason(self):
        text, markup = _resource_search_view(
            _TELEBOT,
            self.session_id,
            chat_id="100",
            user_id="9",
            site_id="nyaa",
            store=self.store,
        )
        self.assertIn("<b>作品 A</b> · Nyaa · 1 项", text)
        self.assertIn("作品 A [1080p]", text)
        self.assertNotIn("作品 A 第 2 集", text)
        self.assertIn("1Lou：响应超时", text)
        self.assertTrue(any(button.text.startswith("✓ Nyaa") for button in markup.buttons))
        self.assertTrue(all(len(button.callback_data) <= 64 for button in markup.buttons))

    def test_source_callback_replaces_message_with_filtered_results(self):
        action_id = self.store.create_action(
            self.session_id, "100", "9", "view", {"site_id": "mikan", "page": 0}
        )

        class Bot:
            def __init__(self):
                self.answers = []
                self.edits = []

            def answer_callback_query(self, *args, **kwargs):
                self.answers.append((args, kwargs))

            def edit_message_text(self, *args, **kwargs):
                self.edits.append((args, kwargs))

        bot = Bot()
        call = SimpleNamespace(
            id="cb",
            data=f"mrs:{action_id}",
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=77),
        )
        with patch(
            "app.modules.telegram_resource_search.get_telegram_resource_search_store",
            return_value=self.store,
        ):
            _handle_resource_search_callback(bot, call, _TELEBOT)
        self.assertIn("<b>作品 A</b> · Mikan · 1 项", bot.edits[0][0][0])
        self.assertIn("作品 A 第 2 集", bot.edits[0][0][0])
        self.assertNotIn("作品 A [1080p]", bot.edits[0][0][0])
        self.assertEqual(bot.answers[0][0][1], "已切换资源范围")

    def test_target_picker_uses_qb_guangya_both_and_back_actions(self):
        text, markup = _resource_target_view(
            _TELEBOT,
            self.session_id,
            "r1",
            chat_id="100",
            user_id="9",
            site_id="nyaa",
            page=0,
            store=self.store,
        )
        self.assertIn("选择下载目标", text)
        self.assertEqual(
            [button.text for button in markup.buttons],
            ["qBittorrent", "光鸭云盘", "全部", "返回资源列表"],
        )
        resolved = [
            self.store.resolve_action(button.callback_data.split(":", 1)[1], "100", "9")
            for button in markup.buttons[:3]
        ]
        self.assertEqual([item["value"]["target"] for item in resolved], ["qb", "guangya", "both"])
        with self.assertRaisesRegex(TelegramResourceSearchError, "过期"):
            self.store.resolve_action(
                markup.buttons[0].callback_data.split(":", 1)[1], "100", "9"
            )

    def test_download_action_requires_single_use_confirmation_before_dispatch(self):
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )

        reset_telegram_write_confirmation_store_for_tests()
        _text, markup = _resource_target_view(
            _TELEBOT,
            self.session_id,
            "r1",
            chat_id="100",
            user_id="9",
            store=self.store,
        )
        download_action = markup.buttons[0].callback_data

        class Bot:
            def __init__(self):
                self.answers = []
                self.edits = []

            def answer_callback_query(self, *args, **kwargs):
                self.answers.append((args, kwargs))

            def edit_message_text(self, *args, **kwargs):
                self.edits.append((args, kwargs))

        bot = Bot()
        call = SimpleNamespace(
            id="choose-download",
            data=download_action,
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=77),
        )
        with patch(
            "app.modules.telegram_resource_search.get_telegram_resource_search_store",
            return_value=self.store,
        ), patch("app.bot.handlers._dispatch_resource_download") as dispatch:
            _handle_resource_search_callback(bot, call, _TELEBOT)
        dispatch.assert_not_called()
        self.assertIn("确认提交资源下载", bot.edits[-1][0][0])
        confirmation_markup = bot.edits[-1][1]["reply_markup"]
        confirmation_data = confirmation_markup.buttons[0].callback_data

        confirmation_call = SimpleNamespace(
            id="confirm-download",
            data=confirmation_data,
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=77),
        )
        with patch("app.bot.handlers.threading.Thread") as thread:
            _handle_write_confirmation_callback(bot, confirmation_call, _TELEBOT)
            thread.assert_called_once()
            self.assertIs(
                thread.call_args.kwargs["target"],
                _dispatch_resource_download,
            )
            self.assertEqual(
                thread.call_args.kwargs["args"],
                (bot, 100, 77, "r1", "qb", "9"),
            )

            _handle_write_confirmation_callback(bot, confirmation_call, _TELEBOT)
            thread.assert_called_once()
        self.assertTrue(bot.answers[-1][1]["show_alert"])
        self.assertIn("已处理", bot.answers[-1][0][1])

    def test_titles_are_entity_normalized_escaped_and_bounded(self):
        long_title = "作品 &amp; 特别篇 <script>alert(1)</script> " + "超长描述" * 30
        session_id = self.store.create_session(
            chat_id="100",
            user_id="9",
            query="作品 &amp; 特别篇",
            items=[{
                "result_id": "entity",
                "site_id": "nyaa",
                "site_name": "Nyaa",
                "title": long_title,
                "size_text": "1.2 GB",
                "seeders": 3,
                "published_at": "2026-08-04T01:00:00+00:00",
            }],
            sites=[{
                "site_id": "nyaa", "site_name": "Nyaa",
                "status": "success", "count": 1, "message": "",
            }],
        )
        text, markup = _resource_search_view(
            _TELEBOT,
            session_id,
            chat_id="100",
            user_id="9",
            store=self.store,
        )
        self.assertIn("作品 &amp; 特别篇", text)
        self.assertNotIn("&amp;amp;", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertIn("…", text)
        resource_button = next(button for button in markup.buttons if button.text.startswith("1. "))
        self.assertIn("作品 & 特别篇", resource_button.text)
        self.assertLessEqual(len(resource_button.text), 37)


class TelegramResourceSearchCommandTests(unittest.TestCase):
    def test_group_resource_search_requires_allowed_user_but_private_chat_stays_compatible(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        class Bot(TelegramBotTests.FakeBot):
            def __init__(self):
                super().__init__()
                self.answers = []

            def answer_callback_query(self, *args, **kwargs):
                self.answers.append((args, kwargs))

        bot = Bot()
        values = {"TG_CHAT_ID": "-100", "TG_AGENT_ALLOWED_USER_IDS": "9"}
        with patch("app.bot.handlers.get", side_effect=lambda key, default="": values.get(key, default)), patch(
            "app.bot.agent_adapter.get", side_effect=lambda key, default="": values.get(key, default)
        ):
            denied = SimpleNamespace(
                chat=SimpleNamespace(id=-100),
                from_user=SimpleNamespace(id=10),
                text="/media_search 作品 A",
            )
            self.assertTrue(handlers._reject_unauthorized_resource_search(bot, denied))
            self.assertEqual(bot.replies[-1][1], "你无权在此群组使用资源搜索")
            allowed = SimpleNamespace(
                chat=SimpleNamespace(id=-100),
                from_user=SimpleNamespace(id=9),
                text="/media_search 作品 A",
            )
            self.assertFalse(handlers._reject_unauthorized_resource_search(bot, allowed))

        private_values = {"TG_CHAT_ID": "100", "TG_AGENT_ALLOWED_USER_IDS": ""}
        with patch("app.bot.handlers.get", side_effect=lambda key, default="": private_values.get(key, default)):
            private = SimpleNamespace(
                chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=10),
                text="/media_search 作品 A",
            )
            self.assertFalse(handlers._reject_unauthorized_resource_search(bot, private))

    def test_group_resource_callback_rechecks_allowlist_before_resolving_action(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        class Bot(TelegramBotTests.FakeBot):
            def __init__(self):
                super().__init__()
                self.answers = []

            def answer_callback_query(self, *args, **kwargs):
                self.answers.append((args, kwargs))

        bot = Bot()
        telebot = TelegramBotTests._telebot_types()
        values = {"TG_CHAT_ID": "-100", "TG_AGENT_ALLOWED_USER_IDS": "8"}
        with patch("app.bot.handlers.get", side_effect=lambda key, default="": values.get(key, default)), patch(
            "app.bot.agent_adapter.get", side_effect=lambda key, default="": values.get(key, default)
        ):
            handlers._register_commands(bot, telebot)
            callback = next(
                handler for filters, handler in bot.callback_handlers
                if filters["func"](SimpleNamespace(data="mrs:opaque"))
            )
            call = SimpleNamespace(
                id="cb",
                data="mrs:opaque",
                from_user=SimpleNamespace(id=9),
                message=SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=7),
            )
            with patch("app.bot.handlers._handle_resource_search_callback") as inner:
                callback(call)
        inner.assert_not_called()
        self.assertEqual(bot.answers[-1][0][1], "你无权在此群组使用资源搜索")
        self.assertTrue(bot.answers[-1][1]["show_alert"])

    def test_chinese_command_and_media_search_command_are_registered(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        with patch("app.bot.handlers.get", side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default):
            handlers._register_commands(bot, telebot)
        commands = [filters.get("commands") for filters, _handler in bot.message_handlers]
        self.assertIn(["media_search"], commands)
        self.assertTrue(any(filters.get("func") for filters, _handler in bot.message_handlers))

    def test_media_search_without_query_replies_with_safe_actionable_example(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default,
        ):
            handlers._register_commands(bot, telebot)
            command_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["media_search"]
            )
            command_handler(SimpleNamespace(
                chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=9),
                text="/media_search",
            ))

        reply = bot.replies[-1][1]
        self.assertIn("请输入要搜索的媒体名称", reply)
        self.assertIn("/media_search 光阴之外", reply)
        self.assertIn("下载第 2 个", reply)
        self.assertNotIn("<媒体名称>", reply)

    def test_media_search_fallback_matches_bot_username_suffix(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default,
        ):
            handlers._register_commands(bot, telebot)
            filters, handler = next(
                (filters, handler) for filters, handler in bot.message_handlers
                if filters.get("content_types") == ["text"]
                and callable(filters.get("func"))
                and filters["func"](SimpleNamespace(text="/media_search@Tencentcoding_bot"))
            )
            self.assertTrue(filters["func"](
                SimpleNamespace(text="/media_search@Tencentcoding_bot")
            ))
            handler(SimpleNamespace(
                chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=9),
                text="/media_search@Tencentcoding_bot",
            ))

        self.assertIn("请输入要搜索的媒体名称", bot.replies[-1][1])

    def test_start_help_groups_commands_and_formats_chinese_alias_consistently(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        with patch("app.bot.handlers.get", side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default):
            handlers._register_commands(bot, telebot)
            start = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["start"]
            )
            start(SimpleNamespace(chat=SimpleNamespace(id=100)))
        help_text = bot.replies[-1][1]
        self.assertIn("<b>资源搜索</b>", help_text)
        self.assertIn("/media_search 片名 — 搜索媒体资源", help_text)
        self.assertIn("/媒体搜索 片名 — 搜索媒体资源", help_text)
        self.assertNotIn(" - ", help_text)


if __name__ == "__main__":
    unittest.main()
